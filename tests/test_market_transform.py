import json
import sys
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal, localcontext
from pathlib import Path
from typing import get_type_hints

import pytest

from getMarket.bubblemaps.tool.market_identity import TargetToken, make_target
from getMarket.bubblemaps.tool.market_transform import (
    Cluster,
    RankedHolder,
    SnapshotModel,
    SubgraphEdge,
    filter_subgraph_edges,
    parse_ranked_holders,
    reconstruct_clusters,
    token_snapshot_fingerprint,
)


FIXTURES = Path(__file__).parent / "fixtures"
TARGET_ADDRESS = "0x1111111111111111111111111111111111111111"
A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
C = "0xcccccccccccccccccccccccccccccccccccccccc"
S = "0xdddddddddddddddddddddddddddddddddddddddd"
U = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
SUPPORTED_METADATA_VALUES = (
    ("label", "Changed"),
    ("entity_id", "entity-1"),
    ("is_contract", True),
    ("is_cex", True),
    ("is_dex", True),
    ("degree", 5),
    ("inward_relations", 7),
    ("outward_relations", 8),
    ("first_activity_date", "2026-07-22T00:00:00Z"),
)


def load_fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def official_holders() -> list[dict]:
    return load_fixture("official_holders.json")


@pytest.fixture
def initial_subgraph() -> list[dict]:
    return load_fixture("official_subgraph_initial.json")


@pytest.fixture
def settled_subgraph() -> list[dict]:
    return load_fixture("official_subgraph_settled.json")


def holder_row(
    address: str,
    rank: int | None,
    *,
    amount: object = "1",
    share: object = "0.01",
    is_supernode: object = False,
    **details: object,
) -> dict:
    return {
        "address": address,
        "address_details": {
            "is_supernode": is_supernode,
            **details,
        },
        "holder_data": {
            "rank": rank,
            "amount": amount,
            "share": share,
        },
    }


def grouped_edge(
    from_address: str,
    to_address: str,
    *,
    total_transfers: object = 1,
    total_value: object = "1.5",
    first_date: object = 100,
    last_date: object = 200,
    token_ref: object | None = None,
    rel_type: str = "GROUPED_TRANSFER",
    **extra: object,
) -> dict:
    return {
        "from_address": from_address,
        "to_address": to_address,
        "rel_type": rel_type,
        "data": {
            "total_transfers": total_transfers,
            "total_value": total_value,
            "first_date": first_date,
            "last_date": last_date,
            "token_ref": token_ref
            if token_ref is not None
            else {"chain": "bsc", "address": TARGET_ADDRESS},
        },
        **extra,
    }


def build_snapshot(
    holder_payload: object,
    subgraph_payload: object,
) -> tuple[tuple[RankedHolder, ...], tuple[SubgraphEdge, ...]]:
    target = make_target("bsc", TARGET_ADDRESS)
    holders = parse_ranked_holders(holder_payload, target=target)
    holder_index = {holder.address: holder for holder in holders}
    edges = filter_subgraph_edges(
        subgraph_payload,
        target=target,
        holders=holder_index,
    )
    return holders, edges


def uppercase_evm(value: object) -> object:
    if isinstance(value, str) and value.startswith("0x"):
        return "0x" + value[2:].upper()
    return value


def add_foreign_rows_and_uppercase_evm(payload: list[dict]) -> list[object]:
    result = deepcopy(payload)
    for row in result:
        row["from_address"] = uppercase_evm(row.get("from_address"))
        row["to_address"] = uppercase_evm(row.get("to_address"))
        data = row.get("data")
        if isinstance(data, dict) and isinstance(data.get("token_ref"), dict):
            token_ref = data["token_ref"]
            token_ref["address"] = uppercase_evm(token_ref.get("address"))

    result.extend(
        [
            grouped_edge(
                A,
                C,
                token_ref={
                    "chain": "bsc",
                    "address": "0x9999999999999999999999999999999999999999",
                },
            ),
            grouped_edge(A, U),
            grouped_edge("not-an-address", B),
            {"rel_type": "GROUPED_TRANSFER", "data": {"token_ref": None}},
            "not-an-edge",
        ]
    )
    return result


