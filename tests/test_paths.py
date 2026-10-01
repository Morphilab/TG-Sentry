"""Tests de rutas XDG y higiene de permisos. Sin red, sin Telegram."""

from __future__ import annotations

import stat
from pathlib import Path

import tg_sentry.paths as paths


def test_config_dir_respeta_xdg_config_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert paths.config_dir() == tmp_path / "tg-sentry"


def test_data_dir_default_en_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert paths.data_dir() == tmp_path / "tg-sentry"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.data_dir() == tmp_path / ".local" / "share" / "tg-sentry"


def test_ensure_private_dir_crea_0700(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c"
    paths.ensure_private_dir(target)
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_ensure_private_dir_normaliza_ancestros_nuevos(tmp_path: Path) -> None:
    target = tmp_path / "x" / "y" / "z"
    paths.ensure_private_dir(target)
    for created in (target, target.parent, target.parent.parent):
        assert stat.S_IMODE(created.stat().st_mode) == 0o700


def test_ensure_private_dir_no_toca_ancestros_preexistentes(tmp_path: Path) -> None:
    fixed = tmp_path / "existente"
    fixed.mkdir(mode=0o755)
    paths.ensure_private_dir(fixed / "hijo")
    assert stat.S_IMODE(fixed.stat().st_mode) == 0o755  # solo el hijo es nuestro
    assert stat.S_IMODE((fixed / "hijo").stat().st_mode) == 0o700


def test_ensure_private_dir_repara_permisos_holgados(tmp_path: Path) -> None:
    target = tmp_path / "suelto"
    target.mkdir(mode=0o755)
    paths.ensure_private_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_file_mode_inexistente_devuelve_none(tmp_path: Path) -> None:
    assert paths.file_mode(tmp_path / "no-existe") is None
