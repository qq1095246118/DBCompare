import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import get_type_hints

import pytest

from common.artifacts import safe_path_component
from getMarket.bubblemaps.tool.market_identity import make_target
from getMarket.bubblemaps.tool.market_transform import (
    Cluster,
    RankedHolder,
    SnapshotModel,
    SubgraphEdge,
)
from getMarket.bubblemaps.tool.transfer_transform import (
    AmbiguousTransferIdentityError,
    TransferCompletenessError,
    TransferResult,
    build_transfer_result,
)


FIXTURES = Path(__file__).parent / "fixtures"
TARGET_ADDRESS = "0x1111111111111111111111111111111111111111"
A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
C = "0xcccccccccccccccccccccccccccccccccccccccc"
S = "0xdddddddddddddddddddddddddddddddddddddddd"
OUTSIDE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
CAPTURED_AT = "2026-07-22T12:00:00Z"


def load_fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def holder(
    address: str,
    source_rank: int,
    *,
    amount: str,
    share: str,
    share_percent: str,
    is_supernode: bool = False,
    metadata: dict | None = None,
) -> RankedHolder:
    return RankedHolder(
        address=address,
        source_rank=source_rank,
        amount=amount,
        share=share,
        share_percent=share_percent,
        is_supernode=is_supernode,
        metadata=metadata or {},
    )


def edge(
    from_address: str,
    to_address: str,
    total_transfers: int,
) -> SubgraphEdge:
    return SubgraphEdge(
        from_address=from_address,
        to_address=to_address,
        total_transfers=total_transfers,
        raw={"fixture": True},
    )


def transfer(
    from_address: str,
    to_address: str,
    *,
    tx_hash: str = "0xfeed",
    date: object = 1,
    value: object = "1.0",
    token_ref: object | None = None,
    rel_type: str = "TRANSFER",
    **extra: object,
) -> dict:
    return {
        "from_address": from_address,
        "to_address": to_address,
        "rel_type": rel_type,
        "data": {
            "value": value,
            "date": date,
            "tx_hash": tx_hash,
            "token_ref": token_ref
            if token_ref is not None
            else {"chain": "bsc", "address": TARGET_ADDRESS},
        },
        **extra,
    }


def one_cluster_snapshot(
    members: tuple[RankedHolder, ...],
    edges: tuple[SubgraphEdge, ...],
) -> SnapshotModel:
    cluster = Cluster(
        cluster_rank=1,
        amount="100",
        share="0.5",
        share_percent="50",
        members=members,
    )
    return SnapshotModel(
        target=make_target("bsc", TARGET_ADDRESS),
        holders=members,
        edges=edges,
        clusters=(cluster,),
        fingerprint="fixture",
        captured_at=CAPTURED_AT,
    )


@pytest.fixture
def snapshot() -> SnapshotModel:
    members = (
        holder(
            A,
            1,
            amount="40",
            share="0.2",
            share_percent="20",
            metadata={"label": "A", "address": "metadata-only"},
        ),
        holder(
            B,
            2,
            amount="30",
            share="0.15",
            share_percent="15",
            metadata={"label": "B"},
        ),
        holder(
            C,
            3,
            amount="20",
            share="0.1",
            share_percent="10",
            metadata={"label": "C"},
        ),
        holder(
            S,
            4,
            amount="10",
            share="0.05",
            share_percent="5",
            is_supernode=True,
            metadata={"label": "Supernode"},
        ),
    )
    return one_cluster_snapshot(
        members,
        (
            edge(A, B, 1),
            edge(B, A, 1),
            edge(B, C, 1),
            edge(C, S, 7),
            edge(S, C, 11),
        ),
    )


@pytest.fixture
def payloads() -> dict[str, list[dict]]:
    return load_fixture("official_member_transfers.json")


def build(snapshot: SnapshotModel, payloads: Mapping[str, object]):
    return build_transfer_result(
        payloads,
        target=snapshot.target,
        clusters=snapshot.clusters,
        edges=snapshot.edges,
        captured_at=CAPTURED_AT,
    )


