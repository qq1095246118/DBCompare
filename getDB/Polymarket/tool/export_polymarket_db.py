"""Export Polymarket selection rows from PostgreSQL on POSIX systems."""

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import re
import sys
import uuid
from zoneinfo import ZoneInfo


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if __package__ in (None, ""):
    sys.path.insert(0, str(_PROJECT_ROOT))

from common.time_window import china_day_bounds


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_OUTPUT_ROOT = _PROJECT_ROOT / "getDB" / "Polymarket" / "db"
_STRICT_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_PRIMARY_FILENAME = "polymarket_db.json"
_ERRORS_FILENAME = "errors.json"
_MANIFEST_FILENAME = "manifest.json"
_LOCK_FILENAME = ".generation.lock"
_HASHED_ARTIFACT_FILENAMES = (_PRIMARY_FILENAME, _ERRORS_FILENAME)
_FINAL_STATUSES = frozenset({"success", "partial", "failed"})
_SOURCE_ERROR_MESSAGE = "Database source operation failed"
_VALIDATION_ERROR_MESSAGE = "Unable to validate Polymarket database export"
_SAFE_SOURCE_ERROR_TYPES = frozenset({"AttributeError", "ConnectionError", "DataError",
    "DatabaseError", "Exception", "IndexError", "IntegrityError", "InterfaceError",
    "InternalError", "KeyError", "LookupError", "NotSupportedError", "OperationalError",
    "OSError", "PermissionError", "ProgrammingError", "RuntimeError", "TimeoutError",
    "TypeError", "ValueError"})


class GenerationLockError(RuntimeError): pass
class GenerationValidationError(RuntimeError): pass


def _parse_date(value: str) -> date:
    if _STRICT_DATE_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
        china_day_bounds(parsed)
    except (OverflowError, ValueError):
        raise argparse.ArgumentTypeError(
            "date must be a valid supported Asia/Shanghai calendar date") from None
    return parsed


def _china_today() -> date: return datetime.now(_SHANGHAI).date()
def _utc_now() -> datetime: return datetime.now(timezone.utc)
def _new_generation_id() -> str: return str(uuid.uuid4())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=_parse_date, default=_china_today())
    parser.add_argument("--output-root", type=Path, default=_DEFAULT_OUTPUT_ROOT)
    return parser.parse_args(argv)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("manifest timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _generation_status(record_count: int, error_count: int) -> str:
    return ("partial" if error_count else "success") if record_count > 0 else "failed"


def _manifest(*, business_date: date, lower: datetime, upper: datetime,
              generation_id: str, captured_at: datetime, status: str,
              source_row_count: int | None, record_count: int, error_count: int,
              category_counts: dict[str, int]) -> dict:
    return {"source": "postgresql", "dataset": "polymarket",
        "generation_id": generation_id, "status": status,
        "business_date": business_date.isoformat(), "timezone": "Asia/Shanghai",
        "utc_lower_bound": _utc_iso(lower), "utc_upper_bound": _utc_iso(upper),
        "captured_at": _utc_iso(captured_at), "source_row_count": source_row_count,
        "record_count": record_count, "error_count": error_count,
        "category_counts": category_counts, "artifacts": {}}


def _safe_source_error_type(error: Exception) -> str:
    name = type(error).__name__
    return name if name in _SAFE_SOURCE_ERROR_TYPES else "Exception"


def _source_error(error: Exception) -> dict:
    return {"stage": "source", "type": _safe_source_error_type(error),
            "message": _SOURCE_ERROR_MESSAGE}


def _no_records_error() -> dict:
    return {"stage": "source_selection", "type": "NoRecordsError",
            "message": "No Polymarket rows found for requested date"}
