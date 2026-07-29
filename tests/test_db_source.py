from copy import deepcopy
from datetime import datetime, timezone
import re

import pytest

from getDB.bubblemaps.tool import db_source
from getDB.bubblemaps.tool.db_source import (
    PgSettings,
    build_db_output,
    fetch_day_rows,
    load_pg_settings,
    select_latest_batches,
)


EXPECTED_MEMBER_FIELDS = {
    "member_rank",
    "member_type",
    "address",
    "source_rank",
    "amount",
    "share",
    "share_percent",
    "label",
    "entity_id",
    "is_contract",
    "is_cex",
    "is_dex",
    "is_supernode",
    "degree",
    "inward_relations",
    "outward_relations",
    "first_activity_date",
}


def _members_by_address(token: dict) -> dict[str, dict]:
    return {
        member["address"]: member
        for cluster in token["clusters"]
        for member in cluster["members"]
    }


def test_latest_batch_is_selected_per_chain_and_token(db_rows) -> None:
    selected = select_latest_batches(db_rows["holders"])

    assert selected == {
        ("eth", "0xtoken"): "00000000-0000-0000-0000-000000000002",
        ("sol", "TokenCaseSensitive"): (
            "00000000-0000-0000-0000-000000000003"
        ),
    }


def test_latest_batch_accepts_datetimes_and_z_strings_with_batch_tie_break() -> None:
    rows = [
        {
            "batch_id": "batch-a",
            "chain": "eth",
            "token_address": "0xTie",
            "created_at": "2026-07-21T03:00:00Z",
        },
        {
            "batch_id": "batch-b",
            "chain": "eth",
            "token_address": "0xTie",
            "created_at": datetime(2026, 7, 21, 3, 0, tzinfo=timezone.utc),
        },
        {
            "batch_id": "batch-z",
            "chain": "sol",
            "token_address": "CaseSensitiveToken",
            "created_at": datetime(2026, 7, 21, 2, 0, tzinfo=timezone.utc),
        },
    ]

    assert select_latest_batches(rows) == {
        ("eth", "0xTie"): "batch-b",
        ("sol", "CaseSensitiveToken"): "batch-z",
    }


def test_db_output_uses_only_selected_clusters_and_cluster_members(db_rows) -> None:
    output, errors, manifest_tokens = build_db_output(
        db_rows["holders"],
        db_rows["clusters"],
    )

    token = output["eth"]["0xtoken"]
    addresses = set(_members_by_address(token))

    assert list(output) == ["eth", "sol"]
    assert list(output["sol"]) == ["TokenCaseSensitive"]
    assert [cluster["cluster_index"] for cluster in token["clusters"]] == [2, 7]
    assert [cluster["cluster_rank"] for cluster in token["clusters"]] == [1, 2]
    assert addresses == {"0xAlpha", "0xBeta", "SharedAddress"}
    assert "0xUnclustered" not in addresses
    assert "0xOldOnly" not in addresses
    assert errors == []
    assert manifest_tokens == [
        {
            "chain": "eth",
            "token_address": "0xtoken",
            "batch_id": "00000000-0000-0000-0000-000000000002",
            "snapshot_created_at": "2026-07-21T01:05:00Z",
            "holder_count": 4,
            "cluster_count": 2,
        },
        {
            "chain": "sol",
            "token_address": "TokenCaseSensitive",
            "batch_id": "00000000-0000-0000-0000-000000000003",
            "snapshot_created_at": "2026-07-21T02:01:00Z",
            "holder_count": 2,
            "cluster_count": 1,
        },
    ]


def test_db_output_preserves_cluster_aggregates_and_common_sorting(db_rows) -> None:
    output, errors, _ = build_db_output(
        db_rows["holders"],
        db_rows["clusters"],
    )

    clusters = output["eth"]["0xtoken"]["clusters"]
    second_cluster = clusters[1]

    assert errors == []
    assert second_cluster["cluster_index"] == 7
    assert second_cluster["amount"] == "40.123456789012345678"
    assert second_cluster["share"] == "0.0400000001"
    assert second_cluster["share_percent"] == "4.0001"
    assert second_cluster["member_count"] == 2
    assert [member["address"] for member in second_cluster["members"]] == [
        "0xBeta",
        "SharedAddress",
    ]
    assert [member["member_rank"] for member in second_cluster["members"]] == [
        1,
        2,
    ]


