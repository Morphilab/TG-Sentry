"""Rutas XDG de tg-sentry con higiene de permisos verificada.

Todo el estado local (config, sesión, logs, auditoría, exports) vive bajo
directorios XDG con permisos restrictivos: directorios 0700 y archivos
sensibles 0600. Telethon crea la sesión SQLite con 0644 si no se fuerza,
por eso cada punto de escritura pasa por aquí.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

APP_NAME = "tg-sentry"


def _xdg(env_var: str, default: Path) -> Path:
    value = os.environ.get(env_var)
    return (Path(value) if value else default).expanduser()


def config_dir() -> Path:
    """~/.config/tg-sentry (respeta XDG_CONFIG_HOME)."""
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP_NAME


def data_dir() -> Path:
    """~/.local/share/tg-sentry (respeta XDG_DATA_HOME)."""
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP_NAME


def state_dir() -> Path:
    """~/.local/state/tg-sentry (respeta XDG_STATE_HOME)."""
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.toml"


def session_path() -> Path:
    return data_dir() / "tg-sentry.session"


def logs_dir() -> Path:
    return state_dir() / "logs"


def audit_dir() -> Path:
    return state_dir() / "audit"


def exports_dir() -> Path:
    return state_dir() / "exports"


def ensure_private_dir(path: Path) -> Path:
    """Crea el directorio y TODOS sus ancestros nuevos con 0700.

    mkdir(parents=True) aplica umask a toda la cadena (0775 típico); aquí
    cada directorio que se crea se fuerza a 0700. Si el directorio final ya
    existía con permisos holgados, se repara. Los ancestros preexistentes
    (p. ej. ~/.local) no se tocan.
    """
    missing: list[Path] = []
    probe = path
    while not probe.exists() and probe != probe.parent:
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        # exist_ok: CLI + TUI en primera ejecución concurrente (2ª auditoría)
        directory.mkdir(exist_ok=True)
        directory.chmod(0o700)
    if path.exists() and stat.S_IMODE(path.stat().st_mode) != 0o700:
        path.chmod(0o700)
    return path


def fsync_dir(directory: Path) -> None:
    """fsync del directorio: renames/unlinks sobreviven a un corte de luz."""
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:  # p. ej. filesystems sin soporte: no es fatal
        pass


def file_mode(path: Path) -> int | None:
    """Modo de permisos del archivo (para diagnóstico), None si no existe."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None


def dir_mode(path: Path) -> int | None:
    """Modo de permisos del directorio (para diagnóstico), None si no existe."""
    return file_mode(path)
