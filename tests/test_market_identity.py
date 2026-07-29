import base64
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from hashlib import sha256

import pytest

from getMarket.bubblemaps.tool.market_identity import (
    TargetToken,
    canonicalize_address,
    make_target,
    token_ref_matches,
)


_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_EVM_SOURCE = "0xC61F0667076521761FB365F52644572E92FD0C94"
_EVM_CANONICAL = "0xc61f0667076521761fb365f52644572e92fd0c94"
_SOLANA_SOURCE = "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv"


def _crc16_xmodem(data):
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _ton_friendly_address(tag=0x11, workchain=0):
    payload = bytes((tag, workchain & 0xFF)) + bytes(range(32))
    checksum = _crc16_xmodem(payload).to_bytes(2, "big")
    return base64.urlsafe_b64encode(payload + checksum).decode().rstrip("=")


_TON_FRIENDLY_ADDRESS = _ton_friendly_address()
_TON_RAW_ADDRESS = "0:" + bytes(range(32)).hex()


class _MembershipErrorMapping(Mapping):
    def __getitem__(self, key):
        return {
            "chain": "bsc",
            "address": "0x1111111111111111111111111111111111111111",
        }[key]

    def __iter__(self):
        return iter(("chain", "address"))

    def __len__(self):
        return 2

    def __contains__(self, key):
        raise RuntimeError("membership failed")


class _FieldAccessErrorMapping(Mapping):
    def __getitem__(self, key):
        raise RuntimeError("field access failed")

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __contains__(self, key):
        return False


class _InterruptingMapping(Mapping):
    def __getitem__(self, key):
        raise AssertionError("field access must not be reached")

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __contains__(self, key):
        raise KeyboardInterrupt


class _IterationObservingString(str):
    def __new__(cls, value):
        instance = super().__new__(cls, value)
        instance.iterated = False
        return instance

    def __iter__(self):
        self.iterated = True
        raise AssertionError("oversized Base58 value must not be iterated")


def _base58check_encode(payload):
    checksum = sha256(sha256(payload).digest()).digest()[:4]
    raw = payload + checksum
    value = int.from_bytes(raw, "big")
    encoded = ""
    while value:
        value, remainder = divmod(value, 58)
        encoded = _BASE58_ALPHABET[remainder] + encoded
    leading_zeroes = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * leading_zeroes + (encoded or "1")


@pytest.mark.parametrize(
    ("chain", "source", "expected"),
    [
        (
            "bsc",
            "0xC61F0667076521761FB365F52644572E92FD0C94",
            "0xc61f0667076521761fb365f52644572e92fd0c94",
        ),
        (
            "solana",
            "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
            "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
        ),
        (
            "tron",
            "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
            "0x0000000000000000000000000000000000000000",
        ),
        (
            "tron",
            "0x3487B63D30B5B2C87FB7FFA8BCFADE38EAAC1ABE",
            "0x3487b63d30b5b2c87fb7ffa8bcfade38eaac1abe",
        ),
    ],
)
def test_canonicalize_address_accepts_supported_forms(chain, source, expected):
    assert canonicalize_address(chain, source) == expected


@pytest.mark.parametrize(
    ("requested_chain", "source", "canonical_chain", "canonical_address"),
    [
        ("ethereum", _EVM_SOURCE, "eth", _EVM_CANONICAL),
        ("eth", _EVM_SOURCE, "eth", _EVM_CANONICAL),
        ("bsc", _EVM_SOURCE, "bsc", _EVM_CANONICAL),
        ("base", _EVM_SOURCE, "base", _EVM_CANONICAL),
        ("arbitrum", _EVM_SOURCE, "arbitrum", _EVM_CANONICAL),
        ("polygon", _EVM_SOURCE, "polygon", _EVM_CANONICAL),
        ("robinhood", _EVM_SOURCE, "robinhood", _EVM_CANONICAL),
        ("solana", _SOLANA_SOURCE, "solana", _SOLANA_SOURCE),
        (
            "tron",
            "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
            "tron",
            "0x0000000000000000000000000000000000000000",
        ),
    ],
)
def test_make_target_accepts_every_supported_chain_alias(
    requested_chain, source, canonical_chain, canonical_address
):
    target = make_target(requested_chain, source)

    assert target.chain == canonical_chain
    assert target.token_address == canonical_address


@pytest.mark.parametrize("chain", ["sonic", "avalanche", "monad", "hyperevm"])
def test_make_target_accepts_new_evm_chains(chain):
    target = make_target(chain, _EVM_SOURCE)

    assert target.chain == chain
    assert target.token_address == _EVM_CANONICAL