def assert_no_decimal(value: object) -> None:
    if isinstance(value, Mapping):
        for child in value.values():
            assert_no_decimal(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            assert_no_decimal(child)
    else:
        assert not isinstance(value, Decimal)


def test_transfer_result_public_contract_is_exact_and_frozen():
    assert [field.name for field in fields(TransferResult)] == [
        "token_document",
        "member_documents",
        "unique_transfer_count",
        "transfer_view_count",
        "count_drifts",
    ]
    assert get_type_hints(TransferResult) == {
        "token_document": dict,
        "member_documents": dict[str, dict],
        "unique_transfer_count": int,
        "transfer_view_count": int,
        "count_drifts": tuple[dict, ...],
    }
    assert TransferResult.__dataclass_params__.frozen is True
    result = TransferResult({}, {}, 0, 0, ())
    with pytest.raises(FrozenInstanceError):
        result.unique_transfer_count = 1
    assert issubclass(AmbiguousTransferIdentityError, ValueError)
    assert issubclass(TransferCompletenessError, ValueError)


def test_build_transfer_result_filters_and_assigns_member_views(
    snapshot,
    payloads,
):
    original = deepcopy(payloads)

    result = build_transfer_result(
        payloads,
        target=snapshot.target,
        clusters=snapshot.clusters,
        edges=snapshot.edges,
        captured_at=CAPTURED_AT,
    )

    token_document = result.token_document
    member_documents = result.member_documents
    assert set(member_documents) == {A, B, C}
    assert member_documents[A]["transfer_count"] == 3
    assert member_documents[B]["transfer_count"] == 3
    assert member_documents[C]["transfer_count"] == 3
    assert result.unique_transfer_count == 6
    assert result.transfer_view_count == 9
    assert S not in member_documents
    assert all(
        "member_role" not in row
        for doc in member_documents.values()
        for row in doc["transfers"]
    )
    supernode = token_document["clusters"][0]["members"][-1]
    assert supernode["address"] == S
    assert supernode["is_supernode"] is True
    assert supernode["transfer_details_available"] is False
    assert supernode["transfer_details_reason"] == "supernode_not_supported"
    assert supernode["transfer_count"] == 0
    assert supernode["transfer_file"] is None

    shared = original[A][0]
    assert shared in member_documents[A]["transfers"]
    assert shared in member_documents[B]["transfers"]
    for member_address in (A, B):
        saved = next(
            row
            for row in member_documents[member_address]["transfers"]
            if row["data"]["tx_hash"] == shared["data"]["tx_hash"]
        )
        assert saved == shared
        assert saved["from_address"] == A
        assert saved["to_address"] == B
        assert "member_role" not in saved

    assert [
        row["data"]["date"] for row in member_documents[B]["transfers"]
    ] == sorted(
        (row["data"]["date"] for row in member_documents[B]["transfers"]),
        reverse=True,
    )
    assert payloads == original
    assert_no_decimal(token_document)
    assert_no_decimal(member_documents)


def test_build_transfer_result_marks_unavailable_ordinary_member_without_file(
    snapshot,
    payloads,
):
    result = build_transfer_result(
        {B: payloads[B], C: payloads[C]},
        target=snapshot.target,
        clusters=snapshot.clusters,
        edges=snapshot.edges,
        captured_at=CAPTURED_AT,
        unavailable_members={A},
    )

    assert set(result.member_documents) == {B, C}
    unavailable = result.token_document["clusters"][0]["members"][0]
    assert unavailable["address"] == A
    assert unavailable["is_supernode"] is False
    assert unavailable["transfer_details_available"] is False
    assert unavailable["transfer_details_reason"] == "capture_failed"
    assert unavailable["transfer_count"] == 0
    assert unavailable["transfer_file"] is None


def test_documents_have_requested_canonical_cluster_and_file_contracts(
    snapshot,
    payloads,
):
    result = build(snapshot, payloads)
    token = result.token_document

    assert token["schema_version"] == "v2"
    assert token["chain"] == snapshot.target.requested_chain
    assert token["token_address"] == snapshot.target.requested_token_address
    assert token["canonical_chain"] == snapshot.target.chain
    assert token["canonical_token_address"] == snapshot.target.token_address
    assert token["captured_at"] == CAPTURED_AT
    assert "transfers" not in token

    cluster = token["clusters"][0]
    assert {key: value for key, value in cluster.items() if key != "members"} == {
        "cluster_rank": 1,
        "amount": "100",
        "share": "0.5",
        "share_percent": "50",
        "member_count": 4,
    }
    assert len(cluster["members"]) == len(snapshot.clusters[0].members)
    for member_rank, (summary, source_holder) in enumerate(
        zip(cluster["members"], snapshot.clusters[0].members, strict=True),
        start=1,
    ):
        expected = {
            "member_rank": member_rank,
            "source_rank": source_holder.source_rank,
            "address": source_holder.address,
            "amount": source_holder.amount,
            "share": source_holder.share,
            "share_percent": source_holder.share_percent,
            "is_supernode": source_holder.is_supernode,
            "metadata": source_holder.metadata,
        }
        if source_holder.is_supernode:
            expected.update(
                {
                    "transfer_details_available": False,
                    "transfer_details_reason": "supernode_not_supported",
                    "transfer_count": 0,
                    "transfer_file": None,
                }
            )
            assert source_holder.address not in result.member_documents
        else:
            document = result.member_documents[source_holder.address]
            transfer_file = (
                f"transfers/{safe_path_component(source_holder.address)}.json"
            )
            expected.update(
                {
                    "transfer_details_available": True,
                    "transfer_count": document["transfer_count"],
                    "transfer_file": transfer_file,
                }
            )
            assert "transfer_details_reason" not in summary
            assert document["schema_version"] == "v2"
            assert document["chain"] == snapshot.target.requested_chain
            assert (
                document["token_address"]
                == snapshot.target.requested_token_address
            )
            assert document["canonical_chain"] == snapshot.target.chain
            assert (
                document["canonical_token_address"]
                == snapshot.target.token_address
            )
            assert document["cluster_rank"] == 1
            assert document["member_address"] == source_holder.address
            assert document["transfer_count"] == len(document["transfers"])
        assert summary == expected


@pytest.mark.parametrize("expected_total", [0, 2])
def test_ordinary_directed_edge_count_must_match(snapshot, payloads, expected_total):
    changed_edges = tuple(
        replace(edge_value, total_transfers=expected_total)
        if (edge_value.from_address, edge_value.to_address) == (A, B)
        else edge_value
        for edge_value in snapshot.edges
    )

    with pytest.raises(TransferCompletenessError, match=f"{A}.*{B}"):
        build_transfer_result(
            payloads,
            target=snapshot.target,
            clusters=snapshot.clusters,
            edges=changed_edges,
            captured_at=CAPTURED_AT,
        )


def test_newer_transfers_after_subgraph_snapshot_are_retained_as_count_drift(
    snapshot,
    payloads,
):
    changed_payloads = deepcopy(payloads)
    old_date = changed_payloads[A][0]["data"]["date"]
    new_transfer = transfer(
        A,
        B,
        tx_hash="0xnew-after-subgraph",
        date=old_date + 1,
    )
    changed_payloads[A].append(deepcopy(new_transfer))
    changed_payloads[B].append(deepcopy(new_transfer))
    changed_edges = tuple(
        replace(
            edge_value,
            raw={"data": {"last_date": old_date}},
        )
        if (edge_value.from_address, edge_value.to_address) == (A, B)
        else edge_value
        for edge_value in snapshot.edges
    )

    result = build_transfer_result(
        changed_payloads,
        target=snapshot.target,
        clusters=snapshot.clusters,
        edges=changed_edges,
        captured_at=CAPTURED_AT,
    )

    assert result.count_drifts == (
        {
            "from_address": A,
            "to_address": B,
            "expected_count": 1,
            "captured_count": 2,
            "edge_last_date": old_date,
        },
    )
    assert any(
        row["data"]["tx_hash"] == "0xnew-after-subgraph"
        for row in result.member_documents[A]["transfers"]
    )


def test_extra_transfer_at_or_before_subgraph_last_date_is_rejected(
    snapshot,
    payloads,
):
    changed_payloads = deepcopy(payloads)
    old_date = changed_payloads[A][0]["data"]["date"]
    old_transfer = transfer(
        A,
        B,
        tx_hash="0xunexpected-old-transfer",
        date=old_date,
    )
    changed_payloads[A].append(deepcopy(old_transfer))
    changed_payloads[B].append(deepcopy(old_transfer))
    changed_edges = tuple(
        replace(
            edge_value,
            raw={"data": {"last_date": old_date}},
        )
        if (edge_value.from_address, edge_value.to_address) == (A, B)
        else edge_value
        for edge_value in snapshot.edges
    )

    with pytest.raises(TransferCompletenessError, match=f"{A}.*{B}"):
        build_transfer_result(
            changed_payloads,
            target=snapshot.target,
            clusters=snapshot.clusters,
            edges=changed_edges,
            captured_at=CAPTURED_AT,
        )


def test_directed_completeness_does_not_accept_same_undirected_total(
    snapshot,
    payloads,
):
    changed_edges = tuple(
        replace(edge_value, total_transfers=2)
        if (edge_value.from_address, edge_value.to_address) == (A, B)
        else replace(edge_value, total_transfers=0)
        if (edge_value.from_address, edge_value.to_address) == (B, A)
        else edge_value
        for edge_value in snapshot.edges
    )

    with pytest.raises(TransferCompletenessError):
        build_transfer_result(
            payloads,
            target=snapshot.target,
            clusters=snapshot.clusters,
            edges=changed_edges,
            captured_at=CAPTURED_AT,
        )


def test_actual_ordinary_pair_without_expected_edge_is_recorded_as_omission(
    snapshot,
    payloads,
):
    changed_edges = tuple(
        edge_value
        for edge_value in snapshot.edges
        if (edge_value.from_address, edge_value.to_address) != (A, B)
    )

    result = build_transfer_result(
        payloads,
        target=snapshot.target,
        clusters=snapshot.clusters,
        edges=changed_edges,
        captured_at=CAPTURED_AT,
    )

    assert result.count_drifts == (
        {
            "from_address": A,
            "to_address": B,
            "expected_count": 0,
            "captured_count": 1,
            "edge_last_date": None,
        },
    )


def test_actual_ordinary_pair_with_explicit_zero_edge_is_rejected(snapshot, payloads):
    changed_edges = tuple(
        replace(edge_value, total_transfers=0)
        if (edge_value.from_address, edge_value.to_address) == (A, B)
        else edge_value
        for edge_value in snapshot.edges
    )

    with pytest.raises(TransferCompletenessError, match=f"{A}.*{B}"):
        build_transfer_result(
            payloads,
            target=snapshot.target,
            clusters=snapshot.clusters,
            edges=changed_edges,
            captured_at=CAPTURED_AT,
        )


def test_supernode_edge_totals_are_not_asserted(snapshot, payloads):
    changed_edges = tuple(
        replace(edge_value, total_transfers=edge_value.total_transfers + 1_000_000)
        if edge_value.from_address == S or edge_value.to_address == S
        else edge_value
        for edge_value in snapshot.edges
    )

    result = build_transfer_result(
        payloads,
        target=snapshot.target,
        clusters=snapshot.clusters,
        edges=changed_edges,
        captured_at=CAPTURED_AT,
    )

    assert result.unique_transfer_count == 6


def test_identical_repeated_fallback_identity_within_one_response_collapses(
    snapshot,
    payloads,
):
    changed = deepcopy(payloads)
    changed[A].append(deepcopy(changed[A][0]))

    result = build(snapshot, changed)

    assert result.unique_transfer_count == 6
    assert result.transfer_view_count == 9


def test_conflicting_repeated_fallback_identity_within_one_response_is_ambiguous():
    member = holder(A, 1, amount="1", share="1", share_percent="100")
    snapshot = one_cluster_snapshot((member,), (edge(A, A, 1),))
    first = transfer(A, A, extra_source="first")
    conflicting = transfer(A, A, extra_source="second")

    with pytest.raises(AmbiguousTransferIdentityError):
        build(snapshot, {A: [first, conflicting]})


def test_equivalent_decimal_spellings_share_the_fallback_identity():
    a = holder(A, 1, amount="1", share="1", share_percent="100")
    snapshot = one_cluster_snapshot((a,), (edge(A, A, 1),))
    first = transfer(A, A, value="1.0")
    second = transfer(A, A, value="1e0")

    with pytest.raises(AmbiguousTransferIdentityError):
        build(snapshot, {A: [first, second]})


def test_identical_duplicate_views_for_both_ordinary_members_collapse():
    members = (
        holder(A, 1, amount="2", share="0.5", share_percent="50"),
        holder(B, 2, amount="2", share="0.5", share_percent="50"),
    )
    snapshot = one_cluster_snapshot(members, (edge(A, B, 1),))
    row = transfer(A, B)

    result = build(
        snapshot,
        {
            A: [deepcopy(row), deepcopy(row)],
            B: [deepcopy(row), deepcopy(row)],
        },
    )

    assert result.unique_transfer_count == 1
    assert result.transfer_view_count == 2
    assert result.member_documents[A]["transfers"] == [row]
    assert result.member_documents[B]["transfers"] == [row]


def test_conflicting_source_objects_for_cross_response_identity_are_ambiguous():
    members = (
        holder(A, 1, amount="2", share="0.5", share_percent="50"),
        holder(B, 2, amount="2", share="0.5", share_percent="50"),
    )
    snapshot = one_cluster_snapshot(members, (edge(A, B, 1),))
    first = transfer(A, B, extra_source="first")
    conflicting = transfer(A, B, extra_source="second")

    with pytest.raises(AmbiguousTransferIdentityError):
        build(snapshot, {A: [first], B: [conflicting]})


def test_tuple_and_list_source_values_cannot_collapse_as_identical_json():
    members = (
        holder(A, 1, amount="2", share="0.5", share_percent="50"),
        holder(B, 2, amount="2", share="0.5", share_percent="50"),
    )
    snapshot = one_cluster_snapshot(members, (edge(A, B, 1),))
    tuple_source = transfer(A, B, extra=(1, 2))
    list_source = transfer(A, B, extra=[1, 2])

    with pytest.raises(ValueError, match="JSON"):
        build(snapshot, {A: [tuple_source], B: [list_source]})


def test_non_string_json_object_key_is_rejected():
    member = holder(A, 1, amount="1", share="1", share_percent="100")
    snapshot = one_cluster_snapshot((member,), (edge(A, A, 1),))
    row = transfer(A, A)
    row["extra"] = {1: "integer key must not stringify"}

    with pytest.raises(ValueError, match="JSON"):
        build(snapshot, {A: [row]})


def test_cyclic_source_object_is_reported_as_value_error():
    member = holder(A, 1, amount="1", share="1", share_percent="100")
    snapshot = one_cluster_snapshot((member,), (edge(A, A, 1),))
    row = transfer(A, A)
    row["extra"] = row

    with pytest.raises(ValueError, match="JSON"):
        build(snapshot, {A: [row]})


@pytest.mark.parametrize(
    ("first_value", "second_value"),
    [
        ("1e10001", "10e10000"),
        ("1e-10001", "10e-10002"),
    ],
    ids=("large-exponent", "small-exponent"),
)
def test_unbounded_equivalent_decimals_share_compact_fallback_identity(
    first_value,
    second_value,
):
    member = holder(A, 1, amount="1", share="1", share_percent="100")
    snapshot = one_cluster_snapshot((member,), (edge(A, A, 1),))
    first = transfer(A, A, value=first_value)
    second = transfer(A, A, value=second_value)

    with pytest.raises(AmbiguousTransferIdentityError):
        build(snapshot, {A: [first, second]})


@pytest.mark.parametrize(
    ("first_value", "second_value"),
    [
        ("1e9999999999999999999", "10e9999999999999999998"),
        ("1e-9999999999999999999", "10e-10000000000000000000"),
    ],
    ids=("above-decimal-max-emax", "below-decimal-min-etiny"),
)
def test_string_exponents_beyond_decimal_runtime_limits_are_exact(
    first_value,
    second_value,
):
    member = holder(A, 1, amount="1", share="1", share_percent="100")
    snapshot = one_cluster_snapshot((member,), (edge(A, A, 1),))

    with pytest.raises(AmbiguousTransferIdentityError):
        build(
            snapshot,
            {
                A: [
                    transfer(A, A, value=first_value),
                    transfer(A, A, value=second_value),
                ]
            },
        )


def test_string_exponent_identity_avoids_runtime_integer_digit_limit():
    member = holder(A, 1, amount="1", share="1", share_percent="100")
    snapshot = one_cluster_snapshot((member,), (edge(A, A, 1),))
    nines = "9" * 5_000
    power_of_ten = "1" + "0" * 5_000

    with pytest.raises(AmbiguousTransferIdentityError):
        build(
            snapshot,
            {
                A: [
                    transfer(A, A, value="10e" + nines),
                    transfer(A, A, value="1e" + power_of_ten),
                ]
            },
        )


def test_decimal_coefficient_has_no_undocumented_length_or_precision_limit():
    member = holder(A, 1, amount="1", share="1", share_percent="100")
    snapshot = one_cluster_snapshot((member,), (edge(A, A, 1),))
    value = "1" * 20_001
    row = transfer(A, A, value=value)

    result = build(snapshot, {A: [row]})

    assert result.member_documents[A]["transfers"][0]["data"]["value"] == value


def test_native_large_integer_value_avoids_runtime_string_digit_limit():
    member = holder(A, 1, amount="1", share="1", share_percent="100")
    snapshot = one_cluster_snapshot((member,), (edge(A, A, 1),))
    value = 10**5_000 + 1
    row = transfer(A, A, value=value)

    result = build(snapshot, {A: [row]})

    assert result.member_documents[A]["transfers"][0]["data"]["value"] == value


def test_target_row_must_contain_the_requested_capture_member(snapshot, payloads):
    changed = deepcopy(payloads)
    changed[C].append(transfer(A, B, tx_hash="0xnot-captured-by-c"))

    with pytest.raises(ValueError, match="capture member"):
        build(snapshot, changed)


def test_missing_member_payload_is_rejected_before_rows_are_processed(
    snapshot,
    payloads,
):
    changed = deepcopy(payloads)
    del changed[C]
    changed[A] = ["malformed row that must not be inspected"]

    with pytest.raises(ValueError, match="payload.*keys|member.*set"):
        build(snapshot, changed)


@pytest.mark.parametrize("extra_key", [S, OUTSIDE])
def test_supernode_or_nonmember_payload_is_rejected_before_row_processing(
    snapshot,
    payloads,
    extra_key,
):
    changed = deepcopy(payloads)
    changed[extra_key] = []
    changed[A] = ["malformed row that must not be inspected"]

    with pytest.raises(ValueError, match="payload.*keys|member.*set"):
        build(snapshot, changed)


@pytest.mark.parametrize(
    "replacement_key",
    ["0x" + "A" * 40, "not-an-address", ""],
)
def test_payload_keys_are_not_silently_normalized(snapshot, payloads, replacement_key):
    changed = deepcopy(payloads)
    changed[replacement_key] = changed.pop(A)

    with pytest.raises(ValueError, match="payload.*keys|member.*set"):
        build(snapshot, changed)


def test_payload_universe_must_be_a_mapping(snapshot):
    with pytest.raises(ValueError, match="mapping"):
        build(snapshot, [])


@pytest.mark.parametrize("malformed", [{}, (), "[]", None])
def test_each_member_response_must_be_a_top_level_array(
    snapshot,
    payloads,
    malformed,
):
    changed = deepcopy(payloads)
    changed[A] = malformed

    with pytest.raises(ValueError, match="top-level list|array"):
        build(snapshot, changed)


def test_malformed_candidate_row_fails_closed(snapshot, payloads):
    changed = deepcopy(payloads)
    changed[A].append("not an object")

    with pytest.raises(ValueError, match="row.*object"):
        build(snapshot, changed)


@pytest.mark.parametrize(
    "row",
    [
        {"rel_type": "TRANSFER"},
        {"rel_type": "TRANSFER", "data": []},
    ],
)
def test_transfer_row_requires_a_data_object(snapshot, payloads, row):
    changed = deepcopy(payloads)
    changed[A].append(row)

    with pytest.raises(ValueError, match="data.*object"):
        build(snapshot, changed)


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("tx_hash", ""),
        ("tx_hash", 123),
        ("date", -1),
        ("date", True),
        ("date", "1"),
        ("value", "-0.1"),
        ("value", 1.5),
        ("value", True),
        ("value", Decimal("1")),
        ("value", "NaN"),
        ("value", "Infinity"),
        ("value", " 1"),
        ("value", ""),
    ],
)
def test_formal_transfer_data_fails_closed(snapshot, payloads, field, malformed):
    changed = deepcopy(payloads)
    row = deepcopy(changed[A][0])
    row["data"][field] = malformed
    changed[A][0] = row
    changed[B][0] = deepcopy(row)

    with pytest.raises(ValueError, match=field):
        build(snapshot, changed)


