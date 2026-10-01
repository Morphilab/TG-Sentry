"""Configuración TOML validada con pydantic y preservación de comentarios.

`config.example.toml` del repo y la constante EXAMPLE_TOML de este módulo son
la misma plantilla (un test los mantiene sincronizados). Al guardar sobre un
config existente se actualizan valores sin perder los comentarios que el
usuario haya escrito.
"""

from __future__ import annotations

import os
from pathlib import Path

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

import tg_sentry.paths as paths

EXAMPLE_TOML = """\
# ============================================================
# tg-sentry — configuración de ejemplo
# Copia este archivo a ~/.config/tg-sentry/config.toml y rellénalo.
# El archivo real debe tener permisos 0600 (tg-sentry lo verifica).
# ============================================================

[telegram]
# OBTÉN ESTOS VALORES A MANO en https://my.telegram.org
# (API development tools). tg-sentry NO scrapea ese sitio.
# Se validan intentando conectar; si son inválidos, error claro.
api_id = 0                  # entero, ej. 12345678
api_hash = ""               # string de 32 caracteres
phone = ""                  # teléfono con prefijo de país, ej. "+34600000000"
# session = ""              # OPCIONAL: ruta de sesión; vacío = XDG por defecto.
                            # Alternativa: variable de entorno TG_SENTRY_STRING_SESSION
                            # (string-session SOLO por env var, nunca en este archivo).

[safety]
# Peers intocables además de los blindados de fábrica (tu propia cuenta,
# Saved Messages, notificaciones de servicio 777000).
# Acepta ids numéricos (ej. 123456789) y usernames (ej. "durov").
whitelist = []
# No tocar chats privados de contactos — EXCLUSIÓN DURA por defecto.
# false = desactiva la protección de contactos (decisión explícita tuya).
protect_contacts = true
# Aviso informativo: chats privados con actividad más reciente que N días
# aparecen marcados en el plan para revisión (el opt-in por ítem que
# bloquee la operación NO está implementado aún).
recent_private_days = 30

[limits]
# writes_per_min SOLO puede BAJAR respecto al default (4). Subirlo exige
# editar este archivo Y pasar --ack-over-limit al aplicar.
writes_per_min = 4
# Topes por tipo de operación (por hora). Empírico-comunitarios, conservadores.
leave_groups_per_hour = 30
block_per_hour = 50
delete_messages_per_hour = 100
# NOTA: los topes duros de sesión (50 destructivas/hora, 200/día) NO son
# configurables — son constantes del código por diseño.
# FloodWait mayor que este umbral pausa la cola (reanudación visible en TUI).
floodwait_pause_threshold_s = 300
# Segundo FloodWait dentro de esta ventana → pausa indefinida + aviso.
floodwait_pattern_window_s = 300
# Cooldown tras borrar mensajes propios, PROPORCIONAL a lo borrado
# (12s por mensaje con el default: grupo con 3 mensajes tuyos espera 60s,
# uno con 90 espera ~18 min). Se omite si no borró nada o es el último
# paso de borrado. El default (1200 = ~300 msgs/h) es el más prudente;
# 300 = ~1200 msgs/h si priorizas agilidad (borrar tu propio contenido
# es la operación de menor riesgo de la herramienta).
own_messages_cooldown_s = 1200

[plan]
# Re-validación del plan contra estado remoto antes de ejecutar.
ttl_min = 5

[cache]
# TTL del inventario local (SQLite). Un plan nuevo fuerza refresco
# o se marca "basado en caché caducada" con advertencia.
ttl_min = 60

[audit]
# Rotación de la auditoría JSONL (append-only; el activo nunca se trunca).
rotate_bytes = 10485760     # 10 MB
keep_days = 90

[logging]
level = "INFO"
"""


class ConfigError(Exception):
    """Configuración ilegible o inválida, con mensaje accionable."""


class TelegramSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_id: int = 0
    api_hash: str = ""
    phone: str = ""
    session: str = ""

    @field_validator("session")
    @classmethod
    def _session_no_es_string_session(cls, value: str) -> str:
        # StringSession SOLO por TG_SENTRY_STRING_SESSION (invariante del
        # proyecto): una pegada en el TOML se trataría como RUTA y save_config
        # la reescribiría (3ª auditoría). Es una ruta corta; un blob base64
        # largo sin separadores de ruta es una session-string.
        value = value.strip()
        if len(value) > 128 and not any(c in value for c in "/\\"):
            raise ConfigError(
                "telegram.session parece una STRING-SESSION: no va en el TOML "
                "(jamás). Exporta TG_SENTRY_STRING_SESSION en su lugar."
            )
        return value


class SafetySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    whitelist: list[str] = Field(default_factory=list)
    protect_contacts: bool = True
    recent_private_days: int = Field(default=30, ge=0)

    @field_validator("whitelist")
    @classmethod
    def _whitelist_parseable(cls, value: list[str]) -> list[str]:
        # Una whitelist malformada ("--77") NO puede colarse en la config:
        # la protección no se puede parsear → se rechaza en la carga con
        # mensaje (4ª auditoría: antes reventaba tarde, en build_plan del
        # CLI con traceback y en la TUI con pantalla de crash).
        from tg_sentry.safety import parse_whitelist

        try:
            parse_whitelist(value)
        except ValueError as exc:
            raise ValueError(
                f"whitelist inválida en [safety]: {exc}"
            ) from exc
        return value


class LimitsSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    writes_per_min: int = Field(default=4, ge=1)
    leave_groups_per_hour: int = Field(default=30, ge=1)
    block_per_hour: int = Field(default=50, ge=1)
    delete_messages_per_hour: int = Field(default=100, ge=1)
    floodwait_pause_threshold_s: int = Field(default=300, ge=1)
    floodwait_pattern_window_s: int = Field(default=300, ge=1)
    # Cooldown entre pasos de borrado masivo de mensajes propios (1 paso =
    # hasta 100 mensajes). Mínimo 300s: bajarlo por debajo es riesgo real.
    own_messages_cooldown_s: int = Field(default=1200, ge=300)


class PlanSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_min: int = Field(default=5, ge=1)


class CacheSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_min: int = Field(default=60, ge=1)


class AuditSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rotate_bytes: int = Field(default=10_485_760, ge=1024)
    keep_days: int = Field(default=90, ge=1)


class LoggingSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"

    @field_validator("level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            msg = f"level debe ser uno de {sorted(allowed)}, recibido: {value!r}"
            raise ValueError(msg)
        return upper


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telegram: TelegramSection = Field(default_factory=TelegramSection)
    safety: SafetySection = Field(default_factory=SafetySection)
    limits: LimitsSection = Field(default_factory=LimitsSection)
    plan: PlanSection = Field(default_factory=PlanSection)
    cache: CacheSection = Field(default_factory=CacheSection)
    audit: AuditSection = Field(default_factory=AuditSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)


def load_config(path: Path | None = None) -> Config:
    """Carga y valida config.toml; errores → ConfigError con mensaje accionable."""
    file = path if path is not None else paths.config_path()
    try:
        raw = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        msg = (
            f"No existe {file}. Copia config.example.toml a esa ruta y rellena "
            "api_id/api_hash (se obtienen A MANO en https://my.telegram.org, "
            "sección API development tools)."
        )
        raise ConfigError(msg) from None
    except OSError as exc:
        msg = f"No se puede leer {file}: {exc}"
        raise ConfigError(msg) from exc
    try:
        doc = tomlkit.parse(raw)
    except Exception as exc:  # tomlkit lanza tipos variados según el fallo
        msg = f"{file} no es TOML válido: {exc}"
        raise ConfigError(msg) from exc
    try:
        return Config.model_validate(doc.unwrap())
    except ValidationError as exc:
        msg = f"{file} inválido:\n{exc}"
        raise ConfigError(msg) from exc


def save_config(config: Config, path: Path | None = None) -> Path:
    """Guarda la configuración preservando comentarios; fuerza 0600.

    Sobre un archivo existente actualiza valores en el documento original
    (los comentarios del usuario sobreviven). En archivo nuevo parte de
    EXAMPLE_TOML para que nazca documentado. La higiene 0700 del directorio
    SOLO aplica a la ruta por defecto de la app: las rutas explícitas del
    usuario no se tocan (no somos dueños de sus permisos).
    """
    file = path if path is not None else paths.config_path()
    if path is None:
        paths.ensure_private_dir(file.parent)
    else:
        file.parent.mkdir(parents=True, exist_ok=True)
    if file.exists():
        doc = tomlkit.parse(file.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.parse(EXAMPLE_TOML)
    _apply_to_doc(config, doc)
    # tmp + replace con 0600 EN LA CREACIÓN (2ª auditoría): el api_hash y el
    # teléfono nacían con umask (0644) hasta el chmod final; un crash a mitad
    # de write_text dejaba además un TOML truncado
    payload = tomlkit.dumps(doc)
    tmp = file.with_suffix(f".tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(file)
    file.chmod(0o600)
    return file


def _apply_to_doc(config: Config, doc: tomlkit.TOMLDocument) -> None:
    data = config.model_dump()
    for section_name, section in data.items():
        if section_name not in doc:
            doc[section_name] = tomlkit.table()
        table = doc[section_name]
        for key, value in section.items():
            table[key] = value
