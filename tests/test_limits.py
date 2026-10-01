"""Tests de la política de límites (solo a la baja / --ack-over-limit)."""

from __future__ import annotations

import pytest

from tg_sentry.config import LimitsSection
from tg_sentry.limits import (
    DEFAULT_WRITES_PER_MIN,
    MAX_DESTRUCTIVE_PER_DAY,
    MAX_DESTRUCTIVE_PER_HOUR,
    LimitsPolicyError,
    validate_policy,
)


def test_constantes_duras_no_cambian() -> None:
    assert MAX_DESTRUCTIVE_PER_HOUR == 50
    assert MAX_DESTRUCTIVE_PER_DAY == 200
    assert DEFAULT_WRITES_PER_MIN == 4


def test_valores_default_o_inferiores_pasan() -> None:
    assert validate_policy(LimitsSection(), ack_over_limit=False) is None
    assert validate_policy(LimitsSection(writes_per_min=1), ack_over_limit=False) is None
    assert validate_policy(
        LimitsSection(leave_groups_per_hour=10, block_per_hour=5), ack_over_limit=False
    ) is None


def test_subir_cualquier_cap_rechazado_sin_ack() -> None:
    with pytest.raises(LimitsPolicyError, match="ack-over-limit"):
        validate_policy(LimitsSection(writes_per_min=10), ack_over_limit=False)
    with pytest.raises(LimitsPolicyError, match="ack-over-limit"):
        validate_policy(LimitsSection(block_per_hour=80), ack_over_limit=False)
    with pytest.raises(LimitsPolicyError, match="ack-over-limit"):
        validate_policy(
            LimitsSection(delete_messages_per_hour=150), ack_over_limit=False
        )


def test_subir_con_ack_pasa() -> None:
    validate_policy(
        LimitsSection(writes_per_min=12, leave_groups_per_hour=40),
        ack_over_limit=True,
    )


def test_el_mensaje_nombra_el_parametro_infractor() -> None:
    with pytest.raises(LimitsPolicyError, match="writes_per_min=10"):
        validate_policy(LimitsSection(writes_per_min=10), ack_over_limit=False)
