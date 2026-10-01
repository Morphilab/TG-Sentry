"""Export del inventario a JSON/CSV con higiene de permisos.

AVISO permanente (lo muestra el CLI al exportar): el export contiene datos
personales (nombres, ids, usernames). Se escribe con 0600 DESDE LA CREACIÓN
(auditado 2026-09: antes nacía con umask y el chmod llegaba al final — un
crash dejaba datos personales world-readable). El export NO se registra en
la auditoría: es una copia de solo lectura fuera del motor.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import tg_sentry.paths as paths
from tg_sentry.models import DialogInfo

Format = Literal["json", "csv"]

_FIELDS = [
    "id",
    "kind",
    "name",
    "username",
    "archived",
    "pinned",
    "muted",
    "unread",
    "last_message_at",
    "role",
]

# Prefijos que Excel/LibreOffice interpretan como FÓRMULA al abrir el CSV:
# el `name` lo controla un TERCERO de Telegram (auditoría 2026-09).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def neutralizar_celda(value: object) -> object:
    """Prefija con ' las celdas que Excel/LibreOffice interpretarían como
    fórmula. Pública: el export de auditoría (CLI) la reutiliza (2ª auditoría)."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def abrir_0600(target: Path):  # type: ignore[no-untyped-def]
    """Abre el destino con modo 0600 EN LA CREACIÓN (sin tocar directorios).

    O_NOFOLLOW si existe: un symlink preexistente en la ruta del usuario
    hacía escribir a través del enlace (2ª auditoría)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    # O_TRUNC conserva el modo de un preexistente holgado: fchmod lo repare
    # ANTES de escribir un solo byte (3ª auditoría)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8", newline="")


def export(
    dialogs: list[DialogInfo], fmt: Format, output: Path | None = None
) -> Path:
    """Escribe el inventario y devuelve la ruta (archivo 0600).

    Con ruta explícita del usuario solo se crea el directorio si falta (no se
    tocan sus permisos); la higiene 0700 aplica solo al exports XDG por defecto.
    """
    if output is not None:
        target = output
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        target = _default_path(fmt)
        paths.ensure_private_dir(target.parent)
    if fmt == "json":
        payload = [dialog.model_dump(mode="json") for dialog in dialogs]
        with abrir_0600(target) as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        with abrir_0600(target) as handle:
            writer = csv.DictWriter(handle, fieldnames=_FIELDS)
            writer.writeheader()
            for dialog in dialogs:
                writer.writerow(
                    {
                        campo: neutralizar_celda(valor)
                        for campo, valor in dialog.model_dump(mode="json").items()
                    }
                )
    target.chmod(0o600)
    return target


def _default_path(fmt: Format) -> Path:
    # con microsegundos: dos exports del mismo formato en el mismo segundo
    # se truncaban entre sí en silencio (auditoría 2026-09)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    return paths.exports_dir() / f"inventario-{stamp}.{fmt}"