def test_db_output_emits_all_common_holder_fields_with_nulls(db_rows) -> None:
    output, errors, _ = build_db_output(
        db_rows["holders"],
        db_rows["clusters"],
    )

    beta = _members_by_address(output["eth"]["0xtoken"])["0xBeta"]

    assert errors == []
    assert set(beta) == EXPECTED_MEMBER_FIELDS
    assert beta["member_type"] == "holder"
    assert beta["source_rank"] == 2
    assert beta["amount"] == "30.000000000000000000"
    assert beta["share"] == "0.0300000000"
    assert beta["share_percent"] == "3.0000"
    assert beta["label"] is None
    assert beta["entity_id"] is None
    assert beta["is_contract"] is None
    assert beta["degree"] is None
    assert beta["first_activity_date"] is None


def test_db_output_joins_members_on_full_snapshot_key_and_preserves_case(
    db_rows,
) -> None:
    output, errors, _ = build_db_output(
        db_rows["holders"],
        db_rows["clusters"],
    )

    eth_shared = _members_by_address(output["eth"]["0xtoken"])[
        "SharedAddress"
    ]
    sol_token = output["sol"]["TokenCaseSensitive"]
    sol_shared = _members_by_address(sol_token)["SharedAddress"]

    assert errors == []
    assert eth_shared["amount"] == "20.000000000000000000"
    assert eth_shared["label"] == "eth spelling"
    assert sol_shared["amount"] == "700.000000000000000000"
    assert sol_shared["label"] == "sol spelling"
    assert "SoLCaseHolder" in _members_by_address(sol_token)


def test_missing_member_fails_the_whole_token_but_not_other_tokens(db_rows) -> None:
    rows = deepcopy(db_rows)
    rows["clusters"][1]["holders"].append("0xMissing")

    output, errors, manifest_tokens = build_db_output(
        rows["holders"],
        rows["clusters"],
    )

    assert "eth" not in output
    assert list(output["sol"]) == ["TokenCaseSensitive"]
    assert [token["chain"] for token in manifest_tokens] == ["sol"]
    assert len(errors) == 1
    assert set(errors[0]) == {
        "chain",
        "token_address",
        "stage",
        "type",
        "message",
    }
    assert errors[0]["chain"] == "eth"
    assert errors[0]["token_address"] == "0xtoken"
    assert errors[0]["stage"] == "cluster_member_resolution"
    assert errors[0]["type"] == "ValueError"
    assert "0xMissing" in errors[0]["message"]


@pytest.mark.parametrize(
    "malformed_holders",
    [None, {"address": "0xAlpha"}, [None], [{"address": "0xAlpha"}]],
)
def test_malformed_cluster_members_fail_the_whole_token(
    db_rows,
    malformed_holders,
) -> None:
    rows = deepcopy(db_rows)
    rows["clusters"][1]["holders"] = malformed_holders

    output, errors, manifest_tokens = build_db_output(
        rows["holders"],
        rows["clusters"],
    )

    assert "eth" not in output
    assert [token["chain"] for token in manifest_tokens] == ["sol"]
    assert len(errors) == 1
    assert errors[0]["stage"] == "cluster_member_resolution"
    assert errors[0]["type"] == "ValueError"


def test_missing_cluster_members_field_fails_the_whole_token(db_rows) -> None:
    rows = deepcopy(db_rows)
    del rows["clusters"][1]["holders"]

    output, errors, manifest_tokens = build_db_output(
        rows["holders"],
        rows["clusters"],
    )

    assert "eth" not in output
    assert [token["chain"] for token in manifest_tokens] == ["sol"]
    assert errors[0]["stage"] == "cluster_member_resolution"