def test_external_endpoint_row_requires_formal_data_validation(
    snapshot,
    payloads,
):
    changed = deepcopy(payloads)
    outside_row = next(row for row in changed[A] if row["to_address"] == OUTSIDE)
    outside_row["data"]["date"] = "malformed-but-irrelevant"
    outside_row["data"]["tx_hash"] = ""

    with pytest.raises(ValueError, match="date|tx_hash"):
        build(snapshot, changed)


def test_external_sender_and_receiver_transfers_are_retained_for_member_view():
    member = holder(A, 1, amount="1", share="1", share_percent="100")
    external_sender = "0x" + "E" * 40
    external_receiver = "0x" + "F" * 40
    snapshot = one_cluster_snapshot(
        (member,),
        (edge(A, external_receiver, 99),),
    )
    incoming = transfer(external_sender, A, tx_hash="0xincoming", date=2)
    outgoing = transfer(A, external_receiver, tx_hash="0xoutgoing", date=1)

    result = build(snapshot, {A: [incoming, outgoing]})

    assert result.unique_transfer_count == 2
    assert result.transfer_view_count == 2
    assert set(result.member_documents) == {A}
    assert result.member_documents[A]["transfers"] == [incoming, outgoing]
    assert result.token_document["clusters"][0]["member_count"] == 1
    assert result.token_document["clusters"][0]["members"][0]["address"] == A


