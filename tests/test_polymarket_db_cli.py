import hashlib
import json
from datetime import date, datetime, timezone

import pytest

from common.artifacts import write_json_atomic
from getDB.Polymarket.tool.contract import build_db_output
from getDB.Polymarket.tool import export_polymarket_db as cli


DAY = date(2026, 7, 29)
LOWER = datetime(2026, 7, 28, 16, tzinfo=timezone.utc)
UPPER = datetime(2026, 7, 29, 16, tzinfo=timezone.utc)
CAPTURED = datetime(2026, 7, 29, 8, 30, tzinfo=timezone.utc)
GENERATION_ID = "00000000-0000-4000-8000-000000000001"


def db_row(*, row_id=1, category="politics", rank=1, content=None):
    business = (
        {"category": category, "market_id": f"market-{row_id}", "rank": rank}
        if content is None
        else content
    )
    return {
        "id": row_id,
        "data_type": "PREDICTION_MARKET_SELECTION",
        "title": f"Title {row_id}",
        "summary": None,
        "content": json.dumps(business) if isinstance(business, dict) else business,
        "from_source": "polymarket",
        "source_url": None,
        "content_hash": f"hash-{row_id}",
        "extra_data": {"market_id": f"market-{row_id}"},
        "published_at": CAPTURED,
        "created_at": CAPTURED,
        "updated_at": CAPTURED,
        "tags": ["prediction-market"],
        "source_updated_at": None,
    }


def generation_payload():
    primary, errors, counts = build_db_output(
        [db_row(row_id=1), db_row(row_id=2, category="finance")]
    )
    manifest = cli._manifest(
        business_date=DAY,
        lower=LOWER,
        upper=UPPER,
        generation_id=GENERATION_ID,
        captured_at=CAPTURED,
        status="success",
        source_row_count=2,
        record_count=2,
        error_count=0,
        category_counts=counts,
    )
    return primary, errors, manifest


def install_source(monkeypatch, rows=None, error=None):
    settings = cli.PgSettings(
        "host-secret", 15432, "database-secret", "user-secret", "password-secret"
    )
    monkeypatch.setattr(cli, "load_pg_settings", lambda: settings)
    if error is None:
        monkeypatch.setattr(cli, "fetch_day_rows", lambda *_args: list(rows or []))
    else:
        monkeypatch.setattr(
            cli, "fetch_day_rows", lambda *_args: (_ for _ in ()).throw(error)
        )
    monkeypatch.setattr(cli, "_new_generation_id", lambda: GENERATION_ID)
    monkeypatch.setattr(cli, "_utc_now", lambda: CAPTURED)


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


def test_write_and_read_validated_generation(tmp_path):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    assert cli._write_artifacts(directory, primary, manifest, errors) is True
    actual_primary, actual_errors, actual_manifest = cli.read_validated_generation(directory)
    assert actual_primary == primary
    assert actual_errors == []
    assert actual_manifest["status"] == "success"
    assert set(actual_manifest["artifacts"]) == {"polymarket_db.json", "errors.json"}
    for filename, metadata in actual_manifest["artifacts"].items():
        assert metadata["sha256"] == hashlib.sha256(
            (directory / filename).read_bytes()
        ).hexdigest()


def test_validated_reader_rejects_tampered_primary(tmp_path):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    assert cli._write_artifacts(directory, primary, manifest, errors) is True
    write_json_atomic(directory / "polymarket_db.json", {"records": []})
    with pytest.raises(cli.GenerationValidationError):
        cli.read_validated_generation(directory)


def test_validated_reader_rejects_manifest_count_mismatch(tmp_path):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    assert cli._write_artifacts(directory, primary, manifest, errors) is True
    committed = json.loads((directory / "manifest.json").read_text())
    committed["record_count"] = 99
    write_json_atomic(directory / "manifest.json", committed)
    with pytest.raises(cli.GenerationValidationError):
        cli.read_validated_generation(directory)


def test_validated_reader_uses_a_shared_lock(tmp_path, monkeypatch):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    assert cli._write_artifacts(directory, primary, manifest, errors) is True
    operations = []
    monkeypatch.setattr(cli.fcntl, "flock", lambda _fd, operation: operations.append(operation))
    cli.read_validated_generation(directory)
    assert operations == [cli.fcntl.LOCK_SH, cli.fcntl.LOCK_UN]


def test_artifact_failure_never_leaves_a_valid_final_manifest(tmp_path, monkeypatch):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    real_write = cli.write_json_atomic

    def fail_primary(path, payload):
        if path.name == "polymarket_db.json":
            raise OSError("write-sentinel")
        real_write(path, payload)

    monkeypatch.setattr(cli, "write_json_atomic", fail_primary)
    assert cli._write_artifacts(directory, primary, manifest, errors) is False
    assert cli.validate_manifest_artifacts(directory) is False
    if (directory / "manifest.json").exists():
        assert json.loads((directory / "manifest.json").read_text())["status"] == "in_progress"


