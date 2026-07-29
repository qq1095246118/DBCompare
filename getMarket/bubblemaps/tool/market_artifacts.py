"""Sharded official-market artifacts and transactional publication helpers."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
from copy import deepcopy
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path
from urllib.parse import parse_qsl, unquote_to_bytes, urlsplit

from common.artifacts import safe_path_component
from getMarket.bubblemaps.tool.market_identity import (
    TargetToken,
    canonicalize_address,
    make_target,
    token_ref_matches,
)
from getMarket.bubblemaps.tool.market_transform import (
    filter_subgraph_edges,
    parse_ranked_holders,
)


HASH_CHUNK_SIZE = 1_048_576
_MAX_JSON_NESTING_DEPTH = 128
_API_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "generation_id",
        "status",
        "business_date",
        "timezone",
        "captured_at",
        "targets_file",
        "targets",
        "tokens",
        "skipped_tokens",
        "artifacts",
    }
)
_API_ENTRY_FIELDS = frozenset(
    {
        "requested_chain",
        "requested_token_address",
        "canonical_chain",
        "canonical_token_address",
        "captured_at",
        "token_file",
        "member_files",
        "raw_files",
        "clean_files",
        "cluster_count",
        "ranked_holder_count",
        "clustered_member_count",
        "ordinary_member_count",
        "supernode_count",
        "unique_transfer_count",
        "transfer_view_count",
        "status",
    }
)
_API_SKIPPED_FIELDS = frozenset(
    {
        "requested_chain",
        "requested_token_address",
        "canonical_chain",
        "canonical_token_address",
        "stage",
        "http_status",
        "attempt_count",
        "reason",
        "captured_at",
        "status",
    }
)
_API_ERROR_REPORT_FIELDS = frozenset({"error_count", "errors"})
_API_ERROR_REQUIRED_FIELDS = frozenset(
    {
        "chain",
        "token_address",
        "stage",
        "type",
        "message",
        "attempt_count",
        "captured_at",
    }
)
_API_ERROR_OPTIONAL_FIELDS = frozenset(
    {
        "member_address",
        "http_status",
        "from_address",
        "to_address",
        "expected_count",
        "captured_count",
        "edge_last_date",
    }
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(?:headers?|cookies?|authorization|api[-_ ]?key|"
    r"x[-_ ]?validation|validation|password|pgpassword|database[-_ ]?url|"
    r"(?:access[-_ ]?|refresh[-_ ]?)?token|secret|credentials?)\b"
    r"\s*[:=]\s*[^\s<>]+"
)
_BM_API_VALUE = re.compile(r"bmAPI-[A-Za-z0-9_-]+")
_DATABASE_USERINFO = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]*)?@"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[^\s<>]+")
_SENSITIVE_RETRY_KEY = re.compile(
    r"(?i)(?:headers?|cookies?|authorization|auth|api[-_]?key|"
    r"secret|credentials?|password|validation)"
)
_SENSITIVE_CREDENTIAL_TERM = re.compile(
    r"(?i)\b(?:credentials?|secret|password|api[-_ ]?key|validation|"
    r"(?:access|refresh|id)[-_ ]?token)\b"
)
_SENSITIVE_PUBLIC_ARTIFACT_KEY = re.compile(
    r"(?:^|_)(?:headers?|cookies?|authorization|auth|api_?key|"
    r"x_?validation|validation|password|pgpassword|database_?url|"
    r"credentials?|secret|(?:access|refresh|id)_?token)(?:$|_)",
    re.IGNORECASE,
)
_PUBLIC_ARTIFACT_IDENTITY_KEYS = frozenset(
    {
        "chain",
        "token_address",
        "canonical_chain",
        "canonical_token_address",
        "token_ref",
        "request_chain",
        "request_token_address",
        "whitelist_token_address",
        "whitelist_token_chain",
        "member_address",
        "from_address",
        "to_address",
    }
)
_PUBLIC_RETRY_TOKEN_KEYS = frozenset(
    {
        "token_ref",
        "token_address",
        "request_token_address",
        "whitelist_token_address",
        "token_chain",
        "whitelist_token_chain",
    }
)
MEMBER_SUBSTAGES = frozenset({"prepare", "open", "trigger", "response", "close"})
_SAFE_FAILURE_MESSAGES = frozenset({"capture failed"})
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_OFFICIAL_API_HOSTS = frozenset(
    {"api.bubblemaps.io", "api-legacy.bubblemaps.io"}
)
_API_REQUEST_CONTRACTS = {
    "holders": ("POST", "/addresses/token-top-holders"),
    "subgraph": ("POST", "/relationships/subgraph"),
    "transfers": ("GET", "/relationships/transfers"),
}
_MAX_API_URL_DECODE_ROUNDS = 8
_FORMAL_TOKEN_FIELDS = frozenset(
    {
        "schema_version",
        "chain",
        "token_address",
        "canonical_chain",
        "canonical_token_address",
        "captured_at",
        "clusters",
    }
)
_FORMAL_CLUSTER_FIELDS = frozenset(
    {
        "cluster_rank",
        "amount",
        "share",
        "share_percent",
        "member_count",
        "members",
    }
)
_FORMAL_MEMBER_BASE_FIELDS = frozenset(
    {
        "member_rank",
        "source_rank",
        "address",
        "amount",
        "share",
        "share_percent",
        "is_supernode",
        "metadata",
    }
)
_FORMAL_MEMBER_COMMON_TRANSFER_FIELDS = frozenset(
    {
        "transfer_details_available",
        "transfer_count",
        "transfer_file",
    }
)
_FORMAL_MEMBER_DOCUMENT_FIELDS = frozenset(
    {
        "schema_version",
        "chain",
        "token_address",
        "canonical_chain",
        "canonical_token_address",
        "cluster_rank",
        "member_address",
        "transfer_count",
        "transfers",
    }
)
_HOLDING_DECIMAL_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z",
    re.ASCII,
)
_MAX_HOLDING_DECIMAL_TEXT_LENGTH = 30_000
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z",
    re.ASCII,
)
_DECIMAL_TEXT_PATTERN = re.compile(
    r"(?P<sign>[+-]?)"
    r"(?:(?P<integer>[0-9]+)(?:\.(?P<fraction>[0-9]*))?"
    r"|\.(?P<fraction_only>[0-9]+))"
    r"(?:[eE](?P<exponent_sign>[+-]?)(?P<exponent>[0-9]+))?\Z",
    re.ASCII,
)
_SAFE_FAILURE_ERROR_FIELDS = frozenset(
    {
        "generation_id",
        "chain",
        "token_address",
        "member_address",
        "member_substage",
        "stage",
        "attempt",
        "attempt_count",
        "attempts",
        "page_attempts",
        "member_attempts",
        "retryable",
        "type",
        "message",
        "captured_at",
    }
)


class MarketGenerationLockError(RuntimeError):
    """The cooperative date-generation lock could not be acquired or released."""


class MarketGenerationValidationError(RuntimeError):
    """A market generation is not a complete, committed success tree."""


class PublicationRecoveryError(RuntimeError):
    """A publication crash state is ambiguous or cannot be recovered safely."""


@dataclass(frozen=True)
class RunPaths:
    output_root: Path
    business_date: date
    generation_id: str
    live: Path
    staging: Path
    failed: Path
    backup: Path
    lock_file: Path

    @classmethod
    def create(
        cls,
        output_root: Path,
        business_date: date,
        generation_id: str,
    ) -> "RunPaths":
        if type(business_date) is not date:
            raise ValueError("business_date must be a native date")
        if (
            not isinstance(generation_id, str)
            or not generation_id
            or generation_id in {".", ".."}
            or Path(generation_id).name != generation_id
            or Path(generation_id).is_absolute()
        ):
            raise ValueError("generation_id must be one safe path component")
        supplied_root = Path(output_root)
        if supplied_root.is_symlink():
            raise ValueError("output_root must not be a symlink")
        root = Path(os.path.realpath(supplied_root))
        day = business_date.isoformat()
        return cls(
            output_root=root,
            business_date=business_date,
            generation_id=generation_id,
            live=root / day,
            staging=root / "_staging" / day / generation_id,
            failed=root / "_failed" / day / generation_id,
            backup=root / "_backups" / day / generation_id,
            lock_file=root / "_locks" / f"{day}.lock",
        )


def hash_file_streaming(
    path: Path,
    chunk_size: int = HASH_CHUNK_SIZE,
) -> str:
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive native integer")
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def validate_relative_artifact_path(path: Path) -> Path:
    candidate = Path(path)
    if (
        candidate.is_absolute()
        or candidate == Path(".")
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError("artifact path must be a non-empty safe relative path")
    return candidate


def _safe_component(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("path component source must be a string")
    try:
        value.encode("utf-8")
        return safe_path_component(value)
    except UnicodeError:
        raise ValueError("path component contains invalid Unicode") from None


def _require_canonical_utc(value: object, description: str) -> str:
    if type(value) is not str or _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{description} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError(f"{description} must be a canonical UTC timestamp") from None
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{description} must be a canonical UTC timestamp")
    return value


def _token_relative_root(prefix: str, target: TargetToken) -> Path:
    return (
        Path(prefix)
        / _safe_component(target.requested_chain)
        / _safe_component(target.requested_token_address)
    )


def _destination(root: Path, relative: Path) -> Path:
    relative = validate_relative_artifact_path(relative)
    root = Path(root)
    resolved_root = root.resolve(strict=False)
    destination = root / relative
    try:
        destination.parent.resolve(strict=False).relative_to(resolved_root)
    except (OSError, ValueError):
        raise ValueError("artifact destination escapes generation root") from None

    current = root
    if current.exists() and current.is_symlink():
        raise ValueError("generation root must not be a symlink")
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError("artifact parent must not be a symlink")
    if destination.is_symlink():
        raise ValueError("artifact destination must not be a symlink")
    return destination


_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _open_directory_path(path: Path, *, create: bool) -> int:
    absolute = Path(path)
    if not absolute.is_absolute():
        absolute = Path.cwd() / absolute
    descriptor = os.open(
        absolute.anchor or "/",
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_relative_directory(
    root_descriptor: int,
    relative: Path,
    *,
    create: bool,
) -> int:
    relative = validate_relative_artifact_path(relative)
    descriptor = os.dup(root_descriptor)
    try:
        for component in relative.parts:
            try:
                child = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_relative_parent(
    root_descriptor: int,
    relative: Path,
    *,
    create: bool,
) -> tuple[int, str]:
    relative = validate_relative_artifact_path(relative)
    parent = relative.parent
    if parent == Path("."):
        return os.dup(root_descriptor), relative.name
    return (
        _open_relative_directory(root_descriptor, parent, create=create),
        relative.name,
    )


def _atomic_write_at_directory(
    parent_descriptor: int,
    filename: str,
    payload: bytes,
) -> None:
    try:
        existing = os.stat(
            filename,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise ValueError("artifact destination must be a regular file")

    temporary = f".tmp-{os.getpid()}-{secrets.token_hex(12)}"
    descriptor: int | None = None
    renamed = False
    primary_error: BaseException | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("artifact write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.rename(
            temporary,
            filename,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        renamed = True
        os.fsync(parent_descriptor)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except Exception as error:
                if primary_error is not None:
                    primary_error.add_note(f"temporary close failure: {error!r}")
                else:
                    raise
        if not renamed:
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            except Exception as error:
                if primary_error is not None:
                    primary_error.add_note(f"temporary unlink failure: {error!r}")
                else:
                    raise


def _write_bytes_secure(path: Path, payload: bytes) -> None:
    if type(payload) is not bytes:
        raise TypeError("artifact payload must be bytes")
    path = Path(path)
    parent_descriptor = _open_directory_path(path.parent, create=True)
    try:
        _atomic_write_at_directory(parent_descriptor, path.name, payload)
    finally:
        os.close(parent_descriptor)


def _write_bytes_relative_secure(
    root_descriptor: int,
    relative: Path,
    payload: bytes,
) -> None:
    parent_descriptor, filename = _open_relative_parent(
        root_descriptor,
        relative,
        create=True,
    )
    try:
        _atomic_write_at_directory(parent_descriptor, filename, payload)
    finally:
        os.close(parent_descriptor)


def _run_relative_paths(paths: RunPaths) -> dict[str, Path]:
    expected = RunPaths.create(
        paths.output_root,
        paths.business_date,
        paths.generation_id,
    )
    for field in ("live", "staging", "failed", "backup", "lock_file"):
        if Path(getattr(paths, field)) != Path(getattr(expected, field)):
            raise ValueError("RunPaths fields do not match their output root")
    day = paths.business_date.isoformat()
    return {
        "live": Path(day),
        "staging": Path("_staging") / day / paths.generation_id,
        "failed": Path("_failed") / day / paths.generation_id,
        "backup": Path("_backups") / day / paths.generation_id,
        "lock_file": Path("_locks") / f"{day}.lock",
        "backup_day": Path("_backups") / day,
        "trash_day": Path("_trash") / day,
    }


def _stat_relative(
    root_descriptor: int,
    relative: Path,
) -> os.stat_result | None:
    try:
        parent_descriptor, name = _open_relative_parent(
            root_descriptor,
            relative,
            create=False,
        )
    except FileNotFoundError:
        return None
    try:
        try:
            return os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
    finally:
        os.close(parent_descriptor)


def _require_real_directory_relative(
    root_descriptor: int,
    relative: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> os.stat_result:
    metadata = _stat_relative(root_descriptor, relative)
    if metadata is None or not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"{relative} is not a real directory")
    identity = (metadata.st_dev, metadata.st_ino)
    if expected_identity is not None and identity != expected_identity:
        raise OSError(f"{relative} changed after validation")
    return metadata


def _entry_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _ensure_relative_directory(root_descriptor: int, relative: Path) -> None:
    descriptor = _open_relative_directory(
        root_descriptor,
        relative,
        create=True,
    )
    os.close(descriptor)


def _unlink_relative(
    root_descriptor: int,
    relative: Path,
    *,
    missing_ok: bool,
) -> None:
    try:
        parent_descriptor, name = _open_relative_parent(
            root_descriptor,
            relative,
            create=False,
        )
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    try:
        try:
            metadata = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{relative} must not be a directory")
        os.unlink(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _rename_relative(
    root_descriptor: int,
    source: Path,
    destination: Path,
    *,
    expected_source_identity: tuple[int, int] | None = None,
) -> None:
    source_parent, source_name = _open_relative_parent(
        root_descriptor,
        source,
        create=False,
    )
    try:
        destination_parent, destination_name = _open_relative_parent(
            root_descriptor,
            destination,
            create=True,
        )
        try:
            source_metadata = os.stat(
                source_name,
                dir_fd=source_parent,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(source_metadata.st_mode):
                raise OSError(f"{source} is not a real directory")
            if (
                expected_source_identity is not None
                and _entry_identity(source_metadata) != expected_source_identity
            ):
                raise OSError(f"{source} changed after validation")
            try:
                os.stat(
                    destination_name,
                    dir_fd=destination_parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(f"{destination} already exists")
            os.rename(
                source_name,
                destination_name,
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
            )
        finally:
            os.close(destination_parent)
    finally:
        os.close(source_parent)


def _fsync_rename_parents(
    root_descriptor: int,
    source: Path,
    destination: Path,
) -> None:
    source_parent, _source_name = _open_relative_parent(
        root_descriptor,
        source,
        create=False,
    )
    try:
        destination_parent, _destination_name = _open_relative_parent(
            root_descriptor,
            destination,
            create=False,
        )
        try:
            os.fsync(source_parent)
            os.fsync(destination_parent)
        finally:
            os.close(destination_parent)
    finally:
        os.close(source_parent)


def _list_real_directory_entries(
    root_descriptor: int,
    relative: Path,
) -> list[tuple[str, os.stat_result]]:
    try:
        descriptor = _open_relative_directory(
            root_descriptor,
            relative,
            create=False,
        )
    except FileNotFoundError:
        return []
    try:
        entries: list[tuple[str, os.stat_result]] = []
        for name in os.listdir(descriptor):
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise OSError(f"{relative / name} is not a real directory")
            entries.append((name, metadata))
        return sorted(entries, key=lambda entry: entry[0])
    finally:
        os.close(descriptor)


def _safe_rmtree_contents(descriptor: int) -> None:
    with os.scandir(descriptor) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            try:
                _safe_rmtree_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)
    os.fsync(descriptor)


def _safe_rmtree_relative(
    root_descriptor: int,
    relative: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    parent_descriptor, name = _open_relative_parent(
        root_descriptor,
        relative,
        create=False,
    )
    try:
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_descriptor)
        try:
            metadata = os.fstat(descriptor)
            if (
                expected_identity is not None
                and _entry_identity(metadata) != expected_identity
            ):
                raise OSError(f"{relative} changed before deletion")
            _safe_rmtree_contents(descriptor)
        finally:
            os.close(descriptor)
        os.rmdir(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _unique_relative_destination(
    root_descriptor: int,
    base: Path,
) -> Path:
    candidate = base
    suffix = 0
    while _stat_relative(root_descriptor, candidate) is not None:
        suffix += 1
        candidate = base.with_name(f"{base.name}-{suffix}")
    return candidate


def _move_to_trash(
    root_descriptor: int,
    *,
    source: Path,
    trash_day: Path,
    expected_source_identity: tuple[int, int],
) -> tuple[Path, tuple[int, int]]:
    _ensure_relative_directory(root_descriptor, trash_day)
    destination = _unique_relative_destination(
        root_descriptor,
        trash_day / source.name,
    )
    _rename_relative(
        root_descriptor,
        source,
        destination,
        expected_source_identity=expected_source_identity,
    )
    _fsync_rename_parents(root_descriptor, source, destination)
    metadata = _require_real_directory_relative(root_descriptor, destination)
    return destination, _entry_identity(metadata)


def _best_effort_cleanup_trash(
    root_descriptor: int,
    trash_day: Path,
) -> None:
    try:
        descriptor = _open_relative_directory(
            root_descriptor,
            trash_day,
            create=False,
        )
    except (FileNotFoundError, OSError):
        return
    try:
        names = list(os.listdir(descriptor))
        entries = [
            (
                name,
                os.stat(name, dir_fd=descriptor, follow_symlinks=False),
            )
            for name in names
        ]
    except OSError:
        return
    finally:
        os.close(descriptor)

    for name, metadata in entries:
        relative = trash_day / name
        try:
            if stat.S_ISDIR(metadata.st_mode):
                _safe_rmtree_relative(
                    root_descriptor,
                    relative,
                    expected_identity=_entry_identity(metadata),
                )
            else:
                _unlink_relative(
                    root_descriptor,
                    relative,
                    missing_ok=True,
                )
        except (OSError, ValueError):
            continue


def _fsync_output_root(root_descriptor: int) -> None:
    os.fsync(root_descriptor)


def _int_text(value: int) -> str:
    sign, digits, exponent = Decimal(value).as_tuple()
    text = "".join(chr(ord("0") + digit) for digit in digits) or "0"
    if exponent > 0:
        text += "0" * exponent
    elif exponent < 0:
        point = len(text) + exponent
        text = (
            "0." + "0" * (-point) + text
            if point <= 0
            else text[:point] + "." + text[point:]
        )
    return ("-" if sign else "") + text


def _parse_unbounded_native_int(text: str) -> int:
    if not text:
        raise ValueError("JSON integer is empty")
    negative = text[0] == "-"
    digits = text[1:] if negative else text
    if (
        not digits
        or any(character < "0" or character > "9" for character in digits)
        or (len(digits) > 1 and digits[0] == "0")
    ):
        raise ValueError("JSON integer syntax is invalid")
    value = 0
    first_chunk_length = len(digits) % 9 or 9
    index = 0
    while index < len(digits):
        chunk_length = first_chunk_length if index == 0 else 9
        chunk_value = 0
        for character in digits[index : index + chunk_length]:
            chunk_value = chunk_value * 10 + ord(character) - ord("0")
        value = value * (10**chunk_length) + chunk_value
        index += chunk_length
    return -value if negative else value


def _json_text(value: object) -> str:
    active: set[int] = set()

    def encode(current: object, depth: int) -> str:
        current_type = type(current)
        if current_type is dict:
            if depth > _MAX_JSON_NESTING_DEPTH:
                raise ValueError("JSON payload exceeds maximum nesting depth")
            identity = id(current)
            if identity in active:
                raise ValueError("JSON payload must be acyclic")
            active.add(identity)
            try:
                items = []
                for key in sorted(current):
                    if type(key) is not str:
                        raise TypeError("JSON object keys must be native strings")
                    items.append(
                        json.dumps(key, ensure_ascii=True)
                        + ":"
                        + encode(current[key], depth + 1)
                    )
                return "{" + ",".join(items) + "}"
            finally:
                active.remove(identity)
        if current_type is list:
            if depth > _MAX_JSON_NESTING_DEPTH:
                raise ValueError("JSON payload exceeds maximum nesting depth")
            identity = id(current)
            if identity in active:
                raise ValueError("JSON payload must be acyclic")
            active.add(identity)
            try:
                return "[" + ",".join(
                    encode(item, depth + 1) for item in current
                ) + "]"
            finally:
                active.remove(identity)
        if current is None:
            return "null"
        if current_type is str:
            return json.dumps(current, ensure_ascii=True)
        if current_type is bool:
            return "true" if current else "false"
        if current_type is int:
            return _int_text(current)
        if current_type is float:
            if not math.isfinite(current):
                raise ValueError("JSON float values must be finite")
            return json.dumps(current, allow_nan=False)
        raise TypeError(
            f"object of type {current_type.__name__} is not JSON serializable"
        )

    try:
        return encode(value, 0) + "\n"
    except RecursionError:
        raise ValueError("JSON payload exceeds maximum nesting depth") from None


def _write_json(path: Path, payload: object) -> None:
    _write_bytes_secure(path, _json_text(payload).encode("ascii"))


def _api_identity_document(target: TargetToken) -> dict[str, str]:
    if not isinstance(target, TargetToken):
        raise ValueError("artifact target must be a TargetToken")
    return {
        "chain": target.requested_chain,
        "token_address": target.requested_token_address,
        "canonical_chain": target.chain,
        "canonical_token_address": target.token_address,
    }


def _decode_percent_encoded_text(value: str, description: str) -> str:
    current = value
    for _round in range(_MAX_API_URL_DECODE_ROUNDS):
        if _INVALID_PERCENT_ESCAPE.search(current) is not None:
            raise ValueError(f"{description} contains an invalid percent escape")
        if "%" not in current:
            return current
        try:
            current = unquote_to_bytes(current).decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{description} contains invalid UTF-8 encoding") from None
    if "%" in current:
        raise ValueError(f"{description} percent encoding is too deeply nested")
    return current


def _public_artifact_key_is_sensitive(key: str) -> bool:
    decoded = _decode_percent_encoded_text(key, "public artifact key")
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", decoded)
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", normalized).strip("_").lower()
    return (
        normalized not in _PUBLIC_ARTIFACT_IDENTITY_KEYS
        and _SENSITIVE_PUBLIC_ARTIFACT_KEY.search(normalized) is not None
    )


def _retry_string_is_sensitive(value: str) -> bool:
    return any(
        pattern.search(value) is not None
        for pattern in (
            _SENSITIVE_ASSIGNMENT,
            _BM_API_VALUE,
            _DATABASE_USERINFO,
            _BEARER_VALUE,
            _SENSITIVE_CREDENTIAL_TERM,
        )
    )


def _retry_key_is_sensitive(key: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", normalized).strip("_").lower()
    if _SENSITIVE_RETRY_KEY.search(normalized) is not None:
        return True
    components = normalized.split("_") if normalized else []
    if "token" not in components:
        return False
    return key not in _PUBLIC_RETRY_TOKEN_KEYS


def _public_artifact_string_is_sensitive(value: str) -> bool:
    current = value
    for _round in range(_MAX_API_URL_DECODE_ROUNDS):
        if _retry_string_is_sensitive(current):
            return True
        if "%" not in current:
            return False
        scan_copy = current.encode("utf-8", errors="replace").decode("utf-8")
        decoded = unquote_to_bytes(scan_copy).decode("utf-8", errors="replace")
        if decoded == current:
            return False
        current = decoded
    if _retry_string_is_sensitive(current):
        return True
    if "%" not in current:
        return False
    scan_copy = current.encode("utf-8", errors="replace").decode("utf-8")
    decoded = unquote_to_bytes(scan_copy).decode("utf-8", errors="replace")
    if decoded != current:
        raise ValueError("public artifact string percent encoding is too deeply nested")
    return False


def _validate_public_artifact_payload(payload: object, description: str) -> None:
    """Reject secrets while keeping the official response payload otherwise intact."""
    try:
        _json_text(payload)
    except (TypeError, ValueError):
        raise ValueError(f"{description} must contain JSON-safe data") from None

    pending = [payload]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if type(current) is dict:
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            for key, value in current.items():
                if type(key) is not str:
                    raise ValueError(f"{description} keys must be native strings")
                if _public_artifact_key_is_sensitive(key):
                    raise ValueError(f"{description} contains a sensitive field")
                pending.append(value)
        elif type(current) is list:
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            pending.extend(current)
        elif type(current) is str and _public_artifact_string_is_sensitive(current):
            raise ValueError(f"{description} contains a sensitive value")


def _validate_api_targets(targets: object) -> dict[str, list[str]]:
    if type(targets) is not dict:
        raise ValueError("targets must be a dictionary")
    normalized: dict[str, list[str]] = {}
    seen_identities: set[tuple[str, str]] = set()
    for chain, addresses in targets.items():
        if type(chain) is not str or not chain or chain != chain.strip():
            raise ValueError("target chain keys must be non-empty strings")
        if type(addresses) is not list or not addresses:
            raise ValueError("target address arrays must be non-empty lists")
        if addresses != sorted(addresses) or len(addresses) != len(set(addresses)):
            raise ValueError("target address arrays must be sorted and unique")
        values: list[str] = []
        for address in addresses:
            try:
                target = make_target(chain, address)
            except (TypeError, ValueError, UnicodeError):
                raise ValueError("target identity is invalid") from None
            if target.chain != chain:
                raise ValueError("target chain must use its canonical name")
            identity = (target.chain, target.token_address)
            if identity in seen_identities:
                raise ValueError("target identities must be unique")
            seen_identities.add(identity)
            values.append(address)
        normalized[chain] = values
    return {chain: normalized[chain] for chain in sorted(normalized)}


def _trusted_api_staging_root(staging: Path) -> Path:
    try:
        root = Path(os.path.abspath(Path(staging)))
        current = Path(root.anchor)
        trusted_var_alias = False
        for component in root.parts[1:]:
            current /= component
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                break
            if not stat.S_ISLNK(metadata.st_mode):
                continue
            if (
                current == Path("/var")
                and os.readlink(current) in {"private/var", "/private/var"}
                and Path(os.path.realpath(current)) == Path("/private/var")
            ):
                trusted_var_alias = True
                continue
            raise ValueError("API generation root must not contain a symlink")

        # macOS exposes /private/var through the root-owned /var system alias.
        if trusted_var_alias:
            return Path("/private/var", *root.parts[2:])
        return root
    except (OSError, UnicodeError, ValueError):
        raise ValueError("API generation root cannot be resolved") from None


def write_targets(staging: Path, targets: dict[str, list[str]]) -> str:
    """Write the validated API target selection as the generation's root artifact."""
    staging = _trusted_api_staging_root(staging)
    validated = _validate_api_targets(targets)
    relative = Path("targets.json")
    _write_json(_destination(staging, relative), validated)
    return str(relative)


