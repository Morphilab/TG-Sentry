"""Tests del comando CLI `apply` (destructivo — con fakes, sin red, sin cuenta)."""

from __future__ import annotations

from pathlib import Path

from tests.fakes import FakeSessionClient
from tests.test_cli_plan import _setup_env
from tests.test_inventory import _to_infos
from tg_sentry import inventory
from tg_sentry.__main__ import EXIT_NO_SESSION, main
from tg_sentry.audit import AuditLog
from tg_sentry.models import DialogInfo, PeerKind
from tg_sentry.planner import PlannerOptions, build_plan


def test_apply_rechaza_identidad_viva_distinta(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """swap de sesión — la caché y el plan dicen 999
    (las re-validaciones de siempre pasan), pero la sesión CONECTADA es la
    cuenta 42. La identidad en el momento destructivo sale de la sesión
    VIVA (get_me), no del inventario: antes era caché-vs-caché."""
    _setup_env(tmp_path, monkeypatch)
    inventory.store(_to_infos(), inventory.db_path(), me_id=999)

    plan = build_plan(
        [DialogInfo(id=500, kind=PeerKind.BOT, name="b")],
        PlannerOptions(ttl_min=30),
        me_id=999,
    )
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json(), encoding="utf-8")

    async def _conectar(_config):  # type: ignore[no-untyped-def]
        return FakeSessionClient(me_id=42)  # la sesión viva es OTRA cuenta

    monkeypatch.setattr("tg_sentry.__main__._connect_authorized", _conectar)
    codigo = main(["apply", "--plan", str(plan_file), "--confirm", "CONFIRMAR"])
    assert codigo == EXIT_NO_SESSION
    assert "NO es la cuenta del plan" in capsys.readouterr().err
    # el motor JAMÁS arrancó: nada auditado
    assert AuditLog().read() == []
