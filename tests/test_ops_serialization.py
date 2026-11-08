"""Serialización REAL de requests de Telethon (sin fakes).

Lección de robustez: el fake de inventory ignora la request y devuelve un
resultado plano — la serialización TL jamás corría en tests y
`GetContactsRequest(hash="")` fallaba en producción con struct.error ANTES
de salir a red (el TL exige int64), dejando el refresh inutilizable y la
protección de contactos sin efecto real. Estos tests ejecutan la
serialización REAL de la librería instalada: son la única red contra esa
clase de bug.
"""

from __future__ import annotations

import struct

import pytest
from telethon.tl.functions.contacts import BlockRequest, GetContactsRequest
from telethon.tl.types import InputPeerUser


def test_getcontacts_hash_int_serializa() -> None:
    """hash=0 (int64 vacío = lista completa) debe serializar sin error."""
    GetContactsRequest(hash=0)._bytes()  # type: ignore[attr-defined]


def test_getcontacts_hash_str_es_struct_error() -> None:
    """Documentación ejecutable del hash="" jamás llega a la red.

    Si este test falla porque ya NO lanza, la librería cambió de contrato:
    revisar la llamada en tg/ops.py contact_user_ids.
    """
    with pytest.raises(struct.error):
        GetContactsRequest(hash="")._bytes()  # type: ignore[attr-defined]


def test_blockrequest_serializa_con_input_peer() -> None:
    """BLOCK usa la API cruda (client.block() no existe en Telethon): el
    request serializa con el InputPeer* al que el cliente resuelve el id
    en producción (BlockRequest.resolve → get_input_entity)."""
    BlockRequest(id=InputPeerUser(user_id=123456789, access_hash=0))._bytes()  # type: ignore[attr-defined]