def change_ranked_holder_amount(
    payload: list[dict],
    *,
    rank: int,
    amount: str,
) -> list[dict]:
    changed = deepcopy(payload)
    for row in changed:
        if row["holder_data"]["rank"] == rank:
            row["holder_data"]["amount"] = amount
            return changed
    raise AssertionError(f"rank {rank} was not present")


def test_public_data_contracts_have_exact_frozen_fields(official_holders):
    assert [field.name for field in fields(RankedHolder)] == [
        "address",
        "source_rank",
        "amount",
        "share",
        "share_percent",
        "is_supernode",
        "metadata",
    ]
    assert [field.name for field in fields(SubgraphEdge)] == [
        "from_address",
        "to_address",
        "total_transfers",
        "raw",
    ]
    assert [field.name for field in fields(Cluster)] == [
        "cluster_rank",
        "amount",
        "share",
        "share_percent",
        "members",
    ]
    assert [field.name for field in fields(SnapshotModel)] == [
        "target",
        "holders",
        "edges",
        "clusters",
        "fingerprint",
        "captured_at",
    ]
    assert get_type_hints(RankedHolder) == {
        "address": str,
        "source_rank": int,
        "amount": str,
        "share": str,
        "share_percent": str,
        "is_supernode": bool,
        "metadata": dict,
    }
    assert get_type_hints(SubgraphEdge) == {
        "from_address": str,
        "to_address": str,
        "total_transfers": int,
        "raw": dict,
    }
    assert get_type_hints(Cluster) == {
        "cluster_rank": int,
        "amount": str,
        "share": str,
        "share_percent": str,
        "members": tuple[RankedHolder, ...],
    }
    assert get_type_hints(SnapshotModel) == {
        "target": TargetToken,
        "holders": tuple[RankedHolder, ...],
        "edges": tuple[SubgraphEdge, ...],
        "clusters": tuple[Cluster, ...],
        "fingerprint": str,
        "captured_at": str,
    }
    for contract in (RankedHolder, SubgraphEdge, Cluster, SnapshotModel):
        assert contract.__dataclass_params__.frozen is True

    holder = build_snapshot(official_holders, [])[0][0]
    with pytest.raises(FrozenInstanceError):
        holder.amount = "999"


def test_parse_ranked_holders_excludes_null_rank_and_keeps_supernode(
    official_holders,
):
    target = make_target("bsc", TARGET_ADDRESS)
    original = deepcopy(official_holders)

    holders = parse_ranked_holders(official_holders, target=target)

    assert [holder.source_rank for holder in holders] == [1, 2, 3, 4]
    assert holders[-1].is_supernode is True
    assert all(holder.address != U for holder in holders)
    assert holders[0].metadata == {"label": "A"}
    assert "is_supernode" not in holders[-1].metadata
    assert official_holders == original


@pytest.mark.parametrize(
    "indices",
    [
        (4, 3, 2, 1, 0),
        (2, 4, 0, 3, 1),
    ],
    ids=("reversed", "shuffled"),
)
def test_parse_ranked_holders_returns_source_rank_order(
    official_holders,
    indices,
):
    target = make_target("bsc", TARGET_ADDRESS)
    payload = [official_holders[index] for index in indices]

    holders = parse_ranked_holders(payload, target=target)

    assert [holder.source_rank for holder in holders] == [1, 2, 3, 4]


def test_parse_ranked_holders_accepts_more_than_ten_thousand_ranked_rows():
    target = make_target("bsc", TARGET_ADDRESS)
    payload = [
        holder_row(
            f"0x{rank:040x}",
            rank,
            amount=str(rank),
            share="0",
        )
        for rank in range(1, 10_002)
    ]

    holders = parse_ranked_holders(payload, target=target)

    assert [holder.source_rank for holder in holders] == list(range(1, 10_002))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload + [payload[0]],
        lambda payload: [
            {
                **payload[0],
                "holder_data": {**payload[0]["holder_data"], "rank": 0},
            }
        ],
        lambda payload: [
            {**payload[0], "address_details": {"is_supernode": None}}
        ],
        lambda payload: [
            {
                **payload[0],
                "holder_data": {
                    **payload[0]["holder_data"],
                    "amount": 1.5,
                },
            }
        ],
    ],
)
def test_parse_ranked_holders_fails_closed_on_duplicate_or_malformed_data(
    official_holders,
    mutation,
):
    target = make_target("bsc", TARGET_ADDRESS)
    with pytest.raises(ValueError):
        parse_ranked_holders(mutation(official_holders), target=target)


