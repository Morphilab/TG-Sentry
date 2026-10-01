"""Tests de la caché SQLite del inventario. Sin red."""

from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.fakes import sample_dialogs
from tg_sentry import inventory
from tg_sentry.models import DialogInfo


def _to_infos() -> list[DialogInfo]:
    from tg_sentry.tg.ops import to_dialog_info

    return [to_dialog_info(d) for d in sample_dialogs()]


def test_store_y_load_roundtrip_orden_preservado(tmp_path: Path) -> None:
    db = tmp_path / "inventory.db"
    infos = _to_infos()
    inventory.store(infos, db)
    loaded = inventory.load(db)
    assert [d.id for d in loaded] == [d.id for d in infos]
    assert [d.kind for d in loaded] == [d.kind for d in infos]
    assert loaded[0].username == "ejemplobot"
    assert loaded[2].archived is True
    assert loaded[5].muted is True
    assert loaded[3].last_message_at == datetime(2020, 1, 1, tzinfo=UTC)


def test_store_reemplaza_contenido_completo(tmp_path: Path) -> None:
    db = tmp_path / "inventory.db"
    inventory.store(_to_infos(), db)
    inventory.store(_to_infos()[:2], db)
    assert len(inventory.load(db)) == 2


def test_store_crea_db_0600(tmp_path: Path) -> None:
    db = tmp_path / "inventory.db"
    inventory.store(_to_infos(), db)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_stats_por_tipo(tmp_path: Path) -> None:
    db = tmp_path / "inventory.db"
    inventory.store(_to_infos(), db)
    stats = inventory.stats(db)
    assert stats.total == 6
    assert stats.by_kind == {
        "bot": 1, "user": 1, "group": 1, "channel": 2, "unknown": 1,
    }
    assert stats.fetched_at is not None


def test_ttl_caché_fresca_vs_caduca_vs_inexistente(tmp_path: Path) -> None:
    db = tmp_path / "inventory.db"
    assert inventory.is_stale(60, db) is True  # sin caché

    inventory.store(_to_infos(), db)
    assert inventory.is_stale(60, db) is False  # fresca
    assert inventory.cache_age(db) is not None
    assert inventory.cache_age(db).total_seconds() < 60  # type: ignore[union-attr]

    # Envejecer manualmente el fetched_at
    import sqlite3

    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'fetched_at'",
            ((datetime.now(UTC) - timedelta(hours=2)).isoformat(),),
        )
    conn.close()
    assert inventory.is_stale(60, db) is True  # caducada (> 60 min)


def test_refresh_con_cliente_fake(tmp_path: Path) -> None:
    import asyncio

    db = tmp_path / "inventory.db"
    progresses: list[int] = []

    class FakeMe:
        id = 987654321

    class FakeContact:
        def __init__(self, cid: int) -> None:
            self.id = cid

    class FakeContactsResult:
        def __init__(self) -> None:
            self.users = [FakeContact(1111), FakeContact(2222)]

    class FakeClient:
        def iter_dialogs(self):  # type: ignore[no-untyped-def]
            async def _gen():
                for dialog in sample_dialogs():
                    yield dialog

            return _gen()

        async def get_me(self) -> FakeMe:
            return FakeMe()

        def __call__(self, _request: object):  # type: ignore[no-untyped-def]
            async def _run() -> FakeContactsResult:
                return FakeContactsResult()

            return _run()

    stats = asyncio.run(
        inventory.refresh(FakeClient(), db, progress=lambda n: progresses.append(n))
    )
    assert stats.total == 6
    assert stats.by_kind["channel"] == 2
    assert len(inventory.load(db)) == 6
    assert progresses == [6]
    assert inventory.me_id(db) == 987654321  # para blindar la propia cuenta
    assert inventory.contact_ids(db) == {1111, 2222}  # para protección dura


def test_refresh_sin_me_id_no_escribe_cache(tmp_path: Path) -> None:
    """FAIL-CLOSED: sin identidad propia la caché NO se toca (auditoría 2026-09)."""
    import asyncio

    db = tmp_path / "inventory.db"
    inventory.store(_to_infos(), db)  # caché previa válida

    class FakeMeSinId:
        id = None

    class FakeClient:
        def iter_dialogs(self):  # type: ignore[no-untyped-def]
            async def _gen():
                for dialog in sample_dialogs():
                    yield dialog

            return _gen()

        async def get_me(self) -> FakeMeSinId:
            return FakeMeSinId()

    import pytest

    with pytest.raises(RuntimeError, match=r"me_id|identidad"):
        asyncio.run(inventory.refresh(FakeClient(), db))
    # la caché anterior sobrevive intacta
    assert len(inventory.load(db)) == 6


def test_refresh_descarta_dialogos_sin_id(tmp_path: Path) -> None:
    """Un diálogo sin id no se inventa como 0: se descarta (auditoría 2026-09)."""
    import asyncio

    from tests.fakes import FakeDialog

    db = tmp_path / "inventory.db"

    class FakeMe:
        id = 7

    class FakeClient:
        def iter_dialogs(self):  # type: ignore[no-untyped-def]
            async def _gen():
                yield FakeDialog(id=0, name="basura")

            return _gen()

        async def get_me(self) -> FakeMe:
            return FakeMe()

        def __call__(self, _request: object):  # type: ignore[no-untyped-def]
            async def _run():
                class R:
                    def __init__(self) -> None:
                        self.users: list[object] = []

                return R()

            return _run()

    stats = asyncio.run(inventory.refresh(FakeClient(), db))
    assert stats.total == 0
    assert inventory.load(db) == []