def test_malformed_token_refs_and_endpoints_are_excluded(snapshot, payloads):
    changed = deepcopy(payloads)
    changed[A].extend(
        [
            transfer(A, B, token_ref={"chain": "bsc"}),
            transfer("not-an-address", B),
            {"rel_type": "IGNORED", "data": None},
        ]
    )

    result = build(snapshot, changed)

    assert result.unique_transfer_count == 6


def test_nonfinite_or_non_json_source_data_fails_closed(snapshot, payloads):
    changed = deepcopy(payloads)
    changed[A][0]["extra"] = float("nan")
    changed[B][0] = deepcopy(changed[A][0])

    with pytest.raises(ValueError, match="JSON"):
        build(snapshot, changed)


def test_self_transfer_is_assigned_once_to_the_member_view():
    member = holder(A, 1, amount="1", share="1", share_percent="100")
    snapshot = one_cluster_snapshot((member,), (edge(A, A, 1),))
    row = transfer(A, A, tx_hash="0xself")

    result = build(snapshot, {A: [row]})

    assert result.unique_transfer_count == 1
    assert result.transfer_view_count == 1
    assert result.member_documents[A]["transfer_count"] == 1
    assert result.member_documents[A]["transfers"] == [row]


def test_equal_date_transfers_use_fallback_identity_as_secondary_sort():
    members = (
        holder(A, 1, amount="2", share="0.5", share_percent="50"),
        holder(B, 2, amount="2", share="0.5", share_percent="50"),
    )
    snapshot = one_cluster_snapshot(members, (edge(A, B, 2),))
    later_identity = transfer(A, B, tx_hash="0xb", date=100)
    earlier_identity = transfer(A, B, tx_hash="0xa", date=100)
    reverse_identity_order = [later_identity, earlier_identity]

    result = build(
        snapshot,
        {
            A: deepcopy(reverse_identity_order),
            B: deepcopy(reverse_identity_order),
        },
    )

    for member_address in (A, B):
        assert [
            row["data"]["tx_hash"]
            for row in result.member_documents[member_address]["transfers"]
        ] == ["0xa", "0xb"]


