"""Read-only PostgreSQL source for Polymarket selection rows."""

import os
from dataclasses import dataclass, field
from datetime import datetime

import dotenv
import psycopg
from psycopg.rows import dict_row


_REQUIRED_PG_VARIABLES = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
_BEGIN_READ_ONLY = "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"
_INFORMATION_QUERY = """
SELECT id, data_type, title, summary, content, from_source, source_url,
       content_hash, extra_data, published_at, created_at, updated_at,
       tags, source_updated_at
FROM public.information
WHERE from_source = %s
  AND data_type = %s
  AND created_at >= %s
  AND created_at < %s
ORDER BY created_at, id;
"""


@dataclass(frozen=True)
class PgSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)


def load_pg_settings() -> PgSettings:
    dotenv.load_dotenv()
    values = {}
    for name in _REQUIRED_PG_VARIABLES:
        value = os.getenv(name)
        if not value:
            raise ValueError(f"Missing required environment variable: {name}")
        values[name] = value
    try:
        port = int(values["PGPORT"])
    except ValueError:
        raise ValueError("Invalid integer environment variable: PGPORT") from None
    if not 1 <= port <= 65_535:
        raise ValueError("Environment variable PGPORT is outside the valid range")
    return PgSettings(values["PGHOST"], port, values["PGDATABASE"],
                      values["PGUSER"], values["PGPASSWORD"])


def fetch_day_rows(settings: PgSettings, lower: datetime, upper: datetime) -> list[dict]:
    with psycopg.connect(
        host=settings.host, port=settings.port, dbname=settings.dbname,
        user=settings.user, password=settings.password,
        row_factory=dict_row, autocommit=True,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(_BEGIN_READ_ONLY)
            try:
                cursor.execute(_INFORMATION_QUERY, (
                    "polymarket", "PREDICTION_MARKET_SELECTION", lower, upper,
                ))
                rows = list(cursor.fetchall())
                cursor.execute("COMMIT;")
            except BaseException:
                try:
                    cursor.execute("ROLLBACK;")
                except BaseException:
                    pass
                raise
    return rows
