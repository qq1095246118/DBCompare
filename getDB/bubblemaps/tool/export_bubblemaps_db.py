"""PostgreSQL export CLI for POSIX systems with ``fcntl.flock`` support."""

import argparse
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

from common.artifacts import output_directory, write_json_atomic
from common.time_window import china_day_bounds
from getDB.bubblemaps.tool.db_source import (
    PgSettings,
    build_db_output,
    fetch_day_rows,
    load_pg_settings,
)


_CHINA_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_DEFAULT_OUTPUT_ROOT = _PROJECT_ROOT / "getDB" / "bubblemaps" / "db"
_STRICT_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_CREDENTIAL_BOUNDARY_CHARS = r"A-Za-z0-9_.-"
_REDACTED = "[REDACTED]"
_SOURCE_ERROR_MESSAGE = "Database source operation failed"
_VALIDATION_ERROR_MESSAGE = "Unable to validate database export generation"
_PRIMARY_FILENAME = "bubblemaps_db.json"
_ERRORS_FILENAME = "errors.json"
_MANIFEST_FILENAME = "manifest.json"
_LOCK_FILENAME = ".generation.lock"
_HASHED_ARTIFACT_FILENAMES = (_PRIMARY_FILENAME, _ERRORS_FILENAME)
_MANIFEST_STATUSES = frozenset({"success", "partial", "failed"})
_SAFE_SOURCE_ERROR_TYPES = frozenset(
    {
        "AttributeError",
        "ConnectionError",
        "DataError",
        "DatabaseError",
        "Exception",
        "IndexError",
        "IntegrityError",
        "InterfaceError",
        "InternalError",
        "KeyError",
        "LookupError",
        "NotSupportedError",
        "OperationalError",
        "OSError",
        "PermissionError",
        "ProgrammingError",
        "RuntimeError",
        "TimeoutError",
        "TypeError",
        "ValueError",
    }
)


class GenerationLockError(RuntimeError):
    pass


class GenerationValidationError(RuntimeError):
    pass


def _parse_date(value: str) -> date:
    if _STRICT_DATE_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD")
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "date must be a valid calendar date in YYYY-MM-DD format"
        ) from None
    try:
        china_day_bounds(parsed_date)
    except (OverflowError, ValueError):
        raise argparse.ArgumentTypeError(
            "date is outside the supported Asia/Shanghai range"
        ) from None
    return parsed_date


def _china_today() -> date:
    return datetime.now(_CHINA_TIME_ZONE).date()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_generation_id() -> str:
    return str(uuid.uuid4())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=_parse_date, default=_china_today())
    parser.add_argument(
        "--output-root",
        type=Path,
        default=_DEFAULT_OUTPUT_ROOT,
    )
    return parser.parse_args(argv)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _manifest(
    business_date: date,
    lower: datetime,
    upper: datetime,
    tokens: list[dict],
    generation_id: str,
    status: str,
) -> dict:
    return {
        "source": "postgresql",
        "generation_id": generation_id,
        "status": status,
        "business_date": business_date.isoformat(),
        "timezone": "Asia/Shanghai",
        "utc_lower_bound": _utc_iso(lower),
        "utc_upper_bound": _utc_iso(upper),
        "captured_at": _utc_iso(_utc_now()),
        "tokens": tokens,
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_manifest(directory: Path) -> dict:
    return {
        filename: {"sha256": _sha256_file(directory / filename)}
        for filename in _HASHED_ARTIFACT_FILENAMES
    }


def read_validated_generation(directory: Path) -> tuple[dict, list, dict]:
    """Read one committed generation while holding the shared lock."""
    with _generation_lock(directory, fcntl.LOCK_SH):
        return _read_validated_generation_unlocked(directory)


def _read_validated_generation_unlocked(
    directory: Path,
) -> tuple[dict, list, dict]:
    try:
        manifest_bytes = (directory / _MANIFEST_FILENAME).read_bytes()
        primary_bytes = (directory / _PRIMARY_FILENAME).read_bytes()
        errors_bytes = (directory / _ERRORS_FILENAME).read_bytes()
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict):
            raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE)
        generation_id = manifest.get("generation_id")
        if not isinstance(generation_id, str) or not generation_id:
            raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE)
        if manifest.get("status") not in _MANIFEST_STATUSES:
            raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE)
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != set(
            _HASHED_ARTIFACT_FILENAMES
        ):
            raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE)
        artifact_bytes = {
            _PRIMARY_FILENAME: primary_bytes,
            _ERRORS_FILENAME: errors_bytes,
        }
        for filename, payload in artifact_bytes.items():
            metadata = artifacts[filename]
            if not isinstance(metadata, dict) or set(metadata) != {"sha256"}:
                raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE)
            expected_hash = metadata["sha256"]
            if not isinstance(expected_hash, str):
                raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE)
            if hashlib.sha256(payload).hexdigest() != expected_hash:
                raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE)
        primary = json.loads(primary_bytes)
        errors = json.loads(errors_bytes)
        if not isinstance(primary, dict) or not isinstance(errors, list):
            raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE)
    except GenerationValidationError:
        raise
    except (json.JSONDecodeError, KeyError, OSError, TypeError, UnicodeError):
        raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE) from None
    return primary, errors, manifest