def read_targets(staging: Path) -> dict[str, list[str]]:
    """Read and validate the generation's root target selection artifact."""
    try:
        staging = _trusted_api_staging_root(staging)
        document = _load_json_document(
            _destination(staging, Path("targets.json"))
        )
        return _validate_api_targets(document)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("API targets artifact cannot be read") from None


def _api_artifact_kind(
    target: TargetToken,
    kind: str,
    *,
    layer: str,
) -> tuple[Path, str, str | None]:
    if not isinstance(target, TargetToken) or type(kind) is not str:
        raise ValueError("artifact target and kind are invalid")
    if layer == "raw":
        if kind == "holders":
            return _token_relative_root("raw", target) / "holders.json", kind, None
        if kind == "subgraph":
            return _token_relative_root("raw", target) / "subgraph.json", kind, None
    elif layer == "clean":
        if kind == "holders":
            return _token_relative_root("clean", target) / "holders.json", kind, None
        if kind == "relationships":
            return _token_relative_root("clean", target) / "relationships.json", kind, None
    if not kind.startswith("transfers/") or kind.count("/") != 1:
        raise ValueError("artifact kind is not part of the API contract")
    raw_member = kind.split("/", 1)[1]
    try:
        member = canonicalize_address(target.chain, raw_member)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("artifact transfer member address is invalid") from None
    if member != raw_member:
        raise ValueError("artifact transfer member address must be canonical")
    return (
        _token_relative_root(layer, target)
        / "transfers"
        / f"{_safe_component(member)}.json",
        "transfers",
        member,
    )


