# tg-sentry

[English](README.md) | **Español**

Monitor y limpiador de cuenta de Telegram — **seguro por diseño**.

> ## ⚠️ ADVERTENCIA — lee esto antes de usar la herramienta
>
> **Esta herramienta puede provocar la suspensión (baneo) de tu cuenta de
> Telegram.** Telegram penaliza las acciones masivas automatizadas (salir de
> grupos, bloquear usuarios, borrar historiales) aunque se respeten los
> FloodWait. tg-sentry aplica límites conservadores y pausas automáticas, pero
> **ningún software puede eliminar ese riesgo**. Úsala bajo tu responsabilidad,
> con cuenta propia y a un ritmo prudente.
>
> Además, los borrados en Telegram son **irreversibles**: todo pasa por
> dry-run → plan → confirmación explícita → auditoría, pero lo ejecutado no se
> puede deshacer.

> ⚠️ **AI Disclosure / Divulgación de IA**
>
> **English:** This project was developed with assistance from artificial intelligence
> tools. Given the automated nature of some components, users are advised to review
> and test the code independently before integrating it into their own systems.
>
> **Español:** Este proyecto fue desarrollado con asistencia de herramientas de
> inteligencia artificial. Dada la naturaleza automatizada de algunos componentes,
> se recomienda que los usuarios revisen y prueben el código independientemente antes
> de integrarlo en sus propios sistemas.

## Qué hace

- **Inventario** de todos tus diálogos (grupos, canales, bots, chats privados)
  con filtros combinables (tipo, nombre, regex, inactividad, archivados…).
- **Limpieza con pasos correctos por tipo**:
  - Bots: eliminar chat + bloquear.
  - Grupos/canales: salir (darse de baja).
  - Privados: eliminar ± revoke (≤48h borra del otro lado) ± bloquear.
  - **Mensajes propios** en cualquier chat: dos modos — solo-para-ti (sin
    límite de edad; en chats privados, bots y grupos básicos) o para-todos
    (≤48h; fuera de 48h el servidor degrada a solo-para-ti). En canales y
    supergrupos la API borra SIEMPRE para todos: el modo solo-para-ti se
    rechaza ahí, nunca se ejecuta un borrado con la promesa invertida.
    Con opción de NO salir del chat (limpiar y conservar).
- **Seguro por diseño**: dry-run por defecto, peers blindados (tu cuenta,
  Saved Messages, 777000, whitelist, chats secretos; la protección de
  «donde eres creador» queda pendiente de verificar tu rol en cada chat y
  hoy avisa «rol sin verificar»),
  re-validación del plan (TTL 5 min), auditoría JSONL con rotación y export.
- **Anti-baneo estructural**: token bucket (~4 ops/min, solo a la baja),
  caps por tipo (30 salidas/h, 50 bloqueos/h), topes duros de sesión
  (50 destructivas/h, 200/día — constantes de código), FloodWait respetado
  con prioridad absoluta (gracia 2× tras cada espera), pausa indefinida ante
  patrón de baneo (2º FloodWait <5 min), cooldown extra entre pasos de
  borrado masivo de mensajes.
- **TUI completa** (Textual): dashboard filtrable, selección masiva,
  plan dry-run, confirmación tecleada, ejecución con barra/ETA, resultados
  agrupados, auditoría revisable y exportable, ajustes.
- **CLI headless** para automatizar el mismo motor.

## Estado

| Milestone | Alcance | Estado |
|---|---|---|
| M0 | Scaffolding, config, sesión segura, login 2FA | ✅ |
| M1 | Inventario + piloto de semánticas de la API | ✅ |
| M2 | Planificador + safety + auditoría | ✅ |
| M3 | Ejecutor + límites + CLI headless | ✅ |
| M4 | TUI base | ✅ |
| M5 | TUI de ejecución + auditoría | ✅ |
| M6 | Mensajes propios + export + docs finales | ✅ |

## Instalación

Requiere Python ≥ 3.11.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
pip install -e .[dev]   # desarrollo
```

## Uso

### TUI (recomendado)

```bash
tg-sentry tui
```

1. Login (si no hay sesión): teléfono → código → 2FA.
2. Dashboard: filtra (tipos, nombre, regex, inactividad, archivados, unread).
3. Selecciona: `espacio` marca, `a` todo lo filtrado, `i` invierte, `n` limpia.
   Los blindados aparecen con 🔒 y jamás se pueden marcar.
4. `p` → plan dry-run (pasos por peer, opciones, avisos, excluidos).
5. `c` → confirmación: contador de destructivas, blindados excluidos, aviso
   de suspensión; teclea el token literal `CONFIRMAR` + Enter.
6. Ejecución con barra/ETA → Resultados. `v` auditoría, `o` ajustes.

### CLI headless

```bash
tg-sentry login
tg-sentry dialogs --refresh              # inventario (solo lectura)
tg-sentry plan --filter "kinds=bot,inactive=365" --output plan.json
tg-sentry apply --plan plan.json --confirm CONFIRMAR [--limit-peers N]
tg-sentry audit [--since 2025-01-01] [--export csv]
tg-sentry export --format json|csv [--output ruta]
tg-sentry doctor                          # diagnóstico de rutas y permisos
```

Filtros: `kinds=bot+channel+group+user`, `name=…`, `regex=…`, `inactive=Nd`,
`archived=true|false|any`, `unread_min=N`.
Opciones de plan: `--delete-own me|all|off` (mensajes propios),
`--no-leave` (limpiar sin salir).

Ejemplos:

```bash
# 10 bots viejos como piloto (~20 ops, ~5 min)
tg-sentry plan --filter "kinds=bot,inactive=365" --output plan.json
tg-sentry apply --plan plan.json --confirm CONFIRMAR --limit-peers 10

# Limpiar TUS mensajes de un grupo que conservas (sin salir)
tg-sentry plan --filter "name=ejemplo" --delete-own me --no-leave --output plan.json

# Canales inactivos >6 meses (darse de baja)
tg-sentry plan --filter "kinds=channel,inactive=180" --output plan.json
```

La configuración vive en `~/.config/tg-sentry/config.toml` (ver
`config.example.toml`), la sesión en `~/.local/share/tg-sentry/` (permisos
`0600` verificados), logs y auditoría en `~/.local/state/tg-sentry/`.

## Documentación

- `docs/security.md` — modelo de amenazas y advertencias
- `docs/telegram-limits.md` — límites conocidos (empíricos) y semánticas

La documentación de diseño (decisiones técnicas, hallazgos del spike y el
protocolo del piloto) es interna del proyecto y no se publica.

## Licencia

MIT.
