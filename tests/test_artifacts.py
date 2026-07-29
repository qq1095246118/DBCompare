import errno
import hashlib
import stat
from datetime import date, datetime

import pytest

import common.artifacts as artifacts
from common.artifacts import (
    output_directory,
    safe_path_component,
    write_json_atomic,
)


def test_output_directory_uses_exact_iso_date(tmp_path) -> None:
    assert output_directory(tmp_path, date(2026, 7, 21)) == (
        tmp_path / "2026-07-21"
    )


def test_output_directory_uses_date_only_for_datetime_input(tmp_path) -> None:
    assert output_directory(tmp_path, datetime(2026, 7, 21, 15, 30)) == (
        tmp_path / "2026-07-21"
    )


def test_safe_path_component_is_deterministic_and_retains_safe_ascii() -> None:
    value = "Alpha-9.v_2"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

    result = safe_path_component(value)

    assert result == f"{value}-{digest}"
    assert safe_path_component(value) == result


def test_safe_path_component_distinguishes_inputs_that_normalize_alike() -> None:
    first_value = "same/?name"
    second_value = "same name"
    first_digest = hashlib.sha256(first_value.encode("utf-8")).hexdigest()[:12]
    second_digest = hashlib.sha256(second_value.encode("utf-8")).hexdigest()[:12]

    first = safe_path_component(first_value)
    second = safe_path_component(second_value)

    assert first == f"same_name-{first_digest}"
    assert second == f"same_name-{second_digest}"
    assert first != second


def test_safe_path_component_strips_leading_and_trailing_dot_underscore() -> None:
    value = "..__name.__"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

    assert safe_path_component(value) == f"name-{digest}"


@pytest.mark.parametrize("value", ["", " /中文:? "])
def test_safe_path_component_falls_back_for_empty_or_only_unsafe(value) -> None:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

    assert safe_path_component(value) == f"value-{digest}"


def test_safe_path_component_limits_readable_prefix_to_eighty_characters() -> None:
    value = "a" * 81
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

    assert safe_path_component(value) == f"{'a' * 80}-{digest}"


