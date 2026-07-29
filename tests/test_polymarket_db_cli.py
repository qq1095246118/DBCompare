from datetime import date, datetime, timezone

import pytest

from getDB.Polymarket.tool import export_polymarket_db as cli


DAY = date(2026, 7, 29)
LOWER = datetime(2026, 7, 28, 16, tzinfo=timezone.utc)
UPPER = datetime(2026, 7, 29, 16, tzinfo=timezone.utc)
CAPTURED = datetime(2026, 7, 29, 8, 30, tzinfo=timezone.utc)
GENERATION_ID = "00000000-0000-4000-8000-000000000001"


def test_parse_args_uses_shanghai_today_and_default_output(monkeypatch):
    monkeypatch.setattr(cli, "_china_today", lambda: DAY)
    arguments = cli.parse_args([])
    assert arguments.date == DAY
    assert arguments.output_root == cli._PROJECT_ROOT / "getDB" / "Polymarket" / "db"


@pytest.mark.parametrize("value", ["2026/07/29", "2026-7-29", "2026-02-30"])
def test_parse_args_rejects_non_strict_or_invalid_dates(value):
    with pytest.raises(SystemExit) as raised:
        cli.parse_args(["--date", value])
    assert raised.value.code == 2


def test_manifest_has_the_exact_generation_contract():
    result = cli._manifest(business_date=DAY, lower=LOWER, upper=UPPER,
        generation_id=GENERATION_ID, captured_at=CAPTURED, status="partial",
        source_row_count=3, record_count=2, error_count=1,
        category_counts={"politics": 2})
    assert result == {"source": "postgresql", "dataset": "polymarket",
        "generation_id": GENERATION_ID, "status": "partial",
        "business_date": "2026-07-29", "timezone": "Asia/Shanghai",
        "utc_lower_bound": "2026-07-28T16:00:00+00:00",
        "utc_upper_bound": "2026-07-29T16:00:00+00:00",
        "captured_at": "2026-07-29T08:30:00+00:00",
        "source_row_count": 3, "record_count": 2, "error_count": 1,
        "category_counts": {"politics": 2}, "artifacts": {}}


@pytest.mark.parametrize(("records", "errors", "expected"),
                         [(2, 0, "success"), (2, 1, "partial"), (0, 1, "failed")])
def test_generation_status_depends_on_usable_records_and_errors(records, errors, expected):
    assert cli._generation_status(records, errors) == expected


def test_source_errors_are_generic_and_allowlist_the_type():
    result = cli._source_error(RuntimeError("host=db-secret password=password-secret"))
    assert result == {"stage": "source", "type": "RuntimeError",
                      "message": "Database source operation failed"}
    assert "secret" not in repr(result)


def test_source_errors_collapse_unrecognized_exception_types():
    class InternalConnectorFailure(Exception): pass
    assert cli._source_error(InternalConnectorFailure("private"))["type"] == "Exception"


def test_no_records_error_has_a_stable_non_secret_shape():
    assert cli._no_records_error() == {"stage": "source_selection",
        "type": "NoRecordsError",
        "message": "No Polymarket rows found for requested date"}
