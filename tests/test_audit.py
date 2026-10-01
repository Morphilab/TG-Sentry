"""Tests de la auditoría JSONL: append, rotación, limpieza, lectura. Sin red."""

from __future__ import annotations

import gzip
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tg_sentry.audit import AuditLog, AuditRecord, StepOutcome, record_outcome


def _record(
    plan_id: str = "p1", peer: int = 1, outcome: StepOutcome = StepOutcome.DONE
) -> AuditRecord:
    return AuditRecord(
        ts=datetime.now(UTC),
        plan_id=plan_id,
        peer_id=peer,
        step="delete_chat",
        outcome=outcome,
        reason=None,
    )


def test_append_y_lectura_roundtrip(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path, rotate_bytes=10 * 1024 * 1024, keep_days=90)
    audit.record(_record(peer=1, outcome=StepOutcome.DONE))
    audit.record(_record(peer=2, outcome=StepOutcome.SKIPPED_OK))
    audit.record(_record(peer=3, outcome=StepOutcome.FAILED))
    leidos = audit.read()
    assert [r.peer_id for r in leidos] == [1, 2, 3]
    assert [r.outcome for r in leidos] == [
        StepOutcome.DONE, StepOutcome.SKIPPED_OK, StepOutcome.FAILED,
    ]


def test_archivo_activo_0600_y_dir_0700(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path, rotate_bytes=10**9, keep_days=90)
    audit.record(_record())
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(audit.active_file.stat().st_mode) == 0o600


def test_rotacion_por_tamano_sin_truncar(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path, rotate_bytes=200, keep_days=90)
    for i in range(10):
        audit.record(_record(peer=i))
    rotados = list(tmp_path.glob("audit-*.jsonl.gz"))
    assert rotados, "debe existir al menos un rotado comprimido"
    # el activo nunca contiene los registros ya rotados, pero read() los ve todos
    assert len(audit.read()) == 10
    with gzip.open(rotados[0], "rt", encoding="utf-8") as handle:
        lineas = [json.loads(line) for line in handle if line.strip()]
    assert all(entry["step"] == "delete_chat" for entry in lineas)


def test_cleanup_borra_rotados_viejos(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path, rotate_bytes=10**9, keep_days=30)
    audit.record(_record())
    audit._rotate()
    viejo = next(tmp_path.glob("audit-*.jsonl.gz"))
    # mtime hace 40 días
    antiguo = datetime.now(UTC) - timedelta(days=40)
    import os

    os.utime(viejo, (antiguo.timestamp(), antiguo.timestamp()))
    audit.record(_record(peer=2))  # activo nuevo
    removed = audit.cleanup()
    assert removed == 1
    assert not viejo.exists()
    assert audit.active_file.exists()


def test_lectura_tolerante_a_linea_corrupta(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path, rotate_bytes=10**9, keep_days=90)
    audit.record(_record(peer=1))
    with audit.active_file.open("a", encoding="utf-8") as handle:
        handle.write("{{{{no es json\n")
    audit.record(_record(peer=2))
    assert [r.peer_id for r in audit.read()] == [1, 2]


def test_estados_no_colapsan() -> None:
    valores = {o.value for o in StepOutcome}
    assert valores == {
        "done", "skipped_ok", "skipped_unreachable", "skipped_external", "failed",
    }


# --- Regresiones auditoría 2026-09 (rama datos) -----------------------------


