"""CLI de tg-sentry.

M0: login interactivo, doctor.
M1: dialogs contra caché SQLite (--refresh para actualizar), export JSON/CSV.
M3: plan (dry-run desde caché) · apply (destructivo, --confirm CONFIRMAR,
     reanudable) · audit (resumen de la auditoría). La TUI (M4+) es aparte.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

import telethon
from telethon import TelegramClient

import tg_sentry
import tg_sentry.paths as paths
from tg_sentry import errors as tg_errors
from tg_sentry import export as exporting
from tg_sentry import inventory, redact, runstate
from tg_sentry.audit import AuditLog, StepOutcome
from tg_sentry.config import Config, ConfigError, load_config, save_config
from tg_sentry.errors import PlanExpiredError
from tg_sentry.executor import EngineLimits, ExecutionEngine
from tg_sentry.export import Format
from tg_sentry.filters import FilterError, apply_filters, parse_filter_expr
from tg_sentry.limits import LimitsPolicyError, validate_policy
from tg_sentry.logging_setup import setup_logging
from tg_sentry.planner import (
    ActionPlan,
    PlannerOptions,
    StepKind,
    blindados_en_plan,
    build_plan,
)
from tg_sentry.tg.client import (
    CredentialsError,
    SesionNoAutorizada,
    connect_and_login,
    segundos_flood,
)
from tg_sentry.tg.ops import TeleOpsGateway

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_NO_SESSION = 3


def _run_tui() -> int:
    """Arranca la TUI. La config se carga dentro (errores se muestran en UI)."""
    from tg_sentry.logging_setup import setup_logging
    from tg_sentry.tui.app import SentryApp

    # Sin esto, cualquier excepción de la TUI era invisible (hallazgo M4):
    # el CLI configuraba logging, la TUI no. El nivel viene del TOML si es
    # legible; si no, INFO (la pantalla de error de la TUI hará el resto).
    nivel = logging.INFO
    try:
        from tg_sentry.config import load_config

        nivel = getattr(logging, load_config().logging.level, logging.INFO)
    except Exception:
        pass
    setup_logging(nivel)
    # mismo registro de secretos que el CLI: la redacción solo actúa sobre lo
    # registrado, y la TUI loguea excepciones (3ª auditoría). Si el TOML no
    # carga, la TUI mostrará el error en UI sin estos valores.
    try:
        from tg_sentry import redact as _redact

        telegram = load_config().telegram
        _redact.register_secret(telegram.api_hash)
        _redact.register_secret(telegram.api_id)
        _redact.register_secret(telegram.phone)
    except Exception:
        pass
    SentryApp().run()
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tg-sentry",
        description="Monitor y limpiador de cuenta de Telegram — seguro por diseño.",
    )
    parser.add_argument("--version", action="version", version=f"tg-sentry {tg_sentry.__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="Login interactivo: teléfono → código → 2FA si aplica")

    dialogs = sub.add_parser("dialogs", help="Listar diálogos desde la caché local")
    dialogs.add_argument("--limit", type=int, default=20, help="Cuántos diálogos listar")
    dialogs.add_argument(
        "--refresh", action="store_true", help="Actualizar la caché primero (solo lectura)"
    )

    export_cmd = sub.add_parser("export", help="Exportar la caché del inventario a JSON/CSV")
    export_cmd.add_argument("--format", choices=("json", "csv"), default="csv")
    export_cmd.add_argument("--output", type=str, default=None, help="Ruta destino (default XDG)")

    plan_cmd = sub.add_parser(
        "plan", help="Construir un plan (dry-run) desde la caché y guardarlo"
    )
    plan_cmd.add_argument(
        "--filter",
        type=str,
        default="",
        help="kinds=bot+channel,regex=…,inactive=90,archived=any,unread_min=N"
        " (nota: el regex no admite comas — usa la TUI para esos patrones)",
    )
    plan_cmd.add_argument(
        "--output", type=str, default=None, help="Ruta del plan (default XDG runs/)"
    )
    plan_cmd.add_argument(
        "--allow-stale", action="store_true", help="Permitir caché caducada (el plan queda marcado)"
    )
    plan_cmd.add_argument(
        "--delete-own",
        choices=("me", "all", "off"),
        default="off",
        help="Borrar tus mensajes propios: me=solo para ti · all=para todos (≤48h) · off",
    )
    plan_cmd.add_argument(
        "--no-leave",
        action="store_true",
        help="NO salir: solo limpiar mensajes propios (chats que conservas)",
    )

    apply_cmd = sub.add_parser("apply", help="Ejecutar un plan confirmado (destructivo)")
    apply_cmd.add_argument("--plan", type=str, required=True, help="Ruta del plan JSON")
    apply_cmd.add_argument(
        "--confirm", type=str, default="", help='Debe ser exactamente "CONFIRMAR"'
    )
    apply_cmd.add_argument(
        "--ack-over-limit", action="store_true", help="Reconoce límites subidos a mano en el TOML"
    )
    apply_cmd.add_argument(
        "--limit-peers", type=int, default=None, help="Ejecutar solo los primeros N peers (piloto)"
    )

    audit_cmd = sub.add_parser("audit", help="Resumen de la auditoría")
    audit_cmd.add_argument("--since", type=str, default=None, help="Fecha ISO (ej. 2026-09-19)")
    audit_cmd.add_argument(
        "--export", choices=("csv", "json"), default=None, help="Exportar los registros filtrados"
    )
    audit_cmd.add_argument("--output", type=str, default=None, help="Ruta destino (default XDG)")

    sub.add_parser("tui", help="Interfaz de terminal completa (Textual)")

    sub.add_parser("doctor", help="Diagnóstico de rutas, permisos y versiones")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "tui":
        return _run_tui()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error de configuración: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    level = getattr(logging, config.logging.level, logging.INFO)
    log_path = setup_logging(level)
    redact.register_secret(config.telegram.api_hash)
    redact.register_secret(config.telegram.api_id)
    redact.register_secret(config.telegram.phone)

    def _excepthook(
        tipo: type[BaseException], valor: BaseException, traza: TracebackType | None
    ) -> None:
        # los tracebacks crudos por stderr NO pasan por logging: un str(exc)
        # con datos del usuario escapaba sin redactar (2ª auditoría)
        logger.critical("excepción no capturada", exc_info=(tipo, valor, traza))

    sys.excepthook = _excepthook
    logger.debug("config cargada; log en %s", log_path)

    try:
        return _despachar(config, args, log_path)
    except tg_errors.FloodWait as exc:
        print(
            f"Telegram pide esperar {exc.seconds}s (FloodWait): reintenta más tarde.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except tg_errors.SessionInvalid as exc:
        print(f"sesión inválida ({exc}): re-haz tg-sentry login", file=sys.stderr)
        return EXIT_NO_SESSION
    except tg_errors.TransientNetwork as exc:
        print(f"fallo transitorio de red: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ConnectionError as exc:
        # connect() de Telethon lanza ConnectionError crudo (login/dialogs):
        # mensaje accionable, no traceback (3ª auditoría)
        print(f"sin conexión con Telegram: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except tg_errors.StepError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SesionNoAutorizada as exc:
        print(f"sesión no autorizada ({exc}): re-haz tg-sentry login", file=sys.stderr)
        return EXIT_NO_SESSION
    except CredentialsError as exc:
        print(f"credenciales: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        # Ctrl+C en login/dialogs/export (apply lo gestiona dentro con su
        # propio mensaje de reanudación): salida limpia, no traceback (4ª aud.)
        print("\n⏹ interrumpido", file=sys.stderr)
        return EXIT_ERROR


def _despachar(config: Config, args: argparse.Namespace, log_path: Path) -> int:
    if args.command == "login":
        return asyncio.run(_cmd_login(config))
    if args.command == "dialogs":
        return asyncio.run(_cmd_dialogs(config, args.limit, args.refresh))
    if args.command == "export":
        return _cmd_export(args.format, args.output)
    if args.command == "plan":
        return _cmd_plan(
            config, args.filter, args.output, args.allow_stale, args.delete_own, args.no_leave
        )
    if args.command == "apply":
        try:
            return asyncio.run(
                _cmd_apply(
                    config, args.plan, args.confirm, args.ack_over_limit, args.limit_peers
                )
            )
        except KeyboardInterrupt:
            # el estado se guarda tras cada paso: corta ANTES del siguiente
            print(
                "\n⏹ interrumpido antes del siguiente paso; "
                "reanuda con el mismo comando apply"
            )
            return EXIT_ERROR
    if args.command == "audit":
        return _cmd_audit(args.since, args.export, args.output)
    if args.command == "doctor":
        return _cmd_doctor(log_path)
    return EXIT_ERROR


async def _cmd_login(config: Config) -> int:
    phone = config.telegram.phone
    phone_nuevo = False
    if not phone:
        phone = input("Teléfono (con prefijo de país, ej. +34600000000): ").strip()
        redact.register_secret(phone)
        phone_nuevo = True
    try:
        client = await connect_and_login(
            config.telegram.api_id,
            config.telegram.api_hash,
            phone,
            config.telegram.session or None,
        )
    except CredentialsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if phone_nuevo:
        config.telegram.phone = phone
        saved = save_config(config)
        print(f"  Teléfono guardado en {saved}")
    try:
        me = await client.get_me()
        name = getattr(me, "first_name", None) or getattr(me, "username", None) or "?"
        print(f"✓ Sesión autorizada como: {name}")
        session_file = paths.session_path()
        print(f"  Sesión: {session_file} (permisos {oct(paths.file_mode(session_file) or 0)})")
        return EXIT_OK
    finally:
        await client.disconnect()


async def _connect_authorized(config: Config) -> TelegramClient | None:
    """Conecta con la sesión existente; None (con mensaje) si no hay sesión."""
    if not paths.session_path().exists():
        print("No hay sesión. Ejecuta antes: tg-sentry login", file=sys.stderr)
        return None
    return await connect_and_login(
        config.telegram.api_id,
        config.telegram.api_hash,
        config.telegram.phone,
        config.telegram.session or None,
    )


async def _cmd_dialogs(config: Config, limit: int, refresh: bool) -> int:
    if refresh or inventory.fetched_at() is None:
        print(
            "Actualizando inventario completo (solo lectura)…"
            if refresh
            else "Sin caché: actualizando inventario completo…"
        )
        client = await _connect_authorized(config)
        if client is None:
            return EXIT_NO_SESSION
        try:

            def progress(count: int) -> None:
                print(f"  …{count} diálogos", end="\r", flush=True)

            try:
                stats = await inventory.refresh(client, progress=progress)
            except ConnectionError as exc:
                print(f"sin conexión con Telegram: {exc}", file=sys.stderr)
                return EXIT_ERROR
            except Exception as exc:
                espera = segundos_flood(exc)
                if espera is None:
                    raise
                # iter_dialogs puede recibir FloodWait refrescando cientos de
                # diálogos: accionable, no traceback (3ª auditoría)
                print(
                    f"Telegram pide esperar {espera}s (FloodWait): "
                    "reintenta más tarde.",
                    file=sys.stderr,
                )
                return EXIT_ERROR
            print(f"✓ Inventario: {stats.total} diálogos — {_fmt_by_kind(stats.by_kind)}")
        finally:
            await client.disconnect()

    age = inventory.cache_age()
    if inventory.is_stale(config.cache.ttl_min):
        print(f"⚠ caché caducada (edad {_fmt_age(age)}); usa --refresh para actualizar")

    try:
        dialogs = inventory.load()
    except inventory.CacheIncompatible as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"{'id':>14}  {'tipo':<7}  {'arch':<4} {'unread':>7}  nombre")
    for dialog in dialogs[:limit]:
        print(
            f"{dialog.id:>14}  {dialog.kind.value:<7}  "
            f"{'sí' if dialog.archived else 'no':<4} {dialog.unread:>7}  {dialog.display}"
        )
    remaining = len(dialogs) - min(limit, len(dialogs))
    if remaining > 0:
        print(f"… y {remaining} más (total {len(dialogs)})")
    return EXIT_OK


def _cmd_export(fmt: Format, output: str | None) -> int:
    if inventory.fetched_at() is None:
        print("Sin caché: ejecuta antes tg-sentry dialogs --refresh", file=sys.stderr)
        return EXIT_NO_SESSION
    try:
        dialogs = inventory.load()
    except inventory.CacheIncompatible as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    target = exporting.export(
        dialogs, fmt, Path(output).expanduser() if output else None
    )
    print(f"✓ {len(dialogs)} diálogos exportados a {target} (permisos 0600)")
    print("  AVISO: el export contiene datos personales (nombres, ids, usernames).")
    return EXIT_OK


def _cmd_plan(
    config: Config,
    filter_expr: str,
    output: str | None,
    allow_stale: bool,
    delete_own: str = "off",
    no_leave: bool = False,
) -> int:
    """Construye el plan (dry-run) desde caché. NADA se ejecuta aquí."""
    if inventory.fetched_at() is None:
        print("Sin caché: ejecuta antes tg-sentry dialogs --refresh", file=sys.stderr)
        return EXIT_NO_SESSION
    stale = inventory.is_stale(config.cache.ttl_min)
    if stale and not allow_stale:
        print(
            f"caché caducada (edad {_fmt_age(inventory.cache_age())}); "
            "usa dialogs --refresh o --allow-stale",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    try:
        filtros = parse_filter_expr(filter_expr)
        seleccion = apply_filters(inventory.load(), filtros)
    except FilterError as exc:
        print(f"error de filtro: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except inventory.CacheIncompatible as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    # Fail-closed: sin identidad propia NO hay plan (auditoría 2026-09).
    try:
        me = inventory.me_id()
    except inventory.CacheIncompatible as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if me is None:
        print(
            "caché sin me_id (identidad de tu cuenta): ejecuta tg-sentry dialogs --refresh",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    try:
        plan = build_plan(
            seleccion,
            PlannerOptions(
                ttl_min=config.plan.ttl_min,
                based_on_cache_fresh=not stale,
                delete_own=None if delete_own == "off" else delete_own,  # type: ignore[arg-type]
                leave=not no_leave,
            ),
            me_id=me,
            whitelist=config.safety.whitelist,
            contact_ids=inventory.contact_ids(),
            protect_contacts=config.safety.protect_contacts,
            recent_private_days=config.safety.recent_private_days,
        )
    except ValueError as exc:
        # whitelist malformada u otra entrada inválida (defensa en profundidad:
        # load_config ya la rechaza): mensaje accionable, no traceback (4ª aud.)
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    destino = (
        Path(output).expanduser()
        if output
        else runstate.runs_dir() / f"plan-{plan.plan_id}.json"
    )
    if output:
        destino.parent.mkdir(parents=True, exist_ok=True)  # ruta del usuario: sin chmod
    else:
        paths.ensure_private_dir(destino.parent)  # dir de la app: higiene 0700
    # 0600 desde la creación (2ª auditoría: el plan lleva nombres de diálogos)
    with exporting.abrir_0600(destino) as handle:
        handle.write(plan.model_dump_json(indent=1))
    destino.chmod(0o600)

    print(f"Plan {plan.plan_id} (DRY-RUN, nada ejecutado)")
    print(f"  {len(plan.peers)} peers · {plan.destructive_count()} operaciones destructivas")
    for paso, cantidad in sorted(plan.counts_by_step().items()):
        print(f"    {cantidad:>4} x {paso}")
    if plan.excluded:
        print(f"  Excluidos ({len(plan.excluded)}):")
        for peer_id, razon in plan.excluded[:10]:
            print(f"    — {peer_id}: {razon}")
        if len(plan.excluded) > 10:
            print(f"    … y {len(plan.excluded) - 10} más")
    avisos = [(p.dialog.id, aviso) for p in plan.peers for aviso in p.warnings]
    if avisos:
        print(f"  Avisos ({len(avisos)}):")
        for peer_id, aviso in avisos[:10]:
            print(f"    ⚠ {peer_id}: {aviso}")
    if stale:
        print("  ⚠ plan BASADO EN CACHÉ CADUCADA (--allow-stale)")
    print(f"Guardado en {destino}")
    print(
        f"Expira en {config.plan.ttl_min} min. Para ejecutar (DESTRUCTIVO):\n"
        f"  tg-sentry apply --plan {destino} --confirm CONFIRMAR"
    )
    return EXIT_OK


async def _cmd_apply(
    config: Config,
    plan_path: str,
    confirm: str,
    ack_over_limit: bool,
    limit_peers: int | None,
) -> int:
    if confirm != "CONFIRMAR":
        print('error: --confirm debe ser exactamente "CONFIRMAR"', file=sys.stderr)
        return EXIT_CONFIG
    if limit_peers is not None and limit_peers < 1:
        # negativo cortaría por el final (peers[:-1] = casi todo el plan)
        print("error: --limit-peers debe ser ≥ 1", file=sys.stderr)
        return EXIT_CONFIG
    try:
        validate_policy(config.limits, ack_over_limit=ack_over_limit)
    except LimitsPolicyError as exc:
        print(f"error de límites: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        plan = ActionPlan.model_validate_json(
            Path(plan_path).expanduser().read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        print(f"no se puede leer el plan {plan_path}: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if plan.is_expired():
        print(
            f"El plan expiró (TTL {config.plan.ttl_min} min): regénéralo con tg-sentry plan",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    # Defensa en profundidad: re-validar exclusiones duras sobre el plan JSON
    # (auditoría 2026-09): un plan con bug/edited a mano NO se ejecuta.
    try:
        me_actual = inventory.me_id()
        contactos = inventory.contact_ids()
    except inventory.CacheIncompatible as exc:
        # caché incompatible a mitad de sesión: fail-closed con mensaje
        # (3ª auditoría: traceback crudo fuera del try)
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if me_actual is None:
        print(
            "caché sin me_id: ejecuta tg-sentry dialogs --refresh antes de aplicar",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    try:
        blindados = blindados_en_plan(
            plan,
            me_id=me_actual,
            whitelist=config.safety.whitelist,
            contact_ids=contactos,
            protect_contacts=config.safety.protect_contacts,
        )
    except ValueError as exc:
        # whitelist no parseable (defensa en profundidad, 4ª auditoría):
        # la re-validación de blindados NO es un traceback — es un rechazo
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if blindados:
        for peer_id, razon in blindados[:5]:
            print(f"BLINDADO en el plan: {peer_id} — {razon}", file=sys.stderr)
        print(
            "El plan contiene peers blindados: NO se ejecuta. "
            "Regénéralo con tg-sentry plan.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    try:
        completados = runstate.load_completed(plan)
    except runstate.RunstateCorrupto as exc:
        # fail-closed: un runstate corrupto NO se salta (re-ejecutaría done)
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    pendientes = sum(
        1
        for peer in plan.peers
        for step in peer.steps
        if (peer.dialog.id, step.kind.value) not in completados
    )
    if pendientes == 0:
        print("Nada pendiente: el plan ya está ejecutado por completo.")
        return EXIT_OK
    print(
        f"Plan {plan.plan_id}: {len(plan.peers)} peers, {pendientes} pasos pendientes"
        + (" (reanudación)" if completados else "")
    )

    client = await _connect_authorized(config)
    if client is None:
        return EXIT_NO_SESSION
    motor = ExecutionEngine(
        TeleOpsGateway(client),
        EngineLimits(
            writes_per_min=config.limits.writes_per_min,
            leave_per_hour=config.limits.leave_groups_per_hour,
            block_per_hour=config.limits.block_per_hour,
            delete_messages_per_hour=config.limits.delete_messages_per_hour,
            floodwait_pause_threshold_s=config.limits.floodwait_pause_threshold_s,
            floodwait_pattern_window_s=config.limits.floodwait_pattern_window_s,
            # antes el CLI ignoraba este valor del TOML (divergencia con la
            # TUI en dirección INSEGURA) — auditoría 2026-09
            own_messages_cooldown_s=config.limits.own_messages_cooldown_s,
        ),
        AuditLog(rotate_bytes=config.audit.rotate_bytes, keep_days=config.audit.keep_days),
    )
    estado: set[runstate.CompletedStep] = set(completados)

    def tras_paso(peer_id: int, paso: StepKind, resultado: StepOutcome) -> None:
        estado.add(runstate.step_key(peer_id, paso))
        runstate.save(plan, estado)
        print(f"  [{resultado.value:>18}] peer {peer_id} · {paso.value}")

    try:
        # lock por plan: CLI y TUI no pueden correr el MISMO plan a la vez
        with runstate.ejecucion_exclusiva(plan) as exclusivo:
            if not exclusivo:
                print(
                    "ya hay una ejecución activa de este plan (¿la TUI?): "
                    "no se lanza otra",
                    file=sys.stderr,
                )
                return EXIT_ERROR
            try:
                resultado = await motor.run(
                    plan, completed=estado, after_step=tras_paso, max_peers=limit_peers
                )
            except PlanExpiredError:
                # ventana TOCTOU: el plan venció TRAS el check inicial y una
                # espera de presupuesto (2ª auditoría) — accionable, no traceback
                print(
                    f"El plan expiró mientras se ejecutaba (TTL "
                    f"{config.plan.ttl_min} min): regénéralo con tg-sentry plan "
                    "y vuelve a aplicar.",
                    file=sys.stderr,
                )
                return EXIT_ERROR
    finally:
        await client.disconnect()

    partes = [f"{resultado.done} done"]
    partes.extend(f"{n} {k}" for k, n in sorted(resultado.skipped.items()))
    partes.append(f"{resultado.failed} failed")
    print("Resultado: " + " · ".join(partes))
    if resultado.aborted:
        print(f"⚠ COLA ABORTADA: {resultado.aborted}")
        print("  Los pasos pendientes quedan reanudables con el mismo comando.")
    return EXIT_OK if resultado.aborted is None else EXIT_ERROR


def _cmd_audit(since: str | None, export_fmt: str | None, output: str | None) -> int:
    registros = AuditLog().read()
    if since:
        try:
            limite = datetime.fromisoformat(since)
        except ValueError:
            print(f"fecha inválida: {since!r} (se espera ISO, ej. 2026-09-19)", file=sys.stderr)
            return EXIT_CONFIG
        if limite.tzinfo is None:
            # los ts de auditoría son aware: comparar con naive era TypeError
            # crudo (auditoría 2026-09) — una fecha sin zona es UTC
            limite = limite.replace(tzinfo=UTC)
        registros = [r for r in registros if r.ts >= limite]

    if export_fmt:
        destino = (
            Path(output).expanduser()
            if output
            else (
                paths.exports_dir()
                / f"auditoria-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}.{export_fmt}"
            )
        )
        if output:
            destino.parent.mkdir(parents=True, exist_ok=True)
        else:
            paths.ensure_private_dir(destino.parent)
        _export_audit(registros, destino, export_fmt)
        print(f"✓ {len(registros)} registros exportados a {destino} (permisos 0600)")
        print("  AVISO: la auditoría contiene ids de peers y razones de fallo.")
        return EXIT_OK

    if not registros:
        print("Auditoría vacía.")
        return EXIT_OK
    por_resultado: Counter[str] = Counter(r.outcome.value for r in registros)
    por_paso: Counter[str] = Counter(r.step for r in registros)
    print(f"Auditoría: {len(registros)} registros")
    print("  por resultado: " + " · ".join(f"{n} {k}" for k, n in sorted(por_resultado.items())))
    print("  por paso:     " + " · ".join(f"{n} {k}" for k, n in sorted(por_paso.items())))
    print("  últimos:")
    for r in registros[-15:]:
        razon = f" — {r.reason}" if r.reason else ""
        print(f"    {r.ts:%Y-%m-%d %H:%M} [{r.outcome.value:>19}] {r.peer_id} {r.step}{razon}")
    return EXIT_OK


def _export_audit(registros: list, destino: Path, fmt: str) -> None:
    import csv as csv_mod
    import json as json_mod

    from tg_sentry import export as export_mod

    filas = [
        {
            "ts": r.ts.isoformat(),
            "plan_id": r.plan_id,
            "peer_id": r.peer_id,
            "step": r.step,
            "outcome": r.outcome.value,
            "reason": r.reason or "",
        }
        for r in registros
    ]
    # 0600 DESDE LA CREACIÓN (2ª auditoría P1: write_text + chmod final dejaba
    # los peer_ids world-readable durante toda la escritura, y para siempre si
    # había crash) + neutralización de fórmulas (reason puede traer texto de
    # terceros vía excepciones)
    campos = list(filas[0].keys()) if filas else [
        "ts", "plan_id", "peer_id", "step", "outcome", "reason"
    ]
    if fmt == "json":
        with export_mod.abrir_0600(destino) as handle:
            json_mod.dump(filas, handle, ensure_ascii=False, indent=2)
    else:
        with export_mod.abrir_0600(destino) as handle:
            writer = csv_mod.DictWriter(handle, fieldnames=campos)
            writer.writeheader()
            for fila in filas:
                writer.writerow(
                    {k: export_mod.neutralizar_celda(v) for k, v in fila.items()}
                )
    destino.chmod(0o600)


def _fmt_by_kind(by_kind: dict[str, int]) -> str:
    return ", ".join(f"{count} {kind}" for kind, count in sorted(by_kind.items()))


def _fmt_age(age: object) -> str:
    seconds = getattr(age, "total_seconds", lambda: 0.0)()
    return f"{int(seconds // 60)} min"


def _cmd_doctor(log_path: Path) -> int:
    import platform

    print(f"tg-sentry {tg_sentry.__version__}")
    print(f"  Python {platform.python_version()} · Telethon {telethon.__version__}")
    print(f"  log activo: {log_path}")

    checks: list[tuple[str, str, bool]] = []

    cfg = paths.config_path()
    cfg_mode = paths.file_mode(cfg)
    checks.append(
        (
            str(cfg),
            f"permisos {oct(cfg_mode)} (esperado 0o600)" if cfg_mode else "ausente",
            cfg_mode == 0o600,
        )
    )

    for label, dir_getter in [
        ("datos ", paths.data_dir),
        ("estado", paths.state_dir),
        ("logs  ", paths.logs_dir),
    ]:
        directory = dir_getter()
        mode = paths.dir_mode(directory)
        checks.append(
            (
                f"{label} {directory}",
                f"permisos {oct(mode)} (esperado 0o700)" if mode is not None else "no creado aún",
                mode is None or mode == 0o700,
            )
        )

    session_file = paths.session_path()
    session_mode = paths.file_mode(session_file)
    checks.append(
        (
            str(session_file),
            f"permisos {oct(session_mode)} (esperado 0o600)"
            if session_mode
            else "sin sesión (login pendiente)",
            session_mode is None or session_mode == 0o600,
        )
    )

    ok = True
    for target, detail, passed in checks:
        mark = "✓" if passed else "⚠"
        ok = ok and passed
        print(f"  {mark} {target} — {detail}")
    return EXIT_OK if ok else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