def validate_manifest_artifacts(directory: Path) -> bool:
    """Return whether a committed generation can be read safely.

    This is diagnostic only. Consumers that need the payloads must use
    ``read_validated_generation`` so validation and parsing share one lock.
    """
    try:
        read_validated_generation(directory)
    except (GenerationLockError, GenerationValidationError):
        return False
    return True


def _credential_values(settings: PgSettings) -> list[str]:
    values = [
        settings.host,
        settings.port,
        settings.dbname,
        settings.user,
        settings.password,
    ]
    return sorted(
        {str(value) for value in values if value is not None and str(value)},
        key=len,
        reverse=True,
    )


def _redact_token_message(message: str, credentials: list[str]) -> str:
    if message in credentials:
        return _REDACTED
    for credential in credentials:
        quoted_values = {
            repr(credential),
            json.dumps(credential, ensure_ascii=False),
        }
        for quoted_value in quoted_values:
            quote = quoted_value[0]
            message = message.replace(
                quoted_value,
                f"{quote}{_REDACTED}{quote}",
            )
        if len(credential) >= 4:
            message = re.sub(
                rf"(?<![{_CREDENTIAL_BOUNDARY_CHARS}])"
                rf"{re.escape(credential)}"
                rf"(?![{_CREDENTIAL_BOUNDARY_CHARS}])",
                _REDACTED,
                message,
            )
    return message


def _redact_token_errors(
    errors: list[dict],
    settings: PgSettings,
) -> list[dict]:
    credentials = _credential_values(settings)
    result = []
    for error in errors:
        redacted = dict(error)
        for field_name in ("chain", "token_address"):
            value = redacted.get(field_name)
            if isinstance(value, str) and value in credentials:
                redacted[field_name] = _REDACTED
        message = redacted.get("message")
        if isinstance(message, str):
            redacted["message"] = _redact_token_message(
                message,
                credentials,
            )
        result.append(redacted)
    return result


def _safe_source_error_type(error: Exception) -> str:
    error_type = type(error).__name__
    if error_type in _SAFE_SOURCE_ERROR_TYPES:
        return error_type
    return "Exception"


def _source_error(error: Exception, settings: PgSettings | None) -> dict:
    return {
        "stage": "source",
        "type": _safe_source_error_type(error),
        "message": _SOURCE_ERROR_MESSAGE,
    }


