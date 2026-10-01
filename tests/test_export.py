"""Tests de export JSON/CSV con permisos. Sin red."""

from __future__ import annotations

import csv
import json
import stat
from pathlib import Path

from tests.test_inventory import _to_infos
from tg_sentry import export as exporting


def test_export_json_contenido_y_permisos(tmp_path: Path) -> None:
    target = tmp_path / "inv.json"
    result = exporting.export(_to_infos(), "json", target)
    assert result == target
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    data = json.loads(target.read_text(encoding="utf-8"))
    assert len(data) == 6
    assert data[0]["kind"] == "bot"
    assert data[2]["archived"] is True


def test_export_csv_filas_y_cabecera(tmp_path: Path) -> None:
    target = tmp_path / "inv.csv"
    exporting.export(_to_infos(), "csv", target)
    with target.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 6
    assert rows[0]["id"] == "111"
    assert rows[0]["kind"] == "bot"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_export_ruta_por_defecto_bajo_exports_xdg(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    target = exporting.export(_to_infos()[:1], "json")
    assert tmp_path in target.parents
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.name.startswith("inventario-") and target.suffix == ".json"


def test_export_ruta_explicita_no_toca_permisos_del_directorio(tmp_path: Path) -> None:
    # regresión: la higiene 0700 es solo para dirs de la app, jamás para
    # directorios arbitrarios del usuario (p. ej. /tmp, 1777/0755)
    ajeno = tmp_path / "dir-ajeno"
    ajeno.mkdir(mode=0o755)
    target = exporting.export(_to_infos()[:1], "json", ajeno / "inv.json")
    assert stat.S_IMODE(ajeno.stat().st_mode) == 0o755  # intacto
    assert stat.S_IMODE(target.stat().st_mode) == 0o600  # el archivo es nuestro


# --- Regresiones auditoría 2026-09 (rama datos) -----------------------------


def test_csv_neutraliza_inyeccion_de_formulas(tmp_path: Path) -> None:
    """El `name` lo controla un tercero: '=cmd...' no puede llegar crudo al
    CSV (se ejecutaría como fórmula al abrirlo en Excel/LibreOffice)."""
    dialogs = _to_infos()
    dialogs[0].name = "=HYPERLINK(\"https://evil\")"
    dialogs[1].name = "+suma"
    target = tmp_path / "out.csv"
    exporting.export(dialogs, "csv", target)
    contenido = target.read_text(encoding="utf-8")
    assert "'=HYPERLINK" in contenido
    assert "'+suma" in contenido
    assert "\n=HYPERLINK" not in contenido


def test_export_nace_0600_desde_la_creacion(tmp_path: Path) -> None:
    """El chmod final llegaba tarde: entre creación y chmod el archivo estaba
    world-readable (datos personales). Ahora os.open lo crea 0600."""
    modos: list[int] = []
    import os as _os

    original = _os.open

    def espiando(path, flags, mode=0o777, *args, **kwargs):  # type: ignore[no-untyped-def]
        modos.append(mode)
        return original(path, flags, mode, *args, **kwargs)

    monkeypatch_target = "tg_sentry.export.os.open"
    from unittest.mock import patch

    with patch(monkeypatch_target, side_effect=espiando):
        exporting.export(_to_infos(), "json", tmp_path / "out.json")
    assert 0o600 in modos


def test_default_path_sin_colision_en_el_mismo_segundo() -> None:
    """Dos exports del mismo formato en el mismo segundo se truncaban entre
    sí en silencio; ahora el nombre lleva microsegundos."""
    from tg_sentry.export import _default_path

    assert _default_path("csv") != _default_path("csv")