def test_parse_ranked_holders_rejects_duplicate_canonical_addresses():
    target = make_target("bsc", TARGET_ADDRESS)
    payload = [
        holder_row(A, 1),
        holder_row("0x" + "A" * 40, 2),
    ]

    with pytest.raises(ValueError, match="duplicate.*address"):
        parse_ranked_holders(payload, target=target)


def test_parse_ranked_holders_rejects_duplicate_source_ranks():
    target = make_target("bsc", TARGET_ADDRESS)
    payload = [holder_row(A, 1), holder_row(B, 1)]

    with pytest.raises(ValueError, match="duplicate.*rank"):
        parse_ranked_holders(payload, target=target)


@pytest.mark.parametrize("payload", [{}, {"holders": []}, (), "[]", None])
def test_parse_ranked_holders_requires_a_top_level_list(payload):
    target = make_target("bsc", TARGET_ADDRESS)
    with pytest.raises(ValueError, match="top-level list"):
        parse_ranked_holders(payload, target=target)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [holder_row(U, None)],
        [
            {
                "address": object(),
                "holder_data": {"rank": None, "amount": 1.5},
                "address_details": None,
            }
        ],
    ],
)
def test_parse_ranked_holders_rejects_empty_or_no_ranked_payload(payload):
    target = make_target("bsc", TARGET_ADDRESS)
    with pytest.raises(ValueError, match="ranked holder"):
        parse_ranked_holders(payload, target=target)


def test_parse_ranked_holders_skips_null_rank_before_unused_validation():
    target = make_target("bsc", TARGET_ADDRESS)
    unused = {
        "address": object(),
        "address_details": {"is_supernode": None, "label": []},
        "holder_data": {"rank": None, "amount": 1.5, "share": float("nan")},
    }

    holders = parse_ranked_holders(
        [unused, holder_row(A, 1)],
        target=target,
    )

    assert [holder.address for holder in holders] == [A]


@pytest.mark.parametrize(
    "payload",
    [
        [None],
        [{}],
        [{"holder_data": None}],
        [{"holder_data": {}}],
        [{"address": A, "holder_data": {"rank": 1}}],
        [
            {
                "address": A,
                "holder_data": {"rank": 1, "amount": "1", "share": "0.1"},
            }
        ],
        [
            {
                "address": A,
                "address_details": {},
                "holder_data": {"rank": 1, "amount": "1", "share": "0.1"},
            }
        ],
    ],
)
def test_parse_ranked_holders_rejects_malformed_ranked_records(payload):
    target = make_target("bsc", TARGET_ADDRESS)
    with pytest.raises(ValueError):
        parse_ranked_holders(payload, target=target)


@pytest.mark.parametrize("rank", [True, False, 0, -1, 1.0, "1"])
def test_parse_ranked_holders_requires_positive_native_integer_rank(rank):
    target = make_target("bsc", TARGET_ADDRESS)
    with pytest.raises(ValueError, match="rank.*positive.*integer"):
        parse_ranked_holders([holder_row(A, rank)], target=target)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", True),
        ("amount", 1.5),
        ("amount", Decimal("1")),
        ("amount", "NaN"),
        ("amount", "Infinity"),
        ("amount", "-0.1"),
        ("share", False),
        ("share", 0.1),
        ("share", Decimal("0.1")),
        ("share", "sNaN"),
        ("share", "-Infinity"),
        ("share", "-0.001"),
    ],
)
def test_parse_ranked_holders_rejects_inexact_nonfinite_or_negative_values(
    field,
    value,
):
    target = make_target("bsc", TARGET_ADDRESS)
    values = {"amount": "1", "share": "0.1", field: value}

    with pytest.raises(ValueError, match=field):
        parse_ranked_holders(
            [holder_row(A, 1, amount=values["amount"], share=values["share"])],
            target=target,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", " 1"),
        ("share", "1_0"),
        ("amount", "9" * 10_001),
        ("share", "1e" + "9" * 50),
        ("amount", "1e10001"),
    ],
)
def test_parse_ranked_holders_enforces_strict_decimal_safeguards(field, value):
    target = make_target("bsc", TARGET_ADDRESS)
    values = {"amount": "1", "share": "0.1", field: value}

    with pytest.raises(ValueError, match=field):
        parse_ranked_holders(
            [holder_row(A, 1, amount=values["amount"], share=values["share"])],
            target=target,
        )