def test_write_json_atomic_creates_parents_and_writes_stable_utf8_lf(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "nested" / "result.json"
    real_named_temporary_file = artifacts.NamedTemporaryFile
    newline_arguments = []

    def recording_named_temporary_file(*args, **kwargs):
        newline_arguments.append(kwargs.get("newline"))
        return real_named_temporary_file(*args, **kwargs)

    monkeypatch.setattr(
        artifacts,
        "NamedTemporaryFile",
        recording_named_temporary_file,
    )

    write_json_atomic(destination, {"z": 1, "a": "雪"})

    assert destination.read_bytes() == (
        '{\n  "a": "雪",\n  "z": 1\n}\n'.encode("utf-8")
    )
    assert newline_arguments == ["\n"]


def test_write_json_atomic_replaces_existing_file_without_temp_leftovers(
    tmp_path,
) -> None:
    destination = tmp_path / "result.json"
    destination.write_text('{"old": true}\n', encoding="utf-8")

    write_json_atomic(destination, {"new": True})

    assert destination.read_text(encoding="utf-8") == '{\n  "new": true\n}\n'
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_json_atomic_preserves_existing_file_on_serialization_failure(
    tmp_path,
) -> None:
    destination = tmp_path / "result.json"
    previous = b'{"stable": true}\n'
    destination.write_bytes(previous)

    with pytest.raises(TypeError):
        write_json_atomic(destination, {"not_json": object()})

    assert destination.read_bytes() == previous
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_json_atomic_rejects_nan_without_replacing_destination(
    tmp_path,
) -> None:
    destination = tmp_path / "result.json"
    previous = b'{"stable": true}\n'
    destination.write_bytes(previous)

    with pytest.raises(ValueError, match="Out of range float values"):
        write_json_atomic(destination, {"not_standard_json": float("nan")})

    assert destination.read_bytes() == previous
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(
    artifacts.os.name == "nt",
    reason="Windows does not support opening directories for fsync",
)
def test_write_json_atomic_syncs_destination_directory(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "result.json"
    real_fsync = artifacts.os.fsync
    synced_directories = []

    def recording_fsync(file_descriptor):
        metadata = artifacts.os.fstat(file_descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            synced_directories.append((metadata.st_dev, metadata.st_ino))
        return real_fsync(file_descriptor)

    monkeypatch.setattr(artifacts.os, "fsync", recording_fsync)

    write_json_atomic(destination, {"complete": True})

    directory_metadata = tmp_path.stat()
    assert synced_directories == [
        (directory_metadata.st_dev, directory_metadata.st_ino)
    ]


@pytest.mark.skipif(
    artifacts.os.name == "nt",
    reason="Windows skips directory fsync",
)
def test_write_json_atomic_syncs_file_before_replace_and_directory_after(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "result.json"
    real_fsync = artifacts.os.fsync
    real_replace = artifacts.os.replace
    events = []

    def recording_fsync(file_descriptor):
        metadata = artifacts.os.fstat(file_descriptor)
        events.append(
            "directory_fsync"
            if stat.S_ISDIR(metadata.st_mode)
            else "file_fsync"
        )
        return real_fsync(file_descriptor)

    def recording_replace(source, target):
        events.append("replace")
        return real_replace(source, target)

    monkeypatch.setattr(artifacts.os, "fsync", recording_fsync)
    monkeypatch.setattr(artifacts.os, "replace", recording_replace)

    write_json_atomic(destination, {"complete": True})

    assert events == ["file_fsync", "replace", "directory_fsync"]


@pytest.mark.skipif(
    artifacts.os.name == "nt",
    reason="Windows skips directory fsync",
)
def test_directory_fsync_preserves_sync_error_when_close_also_fails(
    tmp_path,
    monkeypatch,
) -> None:
    real_close = artifacts.os.close

    def failing_fsync(file_descriptor):
        raise OSError(errno.EIO, "directory fsync failed")

    def failing_close(file_descriptor):
        real_close(file_descriptor)
        raise OSError(errno.EBADF, "directory close failed")

    monkeypatch.setattr(artifacts.os, "fsync", failing_fsync)
    monkeypatch.setattr(artifacts.os, "close", failing_close)

    with pytest.raises(OSError) as raised:
        artifacts._fsync_directory(tmp_path)

    assert raised.value.errno == errno.EIO
    assert any(
        "close failure" in note
        for note in getattr(raised.value, "__notes__", [])
    )


@pytest.mark.skipif(
    artifacts.os.name == "nt",
    reason="Windows skips directory fsync",
)
@pytest.mark.parametrize(
    "error_number",
    sorted(
        {
            errno.EINVAL,
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
        }
    ),
)
def test_write_json_atomic_skips_unsupported_directory_fsync_errors(
    tmp_path,
    monkeypatch,
    error_number,
) -> None:
    destination = tmp_path / "result.json"
    real_fsync = artifacts.os.fsync

    def unsupported_directory_fsync(file_descriptor):
        metadata = artifacts.os.fstat(file_descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            raise OSError(error_number, "directory fsync unsupported")
        return real_fsync(file_descriptor)

    monkeypatch.setattr(artifacts.os, "fsync", unsupported_directory_fsync)

    write_json_atomic(destination, {"complete": True})

    assert destination.read_bytes() == b'{\n  "complete": true\n}\n'


@pytest.mark.skipif(
    artifacts.os.name == "nt",
    reason="Windows skips directory fsync",
)
def test_write_json_atomic_propagates_unrelated_directory_fsync_error(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "result.json"
    real_fsync = artifacts.os.fsync

    def failing_directory_fsync(file_descriptor):
        metadata = artifacts.os.fstat(file_descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            raise OSError(errno.EIO, "directory fsync failed")
        return real_fsync(file_descriptor)

    monkeypatch.setattr(artifacts.os, "fsync", failing_directory_fsync)

    with pytest.raises(OSError) as raised:
        write_json_atomic(destination, {"complete": True})

    assert raised.value.errno == errno.EIO
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_json_atomic_preserves_primary_error_when_cleanup_fails(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "result.json"
    previous = b'{"stable": true}\n'
    destination.write_bytes(previous)
    real_unlink = artifacts.Path.unlink

    def failing_temp_unlink(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise PermissionError(
                errno.EACCES,
                "forced temporary-file cleanup failure",
                path,
            )
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(artifacts.Path, "unlink", failing_temp_unlink)

    with pytest.raises(TypeError, match="not JSON serializable") as raised:
        write_json_atomic(destination, {"not_json": object()})

    assert destination.read_bytes() == previous
    assert any(
        "cleanup failure" in note
        for note in getattr(raised.value, "__notes__", [])
    )
    leftovers = list(tmp_path.glob("*.tmp"))
    assert len(leftovers) == 1
    artifacts.os.unlink(leftovers[0])


def test_write_json_atomic_preserves_destination_when_replace_fails(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "result.json"
    previous = b'{"stable": true}\n'
    destination.write_bytes(previous)

    def failing_replace(source, target):
        raise OSError(errno.EIO, f"cannot replace {source} with {target}")

    monkeypatch.setattr(artifacts.os, "replace", failing_replace)

    with pytest.raises(OSError) as raised:
        write_json_atomic(destination, {"replacement": True})

    assert raised.value.errno == errno.EIO
    assert destination.read_bytes() == previous
    assert list(tmp_path.glob("*.tmp")) == []