def test_raw_endpoint_case_and_value_lexeme_are_preserved():
    members = (
        holder(A, 1, amount="2", share="0.5", share_percent="50"),
        holder(B, 2, amount="2", share="0.5", share_percent="50"),
    )
    snapshot = one_cluster_snapshot(members, (edge(A, B, 1),))
    uppercase_a = "0x" + "A" * 40
    uppercase_b = "0x" + "B" * 40
    row = transfer(uppercase_a, uppercase_b, value="1.00")

    result = build(
        snapshot,
        {A: [deepcopy(row)], B: [deepcopy(row)]},
    )

    for member_address in (A, B):
        saved = result.member_documents[member_address]["transfers"][0]
        assert saved["from_address"] == uppercase_a
        assert saved["to_address"] == uppercase_b
        assert saved["data"]["value"] == "1.00"


def test_full_fallback_identity_sorts_by_value_after_equal_prior_fields():
    members = (
        holder(A, 1, amount="2", share="0.5", share_percent="50"),
        holder(B, 2, amount="2", share="0.5", share_percent="50"),
    )
    snapshot = one_cluster_snapshot(members, (edge(A, B, 2),))
    value_two = transfer(A, B, tx_hash="0xsame", date=100, value="2.00")
    value_one = transfer(A, B, tx_hash="0xsame", date=100, value="1.0")
    reverse_value_order = [value_two, value_one]

    result = build(
        snapshot,
        {
            A: deepcopy(reverse_value_order),
            B: deepcopy(reverse_value_order),
        },
    )

    for member_address in (A, B):
        assert [
            row["data"]["value"]
            for row in result.member_documents[member_address]["transfers"]
        ] == ["1.0", "2.00"]