def test_cluster_resolution_error_is_independent_of_input_order(db_rows) -> None:
    forward_rows = deepcopy(db_rows)
    forward_rows["clusters"][1]["holders"] = ["0xMissingSeven"]
    forward_rows["clusters"][2]["holders"] = ["0xMissingTwo"]
    reverse_rows = {
        "holders": deepcopy(db_rows["holders"]),
        "clusters": list(reversed(deepcopy(forward_rows["clusters"]))),
    }

    forward_result = build_db_output(
        forward_rows["holders"],
        forward_rows["clusters"],
    )
    reverse_result = build_db_output(
        reverse_rows["holders"],
        reverse_rows["clusters"],
    )

    assert forward_result == reverse_result
    assert "0xMissingTwo" in forward_result[1][0]["message"]


def test_missing_cluster_amount_is_an_isolated_normalization_error(db_rows) -> None:
    rows = deepcopy(db_rows)
    rows["clusters"][1]["amount"] = None

    output, errors, manifest_tokens = build_db_output(
        rows["holders"],
        rows["clusters"],
    )

    assert "eth" not in output
    assert list(output["sol"]) == ["TokenCaseSensitive"]
    assert [token["chain"] for token in manifest_tokens] == ["sol"]
    assert len(errors) == 1
    assert errors[0]["stage"] == "cluster_normalization"
    assert errors[0]["type"] == "ValueError"
    assert "Cluster 7" in errors[0]["message"]
    assert "amount" in errors[0]["message"]


def test_malformed_holder_share_is_an_isolated_normalization_error(db_rows) -> None:
    rows = deepcopy(db_rows)
    rows["holders"][2]["share"] = "not-a-decimal"

    output, errors, manifest_tokens = build_db_output(
        rows["holders"],
        rows["clusters"],
    )

    assert "eth" not in output
    assert list(output["sol"]) == ["TokenCaseSensitive"]
    assert [token["chain"] for token in manifest_tokens] == ["sol"]
    assert len(errors) == 1
    assert errors[0]["stage"] == "cluster_normalization"
    assert errors[0]["type"] == "ValueError"
    assert "Cluster 2" in errors[0]["message"]
    assert "0xAlpha" in errors[0]["message"]
    assert "share" in errors[0]["message"]


def test_malformed_cluster_index_is_an_isolated_normalization_error(db_rows) -> None:
    rows = deepcopy(db_rows)
    rows["clusters"][1]["cluster_index"] = "not-an-index"

    output, errors, manifest_tokens = build_db_output(
        rows["holders"],
        rows["clusters"],
    )

    assert "eth" not in output
    assert list(output["sol"]) == ["TokenCaseSensitive"]
    assert [token["chain"] for token in manifest_tokens] == ["sol"]
    assert len(errors) == 1
    assert errors[0]["stage"] == "cluster_normalization"
    assert errors[0]["type"] == "ValueError"
    assert "cluster_index" in errors[0]["message"]


@pytest.mark.parametrize("malformed_index", [2.5, float("inf"), "7"])
def test_non_integer_cluster_index_is_an_isolated_normalization_error(
    db_rows,
    malformed_index,
) -> None:
    rows = deepcopy(db_rows)
    rows["clusters"][1]["cluster_index"] = malformed_index

    output, errors, manifest_tokens = build_db_output(
        rows["holders"],
        rows["clusters"],
    )

    assert "eth" not in output
    assert list(output["sol"]) == ["TokenCaseSensitive"]
    assert [token["chain"] for token in manifest_tokens] == ["sol"]
    assert len(errors) == 1
    assert errors[0]["stage"] == "cluster_normalization"
    assert errors[0]["type"] == "ValueError"
    assert "cluster_index" in errors[0]["message"]


def test_fractional_holder_rank_is_an_isolated_normalization_error(db_rows) -> None:
    rows = deepcopy(db_rows)
    rows["holders"][2]["rank"] = 1.5

    output, errors, manifest_tokens = build_db_output(
        rows["holders"],
        rows["clusters"],
    )

    assert "eth" not in output
    assert list(output["sol"]) == ["TokenCaseSensitive"]
    assert [token["chain"] for token in manifest_tokens] == ["sol"]
    assert len(errors) == 1
    assert errors[0]["stage"] == "cluster_normalization"
    assert errors[0]["type"] == "ValueError"
    assert "0xAlpha" in errors[0]["message"]
    assert "source_rank" in errors[0]["message"]