def test_final_manifest_failure_restores_an_uncommitted_marker(tmp_path, monkeypatch):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    real_write = cli.write_json_atomic

    def fail_final_manifest(path, payload):
        if path.name == "manifest.json" and payload.get("status") == "success":
            raise OSError("manifest-sentinel")
        real_write(path, payload)

    monkeypatch.setattr(cli, "write_json_atomic", fail_final_manifest)
    assert cli._write_artifacts(directory, primary, manifest, errors) is False
    assert cli.validate_manifest_artifacts(directory) is False
    assert json.loads((directory / "manifest.json").read_text())["status"] == "in_progress"


def committed_directory(tmp_path):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    assert cli._write_artifacts(directory, primary, manifest, errors) is True
    return directory


def rewrite_manifest(directory, mutate):
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    write_json_atomic(path, manifest)


def rewrite_hashed_json(directory, filename, mutate):
    path = directory / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    write_json_atomic(path, payload)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][filename]["sha256"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    write_json_atomic(manifest_path, manifest)


def remove_dataset(manifest):
    manifest.pop("dataset")


def add_manifest_field(manifest):
    manifest["unexpected"] = True


def remove_artifact(manifest):
    manifest["artifacts"].pop("errors.json")


def add_artifact(manifest):
    manifest["artifacts"]["extra.json"] = {"sha256": "0" * 64}


@pytest.mark.parametrize(
    "mutate",
    [
        remove_dataset,
        add_manifest_field,
        lambda value: value.__setitem__("generation_id", "not-a-uuid"),
        lambda value: value.__setitem__("business_date", "2026-07-28"),
        lambda value: value.__setitem__("business_date", "20260729"),
        lambda value: value.__setitem__("utc_lower_bound", "2026-07-28T15:00:00+00:00"),
        lambda value: value.__setitem__("captured_at", "2026-07-29T16:30:00+08:00"),
        lambda value: value.__setitem__("record_count", True),
        lambda value: value.__setitem__("category_counts", {"politics": 99}),
        lambda value: value.__setitem__("status", "partial"),
        lambda value: value.__setitem__("source_row_count", 3),
        remove_artifact,
        add_artifact,
        lambda value: value["artifacts"]["errors.json"].__setitem__("sha256", "ABC"),
    ],
)
def test_validated_reader_rejects_each_manifest_invariant(tmp_path, mutate):
    directory = committed_directory(tmp_path)
    rewrite_manifest(directory, mutate)
    with pytest.raises(cli.GenerationValidationError):
        cli.read_validated_generation(directory)


@pytest.mark.parametrize(
    ("filename", "mutate"),
    [
        ("polymarket_db.json", lambda value: value.__setitem__("extra", True)),
        ("polymarket_db.json", lambda value: value["records"][0].__setitem__("extra", True)),
        ("polymarket_db.json", lambda value: value["records"][0].__setitem__(
            "created_at", "2026-07-29T08:30:00+00:00")),
        ("polymarket_db.json", lambda value: value["records"][0].__setitem__(
            "tags", "not-an-array")),
        ("errors.json", lambda value: value.append({"stage": "row_validation"})),
    ],
)
def test_validated_reader_rejects_each_payload_invariant(tmp_path, filename, mutate):
    directory = committed_directory(tmp_path)
    rewrite_hashed_json(directory, filename, mutate)
    with pytest.raises(cli.GenerationValidationError):
        cli.read_validated_generation(directory)


def test_validated_reader_rejects_non_array_errors(tmp_path):
    directory = committed_directory(tmp_path)
    write_json_atomic(directory / "errors.json", {"error": "wrong shape"})
    rewrite_manifest(
        directory,
        lambda value: value["artifacts"]["errors.json"].__setitem__(
            "sha256", hashlib.sha256((directory / "errors.json").read_bytes()).hexdigest()
        ),
    )
    with pytest.raises(cli.GenerationValidationError):
        cli.read_validated_generation(directory)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda error: error.__setitem__("message", "database-secret"),
        lambda error: error.__setitem__("type", "PrivateConnectorFailure"),
        lambda error: error.__setitem__("unexpected", True),
    ],
)
def test_validated_reader_rejects_unsanitized_source_errors(tmp_path, mutate):
    directory = tmp_path / DAY.isoformat()
    primary = {"records": []}
    errors = [cli._source_error(RuntimeError("private"))]
    manifest = cli._manifest(
        business_date=DAY,
        lower=LOWER,
        upper=UPPER,
        generation_id=GENERATION_ID,
        captured_at=CAPTURED,
        status="failed",
        source_row_count=None,
        record_count=0,
        error_count=1,
        category_counts={},
    )
    assert cli._write_artifacts(directory, primary, manifest, errors) is True
    rewrite_hashed_json(directory, "errors.json", lambda value: mutate(value[0]))

    with pytest.raises(cli.GenerationValidationError):
        cli.read_validated_generation(directory)