def test_requested_and_canonical_token_identity_are_both_preserved():
    requested_token = "0xC61F0667076521761FB365F52644572E92FD0C94"
    canonical_token = requested_token.lower()
    target = make_target("ethereum", requested_token)
    member = holder(A, 1, amount="1", share="1", share_percent="100")
    cluster = Cluster(1, "1", "1", "100", (member,))
    row = transfer(
        A,
        A,
        token_ref={"chain": "eth", "address": canonical_token},
    )

    result = build_transfer_result(
        {A: [row]},
        target=target,
        clusters=(cluster,),
        edges=(edge(A, A, 1),),
        captured_at=CAPTURED_AT,
    )

    for document in (result.token_document, result.member_documents[A]):
        assert document["chain"] == "ethereum"
        assert document["token_address"] == requested_token
        assert document["canonical_chain"] == "eth"
        assert document["canonical_token_address"] == canonical_token


def test_returned_raw_transfer_is_owned_independently_from_input(
    snapshot,
    payloads,
):
    original = deepcopy(payloads)
    result = build(snapshot, payloads)
    returned = next(
        row
        for row in result.member_documents[A]["transfers"]
        if row["data"]["tx_hash"] == original[A][0]["data"]["tx_hash"]
    )

    returned["data"]["value"] = "mutated"
    returned["data"]["token_ref"]["chain"] = "mutated"

    assert payloads == original