@pytest.mark.parametrize("tag", [0x11, 0x51, 0x91, 0xD1])
def test_canonicalize_address_normalizes_ton_friendly_address_tags(tag):
    assert canonicalize_address("ton", _ton_friendly_address(tag)) == _TON_RAW_ADDRESS


def test_make_target_normalizes_ton_friendly_address_but_preserves_request():
    target = make_target("ton", _TON_FRIENDLY_ADDRESS)

    assert target.chain == "ton"
    assert target.requested_token_address == _TON_FRIENDLY_ADDRESS
    assert target.token_address == _TON_RAW_ADDRESS


def test_token_ref_matches_ton_friendly_address_tags_canonically():
    target = make_target("ton", _ton_friendly_address(0x11))

    assert token_ref_matches(
        {"chain": "ton", "address": _ton_friendly_address(0x51)}, target
    )


def test_make_target_rejects_unsupported_ton_workchain():
    with pytest.raises(ValueError, match="workchain"):
        make_target("ton", _ton_friendly_address(workchain=1))


@pytest.mark.parametrize(
    "address",
    [
        _TON_FRIENDLY_ADDRESS[:-1] + "A",
        _ton_friendly_address(0),
        _TON_FRIENDLY_ADDRESS[:-1],
        _TON_FRIENDLY_ADDRESS[:-1] + "/",
    ],
)
def test_make_target_rejects_malformed_ton_friendly_address(address):
    with pytest.raises(ValueError, match="TON"):
        make_target("ton", address)


@pytest.mark.parametrize(
    ("chain", "source"),
    [
        ("eth", "0x1234"),
        ("solana", "So1111111111111111111111111111111111111111O"),
        ("tron", "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwc"),
        ("unknown", "0xc61f0667076521761fb365f52644572e92fd0c94"),
    ],
)
def test_canonicalize_address_rejects_invalid_or_unknown_forms(chain, source):
    with pytest.raises(ValueError):
        canonicalize_address(chain, source)


@pytest.mark.parametrize(
    ("chain", "source"),
    [
        (None, "0xc61f0667076521761fb365f52644572e92fd0c94"),
        (1, "0xc61f0667076521761fb365f52644572e92fd0c94"),
        (" eth", "0xc61f0667076521761fb365f52644572e92fd0c94"),
        ("eth ", "0xc61f0667076521761fb365f52644572e92fd0c94"),
        ("ETH", "0xc61f0667076521761fb365f52644572e92fd0c94"),
        ("", "0xc61f0667076521761fb365f52644572e92fd0c94"),
        ("eth", None),
        ("eth", 1),
        ("eth", " 0xc61f0667076521761fb365f52644572e92fd0c94"),
        ("eth", "0xc61f0667076521761fb365f52644572e92fd0c94 "),
        ("eth", ""),
        ("eth", "0xc61f0667076521761fb365f52644572e92fd0c9g"),
    ],
)
def test_canonicalize_address_rejects_non_strings_whitespace_and_bad_hex(
    chain, source
):
    with pytest.raises(ValueError):
        canonicalize_address(chain, source)


@pytest.mark.parametrize("source", ["1" * 31, "1" * 33])
def test_canonicalize_address_rejects_solana_values_not_decoding_to_32_bytes(
    source,
):
    with pytest.raises(ValueError):
        canonicalize_address("solana", source)


def test_canonicalize_address_accepts_solana_leading_zero_bytes():
    source = "1" * 32

    assert canonicalize_address("solana", source) == source


def test_canonicalize_address_accepts_44_character_solana_boundary():
    assert len(_SOLANA_SOURCE) == 44
    assert canonicalize_address("solana", _SOLANA_SOURCE) == _SOLANA_SOURCE


def test_canonicalize_address_rejects_oversized_base58_before_iteration():
    source = _IterationObservingString("z" * 45)

    with pytest.raises(ValueError, match="44"):
        canonicalize_address("solana", source)
    assert source.iterated is False


@pytest.mark.parametrize(
    "payload",
    [
        b"\x42" + b"\x00" * 20,
        b"\x41" + b"\x00" * 19,
        b"\x41" + b"\x00" * 21,
    ],
)
def test_canonicalize_address_rejects_tron_base58check_payload_shape(payload):
    with pytest.raises(ValueError):
        canonicalize_address("tron", _base58check_encode(payload))


def test_make_target_normalizes_chain_and_contract_address():
    assert make_target(
        "ethereum",
        "0xC61F0667076521761FB365F52644572E92FD0C94",
    ) == TargetToken(
        requested_chain="ethereum",
        requested_token_address="0xC61F0667076521761FB365F52644572E92FD0C94",
        chain="eth",
        token_address="0xc61f0667076521761fb365f52644572e92fd0c94",
    )


