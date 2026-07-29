import base64

import pytest

from getDB.bubblemaps.tool.db_source import PgSettings


TOKEN_A = "0xC61F0667076521761FB365F52644572E92FD0C94"
TOKEN_A_CANONICAL = TOKEN_A.lower()
TOKEN_B = "0x1111111111111111111111111111111111111111"
SETTINGS = PgSettings(
    host="db.example.invalid",
    port=15432,
    dbname="analytics",
    user="readonly",
    password="test-only-password",
)
FIXED_OUTPUT_CHAINS = (
    "eth",
    "base",
    "solana",
    "tron",
    "bsc",
    "sonic",
    "ton",
    "avalanche",
    "polygon",
    "monad",
    "hyperevm",
    "arbitrum",
    "robinhood",
)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, parameters=None):
        self.executions.append((statement, parameters))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_value = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def cursor(self):
        return self.cursor_value


def install_fake_connection(monkeypatch, cursor):
    from getMarket.bubblemaps.tool import market_targets

    connect_arguments = []

    def fake_connect(**kwargs):
        connect_arguments.append(kwargs)
        return FakeConnection(cursor)

    monkeypatch.setattr(market_targets.psycopg, "connect", fake_connect)
    return connect_arguments


def _crc16_xmodem(data):
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _ton_friendly_address(tag=0x11):
    payload = bytes((tag, 0)) + bytes(range(32))
    checksum = _crc16_xmodem(payload).to_bytes(2, "big")
    return base64.urlsafe_b64encode(payload + checksum).decode().rstrip("=")


TON_FRIENDLY = _ton_friendly_address()
TON_FRIENDLY_BOUNCEABLE = _ton_friendly_address(0x51)
TON_RAW = "0:" + bytes(range(32)).hex()
TRON_BASE58 = "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8"
TRON_CANONICAL = "0x3487b63d30b5b2c87fb7ffa8bcfade38eaac1abe"


def test_load_targets_filters_empty_values_maps_alias_and_deduplicates(monkeypatch):
    from getMarket.bubblemaps.tool import market_targets

    cursor = FakeCursor(
        [
            {"chain": "arb", "token_address": TOKEN_A},
            {"chain": "arbitrum", "token_address": "0x" + TOKEN_A[2:].upper()},
            {"chain": "monad", "token_address": "   "},
        ]
    )
    connect_arguments = install_fake_connection(monkeypatch, cursor)

    assert market_targets.load_targets(SETTINGS) == {
        "arbitrum": [TOKEN_A]
    }

    query, query_parameters = cursor.executions[1]
    assert "binance_address_metadata" in query
    assert "token_address IS NOT NULL" in query
    assert "btrim(token_address) <> ''" in query
    assert "AND is_active = 1" in query
    assert query_parameters == (list(FIXED_OUTPUT_CHAINS + ("arb",)),)
    assert cursor.executions[0] == (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;",
        None,
    )
    assert cursor.executions[-1] == ("COMMIT;", None)
    assert connect_arguments == [
        {
            "host": SETTINGS.host,
            "port": SETTINGS.port,
            "dbname": SETTINGS.dbname,
            "user": SETTINGS.user,
            "password": SETTINGS.password,
            "row_factory": market_targets.dict_row,
            "autocommit": True,
        }
    ]


def test_load_targets_filters_requested_symbols_in_database(monkeypatch):
    from getMarket.bubblemaps.tool import market_targets

    cursor = FakeCursor([{"chain": "bsc", "token_address": TOKEN_B}])
    install_fake_connection(monkeypatch, cursor)

    assert market_targets.load_targets(
        SETTINGS,
        symbols=("BEAT", "M"),
    ) == {"bsc": [TOKEN_B]}

    query, query_parameters = cursor.executions[1]
    assert "upper(btrim(token_symbol)) = ANY(%s)" in query
    assert query_parameters == (
        list(FIXED_OUTPUT_CHAINS + ("arb",)),
        ["BEAT", "M"],
    )


