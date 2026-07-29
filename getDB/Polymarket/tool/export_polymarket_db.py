"""Export Polymarket selection rows from PostgreSQL on POSIX systems."""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
from zoneinfo import ZoneInfo


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if __package__ in (None, ""):
    sys.path.insert(0, str(_PROJECT_ROOT))

from common.artifacts import write_json_atomic
from common.time_window import china_day_bounds
from getDB.Polymarket.tool.contract import validate_serialized_record


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
_MANIFEST_FIELDS = {
    "source", "dataset", "generation_id", "status", "business_date",
    "timezone", "utc_lower_bound", "utc_upper_bound", "captured_at",
    "source_row_count", "record_count", "error_count", "category_counts",
    "artifacts",
}
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


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


@contextmanager
def _generation_lock(directory: Path, operation: int):
    descriptor = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            directory / _LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600
        )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, operation)
    except BaseException as error:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except Exception:
                pass
        if isinstance(error, Exception):
            raise GenerationLockError("Unable to acquire generation lock") from None
        raise
    body_error = None
    try:
        yield
    except BaseException as error:
        body_error = error
        raise
    finally:
        release_error = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except BaseException as error:
            release_error = error
        try:
            os.close(descriptor)
        except BaseException as error:
            release_error = release_error or error
        if release_error is not None and (
            body_error is None or not isinstance(release_error, Exception)
        ):
            if isinstance(release_error, Exception):
                raise GenerationLockError("Unable to release generation lock") from None
            raise release_error


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_manifest(directory: Path) -> dict:
    return {
        filename: {"sha256": _sha256_file(directory / filename)}
        for filename in _HASHED_ARTIFACT_FILENAMES
    }


def _artifact_error(error: Exception) -> dict:
    return {
        "stage": "artifact",
        "type": type(error).__name__,
        "message": "Unable to write an export artifact",
    }


def _write_artifacts(directory: Path, primary: dict, manifest: dict, errors: list) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with _generation_lock(directory, fcntl.LOCK_EX):
            return _write_artifacts_unlocked(directory, primary, manifest, errors)
    except GenerationLockError:
        return False


def _write_artifacts_unlocked(
    directory: Path, primary: dict, manifest: dict, errors: list
) -> bool:
    primary_path = directory / _PRIMARY_FILENAME
    errors_path = directory / _ERRORS_FILENAME
    manifest_path = directory / _MANIFEST_FILENAME
    in_progress = {**manifest, "status": "in_progress", "artifacts": {}}
    try:
        write_json_atomic(manifest_path, in_progress)
        write_json_atomic(primary_path, primary)
        write_json_atomic(errors_path, errors)
        committed = {**manifest, "artifacts": _artifact_manifest(directory)}
        write_json_atomic(manifest_path, committed)
        return True
    except Exception as error:
        try:
            write_json_atomic(manifest_path, in_progress)
        except Exception:
            try:
                manifest_path.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            write_json_atomic(errors_path, [*errors, _artifact_error(error)])
        except Exception:
            pass
        return False


def _validation_failure() -> None:
    raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE)