def test_missing_cluster_batch_id_is_an_isolated_resolution_error(db_rows) -> None:
    rows = deepcopy(db_rows)
    del rows["clusters"][1]["batch_id"]

    output, errors, manifest_tokens = build_db_output(
        rows["holders"],
        rows["clusters"],
    )

    assert "eth" not in output
    assert list(output["sol"]) == ["TokenCaseSensitive"]
    assert [token["chain"] for token in manifest_tokens] == ["sol"]
    assert len(errors) == 1
    assert errors[0]["stage"] == "cluster_member_resolution"
    assert errors[0]["type"] == "ValueError"
    assert "batch_id" in errors[0]["message"]


def test_malformed_selected_holder_shape_is_an_isolated_resolution_error(
    db_rows,
) -> None:
    rows = deepcopy(db_rows)
    del rows["holders"][5]["address"]

    output, errors, manifest_tokens = build_db_output(
        rows["holders"],
        rows["clusters"],
    )

    assert "eth" not in output
    assert list(output["sol"]) == ["TokenCaseSensitive"]
    assert [token["chain"] for token in manifest_tokens] == ["sol"]
    assert len(errors) == 1
    assert errors[0]["stage"] == "cluster_member_resolution"
    assert errors[0]["type"] == "ValueError"
    assert "address" in errors[0]["message"]


@pytest.mark.parametrize("malformed_address", [None, "", 123])
def test_invalid_selected_holder_address_is_an_isolated_resolution_error(
    db_rows,
    malformed_address,
) -> None:
    rows = deepcopy(db_rows)
    rows["holders"][5]["address"] = malformed_address

    output, errors, manifest_tokens = build_db_output(
        rows["holders"],
        rows["clusters"],
    )

    assert "eth" not in output
    assert list(output["sol"]) == ["TokenCaseSensitive"]
    assert [token["chain"] for token in manifest_tokens] == ["sol"]
    assert len(errors) == 1
    assert errors[0]["stage"] == "cluster_member_resolution"
    assert errors[0]["type"] == "ValueError"
    assert "address" in errors[0]["message"]


def test_datetime_snapshot_is_serialized_for_manifest() -> None:
    created_at = datetime(2026, 7, 21, 3, 4, 5, tzinfo=timezone.utc)
    holders = [
        {
            "batch_id": "batch",
            "chain": "eth",
            "token_address": "0xDate",
            "address": "0xHolder",
            "rank": 1,
            "amount": "1.0",
            "share": "0.1",
            "share_percent": "10.0",
            "created_at": created_at,
        }
    ]

    output, errors, manifest_tokens = build_db_output(holders, [])

    assert output == {"eth": {"0xDate": {"clusters": []}}}
    assert errors == []
    assert manifest_tokens[0]["snapshot_created_at"] == (
        "2026-07-21T03:04:05+00:00"
    )


def _clear_pg_environment(monkeypatch) -> None:
    for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        monkeypatch.delenv(name, raising=False)


def _rendered_exception_messages(error: BaseException) -> str:
    messages = []
    current: BaseException | None = error
    while current is not None:
        messages.append(f"{type(current).__name__}: {current}")
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return "\n".join(messages)


def test_load_pg_settings_loads_dotenv_and_returns_typed_settings(
    monkeypatch,
) -> None:
    loaded = []
    monkeypatch.setattr(db_source.dotenv, "load_dotenv", lambda: loaded.append(True))
    _clear_pg_environment(monkeypatch)
    expected_values = {
        "PGHOST": "db.example.invalid",
        "PGPORT": "15432",
        "PGDATABASE": "analytics",
        "PGUSER": "reader",
        "PGPASSWORD": "test-only-password",
    }
    for name, value in expected_values.items():
        monkeypatch.setenv(name, value)

    result = load_pg_settings()

    assert loaded == [True]
    assert result == PgSettings(
        host="db.example.invalid",
        port=15432,
        dbname="analytics",
        user="reader",
        password="test-only-password",
    )


def test_pg_settings_repr_does_not_expose_password() -> None:
    settings = PgSettings(
        host="db.example.invalid",
        port=15432,
        dbname="analytics",
        user="reader",
        password="password-repr-sentinel",
    )

    assert "PgSettings" in repr(settings)
    assert "password-repr-sentinel" not in repr(settings)