def _decode_api_url_component(value: str) -> str:
    return _decode_percent_encoded_text(value, "API request URL")


def _validate_api_request_contract(
    method: str,
    url: str,
    *,
    target: TargetToken,
    expected_kind: str,
    member_address: str | None,
) -> None:
    try:
        expected_method, expected_path = _API_REQUEST_CONTRACTS[expected_kind]
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (KeyError, TypeError, ValueError):
        raise ValueError("API request URL contract is invalid") from None
    if (
        method != expected_method
        or parsed.scheme != "https"
        or hostname not in _OFFICIAL_API_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or parsed.netloc.lower() != hostname.lower()
        or parsed.path != expected_path
        or _INVALID_PERCENT_ESCAPE.search(parsed.query) is not None
    ):
        raise ValueError("API request is not an official endpoint contract")
    try:
        parsed_pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
        pairs = [
            (_decode_api_url_component(key), _decode_api_url_component(value))
            for key, value in parsed_pairs
        ]
    except ValueError:
        raise ValueError("API request URL query is invalid") from None
    if len(pairs) != len({key for key, _value in pairs}):
        raise ValueError("API request URL query fields must be unique")
    for key, value in pairs:
        if (
            (
                key != "queue_whitelisted_token_map"
                and _retry_key_is_sensitive(key)
            )
            or _retry_string_is_sensitive(key)
            or _retry_string_is_sensitive(value)
        ):
            raise ValueError("API request URL query contains sensitive data")
    query = dict(pairs)
    if expected_kind == "holders":
        expected_query = {"count": "300", "nocache": "false"}
    elif expected_kind == "subgraph":
        expected_query = {
            "whitelist_token_address": target.requested_token_address,
            "whitelist_token_chain": target.chain,
            "queue_whitelisted_token_map": "false",
        }
    elif expected_kind == "transfers":
        if member_address is None:
            raise ValueError("API transfer request member binding is missing")
        expected_query = {
            "address": member_address,
            "whitelist_token_address": target.requested_token_address,
            "whitelist_token_chain": target.chain,
        }
    else:
        raise ValueError("API request kind has no endpoint contract")
    if query != expected_query:
        raise ValueError("API request URL query does not match its target")


def _api_request_metadata(
    value: object,
    target: TargetToken,
    *,
    expected_kind: str,
    member_address: str | None,
) -> tuple[object, dict]:
    """Extract the deliberately redacted metadata exposed by BubblemapsApiClient."""
    try:
        from getMarket.bubblemaps.tool.bubblemaps_api import ApiResult, RequestMetadata
    except ImportError:
        ApiResult = ()  # type: ignore[assignment,misc]
        RequestMetadata = ()  # type: ignore[assignment,misc]
    if not isinstance(value, ApiResult):
        raise ValueError("raw API response must be an ApiResult with request metadata")
    metadata = value.metadata
    if not isinstance(metadata, RequestMetadata):
        raise ValueError("API response metadata is invalid")
    if (
        metadata.request_chain != target.requested_chain
        or metadata.request_token_address != target.requested_token_address
        or type(metadata.method) is not str
        or metadata.method not in {"GET", "POST"}
        or type(metadata.url) is not str
        or type(metadata.status) is not int
        or not 200 <= metadata.status < 300
        or type(metadata.attempts) is not int
        or metadata.attempts <= 0
    ):
        raise ValueError("API response metadata is invalid")
    _validate_api_request_contract(
        metadata.method,
        metadata.url,
        target=target,
        expected_kind=expected_kind,
        member_address=member_address,
    )
    return value.payload, {
        "method": metadata.method,
        "url": metadata.url,
        "status": metadata.status,
        "attempts": metadata.attempts,
    }


def write_raw_response(
    staging: Path,
    target: TargetToken,
    kind: str,
    payload: object,
) -> str:
    """Persist one full official response with only non-sensitive request metadata."""
    staging = _trusted_api_staging_root(staging)
    relative, canonical_kind, member = _api_artifact_kind(target, kind, layer="raw")
    response_payload, request = _api_request_metadata(
        payload,
        target,
        expected_kind=canonical_kind,
        member_address=member,
    )
    _validate_public_artifact_payload(response_payload, "raw API response")
    document = {
        "schema_version": "v3",
        "kind": canonical_kind,
        **_api_identity_document(target),
        "request": request,
        "payload": response_payload,
    }
    if member is not None:
        document["member_address"] = member
    _write_json(_destination(staging, relative), document)
    return str(relative)


def write_clean_response(
    staging: Path,
    target: TargetToken,
    kind: str,
    payload: object,
) -> str:
    """Persist a normalized holder or relationship array under the clean layer."""
    staging = _trusted_api_staging_root(staging)
    relative, _canonical_kind, member = _api_artifact_kind(target, kind, layer="clean")
    if member is not None or type(payload) is not list:
        raise ValueError("clean snapshot payload must be a top-level list")
    _validate_public_artifact_payload(payload, "clean API response")
    _write_json(_destination(staging, relative), payload)
    return str(relative)


def write_clean_member_transfers(
    staging: Path,
    target: TargetToken,
    member_address: str,
    payload: list[dict],
    *,
    cluster_rank: int | None = None,
) -> str:
    """Persist one ordinary member's filtered transfer history in the clean layer."""
    staging = _trusted_api_staging_root(staging)
    relative, _kind, member = _api_artifact_kind(
        target,
        f"transfers/{member_address}",
        layer="clean",
    )
    assert member is not None
    if type(payload) is not list or any(type(row) is not dict for row in payload):
        raise ValueError("clean member transfers must be a list of objects")
    if cluster_rank is not None and (
        type(cluster_rank) is not int or cluster_rank <= 0
    ):
        raise ValueError("clean member cluster_rank must be a positive integer")
    _validate_public_artifact_payload(payload, "clean member transfers")
    _write_json(
        _destination(staging, relative),
        {
            "schema_version": "v3",
            **_api_identity_document(target),
            "cluster_rank": cluster_rank,
            "member_address": member,
            "transfer_count": len(payload),
            "transfers": payload,
        },
    )
    return str(relative)