def test_parse_ranked_holders_canonicalizes_decimal_text_and_preserves_scale():
    target = make_target("bsc", TARGET_ADDRESS)

    holder = parse_ranked_holders(
        [holder_row(A, 1, amount="+001.2300", share="1e-10")],
        target=target,
    )[0]

    assert holder.amount == "1.2300"
    assert holder.share == "0.0000000001"
    assert holder.share_percent == "0.0000000100"


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"),
    reason="interpreter has no integer string-conversion limit",
)
def test_parse_ranked_holders_accepts_large_native_int_without_global_conversion():
    target = make_target("bsc", TARGET_ADDRESS)
    previous_limit = sys.get_int_max_str_digits()
    amount = 10**4_999

    try:
        sys.set_int_max_str_digits(4_300)
        holder = parse_ranked_holders(
            [holder_row(A, 1, amount=amount)],
            target=target,
        )[0]
    finally:
        sys.set_int_max_str_digits(previous_limit)

    assert holder.amount == "1" + "0" * 4_999


def test_parse_ranked_holders_uses_a_private_decimal_context():
    target = make_target("bsc", TARGET_ADDRESS)
    with localcontext() as context:
        context.prec = 2
        context.Emax = 0
        context.Emin = 0
        for signal in context.traps:
            context.traps[signal] = True
        holder = parse_ranked_holders(
            [holder_row(A, 1, amount="123.4500", share="0.1001")],
            target=target,
        )[0]

    assert holder.amount == "123.4500"
    assert holder.share_percent == "10.0100"


def test_parse_ranked_holders_projects_only_supported_scalar_metadata():
    target = make_target("bsc", TARGET_ADDRESS)
    row = holder_row(
        A,
        1,
        label="Alpha",
        entity_id=42,
        is_contract=True,
        degree=1.5,
        first_activity_date=None,
        unsupported={"ignored": True},
    )

    holder = parse_ranked_holders([row], target=target)[0]

    assert holder.metadata == {
        "label": "Alpha",
        "entity_id": 42,
        "is_contract": True,
        "degree": 1.5,
        "first_activity_date": None,
    }
    assert "unsupported" not in holder.metadata
    assert "is_supernode" not in holder.metadata


@pytest.mark.parametrize("value", [[], {}, float("nan"), float("inf")])
def test_parse_ranked_holders_rejects_non_scalar_or_nonfinite_metadata(value):
    target = make_target("bsc", TARGET_ADDRESS)
    with pytest.raises(ValueError, match="metadata.*JSON scalar"):
        parse_ranked_holders(
            [holder_row(A, 1, label=value)],
            target=target,
        )


def test_filter_subgraph_edges_selects_exact_target_and_ranked_endpoints(
    official_holders,
    settled_subgraph,
):
    original = deepcopy(settled_subgraph)
    holders, edges = build_snapshot(official_holders, settled_subgraph)

    assert [(edge.from_address, edge.to_address) for edge in edges] == [
        (A, A),
        (A, B),
        (B, C),
        (C, S),
    ]
    assert [edge.total_transfers for edge in edges] == [1, 1, 2, 3]
    assert all(edge.from_address in {holder.address for holder in holders} for edge in edges)
    assert settled_subgraph == original

    source_by_endpoints = {
        (row["from_address"], row["to_address"]): row
        for row in settled_subgraph
    }
    for edge in edges:
        source = source_by_endpoints[(edge.from_address, edge.to_address)]
        assert edge.raw is not source
        assert edge.raw["data"] is not source["data"]
        assert edge.raw["data"]["token_ref"] is not source["data"]["token_ref"]

    edge = edges[0]
    source = source_by_endpoints[(edge.from_address, edge.to_address)]
    source["source_only"] = True
    source["data"]["total_value"] = "source-mutated"
    source["data"]["token_ref"]["address"] = "0x" + "2" * 40
    assert "source_only" not in edge.raw
    assert edge.raw["data"]["total_value"] == "1"
    assert edge.raw["data"]["token_ref"]["address"] == TARGET_ADDRESS

    edge.raw["raw_only"] = True
    edge.raw["data"]["first_date"] = -1
    edge.raw["data"]["token_ref"]["chain"] = "eth"
    assert "raw_only" not in source
    assert source["data"]["first_date"] == 1300
    assert source["data"]["token_ref"]["chain"] == "bsc"