@pytest.mark.parametrize(
    "missing_name",
    ["PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"],
)
def test_load_pg_settings_names_only_the_missing_variable(
    monkeypatch,
    missing_name,
) -> None:
    monkeypatch.setattr(db_source.dotenv, "load_dotenv", lambda: None)
    _clear_pg_environment(monkeypatch)
    values = {
        "PGHOST": "host-sentinel",
        "PGPORT": "15432",
        "PGDATABASE": "database-sentinel",
        "PGUSER": "user-sentinel",
        "PGPASSWORD": "password-sentinel",
    }
    for name, value in values.items():
        if name != missing_name:
            monkeypatch.setenv(name, value)

    with pytest.raises(ValueError) as raised:
        load_pg_settings()

    message = str(raised.value)
    assert missing_name in message
    assert all(value not in message for value in values.values())


@pytest.mark.parametrize("invalid_port", ["not-a-port-sentinel", "0", "65536"])
def test_load_pg_settings_rejects_invalid_port_without_echoing_it(
    monkeypatch,
    invalid_port,
) -> None:
    monkeypatch.setattr(db_source.dotenv, "load_dotenv", lambda: None)
    _clear_pg_environment(monkeypatch)
    monkeypatch.setenv("PGHOST", "host-sentinel")
    monkeypatch.setenv("PGPORT", invalid_port)
    monkeypatch.setenv("PGDATABASE", "database-sentinel")
    monkeypatch.setenv("PGUSER", "user-sentinel")
    monkeypatch.setenv("PGPASSWORD", "password-sentinel")

    with pytest.raises(ValueError) as raised:
        load_pg_settings()

    message = str(raised.value)
    assert "PGPORT" in message
    assert invalid_port not in message
    assert "password-sentinel" not in message
    rendered_error = _rendered_exception_messages(raised.value)
    assert invalid_port not in rendered_error


class FakeCursor:
    def __init__(
        self,
        holder_rows: list[dict],
        cluster_rows: list[dict],
        *,
        fail_on: str | None = None,
        query_error: BaseException | None = None,
        rollback_error: BaseException | None = None,
    ) -> None:
        self.holder_rows = holder_rows
        self.cluster_rows = cluster_rows
        self.fail_on = fail_on
        self.query_error = query_error
        self.rollback_error = rollback_error
        self.executions: list[tuple[str, object]] = []
        self._current_rows: list[dict] = []
        self.connection = None
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.exited = True

    def execute(self, statement: str, parameters=None) -> None:
        self.executions.append((statement, parameters))
        normalized = _normalized_sql(statement)
        if normalized == "ROLLBACK;" and self.rollback_error is not None:
            raise self.rollback_error
        if self.fail_on is not None and self.fail_on in statement:
            if self.query_error is None:
                raise RuntimeError("configured query failure")
            raise self.query_error
        if "FROM public.bubblemaps_token_holder" in statement:
            self._current_rows = self.holder_rows
        elif "FROM public.bubblemaps_token_cluster" in statement:
            self._current_rows = self.cluster_rows
        else:
            self._current_rows = []

    def fetchall(self) -> list[dict]:
        return self._current_rows


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self._cursor.connection = self
        self.cursor_calls = 0
        self.entered = False
        self.exited = False
        self.exit_error = None

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.exited = True
        self.exit_error = exc_value

    def cursor(self) -> FakeCursor:
        self.cursor_calls += 1
        return self._cursor


def _normalized_sql(statement: str) -> str:
    return " ".join(statement.split())


