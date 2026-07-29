import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from getMarket.bubblemaps.tool.export_bubblemaps_market import main as market_main
from getMarket.bubblemaps.tool.market_artifacts import read_validated_generation


_CHINA_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_PG_ENVIRONMENT = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")


def _live_smoke_skip_reason(environment):
    if environment.get("BUBBLEMAPS_LIVE_SMOKE") != "1":
        return "set BUBBLEMAPS_LIVE_SMOKE=1"
    if (
        environment.get("BUBBLEMAPS_LIVE_API_CONFIRM")
        != "READ_ONLY_ONE_TARGET"
    ):
        return "set BUBBLEMAPS_LIVE_API_CONFIRM=READ_ONLY_ONE_TARGET"
    missing = [name for name in _PG_ENVIRONMENT if not environment.get(name)]
    if missing:
        return "PostgreSQL environment is incomplete: " + ", ".join(missing)
    return None


def _live_smoke_args(output_root: Path):
    return ["--limit", "1", "--output-root", str(output_root)]


def _assert_transfer_reference_sets(live, manifest, entry, token_document):
    ordinary_references = {
        member["transfer_file"]
        for cluster in token_document["clusters"]
        for member in cluster["members"]
        if not member["is_supernode"]
    }
    manifest_references = set(entry["member_files"])
    clean_references = {
        relative
        for relative in entry["clean_files"]
        if Path(relative).parent.name == "transfers"
    }
    assert ordinary_references == manifest_references == clean_references

    for reference in ordinary_references:
        assert type(reference) is str and reference
        relative = Path(reference)
        assert not relative.is_absolute()
        assert all(part not in ("", ".", "..") for part in relative.parts)
        assert reference == relative.as_posix()
        assert reference in manifest["artifacts"]
        assert (live / relative).is_file()


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, "BUBBLEMAPS_LIVE_SMOKE=1"),
        (
            {"BUBBLEMAPS_LIVE_SMOKE": "1"},
            "BUBBLEMAPS_LIVE_API_CONFIRM=READ_ONLY_ONE_TARGET",
        ),
        (
            {
                "BUBBLEMAPS_LIVE_SMOKE": "1",
                "BUBBLEMAPS_LIVE_API_CONFIRM": "READ_ONLY_ONE_TARGET",
            },
            "PostgreSQL environment",
        ),
    ],
)
def test_live_smoke_guard_requires_two_opt_ins_and_postgresql(environment, expected):
    assert expected in _live_smoke_skip_reason(environment)


def test_live_smoke_guard_accepts_explicit_one_target_environment():
    environment = {
        "BUBBLEMAPS_LIVE_SMOKE": "1",
        "BUBBLEMAPS_LIVE_API_CONFIRM": "READ_ONLY_ONE_TARGET",
        **{name: "configured" for name in _PG_ENVIRONMENT},
    }

    assert _live_smoke_skip_reason(environment) is None


def test_live_smoke_arguments_are_bounded_to_one_target(tmp_path):
    assert _live_smoke_args(tmp_path) == [
        "--limit",
        "1",
        "--output-root",
        str(tmp_path),
    ]


def test_transfer_reference_sets_allow_empty_cluster_generation(tmp_path):
    entry = {
        "member_files": [],
        "clean_files": [
            "clean/safe-chain/safe-token/holders.json",
            "clean/safe-chain/safe-token/relationships.json",
        ],
    }
    manifest = {
        "artifacts": {
            relative: {"sha256": "0" * 64}
            for relative in entry["clean_files"]
        },
    }
    token_document = {"clusters": []}

    _assert_transfer_reference_sets(
        tmp_path,
        manifest,
        entry,
        token_document,
    )


@pytest.mark.live_bubblemaps
def test_live_one_token_generation_contract(tmp_path):
    skip_reason = _live_smoke_skip_reason(os.environ)
    if skip_reason is not None:
        pytest.skip(skip_reason)

    output_root = tmp_path / "market"
    earliest_day = datetime.now(_CHINA_TIME_ZONE).date()
    assert market_main(_live_smoke_args(output_root)) == 0
    latest_day = datetime.now(_CHINA_TIME_ZONE).date()

    generation_directories = sorted(
        path
        for path in output_root.iterdir()
        if path.is_dir() and not path.name.startswith("_")
    )
    assert len(generation_directories) == 1
    business_date = datetime.strptime(
        generation_directories[0].name,
        "%Y-%m-%d",
    ).date()
    assert earliest_day <= business_date <= latest_day

    manifest, errors = read_validated_generation(output_root, business_date)
    assert errors == []
    assert manifest["business_date"] == business_date.isoformat()
    assert len(manifest["tokens"]) == 1

    live = generation_directories[0]
    targets = json.loads((live / "targets.json").read_text(encoding="utf-8"))
    assert sum(len(addresses) for addresses in targets.values()) == 1
    assert targets == manifest["targets"]

    entry = manifest["tokens"][0]
    assert entry["raw_files"]
    assert entry["clean_files"]
    assert (live / entry["token_file"]).is_file()
    assert all((live / relative).is_file() for relative in entry["raw_files"])
    assert all((live / relative).is_file() for relative in entry["clean_files"])

    token_document = json.loads(
        (live / entry["token_file"]).read_text(encoding="utf-8")
    )
    _assert_transfer_reference_sets(
        live,
        manifest,
        entry,
        token_document,
    )
    assert all(
        "transfers" not in member
        for cluster in token_document["clusters"]
        for member in cluster["members"]
        if not member["is_supernode"]
    )
