"""Inventario local: caché SQLite de diálogos con TTL.

`refresh` es la única función que toca Telegram (vía client.iter_dialogs +
tg/ops.to_dialog_info); todo lo demás opera sobre la caché local, testeable
sin red. El refresh reemplaza el contenido completo: la caché siempre refleja
el último fetch, no un mezcla.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

import tg_sentry.paths as paths
from tg_sentry.models import DialogInfo, PeerKind
from tg_sentry.tg.ops import contact_user_ids, to_dialog_info

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dialogs (
    id INTEGER PRIMARY KEY,
    ord INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    username TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    muted INTEGER NOT NULL DEFAULT 0,
    unread INTEGER NOT NULL DEFAULT 0,
    last_message_at TEXT,
    role TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class InventoryStats(BaseModel):
    total: int
    by_kind: dict[str, int]
    fetched_at: datetime | None = None


class CacheIncompatible(RuntimeError):
    """La caché no se puede leer (schema/enum de otra versión): regenerar."""


def db_path() -> Path:
    return paths.data_dir() / "inventory.db"


def _connect(db: Path | None = None) -> sqlite3.Connection:
    file = db if db is not None else db_path()
    paths.ensure_private_dir(file.parent)
    if not file.exists():
        # nace 0600 desde la creación: los paths de LECTURA creaban la DB con
        # permisos de umask y sin chmod (auditoría 2026-09)
        import os

        os.close(os.open(file, os.O_WRONLY | os.O_CREAT, 0o600))
    conn = sqlite3.connect(file)
    # CLI + TUI en paralelo: espera por el lock en vez de reventar a los 5s
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(_SCHEMA)
    return conn


def store(
    dialogs: Iterable[DialogInfo],
    db: Path | None = None,
    *,
    me_id: int | None = None,
    contact_ids: set[int] | None = None,
) -> None:
    """Reemplaza el contenido de la caché y marca fetched_at (UTC)."""
    file = db if db is not None else db_path()
    conn = _connect(file)
    try:
        with conn:
            conn.execute("DELETE FROM dialogs")
            conn.executemany(
                "INSERT INTO dialogs (id, ord, kind, name, username, archived,"
                " pinned, muted, unread, last_message_at, role)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        d.id,
                        index,
                        d.kind.value,
                        d.name,
                        d.username,
                        int(d.archived),
                        int(d.pinned),
                        int(d.muted),
                        d.unread,
                        d.last_message_at.isoformat() if d.last_message_at else None,
                        d.role.value if d.role else None,
                    )
                    for index, d in enumerate(dialogs)
                ],
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('fetched_at', ?)",
                (datetime.now(UTC).isoformat(),),
            )
            if me_id is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('me_id', ?)",
                    (str(me_id),),
                )
            if contact_ids is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('contact_ids', ?)",
                    (json.dumps(sorted(contact_ids)),),
                )
    finally:
        conn.close()
    file.chmod(0o600)


def _lectura(proteccion: str, consulta: str, db: Path | None, params: tuple = ()) -> list:
    """Lectura tolerante: una DB corrupta (no SQLite, disco dañado) levanta
    CacheIncompatible con mensaje accionable — antes reventaba con traceback
    crudo y mataba la TUI en el arranque (2ª auditoría)."""
    try:
        conn = _connect(db)  # executescript del schema estalla aquí si hay basura
        try:
            return conn.execute(consulta, params).fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise CacheIncompatible(
            f"la caché del inventario es ilegible ({proteccion}): "
            "ejecuta tg-sentry dialogs --refresh para regenerarla"
        ) from exc


def load(db: Path | None = None) -> list[DialogInfo]:
    """Devuelve la caché completa en el orden del último fetch.

    Una DB de otra versión (enums distintos) NO se traga en silencio:
    CacheIncompatible con instrucción clara (auditoría 2026-09).
    """
    rows = _lectura(
        "load",
        "SELECT id, kind, name, username, archived, pinned, muted, unread,"
        " last_message_at, role FROM dialogs ORDER BY ord",
        db,
    )
    try:
        return [
            DialogInfo(
                id=row[0],
                kind=PeerKind(row[1]),
                name=row[2],
                username=row[3],
                archived=bool(row[4]),
                pinned=bool(row[5]),
                muted=bool(row[6]),
                unread=row[7],
                last_message_at=_dt_aware(row[8]),
                role=None if row[9] is None else _role_from_db(row[9]),
            )
            for row in rows
        ]
    except ValueError as exc:
        raise CacheIncompatible(
            "la caché del inventario es de otra versión o está corrupta: "
            "ejecuta tg-sentry dialogs --refresh para regenerarla"
        ) from exc


def _role_from_db(value: str):  # type: ignore[no-untyped-def]
    from tg_sentry.models import PeerRole

    return PeerRole(value)


def _dt_aware(raw: str | None) -> datetime | None:
    """fromisoformat + normalización naive→UTC: los filtros de inactividad
    comparan contra now(UTC) y naive≥aware era TypeError (2ª auditoría)."""
    if not raw:
        return None
    stamp = datetime.fromisoformat(raw)
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def me_id(db: Path | None = None) -> int | None:
    """Id de la propia cuenta, guardado en cada refresh (para blindarla)."""
    filas = _lectura("me_id", "SELECT value FROM meta WHERE key = 'me_id'", db)
    if not filas:
        return None
    valor = filas[0][0]
    try:
        return int(valor)
    except ValueError:
        # meta corrupta: None → el CLI/TUI tratan el plan como fail-closed con
        # mensaje accionable en vez de reventar con traceback (2ª auditoría)
        logger.warning("meta me_id corrupta (%r): se ignora (refresca)", valor)
        return None


def contact_ids(db: Path | None = None) -> set[int]:
    """Ids de los contactos de la cuenta (meta contact_ids, guarda el refresh)."""
    rows = _lectura(
        "contact_ids", "SELECT value FROM meta WHERE key = 'contact_ids'", db
    )
    if not rows:
        return set()
    try:
        parsed = json.loads(rows[0][0])
        if (
            not isinstance(parsed, list)
            or not all(
                isinstance(x, int) and not isinstance(x, bool) for x in parsed
            )
        ):
            raise TypeError("contact_ids no es una lista de enteros")
        return set(parsed)
    except (ValueError, TypeError):
        # JSON válido de forma incorrecta (["a","b"], {"1":2}) devolvía un set
        # de strings sin aviso → la protección dura de contactos dejaba de
        # matchear en silencio (2ª auditoría). Vacío con warning: visible.
        logger.warning("meta contact_ids corrupta: se ignora (refresca para regenerar)")
        return set()


def fetched_at(db: Path | None = None) -> datetime | None:
    rows = _lectura(
        "fetched_at", "SELECT value FROM meta WHERE key = 'fetched_at'", db
    )
    if not rows:
        return None
    try:
        stamp = datetime.fromisoformat(rows[0][0])
    except (ValueError, TypeError) as exc:
        # meta corrupta (edición manual, escritura a medias): mismo contrato
        # que me_id/contact_ids — CacheIncompatible visible, nunca traceback
        # crudo en cache_age/is_stale (3ª auditoría)
        raise CacheIncompatible(f"meta fetched_at no es una fecha: {exc}") from exc
    if stamp.tzinfo is None:
        # cache_age resta contra now(UTC): naive era TypeError (2ª auditoría)
        stamp = stamp.replace(tzinfo=UTC)
    return stamp


def cache_age(db: Path | None = None) -> timedelta | None:
    stamp = fetched_at(db)
    return None if stamp is None else datetime.now(UTC) - stamp


def is_stale(ttl_min: int, db: Path | None = None) -> bool:
    """Sin caché o con edad > TTL → True."""
    age = cache_age(db)
    return age is None or age > timedelta(minutes=ttl_min)


def stats(db: Path | None = None) -> InventoryStats:
    dialogs = load(db)
    by_kind: dict[str, int] = {}
    for dialog in dialogs:
        by_kind[dialog.kind.value] = by_kind.get(dialog.kind.value, 0) + 1
    return InventoryStats(
        total=len(dialogs), by_kind=by_kind, fetched_at=fetched_at(db)
    )


async def refresh(
    client: object,
    db: Path | None = None,
    progress: Callable[[int], None] | None = None,
) -> InventoryStats:
    """Fetch completo de todos los diálogos → reemplaza la caché. Solo lectura.

    FAIL-CLOSED: si no se puede determinar el id de la propia cuenta, la caché
    NO se escribe (un inventario sin me_id no puede blindar nada — auditoría
    2026-09). Los contactos se guardan también: safety los usa como protección
    dura de chats privados.
    """
    collected: list[DialogInfo] = []
    sin_id = 0
    async for dialog in client.iter_dialogs():  # type: ignore[attr-defined]
        info = to_dialog_info(dialog)
        if not info.id:  # diálogo sin id = basura: inventar 0 corrompe el plan
            sin_id += 1
            continue
        collected.append(info)
        if progress is not None and len(collected) % 50 == 0:
            progress(len(collected))
    if sin_id:
        logger.warning("refresh: %d diálogos sin id descartados", sin_id)
    me = await client.get_me()  # type: ignore[attr-defined]
    own_id = getattr(me, "id", None)
    if not own_id:
        msg = (
            "no se pudo determinar el id de tu cuenta (get_me sin id): "
            "NO se toca la caché — sin identidad propia no hay blindadura"
        )
        raise RuntimeError(msg)
    contactos = await contact_user_ids(client)
    store(collected, db, me_id=own_id, contact_ids=contactos)
    if progress is not None:
        progress(len(collected))
    by_kind: dict[str, int] = {}
    for dialog in collected:
        by_kind[dialog.kind.value] = by_kind.get(dialog.kind.value, 0) + 1
    return InventoryStats(
        total=len(collected),
        by_kind=by_kind,
        fetched_at=datetime.now(UTC),
    )
