from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "analysis/binance-bubblemaps-expanded-universe-2026-08-03/import_pg_transfers.py"
)
SPEC = importlib.util.spec_from_file_location("import_pg_transfers", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_clean_pg_transfer_uses_exact_target_and_timestamp():
    result = MODULE.clean_pg_transfer(
        {
            "event_id": "event-1",
            "tx_hash": "0xABC",
            "from_address": "0xFROM",
            "to_address": "0xTO",
            "amount": Decimal("12.3400"),
            "event_timestamp_ms": None,
            "event_at": datetime(2026, 8, 3, 1, 2, 3, tzinfo=timezone.utc),
        },
        "bsc",
        "0xtoken",
    )

    assert result == {
        "from_address": "0xfrom",
        "to_address": "0xto",
        "rel_type": "TRANSFER",
        "data": {
            "value": "12.34",
            "date": 1785718923000,
            "tx_hash": "0xabc",
            "token_ref": {"chain": "bsc", "address": "0xtoken"},
            "event_id": "event-1",
            "source": "postgresql",
        },
    }


def test_merge_transfers_deduplicates_equivalent_decimal_values():
    existing = {
        "from_address": "0x1",
        "to_address": "0x2",
        "rel_type": "TRANSFER",
        "data": {
            "value": "1.2300",
            "date": 1000,
            "tx_hash": "0xabc",
            "token_ref": {"chain": "bsc", "address": "0xtoken"},
        },
    }
    duplicate = {
        **existing,
        "data": {**existing["data"], "value": "1.23", "source": "postgresql"},
    }
    distinct = {
        **existing,
        "data": {**existing["data"], "value": "2"},
    }

    merged, added = MODULE.merge_transfers([existing], [duplicate, distinct])

    assert added == 1
    assert len(merged) == 2
    assert [Decimal(row["data"]["value"]) for row in merged] == [
        Decimal("1.2300"),
        Decimal("2"),
    ]