def test_filter_subgraph_edges_initial_is_a_strict_selected_subset(
    official_holders,
    initial_subgraph,
    settled_subgraph,
):
    _, initial = build_snapshot(official_holders, initial_subgraph)
    _, settled = build_snapshot(official_holders, settled_subgraph)

    assert {(edge.from_address, edge.to_address) for edge in initial} < {
        (edge.from_address, edge.to_address) for edge in settled
    }


@pytest.mark.parametrize("payload", [{}, (), None, "[]"])
def test_filter_subgraph_edges_requires_a_top_level_list(
    official_holders,
    payload,
):
    target = make_target("bsc", TARGET_ADDRESS)
    holders = parse_ranked_holders(official_holders, target=target)

    with pytest.raises(ValueError, match="top-level list"):
        filter_subgraph_edges(
            payload,
            target=target,
            holders={holder.address: holder for holder in holders},
        )


def test_filter_subgraph_edges_excludes_non_grouped_foreign_native_and_malformed_rows(
    official_holders,
):
    payload = [
        grouped_edge(A, B, rel_type="TRANSFER", total_transfers=0),
        grouped_edge(
            A,
            B,
            token_ref={
                "chain": "eth",
                "address": TARGET_ADDRESS,
            },
        ),
        grouped_edge(A, B, token_ref={"id": "bsc:native"}),
        grouped_edge("bad-address", B),
        {"from_address": A, "to_address": B, "rel_type": "GROUPED_TRANSFER"},
        None,
    ]

    _, edges = build_snapshot(official_holders, payload)

    assert edges == ()


@pytest.mark.parametrize("total_transfers", [0, -1, True, False, 1.0, "1", None])
def test_filter_subgraph_edges_rejects_invalid_target_transfer_counts(
    official_holders,
    total_transfers,
):
    with pytest.raises(ValueError, match="total_transfers.*positive.*integer"):
        build_snapshot(
            official_holders,
            [grouped_edge(A, B, total_transfers=total_transfers)],
        )


def test_filter_subgraph_edges_collapses_identical_directed_duplicates(
    official_holders,
):
    first = grouped_edge(A, B, source="first")
    duplicate = deepcopy(first)
    duplicate["from_address"] = "0x" + "A" * 40
    duplicate["to_address"] = "0x" + "B" * 40
    duplicate["data"]["token_ref"]["address"] = "0x" + "1" * 40
    duplicate["source"] = "second"

    _, edges = build_snapshot(official_holders, [duplicate, first])

    assert len(edges) == 1
    assert (edges[0].from_address, edges[0].to_address) == (A, B)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total_transfers", 2),
        ("first_date", 101),
        ("last_date", 201),
        ("total_value", "1.500"),
    ],
)
def test_filter_subgraph_edges_rejects_conflicting_directed_duplicates(
    official_holders,
    field,
    value,
):
    first = grouped_edge(A, B)
    conflict = deepcopy(first)
    conflict["data"][field] = value

    with pytest.raises(ValueError, match="conflicting duplicate edge"):
        build_snapshot(official_holders, [first, conflict])


def test_filter_subgraph_edges_retains_reverse_direction_as_a_distinct_edge(
    official_holders,
):
    _, edges = build_snapshot(
        official_holders,
        [grouped_edge(B, A), grouped_edge(A, B)],
    )

    assert [(edge.from_address, edge.to_address) for edge in edges] == [
        (A, B),
        (B, A),
    ]