def test_make_target_preserves_tron_base58_for_page_navigation():
    target = make_target("tron", "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8")
    assert target.requested_chain == "tron"
    assert target.requested_token_address == "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8"
    assert target.chain == "tron"
    assert target.token_address == "0x3487b63d30b5b2c87fb7ffa8bcfade38eaac1abe"


def test_target_token_is_frozen():
    target = make_target(
        "bsc", "0x1111111111111111111111111111111111111111"
    )
    with pytest.raises(FrozenInstanceError):
        target.chain = "eth"


@pytest.mark.parametrize(
    "token_ref",
    [
        {"chain": "eth", "address": "0x1111111111111111111111111111111111111111"},
        {"chain": "bsc", "address": "0x2222222222222222222222222222222222222222"},
        {"id": "BNB"},
        {"chain": "bsc", "id": "BNB"},
        None,
        {},
    ],
)
def test_token_ref_matches_rejects_foreign_native_and_malformed_refs(token_ref):
    target = make_target(
        "bsc", "0x1111111111111111111111111111111111111111"
    )
    assert token_ref_matches(token_ref, target) is False


def test_token_ref_matches_accepts_case_variant_current_evm_contract():
    target = make_target(
        "bsc", "0x1111111111111111111111111111111111111111"
    )
    assert token_ref_matches(
        {
            "chain": "bsc",
            "address": "0x1111111111111111111111111111111111111111",
        },
        target,
    )


def test_token_ref_matches_normalizes_response_alias_and_address_case():
    target = make_target(
        "eth", "0xc61f0667076521761fb365f52644572e92fd0c94"
    )
    assert token_ref_matches(
        {
            "chain": "ethereum",
            "address": "0xC61F0667076521761FB365F52644572E92FD0C94",
        },
        target,
    )


def test_token_ref_matches_rejects_id_even_when_address_is_present():
    target = make_target(
        "bsc", "0x1111111111111111111111111111111111111111"
    )
    assert not token_ref_matches(
        {
            "chain": "bsc",
            "address": "0x1111111111111111111111111111111111111111",
            "id": "BNB",
        },
        target,
    )


@pytest.mark.parametrize(
    "token_ref",
    [
        [],
        "bsc:0x1111111111111111111111111111111111111111",
        {"chain": "unknown", "address": "0x1111111111111111111111111111111111111111"},
        {"chain": " bsc", "address": "0x1111111111111111111111111111111111111111"},
        {"chain": "bsc", "address": " 0x1111111111111111111111111111111111111111"},
        {"chain": "bsc", "address": "0x1234"},
        {"chain": "bsc", "address": None},
        {"chain": None, "address": "0x1111111111111111111111111111111111111111"},
    ],
)
def test_token_ref_matches_returns_false_for_malformed_response_data(token_ref):
    target = make_target(
        "bsc", "0x1111111111111111111111111111111111111111"
    )
    assert token_ref_matches(token_ref, target) is False


@pytest.mark.parametrize(
    "token_ref",
    [_MembershipErrorMapping(), _FieldAccessErrorMapping()],
)
def test_token_ref_matches_returns_false_when_mapping_access_raises(token_ref):
    target = make_target(
        "bsc", "0x1111111111111111111111111111111111111111"
    )

    assert token_ref_matches(token_ref, target) is False


def test_token_ref_matches_does_not_swallow_base_exceptions():
    target = make_target(
        "bsc", "0x1111111111111111111111111111111111111111"
    )

    with pytest.raises(KeyboardInterrupt):
        token_ref_matches(_InterruptingMapping(), target)


def test_token_ref_matches_rejects_oversized_base58_without_iteration():
    source = _IterationObservingString("z" * 45)
    target = make_target("solana", _SOLANA_SOURCE)

    assert token_ref_matches(
        {"chain": "solana", "address": source}, target
    ) is False
    assert source.iterated is False


def test_token_ref_matches_compares_tron_base58_and_hex_canonically():
    target = make_target("tron", "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8")
    assert token_ref_matches(
        {
            "chain": "tron",
            "address": "0x3487B63D30B5B2C87FB7FFA8BCFADE38EAAC1ABE",
        },
        target,
    )


def test_token_ref_matches_preserves_solana_case_sensitive_identity():
    source = "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv"
    target = make_target("solana", source)
    assert token_ref_matches({"chain": "solana", "address": source}, target)
    assert not token_ref_matches(
        {
            "chain": "solana",
            "address": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauu",
        },
        target,
    )