def _require_exact_identity(document: dict, target: TargetToken) -> None:
    expected = {
        "chain": target.requested_chain,
        "token_address": target.requested_token_address,
        "canonical_chain": target.chain,
        "canonical_token_address": target.token_address,
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise ValueError("formal document identity does not match target")


def _compare_decimal_magnitudes(left: str, right: str) -> int:
    if len(left) != len(right):
        return 1 if len(left) > len(right) else -1
    if left == right:
        return 0
    return 1 if left > right else -1


def _add_decimal_magnitudes(left: str, right: str) -> str:
    left_index = len(left) - 1
    right_index = len(right) - 1
    carry = 0
    reversed_digits: list[str] = []
    while left_index >= 0 or right_index >= 0 or carry:
        total = carry
        if left_index >= 0:
            total += ord(left[left_index]) - ord("0")
            left_index -= 1
        if right_index >= 0:
            total += ord(right[right_index]) - ord("0")
            right_index -= 1
        reversed_digits.append(chr(ord("0") + total % 10))
        carry = total // 10
    return "".join(reversed(reversed_digits))


def _subtract_decimal_magnitudes(larger: str, smaller: str) -> str:
    larger_index = len(larger) - 1
    smaller_index = len(smaller) - 1
    borrow = 0
    reversed_digits: list[str] = []
    while larger_index >= 0:
        difference = ord(larger[larger_index]) - ord("0") - borrow
        if smaller_index >= 0:
            difference -= ord(smaller[smaller_index]) - ord("0")
            smaller_index -= 1
        if difference < 0:
            difference += 10
            borrow = 1
        else:
            borrow = 0
        reversed_digits.append(chr(ord("0") + difference))
        larger_index -= 1
    return "".join(reversed(reversed_digits)).lstrip("0") or "0"


def _adjust_lexical_exponent(
    exponent_sign: str,
    exponent_digits: str,
    offset: int,
) -> str:
    magnitude = exponent_digits.lstrip("0") or "0"
    base_sign = -1 if exponent_sign == "-" and magnitude != "0" else 0
    if base_sign == 0 and magnitude != "0":
        base_sign = 1
    offset_sign = -1 if offset < 0 else (1 if offset > 0 else 0)
    offset_magnitude = str(abs(offset))
    if base_sign == 0:
        result_sign = offset_sign
        result_magnitude = offset_magnitude
    elif offset_sign == 0:
        result_sign = base_sign
        result_magnitude = magnitude
    elif base_sign == offset_sign:
        result_sign = base_sign
        result_magnitude = _add_decimal_magnitudes(magnitude, offset_magnitude)
    else:
        comparison = _compare_decimal_magnitudes(magnitude, offset_magnitude)
        if comparison == 0:
            return "0"
        if comparison > 0:
            result_sign = base_sign
            result_magnitude = _subtract_decimal_magnitudes(
                magnitude,
                offset_magnitude,
            )
        else:
            result_sign = offset_sign
            result_magnitude = _subtract_decimal_magnitudes(
                offset_magnitude,
                magnitude,
            )
    return ("-" if result_sign < 0 else "") + result_magnitude


def _canonical_string_transfer_value(value: str) -> str:
    match = _DECIMAL_TEXT_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("formal transfer value has invalid decimal syntax")
    integer = match.group("integer") or ""
    fraction = match.group("fraction")
    if fraction is None:
        fraction = match.group("fraction_only") or ""
    significant = (integer + fraction).lstrip("0")
    if not significant:
        return "0"
    if match.group("sign") == "-":
        raise ValueError("formal transfer value must be nonnegative")
    coefficient = significant.rstrip("0")
    stripped_trailing_zeroes = len(significant) - len(coefficient)
    exponent = _adjust_lexical_exponent(
        match.group("exponent_sign") or "",
        match.group("exponent") or "0",
        stripped_trailing_zeroes - len(fraction),
    )
    return f"{coefficient}e{exponent}"


def _canonical_transfer_value(value: object) -> str:
    if type(value) is str:
        return _canonical_string_transfer_value(value)
    if type(value) is not int:
        raise ValueError(
            "formal transfer value must be an exact decimal string or native integer"
        )
    number = Decimal(value)
    if number < 0:
        raise ValueError("formal transfer value must be nonnegative")
    if number.is_zero():
        return "0"
    _sign, coefficient, exponent = number.as_tuple()
    digits = list(coefficient)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    digit_text = "".join(chr(ord("0") + digit) for digit in digits)
    return f"{digit_text}e{exponent}"


def _derive_formal_transfer_counts(
    member_documents: dict[str, dict],
    *,
    target: TargetToken,
) -> tuple[int, int]:
    unique: dict[tuple, object] = {}
    view_count = 0
    for member_address, document in member_documents.items():
        transfers = document.get("transfers")
        if type(transfers) is not list:
            raise ValueError("formal ordinary-member transfers must be a list")
        response_identities: set[tuple] = set()
        for transfer in transfers:
            if type(transfer) is not dict or transfer.get("rel_type") != "TRANSFER":
                raise ValueError("formal transfer row is invalid")
            data = transfer.get("data")
            if type(data) is not dict or not token_ref_matches(data.get("token_ref"), target):
                raise ValueError("formal transfer token reference is invalid")
            try:
                from_address = canonicalize_address(
                    target.chain,
                    transfer.get("from_address"),
                )
                to_address = canonicalize_address(
                    target.chain,
                    transfer.get("to_address"),
                )
            except (TypeError, ValueError, UnicodeError):
                raise ValueError("formal transfer endpoint is invalid") from None
            if member_address not in (from_address, to_address):
                raise ValueError("formal transfer does not contain its member address")
            tx_hash = data.get("tx_hash")
            transfer_date = data.get("date")
            if type(tx_hash) is not str or not tx_hash:
                raise ValueError("formal transfer tx_hash is invalid")
            if type(transfer_date) is not int or transfer_date < 0:
                raise ValueError("formal transfer date is invalid")
            identity = (
                target.chain,
                target.token_address,
                tx_hash,
                from_address,
                to_address,
                transfer_date,
                _canonical_transfer_value(data.get("value")),
            )
            if identity in response_identities:
                raise ValueError("formal member view repeats a fallback identity")
            response_identities.add(identity)
            previous = unique.get(identity)
            if previous is not None and previous != transfer:
                raise ValueError("formal member views conflict for a fallback identity")
            unique[identity] = transfer
            view_count += 1
    return len(unique), view_count


def _holding_decimal(value: object, description: str) -> Decimal:
    if (
        type(value) is not str
        or len(value) > _MAX_HOLDING_DECIMAL_TEXT_LENGTH
        or _HOLDING_DECIMAL_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(f"{description} must be canonical decimal text")
    try:
        number = Decimal(value)
    except DecimalException:
        raise ValueError(f"{description} must be canonical decimal text") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"{description} must be a nonnegative finite decimal")
    return number


def _exact_decimal_sum(values: list[Decimal]) -> Decimal:
    precision = max(
        32,
        sum(len(value.as_tuple().digits) for value in values) + 4,
    )
    try:
        with localcontext() as context:
            context.prec = precision
            total = Decimal(0)
            for value in values:
                total += value
            return total
    except DecimalException:
        raise ValueError("formal Cluster decimal aggregation failed") from None


def _decimal_percent_text(value: Decimal) -> str:
    try:
        with localcontext() as context:
            context.prec = max(32, len(value.as_tuple().digits) + 4)
            return format(value * Decimal(100), "f")
    except DecimalException:
        raise ValueError("formal holding share percentage is invalid") from None


def _validate_metadata(metadata: object) -> None:
    if type(metadata) is not dict:
        raise ValueError("formal member metadata must be an object")
    try:
        _json_text(metadata)
    except (TypeError, ValueError):
        raise ValueError("formal member metadata must be JSON-safe") from None


def _validate_formal_holding_structure(
    token: object,
    *,
    target: TargetToken,
    captured_at: str,
    schema_version: str = "v2",
) -> dict[str, dict]:
    if type(token) is not dict or set(token) != _FORMAL_TOKEN_FIELDS:
        raise ValueError("formal token fields are invalid")
    _require_exact_identity(token, target)
    if (
        token.get("schema_version") != schema_version
        or token.get("captured_at") != captured_at
    ):
        raise ValueError("formal token schema or captured_at is invalid")
    clusters = token.get("clusters")
    if type(clusters) is not list:
        raise ValueError("formal token clusters must be a list")

    summaries: dict[str, dict] = {}
    cluster_sort_rows: list[tuple[Decimal, Decimal, tuple[str, ...], dict]] = []
    for expected_cluster_rank, cluster in enumerate(clusters, start=1):
        if type(cluster) is not dict or set(cluster) != _FORMAL_CLUSTER_FIELDS:
            raise ValueError("formal Cluster fields are invalid")
        if cluster.get("cluster_rank") != expected_cluster_rank:
            raise ValueError("formal Cluster ranks must be contiguous")
        members = cluster.get("members")
        if (
            type(members) is not list
            or cluster.get("member_count") != len(members)
            or len(members) < 2
        ):
            raise ValueError("formal Cluster member_count is invalid")

        amount = _holding_decimal(cluster.get("amount"), "formal Cluster amount")
        share = _holding_decimal(cluster.get("share"), "formal Cluster share")
        _holding_decimal(
            cluster.get("share_percent"),
            "formal Cluster share_percent",
        )
        member_sort_rows: list[tuple[Decimal, Decimal, int, str, dict]] = []
        member_amounts: list[Decimal] = []
        member_shares: list[Decimal] = []
        for expected_member_rank, member in enumerate(members, start=1):
            if type(member) is not dict:
                raise ValueError("formal Cluster member must be an object")
            expected_fields = (
                _FORMAL_MEMBER_BASE_FIELDS
                | _FORMAL_MEMBER_COMMON_TRANSFER_FIELDS
                | (
                    frozenset({"transfer_details_reason"})
                    if member.get("transfer_details_available") is False
                    else frozenset()
                )
            )
            if set(member) != expected_fields:
                raise ValueError("formal Cluster member fields are invalid")
            if member.get("member_rank") != expected_member_rank:
                raise ValueError("formal member ranks must be contiguous")
            source_rank = member.get("source_rank")
            if type(source_rank) is not int or source_rank <= 0:
                raise ValueError("formal member source_rank is invalid")
            address = member.get("address")
            try:
                canonical = canonicalize_address(target.chain, address)
            except (TypeError, ValueError, UnicodeError):
                raise ValueError("formal member address is invalid") from None
            if canonical != address or address in summaries:
                raise ValueError("formal member address is not canonical and unique")
            if type(member.get("is_supernode")) is not bool:
                raise ValueError("formal member Supernode flag is invalid")
            _validate_metadata(member.get("metadata"))
            member_amount = _holding_decimal(
                member.get("amount"),
                "formal member amount",
            )
            member_share = _holding_decimal(
                member.get("share"),
                "formal member share",
            )
            _holding_decimal(
                member.get("share_percent"),
                "formal member share_percent",
            )
            if member["share_percent"] != _decimal_percent_text(member_share):
                raise ValueError("formal member share_percent does not match share")
            summaries[address] = member
            member_amounts.append(member_amount)
            member_shares.append(member_share)
            member_sort_rows.append(
                (
                    member_amount.copy_negate(),
                    member_share.copy_negate(),
                    source_rank,
                    address,
                    member,
                )
            )

        if [row[4] for row in sorted(member_sort_rows, key=lambda row: row[:4])] != members:
            raise ValueError("formal members are not in Task 2 sort order")
        expected_amount = format(_exact_decimal_sum(member_amounts), "f")
        expected_share_number = _exact_decimal_sum(member_shares)
        expected_share = format(expected_share_number, "f")
        expected_share_percent = _decimal_percent_text(expected_share_number)
        if (
            cluster["amount"] != expected_amount
            or cluster["share"] != expected_share
            or cluster["share_percent"] != expected_share_percent
        ):
            raise ValueError("formal Cluster holding totals do not match members")
        cluster_sort_rows.append(
            (
                amount.copy_negate(),
                share.copy_negate(),
                tuple(sorted(member["address"] for member in members)),
                cluster,
            )
        )

    if [row[3] for row in sorted(cluster_sort_rows, key=lambda row: row[:3])] != clusters:
        raise ValueError("formal Clusters are not in Task 2 sort order")
    source_ranks = [member["source_rank"] for member in summaries.values()]
    if len(source_ranks) != len(set(source_ranks)):
        raise ValueError("formal member source_rank values must be unique")
    return summaries


def _crosscheck_selected_holders(
    summaries: dict[str, dict],
    holders: Sequence,
) -> None:
    by_address = {holder.address: holder for holder in holders}
    for address, member in summaries.items():
        holder = by_address.get(address)
        if holder is None:
            raise ValueError("formal Cluster member is absent from selected holders")
        expected = (
            holder.source_rank,
            holder.amount,
            holder.share,
            holder.share_percent,
            holder.is_supernode,
            holder.metadata,
        )
        actual = (
            member["source_rank"],
            member["amount"],
            member["share"],
            member["share_percent"],
            member["is_supernode"],
            member["metadata"],
        )
        if actual != expected:
            raise ValueError("formal Cluster member does not match selected holder")


def _require_formal_transfers_in_raw_union(
    member_documents: dict[str, dict],
    raw_transfers: list[object],
) -> None:
    raw_objects = {_json_text(row) for row in raw_transfers}
    for document in member_documents.values():
        for transfer in document["transfers"]:
            if _json_text(transfer) not in raw_objects:
                raise ValueError("formal transfer is absent from raw response union")


def _api_clean_transfer_path(target: TargetToken, member_address: str) -> Path:
    return (
        _token_relative_root("clean", target)
        / "transfers"
        / f"{_safe_component(member_address)}.json"
    )


def _validate_v3_member_document(
    document: object,
    *,
    target: TargetToken,
    cluster_rank: int,
    member_address: str,
) -> dict:
    if type(document) is not dict or set(document) != _FORMAL_MEMBER_DOCUMENT_FIELDS:
        raise ValueError("clean member transfer document fields are invalid")
    _validate_public_artifact_payload(document, "clean member transfer document")
    _require_exact_identity(document, target)
    transfers = document.get("transfers")
    if (
        document.get("schema_version") != "v3"
        or document.get("cluster_rank") != cluster_rank
        or document.get("member_address") != member_address
        or type(document.get("transfer_count")) is not int
        or document["transfer_count"] < 0
        or type(transfers) is not list
        or document["transfer_count"] != len(transfers)
    ):
        raise ValueError("clean member transfer document is invalid")
    return document


def _prepare_clean_member_document(
    staging: Path,
    *,
    target: TargetToken,
    cluster_rank: int,
    member_address: str,
) -> dict:
    relative = _api_clean_transfer_path(target, member_address)
    try:
        document = _load_json_document(_destination(staging, relative))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise ValueError("clean member transfer document cannot be read") from None
    if type(document) is not dict or set(document) != _FORMAL_MEMBER_DOCUMENT_FIELDS:
        raise ValueError("clean member transfer document fields are invalid")
    if document.get("cluster_rank") is None:
        document["cluster_rank"] = cluster_rank
        _write_json(_destination(staging, relative), document)
    return _validate_v3_member_document(
        document,
        target=target,
        cluster_rank=cluster_rank,
        member_address=member_address,
    )


def _validate_v3_token_and_member_documents(
    staging: Path,
    *,
    target: TargetToken,
    token_document: object,
    captured_at: str,
    prepare_member_documents: bool,
) -> tuple[dict[str, dict], dict[str, dict], int, int, int, int]:
    _validate_public_artifact_payload(token_document, "final token document")
    summaries = _validate_formal_holding_structure(
        token_document,
        target=target,
        captured_at=captured_at,
        schema_version="v3",
    )
    assert type(token_document) is dict
    clusters = token_document.get("clusters")
    if type(clusters) is not list:
        raise ValueError("final token clusters are invalid")

    member_documents: dict[str, dict] = {}
    ordinary_count = 0
    supernode_count = 0
    for cluster_rank, cluster in enumerate(clusters, start=1):
        assert type(cluster) is dict
        members = cluster["members"]
        assert type(members) is list
        for member in members:
            assert type(member) is dict
            address = member["address"]
            if member["is_supernode"]:
                supernode_count += 1
                if (
                    member.get("transfer_details_available") is not False
                    or member.get("transfer_details_reason")
                    != "supernode_not_supported"
                    or member.get("transfer_count") != 0
                    or member.get("transfer_file") is not None
                ):
                    raise ValueError("Supernode must not reference transfer details")
                continue

            ordinary_count += 1
            if member.get("transfer_details_available") is False:
                if (
                    member.get("transfer_details_reason") != "capture_failed"
                    or member.get("transfer_count") != 0
                    or member.get("transfer_file") is not None
                ):
                    raise ValueError(
                        "unavailable ordinary member transfer reference is invalid"
                    )
                continue
            expected_relative = _api_clean_transfer_path(target, address)
            if (
                member.get("transfer_details_available") is not True
                or member.get("transfer_file") != str(expected_relative)
                or "transfer_details_reason" in member
                or type(member.get("transfer_count")) is not int
                or member["transfer_count"] < 0
            ):
                raise ValueError("ordinary member clean transfer reference is invalid")
            if prepare_member_documents:
                member_document = _prepare_clean_member_document(
                    staging,
                    target=target,
                    cluster_rank=cluster_rank,
                    member_address=address,
                )
            else:
                try:
                    member_document = _load_json_document(
                        _destination(staging, expected_relative)
                    )
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                    raise ValueError("clean member transfer document cannot be read") from None
                member_document = _validate_v3_member_document(
                    member_document,
                    target=target,
                    cluster_rank=cluster_rank,
                    member_address=address,
                )
            if member_document["transfer_count"] != member["transfer_count"]:
                raise ValueError("member summary transfer_count does not match clean file")
            member_documents[address] = member_document

    try:
        unique_transfer_count, transfer_view_count = _derive_formal_transfer_counts(
            member_documents,
            target=target,
        )
    except ValueError:
        raise ValueError("clean member transfer identities are invalid") from None
    return (
        summaries,
        member_documents,
        ordinary_count,
        supernode_count,
        unique_transfer_count,
        transfer_view_count,
    )


def _promote_task4_token_document(target: TargetToken, document: dict) -> dict:
    """Translate the Task 4 in-memory layout into the published v3 layout."""
    try:
        promoted = deepcopy(document)
    except Exception:
        raise ValueError("final token document could not be copied safely") from None
    if promoted.get("schema_version") != "v2":
        return promoted
    clusters = promoted.get("clusters")
    if type(clusters) is not list:
        return promoted
    for cluster in clusters:
        if type(cluster) is not dict or type(cluster.get("members")) is not list:
            return promoted
        for member in cluster["members"]:
            if type(member) is not dict or member.get("is_supernode") is True:
                continue
            if member.get("transfer_details_available") is False:
                if (
                    member.get("transfer_details_reason") != "capture_failed"
                    or member.get("transfer_count") != 0
                    or member.get("transfer_file") is not None
                ):
                    return promoted
                continue
            address = member.get("address")
            try:
                canonical = canonicalize_address(target.chain, address)
            except (TypeError, ValueError, UnicodeError):
                return promoted
            if canonical != address:
                return promoted
            legacy_relative = (
                Path("transfers") / f"{_safe_component(address)}.json"
            )
            if member.get("transfer_file") != str(legacy_relative):
                return promoted
            member["transfer_file"] = str(_api_clean_transfer_path(target, address))
    promoted["schema_version"] = "v3"
    return promoted


def write_final_token(
    staging: Path,
    target: TargetToken,
    document: dict,
) -> str:
    """Write a v3 final token summary after binding its clean member histories."""
    staging = _trusted_api_staging_root(staging)
    if not isinstance(target, TargetToken) or type(document) is not dict:
        raise ValueError("final token requires a typed target and object document")
    document = _promote_task4_token_document(target, document)
    captured_at = document.get("captured_at")
    _require_canonical_utc(captured_at, "final token captured_at")
    _validate_v3_token_and_member_documents(
        staging,
        target=target,
        token_document=document,
        captured_at=captured_at,
        prepare_member_documents=True,
    )
    relative = _token_relative_root("data", target) / "token.json"
    _write_json(_destination(staging, relative), document)
    return str(relative)


def _regular_tree_files(root: Path, *, reject_empty_directories: bool) -> set[Path]:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise MarketGenerationValidationError("Generation root is not a real directory")
    files: set[Path] = set()
    for directory_text, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        directory = Path(directory_text)
        for name in directory_names:
            child = directory / name
            if child.is_symlink() or not stat.S_ISDIR(child.stat(follow_symlinks=False).st_mode):
                raise MarketGenerationValidationError(
                    "Generation tree contains a symlink or non-directory entry"
                )
        for name in file_names:
            child = directory / name
            metadata = child.stat(follow_symlinks=False)
            if child.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise MarketGenerationValidationError(
                    "Generation tree contains a symlink or non-regular file"
                )
            relative = child.relative_to(root)
            validate_relative_artifact_path(relative)
            try:
                child.parent.resolve(strict=True).relative_to(root.resolve(strict=True))
            except (OSError, ValueError):
                raise MarketGenerationValidationError(
                    "Generation artifact parent escapes its root"
                ) from None
            files.add(relative)
        if reject_empty_directories and directory != root and not directory_names and not file_names:
            raise MarketGenerationValidationError(
                "Generation tree contains an unexpected empty directory"
            )
    return files


def _api_path_list(
    value: object,
    *,
    expected_root: Path,
    description: str,
) -> list[str]:
    if type(value) is not list:
        raise ValueError(f"{description} must be a unique list")
    values: list[str] = []
    seen: set[str] = set()
    for relative_text in value:
        if type(relative_text) is not str:
            raise ValueError(f"{description} paths must be text")
        if relative_text in seen:
            raise ValueError(f"{description} must be a unique list")
        seen.add(relative_text)
        relative = validate_relative_artifact_path(Path(relative_text))
        if not relative.is_relative_to(expected_root):
            raise ValueError(f"{description} paths must stay within their target")
        values.append(str(relative))
    return values


def _api_entry_document(
    entry: object,
    *,
    targets: dict[str, list[str]],
) -> dict:
    if type(entry) is not dict or set(entry) != _API_ENTRY_FIELDS:
        raise ValueError("API manifest token entry shape is invalid")
    text_fields = (
        "requested_chain",
        "requested_token_address",
        "canonical_chain",
        "canonical_token_address",
        "captured_at",
        "token_file",
    )
    count_fields = (
        "cluster_count",
        "ranked_holder_count",
        "clustered_member_count",
        "ordinary_member_count",
        "supernode_count",
        "unique_transfer_count",
        "transfer_view_count",
    )
    if any(type(entry.get(field)) is not str or not entry[field] for field in text_fields):
        raise ValueError("API manifest token text field is invalid")
    if entry["requested_token_address"] not in targets.get(
        entry["requested_chain"],
        [],
    ):
        raise ValueError("API manifest token requested target is not selected")
    try:
        _require_canonical_utc(entry["captured_at"], "API token captured_at")
        target = make_target(
            entry["requested_chain"],
            entry["requested_token_address"],
        )
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("API manifest token identity is invalid") from None
    if (
        entry["canonical_chain"] != target.chain
        or entry["canonical_token_address"] != target.token_address
        or entry.get("status") != "success"
        or any(type(entry.get(field)) is not int or entry[field] < 0 for field in count_fields)
        or entry["ordinary_member_count"] + entry["supernode_count"]
        != entry["clustered_member_count"]
        or entry["ranked_holder_count"] < entry["clustered_member_count"]
    ):
        raise ValueError("API manifest token entry is invalid")
    raw_root = _token_relative_root("raw", target)
    clean_root = _token_relative_root("clean", target)
    data_root = _token_relative_root("data", target)
    raw_files = _api_path_list(
        entry.get("raw_files"),
        expected_root=raw_root,
        description="API raw artifact",
    )
    clean_files = _api_path_list(
        entry.get("clean_files"),
        expected_root=clean_root,
        description="API clean artifact",
    )
    member_files = _api_path_list(
        entry.get("member_files"),
        expected_root=clean_root / "transfers",
        description="API member artifact",
    )
    token_file = validate_relative_artifact_path(Path(entry["token_file"]))
    if token_file != data_root / "token.json" or not set(member_files).issubset(clean_files):
        raise ValueError("API manifest token references are invalid")
    return {
        **entry,
        "token_file": str(token_file),
        "member_files": member_files,
        "raw_files": raw_files,
        "clean_files": clean_files,
    }


def _api_skipped_document(
    entry: object,
    *,
    targets: dict[str, list[str]],
) -> dict:
    if type(entry) is not dict or set(entry) != _API_SKIPPED_FIELDS:
        raise ValueError("API manifest skipped token shape is invalid")
    text_fields = (
        "requested_chain",
        "requested_token_address",
        "canonical_chain",
        "canonical_token_address",
        "stage",
        "reason",
        "captured_at",
        "status",
    )
    if any(type(entry.get(field)) is not str or not entry[field] for field in text_fields):
        raise ValueError("API manifest skipped token text field is invalid")
    if entry["requested_token_address"] not in targets.get(
        entry["requested_chain"],
        [],
    ):
        raise ValueError("API manifest skipped requested target is not selected")
    try:
        _require_canonical_utc(entry["captured_at"], "API skipped token captured_at")
        target = make_target(
            entry["requested_chain"],
            entry["requested_token_address"],
        )
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("API manifest skipped token identity is invalid") from None
    http_status = entry.get("http_status")
    reason = entry["reason"]
    unavailable_top_holders = (
        reason == "top_holders_not_available"
        and entry["stage"] == "holders"
        and http_status == 400
        and entry["attempt_count"] >= 1
    )
    capture_failed = (
        reason == "capture_failed"
        and entry["stage"] in {"holders", "subgraph", "snapshot", "final"}
        and (http_status is None or type(http_status) is int)
        and entry["attempt_count"] >= 0
    )
    if (
        entry["canonical_chain"] != target.chain
        or entry["canonical_token_address"] != target.token_address
        or type(entry.get("attempt_count")) is not int
        or entry["status"] != "skipped"
        or not (unavailable_top_holders or capture_failed)
    ):
        raise ValueError("API manifest skipped token entry is invalid")
    return {
        "requested_chain": entry["requested_chain"],
        "requested_token_address": entry["requested_token_address"],
        "canonical_chain": entry["canonical_chain"],
        "canonical_token_address": entry["canonical_token_address"],
        "stage": entry["stage"],
        "http_status": entry["http_status"],
        "attempt_count": entry["attempt_count"],
        "reason": entry["reason"],
        "captured_at": entry["captured_at"],
        "status": entry["status"],
    }


def _validate_api_identity_partition(
    targets: dict[str, list[str]],
    entries: list[dict],
    skipped_entries: list[dict],
) -> None:
    target_identities = {
        (target.chain, target.token_address)
        for chain, addresses in targets.items()
        for address in addresses
        for target in (make_target(chain, address),)
    }
    entry_identities = [
        (entry["canonical_chain"], entry["canonical_token_address"])
        for entry in entries
    ]
    skipped_identities = [
        (entry["canonical_chain"], entry["canonical_token_address"])
        for entry in skipped_entries
    ]
    success_set = set(entry_identities)
    skipped_set = set(skipped_identities)
    if len(success_set) != len(entry_identities):
        raise ValueError("API manifest success entries contain duplicate target identities")
    if len(skipped_set) != len(skipped_identities):
        raise ValueError("API manifest skipped entries contain duplicate target identities")
    if success_set & skipped_set:
        raise ValueError("API manifest success and skipped target identities overlap")
    if success_set | skipped_set != target_identities:
        raise ValueError(
            "API manifest success and skipped entries must partition the target list exactly"
        )


def _materialize_api_skipped_entries(
    entries: object,
    *,
    target_count: int,
) -> tuple[object, ...]:
    if type(target_count) is not int or target_count < 0:
        raise ValueError("API target count is invalid")
    values: list[object] = []
    try:
        iterator = iter(entries)
        for _index in range(target_count + 1):
            try:
                values.append(next(iterator))
            except StopIteration:
                return tuple(values)
    except Exception:
        raise ValueError(
            "API manifest skipped entries must be a finite sequence"
        ) from None
    raise ValueError(
        "API manifest skipped entries contain duplicate or too many targets; "
        "they must be a finite sequence"
    )


def _validate_api_error_report(
    report: object,
    *,
    targets: dict[str, list[str]] | None = None,
) -> list[dict]:
    if type(report) is not dict or set(report) != _API_ERROR_REPORT_FIELDS:
        raise ValueError("API error report shape is invalid")
    errors = report.get("errors")
    if (
        type(errors) is not list
        or not errors
        or type(report.get("error_count")) is not int
        or report["error_count"] != len(errors)
    ):
        raise ValueError("API error report count is invalid")
    validated: list[dict] = []
    for entry in errors:
        if (
            type(entry) is not dict
            or not _API_ERROR_REQUIRED_FIELDS.issubset(entry)
            or not set(entry).issubset(
                _API_ERROR_REQUIRED_FIELDS | _API_ERROR_OPTIONAL_FIELDS
            )
        ):
            raise ValueError("API error entry shape is invalid")
        if (
            type(entry["chain"]) is not str
            or type(entry["token_address"]) is not str
            or type(entry["stage"]) is not str
            or entry["stage"]
            not in {"holders", "subgraph", "transfers", "snapshot", "final"}
            or type(entry["type"]) is not str
            or not entry["type"]
            or type(entry["attempt_count"]) is not int
            or entry["attempt_count"] < 0
            or type(entry["captured_at"]) is not str
        ):
            raise ValueError("API error entry value is invalid")
        _require_canonical_utc(entry["captured_at"], "API error captured_at")
        target = make_target(entry["chain"], entry["token_address"])
        if (
            targets is not None
            and entry["token_address"] not in targets.get(target.chain, [])
        ):
            raise ValueError("API error target is not selected")
        member_address = entry.get("member_address")
        if member_address is not None:
            if entry["stage"] != "transfers":
                raise ValueError("API error member is only valid for transfers")
            if canonicalize_address(target.chain, member_address) != member_address:
                raise ValueError("API error member address is invalid")
        http_status = entry.get("http_status")
        if http_status is not None and (
            type(http_status) is not int or not 100 <= http_status <= 599
        ):
            raise ValueError("API error HTTP status is invalid")
        drift_fields = {
            "from_address",
            "to_address",
            "expected_count",
            "captured_count",
            "edge_last_date",
        }
        if entry["type"] in {
            "TransferSnapshotDrift",
            "TransferSubgraphOmission",
        }:
            if (
                entry["stage"] != "final"
                or entry["attempt_count"] != 0
                or set(entry) != _API_ERROR_REQUIRED_FIELDS | drift_fields
                or type(entry["from_address"]) is not str
                or type(entry["to_address"]) is not str
                or canonicalize_address(target.chain, entry["from_address"])
                != entry["from_address"]
                or canonicalize_address(target.chain, entry["to_address"])
                != entry["to_address"]
                or type(entry["expected_count"]) is not int
                or entry["expected_count"] < 0
                or type(entry["captured_count"]) is not int
                or entry["captured_count"] <= entry["expected_count"]
            ):
                raise ValueError("API transfer consistency warning is invalid")
            if entry["type"] == "TransferSnapshotDrift" and (
                entry["message"]
                != "new transfers captured after subgraph snapshot"
                or type(entry["edge_last_date"]) is not int
                or entry["edge_last_date"] < 0
            ):
                raise ValueError("API transfer snapshot drift is invalid")
            if entry["type"] == "TransferSubgraphOmission" and (
                entry["message"]
                != "transfer pair absent from subgraph response"
                or entry["expected_count"] != 0
                or entry["captured_count"] <= 0
                or entry["edge_last_date"] is not None
            ):
                raise ValueError("API transfer subgraph omission is invalid")
        elif entry["message"] != "capture failed" or drift_fields & set(entry):
            raise ValueError("API error entry value is invalid")
        validated.append(deepcopy(entry))
    _validate_public_artifact_payload(
        {"error_count": len(validated), "errors": validated},
        "API error report",
    )
    return validated


def write_error_report(staging: Path, errors: Sequence[dict]) -> str:
    """Write the validated non-sensitive error report for a partial generation."""
    staging = _trusted_api_staging_root(staging)
    try:
        values = [deepcopy(entry) for entry in errors]
    except (TypeError, ValueError, RecursionError):
        raise ValueError("API errors must be a finite sequence") from None
    report = {"error_count": len(values), "errors": values}
    _validate_api_error_report(report)
    relative = Path("error.json")
    _write_json(_destination(staging, relative), report)
    return str(relative)


def build_api_manifest(
    staging: Path,
    *,
    generation_id: str,
    business_date: date,
    captured_at: str,
    targets: dict[str, list[str]],
    entries: Sequence[dict],
    skipped_entries: Iterable[dict] = (),
    errors: Sequence[dict] = (),
) -> dict:
    """Build the exact v3 API generation manifest without historic DB provenance."""
    staging = _trusted_api_staging_root(staging)
    if type(generation_id) is not str or not generation_id:
        raise ValueError("generation_id must be non-empty text")
    if type(business_date) is not date:
        raise ValueError("business_date must be a native date")
    _require_canonical_utc(captured_at, "API manifest captured_at")
    validated_targets = _validate_api_targets(targets)
    staging = Path(staging)
    targets_path = _destination(staging, Path("targets.json"))
    try:
        saved_targets = _load_json_document(targets_path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise ValueError("API targets artifact cannot be read") from None
    if _validate_api_targets(saved_targets) != validated_targets:
        raise ValueError("API targets artifact does not match manifest targets")
    try:
        entry_values = tuple(entries)
    except TypeError:
        raise ValueError("API manifest entries must be a finite sequence") from None
    entry_documents = [
        _api_entry_document(entry, targets=validated_targets)
        for entry in entry_values
    ]
    skipped_values = _materialize_api_skipped_entries(
        skipped_entries,
        target_count=sum(len(addresses) for addresses in validated_targets.values()),
    )
    skipped_documents = [
        _api_skipped_document(entry, targets=validated_targets)
        for entry in skipped_values
    ]
    try:
        error_values = tuple(errors)
    except TypeError:
        raise ValueError("API errors must be a finite sequence") from None
    error_documents: list[dict] = []
    error_path = _destination(staging, Path("error.json"))
    if error_values:
        try:
            saved_error_report = _load_json_document(error_path)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise ValueError("API error report cannot be read") from None
        error_documents = _validate_api_error_report(
            saved_error_report,
            targets=validated_targets,
        )
        if error_documents != list(error_values):
            raise ValueError("API error report does not match manifest errors")
    elif error_path.exists():
        raise ValueError("API error report exists without errors")
    _validate_api_identity_partition(
        validated_targets,
        entry_documents,
        skipped_documents,
    )
    files = _regular_tree_files(staging, reject_empty_directories=True)
    if Path("manifest.json") in files:
        raise ValueError("API manifest must be built before its commit file exists")
    available = {str(path) for path in files}
    attributed = {"targets.json"}
    if error_documents:
        attributed.add("error.json")
    for entry in entry_documents:
        references = (
            entry["token_file"],
            *entry["raw_files"],
            *entry["clean_files"],
        )
        if any(reference not in available for reference in references):
            raise ValueError("API manifest references an absent artifact")
        for reference in references:
            if reference in attributed:
                raise ValueError("API manifest artifact references must be unique")
            attributed.add(reference)
    if attributed != available:
        raise ValueError("every API artifact must belong to a target entry")
    artifacts = {
        str(relative): {"sha256": hash_file_streaming(staging / relative)}
        for relative in sorted(files, key=str)
    }
    manifest = {
        "schema_version": "v3",
        "source": "bubblemaps_api",
        "generation_id": generation_id,
        "status": (
            "partial_success"
            if skipped_documents or error_documents
            else "success"
        ),
        "business_date": business_date.isoformat(),
        "timezone": "Asia/Shanghai",
        "captured_at": captured_at,
        "targets_file": "targets.json",
        "targets": validated_targets,
        "tokens": entry_documents,
        "skipped_tokens": skipped_documents,
        "artifacts": artifacts,
    }
    _validate_public_artifact_payload(manifest, "API manifest")
    return manifest


def write_success_manifest(staging: Path, manifest: dict) -> None:
    if type(manifest) is not dict:
        raise ValueError("success manifest must be an object")
    if (
        manifest.get("schema_version") == "v3"
        or manifest.get("source") == "bubblemaps_api"
    ):
        staging = _trusted_api_staging_root(staging)
    _write_json(
        _destination(Path(staging), Path("manifest.json")),
        manifest,
    )


def _load_json_document(path: Path) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"JSON constant {value!r} is not finite")

    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
            parse_int=_parse_unbounded_native_int,
            parse_float=str,
            parse_constant=reject_constant,
        )
    except RecursionError:
        raise ValueError("JSON document exceeds maximum nesting depth") from None

    pending = [(document, 0)]
    while pending:
        current, depth = pending.pop()
        if type(current) is dict:
            if depth > _MAX_JSON_NESTING_DEPTH:
                raise ValueError("JSON document exceeds maximum nesting depth")
            pending.extend((child, depth + 1) for child in current.values())
        elif type(current) is list:
            if depth > _MAX_JSON_NESTING_DEPTH:
                raise ValueError("JSON document exceeds maximum nesting depth")
            pending.extend((child, depth + 1) for child in current)
    return document