def test_read_sobrevive_gz_truncado(tmp_path: Path) -> None:
    """F1: un .gz truncado (corte de luz en rotación) NO revienta read():
    la auditoría completa (y con ella el conteo 50/h·200/d) sigue viva."""

    log = AuditLog(tmp_path)
    for i in range(3):
        log.record(_record(peer=i))
    log._rotate()
    # trunca el .gz a la mitad
    gz_path = next(tmp_path.glob("audit-*.jsonl.gz"))
    data = gz_path.read_bytes()
    gz_path.write_bytes(data[: len(data) // 2])

    log2 = AuditLog(tmp_path)
    log2.record(_record(peer=99))
    registros = log2.read()  # NO debe lanzar
    assert any(r.peer_id == 99 for r in registros)


def test_read_sobrevive_ts_naive(tmp_path: Path) -> None:
    """F8: un ts sin zona (edición manual) no rompe el sort entero."""
    log = AuditLog(tmp_path)
    log.record(_record(peer=1))
    with log.active_file.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"ts": "2026-09-20T10:00:00", "plan_id": "p", "peer_id": 2, '
            '"step": "leave", "outcome": "done"}\n'
        )
    registros = log.read()
    assert len(registros) == 2
    assert all(r.ts.tzinfo is not None for r in registros)  # normalizados


def test_rotacion_atomica_no_destruye_registros_en_vuelo(tmp_path: Path) -> None:
    """F2: appends concurrentes durante la rotación no se pierden (antes:
    copiar→unlink tenía una ventana que se comía registros → subconteo de
    los topes 50/h·200/d)."""
    log = AuditLog(tmp_path)
    for i in range(3):
        log.record(_record(peer=i))
    log._rotate()
    # el registro escrito justo tras la rotación está en el activo NUEVO
    log.record(_record(peer=77))
    registros = log.read()
    ids = {r.peer_id for r in registros}
    assert {0, 1, 2, 77} <= ids  # nada perdido entre rotado y activo


def test_cleanup_se_aplica_al_registrar(tmp_path: Path) -> None:
    """F9: la retención keep_days existía pero nunca se invocaba."""
    import os
    import time

    log = AuditLog(tmp_path, keep_days=1)
    viejo = tmp_path / "audit-20200101-000000-000000-000000.jsonl.gz"
    viejo.write_bytes(b"")
    os.utime(viejo, (time.time() - 90 * 86400, time.time() - 90 * 86400))
    log.record(_record(peer=5))
    assert not viejo.exists()  # la primera escritura aplicó la retención


def test_audit_since_naive_no_revienta(tmp_path: Path, monkeypatch) -> None:
    """P1 (CLI): `audit --since 2026-09-19` era TypeError naive vs aware."""
    from tg_sentry import __main__ as cli

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from tg_sentry import paths as app_paths

    log = AuditLog(app_paths.audit_dir())
    log.record(_record(peer=7))
    assert cli._cmd_audit("2026-09-19", None, None) == cli.EXIT_OK
    assert cli._cmd_audit("2026-09-19T12:00:00", None, None) == cli.EXIT_OK


def test_cache_de_lectura_ve_los_appends_posteriores(tmp_path: Path) -> None:
    """2ª auditoría: la caché por huella NUNCA puede servir registros
    previos a un record() propio (el presupuesto se contaría de menos)."""
    log = AuditLog(tmp_path, rotate_bytes=10**9, keep_days=90)
    record_outcome("p", 1, "delete_chat", StepOutcome.DONE, audit=log)
    assert len(log.read()) == 1
    record_outcome("p", 2, "block", StepOutcome.DONE, audit=log)
    assert len(log.read()) == 2  # la caché se invalidó con el append
    record_outcome("p", 3, "block", StepOutcome.FAILED, "x", audit=log)
    assert len(log.read()) == 3


def test_lectura_tolera_gz_corrupto_por_bitrot(tmp_path: Path) -> None:
    """2ª auditoría: bit-rot dentro del deflate lanza zlib.error (NO OSError):
    antes read() reventaba entero y con él el presupuesto anti-baneo."""
    import gzip as gz

    log = AuditLog(tmp_path, rotate_bytes=10**9, keep_days=90)
    record_outcome("p", 1, "delete_chat", StepOutcome.DONE, audit=log)
    activo = tmp_path / "audit.jsonl"
    contenido = activo.read_bytes()
    activo.unlink()
    with gz.open(tmp_path / "audit-20200101-000000-000000-1.jsonl.gz", "wb") as g:
        g.write(contenido)
    data = bytearray((tmp_path / "audit-20200101-000000-000000-1.jsonl.gz").read_bytes())
    # corromper bytes del medio del stream deflate
    for i in range(len(data) // 2, min(len(data) // 2 + 12, len(data))):
        data[i] ^= 0xFF
    (tmp_path / "audit-20200101-000000-000000-1.jsonl.gz").write_bytes(bytes(data))
    # NO debe lanzar: descarta el rotado podrido y devuelve lo que quede
    registros = log.read()
    assert isinstance(registros, list)


def test_cleanup_borra_rotando_huerfano(tmp_path: Path) -> None:
    """2ª auditoría: un .rotando huérfano (crash entre rename y compresión)
    se re-leía para siempre y violaba keep_days."""
    from datetime import UTC, datetime

    viejo = tmp_path / "audit.jsonl.20200101-000000-000000-1.rotando"
    viejo.write_text("{}\n", encoding="utf-8")
    os_utime = viejo.stat().st_mtime - 2 * 24 * 3600  # 2 días
    import os as _os

    _os.utime(viejo, (os_utime, os_utime))
    log = AuditLog(tmp_path, rotate_bytes=10**9, keep_days=90)
    borrados = log.cleanup(now=datetime.now(UTC))
    assert borrados >= 1 and not viejo.exists()


def test_rotacion_fallida_conserva_rotando(tmp_path: Path) -> None:
    """P2 3ª auditoría: un fallo de compresión (disco lleno, EIO) NO destruye
    la única copia del histórico — antes el finally borraba el .rotando y los
    registros se perdían (conteo de límites degradado hacia lo peligroso)."""
    import tg_sentry.audit as audit_mod

    audit = AuditLog(tmp_path, rotate_bytes=200, keep_days=90)
    for i in range(4):
        audit.record(_record(peer=i))
    original = audit_mod.gzip.GzipFile

    class _Roto:  # compresión que revienta (p. ej. ENOSPC)
        def __init__(self, *a, **k):  # type: ignore[no-untyped-def]
            raise OSError("No space left on device")

    audit_mod.gzip.GzipFile = _Roto  # type: ignore[assignment,misc]
    try:
        with pytest.raises(OSError):
            audit.record(_record(peer=99))
    finally:
        audit_mod.gzip.GzipFile = original  # type: ignore[assignment]
    rotando = list(tmp_path.glob("*.rotando"))
    assert rotando, "la copia .rotando DEBE conservarse"
    assert list(tmp_path.glob("*.gz.tmp")) == []  # tmp huérfano retirado
    # read() tolera el .rotando: los 4 registros previos siguen contando
    assert len(audit.read()) == 4
    # reintentar registra de nuevo (activo nuevo sin rotación en curso)
    audit.record(_record(peer=99))
    assert len(audit.read()) == 5


def test_gz_rotado_nace_0600(tmp_path: Path) -> None:
    """P2 3ª auditoría: los .gz rotados nacían con umask (0644) y jamás se
    les aplicaba chmod — el histórico completo de peer_ids quedaba legible."""
    audit = AuditLog(tmp_path, rotate_bytes=200, keep_days=90)
    for i in range(4):
        audit.record(_record(peer=i))
    gzs = list(tmp_path.glob("audit-*.jsonl.gz"))
    assert gzs, "la rotación debió producir un .gz"
    assert stat.S_IMODE(gzs[0].stat().st_mode) == 0o600


def test_activo_nace_0600_y_linea_json_rota_se_logea(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """P3 3ª auditoría: el activo nace 0600 (no chmod-posterior). Y una línea
    con JSON roto se descarta CON log (el docstring lo promete; degradaba el
    conteo de límites en silencio)."""
    audit = AuditLog(tmp_path, rotate_bytes=10 * 1024 * 1024, keep_days=90)
    audit.record(_record(peer=1))
    assert stat.S_IMODE(audit.active_file.stat().st_mode) == 0o600
    with audit.active_file.open("a", encoding="utf-8") as handle:
        handle.write("{esto no es json\n")
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="tg_sentry.audit"):
        registros = audit.read()
    assert len(registros) == 1
    assert any("JSON roto" in r.message for r in caplog.records)
