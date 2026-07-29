from copy import deepcopy
from decimal import Decimal

from getDB.bubblemaps.tool.contract import (
    decimal_text,
    normalize_token_clusters,
    put_token,
)


def test_decimal_text_keeps_none_as_none() -> None:
    assert decimal_text(None) is None


def test_decimal_text_preserves_decimal_textual_precision() -> None:
    assert decimal_text(Decimal("12345678901234567890.0012300")) == (
        "12345678901234567890.0012300"
    )


def test_decimal_text_preserves_source_strings_verbatim() -> None:
    assert decimal_text("0.0000000000000000012300") == "0.0000000000000000012300"


def test_normalize_retains_all_clusters_and_deep_copies_source_data() -> None:
    clusters = [
        {
            "cluster_index": index + 1,
            "amount": str(81 - index),
            "share": "0.0100000000",
            "share_percent": "1.00000000",
            "metadata": {"tags": [f"Cluster-{index + 1}"]},
            "members": [
                {
                    "member_type": "holder",
                    "address": f"0xCaSeSensitive{index + 1}",
                    "source_rank": index + 1,
                    "amount": "1.000000000000000000",
                    "share": "0.0010000000",
                    "custom": {"source": "fixture"},
                }
            ],
        }
        for index in range(81)
    ]
    original = deepcopy(clusters)

    result = normalize_token_clusters(clusters)

    assert len(result) == 81
    assert [cluster["cluster_rank"] for cluster in result] == list(range(1, 82))
    assert all(cluster["member_count"] == 1 for cluster in result)
    assert all(cluster["members"][0]["member_rank"] == 1 for cluster in result)
    assert result[0]["amount"] == "81"
    assert result[0]["share"] == "0.0100000000"
    assert result[0]["share_percent"] == "1.00000000"
    assert result[0]["members"][0]["address"] == "0xCaSeSensitive1"
    assert result[0]["members"][0]["amount"] == "1.000000000000000000"
    assert result[0]["members"][0]["share"] == "0.0010000000"
    assert result[0]["members"][0]["custom"] == {"source": "fixture"}
    assert clusters == original
    assert result[0] is not clusters[0]
    assert result[0]["metadata"] is not clusters[0]["metadata"]
    assert result[0]["members"][0] is not clusters[0]["members"][0]

    result[0]["metadata"]["tags"].append("changed")
    assert clusters[0]["metadata"]["tags"] == ["Cluster-1"]


def test_normalize_sorts_clusters_by_exact_amount_share_and_index() -> None:
    clusters = [
        {
            "cluster_index": 1,
            "amount": "100000000000000000000.000000000000000001",
            "share": "0.99",
            "members": [],
        },
        {
            "cluster_index": 4,
            "amount": "10.0",
            "share": "0.20",
            "members": [],
        },
        {
            "cluster_index": 9,
            "amount": "100000000000000000000.000000000000000002",
            "share": "0.01",
            "members": [],
        },
        {
            "cluster_index": 2,
            "amount": "10.00",
            "share": "0.20",
            "members": [],
        },
        {
            "cluster_index": 7,
            "amount": "10",
            "share": "0.30",
            "members": [],
        },
    ]

    result = normalize_token_clusters(clusters)

    assert [cluster["cluster_index"] for cluster in result] == [9, 1, 7, 2, 4]
    assert [cluster["cluster_rank"] for cluster in result] == [1, 2, 3, 4, 5]
    assert [cluster["amount"] for cluster in result] == [
        "100000000000000000000.000000000000000002",
        "100000000000000000000.000000000000000001",
        "10",
        "10.00",
        "10.0",
    ]
    assert [cluster["share"] for cluster in result] == [
        "0.01",
        "0.99",
        "0.30",
        "0.20",
        "0.20",
    ]


def test_normalize_sorts_holders_exactly_and_relationship_nodes_last() -> None:
    clusters = [
        {
            "cluster_index": 1,
            "amount": "200000000000000000000.000000000000000003",
            "share": "0.50",
            "members": [
                {
                    "member_type": "holder",
                    "address": "0xExactSmall",
                    "amount": "100000000000000000000.000000000000000001",
                    "share": "0.99",
                    "source_rank": 1,
                },
                {
                    "member_type": "relationship_node",
                    "address": "0xRelB",
                    "amount": None,
                    "share": None,
                    "share_percent": None,
                    "source_rank": None,
                },
                {
                    "member_type": "holder",
                    "address": "0xRank2",
                    "amount": "5.000",
                    "share": "0.20",
                    "source_rank": 2,
                },
                {
                    "member_type": "holder",
                    "address": "0xAddrB",
                    "amount": "5.000",
                    "share": "0.20",
                    "source_rank": 1,
                },
                {
                    "member_type": "holder",
                    "address": "0xHighShare",
                    "amount": "5.000",
                    "share": "0.30",
                    "source_rank": 99,
                },
                {
                    "member_type": "relationship_node",
                    "address": "0xRelA",
                    "amount": None,
                    "share": None,
                    "share_percent": None,
                    "source_rank": None,
                },
                {
                    "member_type": "holder",
                    "address": "0xExactLarge",
                    "amount": "100000000000000000000.000000000000000002",
                    "share": "0.01",
                    "source_rank": 90,
                },
                {
                    "member_type": "holder",
                    "address": "0xAddrA",
                    "amount": "5.000",
                    "share": "0.20",
                    "source_rank": 1,
                },
            ],
        }
    ]

    result = normalize_token_clusters(clusters)
    members = result[0]["members"]

    assert [member["address"] for member in members] == [
        "0xExactLarge",
        "0xExactSmall",
        "0xHighShare",
        "0xAddrA",
        "0xAddrB",
        "0xRank2",
        "0xRelA",
        "0xRelB",
    ]
    assert [member["member_rank"] for member in members] == list(range(1, 9))
    assert result[0]["member_count"] == 8
    assert members[0]["amount"] == (
        "100000000000000000000.000000000000000002"
    )
    assert members[0]["share"] == "0.01"
    assert members[-2]["amount"] is None
    assert members[-2]["share"] is None
    assert members[-2]["source_rank"] is None


def test_put_token_builds_the_exact_four_level_shape_and_preserves_spelling() -> None:
    output: dict = {}
    clusters = [{"cluster_index": 3, "members": []}]

    result = put_token(output, "EtH", "0xAbCdEf", clusters)

    assert result is None
    assert output == {
        "EtH": {
            "0xAbCdEf": {
                "clusters": clusters,
            }
        }
    }