def test_store_sin_me_id_lo_deja_en_none(tmp_path: Path) -> None:
    db = tmp_path / "inventory.db"
    inventory.store(_to_infos(), db)
    assert inventory.me_id(db) is None


# --- Regresiones auditoría 2026-09 (rama datos) -----------------------------


def test_db_creada_por_lectura_nace_0600(tmp_path: Path) -> None:
    """F5: los paths de LECTURA creaban la DB con permisos de umask."""
    db = tmp_path / "inventory.db"
    inventory.load(db)  # lectura pura sobre DB inexistente
    assert db.exists()
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_load_incompatible_da_error_accionable(tmp_path: Path) -> None:
    """F11: enums de otra versión = mensaje claro, no traceback pydantic."""
    import sqlite3

    import pytest

    db = tmp_path / "inventory.db"
    inventory.store(_to_infos(), db)
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE dialogs SET kind = 'canal_de_otra_version' WHERE id = 111")
    conn.close()
    with pytest.raises(inventory.CacheIncompatible, match="refresh"):
        inventory.load(db)


def test_contact_ids_forma_invalida_devuelve_vacio_con_warning(tmp_path: Path) -> None:
    """2ª auditoría: JSON válido de forma incorrecta devolvía un set de
    strings sin aviso → la protección dura de contactos dejaba de matchear."""
    import json as _json
    import sqlite3

    db = tmp_path / "inventory.db"
    inventory.store(_to_infos(), db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('contact_ids', ?)",
        (_json.dumps(["a", "b"]),),
    )
    conn.commit()
    conn.close()
    assert inventory.contact_ids(db) == set()


def test_me_id_corrupto_devuelve_none_con_warning(tmp_path: Path) -> None:
    """2ª auditoría: int() sobre meta corrupta reventaba con traceback en
    vez del camino fail-closed accionable del CLI."""
    import sqlite3

    db = tmp_path / "inventory.db"
    inventory.store(_to_infos(), db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('me_id', ?)",
        ("no-es-un-int",),
    )
    conn.commit()
    conn.close()
    assert inventory.me_id(db) is None


def test_fetched_at_naive_no_revienta_cache_age(tmp_path: Path) -> None:
    """2ª auditoría: un stamp naive (edición manual) hacía TypeError al
    restar contra now(UTC); se normaliza a UTC como en audit.py."""
    import sqlite3

    db = tmp_path / "inventory.db"
    inventory.store(_to_infos(), db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('fetched_at', ?)",
        ("2026-01-01T00:00:00",),  # sin tz
    )
    conn.commit()
    conn.close()
    edad = inventory.cache_age(db)
    assert edad is not None and edad.total_seconds() > 0


def test_load_last_message_naive_no_reventa_filtros(tmp_path: Path) -> None:
    """2ª auditoría: last_message_at naive comparado con now(UTC) en los
    filtros era TypeError; load() lo normaliza."""
    import sqlite3

    db = tmp_path / "inventory.db"
    inventory.store(_to_infos(), db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE dialogs SET last_message_at = ? WHERE id = 222",
                 ("2026-09-19T12:00:00",))
    conn.commit()
    conn.close()
    loaded = inventory.load(db)  # no debe lanzar
    objetivo = next(d for d in loaded if d.id == 222)
    assert objetivo.last_message_at is not None
    assert objetivo.last_message_at.tzinfo is not None


def test_db_corrupta_levanta_cache_incompatible(tmp_path: Path) -> None:
    """2ª auditoría: un inventory.db ilegible (basura, disco dañado) reventaba
    con traceback crudo y mataba la TUI en el arranque; ahora es
    CacheIncompatible accionable en TODAS las lecturas."""
    import pytest

    db = tmp_path / "inventory.db"
    db.write_bytes(b"esto no es sqlite" * 100)
    with pytest.raises(inventory.CacheIncompatible):
        inventory.load(db)
    # TODAS las lecturas fallan igual de accionable (la conexión entera
    # estalla con el schema sobre basura, no solo el SELECT)
    with pytest.raises(inventory.CacheIncompatible):
        inventory.me_id(db)
    with pytest.raises(inventory.CacheIncompatible):
        inventory.contact_ids(db)
    with pytest.raises(inventory.CacheIncompatible):
        inventory.fetched_at(db)


def test_fetched_at_corrupta_es_cache_incompatible(tmp_path: Path) -> None:
    """P2 3ª auditoría: meta fetched_at corrupto → CacheIncompatible visible
    (contrato "corrupto en TODAS las lecturas"), no ValueError crudo que
    reventaba cache_age/is_stale en CLI y TUI."""
    import sqlite3

    import pytest

    from tg_sentry.inventory import CacheIncompatible

    db = tmp_path / "inv.db"
    inventory.store(_to_infos(), db, me_id=42)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value = 'no-es-una-fecha' WHERE key = 'fetched_at'")
    conn.commit()
    conn.close()
    with pytest.raises(CacheIncompatible):
        inventory.fetched_at(db)
