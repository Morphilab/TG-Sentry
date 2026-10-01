# tg-sentry

**English** | [Español](README.es.md)

Monitor and cleaner for your own Telegram account — **safe by design**.

> ## ⚠️ WARNING — read this before using the tool
>
> **This tool can get your Telegram account suspended (banned).** Telegram
> penalizes automated mass actions (leaving groups, blocking users, deleting
> histories) even when FloodWaits are respected. tg-sentry applies conservative
> limits and automatic pauses, but **no software can eliminate that risk**. Use
> it at your own risk, on your own account, at a prudent pace.
>
> Also, deletions on Telegram are **irreversible**: everything goes through
> dry-run → plan → explicit confirmation → audit, but whatever is executed
> cannot be undone.

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

## What it does

- **Inventory** of all your dialogs (groups, channels, bots, private chats)
  with combinable filters (type, name, regex, inactivity, archived…).
- **Cleanup with the right steps per type**:
  - Bots: delete chat + block.
  - Groups/channels: leave.
  - Private chats: delete ± revoke (≤48h also removes them from the other
    side) ± block.
  - **Your own messages** in any chat: two modes — for-you-only (no age
    limit; in private chats, bots and basic groups) or for-everyone (≤48h;
    beyond 48h the server degrades it to for-you-only). In channels and
    supergroups the API ALWAYS deletes for everyone: the for-you-only mode is
    rejected there — a deletion is never executed with the promise reversed.
    With the option to NOT leave the chat (clean and keep it).
- **Safe by design**: dry-run by default, protected peers (your own account,
  Saved Messages, 777000, whitelist, secret chats; the "where you are the
  creator" protection is pending per-chat role verification and currently
  warns "role unverified"), plan re-validation (5 min TTL), JSONL audit log
  with rotation and export.
- **Structural anti-ban**: token bucket (~4 ops/min, lower-bound only),
  per-type caps (30 leaves/h, 50 blocks/h), hard session caps (50 destructive
  ops/h, 200/day — code constants), FloodWait respected with absolute
  priority (2× grace after every wait), indefinite pause on a ban pattern
  (2nd FloodWait <5 min), extra cooldown between mass message-deletion steps.
- **Full TUI** (Textual): filterable dashboard, bulk selection, dry-run plan,
  typed confirmation, execution with progress bar/ETA, grouped results,
  reviewable and exportable audit, settings.
- **Headless CLI** to automate the same engine.

## Status

| Milestone | Scope | Status |
|---|---|---|
| M0 | Scaffolding, config, safe session, 2FA login | ✅ |
| M1 | Inventory + API semantics pilot | ✅ |
| M2 | Planner + safety + audit | ✅ |
| M3 | Executor + limits + headless CLI | ✅ |
| M4 | TUI base | ✅ |
| M5 | Execution TUI + audit | ✅ |
| M6 | Own messages + export + final docs | ✅ |

## Installation

Requires Python ≥ 3.11.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
pip install -e .[dev]   # development
```

## Usage

### TUI (recommended)

```bash
tg-sentry tui
```

1. Login (if there is no session): phone → code → 2FA.
2. Dashboard: filter (types, name, regex, inactivity, archived, unread).
3. Select: `space` toggles, `a` selects everything filtered, `i` inverts,
   `n` clears. Protected peers show 🔒 and can never be selected.
4. `p` → dry-run plan (steps per peer, options, warnings, excluded).
5. `c` → confirmation: destructive-op counters, excluded protected peers,
   suspension warning; type the literal token `CONFIRMAR` + Enter.
6. Execution with progress bar/ETA → Results. `v` audit, `o` settings.

### Headless CLI

```bash
tg-sentry login
tg-sentry dialogs --refresh              # inventory (read-only)
tg-sentry plan --filter "kinds=bot,inactive=365" --output plan.json
tg-sentry apply --plan plan.json --confirm CONFIRMAR [--limit-peers N]
tg-sentry audit [--since 2025-01-01] [--export csv]
tg-sentry export --format json|csv [--output path]
tg-sentry doctor                          # path and permission diagnostics
```

Filters: `kinds=bot+channel+group+user`, `name=…`, `regex=…`, `inactive=Nd`,
`archived=true|false|any`, `unread_min=N`.
Plan options: `--delete-own me|all|off` (own messages),
`--no-leave` (clean without leaving).

Examples:

```bash
# 10 old bots as a pilot (~20 ops, ~5 min)
tg-sentry plan --filter "kinds=bot,inactive=365" --output plan.json
tg-sentry apply --plan plan.json --confirm CONFIRMAR --limit-peers 10

# Clean YOUR messages from a group you keep (without leaving)
tg-sentry plan --filter "name=example" --delete-own me --no-leave --output plan.json

# Channels inactive >6 months (unsubscribe)
tg-sentry plan --filter "kinds=channel,inactive=180" --output plan.json
```

Configuration lives in `~/.config/tg-sentry/config.toml` (see
`config.example.toml`), the session in `~/.local/share/tg-sentry/`
(verified `0600` permissions), logs and audit in `~/.local/state/tg-sentry/`.

## Documentation

- `docs/security.md` — threat model and warnings
- `docs/telegram-limits.md` — known (empirical) limits and semantics

Design documentation (technical decisions, spike findings, the pilot
protocol) is project-internal and not published.

## License

MIT.