def _strict_integer(value: object, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if type(value) is not int or value < 0:
        _validation_failure()
    return value


def _aware_iso(value: object) -> datetime:
    if not isinstance(value, str):
        _validation_failure()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _validation_failure()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _validation_failure()
    return parsed


def _validate_error(error: object) -> None:
    if type(error) is not dict:
        _validation_failure()
    for field in ("stage", "type", "message"):
        if not isinstance(error.get(field), str) or not error[field]:
            _validation_failure()
    if error["stage"] == "row_validation":
        if set(error) != {"id", "content_hash", "stage", "type", "message"}:
            _validation_failure()
        if error["id"] is not None and type(error["id"]) is not int:
            _validation_failure()
        if error["content_hash"] is not None and not isinstance(
            error["content_hash"], str
        ):
            _validation_failure()


def _validate_generation(
    directory: Path, primary: object, errors: object, manifest: object
) -> None:
    if type(primary) is not dict or set(primary) != {"records"}:
        _validation_failure()
    if type(primary["records"]) is not list or type(errors) is not list:
        _validation_failure()
    if type(manifest) is not dict or set(manifest) != _MANIFEST_FIELDS:
        _validation_failure()
    if manifest["source"] != "postgresql" or manifest["dataset"] != "polymarket":
        _validation_failure()
    if manifest["timezone"] != "Asia/Shanghai" or manifest["status"] not in _FINAL_STATUSES:
        _validation_failure()
    try:
        uuid.UUID(manifest["generation_id"])
        business_date = date.fromisoformat(manifest["business_date"])
    except (AttributeError, TypeError, ValueError):
        _validation_failure()
    if directory.name != business_date.isoformat():
        _validation_failure()
    lower, upper = china_day_bounds(business_date)
    if manifest["utc_lower_bound"] != _utc_iso(lower):
        _validation_failure()
    if manifest["utc_upper_bound"] != _utc_iso(upper):
        _validation_failure()
    if _aware_iso(manifest["captured_at"]).utcoffset() != timezone.utc.utcoffset(None):
        _validation_failure()

    categories = Counter()
    for record in primary["records"]:
        try:
            categories[validate_serialized_record(record)] += 1
        except (TypeError, ValueError):
            _validation_failure()
    for error in errors:
        _validate_error(error)

    record_count = _strict_integer(manifest["record_count"])
    error_count = _strict_integer(manifest["error_count"])
    source_count = _strict_integer(manifest["source_row_count"], nullable=True)
    if record_count != len(primary["records"]) or error_count != len(errors):
        _validation_failure()
    category_counts = manifest["category_counts"]
    if type(category_counts) is not dict or category_counts != dict(categories):
        _validation_failure()
    if any(
        not isinstance(key, str) or type(value) is not int or value <= 0
        for key, value in category_counts.items()
    ):
        _validation_failure()

    row_error_count = sum(error["stage"] == "row_validation" for error in errors)
    status = manifest["status"]
    success_status = (
        status == "success" and record_count > 0 and error_count == 0
        and source_count == record_count
    )
    partial_status = (
        status == "partial" and record_count > 0 and error_count > 0
        and row_error_count == error_count
        and source_count == record_count + row_error_count
    )
    failed_source = (
        status == "failed" and record_count == 0 and error_count > 0
        and source_count is None and error_count == 1
        and errors[0]["stage"] == "source"
    )
    failed_empty = (
        status == "failed" and record_count == 0 and error_count == 1
        and source_count == 0 and errors[0]["stage"] == "source_selection"
    )
    failed_rows = (
        status == "failed" and record_count == 0 and row_error_count > 0
        and row_error_count == error_count and source_count == row_error_count
    )
    if not any((success_status, partial_status, failed_source, failed_empty, failed_rows)):
        _validation_failure()

    artifacts = manifest["artifacts"]
    if type(artifacts) is not dict or set(artifacts) != set(_HASHED_ARTIFACT_FILENAMES):
        _validation_failure()
    for metadata in artifacts.values():
        if type(metadata) is not dict or set(metadata) != {"sha256"}:
            _validation_failure()
        if not isinstance(metadata["sha256"], str) or not _SHA256_PATTERN.fullmatch(
            metadata["sha256"]
        ):
            _validation_failure()


def _read_validated_generation_unlocked(directory: Path) -> tuple[dict, list, dict]:
    try:
        payloads = {
            filename: (directory / filename).read_bytes()
            for filename in (*_HASHED_ARTIFACT_FILENAMES, _MANIFEST_FILENAME)
        }
        manifest = json.loads(payloads[_MANIFEST_FILENAME])
        artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
        if not isinstance(artifacts, dict):
            _validation_failure()
        for filename in _HASHED_ARTIFACT_FILENAMES:
            metadata = artifacts.get(filename)
            if not isinstance(metadata, dict):
                _validation_failure()
            if hashlib.sha256(payloads[filename]).hexdigest() != metadata.get("sha256"):
                _validation_failure()
        primary = json.loads(payloads[_PRIMARY_FILENAME])
        errors = json.loads(payloads[_ERRORS_FILENAME])
        _validate_generation(directory, primary, errors, manifest)
        return primary, errors, manifest
    except GenerationValidationError:
        raise
    except (
        AttributeError, json.JSONDecodeError, KeyError, OSError, TypeError,
        UnicodeError, ValueError,
    ):
        raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE) from None


def read_validated_generation(directory: Path) -> tuple[dict, list, dict]:
    with _generation_lock(directory, fcntl.LOCK_SH):
        return _read_validated_generation_unlocked(directory)


def validate_manifest_artifacts(directory: Path) -> bool:
    try:
        read_validated_generation(directory)
    except (GenerationLockError, GenerationValidationError):
        return False
    return True