def test_fetch_day_rows_uses_one_read_only_transaction_and_exact_queries(
    monkeypatch,
) -> None:
    holder_rows = [{"address": "HolderFromDatabase"}]
    cluster_rows = [{"cluster_index": 4}]
    cursor = FakeCursor(holder_rows, cluster_rows)
    connection = FakeConnection(cursor)
    connection_arguments = []

    def fake_connect(**kwargs):
        connection_arguments.append(kwargs)
        return connection

    monkeypatch.setattr(db_source.psycopg, "connect", fake_connect)
    settings = PgSettings(
        host="host.example.invalid",
        port=15432,
        dbname="analytics",
        user="readonly",
        password="test-only-password",
    )
    lower = datetime(2026, 7, 20, 16, tzinfo=timezone.utc)
    upper = datetime(2026, 7, 21, 16, tzinfo=timezone.utc)

    result = fetch_day_rows(settings, lower, upper)

    assert result == (holder_rows, cluster_rows)
    assert connection.entered and connection.exited
    assert connection.cursor_calls == 1
    assert cursor.entered and cursor.exited
    assert cursor.connection is connection
    assert connection_arguments == [
        {
            "host": settings.host,
            "port": settings.port,
            "dbname": settings.dbname,
            "user": settings.user,
            "password": settings.password,
            "row_factory": db_source.dict_row,
            "autocommit": True,
        }
    ]

    executions = [
        (_normalized_sql(statement), parameters)
        for statement, parameters in cursor.executions
    ]
    assert executions[0] == (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;",
        None,
    )
    assert executions[-1] == ("COMMIT;", None)
    assert [parameters for _, parameters in executions] == [
        None,
        (lower, upper),
        (lower, upper),
        None,
    ]

    holder_sql = executions[1][0]
    cluster_sql = executions[2][0]
    assert holder_sql == (
        "SELECT batch_id, chain, token_address, address, data_source, "
        "data_tags, label, rank, entity_id, amount, share, share_percent, "
        "is_contract, is_cex, is_dex, is_supernode, degree, "
        "inward_relations, outward_relations, first_activity_date, "
        "created_at FROM public.bubblemaps_token_holder "
        "WHERE created_at >= %s AND created_at < %s "
        "ORDER BY chain, token_address, batch_id, rank, address;"
    )
    assert cluster_sql == (
        "SELECT batch_id, chain, token_address, cluster_index, data_source, "
        "data_tags, share, share_percent, amount, holder_count, holders, "
        "created_at FROM public.bubblemaps_token_cluster "
        "WHERE created_at >= %s AND created_at < %s "
        "ORDER BY chain, token_address, batch_id, cluster_index;"
    )
    assert "raw_data" not in holder_sql
    assert "raw_data" not in cluster_sql
    assert all(
        re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE)\b", sql)
        is None
        for sql, _ in executions
    )


def test_fetch_day_rows_rolls_back_and_preserves_query_error_when_rollback_fails(
    monkeypatch,
) -> None:
    query_error = RuntimeError("primary query failure")
    rollback_error = RuntimeError("rollback failure")
    cursor = FakeCursor(
        [{"address": "HolderFromDatabase"}],
        [{"cluster_index": 4}],
        fail_on="FROM public.bubblemaps_token_cluster",
        query_error=query_error,
        rollback_error=rollback_error,
    )
    connection = FakeConnection(cursor)
    connection_arguments = []

    def fake_connect(**kwargs):
        connection_arguments.append(kwargs)
        return connection

    monkeypatch.setattr(db_source.psycopg, "connect", fake_connect)
    settings = PgSettings(
        host="host.example.invalid",
        port=15432,
        dbname="analytics",
        user="readonly",
        password="test-only-password",
    )
    lower = datetime(2026, 7, 20, 16, tzinfo=timezone.utc)
    upper = datetime(2026, 7, 21, 16, tzinfo=timezone.utc)

    with pytest.raises(RuntimeError) as raised:
        fetch_day_rows(settings, lower, upper)

    assert raised.value is query_error
    assert connection_arguments[0]["autocommit"] is True
    assert connection.entered and connection.exited
    assert connection.exit_error is query_error
    assert connection.cursor_calls == 1
    assert cursor.entered and cursor.exited
    assert cursor.connection is connection
    executions = [
        (_normalized_sql(statement), parameters)
        for statement, parameters in cursor.executions
    ]
    assert executions[0] == (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;",
        None,
    )
    assert "FROM public.bubblemaps_token_holder" in executions[1][0]
    assert "FROM public.bubblemaps_token_cluster" in executions[2][0]
    assert executions[3] == ("ROLLBACK;", None)
    assert [parameters for _, parameters in executions] == [
        None,
        (lower, upper),
        (lower, upper),
        None,
    ]
    assert all(statement != "COMMIT;" for statement, _ in executions)
