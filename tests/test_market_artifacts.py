import hashlib
import json
import os
import stat
from contextlib import AbstractContextManager
from itertools import repeat
from typing import get_type_hints
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from copy import deepcopy

import pytest

import getMarket.bubblemaps.tool.market_artifacts as market_artifacts
from common.artifacts import safe_path_component
from getMarket.bubblemaps.tool.bubblemaps_api import ApiResult, RequestMetadata
from getMarket.bubblemaps.tool.market_artifacts import (
    MarketGenerationLockError,
    MarketGenerationValidationError,
    PublicationRecoveryError,
    RunPaths,
    generation_lock,
    hash_file_streaming,
    preserve_failed_run,
    publish_success,
    read_validated_generation,
    recover_interrupted_publish,
    validate_relative_artifact_path,
    validate_staging_generation,
    write_success_manifest,
)
from getMarket.bubblemaps.tool.market_identity import make_target


TOKEN = "0x1111111111111111111111111111111111111111"
MEMBER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SUPERNODE = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
EXTERNAL = "0x9999999999999999999999999999999999999999"
CAPTURED_AT = "2026-07-22T12:30:00Z"
ERROR_RECORD = {
    "chain": "bsc",
    "token_address": TOKEN,
    "stage": "member",
    "type": "RuntimeError",
    "message": "capture failed",
    "captured_at": CAPTURED_AT,
}


def _nested_list(depth: int) -> list:
    value: list = []
    for _ in range(depth):
        value = [value]
    return value


def _api_result(
    *,
    method: str,
    url: str,
    payload: object | None = None,
) -> ApiResult:
    return ApiResult(
        payload=[] if payload is None else payload,
        metadata=RequestMetadata(
            method=method,
            url=url,
            status=200,
            attempts=1,
            request_chain="bsc",
            request_token_address=TOKEN,
        ),
    )


def _official_api_result(
    target,
    kind: str,
    payload: object,
    *,
    member_address: str | None = None,
) -> ApiResult:
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
    elif kind == "transfers" and member_address is not None:
        method = "GET"
        url = (
            "https://api.bubblemaps.io/relationships/transfers"
            f"?address={member_address}"
            f"&whitelist_token_address={target.requested_token_address}"
            f"&whitelist_token_chain={target.chain}"
        )
    else:
        raise ValueError("official API result fixture kind is invalid")
    return ApiResult(
        payload=payload,
        metadata=RequestMetadata(
            method=method,
            url=url,
            status=200,
            attempts=1,
            request_chain=target.requested_chain,
            request_token_address=target.requested_token_address,
        ),
    )


def _relative_files(root: Path) -> set[str]:
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
    }


def _seed_success_staging(
    root: Path,
    *,
    generation_id: str = "generation-1",
) -> dict:
    manifest, _raw_by_member = _seed_two_member_transfer_api_staging(
        root,
        generation_id=generation_id,
    )
    return manifest


def _seed_empty_api_staging(
    root: Path,
    *,
    generation_id: str = "generation-1",
) -> dict:
    market_artifacts.write_targets(root, {})
    manifest = market_artifacts.build_api_manifest(
        root,
        generation_id=generation_id,
        business_date=date(2026, 7, 22),
        captured_at=CAPTURED_AT,
        targets={},
        entries=[],
    )
    write_success_manifest(root, manifest)
    return manifest


def _api_skipped_entry(
    *,
    chain: str = "bsc",
    requested_address: str = TOKEN,
) -> dict:
    target = make_target(chain, requested_address)
    return {
        "requested_chain": target.requested_chain,
        "requested_token_address": target.requested_token_address,
        "canonical_chain": target.chain,
        "canonical_token_address": target.token_address,
        "stage": "holders",
        "http_status": 400,
        "attempt_count": 1,
        "reason": "top_holders_not_available",
        "captured_at": CAPTURED_AT,
        "status": "skipped",
    }


def _write_empty_api_target_artifacts(
    root: Path,
    *,
    chain: str,
    requested_address: str,
) -> dict:
    target = make_target(chain, requested_address)
    raw_holders = market_artifacts.write_raw_response(
        root,
        target,
        "holders",
        _official_api_result(target, "holders", []),
    )
    raw_subgraph = market_artifacts.write_raw_response(
        root,
        target,
        "subgraph",
        _official_api_result(target, "subgraph", []),
    )
    clean_holders = market_artifacts.write_clean_response(
        root,
        target,
        "holders",
        [],
    )
    clean_relationships = market_artifacts.write_clean_response(
        root,
        target,
        "relationships",
        [],
    )
    token_file = market_artifacts.write_final_token(
        root,
        target,
        {
            "schema_version": "v3",
            "chain": target.requested_chain,
            "token_address": target.requested_token_address,
            "canonical_chain": target.chain,
            "canonical_token_address": target.token_address,
            "captured_at": CAPTURED_AT,
            "clusters": [],
        },
    )
    return {
        "requested_chain": target.requested_chain,
        "requested_token_address": target.requested_token_address,
        "canonical_chain": target.chain,
        "canonical_token_address": target.token_address,
        "captured_at": CAPTURED_AT,
        "token_file": token_file,
        "member_files": [],
        "raw_files": [raw_holders, raw_subgraph],
        "clean_files": [clean_holders, clean_relationships],
        "cluster_count": 0,
        "ranked_holder_count": 0,
        "clustered_member_count": 0,
        "ordinary_member_count": 0,
        "supernode_count": 0,
        "unique_transfer_count": 0,
        "transfer_view_count": 0,
        "status": "success",
    }


def _seed_semantic_api_staging(
    root: Path,
    *,
    first_holder_label: str = "member",
    generation_id: str = "generation-1",
) -> dict:
    target = make_target("bsc", TOKEN)
    holders = [
        {
            "address": MEMBER,
            "address_details": {
                "is_supernode": False,
                "label": first_holder_label,
            },
            "holder_data": {"rank": 1, "amount": "60", "share": "0.3"},
        },
        {
            "address": SUPERNODE,
            "address_details": {"is_supernode": True, "label": "supernode"},
            "holder_data": {"rank": 2, "amount": "40", "share": "0.2"},
        },
    ]
    relationships = [
        {
            "from_address": MEMBER,
            "to_address": SUPERNODE,
            "rel_type": "GROUPED_TRANSFER",
            "data": {
                "total_transfers": 1,
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        }
    ]
    targets = {"bsc": [TOKEN]}
    market_artifacts.write_targets(root, targets)
    raw_holders = market_artifacts.write_raw_response(
        root,
        target,
        "holders",
        _official_api_result(target, "holders", holders),
    )
    raw_subgraph = market_artifacts.write_raw_response(
        root,
        target,
        "subgraph",
        _official_api_result(target, "subgraph", relationships),
    )
    clean_holders = market_artifacts.write_clean_response(
        root,
        target,
        "holders",
        holders,
    )
    clean_relationships = market_artifacts.write_clean_response(
        root,
        target,
        "relationships",
        relationships,
    )
    token_file = market_artifacts.write_final_token(
        root,
        target,
        {
            "schema_version": "v3",
            "chain": "bsc",
            "token_address": TOKEN,
            "canonical_chain": "bsc",
            "canonical_token_address": TOKEN,
            "captured_at": CAPTURED_AT,
            "clusters": [],
        },
    )
    entry = {
        "requested_chain": "bsc",
        "requested_token_address": TOKEN,
        "canonical_chain": "bsc",
        "canonical_token_address": TOKEN,
        "captured_at": CAPTURED_AT,
        "token_file": token_file,
        "member_files": [],
        "raw_files": [raw_holders, raw_subgraph],
        "clean_files": [clean_holders, clean_relationships],
        "cluster_count": 0,
        "ranked_holder_count": 2,
        "clustered_member_count": 0,
        "ordinary_member_count": 0,
        "supernode_count": 0,
        "unique_transfer_count": 0,
        "transfer_view_count": 0,
        "status": "success",
    }
    manifest = market_artifacts.build_api_manifest(
        root,
        generation_id=generation_id,
        business_date=date(2026, 7, 22),
        captured_at=CAPTURED_AT,
        targets=targets,
        entries=[entry],
    )
    write_success_manifest(root, manifest)
    return manifest


def _seed_two_member_transfer_api_staging(
    root: Path,
    *,
    generation_id: str = "generation-1",
) -> tuple[dict, dict[str, str]]:
    target = make_target("bsc", TOKEN)
    members = (MEMBER, SUPERNODE)
    holders = [
        {
            "address": address,
            "address_details": {"is_supernode": False},
            "holder_data": {"rank": rank, "amount": "50", "share": "0.25"},
        }
        for rank, address in enumerate(members, start=1)
    ]
    transfers = {
        MEMBER: {
            "from_address": MEMBER,
            "to_address": EXTERNAL,
            "rel_type": "TRANSFER",
            "data": {
                "value": "1",
                "date": 1,
                "tx_hash": "0xmember",
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        },
        SUPERNODE: {
            "from_address": SUPERNODE,
            "to_address": EXTERNAL,
            "rel_type": "TRANSFER",
            "data": {
                "value": "2",
                "date": 2,
                "tx_hash": "0xsecond",
                "token_ref": {"chain": "bsc", "address": TOKEN},
            },
        },
    }
    targets = {"bsc": [TOKEN]}
    market_artifacts.write_targets(root, targets)
    raw_files = [
        market_artifacts.write_raw_response(
            root,
            target,
            "holders",
            _official_api_result(target, "holders", holders),
        ),
        market_artifacts.write_raw_response(
            root,
            target,
            "subgraph",
            _official_api_result(target, "subgraph", []),
        ),
    ]
    clean_files = [
        market_artifacts.write_clean_response(root, target, "holders", holders),
        market_artifacts.write_clean_response(root, target, "relationships", []),
    ]
    raw_by_member = {}
    clean_by_member = {}
    for rank, address in enumerate(members, start=1):
        raw_by_member[address] = market_artifacts.write_raw_response(
            root,
            target,
            f"transfers/{address}",
            _official_api_result(
                target,
                "transfers",
                [transfers[address]],
                member_address=address,
            ),
        )
        clean_by_member[address] = market_artifacts.write_clean_member_transfers(
            root,
            target,
            address,
            [transfers[address]],
            cluster_rank=1,
        )
    raw_files.extend(raw_by_member.values())
    clean_files.extend(clean_by_member.values())
    token_file = market_artifacts.write_final_token(
        root,
        target,
        {
            "schema_version": "v3",
            "chain": "bsc",
            "token_address": TOKEN,
            "canonical_chain": "bsc",
            "canonical_token_address": TOKEN,
            "captured_at": CAPTURED_AT,
            "clusters": [
                {
                    "cluster_rank": 1,
                    "amount": "100",
                    "share": "0.50",
                    "share_percent": "50.00",
                    "member_count": 2,
                    "members": [
                        {
                            "member_rank": rank,
                            "source_rank": rank,
                            "address": address,
                            "amount": "50",
                            "share": "0.25",
                            "share_percent": "25.00",
                            "is_supernode": False,
                            "metadata": {},
                            "transfer_details_available": True,
                            "transfer_count": 1,
                            "transfer_file": clean_by_member[address],
                        }
                        for rank, address in enumerate(members, start=1)
                    ],
                }
            ],
        },
    )
    entry = {
        "requested_chain": "bsc",
        "requested_token_address": TOKEN,
        "canonical_chain": "bsc",
        "canonical_token_address": TOKEN,
        "captured_at": CAPTURED_AT,
        "token_file": token_file,
        "member_files": list(clean_by_member.values()),
        "raw_files": raw_files,
        "clean_files": clean_files,
        "cluster_count": 1,
        "ranked_holder_count": 2,
        "clustered_member_count": 2,
        "ordinary_member_count": 2,
        "supernode_count": 0,
        "unique_transfer_count": 2,
        "transfer_view_count": 2,
        "status": "success",
    }
    manifest = market_artifacts.build_api_manifest(
        root,
        generation_id=generation_id,
        business_date=date(2026, 7, 22),
        captured_at=CAPTURED_AT,
        targets=targets,
        entries=[entry],
    )
    write_success_manifest(root, manifest)
    return manifest, raw_by_member


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _rewrite_manifest(root: Path, manifest: dict) -> None:
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, sort_keys=True),
        encoding="utf-8",
    )


