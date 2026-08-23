"""Hibachi request signing.

Byte layouts and algorithms mirror the official ``hibachi-xyz`` Python SDK
(v0.3.1) exactly, so signatures verify against the production matching engine:

Order create/update payload (big-endian):
    nonce            8 bytes   epoch microseconds
    contract_id      4 bytes
    quantity         8 bytes   int(quantity * 10^underlyingDecimals)
    side             4 bytes   0 = ASK (sell), 1 = BID (buy)
    price            8 bytes   int(price * 2^32 * 10^(settlementDecimals - underlyingDecimals))
                               -- omitted entirely for market orders
    max_fees_percent 8 bytes   int(max_fees_percent * 10^8)

Cancel payload: orderId as 8 bytes BE (or the original order nonce, 8 bytes BE).

Signature: ECDSA secp256k1 over sha256(payload); hex(r ‖ s ‖ v) where r and s
are 32 bytes and v is the 1-byte recovery id — for wallet-backed API keys.
Web-account API keys instead use HMAC-SHA256(secret, payload) hex.
"""

from __future__ import annotations

import hmac as hmac_mod
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256

import eth_keys.datatypes


@dataclass(frozen=True)
class ContractMeta:
    """The subset of Hibachi contract metadata signing depends on."""

    id: int
    underlying_decimals: int
    settlement_decimals: int


def quantity_to_bytes(quantity: Decimal, contract: ContractMeta) -> bytes:
    return int(quantity * Decimal(10) ** contract.underlying_decimals).to_bytes(8, "big")


def price_to_bytes(price: Decimal, contract: ContractMeta) -> bytes:
    scaled = int(
        price
        * Decimal(2) ** 32
        * Decimal(10) ** (contract.settlement_decimals - contract.underlying_decimals)
    )
    return scaled.to_bytes(8, "big")


def order_payload(
    contract: ContractMeta,
    nonce: int,
    quantity: Decimal,
    is_buy: bool,
    max_fees_percent: Decimal,
    price: Decimal | None,
) -> bytes:
    side_word = 1 if is_buy else 0
    return (
        nonce.to_bytes(8, "big")
        + contract.id.to_bytes(4, "big")
        + quantity_to_bytes(quantity, contract)
        + side_word.to_bytes(4, "big")
        + (b"" if price is None else price_to_bytes(price, contract))
        + int(max_fees_percent * Decimal(10) ** 8).to_bytes(8, "big")
    )


def cancel_payload(order_id: int | None = None, nonce: int | None = None) -> bytes:
    if order_id is not None:
        return order_id.to_bytes(8, "big")
    if nonce is None:
        raise ValueError("either order_id or nonce is required to cancel")
    return nonce.to_bytes(8, "big")


class HibachiSigner:
    """Signs binary payloads with either an ECDSA wallet key or an HMAC secret.

    ``private_key``: hex string (with or without 0x). If it parses as 32 bytes
    it is treated as a secp256k1 private key (wallet-created API accounts);
    otherwise it is used verbatim as an HMAC-SHA256 secret (web accounts) —
    the same fallback the official SDK applies.
    """

    def __init__(self, private_key: str):
        key = private_key[2:] if private_key.startswith("0x") else private_key
        self._ecdsa: eth_keys.datatypes.PrivateKey | None = None
        self._hmac_secret: str | None = None
        try:
            key_bytes = bytes.fromhex(key)
            self._ecdsa = eth_keys.datatypes.PrivateKey(key_bytes)
        except Exception:
            # Not a valid 32-byte secp256k1 key (bytes.fromhex raises
            # ValueError; eth_keys raises its own ValidationError on hex of
            # the wrong length) -> treat as a web-account HMAC secret.
            self._hmac_secret = key

    def sign(self, payload: bytes) -> str:
        if self._ecdsa is not None:
            digest = sha256(payload).digest()
            sig = self._ecdsa.sign_msg_hash(digest)
            return (
                sig.r.to_bytes(32, "big").hex()
                + sig.s.to_bytes(32, "big").hex()
                + sig.v.to_bytes(1, "big").hex()
            )
        assert self._hmac_secret is not None
        return hmac_mod.new(self._hmac_secret.encode(), payload, sha256).hexdigest()
