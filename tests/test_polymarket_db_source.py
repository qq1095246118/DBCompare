from datetime import datetime, timezone

import pytest

from getDB.Polymarket.tool import db_source
from getDB.Polymarket.tool.db_source import PgSettings, fetch_day_rows, load_pg_settings


PG_NAMES = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")


def clear_pg(monkeypatch):
    for name in PG_NAMES:
        monkeypatch.delenv(name, raising=False)


def set_pg(monkeypatch):
    values = {
        "PGHOST": "db.example.invalid", "PGPORT": "15432",
        "PGDATABASE": "analytics", "PGUSER": "reader",
        "PGPASSWORD": "password-sentinel",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_load_pg_settings_loads_dotenv_and_hides_password(monkeypatch):
    loaded = []
    monkeypatch.setattr(db_source.dotenv, "load_dotenv", lambda: loaded.append(True))
    clear_pg(monkeypatch)
    set_pg(monkeypatch)
    settings = load_pg_settings()
    assert loaded == [True]
    assert settings == PgSettings("db.example.invalid", 15432, "analytics", "reader", "password-sentinel")
    assert "password-sentinel" not in repr(settings)


@pytest.mark.parametrize("missing_name", PG_NAMES)
def test_load_pg_settings_names_each_missing_variable_without_values(monkeypatch, missing_name):
    monkeypatch.setattr(db_source.dotenv, "load_dotenv", lambda: None)
    clear_pg(monkeypatch)
    values = set_pg(monkeypatch)
    monkeypatch.delenv(missing_name)
    with pytest.raises(ValueError) as raised:
        load_pg_settings()
    assert missing_name in str(raised.value)
    assert all(value not in str(raised.value) for value in values.values())


@pytest.mark.parametrize("port", ["not-an-int", "0", "65536"])
def test_load_pg_settings_rejects_invalid_port_without_echoing_value(monkeypatch, port):
    monkeypatch.setattr(db_source.dotenv, "load_dotenv", lambda: None)
    clear_pg(monkeypatch)
    values = set_pg(monkeypatch)
    monkeypatch.setenv("PGPORT", port)
    with pytest.raises(ValueError) as raised:
        load_pg_settings()
    rendered = repr(raised.value)
    assert "PGPORT" in rendered
    assert port not in rendered
    assert values["PGPASSWORD"] not in rendered


class FakeCursor:
    def __init__(self, rows, *, query_error=None, fetch_error=None, rollback_error=None):
        self.rows = rows
        self.query_error = query_error
        self.fetch_error = fetch_error
        self.rollback_error = rollback_error
        self.executions = []

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, traceback): return False

    def execute(self, statement, parameters=None):
        self.executions.append((" ".join(statement.split()), parameters))
        if statement.strip() == "ROLLBACK;" and self.rollback_error:
            raise self.rollback_error
        if "FROM public.information" in statement and self.query_error:
            raise self.query_error

    def fetchall(self):
        if self.fetch_error:
            raise self.fetch_error
        return self.rows


class FakeConnection:
    def __init__(self, cursor): self._cursor = cursor
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, traceback): return False
    def cursor(self): return self._cursor


def bounds():
    return (datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
            datetime(2026, 7, 29, 16, tzinfo=timezone.utc))


def test_fetch_day_rows_uses_exact_filters_bounds_and_read_only_transaction(monkeypatch):
    rows, cursor, captured = [{"id": 1}], FakeCursor([{"id": 1}]), {}
    monkeypatch.setattr(db_source.psycopg, "connect", lambda **kwargs: captured.update(kwargs) or FakeConnection(cursor))
    lower, upper = bounds()
    result = fetch_day_rows(PgSettings("host", 5432, "db", "user", "password"), lower, upper)
    assert result == rows
    assert captured["autocommit"] is True
    assert captured["row_factory"] is db_source.dict_row
    assert cursor.executions[0] == ("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;", None)
    query, parameters = cursor.executions[1]
    assert "FROM public.information" in query
    assert "from_source = %s" in query and "data_type = %s" in query
    assert "created_at >= %s" in query and "created_at < %s" in query
    assert "ORDER BY created_at, id" in query
    assert parameters == ("polymarket", "PREDICTION_MARKET_SELECTION", lower, upper)
    assert cursor.executions[-1] == ("COMMIT;", None)


@pytest.mark.parametrize("failure_stage", ["query", "fetch"])
def test_fetch_day_rows_rolls_back_without_masking_failure(monkeypatch, failure_stage):
    error = RuntimeError(f"{failure_stage}-sentinel")
    cursor = FakeCursor([], query_error=error if failure_stage == "query" else None,
                        fetch_error=error if failure_stage == "fetch" else None,
                        rollback_error=RuntimeError("rollback-sentinel"))
    monkeypatch.setattr(db_source.psycopg, "connect", lambda **_kwargs: FakeConnection(cursor))
    lower, upper = bounds()
    with pytest.raises(RuntimeError, match=f"{failure_stage}-sentinel"):
        fetch_day_rows(PgSettings("host", 5432, "db", "user", "password"), lower, upper)
    assert cursor.executions[-1] == ("ROLLBACK;", None)
    assert all(statement != "COMMIT;" for statement, _ in cursor.executions)
