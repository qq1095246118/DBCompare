import json
import math
from copy import deepcopy
from datetime import date
from pathlib import Path
import subprocess
import sys

import pytest

from getDB.bubblemaps.tool.db_source import PgSettings
from common.artifacts import safe_path_component
from getMarket.bubblemaps.tool import export_bubblemaps_market as market_cli
from getMarket.bubblemaps.tool.bubblemaps_api import (
    ApiResult,
    BubblemapsApiError,
    RequestMetadata,
    TopHoldersUnavailableError,
)
from getMarket.bubblemaps.tool.market_artifacts import (
    read_validated_generation,
    validate_staging_generation,
)
from getMarket.bubblemaps.tool.market_identity import make_target


DAY = date(2026, 7, 22)
CAPTURED_AT = "2026-07-22T12:30:00Z"
TOKEN = "0x1111111111111111111111111111111111111111"
TOKEN_B = "0x2222222222222222222222222222222222222222"
MEMBER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SUPERNODE = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
EXTERNAL = "0x9999999999999999999999999999999999999999"
FIXTURE_MEMBER_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
FIXTURE_MEMBER_C = "0xcccccccccccccccccccccccccccccccccccccccc"
FIXTURE_SUPERNODE = "0xdddddddddddddddddddddddddddddddddddddddd"
FIXTURE_EXTERNAL = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
FIXTURES = Path(__file__).parent / "fixtures"
README = (
    Path(__file__).parents[1] / "getMarket" / "bubblemaps" / "README.md"
)
PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"
SETTINGS = PgSettings(
    host="db.example.invalid",
    port=5432,
    dbname="analytics",
    user="readonly",
    password="never-persist-this-password",
)


def _api_result(target, kind, payload, *, member_address=None, attempts=1):
    if kind == "holders":
        method = "POST"
        url = (
            "https://api.bubblemaps.io/addresses/token-top-holders"
            "?count=300&nocache=false"
        )
    elif kind == "subgraph":
        method = "POST"
        url = (
            "https://api.bubblemaps.io/relationships/subgraph"
            f"?whitelist_token_address={target.requested_token_address}"
            f"&whitelist_token_chain={target.chain}"
            "&queue_whitelisted_token_map=false"
        )
    else:
        method = "GET"
        url = (
            "https://api.bubblemaps.io/relationships/transfers"
            f"?address={member_address}"
            f"&whitelist_token_address={target.requested_token_address}"
            f"&whitelist_token_chain={target.chain}"
        )
    return ApiResult(
        payload=payload,
        metadata=RequestMetadata(
            method=method,
            url=url,
            status=200,
            attempts=attempts,
            request_chain=target.requested_chain,
            request_token_address=target.requested_token_address,
        ),
    )


def _holders():
    return [
        {
            "address": MEMBER,
            "address_details": {"is_supernode": False, "label": "member"},
            "holder_data": {"rank": 1, "amount": "60", "share": "0.3"},
        },
        {
            "address": SUPERNODE,
            "address_details": {"is_supernode": True, "label": "supernode"},
            "holder_data": {"rank": 2, "amount": "40", "share": "0.2"},
        },
        {
            "address": EXTERNAL,
            "address_details": {"is_supernode": False},
            "holder_data": {"rank": None, "amount": "1", "share": "0.01"},
        },
    ]


def _subgraph(token=TOKEN):
    return [
        {
            "from_address": MEMBER,
            "to_address": SUPERNODE,
            "rel_type": "GROUPED_TRANSFER",
            "data": {
                "total_transfers": 1,
                "total_value": "1",
                "first_date": 1,
                "last_date": 1,
                "token_ref": {"chain": "bsc", "address": token},
            },
        },
        {
            "from_address": MEMBER,
            "to_address": EXTERNAL,
            "rel_type": "GROUPED_TRANSFER",
            "data": {
                "total_transfers": 99,
                "token_ref": {"chain": "bsc", "address": token},
            },
        },
    ]


def _transfers(token=TOKEN):
    foreign_token = TOKEN_B if token != TOKEN_B else TOKEN
    return [
        {
            "from_address": MEMBER,
            "to_address": EXTERNAL,
            "rel_type": "TRANSFER",
            "data": {
                "value": "1",
                "date": 1,
                "tx_hash": "0xexternal",
                "token_ref": {"chain": "bsc", "address": token},
            },
        },
        {
            "from_address": MEMBER,
            "to_address": EXTERNAL,
            "rel_type": "TRANSFER",
            "data": {
                "value": "2",
                "date": 2,
                "tx_hash": "0xwrong-token",
                "token_ref": {
                    "chain": "bsc",
                    "address": foreign_token,
                },
            },
        },
    ]


def _install_dependencies(monkeypatch, output_root, client, *, targets=None):
    monkeypatch.setattr(market_cli, "_china_today", lambda: DAY)
    monkeypatch.setattr(market_cli, "_utc_now", lambda: CAPTURED_AT)
    monkeypatch.setattr(market_cli, "load_pg_settings", lambda: SETTINGS)
    monkeypatch.setattr(
        market_cli,
        "load_targets",
        lambda settings: (targets or {"bsc": [TOKEN]}) if settings == SETTINGS else {},
    )
    monkeypatch.setattr(market_cli, "BubblemapsApiClient", lambda **_kwargs: client)
    return ["--output-root", str(output_root)]


def test_clean_holders_converts_official_floats_without_mutating_raw(
    tmp_path,
    monkeypatch,
):
    target = make_target("bsc", TOKEN)
    payload = [
        {
            "address": MEMBER,
            "address_details": {"is_supernode": False, "label": "member"},
            "holder_data": {
                "rank": 1,
                "amount": 2000000.0,
                "share": 0.12145995659816326,
            },
        }
    ]
    original = deepcopy(payload)
    result = _api_result(target, "holders", payload)

    raw_file = market_cli.write_raw_response(
        tmp_path,
        target,
        "holders",
        result,
    )
    raw = json.loads((tmp_path / raw_file).read_text())
    clean = market_cli.clean_holders(result, target)
    market_cli.write_clean_response(tmp_path, target, "holders", clean)
    market_cli.write_clean_response(tmp_path, target, "relationships", [])
    monkeypatch.setattr(market_cli, "_utc_now", lambda: CAPTURED_AT)
    snapshot = market_cli.read_clean_snapshot(tmp_path, target)

    assert type(raw["payload"][0]["holder_data"]["amount"]) is float
    assert type(raw["payload"][0]["holder_data"]["share"]) is float
    assert raw["payload"][0]["holder_data"] == {
        "rank": 1,
        "amount": 2000000.0,
        "share": 0.12145995659816326,
    }
    assert clean[0]["holder_data"] == {
        "rank": 1,
        "amount": "2000000.0",
        "share": "0.12145995659816326",
    }
    assert snapshot.holders[0].amount == "2000000.0"
    assert snapshot.holders[0].share == "0.12145995659816326"
    assert payload == original
    assert result.payload is payload


