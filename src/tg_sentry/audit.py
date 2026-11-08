"""Auditoría JSONL append-only con rotación.

Estados (no colapsar, son contractuales):
- done — paso ejecutado con éxito.
- skipped_ok — ya estaba hecho POR NOSOTROS (idempotencia real).
- skipped_unreachable — nunca accesible / peer inválido. NO es éxito.
- skipped_external — borrado/cambiado por terceros.
- failed — fallo con razón.

El archivo activo NUNCA se trunca: al superar rotate_bytes se RENOMBRA
primero (atómico: los appends concurrentes caen en el activo nuevo, nada
se pierde — robustez) y luego se comprime (gzip) con fsync.
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
        # caché de lectura (robustez: el motor relee TODO el historial en
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
        """Serializa record()/rotación ENTRE PROCESOS (robustez: la
        carrera CLI+TUI podía perder el registro del outcome — dirección
        peligrosa para el presupuesto). flock es por file descriptor, así
        que no hay re-entrada: _rotate se llama SIEMPRE bajo este lock."""
        if fcntl is None:  # pragma: no cover — SO no-POSIX
            yield
            return
        # ensure_private_dir (no mkdir crudo): los ANCESTROS nuevos nacen
        # 0700, no con umask 0755 (robustez — convención de paths.py)
        paths.ensure_private_dir(self._directory)
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
        paths.ensure_private_dir(self._directory)
        if not self._cleanup_hecho:
            # retención real (antes cleanup() no tenía efecto); con fallo
            # silencioso: limpiar no puede romper el registro
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
            # 0600 DESDE LA CREACIÓN (robustez: open("a")+chmod posterior
            # dejaba una ventana con peer_ids a 0644); el chmod posterior
            # repara además un activo legacy preexistente holgado.
            # O_NOFOLLOW (robustez): un symlink plantado en audit.jsonl
            # desviaría los peer_ids a un archivo en ubicación arbitraria
            # (misma defensa que export.abrir_0600).
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.active_file, flags, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(entry.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.active_file.chmod(0o600)
        # el cache de lectura NO puede servir registros previos a este append
        self._cache_huella = None
        self._cache_registros = None

    def _comprimir(self, renombrado: Path, comprimido: Path) -> None:
        """Comprime renombrado→comprimido (tmp+fsync+replace atómicos).

        OSError: el .rotando queda como ÚNICA copia — se conserva (read()
        lo tolera) y la PRÓXIMA rotación lo reintenta; cleanup() no lo
        retira sin su .gz compañero (robustez: el log prometía un
        reintento inexistente y cleanup destruía la única copia a 1h,
        degradando el conteo de topes hacia lo peligroso).
        """
        temporal = comprimido.with_suffix(".gz.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            # un symlink plantado desviaría el histórico a ubicación
            # arbitraria (robustez, defensa en profundidad como en
            # export.abrir_0600)
            flags |= os.O_NOFOLLOW
        try:
            # .gz nace 0600 (robustez: gzip.open lo creaba con umask y
            # todo el histórico rotado quedaba 0644 de por vida)
            fd = os.open(temporal, flags, 0o600)
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
            # CONSERVAR la copia .rotando: borrarla destruiría la ÚNICA
            # copia de los registros (robustez: un OSError de la
            # compresión — disco lleno, EIO — perdía el histórico). El
            # .gz.tmp huérfano sí se retira.
            with contextlib.suppress(OSError):
                temporal.unlink(missing_ok=True)
            logger.exception(
                "compresión de auditoría falló: se conserva %s (la próxima "
                "rotación reintenta)",
                renombrado.name,
            )
            raise
        renombrado.unlink(missing_ok=True)
        paths.fsync_dir(self._directory)

    def _rotate(self) -> None:
        """Rotación ATÓMICA y serializada por _lock (robustez):

        0. reintenta .rotando de una rotación fallida anterior (conservados
           como única copia — robustez: el reintento prometido no
           existía y cleanup los retiraba a 1h).
        1. rename del activo → temporal (los appends de otros procesos ya
           escriben en un activo NUEVO: ningún registro en vuelo se destruye).
        2. compresión del temporal → .gz TEMPORAL con fsync y rename atómico
           (robustez: un crash entre compresión y unlink dejaba el mismo
           contenido en .gz y .rotando → doble conteo; y un read() concurrent
           podía abrir un .gz a medio escribir).
        3. unlink del temporal (con fsync del dir).

        FileNotFoundError en el rename: otro proceso rotó justo antes (carrera
        residual sin lock disponible): el activo nuevo ya recibe los appends.
        """
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f") + f"-{os.getpid()}"
        comprimido = self._directory / f"audit-{stamp}.jsonl.gz"
        renombrado = self._directory / f"{AUDIT_FILENAME}.{stamp}.rotando"
        for pendiente in sorted(
            self._directory.glob(f"{AUDIT_FILENAME}.*.rotando")
        ):
            sello = pendiente.name.split(".")[-2]
            with contextlib.suppress(OSError):
                self._comprimir(
                    pendiente, self._directory / f"audit-{sello}.jsonl.gz"
                )
        try:
            self.active_file.rename(renombrado)
        except FileNotFoundError:
            return  # ya rotado por otro proceso: no hay nada que hacer aquí
        paths.fsync_dir(self._directory)
        self._comprimir(renombrado, comprimido)

    def cleanup(self, now: datetime | None = None) -> int:
        """Borra rotados más viejos que keep_days; devuelve cuántos.

        `.rotando` huérfanos: SOLO se retiran si existe su .gz compañero
        (redundantes — crash entre replace y unlink) o si sus registros ya
        pasaron la retención. La regla de 1h de las auditorías 2-5 DESTRUÍA
        la única copia de una compresión fallida a la primera hora
        (robustez: degradación del conteo de topes hacia lo peligroso;
        ahora _rotate reintenta la compresión y este método la respeta).
        También retira `.gz.tmp` huérfanos (crash duro —SIGKILL/corte de
        luz— a mitad de compresión: el unlink del except nunca corre — 5ª
        auditoría; su contenido parcial NO entra en read(), solo acumulaba
        basura).
        """
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:  # naive → UTC (robustez)
            reference = reference.replace(tzinfo=UTC)
        cutoff = reference - timedelta(days=self._keep_days)
        removed = 0
        for rotated in self._directory.glob("audit-*.jsonl.gz"):
            if datetime.fromtimestamp(rotated.stat().st_mtime, UTC) < cutoff:
                rotated.unlink()
                removed += 1
        for huerfano in self._directory.glob(f"{AUDIT_FILENAME}.*.rotando"):
            edad = datetime.fromtimestamp(huerfano.stat().st_mtime, UTC)
            if edad < cutoff:
                # los registros ya pasaron la retención: la copia muere con
                # su contenido, haya o no .gz compañero
                huerfano.unlink()
                removed += 1
                continue
            sello = huerfano.name.split(".")[-2]
            compañero = self._directory / f"audit-{sello}.jsonl.gz"
            if compañero.exists():
                # compresión completada pero crash antes del unlink: el
                # .rotando es redundante (doble conteo en read()) → retirar
                huerfano.unlink()
                removed += 1
            # sin compañero y dentro de la retención: ÚNICA copia — se
            # conserva; _rotate la reintenta en la próxima rotación
        for temporal in self._directory.glob("*.jsonl.gz.tmp"):
            edad = datetime.fromtimestamp(temporal.stat().st_mtime, UTC)
            if edad < cutoff or (reference - edad) > timedelta(hours=1):
                temporal.unlink()
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
                # conteo y lo sirve estable (robustez)
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
        visible (robustez).

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
        """Parseo INCREMENTAL por bloques: la pérdida ante bit-rot queda
        acotada al ÚLTIMO bloque de lectura, no al archivo entero (6ª
        auditoría: un .gz dañado a mitad descartaba TODO el histórico y el
        conteo de topes bajaba en silencio)."""
        records: list[AuditRecord] = []
        pendiente = ""
        try:
            while True:
                chunk = handle.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, bytes):  # defensivo: handles binarios
                    chunk = chunk.decode("utf-8", errors="replace")
                pendiente += chunk
                while "\n" in pendiente:
                    linea, pendiente = pendiente.split("\n", 1)
                    registro = AuditLog._parse_linea(linea)
                    if registro is not None:
                        records.append(registro)
            if pendiente.strip():
                registro = AuditLog._parse_linea(pendiente)
                if registro is not None:
                    records.append(registro)
        except (EOFError, zlib.error, OSError) as exc:
            logger.warning(
                "lectura de auditoría cortada a mitad (%s): se conservan %d "
                "registros del prefijo",
                exc,
                len(records),
            )
        return records

    @staticmethod
    def _parse_linea(linea: str) -> AuditRecord | None:
        linea = linea.strip()
        if not linea:
            return None
        try:
            record = AuditRecord.model_validate(json.loads(linea))
        except json.JSONDecodeError as exc:
            # línea a medio escribir (solo puede ser la última) o bit-rot:
            # VISIBLE — un descarte silencioso baja el conteo de límites
            # (robustez: el docstring prometía log y el código tragaba)
            logger.warning("auditoría línea descartada (JSON roto): %s", exc)
            return None
        except ValidationError as exc:
            # registro válido como JSON pero no como schema: visible,
            # porque degrada el conteo de límites (robustez)
            logger.warning("auditoría línea descartada (schema no validable): %s", exc)
            return None
        if record.ts.tzinfo is None:
            # ts naive (edición manual, herramienta externa): sin tz el
            # sort con aware revienta entero — se normaliza a UTC
            record = record.model_copy(update={"ts": record.ts.replace(tzinfo=UTC)})
        return record


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
