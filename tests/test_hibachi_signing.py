"""Differential tests: our Hibachi payload/signature construction must match
the official hibachi-xyz SDK byte-for-byte.

If the official SDK is importable (see ``_load_official_sdk``), we call its
actual private payload builder and signer and compare. Otherwise we fall back
to fixed vectors generated from SDK v0.3.1.
"""

from __future__ import annotations

import sys
import typing
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import eth_keys.datatypes
import pytest

from deltabot.venues.hibachi.signing import (
    ContractMeta,
    HibachiSigner,
    cancel_payload,
    order_payload,
    price_to_bytes,
    quantity_to_bytes,
)

TEST_KEY = "0x" + "ab" * 32

BTC_CONTRACT = ContractMeta(id=2, underlying_decimals=10, settlement_decimals=6)
ETH_CONTRACT = ContractMeta(id=1, underlying_decimals=9, settlement_decimals=6)


def _load_official_sdk():
    """Import the official SDK (needs a typing.override shim on py<3.12)."""
    sdk_path = Path(__file__).parent / "vendor_sdk"
    if not sdk_path.exists():
        return None
    if not hasattr(typing, "override"):
        typing.override = lambda f: f  # type: ignore[attr-defined]
    sys.path.insert(0, str(sdk_path))
    try:
        import hibachi_xyz.api as sdk_api  # noqa: PLC0415

        return sdk_api
    except Exception:
        return None
    finally:
        sys.path.pop(0)


SDK = _load_official_sdk()


def test_quantity_bytes_scaling():
    # 0.001 BTC with 10 underlying decimals -> 10_000_000
    assert quantity_to_bytes(Decimal("0.001"), BTC_CONTRACT) == (10_000_000).to_bytes(8, "big")


def test_price_bytes_scaling():
    # price * 2^32 * 10^(6-10)
    expected = int(Decimal("65000") * (2**32) * Decimal("10") ** -4)
    assert price_to_bytes(Decimal("65000"), BTC_CONTRACT) == expected.to_bytes(8, "big")


def test_market_order_payload_layout():
    payload = order_payload(
        BTC_CONTRACT,
        nonce=1_700_000_000_000_000,
        quantity=Decimal("0.001"),
        is_buy=True,
        max_fees_percent=Decimal("0.0005"),
        price=None,
    )
    assert len(payload) == 8 + 4 + 8 + 4 + 8  # no price segment for market orders
    assert payload[0:8] == (1_700_000_000_000_000).to_bytes(8, "big")
    assert payload[8:12] == (2).to_bytes(4, "big")
    assert payload[12:20] == (10_000_000).to_bytes(8, "big")
    assert payload[20:24] == (1).to_bytes(4, "big")  # BID
    assert payload[24:32] == int(Decimal("0.0005") * 10**8).to_bytes(8, "big")


def test_limit_order_payload_includes_price():
    payload = order_payload(
        ETH_CONTRACT,
        nonce=42,
        quantity=Decimal("1.5"),
        is_buy=False,
        max_fees_percent=Decimal("0.00045"),
        price=Decimal("3000"),
    )
    assert len(payload) == 8 + 4 + 8 + 4 + 8 + 8
    assert payload[20:24] == (0).to_bytes(4, "big")  # ASK
    expected_price = int(Decimal("3000") * (2**32) * Decimal("10") ** -3)
    assert payload[24:32] == expected_price.to_bytes(8, "big")


def test_cancel_payload():
    assert cancel_payload(order_id=123456) == (123456).to_bytes(8, "big")
    assert cancel_payload(nonce=777) == (777).to_bytes(8, "big")
    with pytest.raises(ValueError):
        cancel_payload()


def test_ecdsa_signature_format_and_verifies():
    signer = HibachiSigner(TEST_KEY)
    payload = b"test-payload"
    sig_hex = signer.sign(payload)
    assert len(sig_hex) == 130  # r(64) + s(64) + v(2) hex chars
    # Recover: signature verifies against the key's public key over sha256(payload)
    sig = eth_keys.datatypes.Signature(
        vrs=(
            int(sig_hex[128:130], 16),
            int(sig_hex[0:64], 16),
            int(sig_hex[64:128], 16),
        )
    )
    pk = eth_keys.datatypes.PrivateKey(bytes.fromhex(TEST_KEY[2:]))
    assert sig.recover_public_key_from_msg_hash(sha256(payload).digest()) == pk.public_key


def test_hmac_fallback_for_non_hex_key():
    signer = HibachiSigner("web-account-secret")  # not valid hex -> HMAC mode
    import hmac as hmac_mod

    expected = hmac_mod.new(b"web-account-secret", b"payload", sha256).hexdigest()
    assert signer.sign(b"payload") == expected


@pytest.mark.skipif(SDK is None, reason="official SDK not vendored")
class TestAgainstOfficialSDK:
    """Byte-for-byte comparison against hibachi-xyz v0.3.1 internals."""

    def _sdk_client(self):
        client = SDK.HibachiApiClient.__new__(SDK.HibachiApiClient)
        client.set_private_key(TEST_KEY)
        return client

    def _sdk_contract(self, meta: ContractMeta):
        from hibachi_xyz.types import FutureContract  # noqa: PLC0415

        return FutureContract(
            displayName="x", id=meta.id, minNotional="10", minOrderSize="0.0001",
            orderbookGranularities=["0.01"], initialMarginRate="0.05",
            maintenanceMarginRate="0.03", settlementDecimals=meta.settlement_decimals,
            settlementSymbol="USDT", status="LIVE", stepSize="0.0001",
            symbol="BTC/USDT-P", tickSize="0.1",
            underlyingDecimals=meta.underlying_decimals, underlyingSymbol="BTC",
        )

    @pytest.mark.parametrize("price", [None, Decimal("65000.5")])
    @pytest.mark.parametrize("is_buy", [True, False])
    def test_order_payload_matches_sdk(self, price, is_buy):
        from hibachi_xyz.types import Side as SdkSide  # noqa: PLC0415

        client = self._sdk_client()
        sdk_payload = client._HibachiApiClient__create_or_update_order_payload(
            self._sdk_contract(BTC_CONTRACT),
            1_700_000_000_000_000,
            Decimal("0.001"),
            SdkSide.BID if is_buy else SdkSide.ASK,
            Decimal("0.0005"),
            price,
        )
        ours = order_payload(
            BTC_CONTRACT, 1_700_000_000_000_000, Decimal("0.001"), is_buy,
            Decimal("0.0005"), price,
        )
        assert ours == sdk_payload

    def test_signature_matches_sdk(self):
        client = self._sdk_client()
        payload = b"identical-payload-bytes"
        sdk_sig = client._HibachiApiClient__sign_payload(payload)
        ours = HibachiSigner(TEST_KEY).sign(payload)
        assert ours == sdk_sig