def test_reconstruct_clusters_uses_ranked_connected_components_only(
    official_holders,
    settled_subgraph,
):
    target = make_target("bsc", TARGET_ADDRESS)
    holders = parse_ranked_holders(official_holders, target=target)
    holder_index = {holder.address: holder for holder in holders}
    edges = filter_subgraph_edges(
        settled_subgraph,
        target=target,
        holders=holder_index,
    )

    clusters = reconstruct_clusters(holders, edges)

    assert [[m.source_rank for m in c.members] for c in clusters] == [
        [1, 2, 3, 4]
    ]
    assert clusters[0].amount == "100"
    assert clusters[0].share == "1.00"
    assert clusters[0].share_percent == "100.00"


def test_unranked_bridge_does_not_connect_ranked_components(official_holders):
    target = make_target("bsc", TARGET_ADDRESS)
    holders = parse_ranked_holders(official_holders, target=target)
    edges = filter_subgraph_edges(
        [grouped_edge(A, U), grouped_edge(U, C)],
        target=target,
        holders={holder.address: holder for holder in holders},
    )

    assert edges == ()
    assert reconstruct_clusters(holders, edges) == ()


def test_self_edges_and_isolated_ranked_holders_do_not_form_clusters(
    official_holders,
):
    holders, edges = build_snapshot(official_holders, [grouped_edge(A, A)])

    assert len(edges) == 1
    assert reconstruct_clusters(holders, edges) == ()


def test_valid_subgraph_with_no_accepted_ranked_edge_yields_no_clusters(
    official_holders,
):
    holders, edges = build_snapshot(
        official_holders,
        [
            grouped_edge(A, U),
            grouped_edge(
                A,
                B,
                token_ref={
                    "chain": "bsc",
                    "address": "0x9999999999999999999999999999999999999999",
                },
            ),
        ],
    )

    assert edges == ()
    assert reconstruct_clusters(holders, edges) == ()


def test_reconstruct_clusters_uses_all_deterministic_sort_keys():
    addresses = {
        letter: "0x" + digit * 40
        for letter, digit in zip("abcdefgh", "12345678", strict=True)
    }
    payload = [
        holder_row(addresses["a"], 8, amount="15", share="0.15"),
        holder_row(addresses["b"], 2, amount="15", share="0.15"),
        holder_row(addresses["c"], 3, amount="10", share="0.09"),
        holder_row(addresses["d"], 4, amount="30", share="0.01"),
        holder_row(addresses["e"], 5, amount="15", share="0.10"),
        holder_row(addresses["f"], 6, amount="15", share="0.30"),
        holder_row(addresses["g"], 7, amount="15", share="0.15"),
        holder_row(addresses["h"], 1, amount="15", share="0.15"),
    ]
    edge_payload = [
        grouped_edge(addresses["h"], addresses["g"]),
        grouped_edge(addresses["a"], addresses["b"]),
        grouped_edge(addresses["f"], addresses["e"]),
        grouped_edge(addresses["c"], addresses["d"]),
    ]
    holders, edges = build_snapshot(payload, edge_payload)

    clusters = reconstruct_clusters(holders, reversed(edges))

    assert [cluster.cluster_rank for cluster in clusters] == [1, 2, 3, 4]
    assert [[member.address for member in cluster.members] for cluster in clusters] == [
        [addresses["d"], addresses["c"]],
        [addresses["f"], addresses["e"]],
        [addresses["b"], addresses["a"]],
        [addresses["h"], addresses["g"]],
    ]
    assert [(cluster.amount, cluster.share) for cluster in clusters] == [
        ("40", "0.10"),
        ("30", "0.40"),
        ("30", "0.30"),
        ("30", "0.30"),
    ]


def test_reconstruct_clusters_uses_private_decimal_context():
    holders, edges = build_snapshot(
        [
            holder_row(A, 1, amount="123.4500", share="0.1001"),
            holder_row(B, 2, amount="0.5500", share="0.2002"),
        ],
        [grouped_edge(A, B)],
    )

    with localcontext() as context:
        context.prec = 2
        context.Emax = 0
        context.Emin = 0
        for signal in context.traps:
            context.traps[signal] = True
        cluster = reconstruct_clusters(holders, edges)[0]

    assert cluster.amount == "124.0000"
    assert cluster.share == "0.3003"
    assert cluster.share_percent == "30.0300"


