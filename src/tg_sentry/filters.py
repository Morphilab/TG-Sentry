"""Filtros combinables del inventario.

El regex se compila con `safe_compile`: longitud acotada y errores de
sintaxis capturados. NOTE (auditoría 2026-09): la ejecución del regex es
SÍNCRONA (Python `re` no tiene timeout). La TUI la descarga a un hilo con
timeout (el event loop no se congela; el hilo huérfano termina solo tarde
o temprano); en CLI el patrón es del propio usuario sobre datos locales.
No hay protección total anti-ReDoS sin una librería de tiempo lineal.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tg_sentry.models import DialogInfo, PeerKind, PeerRole

MAX_PATTERN_LEN = 200
# Cota anti-overflow: timedelta(days=huge) revienta; 100 años basta.
MAX_INACTIVE_DAYS = 36500


class FilterError(ValueError):
    """Filtro inválido (regex roto, patrón demasiado largo)."""


def safe_compile(pattern: str) -> re.Pattern[str]:
    """Compila un regex con límites: longitud y errores claros."""
    if len(pattern) > MAX_PATTERN_LEN:
        msg = f"patrón demasiado largo (máx. {MAX_PATTERN_LEN} caracteres)"
        raise FilterError(msg)
    try:
        return re.compile(pattern)
    except re.error as exc:
        msg = f"regex inválido: {exc}"
        raise FilterError(msg) from exc


class Filters(BaseModel):
    """Filtros combinables (todos opcionales, AND entre ellos).

    inactive_days: diálogos cuyo último mensaje es más viejo que N días
    (los SIN mensajes cuentan como inactivos — decisión M1, documentada).
    """

    model_config = ConfigDict(extra="forbid")

    kinds: list[PeerKind] | None = None
    name_contains: str | None = None
    name_regex: str | None = None
    inactive_days: int | None = Field(default=None, ge=0, le=MAX_INACTIVE_DAYS)
    archived: bool | None = None
    unread_min: int | None = Field(default=None, ge=0)
    role: PeerRole | None = None


def parse_filter_expr(expr: str) -> Filters:
    """Parsea la expresión CLI: clave=valor,clave2=valor2 (todas opcionales).

    Claves: kinds (lista separada por '+'), name, regex, inactive (días),
    archived (true|false|any), unread_min, role. Errores → FilterError.
    """
    values: dict[str, object] = {}
    vistas: set[str] = set()
    for chunk in expr.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            msg = f"filtro malformado (se espera clave=valor): {chunk!r}"
            raise FilterError(msg)
        key, _, raw = chunk.partition("=")
        key, raw = key.strip(), raw.strip()
        if not raw:
            msg = f"valor vacío para el filtro {key!r}"
            raise FilterError(msg)
        if key in vistas:
            msg = f"clave de filtro duplicada: {key!r}"
            raise FilterError(msg)
        vistas.add(key)
        match key:
            case "kinds":
                kinds = []
                for token in raw.split("+"):
                    token = token.strip()
                    try:
                        kinds.append(PeerKind(token))
                    except ValueError as exc:
                        msg = f"tipo inválido {token!r}; válidos: {[k.value for k in PeerKind]}"
                        raise FilterError(msg) from exc
                values["kinds"] = kinds
            case "name":
                values["name_contains"] = raw
            case "regex":
                values["name_regex"] = raw
            case "inactive":
                values["inactive_days"] = _parse_int(key, raw)
            case "unread_min":
                values["unread_min"] = _parse_int(key, raw)
            case "archived":
                if raw.lower() not in {"true", "false", "any"}:
                    msg = "archived debe ser true|false|any"
                    raise FilterError(msg)
                values["archived"] = None if raw.lower() == "any" else raw.lower() == "true"
            case "role":
                try:
                    values["role"] = PeerRole(raw)
                except ValueError as exc:
                    msg = f"rol inválido {raw!r}; válidos: {[r.value for r in PeerRole]}"
                    raise FilterError(msg) from exc
            case _:
                msg = f"clave de filtro desconocida: {key!r}"
                raise FilterError(msg)
    try:
        return Filters.model_validate(values)
    except ValidationError as exc:
        # fuera del contrato del CLI: inactive=-5 llegaba como traceback
        # pydantic crudo en vez de "error de filtro" (2ª auditoría)
        msg = str(exc)
        raise FilterError(msg) from exc


def _parse_int(key: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        msg = f"{key} debe ser un entero, recibido: {raw!r}"
        raise FilterError(msg) from exc


def _normalizar(texto: str) -> str:
    """Casefold + NFC: matching de nombres estable ante unicode raro
    (İstanbul, ß, acentos descompuestos — auditoría 2026-09)."""
    return unicodedata.normalize("NFC", texto).casefold()


def apply_filters(dialogs: list[DialogInfo], filters: Filters) -> list[DialogInfo]:
    """Aplica todos los filtros activos (AND). FilterError si el regex no compila."""
    regex = safe_compile(filters.name_regex) if filters.name_regex else None
    needle = _normalizar(filters.name_contains) if filters.name_contains else None
    now = datetime.now(UTC)
    cutoff = (
        now - timedelta(days=filters.inactive_days)
        if filters.inactive_days is not None
        else None
    )

    result: list[DialogInfo] = []
    for dialog in dialogs:
        if filters.kinds and dialog.kind not in filters.kinds:
            continue
        if needle is not None and needle not in _normalizar(dialog.display):
            continue
        if regex is not None and not regex.search(dialog.display):
            continue
        # Inactivo = sin mensajes (None) o último mensaje anterior al corte.
        if (
            cutoff is not None
            and dialog.last_message_at is not None
            and dialog.last_message_at >= cutoff
        ):
            continue
        if filters.archived is not None and dialog.archived != filters.archived:
            continue
        if filters.unread_min is not None and dialog.unread < filters.unread_min:
            continue
        if filters.role is not None and dialog.role != filters.role:
            continue
        result.append(dialog)
    return result
