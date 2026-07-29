from copy import deepcopy
from decimal import Decimal


def _descending_decimal(value: object) -> Decimal:
    return Decimal(str(value)).copy_negate()


def _member_sort_key(member: dict) -> tuple[int, Decimal, Decimal, int, str]:
    amount = member.get("amount")
    share = member.get("share")
    if amount is None and share is None:
        return (1, Decimal(0), Decimal(0), 0, member["address"])
    return (
        0,
        _descending_decimal(amount),
        _descending_decimal(share),
        int(member["source_rank"]),
        member["address"],
    )


def decimal_text(value: object | None) -> str | None:
    return None if value is None else str(value)


def normalize_token_clusters(clusters: list[dict]) -> list[dict]:
    result = deepcopy(clusters)
    result.sort(
        key=lambda cluster: (
            _descending_decimal(cluster["amount"]),
            _descending_decimal(cluster["share"]),
            int(cluster["cluster_index"]),
        )
    )
    for cluster_rank, cluster in enumerate(result, start=1):
        cluster["cluster_rank"] = cluster_rank
        cluster["members"].sort(key=_member_sort_key)
        for member_rank, member in enumerate(cluster["members"], start=1):
            member["member_rank"] = member_rank
        cluster["member_count"] = len(cluster["members"])
    return result


def put_token(
    output: dict,
    chain: str,
    token_address: str,
    clusters: list[dict],
) -> None:
    output.setdefault(chain, {})[token_address] = {"clusters": clusters}
