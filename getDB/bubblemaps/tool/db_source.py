from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import decimal
import os

import dotenv
import psycopg
from psycopg.rows import dict_row

from getDB.bubblemaps.tool.contract import (
    decimal_text,
    normalize_token_clusters,
    put_token,
)


_REQUIRED_PG_VARIABLES = (
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
)

_BEGIN_READ_ONLY = (
    "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"
)

_HOLDER_QUERY = """
SELECT batch_id, chain, token_address, address, data_source, data_tags,
       label, rank, entity_id, amount, share, share_percent,
       is_contract, is_cex, is_dex, is_supernode, degree,
       inward_relations, outward_relations, first_activity_date,
       created_at
FROM public.bubblemaps_token_holder
WHERE created_at >= %s AND created_at < %s
ORDER BY chain, token_address, batch_id, rank, address;
"""

_CLUSTER_QUERY = """
SELECT batch_id, chain, token_address, cluster_index, data_source,
       data_tags, share, share_percent, amount, holder_count,
       holders, created_at
FROM public.bubblemaps_token_cluster
WHERE created_at >= %s AND created_at < %s
ORDER BY chain, token_address, batch_id, cluster_index;
"""


@dataclass(frozen=True)
class PgSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class _Snapshot:
    batch_id: str
    chain: str
    token_address: str
    created_at: datetime
    created_at_text: str
    holder_rows: list[dict]