def test_conflicting_duplicate_directed_expectations_fail_closed(snapshot, payloads):
    changed_edges = snapshot.edges + (edge(A, B, 2),)

    with pytest.raises(TransferCompletenessError, match="conflicting"):
        build_transfer_result(
            payloads,
            target=snapshot.target,
            clusters=snapshot.clusters,
            edges=changed_edges,
            captured_at=CAPTURED_AT,
        )


def test_matching_duplicate_directed_expectations_are_idempotent(snapshot, payloads):
    changed_edges = snapshot.edges + (edge(A, B, 1),)

    result = build_transfer_result(
        payloads,
        target=snapshot.target,
        clusters=snapshot.clusters,
        edges=changed_edges,
        captured_at=CAPTURED_AT,
    )

    assert result.unique_transfer_count == 6


def test_cross_cluster_formal_rows_are_retained_without_completeness_check():
    a = holder(A, 1, amount="1", share="0.5", share_percent="50")
    b = holder(B, 2, amount="1", share="0.5", share_percent="50")
    clusters = (
        Cluster(1, "1", "0.5", "50", (a,)),
        Cluster(2, "1", "0.5", "50", (b,)),
    )
    row = transfer(A, B)

    result = build_transfer_result(
        {A: [deepcopy(row)], B: [deepcopy(row)]},
        target=make_target("bsc", TARGET_ADDRESS),
        clusters=clusters,
        edges=(edge(A, B, 99),),
        captured_at=CAPTURED_AT,
    )

    assert result.unique_transfer_count == 1
    assert result.transfer_view_count == 2
    assert result.member_documents[A]["transfers"] == [row]
    assert result.member_documents[B]["transfers"] == [row]


def test_duplicate_cluster_ranks_fail_before_payload_rows_are_processed():
    a = holder(A, 1, amount="1", share="0.5", share_percent="50")
    b = holder(B, 2, amount="1", share="0.5", share_percent="50")
    clusters = (
        Cluster(1, "1", "0.5", "50", (a,)),
        Cluster(1, "1", "0.5", "50", (b,)),
    )

    with pytest.raises(ValueError, match="cluster_rank"):
        build_transfer_result(
            {A: ["must not be inspected"], B: []},
            target=make_target("bsc", TARGET_ADDRESS),
            clusters=clusters,
            edges=(),
            captured_at=CAPTURED_AT,
        )


@pytest.mark.parametrize(
    "cluster_ranks",
    [(2,), (1, 3), (2, 1)],
    ids=("first-rank-not-one", "rank-gap", "out-of-order"),
)
def test_cluster_ranks_must_be_sequential_in_supplied_order_before_rows(
    cluster_ranks,
):
    addresses = (A, B)
    clusters = tuple(
        Cluster(
            cluster_rank,
            "1",
            "0.5",
            "50",
            (
                holder(
                    addresses[index],
                    index + 1,
                    amount="1",
                    share="0.5",
                    share_percent="50",
                ),
            ),
        )
        for index, cluster_rank in enumerate(cluster_ranks)
    )
    payloads = {
        addresses[index]: ["must not be inspected"] if index == 0 else []
        for index in range(len(cluster_ranks))
    }

    with pytest.raises(ValueError, match="cluster_rank"):
        build_transfer_result(
            payloads,
            target=make_target("bsc", TARGET_ADDRESS),
            clusters=clusters,
            edges=(),
            captured_at=CAPTURED_AT,
        )


