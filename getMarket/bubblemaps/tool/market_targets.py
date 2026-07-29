from collections.abc import Mapping

import psycopg
from psycopg.rows import dict_row

from getDB.bubblemaps.tool.db_source import PgSettings
from getMarket.bubblemaps.tool.market_identity import TargetToken, make_target


_BEGIN_READ_ONLY = "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"
_OUTPUT_CHAINS = (
    "eth",
    "base",
    "solana",
    "tron",
    "bsc",
    "sonic",
    "ton",
    "avalanche",
    "polygon",
    "monad",
    "hyperevm",
    "arbitrum",
    "robinhood",
)
_SOURCE_CHAINS = _OUTPUT_CHAINS + ("arb",)
_SOURCE_CHAIN_ALIASES = {"arb": "arbitrum"}
_TARGET_QUERY = """
SELECT chain, token_address
FROM public.binance_address_metadata
WHERE chain = ANY(%s)
  AND is_active = 1
  AND token_address IS NOT NULL
  AND btrim(token_address) <> ''
{symbol_filter}
ORDER BY chain, token_address;
"""


def load_targets(
    settings: PgSettings,
    *,
    symbols: tuple[str, ...] | None = None,
) -> dict[str, list[str]]:
    """Load and normalize supported token targets from PostgreSQL."""
    symbol_filter = ""
    query_parameters: list[object] = [list(_SOURCE_CHAINS)]
    if symbols is not None:
        symbol_filter = "  AND upper(btrim(token_symbol)) = ANY(%s)"
        query_parameters.append(list(symbols))

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
                cursor.execute(
                    _TARGET_QUERY.format(symbol_filter=symbol_filter),
                    tuple(query_parameters),
                )
                rows = list(cursor.fetchall())
                cursor.execute("COMMIT;")
            except BaseException:
                try:
                    cursor.execute("ROLLBACK;")
                except BaseException:
                    pass
                raise

    normalized: list[TargetToken] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("target query row must be a mapping")
        source_chain = row.get("chain")
        token_address = row.get("token_address")
        if not isinstance(source_chain, str) or not source_chain.strip():
            continue
        if not isinstance(token_address, str) or not token_address.strip():
            continue
        target = make_target(
            _SOURCE_CHAIN_ALIASES.get(source_chain, source_chain), token_address
        )
        normalized.append(target)

    return targets_to_dict(normalized)


def _requested_address_preference(target: TargetToken) -> tuple[int, str]:
    if target.chain == "ton":
        is_raw = target.requested_token_address == target.token_address
        return (int(is_raw), target.requested_token_address)
    return (0, target.token_address)


def _dedupe_targets(targets: list[TargetToken]) -> list[TargetToken]:
    by_identity: dict[tuple[str, str], TargetToken] = {}
    for target in targets:
        if not isinstance(target, TargetToken):
            raise TypeError("selected targets must contain TargetToken values")
        identity = (target.chain, target.token_address)
        current = by_identity.get(identity)
        if current is None or _requested_address_preference(
            target
        ) < _requested_address_preference(current):
            by_identity[identity] = target
    return [by_identity[identity] for identity in sorted(by_identity)]


def targets_to_dict(selected: list[TargetToken]) -> dict[str, list[str]]:
    """Convert selected identities to the validated pipeline JSON shape."""
    grouped: dict[str, list[str]] = {}
    for target in _dedupe_targets(selected):
        grouped.setdefault(target.chain, []).append(
            target.requested_token_address
        )
    return {
        chain: sorted(addresses)
        for chain, addresses in sorted(grouped.items())
    }


def select_targets(
    targets: Mapping[str, list[str]],
    limit: int | None,
    chain: str | None,
    token_address: str | None,
) -> list[TargetToken]:
    """Return deterministic target tokens for a whole or single-target run."""
    if (chain is None) != (token_address is None):
        raise ValueError("chain and token_address must be provided together")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be a positive integer")

    normalized = _dedupe_targets([
        make_target(target_chain, address)
        for target_chain, addresses in targets.items()
        for address in addresses
    ])

    if chain is not None and token_address is not None:
        requested = make_target(chain, token_address)
        if (requested.chain, requested.token_address) not in {
            (target.chain, target.token_address) for target in normalized
        }:
            raise ValueError("requested target is not present in the database result")
        return [requested]

    return normalized if limit is None else normalized[:limit]