@contextmanager
def _generation_lock(directory: Path, operation: int):
    """Hold a cooperative generation lock; requires POSIX ``fcntl.flock``."""
    file_descriptor: int | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        file_descriptor = os.open(
            directory / _LOCK_FILENAME,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        os.fchmod(file_descriptor, 0o600)
        fcntl.flock(file_descriptor, operation)
    except BaseException as error:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except Exception:
                pass
        if isinstance(error, Exception):
            raise GenerationLockError(
                "Unable to acquire generation lock"
            ) from None
        raise

    body_error: BaseException | None = None
    try:
        yield
    except BaseException as error:
        body_error = error
        raise
    finally:
        release_error: BaseException | None = None
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_UN)
        except BaseException as error:
            release_error = error
        try:
            os.close(file_descriptor)
        except BaseException as error:
            if release_error is None:
                release_error = error

        if release_error is not None and (
            body_error is None or not isinstance(release_error, Exception)
        ):
            if isinstance(release_error, Exception):
                raise GenerationLockError(
                    "Unable to release generation lock"
                ) from None
            raise release_error


def _write_artifacts(
    directory: Path,
    primary: dict,
    manifest: dict,
    errors: list[dict],
) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except Exception as error:
        return _write_artifact_failure(
            directory / _ERRORS_FILENAME,
            errors,
            error,
        )

    with _generation_lock(directory, fcntl.LOCK_EX):
        return _write_artifacts_unlocked(directory, primary, manifest, errors)


def _write_artifacts_unlocked(
    directory: Path,
    primary: dict,
    manifest: dict,
    errors: list[dict],
) -> bool:
    primary_path = directory / _PRIMARY_FILENAME
    errors_path = directory / _ERRORS_FILENAME
    manifest_path = directory / _MANIFEST_FILENAME
    in_progress_manifest = {
        **manifest,
        "status": "in_progress",
        "tokens": [],
        "artifacts": {},
    }

    try:
        write_json_atomic(manifest_path, in_progress_manifest)
    except Exception as error:
        try:
            manifest_path.unlink(missing_ok=True)
        except Exception:
            pass
        return _write_artifact_failure(errors_path, errors, error)

    try:
        write_json_atomic(primary_path, primary)
    except Exception as error:
        return _write_artifact_failure(errors_path, errors, error)

    try:
        write_json_atomic(errors_path, errors)
    except Exception:
        raise RuntimeError(
            "Unable to write database export artifacts"
        ) from None

    try:
        committed_manifest = {
            **manifest,
            "artifacts": _artifact_manifest(directory),
        }
        # This is the commit record and must remain the final normal write.
        write_json_atomic(manifest_path, committed_manifest)
    except Exception as error:
        try:
            write_json_atomic(manifest_path, in_progress_manifest)
        except Exception:
            try:
                manifest_path.unlink(missing_ok=True)
            except Exception:
                pass
        return _write_artifact_failure(errors_path, errors, error)
    return True


def _write_artifact_failure(
    errors_path: Path,
    errors: list[dict],
    error: Exception,
) -> bool:
    artifact_errors = [
        *errors,
        {
            "stage": "artifact",
            "type": type(error).__name__,
            "message": "Unable to write an export artifact",
        },
    ]
    try:
        write_json_atomic(errors_path, artifact_errors)
    except Exception:
        raise RuntimeError(
            "Unable to write database export artifacts"
        ) from None
    return False


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    lower, upper = china_day_bounds(arguments.date)
    settings: PgSettings | None = None

    try:
        settings = load_pg_settings()
        holder_rows, cluster_rows = fetch_day_rows(settings, lower, upper)
        primary, errors, tokens = build_db_output(holder_rows, cluster_rows)
        errors = _redact_token_errors(errors, settings)
        status = "partial" if errors else "success"
    except Exception as error:
        primary = {}
        errors = [_source_error(error, settings)]
        tokens = []
        status = "failed"

    manifest = _manifest(
        arguments.date,
        lower,
        upper,
        tokens,
        _new_generation_id(),
        status,
    )
    directory = output_directory(arguments.output_root, arguments.date)
    artifacts_written = _write_artifacts(directory, primary, manifest, errors)
    return 0 if artifacts_written and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