def test_validated_reader_rejects_mutated_no_records_error(tmp_path):
    directory = tmp_path / DAY.isoformat()
    primary = {"records": []}
    errors = [cli._no_records_error()]
    manifest = cli._manifest(
        business_date=DAY,
        lower=LOWER,
        upper=UPPER,
        generation_id=GENERATION_ID,
        captured_at=CAPTURED,
        status="failed",
        source_row_count=0,
        record_count=0,
        error_count=1,
        category_counts={},
    )
    assert cli._write_artifacts(directory, primary, manifest, errors) is True
    rewrite_hashed_json(
        directory,
        "errors.json",
        lambda value: value[0].__setitem__("message", "different message"),
    )

    with pytest.raises(cli.GenerationValidationError):
        cli.read_validated_generation(directory)


def test_main_commits_successful_generation(tmp_path, monkeypatch):
    install_source(monkeypatch, [db_row(row_id=2), db_row(row_id=1)])
    exit_code = cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)])
    assert exit_code == 0
    primary, errors, manifest = cli.read_validated_generation(tmp_path / DAY.isoformat())
    assert [row["id"] for row in primary["records"]] == [1, 2]
    assert errors == []
    assert manifest["status"] == "success"
    assert manifest["source_row_count"] == 2
    assert manifest["record_count"] == 2


def test_main_queries_the_exact_shanghai_day_bounds(tmp_path, monkeypatch):
    settings = cli.PgSettings("host", 15432, "db", "user", "password")
    captured_bounds = []
    monkeypatch.setattr(cli, "load_pg_settings", lambda: settings)
    monkeypatch.setattr(
        cli, "fetch_day_rows",
        lambda _settings, lower, upper: captured_bounds.append((lower, upper)) or [],
    )
    monkeypatch.setattr(cli, "_new_generation_id", lambda: GENERATION_ID)
    monkeypatch.setattr(cli, "_utc_now", lambda: CAPTURED)
    cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)])
    assert captured_bounds == [(LOWER, UPPER)]


def test_main_commits_partial_generation_and_returns_one(tmp_path, monkeypatch):
    install_source(monkeypatch, [db_row(row_id=1), db_row(row_id=2, rank=0)])
    exit_code = cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)])
    assert exit_code == 1
    primary, errors, manifest = cli.read_validated_generation(tmp_path / DAY.isoformat())
    assert [row["id"] for row in primary["records"]] == [1]
    assert [error["id"] for error in errors] == [2]
    assert manifest["status"] == "partial"
    assert manifest["source_row_count"] == 2
    assert manifest["record_count"] == 1
    assert manifest["error_count"] == 1


def test_main_treats_zero_source_rows_as_a_failed_generation(tmp_path, monkeypatch):
    install_source(monkeypatch, [])
    exit_code = cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)])
    assert exit_code == 1
    primary, errors, manifest = cli.read_validated_generation(tmp_path / DAY.isoformat())
    assert primary == {"records": []}
    assert errors == [cli._no_records_error()]
    assert manifest["status"] == "failed"
    assert manifest["source_row_count"] == 0
    assert manifest["category_counts"] == {}


def test_main_treats_all_invalid_rows_as_a_failed_generation(tmp_path, monkeypatch):
    install_source(monkeypatch, [db_row(row_id=1, rank=0), db_row(row_id=2, rank=True)])
    exit_code = cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)])
    assert exit_code == 1
    primary, errors, manifest = cli.read_validated_generation(tmp_path / DAY.isoformat())
    assert primary == {"records": []}
    assert len(errors) == 2
    assert manifest["status"] == "failed"
    assert manifest["source_row_count"] == 2
    assert manifest["record_count"] == 0


def test_main_sanitizes_source_failure_and_uses_unknown_source_count(tmp_path, monkeypatch):
    install_source(monkeypatch, error=RuntimeError(
        "host-secret user-secret database-secret password-secret query-secret"))
    exit_code = cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)])
    assert exit_code == 1
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = cli.read_validated_generation(directory)
    assert primary == {"records": []}
    assert errors == [{"stage": "source", "type": "RuntimeError",
                       "message": "Database source operation failed"}]
    assert manifest["source_row_count"] is None
    persisted = "".join((directory / name).read_text(encoding="utf-8")
                        for name in ("polymarket_db.json", "errors.json", "manifest.json"))
    assert all(secret not in persisted for secret in (
        "host-secret", "user-secret", "database-secret", "password-secret", "query-secret"))


def test_failed_rerun_supersedes_an_older_success_for_the_same_date(tmp_path, monkeypatch):
    install_source(monkeypatch, [db_row()])
    arguments = ["--date", DAY.isoformat(), "--output-root", str(tmp_path)]
    assert cli.main(arguments) == 0
    install_source(monkeypatch, [])
    assert cli.main(arguments) == 1
    primary, errors, manifest = cli.read_validated_generation(tmp_path / DAY.isoformat())
    assert primary == {"records": []}
    assert errors == [cli._no_records_error()]
    assert manifest["status"] == "failed"


def test_main_returns_one_when_artifacts_cannot_be_committed(tmp_path, monkeypatch):
    install_source(monkeypatch, [db_row()])
    monkeypatch.setattr(cli, "_write_artifacts", lambda *_args: False)
    assert cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)]) == 1