def _validate_api_manifest_shape(
    manifest: object,
    *,
    has_errors: bool = False,
) -> dict:
    if type(has_errors) is not bool:
        raise MarketGenerationValidationError("API error report state is invalid")
    if type(manifest) is not dict or set(manifest) != _API_MANIFEST_FIELDS:
        raise MarketGenerationValidationError("API generation manifest shape is invalid")
    try:
        _validate_public_artifact_payload(manifest, "API manifest")
    except ValueError:
        raise MarketGenerationValidationError(
            "API generation manifest contains unsafe data"
        ) from None
    if (
        manifest.get("schema_version") != "v3"
        or manifest.get("source") != "bubblemaps_api"
        or manifest.get("status") not in {"success", "partial_success"}
        or manifest.get("timezone") != "Asia/Shanghai"
        or type(manifest.get("generation_id")) is not str
        or not manifest["generation_id"]
        or type(manifest.get("business_date")) is not str
        or type(manifest.get("captured_at")) is not str
        or manifest.get("targets_file") != "targets.json"
    ):
        raise MarketGenerationValidationError("API generation is not a committed success")
    try:
        _require_canonical_utc(manifest["captured_at"], "API manifest captured_at")
        date.fromisoformat(manifest["business_date"])
        targets = _validate_api_targets(manifest.get("targets"))
        tokens = manifest.get("tokens")
        if type(tokens) is not list:
            raise ValueError("tokens must be a list")
        entries = [_api_entry_document(entry, targets=targets) for entry in tokens]
        skipped_tokens = manifest.get("skipped_tokens")
        if type(skipped_tokens) is not list:
            raise ValueError("skipped_tokens must be a list")
        skipped_entries = [
            _api_skipped_document(entry, targets=targets)
            for entry in skipped_tokens
        ]
        if (manifest["status"] == "success") != (
            not skipped_entries and not has_errors
        ):
            raise ValueError("API manifest status is inconsistent with partial content")
        _validate_api_identity_partition(targets, entries, skipped_entries)
    except (TypeError, ValueError, UnicodeError):
        raise MarketGenerationValidationError("API manifest content is invalid") from None
    artifacts = manifest.get("artifacts")
    if type(artifacts) is not dict:
        raise MarketGenerationValidationError("API manifest artifacts must be an object")
    return manifest


