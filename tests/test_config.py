"""Tests de configuración: carga, validación, guardado y sincronía con el ejemplo."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from tg_sentry.config import EXAMPLE_TOML, Config, ConfigError, load_config, save_config

REPO_EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def test_example_toml_del_repo_sincronizado_con_el_modulo() -> None:
    assert REPO_EXAMPLE.read_text(encoding="utf-8") == EXAMPLE_TOML


def test_load_config_desde_plantilla_completa(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text(EXAMPLE_TOML, encoding="utf-8")
    config = load_config(file)
    assert config.telegram.api_id == 0
    assert config.safety.protect_contacts is True
    assert config.limits.writes_per_min == 4
    assert config.plan.ttl_min == 5
    assert config.logging.level == "INFO"


def test_load_config_archivo_inexistente_mensaje_accionable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"config\.example\.toml"):
        load_config(tmp_path / "no-existe.toml")


def test_load_config_toml_roto(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text("[telegram\napi_id = ??", encoding="utf-8")
    with pytest.raises(ConfigError, match="TOML"):
        load_config(file)


def test_load_config_valor_invalido(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text('[logging]\nlevel = "BROKEN"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="inválido"):
        load_config(file)


def test_save_config_crea_desde_plantilla_con_0600(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    config = Config()
    config.telegram.api_id = 12345678
    config.telegram.api_hash = "a" * 32
    save_config(config, file)
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    reloaded = load_config(file)
    assert reloaded.telegram.api_id == 12345678
    assert reloaded.telegram.api_hash == "a" * 32


def test_save_config_preserva_comentarios_del_usuario(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text(
        "# COMENTARIO CLAVE DEL USUARIO\n[telegram]\napi_id = 111\napi_hash = \"\"\n",
        encoding="utf-8",
    )
    config = load_config(file)
    config.telegram.api_id = 222
    save_config(config, file)
    content = file.read_text(encoding="utf-8")
    assert "COMENTARIO CLAVE DEL USUARIO" in content
    assert load_config(file).telegram.api_id == 222


def test_secciones_desconocidas_rechazadas(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text("[secreta]\nalgo = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(file)


def test_load_config_whitelist_malformada_rechazada_en_carga(tmp_path: Path) -> None:
    """4ª auditoría: una whitelist no parseable ("--77") se rechaza EN LA
    CARGA con mensaje accionable — antes reventaba tarde (traceback en el
    CLI `plan`, pantalla de crash de Textual en la TUI al recargar)."""
    file = tmp_path / "config.toml"
    file.write_text('[safety]\nwhitelist = ["--77"]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="whitelist"):
        load_config(file)


def test_load_config_whitelist_valida_aceptada(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text(
        '[safety]\nwhitelist = ["123456789", "@Durov", "-1001234567890"]\n',
        encoding="utf-8",
    )
    config = load_config(file)
    assert config.safety.whitelist == ["123456789", "@Durov", "-1001234567890"]
