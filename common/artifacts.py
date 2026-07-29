import errno
import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile


_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = {
    errno.EINVAL,
    errno.ENOSYS,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return

    file_descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    sync_error: BaseException | None = None
    try:
        try:
            os.fsync(file_descriptor)
        except OSError as error:
            if error.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
                raise
    except BaseException as error:
        sync_error = error
        raise
    finally:
        try:
            os.close(file_descriptor)
        except Exception as close_error:
            if sync_error is None:
                raise
            sync_error.add_note(
                f"Directory close failure after fsync: {close_error!r}"
            )


def output_directory(root: Path, day: date) -> Path:
    return root / day.strftime("%Y-%m-%d")


def safe_path_component(value: str) -> str:
    normalized = (
        re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "value"
    )
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{normalized[:80]}-{digest}"


def write_json_atomic(path: Path, payload: object) -> None:
    """Atomically replace ``path`` and sync the committed JSON entry.

    The temporary file is fsynced before replacement, and the destination
    directory is fsynced afterward where supported. A directory-sync failure
    can therefore be raised after ``path`` already contains the new payload.
    Newly created ancestor directory entries are not synced, so this function
    does not guarantee their persistence across a crash.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    primary_error: BaseException | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except Exception as cleanup_error:
                if primary_error is None:
                    raise
                primary_error.add_note(
                    "Temporary-file cleanup failure for "
                    f"{temporary_path}: {cleanup_error!r}"
                )