def _validate_api_raw_document(
    document: object,
    *,
    target: TargetToken,
    expected_kind: str,
    member_address: str | None = None,
) -> list:
    expected_fields = {
        "schema_version",
        "kind",
        "chain",
        "token_address",
        "canonical_chain",
        "canonical_token_address",
        "request",
        "payload",
    }
    if member_address is not None:
        expected_fields.add("member_address")
    if type(document) is not dict or set(document) != expected_fields:
        raise ValueError("raw API response fields are invalid")
    _validate_public_artifact_payload(document, "raw API response")
    _require_exact_identity(document, target)
    if document.get("schema_version") != "v3" or document.get("kind") != expected_kind:
        raise ValueError("raw API response contract is invalid")
    if member_address is not None and document.get("member_address") != member_address:
        raise ValueError("raw transfer response member is invalid")
    request = document.get("request")
    if (
        type(request) is not dict
        or set(request) != {"method", "url", "status", "attempts"}
        or type(request.get("method")) is not str
        or type(request.get("url")) is not str
        or type(request.get("status")) is not int
        or not 200 <= request["status"] < 300
        or type(request.get("attempts")) is not int
        or request["attempts"] <= 0
    ):
        raise ValueError("raw API request metadata is invalid")
    _validate_api_request_contract(
        request["method"],
        request["url"],
        target=target,
        expected_kind=expected_kind,
        member_address=member_address,
    )
    payload = document.get("payload")
    if type(payload) is not list:
        raise ValueError("raw API response payload must be a top-level list")
    return payload