def _refresh_artifact_hash(root: Path, manifest: dict, relative: str) -> None:
    manifest["artifacts"][relative]["sha256"] = hashlib.sha256(
        (root / relative).read_bytes()
    ).hexdigest()


def test_validation_rejects_transfer_absent_from_token_raw_union() -> None:
    real_transfer = {
        "from_address": MEMBER,
        "to_address": EXTERNAL,
        "rel_type": "TRANSFER",
        "data": {
            "value": "1",
            "date": 1,
            "tx_hash": "0xreal",
            "token_ref": {"chain": "bsc", "address": TOKEN},
        },
    }
    fabricated_transfer = deepcopy(real_transfer)
    fabricated_transfer["data"]["tx_hash"] = "0xfabricated"

    with pytest.raises(ValueError, match="absent from raw response union"):
        market_artifacts._require_formal_transfers_in_raw_union(
            {MEMBER: {"transfers": [fabricated_transfer]}},
            [real_transfer],
        )


def test_run_paths_keep_transient_trees_outside_live_date(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")

    assert paths.output_root == tmp_path
    assert paths.business_date == date(2026, 7, 22)
    assert paths.generation_id == "generation-1"
    assert paths.live == tmp_path / "2026-07-22"
    assert paths.staging == tmp_path / "_staging/2026-07-22/generation-1"
    assert paths.failed == tmp_path / "_failed/2026-07-22/generation-1"
    assert paths.backup == tmp_path / "_backups/2026-07-22/generation-1"
    assert paths.lock_file == tmp_path / "_locks/2026-07-22.lock"


@pytest.mark.parametrize("generation_id", ["", ".", "..", "a/b", "/tmp/x"])
def test_run_paths_reject_unsafe_generation_ids(tmp_path, generation_id) -> None:
    with pytest.raises(ValueError):
        RunPaths.create(tmp_path, date(2026, 7, 22), generation_id)


def test_run_paths_canonicalizes_trusted_macos_var_alias() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "market-output"
        paths = RunPaths.create(root, date(2026, 7, 22), "generation-1")

        assert paths.output_root == Path(os.path.realpath(root))
        with generation_lock(paths, shared=False):
            pass


def test_v3_writers_canonicalize_trusted_macos_var_alias() -> None:
    with TemporaryDirectory() as directory:
        staging = Path(directory) / "api-staging"
        resolved_staging = Path(os.path.realpath(staging))
        if staging == resolved_staging:
            pytest.skip("platform temporary directory has no filesystem alias")

        targets_file = market_artifacts.write_targets(staging, {})
        manifest = market_artifacts.build_api_manifest(
            staging,
            generation_id="generation-1",
            business_date=date(2026, 7, 22),
            captured_at=CAPTURED_AT,
            targets={},
            entries=[],
        )
        write_success_manifest(staging, manifest)

        assert targets_file == "targets.json"
        assert (resolved_staging / targets_file).is_file()
        assert validate_staging_generation(staging) == manifest


def test_api_staging_root_preserves_var_when_it_is_a_real_directory(
    monkeypatch,
) -> None:
    class DirectoryMetadata:
        st_mode = stat.S_IFDIR | 0o755

    def fake_lstat(path):
        if Path(path) == Path("/var"):
            return DirectoryMetadata()
        raise FileNotFoundError(path)

    monkeypatch.setattr(market_artifacts.os, "lstat", fake_lstat)

    staging = Path("/var/nonexistent-dbcompare/generation-1")
    assert market_artifacts._trusted_api_staging_root(staging) == staging


def test_api_staging_root_maps_only_confirmed_var_system_alias(monkeypatch) -> None:
    class SymlinkMetadata:
        st_mode = stat.S_IFLNK | 0o777

    def fake_lstat(path):
        if Path(path) == Path("/var"):
            return SymlinkMetadata()
        raise FileNotFoundError(path)

    def fake_realpath(path):
        assert Path(path) == Path("/var")
        return "/private/var"

    monkeypatch.setattr(market_artifacts.os, "lstat", fake_lstat)
    monkeypatch.setattr(market_artifacts.os, "readlink", lambda path: "private/var")
    monkeypatch.setattr(market_artifacts.os.path, "realpath", fake_realpath)

    staging = Path("/var/nonexistent-dbcompare/generation-1")
    assert market_artifacts._trusted_api_staging_root(staging) == Path(
        "/private/var/nonexistent-dbcompare/generation-1"
    )


def test_v3_writers_reject_intermediate_staging_symlink_without_external_write(
    tmp_path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (output / "_staging").symlink_to(external, target_is_directory=True)
    staging = output / "_staging" / "2026-07-22" / "generation-1"

    with pytest.raises(ValueError, match="generation root|symlink"):
        market_artifacts.write_targets(staging, {})

    assert not (external / "2026-07-22" / "generation-1" / "targets.json").exists()


def test_run_paths_rejects_output_root_that_is_itself_a_symlink(tmp_path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    alias = tmp_path / "root-alias"
    alias.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ValueError, match="output_root|symlink"):
        RunPaths.create(alias, date(2026, 7, 22), "generation-1")


def test_hash_file_streaming_reads_in_bounded_chunks(tmp_path, monkeypatch) -> None:
    path = tmp_path / "large.json"
    payload = b"x" * (3 * 31 + 17)
    path.write_bytes(payload)
    read_sizes = []
    real_open = Path.open

    class RecordingReader:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        def read(self, size=-1):
            read_sizes.append(size)
            return self.wrapped.read(size)

    def recording_open(self, *args, **kwargs):
        opened = real_open(self, *args, **kwargs)
        if self == path:
            opened.__enter__()
            return RecordingReader(opened)
        return opened

    monkeypatch.setattr(Path, "open", recording_open)

    assert hash_file_streaming(path, chunk_size=31) == hashlib.sha256(payload).hexdigest()
    assert read_sizes == [31, 31, 31, 31, 31]


@pytest.mark.parametrize(
    "relative",
    [Path("../escape"), Path("a/../../escape"), Path("/absolute"), Path(".")],
)
def test_artifact_paths_reject_escape_absolute_and_empty_paths(relative) -> None:
    with pytest.raises(ValueError):
        validate_relative_artifact_path(relative)


def test_artifact_path_validation_returns_unchanged_safe_path() -> None:
    relative = Path("raw/bsc/token/holders.json")
    assert validate_relative_artifact_path(relative) == relative


def test_api_artifacts_write_v3_tree_and_bind_clean_member_files(tmp_path) -> None:
    target = make_target("bsc", TOKEN)
    targets = {"bsc": [TOKEN]}
    holder_payload = [
        {
            "address": MEMBER,
            "address_details": {"is_supernode": False},
            "holder_data": {"rank": 1, "amount": "60", "share": "0.3"},
        },
        {
            "address": SUPERNODE,
            "address_details": {"is_supernode": True},
            "holder_data": {"rank": 2, "amount": "40", "share": "0.2"},
        },
    ]
    transfer = {
        "from_address": MEMBER,
        "to_address": EXTERNAL,
        "rel_type": "TRANSFER",
        "data": {
            "value": "1",
            "date": 1,
            "tx_hash": "0xexternal",
            "token_ref": {"chain": "bsc", "address": TOKEN},
        },
    }
    transfer_file = str(
        market_artifacts._token_relative_root("clean", target)
        / "transfers"
        / f"{safe_path_component(MEMBER)}.json"
    )
    token_document = {
        "schema_version": "v3",
        "chain": "bsc",
        "token_address": TOKEN,
        "canonical_chain": "bsc",
        "canonical_token_address": TOKEN,
        "captured_at": CAPTURED_AT,
        "clusters": [
            {
                "cluster_rank": 1,
                "amount": "100",
                "share": "0.5",
                "share_percent": "50.0",
                "member_count": 2,
                "members": [
                    {
                        "member_rank": 1,
                        "source_rank": 1,
                        "address": MEMBER,
                        "amount": "60",
                        "share": "0.3",
                        "share_percent": "30.0",
                        "is_supernode": False,
                        "metadata": {},
                        "transfer_details_available": True,
                        "transfer_count": 1,
                        "transfer_file": transfer_file,
                    },
                    {
                        "member_rank": 2,
                        "source_rank": 2,
                        "address": SUPERNODE,
                        "amount": "40",
                        "share": "0.2",
                        "share_percent": "20.0",
                        "is_supernode": True,
                        "metadata": {},
                        "transfer_details_available": False,
                        "transfer_details_reason": "supernode_not_supported",
                        "transfer_count": 0,
                        "transfer_file": None,
                    },
                ],
            }
        ],
    }

    targets_file = market_artifacts.write_targets(tmp_path, targets)
    raw_holders = market_artifacts.write_raw_response(
        tmp_path,
        target,
        "holders",
        _official_api_result(target, "holders", holder_payload),
    )
    raw_subgraph = market_artifacts.write_raw_response(
        tmp_path,
        target,
        "subgraph",
        _official_api_result(target, "subgraph", []),
    )
    raw_transfers = market_artifacts.write_raw_response(
        tmp_path,
        target,
        f"transfers/{MEMBER}",
        _official_api_result(
            target,
            "transfers",
            [transfer],
            member_address=MEMBER,
        ),
    )
    clean_holders = market_artifacts.write_clean_response(
        tmp_path,
        target,
        "holders",
        holder_payload,
    )
    clean_relationships = market_artifacts.write_clean_response(
        tmp_path,
        target,
        "relationships",
        [],
    )
    clean_transfers = market_artifacts.write_clean_member_transfers(
        tmp_path,
        target,
        MEMBER,
        [transfer],
        cluster_rank=1,
    )
    token_file = market_artifacts.write_final_token(
        tmp_path,
        target,
        token_document,
    )
    entry = {
        "requested_chain": "bsc",
        "requested_token_address": TOKEN,
        "canonical_chain": "bsc",
        "canonical_token_address": TOKEN,
        "captured_at": CAPTURED_AT,
        "token_file": token_file,
        "member_files": [clean_transfers],
        "raw_files": [raw_holders, raw_subgraph, raw_transfers],
        "clean_files": [clean_holders, clean_relationships, clean_transfers],
        "cluster_count": 1,
        "ranked_holder_count": 2,
        "clustered_member_count": 2,
        "ordinary_member_count": 1,
        "supernode_count": 1,
        "unique_transfer_count": 1,
        "transfer_view_count": 1,
        "status": "success",
    }
    manifest = market_artifacts.build_api_manifest(
        tmp_path,
        generation_id="generation-1",
        business_date=date(2026, 7, 22),
        captured_at=CAPTURED_AT,
        targets=targets,
        entries=[entry],
    )
    write_success_manifest(tmp_path, manifest)

    assert _relative_files(tmp_path) == {
        targets_file,
        raw_holders,
        raw_subgraph,
        raw_transfers,
        clean_holders,
        clean_relationships,
        clean_transfers,
        token_file,
        "manifest.json",
    }
    assert manifest["schema_version"] == "v3"
    assert manifest["source"] == "bubblemaps_api"
    assert "input_generation" not in manifest
    assert manifest["targets"] == targets
    assert token_document["clusters"][0]["members"][0]["transfer_file"] == transfer_file
    assert not (tmp_path / "data" / "bsc" / TOKEN / "transfers").exists()
    assert validate_staging_generation(tmp_path) == manifest

    (tmp_path / clean_transfers).unlink()
    with pytest.raises(
        MarketGenerationValidationError,
        match="artifact|clean|member|empty",
    ):
        validate_staging_generation(tmp_path)

    invalid_entry = deepcopy(entry)
    invalid_entry["clean_files"][-1] = (
        f"clean/bsc/{EXTERNAL}/transfers/{safe_path_component(MEMBER)}.json"
    )
    with pytest.raises(ValueError, match="clean|target"):
        market_artifacts.build_api_manifest(
            tmp_path,
            generation_id="generation-2",
            business_date=date(2026, 7, 22),
            captured_at=CAPTURED_AT,
            targets=targets,
            entries=[invalid_entry],
        )


def test_api_validation_rejects_substituted_raw_holders_after_rehash(tmp_path) -> None:
    manifest = _seed_semantic_api_staging(tmp_path)
    holder_file = next(
        path
        for path in manifest["tokens"][0]["raw_files"]
        if path.endswith("holders.json")
    )
    document = json.loads((tmp_path / holder_file).read_text())
    document["payload"] = [
        {
            "address": EXTERNAL,
            "address_details": {"is_supernode": False},
            "holder_data": {"rank": 1, "amount": "1", "share": "0.01"},
        }
    ]
    (tmp_path / holder_file).write_text(json.dumps(document), encoding="utf-8")
    _refresh_artifact_hash(tmp_path, manifest, holder_file)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError, match="API|holder|clean|raw"):
        validate_staging_generation(tmp_path)


def test_api_validation_rejects_substituted_raw_subgraph_after_rehash(
    tmp_path,
) -> None:
    manifest = _seed_semantic_api_staging(tmp_path)
    subgraph_file = next(
        path
        for path in manifest["tokens"][0]["raw_files"]
        if path.endswith("subgraph.json")
    )
    document = json.loads((tmp_path / subgraph_file).read_text())
    document["payload"] = []
    (tmp_path / subgraph_file).write_text(json.dumps(document), encoding="utf-8")
    _refresh_artifact_hash(tmp_path, manifest, subgraph_file)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError, match="API|relationship|clean|raw"):
        validate_staging_generation(tmp_path)