def _created_at_value(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        iso_value = f"{value[:-1]}+00:00" if value.endswith("Z") else value
        try:
            result = datetime.fromisoformat(iso_value)
        except ValueError as error:
            raise ValueError("created_at must be an ISO-8601 timestamp") from error
    else:
        raise TypeError("created_at must be a datetime or ISO-8601 string")

    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _datetime_text(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _holder_groups(
    holder_rows: list[dict],
) -> dict[tuple[str, str, str], list[dict]]:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in holder_rows:
        key = (
            str(row["batch_id"]),
            row["chain"],
            row["token_address"],
        )
        groups[key].append(row)
    return dict(groups)


def _latest_snapshots(holder_rows: list[dict]) -> dict[tuple[str, str], _Snapshot]:
    latest: dict[tuple[str, str], _Snapshot] = {}
    for (batch_id, chain, token_address), rows in _holder_groups(
        holder_rows
    ).items():
        latest_row = max(
            rows,
            key=lambda row: (
                _created_at_value(row["created_at"]),
                _datetime_text(row["created_at"]),
            ),
        )
        snapshot = _Snapshot(
            batch_id=batch_id,
            chain=chain,
            token_address=token_address,
            created_at=_created_at_value(latest_row["created_at"]),
            created_at_text=_datetime_text(latest_row["created_at"]),
            holder_rows=rows,
        )
        token_key = (chain, token_address)
        current = latest.get(token_key)
        if current is None or (snapshot.created_at, snapshot.batch_id) > (
            current.created_at,
            current.batch_id,
        ):
            latest[token_key] = snapshot

    return {key: latest[key] for key in sorted(latest)}


def select_latest_batches(
    holder_rows: list[dict],
) -> dict[tuple[str, str], str]:
    return {
        token_key: snapshot.batch_id
        for token_key, snapshot in _latest_snapshots(holder_rows).items()
    }


def _holder_member(row: dict) -> dict:
    return {
        "member_type": "holder",
        "address": row["address"],
        "source_rank": row.get("rank"),
        "amount": decimal_text(row.get("amount")),
        "share": decimal_text(row.get("share")),
        "share_percent": decimal_text(row.get("share_percent")),
        "label": row.get("label"),
        "entity_id": row.get("entity_id"),
        "is_contract": row.get("is_contract"),
        "is_cex": row.get("is_cex"),
        "is_dex": row.get("is_dex"),
        "is_supernode": row.get("is_supernode"),
        "degree": row.get("degree"),
        "inward_relations": row.get("inward_relations"),
        "outward_relations": row.get("outward_relations"),
        "first_activity_date": _datetime_text(row.get("first_activity_date")),
    }


def _resolve_cluster(
    row: dict,
    holder_index: dict[tuple[str, str, str, str], dict],
) -> dict:
    cluster_index = row.get("cluster_index")
    if "holders" not in row:
        raise ValueError(f"Cluster {cluster_index!r} has no holders field")

    holder_addresses = row["holders"]
    if not isinstance(holder_addresses, list):
        raise ValueError(f"Cluster {cluster_index!r} holders must be a list")

    batch_id = str(row["batch_id"])
    chain = row["chain"]
    token_address = row["token_address"]
    members = []
    for address in holder_addresses:
        if not isinstance(address, str) or not address:
            raise ValueError(
                f"Cluster {cluster_index!r} contains a malformed holder address"
            )
        holder_key = (batch_id, chain, token_address, address)
        holder = holder_index.get(holder_key)
        if holder is None:
            raise ValueError(
                f"Cluster {cluster_index!r} member {address!r} was not found "
                "in the selected holder snapshot"
            )
        members.append(_holder_member(holder))

    return {
        "cluster_index": cluster_index,
        "share": decimal_text(row.get("share")),
        "share_percent": decimal_text(row.get("share_percent")),
        "amount": decimal_text(row.get("amount")),
        "members": members,
    }


def _holder_index(snapshot: _Snapshot) -> dict[tuple[str, str, str, str], dict]:
    result: dict[tuple[str, str, str, str], dict] = {}
    for row in snapshot.holder_rows:
        try:
            address = row["address"]
            key = (
                str(row["batch_id"]),
                row["chain"],
                row["token_address"],
                address,
            )
        except KeyError as error:
            raise ValueError(
                f"Selected holder row is missing required field {error.args[0]!r}"
            ) from None
        if not isinstance(address, str) or not address:
            raise ValueError(
                "Selected holder row field 'address' must be a non-empty string"
            )
        result[key] = row
    return result


def _cluster_index_sort_key(row: dict) -> int:
    try:
        cluster_index = row["cluster_index"]
    except KeyError:
        raise ValueError(
            "Cluster is missing required field 'cluster_index'"
        ) from None
    if not isinstance(cluster_index, int) or isinstance(cluster_index, bool):
        raise ValueError("Cluster field 'cluster_index' must be an integer")
    return cluster_index


def _validate_decimal(value: object, description: str) -> None:
    if value is None:
        raise ValueError(f"{description} is required")
    try:
        number = decimal.Decimal(str(value))
    except (decimal.DecimalException, TypeError, ValueError):
        raise ValueError(f"{description} must be a decimal") from None
    if not number.is_finite():
        raise ValueError(f"{description} must be a finite decimal")


def _validate_optional_decimal(value: object, description: str) -> None:
    if value is not None:
        _validate_decimal(value, description)


def _validate_integer(value: object, description: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{description} must be an integer")


def _selected_snapshot_clusters(
    cluster_rows: list[dict],
    snapshot: _Snapshot,
) -> list[dict]:
    selected = []
    for row in cluster_rows:
        try:
            batch_id = row["batch_id"]
        except KeyError:
            raise ValueError(
                "Cluster row is missing required field 'batch_id'"
            ) from None
        if batch_id is None or str(batch_id) == "":
            raise ValueError("Cluster row field 'batch_id' is required")
        if str(batch_id) == snapshot.batch_id:
            selected.append(row)
    return selected


def _validate_cluster_numbers(cluster: dict) -> None:
    cluster_index = cluster["cluster_index"]
    cluster_description = f"Cluster {cluster_index!r}"
    _validate_decimal(
        cluster.get("amount"),
        f"{cluster_description} field 'amount'",
    )
    _validate_decimal(
        cluster.get("share"),
        f"{cluster_description} field 'share'",
    )
    _validate_optional_decimal(
        cluster.get("share_percent"),
        f"{cluster_description} field 'share_percent'",
    )
    for member in cluster["members"]:
        address = member.get("address")
        member_description = (
            f"{cluster_description} member {address!r}"
        )
        _validate_decimal(
            member.get("amount"),
            f"{member_description} field 'amount'",
        )
        _validate_decimal(
            member.get("share"),
            f"{member_description} field 'share'",
        )
        _validate_optional_decimal(
            member.get("share_percent"),
            f"{member_description} field 'share_percent'",
        )
        _validate_integer(
            member.get("source_rank"),
            f"{member_description} field 'source_rank'",
        )


def _token_error(
    chain: str,
    token_address: str,
    stage: str,
    error: BaseException,
) -> dict:
    return {
        "chain": chain,
        "token_address": token_address,
        "stage": stage,
        "type": type(error).__name__,
        "message": str(error),
    }


def build_db_output(
    holder_rows: list[dict],
    cluster_rows: list[dict],
) -> tuple[dict, list[dict], list[dict]]:
    snapshots = _latest_snapshots(holder_rows)
    selected_batches = {
        token_key: snapshot.batch_id
        for token_key, snapshot in snapshots.items()
    }

    token_clusters: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in cluster_rows:
        token_key = (row["chain"], row["token_address"])
        if token_key in selected_batches:
            token_clusters[token_key].append(row)

    output: dict = {}
    errors: list[dict] = []
    manifest_tokens: list[dict] = []
    for token_key, snapshot in snapshots.items():
        chain, token_address = token_key
        try:
            cluster_source_rows = _selected_snapshot_clusters(
                token_clusters.get(token_key, []),
                snapshot,
            )
        except (KeyError, TypeError, ValueError) as error:
            errors.append(
                _token_error(
                    chain,
                    token_address,
                    "cluster_member_resolution",
                    error,
                )
            )
            continue

        try:
            cluster_source_rows = sorted(
                cluster_source_rows,
                key=_cluster_index_sort_key,
            )
        except (decimal.DecimalException, KeyError, TypeError, ValueError) as error:
            errors.append(
                _token_error(
                    chain,
                    token_address,
                    "cluster_normalization",
                    error,
                )
            )
            continue

        try:
            holders_by_key = _holder_index(snapshot)
            resolved_clusters = [
                _resolve_cluster(cluster, holders_by_key)
                for cluster in cluster_source_rows
            ]
        except (KeyError, TypeError, ValueError) as error:
            errors.append(
                _token_error(
                    chain,
                    token_address,
                    "cluster_member_resolution",
                    error,
                )
            )
            continue

        try:
            for cluster in resolved_clusters:
                _validate_cluster_numbers(cluster)
            clusters = normalize_token_clusters(resolved_clusters)
        except (decimal.DecimalException, KeyError, TypeError, ValueError) as error:
            errors.append(
                _token_error(
                    chain,
                    token_address,
                    "cluster_normalization",
                    error,
                )
            )
            continue

        put_token(output, chain, token_address, clusters)
        manifest_tokens.append(
            {
                "chain": chain,
                "token_address": token_address,
                "batch_id": snapshot.batch_id,
                "snapshot_created_at": snapshot.created_at_text,
                "holder_count": len(snapshot.holder_rows),
                "cluster_count": len(cluster_source_rows),
            }
        )

    return output, errors, manifest_tokens


def load_pg_settings() -> PgSettings:
    dotenv.load_dotenv()
    values: dict[str, str] = {}
    for variable_name in _REQUIRED_PG_VARIABLES:
        value = os.getenv(variable_name)
        if not value:
            raise ValueError(
                f"Missing required environment variable: {variable_name}"
            )
        values[variable_name] = value

    try:
        port = int(values["PGPORT"])
    except ValueError:
        raise ValueError("Invalid integer environment variable: PGPORT") from None
    if not 1 <= port <= 65_535:
        raise ValueError("Environment variable PGPORT is outside the valid range")

    return PgSettings(
        host=values["PGHOST"],
        port=port,
        dbname=values["PGDATABASE"],
        user=values["PGUSER"],
        password=values["PGPASSWORD"],
    )


def fetch_day_rows(
    settings: PgSettings,
    lower: datetime,
    upper: datetime,
) -> tuple[list[dict], list[dict]]:
    with psycopg.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.dbname,
        user=settings.user,
        password=settings.password,
        row_factory=dict_row,
        autocommit=True,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(_BEGIN_READ_ONLY)
            try:
                cursor.execute(_HOLDER_QUERY, (lower, upper))
                holder_rows = list(cursor.fetchall())
                cursor.execute(_CLUSTER_QUERY, (lower, upper))
                cluster_rows = list(cursor.fetchall())
                cursor.execute("COMMIT;")
            except BaseException:
                try:
                    cursor.execute("ROLLBACK;")
                except BaseException:
                    pass
                raise
    return holder_rows, cluster_rows