def test_duplicate_source_ranks_fail_before_payload_rows_are_processed():
    members = (
        holder(A, 1, amount="1", share="0.5", share_percent="50"),
        holder(B, 1, amount="1", share="0.5", share_percent="50"),
    )
    cluster = Cluster(1, "2", "1", "100", members)

    with pytest.raises(ValueError, match="source_rank"):
        build_transfer_result(
            {A: ["must not be inspected"], B: []},
            target=make_target("bsc", TARGET_ADDRESS),
            clusters=(cluster,),
            edges=(),
            captured_at=CAPTURED_AT,
        )


@pytest.mark.parametrize("bad_rank", [0, -1, True, "1", None])
def test_cluster_rank_must_be_a_positive_native_integer(bad_rank):
    member = holder(A, 1, amount="1", share="1", share_percent="100")
    cluster = Cluster(bad_rank, "1", "1", "100", (member,))

    with pytest.raises(ValueError, match="cluster_rank"):
        build_transfer_result(
            {A: []},
            target=make_target("bsc", TARGET_ADDRESS),
            clusters=(cluster,),
            edges=(),
            captured_at=CAPTURED_AT,
        )


@pytest.mark.parametrize("bad_rank", [0, -1, True, "1", None])
def test_source_rank_must_be_a_positive_native_integer(bad_rank):
    member = holder(A, bad_rank, amount="1", share="1", share_percent="100")
    cluster = Cluster(1, "1", "1", "100", (member,))

    with pytest.raises(ValueError, match="source_rank"):
        build_transfer_result(
            {A: []},
            target=make_target("bsc", TARGET_ADDRESS),
            clusters=(cluster,),
            edges=(),
            captured_at=CAPTURED_AT,
        )


def test_empty_cluster_set_accepts_empty_payload_mapping():
    result = build_transfer_result(
        MappingProxyType({}),
        target=make_target("bsc", TARGET_ADDRESS),
        clusters=(),
        edges=(),
        captured_at=CAPTURED_AT,
    )

    assert result.token_document["clusters"] == []
    assert result.member_documents == {}
    assert result.unique_transfer_count == 0
    assert result.transfer_view_count == 0


def test_all_supernode_cluster_accepts_empty_payload_mapping():
    supernode = holder(
        S,
        1,
        amount="1",
        share="1",
        share_percent="100",
        is_supernode=True,
    )
    cluster = Cluster(1, "1", "1", "100", (supernode,))

    result = build_transfer_result(
        {},
        target=make_target("bsc", TARGET_ADDRESS),
        clusters=(cluster,),
        edges=(edge(S, S, 999),),
        captured_at=CAPTURED_AT,
    )

    assert result.member_documents == {}
    member = result.token_document["clusters"][0]["members"][0]
    assert member["is_supernode"] is True
    assert member["transfer_details_available"] is False
    assert member["transfer_details_reason"] == "supernode_not_supported"
    assert member["transfer_count"] == 0
    assert member["transfer_file"] is None


def test_supernode_position_does_not_suppress_empty_ordinary_member_document():
    supernode = holder(
        S,
        1,
        amount="2",
        share="0.75",
        share_percent="75",
        is_supernode=True,
        metadata={"label": "Supernode first"},
    )
    ordinary = holder(
        A,
        2,
        amount="1",
        share="0.25",
        share_percent="25",
        metadata={"label": "Empty ordinary"},
    )
    cluster = Cluster(1, "3", "1", "100", (supernode, ordinary))

    result = build_transfer_result(
        {A: []},
        target=make_target("bsc", TARGET_ADDRESS),
        clusters=(cluster,),
        edges=(),
        captured_at=CAPTURED_AT,
    )

    assert set(result.member_documents) == {A}
    ordinary_document = result.member_documents[A]
    assert ordinary_document["transfer_count"] == 0
    assert ordinary_document["transfers"] == []

    supernode_summary, ordinary_summary = result.token_document["clusters"][0][
        "members"
    ]
    assert supernode_summary["member_rank"] == 1
    assert supernode_summary["address"] == S
    assert supernode_summary["transfer_details_available"] is False
    assert (
        supernode_summary["transfer_details_reason"]
        == "supernode_not_supported"
    )
    assert supernode_summary["transfer_count"] == 0
    assert supernode_summary["transfer_file"] is None
    assert S not in result.member_documents

    assert ordinary_summary["member_rank"] == 2
    assert ordinary_summary["address"] == A
    assert ordinary_summary["transfer_details_available"] is True
    assert "transfer_details_reason" not in ordinary_summary
    assert ordinary_summary["transfer_count"] == 0
    assert ordinary_summary["transfer_file"] == (
        f"transfers/{safe_path_component(A)}.json"
    )