def _validate_api_raw_set(
    staging: Path,
    *,
    target: TargetToken,
    raw_files: set[str],
    ordinary_addresses: set[str],
) -> tuple[list, list, dict[str, list]]:
    root = _token_relative_root("raw", target)
    expected = {
        str(root / "holders.json"),
        str(root / "subgraph.json"),
        *(
            str(root / "transfers" / f"{_safe_component(address)}.json")
            for address in ordinary_addresses
        ),
    }
    if raw_files != expected:
        raise ValueError("raw API artifacts do not match ordinary member roles")
    holder_payload = _validate_api_raw_document(
        _load_json_document(_destination(staging, root / "holders.json")),
        target=target,
        expected_kind="holders",
    )
    subgraph_payload = _validate_api_raw_document(
        _load_json_document(_destination(staging, root / "subgraph.json")),
        target=target,
        expected_kind="subgraph",
    )
    transfers_by_member: dict[str, list] = {}
    for address in sorted(ordinary_addresses):
        relative = root / "transfers" / f"{_safe_component(address)}.json"
        transfers_by_member[address] = _validate_api_raw_document(
            _load_json_document(_destination(staging, relative)),
            target=target,
            expected_kind="transfers",
            member_address=address,
        )
    return holder_payload, subgraph_payload, transfers_by_member


def _canonical_api_relationship_documents(
    edges: Sequence,
    target: TargetToken,
) -> list[dict]:
    documents: list[dict] = []
    for edge in edges:
        try:
            document = deepcopy(edge.raw)
            data = deepcopy(document["data"])
        except Exception:
            raise ValueError("filtered API relationship cannot be copied safely") from None
        if type(document) is not dict or type(data) is not dict:
            raise ValueError("filtered API relationship document is invalid")
        document["from_address"] = edge.from_address
        document["to_address"] = edge.to_address
        data["total_transfers"] = edge.total_transfers
        data["token_ref"] = {
            "chain": target.chain,
            "address": target.token_address,
        }
        document["data"] = data
        documents.append(document)
    return documents


def _validate_api_clean_set(
    staging: Path,
    *,
    target: TargetToken,
    clean_files: set[str],
    ordinary_addresses: set[str],
    summaries: dict[str, dict],
    member_documents: dict[str, dict],
    ranked_holder_count: int,
    raw_holder_payload: list,
    raw_subgraph_payload: list,
) -> None:
    root = _token_relative_root("clean", target)
    expected = {
        str(root / "holders.json"),
        str(root / "relationships.json"),
        *(
            str(root / "transfers" / f"{_safe_component(address)}.json")
            for address in ordinary_addresses
        ),
    }
    if clean_files != expected:
        raise ValueError("clean API artifacts do not match ordinary member roles")
    holders = _load_json_document(_destination(staging, root / "holders.json"))
    relationships = _load_json_document(
        _destination(staging, root / "relationships.json")
    )
    if type(holders) is not list or type(relationships) is not list:
        raise ValueError("clean API snapshots must be top-level lists")
    _validate_public_artifact_payload(holders, "clean holders")
    _validate_public_artifact_payload(relationships, "clean relationships")
    raw_ranked_holders = parse_ranked_holders(raw_holder_payload, target=target)
    ranked_holders = parse_ranked_holders(holders, target=target)
    if (
        ranked_holders != raw_ranked_holders
        or len(holders) != len(ranked_holders)
    ):
        raise ValueError("clean holders do not match normalized raw holders")
    if len(ranked_holders) != ranked_holder_count:
        raise ValueError("clean holder count does not match manifest")
    holder_index = {holder.address: holder for holder in ranked_holders}
    raw_edges = filter_subgraph_edges(
        raw_subgraph_payload,
        target=target,
        holders=holder_index,
    )
    clean_edges = filter_subgraph_edges(
        relationships,
        target=target,
        holders=holder_index,
    )
    if (
        len(relationships) != len(clean_edges)
        or _canonical_api_relationship_documents(raw_edges, target)
        != _canonical_api_relationship_documents(clean_edges, target)
    ):
        raise ValueError("clean relationships do not match filtered raw subgraph")
    _crosscheck_selected_holders(summaries, ranked_holders)
    expected_member_paths = {
        str(_api_clean_transfer_path(target, address))
        for address in ordinary_addresses
    }
    if expected_member_paths != set(clean_files) - {
        str(root / "holders.json"),
        str(root / "relationships.json"),
    }:
        raise ValueError("clean member transfer paths are invalid")
    if set(member_documents) != ordinary_addresses:
        raise ValueError("clean member document roles are invalid")


def _validate_api_token_entry_documents(staging: Path, entry: dict) -> None:
    target = make_target(
        entry["requested_chain"],
        entry["requested_token_address"],
    )
    token_relative = _token_relative_root("data", target) / "token.json"
    if entry["token_file"] != str(token_relative):
        raise ValueError("final token path is invalid")
    token_document = _load_json_document(_destination(staging, token_relative))
    (
        summaries,
        member_documents,
        ordinary_count,
        supernode_count,
        unique_transfer_count,
        transfer_view_count,
    ) = _validate_v3_token_and_member_documents(
        staging,
        target=target,
        token_document=token_document,
        captured_at=entry["captured_at"],
        prepare_member_documents=False,
    )
    assert type(token_document) is dict
    clusters = token_document["clusters"]
    assert type(clusters) is list
    ordinary_addresses = set(member_documents)
    expected_member_files = {
        str(_api_clean_transfer_path(target, address))
        for address in ordinary_addresses
    }
    if (
        set(entry["member_files"]) != expected_member_files
        or len(clusters) != entry["cluster_count"]
        or ordinary_count != entry["ordinary_member_count"]
        or supernode_count != entry["supernode_count"]
        or ordinary_count + supernode_count != entry["clustered_member_count"]
        or unique_transfer_count != entry["unique_transfer_count"]
        or transfer_view_count != entry["transfer_view_count"]
    ):
        raise ValueError("final token summary does not match manifest")
    holder_payload, subgraph_payload, raw_transfers_by_member = _validate_api_raw_set(
        staging,
        target=target,
        raw_files=set(entry["raw_files"]),
        ordinary_addresses=ordinary_addresses,
    )
    _validate_api_clean_set(
        staging,
        target=target,
        clean_files=set(entry["clean_files"]),
        ordinary_addresses=ordinary_addresses,
        summaries=summaries,
        member_documents=member_documents,
        ranked_holder_count=entry["ranked_holder_count"],
        raw_holder_payload=holder_payload,
        raw_subgraph_payload=subgraph_payload,
    )
    if type(holder_payload) is not list:
        raise ValueError("raw holder payload is invalid")
    raw_transfer_union = [
        transfer
        for payload in raw_transfers_by_member.values()
        for transfer in payload
    ]
    _require_formal_transfers_in_raw_union(
        member_documents,
        raw_transfer_union,
    )


def _validate_api_staging_generation(staging: Path) -> dict:
    staging = Path(staging)
    try:
        files = _regular_tree_files(staging, reject_empty_directories=True)
        if Path("manifest.json") not in files or Path("targets.json") not in files:
            raise MarketGenerationValidationError("API generation commit files are missing")
        has_errors = Path("error.json") in files
        manifest = _validate_api_manifest_shape(
            _load_json_document(_destination(staging, Path("manifest.json"))),
            has_errors=has_errors,
        )
        artifacts = manifest["artifacts"]
        assert type(artifacts) is dict
        actual_artifacts = {str(path) for path in files - {Path("manifest.json")}}
        if set(artifacts) != actual_artifacts:
            raise MarketGenerationValidationError("API artifact tree is not exact")
        for relative_text, metadata in artifacts.items():
            relative = validate_relative_artifact_path(Path(relative_text))
            if (
                type(metadata) is not dict
                or set(metadata) != {"sha256"}
                or type(metadata.get("sha256")) is not str
                or _SHA256_PATTERN.fullmatch(metadata["sha256"]) is None
                or hash_file_streaming(_destination(staging, relative)) != metadata["sha256"]
            ):
                raise MarketGenerationValidationError("API artifact hash metadata is invalid")
        targets = _validate_api_targets(
            _load_json_document(_destination(staging, Path("targets.json")))
        )
        if targets != manifest["targets"]:
            raise MarketGenerationValidationError("API targets artifact is inconsistent")
        attributed = {"targets.json"}
        if has_errors:
            _validate_api_error_report(
                _load_json_document(_destination(staging, Path("error.json"))),
                targets=targets,
            )
            attributed.add("error.json")
        for entry in manifest["tokens"]:
            assert type(entry) is dict
            references = (
                entry["token_file"],
                *entry["raw_files"],
                *entry["clean_files"],
            )
            if any(reference not in artifacts for reference in references):
                raise MarketGenerationValidationError("API token artifact reference is invalid")
            for reference in references:
                if reference in attributed:
                    raise MarketGenerationValidationError(
                        "API token artifact references are not unique"
                    )
                attributed.add(reference)
            _validate_api_token_entry_documents(staging, entry)
        if attributed != set(artifacts):
            raise MarketGenerationValidationError(
                "every committed API artifact must belong to one target entry"
            )
        return manifest
    except MarketGenerationValidationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise MarketGenerationValidationError(
            "Unable to validate Bubblemaps API generation"
        ) from None


def validate_staging_generation(staging: Path) -> dict:
    """Validate one committed v3 Bubblemaps API generation."""
    return _validate_api_staging_generation(staging)


def _close_lock_descriptor(descriptor: int) -> None:
    os.close(descriptor)


@contextmanager
def generation_lock(
    paths: RunPaths,
    *,
    shared: bool,
) -> AbstractContextManager[None]:
    if not isinstance(paths, RunPaths) or type(shared) is not bool:
        raise MarketGenerationLockError("Unable to acquire market generation lock")
    descriptor: int | None = None
    root_descriptor: int | None = None
    lock_parent_descriptor: int | None = None
    try:
        relatives = _run_relative_paths(paths)
        root_descriptor = _open_directory_path(paths.output_root, create=True)
        lock_parent_descriptor, lock_name = _open_relative_parent(
            root_descriptor,
            relatives["lock_file"],
            create=True,
        )
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            lock_name,
            flags,
            0o600,
            dir_fd=lock_parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("lock entry is not a regular file")
        os.fchmod(descriptor, 0o600)
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
    except BaseException as error:
        if descriptor is not None:
            try:
                _close_lock_descriptor(descriptor)
            except Exception:
                pass
        if isinstance(error, Exception):
            raise MarketGenerationLockError(
                "Unable to acquire market generation lock"
            ) from None
        raise
    finally:
        if lock_parent_descriptor is not None:
            os.close(lock_parent_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)

    body_error: BaseException | None = None
    try:
        yield
    except BaseException as error:
        body_error = error
        raise
    finally:
        release_errors: list[tuple[str, BaseException]] = []
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except BaseException as error:
            release_errors.append(("unlock", error))
        try:
            _close_lock_descriptor(descriptor)
        except BaseException as error:
            release_errors.append(("close", error))
        if release_errors:
            if body_error is not None:
                for operation, error in release_errors:
                    body_error.add_note(
                        f"market generation lock {operation} failure: {error!r}"
                    )
            elif all(isinstance(error, Exception) for _operation, error in release_errors):
                cleanup_error = MarketGenerationLockError(
                    "Unable to release market generation lock"
                )
                for operation, error in release_errors:
                    cleanup_error.add_note(f"{operation} failure: {error!r}")
                raise cleanup_error from None
            else:
                raise release_errors[0][1]


def read_validated_generation(
    output_root: Path,
    business_date: date,
) -> tuple[dict, list]:
    paths = RunPaths.create(output_root, business_date, "reader")
    with generation_lock(paths, shared=True):
        manifest = validate_staging_generation(paths.live)
        if manifest["business_date"] != business_date.isoformat():
            raise MarketGenerationValidationError(
                "Committed generation business date does not match directory"
            )
        error_path = paths.live / "error.json"
        if not error_path.exists():
            return manifest, []
        try:
            errors = _validate_api_error_report(
                _load_json_document(error_path),
                targets=manifest["targets"],
            )
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            raise MarketGenerationValidationError(
                "Unable to read committed API generation errors"
            ) from None
        return manifest, errors


def _sanitize_error_primitive(value: object) -> object:
    value_type = type(value)
    if value_type is str:
        if (
            _SENSITIVE_ASSIGNMENT.search(value) is not None
            or _BEARER_VALUE.search(value) is not None
        ):
            return "[REDACTED]"
        text = _BM_API_VALUE.sub("[REDACTED]", value)
        return _DATABASE_USERINFO.sub(r"\1[REDACTED]@", text)
    if value is None or value_type in (bool, int):
        return value
    if value_type is float and math.isfinite(value):
        return value
    return "[REDACTED]"


