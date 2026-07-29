import base64
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass


_CHAIN_ALIASES = {
    "ethereum": "eth",
    "eth": "eth",
    "bsc": "bsc",
    "base": "base",
    "arbitrum": "arbitrum",
    "polygon": "polygon",
    "robinhood": "robinhood",
    "sonic": "sonic",
    "ton": "ton",
    "avalanche": "avalanche",
    "monad": "monad",
    "hyperevm": "hyperevm",
    "solana": "solana",
    "tron": "tron",
}

_EVM_CHAINS = frozenset(
    {
        "eth",
        "bsc",
        "base",
        "arbitrum",
        "polygon",
        "robinhood",
        "sonic",
        "avalanche",
        "monad",
        "hyperevm",
    }
)
_EVM_ADDRESS_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}", re.ASCII)
_TON_FRIENDLY_ADDRESS_PATTERN = re.compile(r"[A-Za-z0-9_-]{48}", re.ASCII)
_TON_RAW_ADDRESS_PATTERN = re.compile(r"(-1|0):([0-9a-fA-F]{64})", re.ASCII)
_TON_FRIENDLY_ADDRESS_TAGS = frozenset({0x11, 0x51, 0x91, 0xD1})
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_DIGITS = {character: index for index, character in enumerate(_BASE58_ALPHABET)}
_MAX_BASE58_ADDRESS_LENGTH = 44


@dataclass(frozen=True)
class TargetToken:
    requested_chain: str
    requested_token_address: str
    chain: str
    token_address: str


def _validated_text(value: object, description: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(
            f"{description} must be a non-empty string without surrounding whitespace"
        )
    return value


def _canonicalize_chain(chain: object) -> str:
    requested_chain = _validated_text(chain, "chain")
    try:
        return _CHAIN_ALIASES[requested_chain]
    except KeyError:
        raise ValueError(f"unsupported chain: {requested_chain!r}") from None


def _decode_base58(value: str) -> bytes:
    if len(value) > _MAX_BASE58_ADDRESS_LENGTH:
        raise ValueError(
            "Base58 address must not exceed "
            f"{_MAX_BASE58_ADDRESS_LENGTH} characters"
        )

    number = 0
    for character in value:
        try:
            digit = _BASE58_DIGITS[character]
        except KeyError:
            raise ValueError("address contains a non-Base58 character") from None
        number = number * 58 + digit

    byte_length = (number.bit_length() + 7) // 8
    decoded = number.to_bytes(byte_length, "big") if byte_length else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + decoded


def _canonicalize_evm_address(address: str) -> str:
    if _EVM_ADDRESS_PATTERN.fullmatch(address) is None:
        raise ValueError("EVM address must be 0x followed by 40 hexadecimal digits")
    return address.lower()


def _canonicalize_solana_address(address: str) -> str:
    if len(_decode_base58(address)) != 32:
        raise ValueError("Solana address must decode to exactly 32 bytes")
    return address


def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _canonicalize_ton_address(address: str) -> str:
    raw_match = _TON_RAW_ADDRESS_PATTERN.fullmatch(address)
    if raw_match is not None:
        return f"{raw_match.group(1)}:{raw_match.group(2).lower()}"

    if _TON_FRIENDLY_ADDRESS_PATTERN.fullmatch(address) is None:
        raise ValueError(
            "TON address must be raw workchain:64hex or contain 48 Base64url characters"
        )

    decoded = base64.urlsafe_b64decode(address + "=" * (-len(address) % 4))
    if len(decoded) != 36:
        raise ValueError("TON friendly address must decode to exactly 36 bytes")
    if decoded[0] not in _TON_FRIENDLY_ADDRESS_TAGS:
        raise ValueError("TON friendly address has an unsupported tag")
    if _crc16_xmodem(decoded[:-2]) != int.from_bytes(decoded[-2:], "big"):
        raise ValueError("TON friendly address has an invalid CRC16-XMODEM checksum")
    workchain = int.from_bytes(decoded[1:2], "big", signed=True)
    if workchain not in (0, -1):
        raise ValueError("TON friendly address has an unsupported workchain")
    return f"{workchain}:{decoded[2:34].hex()}"


def _canonicalize_tron_address(address: str) -> str:
    if address.startswith("0x"):
        return _canonicalize_evm_address(address)

    decoded = _decode_base58(address)
    if len(decoded) < 4:
        raise ValueError("TRON address is too short for Base58Check")

    payload, checksum = decoded[:-4], decoded[-4:]
    expected_checksum = hashlib.sha256(
        hashlib.sha256(payload).digest()
    ).digest()[:4]
    if checksum != expected_checksum:
        raise ValueError("TRON address has an invalid Base58Check checksum")
    if len(payload) != 21 or payload[0] != 0x41:
        raise ValueError(
            "TRON address payload must contain prefix 0x41 and 20 address bytes"
        )
    return "0x" + payload[1:].hex()


def _canonicalize_for_chain(chain: str, address: str) -> str:
    if chain in _EVM_CHAINS:
        return _canonicalize_evm_address(address)
    if chain == "solana":
        return _canonicalize_solana_address(address)
    if chain == "ton":
        return _canonicalize_ton_address(address)
    if chain == "tron":
        return _canonicalize_tron_address(address)
    raise ValueError(f"unsupported chain: {chain!r}")


def make_target(chain: object, token_address: object) -> TargetToken:
    requested_chain = _validated_text(chain, "chain")
    requested_token_address = _validated_text(token_address, "token address")
    canonical_chain = _canonicalize_chain(requested_chain)
    canonical_address = _canonicalize_for_chain(
        canonical_chain, requested_token_address
    )
    return TargetToken(
        requested_chain=requested_chain,
        requested_token_address=requested_token_address,
        chain=canonical_chain,
        token_address=canonical_address,
    )


def canonicalize_address(chain: str, address: object) -> str:
    canonical_chain = _canonicalize_chain(chain)
    requested_address = _validated_text(address, "address")
    return _canonicalize_for_chain(canonical_chain, requested_address)


def token_ref_matches(token_ref: object, target: TargetToken) -> bool:
    if not isinstance(token_ref, Mapping):
        return False

    try:
        if "id" in token_ref:
            return False
        response_chain = _canonicalize_chain(token_ref.get("chain"))
        response_address = _validated_text(token_ref.get("address"), "address")
        canonical_address = _canonicalize_for_chain(
            response_chain, response_address
        )
    except Exception:
        return False

    return (
        response_chain == target.chain
        and canonical_address == target.token_address
    )