def test_load_targets_rejects_invalid_non_empty_addresses(monkeypatch):
    from getMarket.bubblemaps.tool import market_targets

    cursor = FakeCursor([{"chain": "eth", "token_address": "not-an-address"}])
    install_fake_connection(monkeypatch, cursor)

    with pytest.raises(ValueError, match="EVM address"):
        market_targets.load_targets(SETTINGS)


def test_load_targets_preserves_deterministic_ton_requested_representation(monkeypatch):
    from getMarket.bubblemaps.tool import market_targets

    cursor = FakeCursor(
        [
            {"chain": "ton", "token_address": TON_FRIENDLY_BOUNCEABLE},
            {"chain": "ton", "token_address": TON_FRIENDLY},
            {"chain": "ton", "token_address": TON_RAW},
        ]
    )
    install_fake_connection(monkeypatch, cursor)

    assert market_targets.load_targets(SETTINGS) == {"ton": [TON_FRIENDLY]}


def test_select_targets_sorts_pairs_before_limit_and_selects_one_target():
    from getMarket.bubblemaps.tool import market_targets

    targets = {
        "eth": [TOKEN_B, TOKEN_A_CANONICAL],
        "solana": ["2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv"],
    }

    selected = market_targets.select_targets(targets, limit=2, chain=None, token_address=None)

    assert [(target.chain, target.token_address) for target in selected] == [
        ("eth", TOKEN_B),
        ("eth", TOKEN_A_CANONICAL),
    ]
    assert market_targets.select_targets(
        targets, limit=None, chain="eth", token_address=TOKEN_A
    ) == [market_targets.make_target("eth", TOKEN_A)]


def test_select_targets_deduplicates_aliases_and_address_case_by_canonical_pair():
    from getMarket.bubblemaps.tool import market_targets

    targets = {
        "ethereum": [TOKEN_A],
        "eth": [TOKEN_A_CANONICAL],
    }

    selected = market_targets.select_targets(
        targets, limit=None, chain=None, token_address=None
    )

    assert [(target.chain, target.token_address) for target in selected] == [
        ("eth", TOKEN_A_CANONICAL)
    ]


def test_targets_to_dict_preserves_requested_address_representation():
    from getMarket.bubblemaps.tool import market_targets

    selected = [
        market_targets.make_target("ton", TON_FRIENDLY),
        market_targets.make_target("eth", TOKEN_A),
        market_targets.make_target("tron", TRON_BASE58),
    ]

    assert market_targets.targets_to_dict(selected) == {
        "eth": [TOKEN_A],
        "ton": [TON_FRIENDLY],
        "tron": [TRON_BASE58],
    }


def test_tron_base58_survives_generation_target_roundtrip(tmp_path):
    from getMarket.bubblemaps.tool import market_artifacts, market_targets

    targets = market_targets.targets_to_dict(
        [market_targets.make_target("tron", TRON_BASE58)]
    )

    market_artifacts.write_targets(tmp_path, targets)
    reloaded = market_artifacts.read_targets(tmp_path)
    selected = market_targets.select_targets(
        reloaded,
        limit=None,
        chain="tron",
        token_address=TRON_BASE58,
    )

    assert reloaded == {"tron": [TRON_BASE58]}
    assert selected == [market_targets.make_target("tron", TRON_BASE58)]
    assert selected[0].requested_token_address == TRON_BASE58
    assert selected[0].token_address == TRON_CANONICAL


def test_select_targets_rejects_incomplete_or_missing_single_target():
    from getMarket.bubblemaps.tool import market_targets

    targets = {"eth": [TOKEN_A_CANONICAL]}

    with pytest.raises(ValueError, match="together"):
        market_targets.select_targets(targets, limit=None, chain="eth", token_address=None)
    with pytest.raises(ValueError, match="not present"):
        market_targets.select_targets(
            targets, limit=None, chain="base", token_address=TOKEN_A
        )