def test_api_validation_accepts_swapped_member_raw_transfers_when_union_is_unchanged(
    tmp_path,
) -> None:
    manifest, raw_by_member = _seed_two_member_transfer_api_staging(tmp_path)
    assert validate_staging_generation(tmp_path) == manifest
    first_path = tmp_path / raw_by_member[MEMBER]
    second_path = tmp_path / raw_by_member[SUPERNODE]
    first_document = json.loads(first_path.read_text())
    second_document = json.loads(second_path.read_text())
    first_document["payload"], second_document["payload"] = (
        second_document["payload"],
        first_document["payload"],
    )
    first_path.write_text(json.dumps(first_document), encoding="utf-8")
    second_path.write_text(json.dumps(second_document), encoding="utf-8")
    _refresh_artifact_hash(tmp_path, manifest, raw_by_member[MEMBER])
    _refresh_artifact_hash(tmp_path, manifest, raw_by_member[SUPERNODE])
    _rewrite_manifest(tmp_path, manifest)

    assert validate_staging_generation(tmp_path) == manifest


def test_api_manifest_validation_rejects_sensitive_values_after_rewrite(
    tmp_path,
) -> None:
    write_targets = market_artifacts.write_targets
    build_api_manifest = market_artifacts.build_api_manifest

    write_targets(tmp_path, {})
    manifest = build_api_manifest(
        tmp_path,
        generation_id="generation-1",
        business_date=date(2026, 7, 22),
        captured_at=CAPTURED_AT,
        targets={},
        entries=[],
    )
    manifest["generation_id"] = "Authorization=Bearer should-not-persist"
    write_success_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError, match="manifest|API|sensitive"):
        validate_staging_generation(tmp_path)


def test_only_skipped_api_generation_builds_publishes_validates_and_reads(
    tmp_path,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    targets = {"bsc": [TOKEN]}
    targets_file = market_artifacts.write_targets(paths.staging, targets)
    skipped = _api_skipped_entry()

    manifest = market_artifacts.build_api_manifest(
        paths.staging,
        generation_id="generation-1",
        business_date=date(2026, 7, 22),
        captured_at=CAPTURED_AT,
        targets=targets,
        entries=[],
        skipped_entries=[skipped],
    )
    write_success_manifest(paths.staging, manifest)

    assert manifest["status"] == "partial_success"
    assert manifest["tokens"] == []
    assert manifest["skipped_tokens"] == [skipped]
    assert set(manifest["artifacts"]) == {targets_file}
    assert validate_staging_generation(paths.staging) == manifest

    publish_success(paths)
    validated, errors = read_validated_generation(tmp_path, date(2026, 7, 22))

    assert _relative_files(paths.live) == {targets_file, "manifest.json"}
    assert validated == manifest
    assert errors == []


def test_success_api_manifest_exposes_empty_skipped_tokens(tmp_path) -> None:
    manifest = _seed_empty_api_staging(tmp_path)

    assert manifest["status"] == "success"
    assert manifest["skipped_tokens"] == []
    assert validate_staging_generation(tmp_path) == manifest


def test_api_manifest_accepts_success_and_skipped_exact_target_partition(
    tmp_path,
) -> None:
    success_manifest = _seed_semantic_api_staging(tmp_path)
    (tmp_path / "manifest.json").unlink()
    targets = {"bsc": sorted([TOKEN, EXTERNAL])}
    market_artifacts.write_targets(tmp_path, targets)
    skipped = _api_skipped_entry(requested_address=EXTERNAL)

    manifest = market_artifacts.build_api_manifest(
        tmp_path,
        generation_id="generation-1",
        business_date=date(2026, 7, 22),
        captured_at=CAPTURED_AT,
        targets=targets,
        entries=success_manifest["tokens"],
        skipped_entries=[skipped],
    )
    write_success_manifest(tmp_path, manifest)

    assert manifest["status"] == "partial_success"
    assert manifest["skipped_tokens"] == [skipped]
    assert validate_staging_generation(tmp_path) == manifest


def test_api_manifest_rejects_target_omitted_from_success_and_skipped(tmp_path) -> None:
    targets = {"bsc": sorted([TOKEN, EXTERNAL])}
    market_artifacts.write_targets(tmp_path, targets)

    with pytest.raises(ValueError, match="target"):
        market_artifacts.build_api_manifest(
            tmp_path,
            generation_id="generation-1",
            business_date=date(2026, 7, 22),
            captured_at=CAPTURED_AT,
            targets=targets,
            entries=[],
            skipped_entries=[_api_skipped_entry()],
        )


def test_api_manifest_rejects_success_skipped_identity_overlap(tmp_path) -> None:
    targets = {"bsc": [TOKEN]}
    market_artifacts.write_targets(tmp_path, targets)
    entry = _write_empty_api_target_artifacts(
        tmp_path,
        chain="bsc",
        requested_address=TOKEN,
    )

    with pytest.raises(ValueError, match="overlap"):
        market_artifacts.build_api_manifest(
            tmp_path,
            generation_id="generation-1",
            business_date=date(2026, 7, 22),
            captured_at=CAPTURED_AT,
            targets=targets,
            entries=[entry],
            skipped_entries=[_api_skipped_entry()],
        )


def test_api_manifest_rejects_duplicate_skipped_identity(tmp_path) -> None:
    targets = {"bsc": [TOKEN]}
    market_artifacts.write_targets(tmp_path, targets)
    skipped = _api_skipped_entry()

    with pytest.raises(ValueError, match="duplicate"):
        market_artifacts.build_api_manifest(
            tmp_path,
            generation_id="generation-1",
            business_date=date(2026, 7, 22),
            captured_at=CAPTURED_AT,
            targets=targets,
            entries=[],
            skipped_entries=[skipped, deepcopy(skipped)],
        )


def test_api_manifest_rejects_invalid_skipped_entries_sequence(tmp_path) -> None:
    market_artifacts.write_targets(tmp_path, {})

    with pytest.raises(ValueError, match="skipped.*finite sequence"):
        market_artifacts.build_api_manifest(
            tmp_path,
            generation_id="generation-1",
            business_date=date(2026, 7, 22),
            captured_at=CAPTURED_AT,
            targets={},
            entries=[],
            skipped_entries=None,
        )


def test_api_manifest_safely_rejects_failing_skipped_entries_iterator(
    tmp_path,
) -> None:
    class FailingEntries:
        def __iter__(self):
            raise RuntimeError("unsafe iterator detail")

    market_artifacts.write_targets(tmp_path, {})

    with pytest.raises(ValueError, match="skipped.*finite sequence") as captured:
        market_artifacts.build_api_manifest(
            tmp_path,
            generation_id="generation-1",
            business_date=date(2026, 7, 22),
            captured_at=CAPTURED_AT,
            targets={},
            entries=[],
            skipped_entries=FailingEntries(),
        )

    assert "unsafe iterator detail" not in str(captured.value)


def test_api_manifest_safely_rejects_infinite_skipped_entries_iterable(
    tmp_path,
) -> None:
    targets = {"bsc": [TOKEN]}
    market_artifacts.write_targets(tmp_path, targets)

    with pytest.raises(ValueError, match="skipped.*finite sequence"):
        market_artifacts.build_api_manifest(
            tmp_path,
            generation_id="generation-1",
            business_date=date(2026, 7, 22),
            captured_at=CAPTURED_AT,
            targets=targets,
            entries=[],
            skipped_entries=repeat(_api_skipped_entry()),
        )


def test_api_manifest_accepts_finite_skipped_entries_generator(tmp_path) -> None:
    targets = {"bsc": [TOKEN]}
    market_artifacts.write_targets(tmp_path, targets)
    skipped = _api_skipped_entry()

    manifest = market_artifacts.build_api_manifest(
        tmp_path,
        generation_id="generation-1",
        business_date=date(2026, 7, 22),
        captured_at=CAPTURED_AT,
        targets=targets,
        entries=[],
        skipped_entries=(entry for entry in [skipped]),
    )

    assert manifest["skipped_tokens"] == [skipped]


def test_api_skipped_document_returns_a_new_normalized_dictionary() -> None:
    skipped = _api_skipped_entry()

    normalized = market_artifacts._api_skipped_document(
        skipped,
        targets={"bsc": [TOKEN]},
    )

    assert normalized == skipped
    assert normalized is not skipped


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("stage", "subgraph"),
        ("http_status", 404),
        ("http_status", True),
        ("attempt_count", 0),
        ("attempt_count", True),
        ("reason", "request_failed"),
        ("status", "failed"),
        ("captured_at", "2026-07-22T12:30:00+00:00"),
        ("canonical_chain", "eth"),
        ("canonical_token_address", EXTERNAL),
        ("requested_token_address", EXTERNAL),
    ],
)
def test_api_skipped_document_rejects_invalid_field_mutations(field, invalid) -> None:
    skipped = _api_skipped_entry()
    skipped[field] = invalid

    with pytest.raises(ValueError, match="skipped|target|identity|captured_at"):
        market_artifacts._api_skipped_document(
            skipped,
            targets={"bsc": [TOKEN]},
        )


