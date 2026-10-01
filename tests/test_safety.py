"""Tests de la capa de seguridad (blindados, whitelist, protecciones)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tg_sentry.models import DialogInfo, PeerKind, PeerRole
from tg_sentry.safety import hard_exclusions, parse_whitelist, soft_warnings


def _dialog(**kwargs) -> DialogInfo:
    base = {"id": 100, "kind": PeerKind.USER, "name": "x"}
    base.update(kwargs)
    return DialogInfo(**base)


def test_parse_whitelist_separa_ids_y_usernames() -> None:
    ids, usernames = parse_whitelist(["123456789", "@Durov", "durov2", "  -77  ", ""])
    # Los ids se aceptan en TODAS sus formas (protección de más, jamás de menos)
    assert 123456789 in ids and -123456789 in ids
    assert -(123456789 + 10**12) in ids  # forma marcada de canal desde id crudo
    assert -77 in ids and 77 in ids
    assert usernames == {"durov", "durov2"}


def test_whitelist_por_forma_marcada_de_canal() -> None:
    """El usuario pega el id crudo (123) o el marcado (-100…123): protege igual."""
    marcado = _dialog(id=-(10**12 + 123), kind=PeerKind.CHANNEL)
    razones = hard_exclusions(
        marcado, me_id=1, whitelist_ids=parse_whitelist(["123"])[0],
        whitelist_usernames=set(),
    )
    assert any("whitelist" in r for r in razones)


def test_blindados_de_fabrica() -> None:
    # 777000 (servicio) y la propia cuenta quedan fuera SIEMPRE
    assert hard_exclusions(
        _dialog(id=777000), me_id=None, whitelist_ids=set(), whitelist_usernames=set()
    )
    assert hard_exclusions(
        _dialog(id=42), me_id=42, whitelist_ids=set(), whitelist_usernames=set()
    )


def test_whitelist_por_id_y_username() -> None:
    por_id = _dialog(id=555, username=None)
    por_username = _dialog(id=556, username="MiCanal")
    assert hard_exclusions(
        por_id, me_id=1, whitelist_ids={555}, whitelist_usernames=set()
    )
    assert hard_exclusions(
        por_username, me_id=1, whitelist_ids=set(), whitelist_usernames={"micanal"}
    )


def test_creador_y_secreto_y_desconocido_excluidos() -> None:
    vacio = {"me_id": 1, "whitelist_ids": set(), "whitelist_usernames": set()}
    assert hard_exclusions(_dialog(role=PeerRole.CREATOR), **vacio)
    assert hard_exclusions(_dialog(kind=PeerKind.SECRET), **vacio)
    assert hard_exclusions(_dialog(kind=PeerKind.UNKNOWN), **vacio)


def test_usuario_normal_sin_razones() -> None:
    razones = hard_exclusions(
        _dialog(id=100), me_id=1, whitelist_ids=set(), whitelist_usernames=set()
    )
    assert razones == []


def test_aviso_admin_pierde_rol() -> None:
    avisos = soft_warnings(_dialog(id=1, role=PeerRole.ADMIN))
    assert any("pierdes el rol" in aviso for aviso in avisos)


def test_me_id_none_bloquea_todo_fail_closed() -> None:
    """Sin identidad propia NADA es operable (auditoría 2026-09, P1)."""
    razones = hard_exclusions(
        _dialog(id=100), me_id=None, whitelist_ids=set(), whitelist_usernames=set()
    )
    assert any("me_id" in r for r in razones)


def test_contacto_exclusion_dura_no_aviso() -> None:
    """Los contactos son EXCLUSIÓN DURA con protect_contacts (config.example
    lo promete; antes era un aviso sin fuente de contactos — placebo)."""
    base = dict(me_id=1, whitelist_ids=set(), whitelist_usernames=set())
    razones = hard_exclusions(
        _dialog(id=88), contact_ids={88}, protect_contacts=True, **base
    )
    assert any("contacto" in r for r in razones)
    # opt-out explícito: sin protección ni aviso
    sin = hard_exclusions(
        _dialog(id=88), contact_ids={88}, protect_contacts=False, **base
    )
    assert sin == []


def test_role_none_avisa_en_grupo_y_canal() -> None:
    """role=None (spike GetParticipant pendiente): no se puede descartar ser
    creador — aviso honesto en grupos/canales, nada en privados/bots."""
    reciente = datetime.now(UTC) - timedelta(days=400)
    grupo = soft_warnings(
        _dialog(id=2, kind=PeerKind.GROUP, role=None, last_message_at=reciente)
    )
    assert any("creador" in a for a in grupo)
    canal = soft_warnings(
        _dialog(id=3, kind=PeerKind.CHANNEL, role=None, last_message_at=reciente)
    )
    assert any("creador" in a for a in canal)
    privado = soft_warnings(
        _dialog(id=4, kind=PeerKind.USER, role=None, last_message_at=reciente)
    )
    assert not any("creador" in a for a in privado)


def test_aviso_actividad_reciente_solo_en_privados() -> None:
    reciente = datetime.now(UTC) - timedelta(days=2)
    viejo = datetime.now(UTC) - timedelta(days=400)
    aviso = soft_warnings(_dialog(id=1, last_message_at=reciente))
    assert any("actividad reciente" in a for a in aviso)
    viejo_aviso = soft_warnings(_dialog(id=1, last_message_at=viejo))
    assert not any("actividad reciente" in a for a in viejo_aviso)
    # un GRUPO con rol verificado (member) y actividad reciente no dispara
    # el aviso de privados (y el de rol ya no aplica)
    assert not soft_warnings(
        _dialog(id=2, kind=PeerKind.GROUP, role=PeerRole.MEMBER, last_message_at=reciente)
    )


def test_aviso_desactivable_por_dias_cero() -> None:
    reciente = datetime.now(UTC) - timedelta(days=2)
    assert not soft_warnings(_dialog(id=1, last_message_at=reciente), recent_private_days=0)


def test_whitelist_id_malformado_se_rechaza_con_mensaje() -> None:
    """P3 3ª auditoría: '--77' pasaba el chequeo isdigit y reventaba con
    ValueError crudo de int() dentro de build_plan; ahora el mensaje dice qué
    entrada es inválida (tratarla como username dejaría de proteger)."""
    import pytest as _pytest

    with _pytest.raises(ValueError, match="--77"):
        parse_whitelist(["--77"])
    # '-77' (un solo signo) sigue siendo válido
    ids, usernames = parse_whitelist(["-77", "durov2"])
    assert -77 in ids and 77 in ids
    assert "durov2" in usernames