def _sanitize_error_record(value: object) -> dict:
    if type(value) is not dict:
        raise ValueError("failure error record must be an object")
    sanitized: dict = {}
    for key, child in value.items():
        if type(key) is not str:
            raise ValueError("failure error keys must be native strings")
        if key not in _SAFE_FAILURE_ERROR_FIELDS:
            continue
        if key == "message":
            sanitized[key] = (
                child
                if type(child) is str and child in _SAFE_FAILURE_MESSAGES
                else "[REDACTED]"
            )
            continue
        if key == "member_substage":
            if type(child) is str and child in MEMBER_SUBSTAGES:
                sanitized[key] = child
            continue
        sanitized[key] = _sanitize_error_primitive(child)
    return sanitized


def preserve_failed_run(paths: RunPaths, error_record: dict) -> None:
    if not isinstance(paths, RunPaths) or type(error_record) is not dict:
        raise ValueError("failed-run preservation requires paths and an error object")
    relatives = _run_relative_paths(paths)
    root_descriptor = _open_directory_path(paths.output_root, create=True)
    try:
        _ensure_relative_directory(root_descriptor, relatives["staging"])
        staging_metadata = _require_real_directory_relative(
            root_descriptor,
            relatives["staging"],
        )
        staging_identity = _entry_identity(staging_metadata)
        for commit_file in ("manifest.json", "errors.json"):
            _unlink_relative(
                root_descriptor,
                relatives["staging"] / commit_file,
                missing_ok=True,
            )

        data_relative = relatives["staging"] / "data"
        data_metadata = _stat_relative(root_descriptor, data_relative)
        if data_metadata is not None:
            if not stat.S_ISDIR(data_metadata.st_mode):
                raise ValueError("failed formal data must be a real directory")
            diagnostic_relative = relatives["staging"] / "diagnostic"
            _ensure_relative_directory(root_descriptor, diagnostic_relative)
            destination = diagnostic_relative / "data"
            if _stat_relative(root_descriptor, destination) is not None:
                raise ValueError(
                    "failed diagnostic data destination already exists"
                )
            _rename_relative(
                root_descriptor,
                data_relative,
                destination,
                expected_source_identity=_entry_identity(data_metadata),
            )
            _fsync_rename_parents(
                root_descriptor,
                data_relative,
                destination,
            )

        sanitized = _sanitize_error_record(error_record)
        _write_bytes_relative_secure(
            root_descriptor,
            relatives["staging"] / "error.json",
            _json_text(sanitized).encode("ascii"),
        )
        failed_day = relatives["failed"].parent
        _ensure_relative_directory(root_descriptor, failed_day)
        destination = _unique_relative_destination(
            root_descriptor,
            relatives["failed"],
        )
        _rename_relative(
            root_descriptor,
            relatives["staging"],
            destination,
            expected_source_identity=staging_identity,
        )
        _fsync_rename_parents(
            root_descriptor,
            relatives["staging"],
            destination,
        )
        _fsync_output_root(root_descriptor)
    finally:
        os.close(root_descriptor)


def _validated_recovery_tree(directory: Path, business_date: date) -> dict:
    try:
        manifest = validate_staging_generation(directory)
    except MarketGenerationValidationError:
        raise PublicationRecoveryError(
            "Publication recovery found an invalid generation tree"
        ) from None
    if manifest["business_date"] != business_date.isoformat():
        raise PublicationRecoveryError(
            "Publication recovery generation date does not match"
        )
    return manifest


def recover_interrupted_publish(paths: RunPaths) -> None:
    if not isinstance(paths, RunPaths):
        raise PublicationRecoveryError("Publication recovery paths are invalid")
    try:
        relatives = _run_relative_paths(paths)
        root_descriptor = _open_directory_path(paths.output_root, create=True)
    except (OSError, ValueError):
        raise PublicationRecoveryError("Publication recovery paths are invalid") from None
    try:
        _best_effort_cleanup_trash(root_descriptor, relatives["trash_day"])
        try:
            entries = _list_real_directory_entries(
                root_descriptor,
                relatives["backup_day"],
            )
        except OSError:
            raise PublicationRecoveryError(
                "Unable to inspect publication backups"
            ) from None
        if not entries:
            return
        if len(entries) != 1:
            raise PublicationRecoveryError(
                "Publication recovery requires exactly one real backup generation"
            )

        backup_name, backup_metadata = entries[0]
        backup_relative = relatives["backup_day"] / backup_name
        backup_identity = _entry_identity(backup_metadata)
        _validated_recovery_tree(
            paths.output_root / backup_relative,
            paths.business_date,
        )
        try:
            _require_real_directory_relative(
                root_descriptor,
                backup_relative,
                expected_identity=backup_identity,
            )
        except OSError:
            raise PublicationRecoveryError(
                "Publication backup changed during validation"
            ) from None

        live_metadata = _stat_relative(root_descriptor, relatives["live"])
        if live_metadata is not None:
            if not stat.S_ISDIR(live_metadata.st_mode):
                raise PublicationRecoveryError("Live publication tree is invalid")
            live_identity = _entry_identity(live_metadata)
            _validated_recovery_tree(paths.live, paths.business_date)
            try:
                _require_real_directory_relative(
                    root_descriptor,
                    relatives["live"],
                    expected_identity=live_identity,
                )
                trash_relative, trash_identity = _move_to_trash(
                    root_descriptor,
                    source=backup_relative,
                    trash_day=relatives["trash_day"],
                    expected_source_identity=backup_identity,
                )
                _fsync_output_root(root_descriptor)
            except OSError:
                raise PublicationRecoveryError(
                    "Unable to retire superseded publication backup"
                ) from None
            try:
                _safe_rmtree_relative(
                    root_descriptor,
                    trash_relative,
                    expected_identity=trash_identity,
                )
            except OSError:
                pass
            return

        activated = False
        try:
            _rename_relative(
                root_descriptor,
                backup_relative,
                relatives["live"],
                expected_source_identity=backup_identity,
            )
            activated = True
            _fsync_rename_parents(
                root_descriptor,
                backup_relative,
                relatives["live"],
            )
            _fsync_output_root(root_descriptor)
        except BaseException as error:
            rollback_error: BaseException | None = None
            if activated:
                try:
                    _rename_relative(
                        root_descriptor,
                        relatives["live"],
                        backup_relative,
                        expected_source_identity=backup_identity,
                    )
                    activated = False
                    _fsync_rename_parents(
                        root_descriptor,
                        relatives["live"],
                        backup_relative,
                    )
                except BaseException as rollback_failure:
                    rollback_error = rollback_failure
            if rollback_error is not None:
                raise PublicationRecoveryError(
                    "Publication recovery failed and could not roll back"
                ) from error
            if isinstance(error, Exception):
                raise PublicationRecoveryError(
                    "Unable to restore interrupted publication backup"
                ) from None
            raise
    finally:
        os.close(root_descriptor)


def publish_success(paths: RunPaths) -> None:
    if not isinstance(paths, RunPaths):
        raise ValueError("publication paths must be RunPaths")
    relatives = _run_relative_paths(paths)
    root_descriptor = _open_directory_path(paths.output_root, create=True)
    try:
        _best_effort_cleanup_trash(root_descriptor, relatives["trash_day"])
        try:
            staging_metadata = _require_real_directory_relative(
                root_descriptor,
                relatives["staging"],
            )
        except OSError:
            raise MarketGenerationValidationError(
                "Staging generation is not a real directory"
            ) from None
        staging_identity = _entry_identity(staging_metadata)
        staging_manifest = validate_staging_generation(paths.staging)
        try:
            _require_real_directory_relative(
                root_descriptor,
                relatives["staging"],
                expected_identity=staging_identity,
            )
        except OSError:
            raise MarketGenerationValidationError(
                "Staging generation changed during validation"
            ) from None
        if (
            staging_manifest["business_date"] != paths.business_date.isoformat()
            or staging_manifest["generation_id"] != paths.generation_id
        ):
            raise MarketGenerationValidationError(
                "Staging generation identity does not match publication paths"
            )

        live_metadata = _stat_relative(root_descriptor, relatives["live"])
        old_live_exists = live_metadata is not None
        old_live_identity: tuple[int, int] | None = None
        if live_metadata is not None:
            if not stat.S_ISDIR(live_metadata.st_mode):
                raise MarketGenerationValidationError("Existing live tree is invalid")
            old_live_identity = _entry_identity(live_metadata)
            live_manifest = validate_staging_generation(paths.live)
            try:
                _require_real_directory_relative(
                    root_descriptor,
                    relatives["live"],
                    expected_identity=old_live_identity,
                )
            except OSError:
                raise MarketGenerationValidationError(
                    "Existing live tree changed during validation"
                ) from None
            if live_manifest["business_date"] != paths.business_date.isoformat():
                raise MarketGenerationValidationError(
                    "Existing live generation date does not match"
                )

        if _stat_relative(root_descriptor, relatives["backup"]) is not None:
            raise PublicationRecoveryError(
                "Publication backup already exists; recover before publishing"
            )
        try:
            _ensure_relative_directory(root_descriptor, relatives["backup"].parent)
            _ensure_relative_directory(root_descriptor, relatives["trash_day"])
        except OSError:
            raise PublicationRecoveryError(
                "Publication transient roots are invalid"
            ) from None

        activation_manifest = validate_staging_generation(paths.staging)
        if (
            activation_manifest["business_date"]
            != paths.business_date.isoformat()
            or activation_manifest["generation_id"] != paths.generation_id
        ):
            raise MarketGenerationValidationError(
                "Staging generation identity changed before activation"
            )
        try:
            _require_real_directory_relative(
                root_descriptor,
                relatives["staging"],
                expected_identity=staging_identity,
            )
        except OSError:
            raise MarketGenerationValidationError(
                "Staging generation changed before activation"
            ) from None

        old_moved = False
        new_activated = False
        committed = False
        try:
            if old_live_exists:
                assert old_live_identity is not None
                _rename_relative(
                    root_descriptor,
                    relatives["live"],
                    relatives["backup"],
                    expected_source_identity=old_live_identity,
                )
                old_moved = True
                _fsync_rename_parents(
                    root_descriptor,
                    relatives["live"],
                    relatives["backup"],
                )
            _rename_relative(
                root_descriptor,
                relatives["staging"],
                relatives["live"],
                expected_source_identity=staging_identity,
            )
            new_activated = True
            _fsync_rename_parents(
                root_descriptor,
                relatives["staging"],
                relatives["live"],
            )
            activated_manifest = validate_staging_generation(paths.live)
            if (
                activated_manifest["business_date"]
                != paths.business_date.isoformat()
                or activated_manifest["generation_id"] != paths.generation_id
            ):
                raise MarketGenerationValidationError(
                    "Activated generation identity is invalid"
                )
            _require_real_directory_relative(
                root_descriptor,
                relatives["live"],
                expected_identity=staging_identity,
            )
            _fsync_output_root(root_descriptor)
            committed = True
        except BaseException as error:
            rollback_errors: list[BaseException] = []
            if new_activated:
                try:
                    _rename_relative(
                        root_descriptor,
                        relatives["live"],
                        relatives["staging"],
                        expected_source_identity=staging_identity,
                    )
                    new_activated = False
                    _fsync_rename_parents(
                        root_descriptor,
                        relatives["live"],
                        relatives["staging"],
                    )
                except BaseException as rollback_error:
                    rollback_errors.append(rollback_error)
            if old_moved:
                try:
                    assert old_live_identity is not None
                    _rename_relative(
                        root_descriptor,
                        relatives["backup"],
                        relatives["live"],
                        expected_source_identity=old_live_identity,
                    )
                    old_moved = False
                    _fsync_rename_parents(
                        root_descriptor,
                        relatives["backup"],
                        relatives["live"],
                    )
                except BaseException as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                recovery_error = PublicationRecoveryError(
                    "Publication failed and the previous live tree could not be restored"
                )
                for rollback_error in rollback_errors:
                    recovery_error.add_note(f"rollback failure: {rollback_error!r}")
                raise recovery_error from error
            raise

        if committed and old_moved:
            assert old_live_identity is not None
            try:
                trash_relative, trash_identity = _move_to_trash(
                    root_descriptor,
                    source=relatives["backup"],
                    trash_day=relatives["trash_day"],
                    expected_source_identity=old_live_identity,
                )
                _fsync_output_root(root_descriptor)
            except Exception:
                return
            try:
                _safe_rmtree_relative(
                    root_descriptor,
                    trash_relative,
                    expected_identity=trash_identity,
                )
            except Exception:
                pass
    finally:
        os.close(root_descriptor)