def test_api_skipped_document_rejects_nonexact_shape() -> None:
    skipped = _api_skipped_entry()
    skipped["raw_files"] = []

    with pytest.raises(ValueError, match="shape"):
        market_artifacts._api_skipped_document(
            skipped,
            targets={"bsc": [TOKEN]},
        )


@pytest.mark.parametrize(
    ("targets", "skipped_entries", "status"),
    [
        ({"bsc": [TOKEN]}, [_api_skipped_entry()], "success"),
        ({}, [], "partial_success"),
    ],
)
def test_api_manifest_shape_rejects_status_skipped_list_inconsistency(
    tmp_path,
    targets,
    skipped_entries,
    status,
) -> None:
    market_artifacts.write_targets(tmp_path, targets)
    manifest = market_artifacts.build_api_manifest(
        tmp_path,
        generation_id="generation-1",
        business_date=date(2026, 7, 22),
        captured_at=CAPTURED_AT,
        targets=targets,
        entries=[],
        skipped_entries=skipped_entries,
    )
    manifest["status"] = status

    with pytest.raises(MarketGenerationValidationError, match="status|success|content"):
        market_artifacts._validate_api_manifest_shape(manifest)


def test_write_targets_preserves_requested_evm_representation(tmp_path) -> None:
    uppercase_evm = "0xABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"

    market_artifacts.write_targets(tmp_path, {"bsc": [uppercase_evm]})

    assert json.loads((tmp_path / "targets.json").read_text()) == {
        "bsc": [uppercase_evm]
    }


def test_write_targets_preserves_valid_requested_ton_representation(tmp_path) -> None:
    requested_raw = "0:" + "AB" * 32

    market_artifacts.write_targets(tmp_path, {"ton": [requested_raw]})

    assert json.loads((tmp_path / "targets.json").read_text()) == {
        "ton": [requested_raw]
    }


def test_build_api_manifest_rejects_entry_representation_absent_from_targets(
    tmp_path,
) -> None:
    requested_address = "0xABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"
    canonical_address = requested_address.lower()
    targets = {"bsc": [canonical_address]}
    market_artifacts.write_targets(tmp_path, targets)
    entry = _write_empty_api_target_artifacts(
        tmp_path,
        chain="bsc",
        requested_address=requested_address,
    )

    with pytest.raises(ValueError, match="requested target|target list"):
        market_artifacts.build_api_manifest(
            tmp_path,
            generation_id="generation-1",
            business_date=date(2026, 7, 22),
            captured_at=CAPTURED_AT,
            targets=targets,
            entries=[entry],
        )


def test_api_manifest_shape_rejects_ton_entry_representation_absent_from_targets(
    tmp_path,
) -> None:
    requested_address = "0:" + "AB" * 32
    selected_address = requested_address.lower()
    initial_targets = {"ton": [requested_address]}
    market_artifacts.write_targets(tmp_path, initial_targets)
    entry = _write_empty_api_target_artifacts(
        tmp_path,
        chain="ton",
        requested_address=requested_address,
    )
    manifest = market_artifacts.build_api_manifest(
        tmp_path,
        generation_id="generation-1",
        business_date=date(2026, 7, 22),
        captured_at=CAPTURED_AT,
        targets=initial_targets,
        entries=[entry],
    )
    manifest["targets"] = {"ton": [selected_address]}

    with pytest.raises(MarketGenerationValidationError, match="target|manifest|API"):
        market_artifacts._validate_api_manifest_shape(manifest)


def test_api_raw_writer_rejects_sensitive_payload_before_writing(tmp_path) -> None:
    target = make_target("bsc", TOKEN)

    with pytest.raises(ValueError, match="sensitive"):
        market_artifacts.write_raw_response(
            tmp_path,
            target,
            "holders",
            _official_api_result(
                target,
                "holders",
                {"headers": {"authorization": "Bearer should-not-persist"}},
            ),
        )

    assert not (tmp_path / "raw").exists()


@pytest.mark.parametrize(
    "encoded_key",
    [
        "api%255Fkey",
        "note%ZZ",
        "api%2525252525252525255Fkey",
    ],
)
def test_api_raw_writer_rejects_encoded_invalid_or_overdeep_payload_keys(
    tmp_path,
    encoded_key,
) -> None:
    target = make_target("bsc", TOKEN)
    url = (
        "https://api.bubblemaps.io/addresses/token-top-holders"
        "?count=300&nocache=false"
    )

    with pytest.raises(ValueError, match="sensitive|percent|encoding|escape"):
        market_artifacts.write_raw_response(
            tmp_path,
            target,
            "holders",
            _api_result(
                method="POST",
                url=url,
                payload=[{encoded_key: "opaquevalue"}],
            ),
        )

    assert not (tmp_path / "raw").exists()


def test_api_generation_validator_rejects_encoded_payload_key_after_rehash(
    tmp_path,
) -> None:
    manifest = _seed_semantic_api_staging(tmp_path)
    holder_file = next(
        path
        for path in manifest["tokens"][0]["raw_files"]
        if path.endswith("holders.json")
    )
    document = json.loads((tmp_path / holder_file).read_text())
    document["payload"][0]["api%255Fkey"] = "opaquevalue"
    (tmp_path / holder_file).write_text(json.dumps(document), encoding="utf-8")
    _refresh_artifact_hash(tmp_path, manifest, holder_file)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError, match="API|sensitive"):
        validate_staging_generation(tmp_path)


@pytest.mark.parametrize(
    "encoded_value",
    [
        "Bearer%20should-not-persist",
        "bmAPI%2Dshould-not-persist",
        "access%5Ftoken%3Dshould-not-persist",
        "Bearer%20should-not-persist%ZZ",
    ],
)
def test_api_raw_writer_rejects_encoded_sensitive_string_values(
    tmp_path,
    encoded_value,
) -> None:
    target = make_target("bsc", TOKEN)

    with pytest.raises(ValueError, match="sensitive"):
        market_artifacts.write_raw_response(
            tmp_path,
            target,
            "holders",
            _official_api_result(
                target,
                "holders",
                [{"note": encoded_value}],
            ),
        )

    assert not (tmp_path / "raw").exists()


def test_api_raw_writer_rejects_overdeep_encoded_string_value(tmp_path) -> None:
    target = make_target("bsc", TOKEN)

    with pytest.raises(ValueError, match="deeply nested"):
        market_artifacts.write_raw_response(
            tmp_path,
            target,
            "holders",
            _official_api_result(
                target,
                "holders",
                [{"note": "opaque%252525252525252525"}],
            ),
        )

    assert not (tmp_path / "raw").exists()


def test_api_raw_writer_preserves_ordinary_malformed_percent_value(tmp_path) -> None:
    target = make_target("bsc", TOKEN)

    relative = market_artifacts.write_raw_response(
        tmp_path,
        target,
        "holders",
        _official_api_result(
            target,
            "holders",
            [{"note": "opaque%ZZ"}],
        ),
    )

    assert json.loads((tmp_path / relative).read_text())["payload"] == [
        {"note": "opaque%ZZ"}
    ]


def test_api_generation_allows_and_preserves_holder_label_ending_in_percent(
    tmp_path,
) -> None:
    label = "Liquidity pool 12%"
    manifest = _seed_semantic_api_staging(
        tmp_path,
        first_holder_label=label,
    )
    holder_file = next(
        path
        for path in manifest["tokens"][0]["raw_files"]
        if path.endswith("holders.json")
    )

    assert json.loads((tmp_path / holder_file).read_text())["payload"][0][
        "address_details"
    ]["label"] == label
    assert validate_staging_generation(tmp_path) == manifest


@pytest.mark.parametrize(
    "label",
    [
        "Official holder \ud800 label",
        "Official holder \ud800 50%",
    ],
)
def test_api_generation_preserves_lone_surrogate_holder_label(
    tmp_path,
    label,
) -> None:
    manifest = _seed_semantic_api_staging(
        tmp_path,
        first_holder_label=label,
    )
    holder_file = next(
        path
        for path in manifest["tokens"][0]["raw_files"]
        if path.endswith("holders.json")
    )
    raw_bytes = (tmp_path / holder_file).read_bytes()

    assert b"\\ud800" in raw_bytes
    assert json.loads(raw_bytes)["payload"][0]["address_details"]["label"] == label
    assert validate_staging_generation(tmp_path) == manifest