@pytest.mark.parametrize("field", ["amount", "share"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_clean_holders_rejects_nonfinite_official_float(field, value):
    target = make_target("bsc", TOKEN)
    holder_data = {"rank": 1, "amount": 1, "share": "0.1", field: value}
    payload = [
        {
            "address": MEMBER,
            "address_details": {"is_supernode": False},
            "holder_data": holder_data,
        }
    ]

    with pytest.raises(ValueError, match="finite"):
        market_cli.clean_holders(payload, target)


@pytest.mark.parametrize("value", [True, False])
def test_clean_holders_does_not_treat_bool_as_official_number(value):
    target = make_target("bsc", TOKEN)
    payload = [
        {
            "address": MEMBER,
            "address_details": {"is_supernode": False},
            "holder_data": {"rank": 1, "amount": value, "share": "0.1"},
        }
    ]

    with pytest.raises(ValueError, match="decimal string or native integer"):
        market_cli.clean_holders(payload, target)


def _write_transfer_snapshot(staging, target):
    holders = market_cli.clean_holders(_holders(), target)
    relationships = market_cli.clean_relationships(
        _subgraph(target.token_address),
        target,
        holders,
    )
    market_cli.write_clean_response(staging, target, "holders", holders)
    market_cli.write_clean_response(
        staging,
        target,
        "relationships",
        relationships,
    )
    return market_cli.read_clean_snapshot(staging, target)


def test_clean_transfers_converts_official_float_without_mutating_raw_and_roundtrips(
    tmp_path,
    monkeypatch,
):
    target = make_target("bsc", TOKEN)
    snapshot = _write_transfer_snapshot(tmp_path, target)
    payload = _transfers()
    payload[0]["data"]["value"] = 67004.79511954219
    original = deepcopy(payload)
    result = _api_result(
        target,
        "transfers",
        payload,
        member_address=MEMBER,
    )

    raw_file = market_cli.write_raw_response(
        tmp_path,
        target,
        f"transfers/{MEMBER}",
        result,
    )
    cleaned = market_cli.clean_transfers(result, target, MEMBER, snapshot)
    clean_file = market_cli.write_clean_member_transfers(
        tmp_path,
        target,
        MEMBER,
        cleaned,
        cluster_rank=1,
    )
    monkeypatch.setattr(market_cli, "_utc_now", lambda: CAPTURED_AT)
    reloaded_snapshot, transfer_files = market_cli.read_all_clean_snapshot(
        tmp_path,
        target,
    )
    token_document = market_cli.assemble_token(reloaded_snapshot, transfer_files)

    raw_document = json.loads((tmp_path / raw_file).read_text())
    clean_document = json.loads((tmp_path / clean_file).read_text())
    assert type(raw_document["payload"][0]["data"]["value"]) is float
    assert raw_document["payload"][0]["data"]["value"] == 67004.79511954219
    assert clean_document["transfers"][0]["data"]["value"] == "67004.79511954219"
    expected_token_reference = str(Path("transfers") / Path(clean_file).name)
    assert (
        token_document["clusters"][0]["members"][0]["transfer_file"]
        == expected_token_reference
    )
    assert token_document["clusters"][0]["members"][0]["transfer_count"] == 1
    assert payload == original
    assert result.payload is payload


def test_duplicate_api_transfers_remain_raw_but_collapse_in_clean_and_final(tmp_path):
    target = make_target("bsc", TOKEN)
    snapshot = _write_transfer_snapshot(tmp_path, target)
    row = _transfers()[0]
    payload = [deepcopy(row), deepcopy(row)]
    result = _api_result(
        target,
        "transfers",
        payload,
        member_address=MEMBER,
    )

    raw_file = market_cli.write_raw_response(
        tmp_path,
        target,
        f"transfers/{MEMBER}",
        result,
    )
    cleaned = market_cli.clean_transfers(result, target, MEMBER, snapshot)
    clean_file = market_cli.write_clean_member_transfers(
        tmp_path,
        target,
        MEMBER,
        cleaned,
        cluster_rank=1,
    )
    reloaded_snapshot, transfer_files = market_cli.read_all_clean_snapshot(
        tmp_path,
        target,
    )
    token_document = market_cli.assemble_token(reloaded_snapshot, transfer_files)

    raw_document = json.loads((tmp_path / raw_file).read_text())
    clean_document = json.loads((tmp_path / clean_file).read_text())
    member_summary = token_document["clusters"][0]["members"][0]
    assert raw_document["payload"] == payload
    assert len(raw_document["payload"]) == 2
    assert clean_document["transfers"] == [row]
    assert clean_document["transfer_count"] == 1
    assert member_summary["transfer_count"] == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_clean_transfers_rejects_nonfinite_official_float(tmp_path, value):
    target = make_target("bsc", TOKEN)
    snapshot = _write_transfer_snapshot(tmp_path, target)
    payload = _transfers()
    payload[0]["data"]["value"] = value

    with pytest.raises(ValueError, match="exact decimal string or native integer"):
        market_cli.clean_transfers(payload, target, MEMBER, snapshot)


def test_clean_transfers_skips_nonfinite_values_on_unselected_rows(tmp_path):
    target = make_target("bsc", TOKEN)
    snapshot = _write_transfer_snapshot(tmp_path, target)
    payload = _transfers()
    payload[1]["data"]["value"] = float("nan")
    ignored = deepcopy(payload[0])
    ignored["rel_type"] = "IGNORED"
    ignored["data"]["value"] = float("inf")
    payload.append(ignored)

    cleaned = market_cli.clean_transfers(payload, target, MEMBER, snapshot)

    assert cleaned == [payload[0]]
    assert cleaned[0] is not payload[0]
    assert math.isnan(payload[1]["data"]["value"])
    assert math.isinf(payload[2]["data"]["value"])


@pytest.mark.parametrize("value", [7, "7.50"])
def test_clean_transfers_preserves_strict_integer_and_string_values(tmp_path, value):
    target = make_target("bsc", TOKEN)
    snapshot = _write_transfer_snapshot(tmp_path, target)
    payload = _transfers()
    payload[0]["data"]["value"] = value

    cleaned = market_cli.clean_transfers(payload, target, MEMBER, snapshot)

    assert cleaned[0]["data"]["value"] == value
    assert type(cleaned[0]["data"]["value"]) is type(value)


@pytest.mark.parametrize("value", [True, False])
def test_clean_transfers_does_not_treat_bool_as_official_number(tmp_path, value):
    target = make_target("bsc", TOKEN)
    snapshot = _write_transfer_snapshot(tmp_path, target)
    payload = _transfers()
    payload[0]["data"]["value"] = value

    with pytest.raises(ValueError, match="decimal string or native integer"):
        market_cli.clean_transfers(payload, target, MEMBER, snapshot)


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _assert_official_transfer_fixture_shape(transfers_by_member):
    assert type(transfers_by_member) is dict
    for rows in transfers_by_member.values():
        assert type(rows) is list
        for row in rows:
            assert type(row) is dict
            for endpoint in ("from_address", "to_address"):
                value = row.get(endpoint)
                assert type(value) is str and value, endpoint

            pending = [row]
            while pending:
                current = pending.pop()
                if type(current) is dict:
                    assert "member_role" not in current, "member_role"
                    pending.extend(current.values())
                elif type(current) is list:
                    pending.extend(current)


def test_official_transfer_fixture_requires_endpoints_on_filtered_rows():
    transfers_by_member = _fixture("official_member_transfers.json")
    other_chain = next(
        row
        for rows in transfers_by_member.values()
        for row in rows
        if row["data"]["token_ref"].get("chain") == "eth"
    )
    del other_chain["to_address"]

    with pytest.raises(AssertionError, match="to_address"):
        _assert_official_transfer_fixture_shape(transfers_by_member)


def test_official_transfer_fixture_rejects_member_role_at_any_depth():
    transfers_by_member = deepcopy(_fixture("official_member_transfers.json"))
    other_token = next(
        row
        for rows in transfers_by_member.values()
        for row in rows
        if row["data"]["token_ref"].get("chain") == "bsc"
        and row["data"]["token_ref"].get("address") not in (None, TOKEN)
    )
    other_token["data"]["member_role"] = "sender"

    with pytest.raises(AssertionError, match="member_role"):
        _assert_official_transfer_fixture_shape(transfers_by_member)


def test_cli_import_does_not_import_playwright_in_fresh_interpreter():
    project_root = Path(__file__).parents[1]
    probe = """
import builtins
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'playwright' or name.startswith('playwright.'):
        raise RuntimeError('browser import is forbidden')
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
import getMarket.bubblemaps.tool.export_bubblemaps_market
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_repository_has_no_legacy_browser_capture_modules_or_dependency():
    project_root = Path(__file__).parents[1]
    legacy_modules = (
        "getMarket/bubblemaps/tool/page_capture.py",
        "getMarket/bubblemaps/tool/member_transfer_capture.py",
        "getMarket/bubblemaps/tool/static_asset_cache.py",
    )

    assert [path for path in legacy_modules if (project_root / path).exists()] == []
    assert "playwright" not in PYPROJECT.read_text(encoding="utf-8").lower()
    for name in (
        "write_snapshot_evidence",
        "write_member_evidence",
        "write_retry_evidence",
        "write_failure_evidence",
    ):
        assert not hasattr(market_cli.market_artifacts, name)


def test_readme_describes_only_current_postgresql_api_market_cli():
    readme = README.read_text(encoding="utf-8")

    assert "从 PostgreSQL 读取目标" in readme
    assert "Bubblemaps API" in readme
    assert "--limit 1" in readme
    assert "--headed" not in readme
    assert "--db-file" not in readme
    assert "Bubblemaps V2 页面" not in readme
    assert "--date" not in readme
    assert "errors.json" not in readme
    assert "同一 Cluster 内的正式 `TRANSFER`" not in readme
    assert "另一端可以是 external、unranked 或其他 Cluster 地址" in readme
    assert "不会为外部对端生成 member 文档" in readme
    assert (
        "data/<safe-chain>/<safe-token>/\n"
        "    token.json\n"
        "    transfers/<safe-member>.json"
    ) not in readme
    assert "`transfer_file` 引用 `clean/" in readme


def test_live_smoke_marker_describes_current_api_transport():
    pyproject = PYPROJECT.read_text(encoding="utf-8")

    assert (
        "live_bubblemaps: read-only smoke tests against the current "
        "Bubblemaps API"
    ) in pyproject
    assert "current Bubblemaps page" not in pyproject


def test_parse_args_exposes_only_current_market_inputs():
    args = market_cli.parse_args([])

    assert not hasattr(market_cli, "async_playwright")
    assert not hasattr(args, "date")
    assert not hasattr(args, "db_file")
    assert not hasattr(args, "headed")
    assert args.api_min_interval == 2.1
    assert market_cli.parse_args(["--limit", "1"]).limit == 1
    single = market_cli.parse_args(
        ["--chain", "bsc", "--token-address", TOKEN]
    )
    assert (single.chain, single.token_address) == ("bsc", TOKEN)


def test_parse_args_normalizes_and_deduplicates_symbol_list():
    args = market_cli.parse_args(["--symbols", "m, BEAT,b,m"])

    assert args.symbols == ("B", "BEAT", "M")


@pytest.mark.parametrize("value", ["", ",", "M,,BEAT"])
def test_parse_args_rejects_empty_symbol_entries(value):
    with pytest.raises(SystemExit):
        market_cli.parse_args(["--symbols", value])


def test_parse_args_rejects_symbols_with_single_target():
    with pytest.raises(SystemExit):
        market_cli.parse_args(
            [
                "--symbols",
                "M,BEAT",
                "--chain",
                "bsc",
                "--token-address",
                TOKEN,
            ]
        )


@pytest.mark.parametrize(
    "argv",
    [["--date", DAY.isoformat()], ["--db-file", "old.json"], ["--headed"]],
)
def test_parse_args_rejects_removed_browser_and_history_inputs(argv):
    with pytest.raises(SystemExit):
        market_cli.parse_args(argv)


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "true"])
def test_parse_args_rejects_nonpositive_or_noninteger_limit(value):
    with pytest.raises(SystemExit):
        market_cli.parse_args(["--limit", value])


@pytest.mark.parametrize(
    "option", ["--api-timeout", "--api-retry-delay", "--api-min-interval"]
)
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_parse_args_rejects_nonfinite_api_timing_values(option, value):
    with pytest.raises(SystemExit) as raised:
        market_cli.parse_args([option, value])

    assert raised.value.code == 2


@pytest.mark.parametrize("value", ["-1", "-0.1"])
def test_parse_args_rejects_negative_api_min_interval(value):
    with pytest.raises(SystemExit) as raised:
        market_cli.parse_args(["--api-min-interval", value])

    assert raised.value.code == 2


def test_run_generation_passes_default_and_explicit_api_min_interval(tmp_path, monkeypatch):
    received: list[float] = []

    class FakeClient:
        async def get_top_holders(self, target):
            return _api_result(target, "holders", _holders())

        async def get_subgraph(self, target, ranked_addresses):
            return _api_result(target, "subgraph", _subgraph(target.token_address))

        async def get_transfers(self, target, member_address):
            return _api_result(
                target, "transfers", _transfers(target.token_address), member_address=member_address
            )

    def make_client(**kwargs):
        received.append(kwargs["min_request_interval"])
        return FakeClient()

    monkeypatch.setattr(market_cli, "BubblemapsApiClient", make_client)
    monkeypatch.setattr(market_cli, "_china_today", lambda: DAY)
    monkeypatch.setattr(market_cli, "_utc_now", lambda: CAPTURED_AT)
    monkeypatch.setattr(market_cli, "load_pg_settings", lambda: SETTINGS)
    monkeypatch.setattr(market_cli, "load_targets", lambda _settings: {"bsc": [TOKEN]})

    assert market_cli.main(["--output-root", str(tmp_path / "default")]) == 0
    assert market_cli.main([
        "--output-root", str(tmp_path / "explicit"), "--api-min-interval", "3.5"
    ]) == 0
    assert received == [2.1, 3.5]


def test_run_generation_passes_symbols_to_database_target_loader(tmp_path, monkeypatch):
    received = []

    class FakeClient:
        async def get_top_holders(self, target):
            return _api_result(target, "holders", _holders())

        async def get_subgraph(self, target, ranked_addresses):
            return _api_result(target, "subgraph", _subgraph(target.token_address))

        async def get_transfers(self, target, member_address):
            return _api_result(
                target,
                "transfers",
                _transfers(target.token_address),
                member_address=member_address,
            )

    monkeypatch.setattr(market_cli, "_china_today", lambda: DAY)
    monkeypatch.setattr(market_cli, "_utc_now", lambda: CAPTURED_AT)
    monkeypatch.setattr(market_cli, "load_pg_settings", lambda: SETTINGS)
    monkeypatch.setattr(market_cli, "BubblemapsApiClient", lambda **_kwargs: FakeClient())

    def load_selected(settings, *, symbols=None):
        received.append((settings, symbols))
        return {"bsc": [TOKEN]}

    monkeypatch.setattr(market_cli, "load_targets", load_selected)

    assert market_cli.main(
        [
            "--output-root",
            str(tmp_path / "symbols"),
            "--symbols",
            "m,BEAT,m",
        ]
    ) == 0
    assert received == [(SETTINGS, ("BEAT", "M"))]


@pytest.mark.parametrize(
    "argv",
    [["--chain", "bsc"], ["--token-address", TOKEN]],
)
def test_parse_args_requires_complete_single_target_pair(argv):
    with pytest.raises(SystemExit):
        market_cli.parse_args(argv)


def test_fake_client_pipeline_publishes_only_after_all_api_stages(
    tmp_path,
    monkeypatch,
):
    output_root = tmp_path / "market"
    events = []

    class FakeClient:
        async def get_top_holders(self, target):
            events.append(f"holders:{target.token_address}")
            assert not (output_root / DAY.isoformat()).exists()
            return _api_result(target, "holders", _holders())

        async def get_subgraph(self, target, ranked_addresses):
            events.append(f"subgraph:{target.token_address}")
            assert ranked_addresses == [MEMBER, SUPERNODE]
            assert not (output_root / DAY.isoformat()).exists()
            return _api_result(target, "subgraph", _subgraph(target.token_address))

        async def get_transfers(self, target, member_address):
            events.append(f"transfers:{target.token_address}:{member_address}")
            assert member_address == MEMBER
            assert not (output_root / DAY.isoformat()).exists()
            staging = next((output_root / "_staging" / DAY.isoformat()).iterdir())
            assert len(list(staging.glob("raw/*/*/holders.json"))) == 2
            assert len(list(staging.glob("raw/*/*/subgraph.json"))) == 2
            assert len(list(staging.glob("clean/*/*/holders.json"))) == 2
            assert len(list(staging.glob("clean/*/*/relationships.json"))) == 2
            assert not (staging / "data").exists()
            return _api_result(
                target,
                "transfers",
                _transfers(target.token_address),
                member_address=member_address,
            )

    real_write_final = market_cli.write_final_token

    def observed_write_final(staging, target, document):
        events.append(f"final:{target.token_address}")
        assert events.count(f"transfers:{TOKEN}:{MEMBER}") == 1
        assert events.count(f"transfers:{TOKEN_B}:{MEMBER}") == 1
        return real_write_final(staging, target, document)

    monkeypatch.setattr(market_cli, "write_final_token", observed_write_final)
    argv = _install_dependencies(
        monkeypatch,
        output_root,
        FakeClient(),
        targets={"bsc": [TOKEN, TOKEN_B]},
    )

    assert market_cli.main(argv) == 0

    live = output_root / DAY.isoformat()
    manifest = validate_staging_generation(live)
    assert events == [
        f"holders:{TOKEN}",
        f"subgraph:{TOKEN}",
        f"holders:{TOKEN_B}",
        f"subgraph:{TOKEN_B}",
        f"transfers:{TOKEN}:{MEMBER}",
        f"transfers:{TOKEN_B}:{MEMBER}",
        f"final:{TOKEN}",
        f"final:{TOKEN_B}",
    ]
    assert manifest["schema_version"] == "v3"
    assert manifest["source"] == "bubblemaps_api"
    assert manifest["status"] == "success"
    assert manifest["skipped_tokens"] == []
    assert manifest["targets"] == {"bsc": [TOKEN, TOKEN_B]}
    assert len(manifest["tokens"]) == 2
    for entry in manifest["tokens"]:
        assert len(entry["member_files"]) == 1
        member_document = json.loads((live / entry["member_files"][0]).read_text())
        expected = _transfers(entry["canonical_token_address"])[0]
        assert member_document["transfers"] == [expected]
        token_document = json.loads((live / entry["token_file"]).read_text())
        ordinary, supernode = token_document["clusters"][0]["members"]
        assert ordinary["transfer_file"] == entry["member_files"][0]
        assert "transfers" not in ordinary
        assert supernode["transfer_file"] is None
    assert all("password" not in path.read_text().lower() for path in live.rglob("*.json"))


def test_cli_skips_unavailable_top_holders_and_publishes_valid_partial_generation(
    tmp_path,
    monkeypatch,
):
    output_root = tmp_path / "market"
    events = []

    class FakeClient:
        async def get_top_holders(self, target):
            events.append(f"holders:{target.token_address}")
            if target.token_address == TOKEN_B:
                raise TopHoldersUnavailableError(attempts=2)
            return _api_result(target, "holders", _holders())

        async def get_subgraph(self, target, ranked_addresses):
            events.append(f"subgraph:{target.token_address}")
            return _api_result(target, "subgraph", _subgraph(target.token_address))

        async def get_transfers(self, target, member_address):
            events.append(f"transfers:{target.token_address}:{member_address}")
            return _api_result(
                target,
                "transfers",
                _transfers(target.token_address),
                member_address=member_address,
            )

    real_write_final = market_cli.write_final_token

    def observed_write_final(staging, target, document):
        events.append(f"final:{target.token_address}")
        return real_write_final(staging, target, document)

    monkeypatch.setattr(market_cli, "write_final_token", observed_write_final)
    argv = _install_dependencies(
        monkeypatch,
        output_root,
        FakeClient(),
        targets={"bsc": [TOKEN, TOKEN_B]},
    )

    assert market_cli.main(argv) == 0

    manifest, errors = read_validated_generation(output_root, DAY)
    live = output_root / DAY.isoformat()
    skipped = {
        "requested_chain": "bsc",
        "requested_token_address": TOKEN_B,
        "canonical_chain": "bsc",
        "canonical_token_address": TOKEN_B,
        "stage": "holders",
        "http_status": 400,
        "attempt_count": 2,
        "reason": "top_holders_not_available",
        "captured_at": CAPTURED_AT,
        "status": "skipped",
    }

    assert errors == []
    assert manifest["status"] == "partial_success"
    assert [entry["canonical_token_address"] for entry in manifest["tokens"]] == [TOKEN]
    assert manifest["skipped_tokens"] == [skipped]
    assert events == [
        f"holders:{TOKEN}",
        f"subgraph:{TOKEN}",
        f"holders:{TOKEN_B}",
        f"transfers:{TOKEN}:{MEMBER}",
        f"final:{TOKEN}",
    ]
    token_b_component = safe_path_component(TOKEN_B)
    assert not list(live.glob(f"raw/*/{token_b_component}/**/*"))
    assert not list(live.glob(f"clean/*/{token_b_component}/**/*"))
    assert not list(live.glob(f"data/*/{token_b_component}/**/*"))
    assert validate_staging_generation(live) == manifest


def test_cli_records_generic_holders_error_and_publishes_partial_generation(
    tmp_path,
    monkeypatch,
):
    output_root = tmp_path / "market"

    class FailingClient:
        async def get_top_holders(self, target):
            raise BubblemapsApiError("generic holders failure", attempts=2)

    argv = _install_dependencies(monkeypatch, output_root, FailingClient())

    assert market_cli.main(argv) == 0

    live = output_root / DAY.isoformat()
    manifest, errors = read_validated_generation(output_root, DAY)
    report = json.loads((live / "error.json").read_text())
    assert manifest["status"] == "partial_success"
    assert manifest["tokens"] == []
    assert len(manifest["skipped_tokens"]) == 1
    assert manifest["skipped_tokens"][0]["reason"] == "capture_failed"
    assert report == {"error_count": 1, "errors": errors}
    assert errors[0]["stage"] == "holders"
    assert errors[0]["type"] == "BubblemapsApiError"
    assert errors[0]["attempt_count"] == 2


def test_cli_publishes_new_transfers_after_subgraph_snapshot_as_partial_success(
    tmp_path,
    monkeypatch,
):
    output_root = tmp_path / "market"
    second_member = EXTERNAL
    holders = [
        {
            "address": MEMBER,
            "address_details": {"is_supernode": False},
            "holder_data": {"rank": 1, "amount": "60", "share": "0.3"},
        },
        {
            "address": second_member,
            "address_details": {"is_supernode": False},
            "holder_data": {"rank": 2, "amount": "40", "share": "0.2"},
        },
    ]
    subgraph = [
        {
            "from_address": MEMBER,
            "to_address": second_member,
            "rel_type": "GROUPED_TRANSFER",
            "data": {
                "total_transfers": 1,
                "total_value": "1",
                "first_date": 1,
                "last_date": 1,
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        }
    ]
    transfers = [
        {
            "from_address": MEMBER,
            "to_address": second_member,
            "rel_type": "TRANSFER",
            "data": {
                "value": "1",
                "date": 1,
                "tx_hash": "0xexisting",
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        },
        {
            "from_address": MEMBER,
            "to_address": second_member,
            "rel_type": "TRANSFER",
            "data": {
                "value": "2",
                "date": 2,
                "tx_hash": "0xnew-during-capture",
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        },
    ]

    class ChangingClient:
        async def get_top_holders(self, target):
            return _api_result(target, "holders", holders)

        async def get_subgraph(self, target, ranked_addresses):
            return _api_result(target, "subgraph", subgraph)

        async def get_transfers(self, target, member_address):
            return _api_result(
                target,
                "transfers",
                transfers,
                member_address=member_address,
            )

    argv = _install_dependencies(monkeypatch, output_root, ChangingClient())

    assert market_cli.main(argv) == 0

    manifest, errors = read_validated_generation(output_root, DAY)
    assert manifest["status"] == "partial_success"
    assert len(manifest["tokens"]) == 1
    assert errors == [
        {
            "chain": "bsc",
            "token_address": TOKEN,
            "stage": "final",
            "type": "TransferSnapshotDrift",
            "message": "new transfers captured after subgraph snapshot",
            "attempt_count": 0,
            "captured_at": CAPTURED_AT,
            "from_address": MEMBER,
            "to_address": second_member,
            "expected_count": 1,
            "captured_count": 2,
            "edge_last_date": 1,
        }
    ]
    token_file = output_root / DAY.isoformat() / manifest["tokens"][0]["token_file"]
    token_document = json.loads(token_file.read_text())
    first_member = token_document["clusters"][0]["members"][0]
    assert first_member["transfer_count"] == 2


def test_cli_publishes_transfer_pair_omitted_by_subgraph_as_partial_success(
    tmp_path,
    monkeypatch,
):
    output_root = tmp_path / "market"
    second_member = EXTERNAL
    holders = [
        {
            "address": MEMBER,
            "address_details": {"is_supernode": False},
            "holder_data": {"rank": 1, "amount": "60", "share": "0.3"},
        },
        {
            "address": second_member,
            "address_details": {"is_supernode": False},
            "holder_data": {"rank": 2, "amount": "40", "share": "0.2"},
        },
    ]
    subgraph = [
        {
            "from_address": second_member,
            "to_address": MEMBER,
            "rel_type": "GROUPED_TRANSFER",
            "data": {
                "total_transfers": 1,
                "total_value": "1",
                "first_date": 1,
                "last_date": 1,
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        }
    ]
    transfers = [
        {
            "from_address": second_member,
            "to_address": MEMBER,
            "rel_type": "TRANSFER",
            "data": {
                "value": "1",
                "date": 1,
                "tx_hash": "0xrepresented-by-subgraph",
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        },
        {
            "from_address": MEMBER,
            "to_address": second_member,
            "rel_type": "TRANSFER",
            "data": {
                "value": "2",
                "date": 2,
                "tx_hash": "0xomitted-by-subgraph",
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        },
    ]

    class OmittedPairClient:
        async def get_top_holders(self, target):
            return _api_result(target, "holders", holders)

        async def get_subgraph(self, target, ranked_addresses):
            return _api_result(target, "subgraph", subgraph)

        async def get_transfers(self, target, member_address):
            return _api_result(
                target,
                "transfers",
                transfers,
                member_address=member_address,
            )

    argv = _install_dependencies(monkeypatch, output_root, OmittedPairClient())

    assert market_cli.main(argv) == 0

    manifest, errors = read_validated_generation(output_root, DAY)
    assert manifest["status"] == "partial_success"
    assert len(manifest["tokens"]) == 1
    assert errors == [
        {
            "chain": "bsc",
            "token_address": TOKEN,
            "stage": "final",
            "type": "TransferSubgraphOmission",
            "message": "transfer pair absent from subgraph response",
            "attempt_count": 0,
            "captured_at": CAPTURED_AT,
            "from_address": MEMBER,
            "to_address": second_member,
            "expected_count": 0,
            "captured_count": 1,
            "edge_last_date": None,
        }
    ]
    token_file = output_root / DAY.isoformat() / manifest["tokens"][0]["token_file"]
    token_document = json.loads(token_file.read_text())
    member_document = next(
        member
        for member in token_document["clusters"][0]["members"]
        if member["address"] == MEMBER
    )
    transfer_file = output_root / DAY.isoformat() / member_document["transfer_file"]
    saved_transfers = json.loads(transfer_file.read_text())["transfers"]
    assert {row["data"]["tx_hash"] for row in saved_transfers} == {
        "0xrepresented-by-subgraph",
        "0xomitted-by-subgraph",
    }


def test_cli_merges_asymmetric_member_transfer_responses(tmp_path, monkeypatch):
    output_root = tmp_path / "market"
    second_member = EXTERNAL
    holders = [
        {
            "address": MEMBER,
            "address_details": {"is_supernode": False},
            "holder_data": {"rank": 1, "amount": "60", "share": "0.3"},
        },
        {
            "address": second_member,
            "address_details": {"is_supernode": False},
            "holder_data": {"rank": 2, "amount": "40", "share": "0.2"},
        },
    ]
    represented = {
        "from_address": second_member,
        "to_address": MEMBER,
        "rel_type": "TRANSFER",
        "data": {
            "value": "1",
            "date": 1,
            "tx_hash": "0xrepresented-by-subgraph",
            "token_ref": {"chain": "bsc", "address": TOKEN},
        },
    }
    only_returned_for_member = {
        "from_address": MEMBER,
        "to_address": second_member,
        "rel_type": "TRANSFER",
        "data": {
            "value": "2",
            "date": 2,
            "tx_hash": "0xonly-returned-for-first-member",
            "token_ref": {"chain": "bsc", "address": TOKEN},
        },
    }
    subgraph = [
        {
            "from_address": second_member,
            "to_address": MEMBER,
            "rel_type": "GROUPED_TRANSFER",
            "data": {
                "total_transfers": 1,
                "total_value": "1",
                "first_date": 1,
                "last_date": 1,
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        }
    ]

    class AsymmetricClient:
        async def get_top_holders(self, target):
            return _api_result(target, "holders", holders)

        async def get_subgraph(self, target, ranked_addresses):
            return _api_result(target, "subgraph", subgraph)

        async def get_transfers(self, target, member_address):
            rows = [represented]
            if member_address == MEMBER:
                rows.append(only_returned_for_member)
            return _api_result(
                target,
                "transfers",
                rows,
                member_address=member_address,
            )

    argv = _install_dependencies(monkeypatch, output_root, AsymmetricClient())

    assert market_cli.main(argv) == 0

    manifest, errors = read_validated_generation(output_root, DAY)
    assert manifest["status"] == "partial_success"
    assert [error["type"] for error in errors] == ["TransferSubgraphOmission"]
    token_file = output_root / DAY.isoformat() / manifest["tokens"][0]["token_file"]
    token_document = json.loads(token_file.read_text())
    member_summaries = token_document["clusters"][0]["members"]
    expected_hashes = {
        "0xrepresented-by-subgraph",
        "0xonly-returned-for-first-member",
    }
    for summary in member_summaries:
        transfer_file = output_root / DAY.isoformat() / summary["transfer_file"]
        document = json.loads(transfer_file.read_text())
        assert document["transfer_count"] == 2
        assert {row["data"]["tx_hash"] for row in document["transfers"]} == (
            expected_hashes
        )


def test_cli_keeps_token_when_member_transfer_fails_and_continues(
    tmp_path,
    monkeypatch,
):
    output_root = tmp_path / "market"
    events = []

    class PartialClient:
        async def get_top_holders(self, target):
            events.append(("holders", target.token_address))
            return _api_result(target, "holders", _holders())

        async def get_subgraph(self, target, ranked_addresses):
            events.append(("subgraph", target.token_address))
            return _api_result(
                target,
                "subgraph",
                _subgraph(target.token_address),
            )

        async def get_transfers(self, target, member_address):
            events.append(("transfers", target.token_address))
            if target.token_address == TOKEN:
                raise BubblemapsApiError("temporary transfer failure", attempts=3)
            return _api_result(
                target,
                "transfers",
                _transfers(target.token_address),
                member_address=member_address,
            )

    argv = _install_dependencies(
        monkeypatch,
        output_root,
        PartialClient(),
        targets={"bsc": [TOKEN, TOKEN_B]},
    )

    assert market_cli.main(argv) == 0

    manifest, errors = read_validated_generation(output_root, DAY)
    live = output_root / DAY.isoformat()
    assert manifest["status"] == "partial_success"
    assert len(manifest["tokens"]) == 2
    assert len(errors) == 1
    assert errors[0]["stage"] == "transfers"
    assert errors[0]["member_address"] == MEMBER
    assert events[-2:] == [("transfers", TOKEN), ("transfers", TOKEN_B)]

    token_entry = next(
        entry
        for entry in manifest["tokens"]
        if entry["canonical_token_address"] == TOKEN
    )
    token_document = json.loads((live / token_entry["token_file"]).read_text())
    ordinary = token_document["clusters"][0]["members"][0]
    assert ordinary["transfer_details_available"] is False
    assert ordinary["transfer_details_reason"] == "capture_failed"
    assert ordinary["transfer_count"] == 0
    assert ordinary["transfer_file"] is None
    assert token_entry["member_files"] == []


def test_cli_rejects_staging_symlink_before_external_write_or_api_call(
    tmp_path,
    monkeypatch,
):
    output_root = tmp_path / "market"
    external = tmp_path / "external"
    output_root.mkdir()
    external.mkdir()
    sentinel = external / "targets.json"
    sentinel.write_text("external sentinel\n", encoding="utf-8")
    (output_root / "_staging").symlink_to(external, target_is_directory=True)
    api_calls = []

    class FakeClient:
        async def get_top_holders(self, target):
            api_calls.append(("holders", target.token_address))
            return _api_result(target, "holders", _holders())

        async def get_subgraph(self, target, ranked_addresses):
            api_calls.append(("subgraph", target.token_address))
            return _api_result(target, "subgraph", _subgraph(target.token_address))

        async def get_transfers(self, target, member_address):
            api_calls.append(("transfers", target.token_address))
            return _api_result(
                target,
                "transfers",
                _transfers(target.token_address),
                member_address=member_address,
            )

    argv = _install_dependencies(monkeypatch, output_root, FakeClient())

    assert market_cli.main(argv) == 1

    assert api_calls == []
    assert sentinel.read_text(encoding="utf-8") == "external sentinel\n"
    assert [path.relative_to(external) for path in external.rglob("*")] == [
        Path("targets.json")
    ]
    assert not (output_root / DAY.isoformat()).exists()


def test_artifact_read_targets_roundtrips_ton_without_mutating_input(tmp_path):
    requested_address = "0:" + "AB" * 32
    targets = {"ton": [requested_address]}

    market_cli.market_artifacts.write_targets(tmp_path, targets)
    reloaded = market_cli.market_artifacts.read_targets(tmp_path)

    assert reloaded == targets
    assert reloaded is not targets
    assert reloaded["ton"] is not targets["ton"]
    assert targets == {"ton": [requested_address]}


def test_artifact_read_targets_reports_invalid_json_without_sensitive_detail(tmp_path):
    secret = "password=must-not-escape"
    (tmp_path / "targets.json").write_text(
        '{"bsc": ["' + secret + '"]',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as raised:
        market_cli.market_artifacts.read_targets(tmp_path)

    assert str(raised.value) == "API targets artifact cannot be read"
    assert secret not in str(raised.value)


def test_artifact_read_targets_rejects_destination_symlink(tmp_path):
    staging = tmp_path / "staging"
    external = tmp_path / "external-targets.json"
    staging.mkdir()
    external.write_text("{}\n", encoding="utf-8")
    (staging / "targets.json").symlink_to(external)

    with pytest.raises(ValueError, match="cannot be read"):
        market_cli.market_artifacts.read_targets(staging)

    assert external.read_text(encoding="utf-8") == "{}\n"


def test_official_transfer_fixture_preserves_raw_and_publishes_filtered_references(
    tmp_path,
    monkeypatch,
):
    output_root = tmp_path / "market"
    holders = _fixture("official_holders.json")
    transfers_by_member = _fixture("official_member_transfers.json")
    _assert_official_transfer_fixture_shape(transfers_by_member)
    subgraph = [
        {
            "from_address": MEMBER,
            "to_address": FIXTURE_MEMBER_B,
            "rel_type": "GROUPED_TRANSFER",
            "data": {
                "total_transfers": 1,
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        },
        {
            "from_address": FIXTURE_MEMBER_B,
            "to_address": MEMBER,
            "rel_type": "GROUPED_TRANSFER",
            "data": {
                "total_transfers": 1,
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        },
        {
            "from_address": FIXTURE_MEMBER_B,
            "to_address": FIXTURE_MEMBER_C,
            "rel_type": "GROUPED_TRANSFER",
            "data": {
                "total_transfers": 1,
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        },
        {
            "from_address": FIXTURE_MEMBER_C,
            "to_address": FIXTURE_SUPERNODE,
            "rel_type": "GROUPED_TRANSFER",
            "data": {
                "total_transfers": 1,
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        },
    ]

    class FakeClient:
        async def get_top_holders(self, target):
            return _api_result(target, "holders", holders)

        async def get_subgraph(self, target, ranked_addresses):
            assert ranked_addresses == [
                MEMBER,
                FIXTURE_MEMBER_B,
                FIXTURE_MEMBER_C,
                FIXTURE_SUPERNODE,
            ]
            return _api_result(target, "subgraph", subgraph)

        async def get_transfers(self, target, member_address):
            assert member_address in transfers_by_member
            return _api_result(
                target,
                "transfers",
                transfers_by_member[member_address],
                member_address=member_address,
            )

    argv = _install_dependencies(monkeypatch, output_root, FakeClient())

    assert market_cli.main(argv) == 0

    live = output_root / DAY.isoformat()
    manifest = validate_staging_generation(live)
    assert json.loads((live / "targets.json").read_text()) == {"bsc": [TOKEN]}
    assert len(manifest["tokens"]) == 1
    entry = manifest["tokens"][0]

    ordinary_addresses = {MEMBER, FIXTURE_MEMBER_B, FIXTURE_MEMBER_C}
    chain_component = safe_path_component("bsc")
    token_component = safe_path_component(TOKEN)
    clean_rows_by_member = {}
    all_fixture_rows = [
        row for rows in transfers_by_member.values() for row in rows
    ]
    for member_address in ordinary_addresses:
        member_component = safe_path_component(member_address)
        raw_relative = (
            f"raw/{chain_component}/{token_component}/transfers/"
            f"{member_component}.json"
        )
        clean_relative = (
            f"clean/{chain_component}/{token_component}/transfers/"
            f"{member_component}.json"
        )
        raw_document = json.loads((live / raw_relative).read_text())
        clean_document = json.loads((live / clean_relative).read_text())

        assert raw_document["payload"] == transfers_by_member[member_address]
        expected_by_hash = {
            row["data"]["tx_hash"]: row
            for row in all_fixture_rows
            if row.get("rel_type") == "TRANSFER"
            and row.get("data", {}).get("token_ref")
            == {"chain": "bsc", "address": TOKEN}
            and member_address
            in (row.get("from_address"), row.get("to_address"))
        }
        expected_clean = sorted(
            expected_by_hash.values(),
            key=lambda row: row["data"]["date"],
            reverse=True,
        )
        assert clean_document["transfers"] == expected_clean
        clean_rows_by_member[member_address] = clean_document["transfers"]
        assert raw_relative in entry["raw_files"]
        assert clean_relative in entry["clean_files"]

    assert any(
        row["data"]["token_ref"].get("chain") == "bsc"
        and row["data"]["token_ref"].get("address") not in (None, TOKEN)
        for row in all_fixture_rows
    )
    assert any(
        row["data"]["token_ref"]
        == {"chain": "eth", "address": TOKEN}
        for row in all_fixture_rows
    )
    clean_rows = [row for rows in clean_rows_by_member.values() for row in rows]
    assert any(
        row["from_address"] in ordinary_addresses
        and row["to_address"] == FIXTURE_EXTERNAL
        for row in clean_rows
    )
    assert any(
        row["from_address"] == FIXTURE_EXTERNAL
        and row["to_address"] in ordinary_addresses
        for row in clean_rows
    )
    assert all(
        row["data"]["token_ref"] == {"chain": "bsc", "address": TOKEN}
        for row in clean_rows
    )

    token_document = json.loads((live / entry["token_file"]).read_text())
    members = [
        member
        for cluster in token_document["clusters"]
        for member in cluster["members"]
    ]
    assert {member["address"] for member in members} == {
        *ordinary_addresses,
        FIXTURE_SUPERNODE,
    }
    for member in members:
        assert "transfers" not in member
        if member["is_supernode"]:
            assert member["transfer_file"] is None
        else:
            expected_reference = (
                f"clean/{chain_component}/{token_component}/transfers/"
                f"{safe_path_component(member['address'])}.json"
            )
            assert member["transfer_file"] == expected_reference
            assert expected_reference in entry["member_files"]

    external_component = safe_path_component(FIXTURE_EXTERNAL)
    assert all(external_component not in path for path in entry["member_files"])
    assert not list(live.rglob(f"{external_component}.json"))


def test_member_capture_failure_publishes_partial_generation_without_secrets(
    tmp_path,
    monkeypatch,
):
    output_root = tmp_path / "market"
    secret = "bmAPI-never-persist-this-secret"

    class SuccessfulClient:
        async def get_top_holders(self, target):
            return _api_result(target, "holders", _holders())

        async def get_subgraph(self, target, ranked_addresses):
            return _api_result(target, "subgraph", _subgraph(target.token_address))

        async def get_transfers(self, target, member_address):
            return _api_result(
                target,
                "transfers",
                _transfers(target.token_address),
                member_address=member_address,
            )

    class FailingClient:
        async def get_top_holders(self, target):
            return _api_result(target, "holders", _holders())

        async def get_subgraph(self, target, ranked_addresses):
            return _api_result(target, "subgraph", _subgraph(target.token_address))

        async def get_transfers(self, target, member_address):
            error = RuntimeError(f"X-Validation={secret}")
            error.attempts = 2
            raise error

    generation_ids = iter(["existing-live-sentinel", "failed-generation"])
    monkeypatch.setattr(
        market_cli.uuid,
        "uuid4",
        lambda: type("GenerationId", (), {"hex": next(generation_ids)})(),
    )
    argv = _install_dependencies(monkeypatch, output_root, SuccessfulClient())

    assert market_cli.main(argv) == 0

    live = output_root / DAY.isoformat()
    assert validate_staging_generation(live)["generation_id"] == "existing-live-sentinel"
    _install_dependencies(monkeypatch, output_root, FailingClient())

    assert market_cli.main(argv) == 0

    manifest, errors = read_validated_generation(output_root, DAY)
    assert manifest["generation_id"] == "failed-generation"
    assert manifest["status"] == "partial_success"
    assert len(list((live / "raw").glob("*/*/holders.json"))) == 1
    assert len(list((live / "raw").glob("*/*/subgraph.json"))) == 1
    assert len(list((live / "clean").glob("*/*/holders.json"))) == 1
    assert len(list((live / "clean").glob("*/*/relationships.json"))) == 1
    error_text = (live / "error.json").read_text()
    report = json.loads(error_text)
    assert report == {"error_count": 1, "errors": errors}
    error = errors[0]
    assert error == {
        "chain": "bsc",
        "token_address": TOKEN,
        "member_address": MEMBER,
        "stage": "transfers",
        "type": "RuntimeError",
        "message": "capture failed",
        "attempt_count": 2,
        "captured_at": CAPTURED_AT,
    }
    assert secret not in error_text
    assert SETTINGS.password not in error_text
    assert not (output_root / "_failed" / DAY.isoformat()).exists()


def test_clean_failure_after_successful_request_records_one_attempt(
    tmp_path,
    monkeypatch,
):
    output_root = tmp_path / "market"

    class SuccessfulRequestClient:
        async def get_top_holders(self, target):
            return _api_result(target, "holders", _holders(), attempts=4)

    argv = _install_dependencies(
        monkeypatch,
        output_root,
        SuccessfulRequestClient(),
    )
    argv.extend(["--api-max-attempts", "5"])
    monkeypatch.setattr(
        market_cli,
        "clean_holders",
        lambda _payload, _target: (_ for _ in ()).throw(RuntimeError("clean failed")),
    )

    assert market_cli.main(argv) == 0

    manifest, errors = read_validated_generation(output_root, DAY)
    assert manifest["status"] == "partial_success"
    assert len(errors) == 1
    error = errors[0]
    assert error["stage"] == "holders"
    assert error["attempt_count"] == 1
