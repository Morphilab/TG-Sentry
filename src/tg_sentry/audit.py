"""Auditoría JSONL append-only con rotación.

Estados (no colapsar, son contractuales):
- done               — paso ejecutado con éxito.
- skipped_ok         — ya estaba hecho POR NOSOTROS (idempotencia real).
- skipped_unreachable — nunca accesible / peer inválido. NO es éxito.
- skipped_external   — borrado/cambiado por terceros.
- failed             — fallo con razón.

El archivo activo NUNCA se trunca: al superar rotate_bytes se RENOMBRA
primero (atómico: los appends concurrentes caen en el activo nuevo, nada
se pierde — auditoría 2026-09) y luego se comprime (gzip) con fsync.
cleanup() borra los rotados más viejos que keep_days. read() devuelve todos
los registros (activo + rotados + rotados a medio comprimir) por ts, y
jamás revienta por un contenedor o línea corruptos: los descarta con log
(se presupone conservador en el conteo de límites).
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import os
import shutil
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ValidationError

import tg_sentry.paths as paths

logger = logging.getLogger(__name__)

AUDIT_FILENAME = "audit.jsonl"
LOCK_FILENAME = "audit.lock"

try:  # flock es POSIX; en otro SO la serialización no está disponible
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class StepOutcome(StrEnum):
    DONE = "done"
    SKIPPED_OK = "skipped_ok"
    SKIPPED_UNREACHABLE = "skipped_unreachable"
    SKIPPED_EXTERNAL = "skipped_external"
    FAILED = "failed"


class AuditRecord(BaseModel):
    ts: datetime
    plan_id: str
    peer_id: int
    step: str
    outcome: StepOutcome
    reason: str | None = None


class AuditLog:
    def __init__(
        self,
        directory: Path | None = None,
        *,
        rotate_bytes: int = 10 * 1024 * 1024,
        keep_days: int = 90,
    ) -> None:
        self._directory = directory if directory is not None else paths.audit_dir()
        self._rotate_bytes = rotate_bytes
        self._keep_days = keep_days
        self._cleanup_hecho = False
        # caché de lectura (2ª auditoría: el motor relee TODO el historial en
        # cada paso y la TUI en cada tecla): huella (nombre, mtime, tamaño) de
        # cada archivo; cualquier append/rotación de CUALQUIER proceso la
        # invalida. record() la invalida explícitamente.
        self._cache_huella: tuple | None = None
        self._cache_registros: list[AuditRecord] | None = None

    @property
    def active_file(self) -> Path:
        return self._directory / AUDIT_FILENAME

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        """Serializa record()/rotación ENTRE PROCESOS (2ª auditoría: la
        carrera CLI+TUI podía perder el registro del outcome — dirección
        peligrosa para el presupuesto). flock es por file descriptor, así
        que no hay re-entrada: _rotate se llama SIEMPRE bajo este lock."""
        if fcntl is None:  # pragma: no cover — SO no-POSIX
            yield
            return
        self._directory.mkdir(parents=True, exist_ok=True)
        handle = open(  # noqa: SIM115 — el handle vive hasta el finally
            self._directory / LOCK_FILENAME, "w"
        )
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def record(self, entry: AuditRecord) -> None:
        """Añade un registro (flush+fsync: cada done es un compromiso);
        rota antes si el activo supera el umbral. Aplica retención una vez."""
        self._directory.mkdir(parents=True, exist_ok=True)
        self._directory.chmod(0o700)
        if not self._cleanup_hecho:
            # retención real (antes cleanup() era código muerto — auditoría
            # 2026-09); con fallo silencioso: limpiar no puede romper el registro
            self._cleanup_hecho = True
            try:
                self.cleanup()
            except OSError:
                logger.warning("no se pudo aplicar la retención de auditoría")
        with self._lock():
            if (
                self.active_file.exists()
                and self.active_file.stat().st_size >= self._rotate_bytes
            ):
                self._rotate()
            # 0600 DESDE LA CREACIÓN (3ª auditoría: open("a")+chmod posterior
            # dejaba una ventana con peer_ids a 0644); el chmod posterior
            # repara además un activo legacy preexistente holgado
            fd = os.open(
                self.active_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
            )
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(entry.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.active_file.chmod(0o600)
        # el cache de lectura NO puede servir registros previos a este append
        self._cache_huella = None
        self._cache_registros = None

    def _rotate(self) -> None:
        """Rotación ATÓMICA y serializada por _lock (auditoría 2026-09):

        1. rename del activo → temporal (los appends de otros procesos ya
           escriben en un activo NUEVO: ningún registro en vuelo se destruye).
        2. compresión del temporal → .gz TEMPORAL con fsync y rename atómico
           (2ª auditoría: un crash entre compresión y unlink dejaba el mismo
           contenido en .gz y .rotando → doble conteo; y un read() concurrent
           podía abrir un .gz a medio escribir).
        3. unlink del temporal (con fsync del dir).

        FileNotFoundError en el rename: otro proceso rotó justo antes (carrera
        residual sin lock disponible): el activo nuevo ya recibe los appends.
        """
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f") + f"-{os.getpid()}"
        comprimido = self._directory / f"audit-{stamp}.jsonl.gz"
        renombrado = self._directory / f"{AUDIT_FILENAME}.{stamp}.rotando"
        try:
            self.active_file.rename(renombrado)
        except FileNotFoundError:
            return  # ya rotado por otro proceso: no hay nada que hacer aquí
        paths.fsync_dir(self._directory)
        # .gz nace 0600 (3ª auditoría: gzip.open lo creaba con umask y todo
        # el histórico rotado quedaba 0644 de por vida)
        temporal = comprimido.with_suffix(".gz.tmp")
        try:
            fd = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with (
                os.fdopen(fd, "wb") as raw,
                gzip.GzipFile(fileobj=raw, mode="wb") as target,
                renombrado.open("rb") as source,
            ):
                shutil.copyfileobj(source, target, length=1024 * 256)
                raw.flush()
                os.fsync(raw.fileno())
            temporal.replace(comprimido)
        except OSError:
            # CONSERVAR la copia .rotando: read() la tolera y cleanup la retira
            # tras 1h. Borrarla destruiría la ÚNICA copia de los registros
            # (3ª auditoría: un OSError de la compresión — disco lleno, EIO —
            # perdía el histórico y degradaba el conteo de límites hacia lo
            # peligroso). El .gz.tmp huérfano sí se retira.
            with contextlib.suppress(OSError):
                temporal.unlink(missing_ok=True)
            logger.exception(
                "rotación de auditoría falló: se conserva %s (reintentará al "
                "superar el umbral de nuevo)",
                renombrado.name,
            )
            raise
        renombrado.unlink(missing_ok=True)
        paths.fsync_dir(self._directory)

    def cleanup(self, now: datetime | None = None) -> int:
        """Borra rotados más viejos que keep_days; devuelve cuántos.

        Incluye `.rotando` huérfanos (crash entre rename y compresión):
        antes se re-leían para siempre y violaban la retención (2ª auditoría).
        Edad > 1h: una rotación en vuelo real nunca tarda tanto."""
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:  # naive → UTC (auditoría 2026-09)
            reference = reference.replace(tzinfo=UTC)
        cutoff = reference - timedelta(days=self._keep_days)
        removed = 0
        for rotated in self._directory.glob("audit-*.jsonl.gz"):
            if datetime.fromtimestamp(rotated.stat().st_mtime, UTC) < cutoff:
                rotated.unlink()
                removed += 1
        for huerfano in self._directory.glob(f"{AUDIT_FILENAME}.*.rotando"):
            edad = datetime.fromtimestamp(huerfano.stat().st_mtime, UTC)
            if edad < cutoff or (reference - edad) > timedelta(hours=1):
                huerfano.unlink()
                removed += 1
        return removed

    def _huella(self) -> tuple:
        """Huella del contenido del directorio de auditoría (ver read())."""
        huellas: list[tuple] = []
        try:
            entradas = sorted(self._directory.iterdir())
        except OSError:
            return ()
        for path in entradas:
            if not (
                path.name.startswith("audit-")
                or path.name == AUDIT_FILENAME
                # rotaciones a medio hacer (.rotando): si no entran en la
                # huella, un read() concurrente a _rotate cachea el doble
                # conteo y lo sirve estable (3ª auditoría)
                or path.name.startswith(f"{AUDIT_FILENAME}.")
            ):
                continue
            try:
                st = path.stat()
                huellas.append((path.name, st.st_mtime_ns, st.st_size))
            except OSError:
                huellas.append((path.name, -1, -1))
        return tuple(huellas)

    def read(self) -> list[AuditRecord]:
        """Todos los registros (rotados + activo + rotaciones a medio hacer)
        ordenados por ts. NUNCA revienta por datos corruptos: descarta con
        log — un descarte puede BAJAR el conteo de límites, así que se hace
        visible (auditoría 2026-09).

        Caché por huella del directorio: si ningún archivo cambió desde la
        última lectura estable, se sirve la lista previa (el motor lee en
        CADA paso y la TUI en cada tecla). Escritura concurrente detectada
        (huella inestable entre antes/después) → sin cachear, conservador."""
        if (
            self._cache_registros is not None
            and self._cache_huella == self._huella()
        ):
            return list(self._cache_registros)
        for _ in range(2):
            antes = self._huella()
            records = self._leer_todo()
            despues = self._huella()
            if antes == despues:
                self._cache_huella = despues
                self._cache_registros = records
                break
        return records

    def _leer_todo(self) -> list[AuditRecord]:
        records: list[AuditRecord] = []
        for path in sorted(self._directory.glob("audit-*.jsonl.gz")):
            try:
                with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                    records.extend(self._parse(handle))
            except (OSError, EOFError, zlib.error, UnicodeDecodeError) as exc:
                # .gz truncado o con bit-rot (corte de luz durante rotación):
                # los registros del resto del sistema siguen siendo legibles
                logger.warning("rotado ilegible %s: %s (descartado)", path.name, exc)
        for path in sorted(self._directory.glob(f"{AUDIT_FILENAME}.*.rotando")):
            # rotación interrumpida entre rename y compresión: contenido válido
            try:
                with path.open(encoding="utf-8", errors="replace") as handle:
                    records.extend(self._parse(handle))
            except OSError as exc:
                logger.warning("rotación incompleta %s: %s", path.name, exc)
        if self.active_file.exists():
            try:
                with self.active_file.open(encoding="utf-8", errors="replace") as handle:
                    records.extend(self._parse(handle))
            except OSError as exc:
                logger.warning("audit activo ilegible: %s", exc)
        records.sort(key=lambda record: record.ts)
        return records

    @staticmethod
    def _parse(handle) -> list[AuditRecord]:  # type: ignore[no-untyped-def]
        records = []
        for numero, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = AuditRecord.model_validate(json.loads(line))
            except json.JSONDecodeError as exc:
                # línea a medio escribir (solo puede ser la última) o bit-rot:
                # VISIBLE — un descarte silencioso baja el conteo de límites
                # (3ª auditoría: el docstring prometía log y el código tragaba)
                logger.warning(
                    "auditoría línea %d descartada (JSON roto): %s", numero, exc
                )
                continue
            except ValidationError as exc:
                # registro válido como JSON pero no como schema: visible,
                # porque degrada el conteo de límites (auditoría 2026-09)
                logger.warning(
                    "auditoría línea %d descartada (schema no validable): %s",
                    numero,
                    exc,
                )
                continue
            if record.ts.tzinfo is None:
                # ts naive (edición manual, herramienta externa): sin tz el
                # sort con aware revienta entero — se normaliza a UTC
                record = record.model_copy(
                    update={"ts": record.ts.replace(tzinfo=UTC)}
                )
            records.append(record)
        return records


def record_outcome(
    plan_id: str,
    peer_id: int,
    step: str,
    outcome: StepOutcome,
    reason: str | None = None,
    audit: AuditLog | None = None,
) -> AuditRecord:
    """Registra un resultado y lo persiste en la auditoría por defecto."""
    entry = AuditRecord(
        ts=datetime.now(UTC),
        plan_id=plan_id,
        peer_id=peer_id,
        step=step,
        outcome=outcome,
        reason=reason,
    )
    (audit if audit is not None else AuditLog()).record(entry)
    return entry