def test_api_raw_writer_rejects_encoded_secret_after_lone_surrogate(
    tmp_path,
) -> None:
    target = make_target("bsc", TOKEN)

    with pytest.raises(ValueError, match="sensitive"):
        market_artifacts.write_raw_response(
            tmp_path,
            target,
            "holders",
            _official_api_result(
                target,
                "holders",
                [{"note": "\ud800Bearer%20should-not-persist"}],
            ),
        )

    assert not (tmp_path / "raw").exists()


def test_api_generation_validator_rejects_encoded_string_value_after_rehash(
    tmp_path,
) -> None:
    manifest = _seed_semantic_api_staging(tmp_path)
    holder_file = next(
        path
        for path in manifest["tokens"][0]["raw_files"]
        if path.endswith("holders.json")
    )
    document = json.loads((tmp_path / holder_file).read_text())
    document["payload"][0]["note"] = "Bearer%20should-not-persist"
    (tmp_path / holder_file).write_text(json.dumps(document), encoding="utf-8")
    _refresh_artifact_hash(tmp_path, manifest, holder_file)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError, match="API|sensitive"):
        validate_staging_generation(tmp_path)


def test_api_raw_writer_rejects_payload_without_request_provenance(tmp_path) -> None:
    target = make_target("bsc", TOKEN)

    with pytest.raises(ValueError, match="ApiResult|metadata|provenance"):
        market_artifacts.write_raw_response(tmp_path, target, "holders", [])

    assert not (tmp_path / "raw").exists()


def test_api_generation_validator_rejects_null_raw_request_after_rehash(
    tmp_path,
) -> None:
    manifest = _seed_semantic_api_staging(tmp_path)
    holder_file = next(
        path
        for path in manifest["tokens"][0]["raw_files"]
        if path.endswith("holders.json")
    )
    document = json.loads((tmp_path / holder_file).read_text())
    document["request"] = None
    (tmp_path / holder_file).write_text(json.dumps(document), encoding="utf-8")
    _refresh_artifact_hash(tmp_path, manifest, holder_file)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError, match="API|request|metadata"):
        validate_staging_generation(tmp_path)


def test_api_raw_writer_accepts_official_bound_request_metadata(tmp_path) -> None:
    target = make_target("bsc", TOKEN)
    url = (
        "https://api.bubblemaps.io/addresses/token-top-holders"
        "?count=300&nocache=false"
    )

    relative = market_artifacts.write_raw_response(
        tmp_path,
        target,
        "holders",
        _api_result(method="POST", url=url),
    )

    assert json.loads((tmp_path / relative).read_text())["request"]["url"] == url


@pytest.mark.parametrize(
    ("kind", "method", "url"),
    [
        (
            "holders",
            "POST",
            "https://evil.example/addresses/token-top-holders"
            "?count=300&nocache=false",
        ),
        (
            "holders",
            "POST",
            "https://api.bubblemaps.io/relationships/subgraph"
            "?count=300&nocache=false",
        ),
        (
            "holders",
            "POST",
            "https://api.bubblemaps.io/addresses/token-top-holders"
            "?count=250&nocache=false",
        ),
        (
            "holders",
            "GET",
            "https://api.bubblemaps.io/addresses/token-top-holders"
            "?count=300&nocache=false",
        ),
        (
            "subgraph",
            "POST",
            "https://api.bubblemaps.io/relationships/subgraph"
            f"?whitelist_token_address={EXTERNAL}&whitelist_token_chain=bsc"
            "&queue_whitelisted_token_map=false",
        ),
    ],
)
def test_api_raw_writer_rejects_request_metadata_outside_endpoint_contract(
    tmp_path,
    kind,
    method,
    url,
) -> None:
    target = make_target("bsc", TOKEN)

    with pytest.raises(ValueError, match="URL|request|endpoint|target"):
        market_artifacts.write_raw_response(
            tmp_path,
            target,
            kind,
            _api_result(method=method, url=url),
        )

    assert not (tmp_path / "raw").exists()


@pytest.mark.parametrize(
    "query_suffix",
    [
        "&api%255Fkey=opaquevalue",
        "&x%252Dvalidation=opaquevalue",
        "&note=%ZZ",
    ],
)
def test_api_raw_writer_rejects_encoded_or_invalid_query_secrets(
    tmp_path,
    query_suffix,
) -> None:
    target = make_target("bsc", TOKEN)
    url = (
        "https://api.bubblemaps.io/addresses/token-top-holders"
        "?count=300&nocache=false"
        + query_suffix
    )

    with pytest.raises(ValueError, match="URL|request|query|sensitive"):
        market_artifacts.write_raw_response(
            tmp_path,
            target,
            "holders",
            _api_result(method="POST", url=url),
        )

    assert not (tmp_path / "raw").exists()


def test_api_raw_validator_rejects_repeatedly_encoded_sensitive_query() -> None:
    target = make_target("bsc", TOKEN)
    document = {
        "schema_version": "v3",
        "kind": "holders",
        "chain": "bsc",
        "token_address": TOKEN,
        "canonical_chain": "bsc",
        "canonical_token_address": TOKEN,
        "request": {
            "method": "POST",
            "url": (
                "https://api.bubblemaps.io/addresses/token-top-holders"
                "?count=300&nocache=false&api%255Fkey=opaquevalue"
            ),
            "status": 200,
            "attempts": 1,
        },
        "payload": [],
    }

    with pytest.raises(ValueError, match="URL|request|query|sensitive"):
        market_artifacts._validate_api_raw_document(
            document,
            target=target,
            expected_kind="holders",
        )


def test_write_final_token_promotes_task4_v2_document_to_v3_clean_reference(
    tmp_path,
) -> None:
    target = make_target("bsc", TOKEN)
    external_transfer = {
        "from_address": MEMBER,
        "to_address": EXTERNAL,
        "rel_type": "TRANSFER",
        "data": {
            "value": "1",
            "date": 1,
            "tx_hash": "0xexternal",
            "token_ref": {"chain": "bsc", "address": TOKEN},
        },
    }
    clean_transfer = market_artifacts.write_clean_member_transfers(
        tmp_path,
        target,
        MEMBER,
        [external_transfer],
        cluster_rank=1,
    )
    v2_document = {
        "schema_version": "v2",
        "chain": "bsc",
        "token_address": TOKEN,
        "canonical_chain": "bsc",
        "canonical_token_address": TOKEN,
        "captured_at": CAPTURED_AT,
        "clusters": [
            {
                "cluster_rank": 1,
                "amount": "100",
                "share": "0.5",
                "share_percent": "50.0",
                "member_count": 2,
                "members": [
                    {
                        "member_rank": 1,
                        "source_rank": 1,
                        "address": MEMBER,
                        "amount": "60",
                        "share": "0.3",
                        "share_percent": "30.0",
                        "is_supernode": False,
                        "metadata": {},
                        "transfer_details_available": True,
                        "transfer_count": 1,
                        "transfer_file": (
                            f"transfers/{safe_path_component(MEMBER)}.json"
                        ),
                    },
                    {
                        "member_rank": 2,
                        "source_rank": 2,
                        "address": SUPERNODE,
                        "amount": "40",
                        "share": "0.2",
                        "share_percent": "20.0",
                        "is_supernode": True,
                        "metadata": {},
                        "transfer_details_available": False,
                        "transfer_details_reason": "supernode_not_supported",
                        "transfer_count": 0,
                        "transfer_file": None,
                    },
                ],
            }
        ],
    }

    token_file = market_artifacts.write_final_token(tmp_path, target, v2_document)

    written = json.loads((tmp_path / token_file).read_text())
    member = written["clusters"][0]["members"][0]
    assert written["schema_version"] == "v3"
    assert member["transfer_file"] == clean_transfer
    assert json.loads((tmp_path / clean_transfer).read_text())["transfers"] == [
        external_transfer
    ]