def test_snapshot_fingerprint_ignores_order_case_and_excluded_rows(
    official_holders,
    settled_subgraph,
):
    first = build_snapshot(official_holders, settled_subgraph)
    upper_holders = deepcopy(list(reversed(official_holders)))
    for row in upper_holders:
        row["address"] = uppercase_evm(row["address"])
    second = build_snapshot(
        upper_holders,
        add_foreign_rows_and_uppercase_evm(list(reversed(settled_subgraph))),
    )

    assert token_snapshot_fingerprint(*first) == token_snapshot_fingerprint(*second)


def test_snapshot_fingerprint_changes_for_formal_holder_or_edge_change(
    official_holders,
    settled_subgraph,
):
    baseline = token_snapshot_fingerprint(
        *build_snapshot(official_holders, settled_subgraph)
    )
    changed = change_ranked_holder_amount(
        official_holders,
        rank=2,
        amount="31",
    )

    assert token_snapshot_fingerprint(*build_snapshot(changed, settled_subgraph)) != baseline


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("address", "0x9999999999999999999999999999999999999999"),
        ("source_rank", 99),
        ("amount", "41"),
        ("share", "0.41"),
        ("share_percent", "41.00"),
        ("is_supernode", True),
        ("metadata", {"label": "Changed"}),
    ],
)
def test_snapshot_fingerprint_includes_every_formal_holder_field(
    official_holders,
    field,
    value,
):
    holders, edges = build_snapshot(official_holders, [])
    baseline = token_snapshot_fingerprint(holders, edges)
    changed_holders = (
        replace(holders[0], **{field: value}),
        *holders[1:],
    )

    assert token_snapshot_fingerprint(changed_holders, edges) != baseline


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total_transfers", 10),
        ("first_date", 9999),
        ("last_date", 9999),
        ("total_value", "15.50"),
    ],
)
def test_snapshot_fingerprint_includes_every_formal_edge_field(
    official_holders,
    settled_subgraph,
    field,
    value,
):
    baseline = token_snapshot_fingerprint(
        *build_snapshot(official_holders, settled_subgraph)
    )
    changed = deepcopy(settled_subgraph)
    changed[0]["data"][field] = value

    assert token_snapshot_fingerprint(*build_snapshot(official_holders, changed)) != baseline


def test_snapshot_fingerprint_includes_edge_direction(
    official_holders,
):
    forward = build_snapshot(official_holders, [grouped_edge(A, B)])
    reverse = build_snapshot(official_holders, [grouped_edge(B, A)])

    assert token_snapshot_fingerprint(*forward) != token_snapshot_fingerprint(*reverse)


@pytest.mark.parametrize(("field", "value"), SUPPORTED_METADATA_VALUES)
def test_snapshot_fingerprint_includes_every_supported_metadata_field(
    official_holders,
    field,
    value,
):
    changed = deepcopy(official_holders)
    changed[0]["address_details"][field] = value
    baseline = build_snapshot(official_holders, [])
    changed_snapshot = build_snapshot(changed, [])

    assert changed_snapshot[0][0].metadata[field] == value
    assert token_snapshot_fingerprint(*baseline) != token_snapshot_fingerprint(
        *changed_snapshot
    )


def test_snapshot_fingerprint_ignores_unknown_edge_raw_fields(
    official_holders,
):
    first = grouped_edge(A, B, observation="first")
    second = grouped_edge(A, B, observation={"different": [1, 2, 3]})

    assert token_snapshot_fingerprint(*build_snapshot(official_holders, [first])) == token_snapshot_fingerprint(
        *build_snapshot(official_holders, [second])
    )


def test_snapshot_fingerprint_is_a_sha256_hex_digest(official_holders):
    fingerprint = token_snapshot_fingerprint(
        *build_snapshot(official_holders, [])
    )

    assert len(fingerprint) == 64
    assert set(fingerprint) <= set("0123456789abcdef")
