# Modelo de seguridad y amenazas — tg-sentry

> Estado: **v0.1.0 (documento vivo)** — se amplía con cada mejora.

## Advertencia principal (no negociable)

**Esta herramienta puede provocar la suspensión (baneo) de tu cuenta de
Telegram.** Telegram penaliza acciones masivas automatizadas aunque se
respeten los FloodWait. tg-sentry aplica límites conservadores, pero **ningún
software elimina ese riesgo**. Ninguna salvaguarda de este documento debe
leerse como "baneo imposible".

## Modelo de amenazas

| Amenaza | Mitigación estructural | Residual |
|---|---|---|
| Baneo/suspensión por acciones masivas | Rate limit conservador (4/min, solo configurable a la baja), topes por tipo y de sesión (50/h, 200/día en código), FloodWait con prioridad absoluta, pausa indefinida ante patrón de baneo | **Alto e inevitable** — es el riesgo primario |
| Borrado irreversible por error | Dry-run por defecto, plan congelado + re-validación TTL 5 min, confirmación tecleada `CONFIRMAR`, lista de blindados excluidos visible | Bajo |
| Pérdida de trazabilidad | Auditoría JSONL append-only con rotación; estados diferenciados (`done`/`skipped_ok`/`skipped_unreachable`/`skipped_external`/`failed`) | Bajo |
| Fuga de credenciales | Sesión en XDG 0600 (dir 0700), StringSession solo por env var, config 0600, logs redactados (api_hash, api_id, teléfono, códigos, 2FA) | Bajo |
| Operar sobre lo que no se debía | Peers blindados de fábrica (propia cuenta, Saved Messages, 777000, whitelist), protección de contactos por defecto; la protección «propios como creador» está pendiente de verificar el rol por chat (hoy avisa «rol sin verificar») | Bajo |

## Invariantes de diseño

1. Nada destructivo sin plan + confirmación + auditoría. Sin excepciones.
2. Las rarezas de la API viven solo en `tg/ops.py`.
3. Los topes duros de sesión son constantes de código, no de config.
4. FloodWait manda sobre cualquier tasa local.
5. Tests jamás tocan Telegram ni cuentas reales.

## Superficie local

| Qué | Ruta | Permisos |
|---|---|---|
| Config | `~/.config/tg-sentry/config.toml` | 0600 |
| Sesión | `~/.local/share/tg-sentry/` | dir 0700, archivo 0600 |
| Logs | `~/.local/state/tg-sentry/logs/` | dir 0700 |
| Auditoría | `~/.local/state/tg-sentry/audit/` | dir 0700 |
| Exports | `~/.local/state/tg-sentry/exports/` | dir 0700 |

Los exports (JSON/CSV) contienen datos personales (nombres, ids, usernames):
se avisa en la UI/CLI al crearlos y nacen con permisos 0600. **NO quedan
registrados en la auditoría** (son copias de solo lectura fuera del motor):
su ubicación es responsabilidad del usuario.