def test_publish_success_activates_a_valid_v3_api_generation(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    expected = _seed_empty_api_staging(paths.staging)

    publish_success(paths)

    assert not paths.staging.exists()
    assert validate_staging_generation(paths.live) == expected


def test_publish_success_leaves_invalid_v3_staging_unpublished(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    manifest = _seed_empty_api_staging(paths.staging)
    manifest["targets_file"] = "outside.json"
    _rewrite_manifest(paths.staging, manifest)

    with pytest.raises(MarketGenerationValidationError, match="manifest|API|targets"):
        publish_success(paths)

    assert paths.staging.is_dir()
    assert not paths.live.exists()


def test_preserve_failed_v3_api_staging_keeps_partial_tree_out_of_live(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    market_artifacts.write_targets(paths.staging, {"bsc": [TOKEN]})

    preserve_failed_run(paths, ERROR_RECORD)

    assert (paths.failed / "targets.json").is_file()
    assert (paths.failed / "error.json").is_file()
    assert not paths.live.exists()


def test_validate_staging_generation_returns_exact_success_manifest(tmp_path) -> None:
    manifest = _seed_success_staging(tmp_path)
    assert validate_staging_generation(tmp_path) == manifest


def test_validate_staging_generation_rejects_hash_tamper(tmp_path) -> None:
    manifest = _seed_success_staging(tmp_path)
    artifact = next(iter(manifest["artifacts"]))
    with (tmp_path / artifact).open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


def test_validate_staging_generation_rejects_unlisted_file(tmp_path) -> None:
    _seed_success_staging(tmp_path)
    (tmp_path / "unlisted.json").write_text("{}")

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cluster_count", 999),
        ("ranked_holder_count", "3"),
        ("requested_chain", "eth"),
        ("token_file", "errors.json"),
    ],
)
def test_validation_rejects_semantically_tampered_manifest_entry(
    tmp_path,
    field,
    value,
) -> None:
    manifest = _seed_success_staging(tmp_path)
    manifest["tokens"][0][field] = value
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


def test_validation_rejects_token_summary_transfer_count_mismatch_after_rehash(
    tmp_path,
) -> None:
    manifest = _seed_success_staging(tmp_path)
    token_path = manifest["tokens"][0]["token_file"]
    token = json.loads((tmp_path / token_path).read_text())
    token["clusters"][0]["members"][0]["transfer_count"] = 8
    (tmp_path / token_path).write_text(json.dumps(token))
    _refresh_artifact_hash(tmp_path, manifest, token_path)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


def test_validation_rejects_nonpositive_source_rank_after_rehash(tmp_path) -> None:
    manifest = _seed_success_staging(tmp_path)
    token_path = manifest["tokens"][0]["token_file"]
    token = json.loads((tmp_path / token_path).read_text())
    token["clusters"][0]["members"][0]["source_rank"] = 0
    (tmp_path / token_path).write_text(json.dumps(token))
    _refresh_artifact_hash(tmp_path, manifest, token_path)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


@pytest.mark.parametrize(
    "location",
    ("token", "cluster", "member", "member_document"),
)
def test_validation_rejects_extra_formal_fields_after_rehash(
    tmp_path,
    location,
) -> None:
    manifest = _seed_success_staging(tmp_path)
    token_path = manifest["tokens"][0]["token_file"]
    member_path = manifest["tokens"][0]["member_files"][0]
    token = json.loads((tmp_path / token_path).read_text())
    member_document = json.loads((tmp_path / member_path).read_text())
    containers = {
        "token": token,
        "cluster": token["clusters"][0],
        "member": token["clusters"][0]["members"][0],
        "member_document": member_document,
    }
    containers[location]["password"] = "must-not-be-accepted"
    changed_path = member_path if location == "member_document" else token_path
    changed_document = member_document if location == "member_document" else token
    (tmp_path / changed_path).write_text(
        json.dumps(changed_document),
        encoding="utf-8",
    )
    _refresh_artifact_hash(tmp_path, manifest, changed_path)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


def test_validation_rejects_task2_member_order_after_rehash(tmp_path) -> None:
    manifest = _seed_success_staging(tmp_path)
    token_path = manifest["tokens"][0]["token_file"]
    token = json.loads((tmp_path / token_path).read_text())
    members = token["clusters"][0]["members"]
    members.reverse()
    for rank, member in enumerate(members, start=1):
        member["member_rank"] = rank
    (tmp_path / token_path).write_text(json.dumps(token), encoding="utf-8")
    _refresh_artifact_hash(tmp_path, manifest, token_path)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


def test_validation_matches_formal_source_rank_to_selected_holder_after_rehash(
    tmp_path,
) -> None:
    manifest = _seed_success_staging(tmp_path)
    token_path = manifest["tokens"][0]["token_file"]
    token = json.loads((tmp_path / token_path).read_text())
    token["clusters"][0]["members"][0]["source_rank"] = 3
    (tmp_path / token_path).write_text(json.dumps(token), encoding="utf-8")
    _refresh_artifact_hash(tmp_path, manifest, token_path)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


def test_validation_requires_each_formal_transfer_in_raw_union_after_rehash(
    tmp_path,
) -> None:
    manifest = _seed_success_staging(tmp_path)
    raw_member = next(
        relative
        for relative in manifest["tokens"][0]["raw_files"]
        if relative.endswith(f"transfers/{safe_path_component(MEMBER)}.json")
    )
    envelope = json.loads((tmp_path / raw_member).read_text())
    envelope["payload"] = []
    (tmp_path / raw_member).write_text(json.dumps(envelope), encoding="utf-8")
    _refresh_artifact_hash(tmp_path, manifest, raw_member)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


def test_validation_rejects_noncanonical_batch_capture_timestamp(tmp_path) -> None:
    manifest = _seed_success_staging(tmp_path)
    manifest["captured_at"] = "2026-07-22 12:30:00+00:00"
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


def test_validation_derives_unique_transfer_count_from_formal_views(tmp_path) -> None:
    manifest = _seed_success_staging(tmp_path)
    manifest["tokens"][0]["unique_transfer_count"] = 9
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


def test_validate_staging_generation_rejects_symlink_even_with_matching_name(
    tmp_path,
) -> None:
    manifest = _seed_success_staging(tmp_path)
    artifact = next(
        relative
        for relative in manifest["artifacts"]
        if relative.startswith("raw/")
    )
    target = tmp_path / artifact
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


def test_validation_rejects_missing_holder_role_after_exact_rehash(tmp_path) -> None:
    manifest = _seed_success_staging(tmp_path)
    token = manifest["tokens"][0]
    holder_file = next(path for path in token["raw_files"] if path.endswith("holders.json"))
    (tmp_path / holder_file).unlink()
    token["raw_files"].remove(holder_file)
    del manifest["artifacts"][holder_file]
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


@pytest.mark.parametrize("replacement", [{}, {"schema_version": "v2"}])
def test_validation_rejects_malformed_holder_envelope_after_rehash(
    tmp_path,
    replacement,
) -> None:
    manifest = _seed_success_staging(tmp_path)
    holder_file = next(
        path
        for path in manifest["tokens"][0]["raw_files"]
        if path.endswith("holders.json")
    )
    (tmp_path / holder_file).write_text(json.dumps(replacement))
    _refresh_artifact_hash(tmp_path, manifest, holder_file)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(MarketGenerationValidationError):
        validate_staging_generation(tmp_path)


def test_generation_lock_is_nonblocking_and_owner_only(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    competing = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-2")

    with generation_lock(paths, shared=False):
        assert paths.lock_file.is_file()
        assert stat.S_IMODE(paths.lock_file.stat().st_mode) == 0o600
        with pytest.raises(MarketGenerationLockError):
            with generation_lock(competing, shared=False):
                pass

    with generation_lock(paths, shared=True):
        pass


def test_generation_lock_declares_context_manager_return_contract() -> None:
    assert get_type_hints(generation_lock)["return"] == AbstractContextManager[None]


def test_generation_lock_rejects_symlink_lock_file(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    paths.lock_file.parent.mkdir(parents=True)
    outside = tmp_path / "outside.lock"
    outside.write_text("")
    paths.lock_file.symlink_to(outside)

    with pytest.raises(MarketGenerationLockError):
        with generation_lock(paths, shared=False):
            pass


def test_generation_lock_rejects_symlink_locks_ancestor(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    outside = tmp_path / "outside-locks"
    outside.mkdir()
    (tmp_path / "_locks").symlink_to(outside, target_is_directory=True)

    with pytest.raises(MarketGenerationLockError):
        with generation_lock(paths, shared=False):
            pass

    assert list(outside.iterdir()) == []


def test_generation_lock_reports_unlock_and_close_failures(
    tmp_path,
    monkeypatch,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    real_flock = market_artifacts.fcntl.flock
    real_close = market_artifacts._close_lock_descriptor
    close_calls = []

    def failing_unlock(descriptor, operation):
        if operation == market_artifacts.fcntl.LOCK_UN:
            raise OSError("forced unlock failure")
        return real_flock(descriptor, operation)

    def close_then_fail(descriptor):
        close_calls.append(descriptor)
        real_close(descriptor)
        raise OSError("forced close failure")

    monkeypatch.setattr(market_artifacts.fcntl, "flock", failing_unlock)
    monkeypatch.setattr(
        market_artifacts,
        "_close_lock_descriptor",
        close_then_fail,
    )

    with pytest.raises(MarketGenerationLockError) as captured:
        with generation_lock(paths, shared=False):
            pass

    notes = "\n".join(getattr(captured.value, "__notes__", ()))
    assert "unlock" in notes
    assert "close" in notes
    assert len(close_calls) == 1


def test_generation_lock_attaches_cleanup_failure_to_body_error(
    tmp_path,
    monkeypatch,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    real_flock = market_artifacts.fcntl.flock

    def failing_unlock(descriptor, operation):
        if operation == market_artifacts.fcntl.LOCK_UN:
            raise OSError("forced unlock failure")
        return real_flock(descriptor, operation)

    monkeypatch.setattr(market_artifacts.fcntl, "flock", failing_unlock)

    with pytest.raises(RuntimeError, match="body failure") as captured:
        with generation_lock(paths, shared=False):
            raise RuntimeError("body failure")

    notes = "\n".join(getattr(captured.value, "__notes__", ()))
    assert "unlock" in notes


def test_read_validated_generation_returns_manifest_and_empty_errors(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    manifest = _seed_success_staging(paths.live)

    validated, errors = read_validated_generation(
        tmp_path,
        date(2026, 7, 22),
    )

    assert validated == manifest
    assert errors == []


def test_read_validated_generation_returns_v3_manifest_without_errors_file(
    tmp_path,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    manifest = _seed_empty_api_staging(paths.live)

    validated, errors = read_validated_generation(
        tmp_path,
        date(2026, 7, 22),
    )

    assert not (paths.live / "errors.json").exists()
    assert validated == manifest
    assert errors == []


def test_reader_uses_same_nonblocking_external_lock(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    _seed_success_staging(paths.live)

    with generation_lock(paths, shared=False):
        with pytest.raises(MarketGenerationLockError):
            read_validated_generation(tmp_path, date(2026, 7, 22))


def test_reader_rejects_failed_generation_tree(tmp_path) -> None:
    paths = RunPaths.create(
        tmp_path,
        date(2026, 7, 22),
        "generation-1",
    )
    _seed_success_staging(paths.staging)
    preserve_failed_run(paths, ERROR_RECORD)
    failed_root = tmp_path / "_failed"
    failed_generation = paths.failed

    assert (failed_generation / "error.json").is_file()
    assert not (failed_generation / "manifest.json").exists()

    with pytest.raises(
        MarketGenerationValidationError,
        match="API generation commit files are missing",
    ):
        read_validated_generation(failed_root, date(2026, 7, 22))


def test_reader_rejects_live_generation_with_tampered_shard(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    manifest = _seed_success_staging(paths.live)
    token_file = manifest["tokens"][0]["token_file"]
    with (paths.live / token_file).open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(MarketGenerationValidationError):
        read_validated_generation(tmp_path, date(2026, 7, 22))


def test_preserve_failed_run_has_error_diagnostic_data_and_no_commit_files(
    tmp_path,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    _seed_success_staging(paths.staging)

    preserve_failed_run(paths, ERROR_RECORD)

    assert not paths.live.exists()
    assert not paths.staging.exists()
    assert not (paths.failed / "manifest.json").exists()
    assert not (paths.failed / "errors.json").exists()
    assert json.loads((paths.failed / "error.json").read_text()) == ERROR_RECORD
    assert (paths.failed / "diagnostic" / "data").is_dir()
    assert (paths.failed / "raw").is_dir()
    assert not (paths.failed / "data").exists()


@pytest.mark.parametrize(
    "member_substage",
    ["prepare", "open", "trigger", "response", "close"],
)
def test_preserve_failed_run_keeps_valid_member_substage(
    tmp_path,
    member_substage,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    paths.staging.mkdir(parents=True)

    preserve_failed_run(
        paths,
        {**ERROR_RECORD, "member_substage": member_substage},
    )

    persisted = json.loads((paths.failed / "error.json").read_text())
    assert persisted == {**ERROR_RECORD, "member_substage": member_substage}


@pytest.mark.parametrize(
    "member_substage",
    ["unknown", 1, True],
    ids=["invalid-string", "non-string", "bool"],
)
def test_preserve_failed_run_drops_invalid_member_substage_and_secrets(
    tmp_path,
    member_substage,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    paths.staging.mkdir(parents=True)
    secret = "MEMBER_SUBSTAGE_PRIVATE_SENTINEL"

    preserve_failed_run(
        paths,
        {
            **ERROR_RECORD,
            "member_substage": member_substage,
            "headers": {"authorization": secret},
        },
    )

    raw = (paths.failed / "error.json").read_text()
    persisted = json.loads(raw)
    assert "member_substage" not in persisted
    assert persisted["message"] == "capture failed"
    assert secret not in raw


def test_preserve_failed_run_omits_absent_member_substage(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    paths.staging.mkdir(parents=True)

    preserve_failed_run(paths, ERROR_RECORD)

    persisted = json.loads((paths.failed / "error.json").read_text())
    assert "member_substage" not in persisted


@pytest.mark.parametrize("reserved", ["_staging", "_failed"])
def test_preserve_failed_run_rejects_symlink_reserved_ancestor(
    tmp_path,
    reserved,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    outside = tmp_path / f"outside-{reserved}"
    outside.mkdir()
    (tmp_path / reserved).symlink_to(outside, target_is_directory=True)
    if reserved == "_failed":
        paths.staging.mkdir(parents=True)

    with pytest.raises((ValueError, OSError, PublicationRecoveryError)):
        preserve_failed_run(paths, ERROR_RECORD)

    assert not any(path.name == "error.json" for path in outside.rglob("*"))


def test_preserve_failed_run_rejects_diagnostic_symlink_without_external_write(
    tmp_path,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    (paths.staging / "data").mkdir(parents=True)
    (paths.staging / "data" / "token.json").write_text("{}")
    outside = tmp_path / "outside-diagnostic"
    outside.mkdir()
    (paths.staging / "diagnostic").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises((ValueError, OSError)):
        preserve_failed_run(paths, ERROR_RECORD)

    assert list(outside.iterdir()) == []
    assert (paths.staging / "data" / "token.json").is_file()


def test_failed_rerun_leaves_previous_live_generation_byte_identical(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    _seed_success_staging(paths.live)
    before = _tree_hashes(paths.live)
    _seed_success_staging(paths.staging)

    preserve_failed_run(paths, ERROR_RECORD)

    assert _tree_hashes(paths.live) == before


def test_preserve_failed_run_uses_unique_directory_without_overwrite(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    paths.failed.mkdir(parents=True)
    marker = paths.failed / "existing.txt"
    marker.write_text("keep")
    _seed_success_staging(paths.staging)

    preserve_failed_run(paths, ERROR_RECORD)

    assert marker.read_text() == "keep"
    preserved = paths.failed.parent / "generation-1-1"
    assert json.loads((preserved / "error.json").read_text()) == ERROR_RECORD


def test_preserve_failed_run_redacts_sensitive_error_values(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    paths.staging.mkdir(parents=True)
    secret_error = {
        "stage": "capture",
        "authorization": "Bearer TOP_SECRET_VALUE",
        "message": (
            "api_key=bmAPI-TOP_SECRET_TOKEN "
            "postgresql://dbuser:dbpass@example.test/db "
            "authorization: Bearer MESSAGE_SECRET_VALUE"
        ),
    }

    preserve_failed_run(paths, secret_error)

    payload = (paths.failed / "error.json").read_text()
    assert "TOP_SECRET" not in payload
    assert "dbuser" not in payload
    assert "dbpass" not in payload
    assert "MESSAGE_SECRET" not in payload
    assert "[REDACTED]" in payload


def test_preserve_failed_run_redacts_standalone_bearer_value(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    paths.staging.mkdir(parents=True)
    sentinel = "STANDALONE_BEARER_SENTINEL"
    error = {
        **ERROR_RECORD,
        "message": f"upstream rejected Bearer {sentinel}",
    }

    preserve_failed_run(paths, error)

    persisted = (paths.failed / "error.json").read_text()
    assert sentinel not in persisted
    assert "[REDACTED]" in persisted


@pytest.mark.parametrize(
    "message",
    [
        "upstream credential was NATURAL_LANGUAGE_SENTINEL",
        "upstream secret is NATURAL_LANGUAGE_SENTINEL",
        "API key was NATURAL_LANGUAGE_SENTINEL",
        "unexpected upstream detail NATURAL_LANGUAGE_SENTINEL",
    ],
)
def test_preserve_failed_run_redacts_natural_language_credentials(
    tmp_path,
    message,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    paths.staging.mkdir(parents=True)

    preserve_failed_run(paths, {**ERROR_RECORD, "message": message})

    persisted = (paths.failed / "error.json").read_text()
    assert "NATURAL_LANGUAGE_SENTINEL" not in persisted
    assert "[REDACTED]" in persisted


def test_preserve_failed_run_drops_unknown_sensitive_containers_from_whole_tree(
    tmp_path,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "generation-1")
    paths.staging.mkdir(parents=True)
    sentinel = "UNIQUE_SENTINEL_MUST_NOT_PERSIST"
    error = {
        "chain": "bsc",
        "token_address": TOKEN,
        "stage": "capture",
        "type": "RuntimeError",
        "message": "capture failed",
        "captured_at": CAPTURED_AT,
        "headers": {"Authorization": sentinel},
        "cookies": [{"secret": sentinel}],
        "credential_map": {"password": sentinel},
        "unknown_nested": {"value": sentinel},
        "api_token": sentinel,
    }

    preserve_failed_run(paths, error)

    for path in paths.failed.rglob("*"):
        if path.is_file():
            assert sentinel.encode() not in path.read_bytes()
    persisted = json.loads((paths.failed / "error.json").read_text())
    assert set(persisted) == {
        "chain",
        "token_address",
        "stage",
        "type",
        "message",
        "captured_at",
    }


def test_recovery_restores_sole_valid_backup_from_any_generation_id(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "current-generation")
    old_backup = paths.backup.parent / "older-generation"
    expected = _seed_success_staging(old_backup)

    recover_interrupted_publish(paths)

    assert not old_backup.exists()
    assert validate_staging_generation(paths.live) == expected


def test_recovery_removes_sole_backup_when_valid_live_is_present(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "current-generation")
    _seed_success_staging(paths.live)
    backup = paths.backup.parent / "older-generation"
    _seed_success_staging(backup)

    recover_interrupted_publish(paths)

    assert paths.live.is_dir()
    assert not backup.exists()


def test_recovery_cleanup_failure_does_not_leave_scanned_backup(
    tmp_path,
    monkeypatch,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "current-generation")
    _seed_success_staging(paths.live)
    backup = paths.backup.parent / "older-generation"
    _seed_success_staging(backup, generation_id="older-generation")

    def failing_cleanup(*_args, **_kwargs):
        raise OSError("forced cleanup failure")

    monkeypatch.setattr(
        market_artifacts,
        "_safe_rmtree_relative",
        failing_cleanup,
        raising=False,
    )

    recover_interrupted_publish(paths)

    assert not backup.exists()
    trash_entries = list((tmp_path / "_trash" / "2026-07-22").iterdir())
    assert len(trash_entries) == 1
    recover_interrupted_publish(paths)
    assert validate_staging_generation(paths.live)["status"] == "success"


def test_recovery_opportunistically_removes_multiple_stale_trash_trees(
    tmp_path,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "current-generation")
    trash_day = tmp_path / "_trash" / "2026-07-22"
    for name in ("stale-one", "stale-two"):
        directory = trash_day / name
        directory.mkdir(parents=True)
        (directory / "leftover.json").write_text("{}")

    recover_interrupted_publish(paths)

    assert list(trash_day.iterdir()) == []


def test_trash_cleanup_failure_does_not_block_main_recovery(
    tmp_path,
    monkeypatch,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "current-generation")
    backup = paths.backup.parent / "older-generation"
    _seed_success_staging(backup, generation_id="older-generation")
    trash_day = tmp_path / "_trash" / "2026-07-22"
    failing = trash_day / "stale-failing"
    removable = trash_day / "stale-removable"
    for directory in (failing, removable):
        directory.mkdir(parents=True)
        (directory / "leftover.json").write_text("{}")
    real_rmtree = market_artifacts._safe_rmtree_relative

    def fail_one_trash(root_descriptor, relative, **kwargs):
        if Path(relative).name == "stale-failing":
            raise OSError("forced stale trash cleanup failure")
        return real_rmtree(root_descriptor, relative, **kwargs)

    monkeypatch.setattr(
        market_artifacts,
        "_safe_rmtree_relative",
        fail_one_trash,
    )

    recover_interrupted_publish(paths)

    assert validate_staging_generation(paths.live)["generation_id"] == "older-generation"
    assert failing.is_dir()
    assert not removable.exists()


def test_opportunistic_trash_cleanup_never_follows_symlink(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "current-generation")
    trash_day = tmp_path / "_trash" / "2026-07-22"
    trash_day.mkdir(parents=True)
    outside = tmp_path / "outside-trash-target"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep")
    (trash_day / "linked").symlink_to(outside, target_is_directory=True)

    recover_interrupted_publish(paths)

    assert marker.read_text() == "keep"


def test_recovery_rejects_multiple_backups_without_deleting_any(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "current-generation")
    first = paths.backup.parent / "first"
    second = paths.backup.parent / "second"
    _seed_success_staging(first)
    _seed_success_staging(second)

    with pytest.raises(PublicationRecoveryError):
        recover_interrupted_publish(paths)

    assert first.exists()
    assert second.exists()
    assert not paths.live.exists()


def test_recovery_rejects_invalid_backup_without_deleting_it(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "current-generation")
    invalid = paths.backup.parent / "older-generation"
    invalid.mkdir(parents=True)
    (invalid / "not-a-generation.txt").write_text("keep")

    with pytest.raises(PublicationRecoveryError):
        recover_interrupted_publish(paths)

    assert (invalid / "not-a-generation.txt").read_text() == "keep"
    assert not paths.live.exists()


def test_recovery_rejects_invalid_live_and_preserves_valid_backup(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "current-generation")
    paths.live.mkdir(parents=True)
    (paths.live / "invalid.txt").write_text("invalid")
    backup = paths.backup.parent / "older-generation"
    _seed_success_staging(backup)

    with pytest.raises(PublicationRecoveryError):
        recover_interrupted_publish(paths)

    assert (paths.live / "invalid.txt").read_text() == "invalid"
    assert backup.exists()


def test_publish_success_activates_valid_staging_and_removes_old_backup(
    tmp_path,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "new-generation")
    _seed_success_staging(paths.live, generation_id="old-generation")
    _seed_success_staging(paths.staging, generation_id="new-generation")

    publish_success(paths)

    assert not paths.staging.exists()
    assert not paths.backup.exists()
    assert validate_staging_generation(paths.live)["generation_id"] == "new-generation"


def test_publish_rolls_back_old_live_when_staging_activation_fails(
    tmp_path,
    monkeypatch,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "new-generation")
    _seed_success_staging(paths.live, generation_id="old-generation")
    _seed_success_staging(paths.staging, generation_id="new-generation")
    before = _tree_hashes(paths.live)
    real_rename = market_artifacts._rename_relative
    staging_relative = Path("_staging/2026-07-22/new-generation")
    live_relative = Path("2026-07-22")

    def failing_activation(root_descriptor, source, destination, **kwargs):
        if source == staging_relative and destination == live_relative:
            raise OSError("forced activation failure")
        return real_rename(root_descriptor, source, destination, **kwargs)

    monkeypatch.setattr(market_artifacts, "_rename_relative", failing_activation)

    with pytest.raises(OSError, match="activation failure"):
        publish_success(paths)

    assert _tree_hashes(paths.live) == before
    assert paths.staging.exists()
    assert not paths.backup.exists()


def test_publish_rolls_back_old_live_when_commit_fsync_fails(
    tmp_path,
    monkeypatch,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "new-generation")
    _seed_success_staging(paths.live, generation_id="old-generation")
    _seed_success_staging(paths.staging, generation_id="new-generation")
    before = _tree_hashes(paths.live)

    def failing_fsync(_directory):
        raise OSError("forced root fsync failure")

    monkeypatch.setattr(market_artifacts, "_fsync_output_root", failing_fsync)

    with pytest.raises(OSError, match="root fsync failure"):
        publish_success(paths)

    assert _tree_hashes(paths.live) == before
    assert validate_staging_generation(paths.staging)["generation_id"] == "new-generation"
    assert not paths.backup.exists()


def test_publish_cleanup_failure_moves_backup_out_of_recovery_scan_before_delete(
    tmp_path,
    monkeypatch,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "new-generation")
    _seed_success_staging(paths.live, generation_id="old-generation")
    _seed_success_staging(paths.staging, generation_id="new-generation")

    def failing_cleanup(*_args, **_kwargs):
        raise OSError("forced cleanup failure")

    monkeypatch.setattr(
        market_artifacts,
        "_safe_rmtree_relative",
        failing_cleanup,
        raising=False,
    )

    publish_success(paths)

    assert validate_staging_generation(paths.live)["generation_id"] == "new-generation"
    assert not paths.backup.exists()
    trash_entries = list((tmp_path / "_trash" / "2026-07-22").iterdir())
    assert len(trash_entries) == 1
    assert validate_staging_generation(trash_entries[0])["generation_id"] == "old-generation"

    recover_interrupted_publish(paths)
    assert validate_staging_generation(paths.live)["generation_id"] == "new-generation"


def test_publish_rejects_symlink_backups_ancestor_without_external_rename(
    tmp_path,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "new-generation")
    _seed_success_staging(paths.live, generation_id="old-generation")
    _seed_success_staging(paths.staging, generation_id="new-generation")
    before = _tree_hashes(paths.live)
    outside = tmp_path / "outside-backups"
    outside.mkdir()
    (tmp_path / "_backups").symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, PublicationRecoveryError)):
        publish_success(paths)

    assert _tree_hashes(paths.live) == before
    assert list(outside.iterdir()) == []


def test_publish_rejects_symlink_trash_ancestor_without_external_write(
    tmp_path,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "new-generation")
    _seed_success_staging(paths.live, generation_id="old-generation")
    _seed_success_staging(paths.staging, generation_id="new-generation")
    before = _tree_hashes(paths.live)
    outside = tmp_path / "outside-trash"
    outside.mkdir()
    (tmp_path / "_trash").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PublicationRecoveryError):
        publish_success(paths)

    assert _tree_hashes(paths.live) == before
    assert list(outside.iterdir()) == []


def test_publish_rejects_staging_entry_swapped_after_validation(
    tmp_path,
    monkeypatch,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "new-generation")
    _seed_success_staging(paths.live, generation_id="old-generation")
    _seed_success_staging(paths.staging, generation_id="new-generation")
    before = _tree_hashes(paths.live)
    displaced = tmp_path / "displaced-staging"
    outside = tmp_path / "outside-staging-swap"
    outside.mkdir()
    real_rename = market_artifacts._rename_relative
    staging_relative = Path("_staging/2026-07-22/new-generation")
    swapped = False

    def swap_before_activation(root_descriptor, source, destination, **kwargs):
        nonlocal swapped
        if source == staging_relative and not swapped:
            swapped = True
            paths.staging.rename(displaced)
            paths.staging.symlink_to(outside, target_is_directory=True)
        return real_rename(root_descriptor, source, destination, **kwargs)

    monkeypatch.setattr(
        market_artifacts,
        "_rename_relative",
        swap_before_activation,
    )

    with pytest.raises(OSError):
        publish_success(paths)

    assert swapped is True
    assert _tree_hashes(paths.live) == before
    assert list(outside.iterdir()) == []


def test_recovery_rejects_symlink_backups_ancestor_without_external_rename(
    tmp_path,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "current-generation")
    outside = tmp_path / "outside-recovery"
    backup = outside / "2026-07-22" / "older-generation"
    _seed_success_staging(backup, generation_id="older-generation")
    (tmp_path / "_backups").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PublicationRecoveryError):
        recover_interrupted_publish(paths)

    assert backup.is_dir()
    assert not paths.live.exists()


def test_recovery_rejects_backup_entry_swapped_after_validation(
    tmp_path,
    monkeypatch,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "current-generation")
    backup = paths.backup.parent / "older-generation"
    _seed_success_staging(backup, generation_id="older-generation")
    displaced = tmp_path / "displaced-backup"
    outside = tmp_path / "outside-backup-swap"
    outside.mkdir()
    real_rename = market_artifacts._rename_relative
    backup_relative = Path("_backups/2026-07-22/older-generation")
    swapped = False

    def swap_before_recovery(root_descriptor, source, destination, **kwargs):
        nonlocal swapped
        if source == backup_relative and not swapped:
            swapped = True
            backup.rename(displaced)
            backup.symlink_to(outside, target_is_directory=True)
        return real_rename(root_descriptor, source, destination, **kwargs)

    monkeypatch.setattr(
        market_artifacts,
        "_rename_relative",
        swap_before_recovery,
    )

    with pytest.raises(PublicationRecoveryError):
        recover_interrupted_publish(paths)

    assert swapped is True
    assert not paths.live.exists()
    assert list(outside.iterdir()) == []


def test_recovery_rolls_back_when_renamed_backup_parent_fsync_fails(
    tmp_path,
    monkeypatch,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "current-generation")
    backup = paths.backup.parent / "older-generation"
    expected = _seed_success_staging(backup, generation_id="older-generation")
    real_fsync = market_artifacts.os.fsync
    calls = 0

    def fail_first_rename_parent_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("forced recovery rename parent fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(
        market_artifacts.os,
        "fsync",
        fail_first_rename_parent_fsync,
    )

    with pytest.raises(Exception):
        recover_interrupted_publish(paths)

    assert not paths.live.exists()
    assert validate_staging_generation(backup) == expected


@pytest.mark.parametrize("fail_at", [1, 3], ids=["old-live-rename", "activation-rename"])
def test_publish_rolls_back_when_renamed_parent_fsync_fails(
    tmp_path,
    monkeypatch,
    fail_at,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "new-generation")
    _seed_success_staging(paths.live, generation_id="old-generation")
    _seed_success_staging(paths.staging, generation_id="new-generation")
    before = _tree_hashes(paths.live)
    paths.backup.parent.mkdir(parents=True)
    (tmp_path / "_trash" / "2026-07-22").mkdir(parents=True)
    real_fsync = market_artifacts.os.fsync
    calls = 0

    def fail_selected_rename_parent_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError("forced publish rename parent fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(
        market_artifacts.os,
        "fsync",
        fail_selected_rename_parent_fsync,
    )

    with pytest.raises(Exception):
        publish_success(paths)

    assert _tree_hashes(paths.live) == before
    assert validate_staging_generation(paths.staging)["generation_id"] == "new-generation"
    assert not paths.backup.exists()


def test_publish_revalidates_staging_contents_immediately_before_activation(
    tmp_path,
    monkeypatch,
) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "new-generation")
    _seed_success_staging(paths.live, generation_id="old-generation")
    staging_manifest = _seed_success_staging(
        paths.staging,
        generation_id="new-generation",
    )
    before = _tree_hashes(paths.live)
    real_validate = market_artifacts.validate_staging_generation
    tampered = False

    def validate_then_tamper(directory):
        nonlocal tampered
        manifest = real_validate(directory)
        if Path(directory) == paths.staging and not tampered:
            tampered = True
            token_path = staging_manifest["tokens"][0]["token_file"]
            artifact = paths.staging / token_path
            artifact.write_bytes(artifact.read_bytes() + b" ")
        return manifest

    monkeypatch.setattr(
        market_artifacts,
        "validate_staging_generation",
        validate_then_tamper,
    )

    with pytest.raises(MarketGenerationValidationError):
        publish_success(paths)

    assert tampered is True
    assert _tree_hashes(paths.live) == before
    assert not paths.backup.exists()


def test_publish_rejects_invalid_staging_without_touching_live(tmp_path) -> None:
    paths = RunPaths.create(tmp_path, date(2026, 7, 22), "new-generation")
    _seed_success_staging(paths.live, generation_id="old-generation")
    before = _tree_hashes(paths.live)
    paths.staging.mkdir(parents=True)
    (paths.staging / "invalid.txt").write_text("invalid")

    with pytest.raises(MarketGenerationValidationError):
        publish_success(paths)

    assert _tree_hashes(paths.live) == before


def test_write_success_manifest_uses_secure_unbounded_writer(tmp_path) -> None:
    staging = tmp_path / "staging"
    value = 10**5000

    write_success_manifest(staging, {"large_native_integer": value})

    assert market_artifacts._load_json_document(
        staging / "manifest.json"
    ) == {"large_native_integer": value}


def test_write_success_manifest_rejects_symlink_staging_without_external_write(
    tmp_path,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    staging = tmp_path / "staging"
    staging.symlink_to(external, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        write_success_manifest(staging, {"status": "success"})

    assert not (external / "manifest.json").exists()


def test_write_success_manifest_rejects_parent_swap_without_external_write(
    tmp_path,
    monkeypatch,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    displaced = tmp_path / "displaced"
    external = tmp_path / "external"
    external.mkdir()
    original_destination = market_artifacts._destination

    def swap_after_check(root, relative):
        destination = original_destination(root, relative)
        staging.rename(displaced)
        staging.symlink_to(external, target_is_directory=True)
        return destination

    monkeypatch.setattr(market_artifacts, "_destination", swap_after_check)

    with pytest.raises((OSError, ValueError)):
        write_success_manifest(staging, {"status": "success"})

    assert not (external / "manifest.json").exists()
