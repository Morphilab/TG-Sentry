"""Tests del comando CLI `plan` (dry-run) contra caché temporal. Sin red."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.test_inventory import _to_infos
from tg_sentry import inventory
from tg_sentry.__main__ import EXIT_CONFIG, EXIT_NO_SESSION, EXIT_OK, main
from tg_sentry.config import Config, save_config
from tg_sentry.planner import ActionPlan


def _setup_env(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_file = tmp_path / "conf" / "tg-sentry" / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    save_config(Config(), config_file)
    return config_file


def test_plan_desde_cache_filtrado(tmp_path: Path, monkeypatch, capsys) -> None:
    _setup_env(tmp_path, monkeypatch)
    inventory.store(_to_infos(), inventory.db_path(), me_id=42)

    salida = tmp_path / "plan.json"
    codigo = main(["plan", "--filter", "kinds=bot", "--output", str(salida)])
    assert codigo == EXIT_OK
    plan = ActionPlan.model_validate_json(salida.read_text(encoding="utf-8"))
    assert len(plan.peers) == 1
    assert [s.kind.value for s in plan.peers[0].steps] == ["delete_chat", "block"]
    # 777000 no está en los fakes, pero el excluido 555 (unknown) sí sale listado
    consola = capsys.readouterr().out
    assert "DRY-RUN" in consola
    assert "destructivas" in consola


def test_plan_rechaza_cache_caducada(tmp_path: Path, monkeypatch, capsys) -> None:
    _setup_env(tmp_path, monkeypatch)
    inventory.store(_to_infos(), inventory.db_path(), me_id=42)
    conn = sqlite3.connect(inventory.db_path())
    with conn:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'fetched_at'",
            ((datetime.now(UTC) - timedelta(hours=3)).isoformat(),),
        )
    conn.close()

    codigo = main(["plan", "--filter", "kinds=bot", "--output", str(tmp_path / "p.json")])
    assert codigo == EXIT_CONFIG
    assert "caducada" in capsys.readouterr().err

    # con --allow-stale pasa y el plan queda marcado
    codigo = main(
        ["plan", "--filter", "kinds=bot", "--output", str(tmp_path / "p.json"), "--allow-stale"]
    )
    assert codigo == EXIT_OK
    datos = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert datos["based_on_cache_fresh"] is False


def test_plan_sin_cache(tmp_path: Path, monkeypatch, capsys) -> None:
    _setup_env(tmp_path, monkeypatch)
    codigo = main(["plan", "--output", str(tmp_path / "p.json")])
    assert codigo == EXIT_NO_SESSION
    assert "Sin caché" in capsys.readouterr().err


def test_plan_whitelist_malformada_rechazada_sin_traceback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """4ª auditoría: "--77" en la whitelist se rechaza EN LA CARGA de config
    (validador de SafetySection) con mensaje accionable y EXIT_CONFIG."""
    _setup_env(tmp_path, monkeypatch)
    inventory.store(_to_infos(), inventory.db_path(), me_id=42)
    config_file = tmp_path / "conf" / "tg-sentry" / "config.toml"
    config_file.write_text(
        '[telegram]\napi_id = 0\napi_hash = ""\nphone = ""\n'
        '[safety]\nwhitelist = ["--77"]\n',
        encoding="utf-8",
    )
    codigo = main(
        ["plan", "--filter", "kinds=bot", "--output", str(tmp_path / "p.json")]
    )
    assert codigo == EXIT_CONFIG
    assert "whitelist" in capsys.readouterr().err
