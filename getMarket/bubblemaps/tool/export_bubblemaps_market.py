"""Generate Bubblemaps market artifacts from PostgreSQL targets and official APIs."""

from __future__ import annotations

import argparse
import asyncio
import copy
from datetime import date, datetime, timezone
import math
from pathlib import Path
import sys
import uuid
from zoneinfo import ZoneInfo


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if __package__ in (None, ""):
    sys.path.insert(0, str(_PROJECT_ROOT))

from getDB.bubblemaps.tool.db_source import load_pg_settings
from getMarket.bubblemaps.tool.bubblemaps_api import (
    ApiResult,
    BubblemapsApiClient,
    TopHoldersUnavailableError,
)
from getMarket.bubblemaps.tool import market_artifacts
from getMarket.bubblemaps.tool.market_artifacts import (
    MarketGenerationLockError,
    PublicationRecoveryError,
    RunPaths,
    build_api_manifest,
    generation_lock,
    preserve_failed_run,
    publish_success,
    read_targets,
    recover_interrupted_publish,
    validate_staging_generation,
    write_clean_member_transfers,
    write_clean_response,
    write_error_report,
    write_final_token,
    write_raw_response,
    write_success_manifest,
    write_targets,
)
from getMarket.bubblemaps.tool.market_identity import (
    TargetToken,
    canonicalize_address,
)
from getMarket.bubblemaps.tool.market_targets import (
    load_targets,
    select_targets,
    targets_to_dict,
)
from getMarket.bubblemaps.tool.market_transform import (
    RankedHolder,
    SnapshotModel,
    SubgraphEdge,
    filter_subgraph_edges,
    parse_ranked_holders,
    reconstruct_clusters,
    token_snapshot_fingerprint,
)
from getMarket.bubblemaps.tool.transfer_transform import (
    TransferResult,
    _filter_member_payload,
    build_transfer_result,
)


_CHINA_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_DEFAULT_OUTPUT_ROOT = _PROJECT_ROOT / "getMarket" / "bubblemaps" / "market"


class BatchFailure(RuntimeError):
    def __init__(self, error_record: dict) -> None:
        self.error_record = error_record
        super().__init__("capture failed")


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be a positive integer") from None
    if parsed < 1 or str(parsed) != value:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be positive") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be nonnegative") from None
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def _symbol_list(value: str) -> tuple[str, ...]:
    entries = value.split(",")
    if any(not entry.strip() for entry in entries):
        raise argparse.ArgumentTypeError(
            "symbols must be a comma-separated list without empty entries"
        )
    return tuple(sorted({entry.strip().upper() for entry in entries}))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=_DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument("--symbols", type=_symbol_list)
    parser.add_argument("--chain")
    parser.add_argument("--token-address")
    parser.add_argument("--api-timeout", type=_positive_float, default=20.0)
    parser.add_argument("--api-max-attempts", type=_positive_int, default=3)
    parser.add_argument("--api-retry-delay", type=_nonnegative_float, default=0.25)
    parser.add_argument("--api-min-interval", type=_nonnegative_float, default=2.1)
    arguments = parser.parse_args(argv)
    if (arguments.chain is None) != (arguments.token_address is None):
        parser.error("--chain and --token-address must be provided together")
    if arguments.symbols is not None and arguments.chain is not None:
        parser.error("--symbols cannot be combined with --chain and --token-address")
    return arguments


def _china_today() -> date:
    return datetime.now(_CHINA_TIME_ZONE).date()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _payload(value: object) -> object:
    return value.payload if isinstance(value, ApiResult) else value


def _holder_document(holder: RankedHolder) -> dict:
    return {
        "address": holder.address,
        "address_details": {
            "is_supernode": holder.is_supernode,
            **copy.deepcopy(holder.metadata),
        },
        "holder_data": {
            "rank": holder.source_rank,
            "amount": holder.amount,
            "share": holder.share,
        },
    }


def _holder_payload_for_strict_parser(payload: object) -> object:
    try:
        normalized = copy.deepcopy(payload)
    except Exception:
        raise ValueError("holder payload could not be copied safely") from None
    if type(normalized) is not list:
        return normalized
    for index, row in enumerate(normalized):
        if type(row) is not dict:
            continue
        holder_data = row.get("holder_data")
        if type(holder_data) is not dict:
            continue
        for field in ("amount", "share"):
            value = holder_data.get(field)
            if type(value) is not float:
                continue
            if not math.isfinite(value):
                raise ValueError(
                    f"holder row {index} {field} must be a finite decimal"
                )
            holder_data[field] = repr(value)
    return normalized


def clean_holders(payload: object, target: TargetToken) -> list[dict]:
    """Return only normalized ranked holders for one exact target."""
    normalized = _holder_payload_for_strict_parser(_payload(payload))
    holders = parse_ranked_holders(normalized, target=target)
    return [_holder_document(holder) for holder in holders]


def _relationship_document(edge: SubgraphEdge, target: TargetToken) -> dict:
    try:
        document = copy.deepcopy(edge.raw)
        data = copy.deepcopy(document["data"])
    except Exception:
        raise ValueError("clean relationship could not be copied safely") from None
    if type(document) is not dict or type(data) is not dict:
        raise ValueError("clean relationship must be an object")
    document["from_address"] = edge.from_address
    document["to_address"] = edge.to_address
    document["rel_type"] = "GROUPED_TRANSFER"
    data["total_transfers"] = edge.total_transfers
    data["token_ref"] = {
        "chain": target.chain,
        "address": target.token_address,
    }
    document["data"] = data
    return document


def clean_relationships(
    payload: object,
    target: TargetToken,
    holders: list[dict],
) -> list[dict]:
    """Filter subgraph rows to exact-target relationships between ranked holders."""
    ranked = parse_ranked_holders(holders, target=target)
    holder_index = {holder.address: holder for holder in ranked}
    edges = filter_subgraph_edges(
        _payload(payload),
        target=target,
        holders=holder_index,
    )
    return [_relationship_document(edge, target) for edge in edges]


def _clean_relative(target: TargetToken, kind: str) -> Path:
    relative, _canonical_kind, _member = market_artifacts._api_artifact_kind(
        target,
        kind,
        layer="clean",
    )
    return relative


def _read_json(path: Path) -> object:
    try:
        return market_artifacts._load_json_document(path)
    except (OSError, UnicodeError, ValueError):
        raise ValueError("clean artifact cannot be read") from None


def _read_clean_list(staging: Path, target: TargetToken, kind: str) -> list:
    payload = _read_json(Path(staging) / _clean_relative(target, kind))
    if type(payload) is not list:
        raise ValueError("clean snapshot artifact must be a top-level list")
    return payload


def read_clean_snapshot(staging: Path, target: TargetToken) -> SnapshotModel:
    """Reconstruct a typed snapshot exclusively from the current clean staging tree."""
    holder_payload = _read_clean_list(staging, target, "holders")
    relationship_payload = _read_clean_list(staging, target, "relationships")
    holders = parse_ranked_holders(holder_payload, target=target)
    edges = filter_subgraph_edges(
        relationship_payload,
        target=target,
        holders={holder.address: holder for holder in holders},
    )
    if len(relationship_payload) != len(edges):
        raise ValueError("clean relationships contain an unselected row")
    clusters = reconstruct_clusters(holders, edges)
    return SnapshotModel(
        target=target,
        holders=holders,
        edges=edges,
        clusters=clusters,
        fingerprint=token_snapshot_fingerprint(holders, edges),
        captured_at=_utc_now(),
    )


def ordinary_cluster_members(snapshot: SnapshotModel) -> tuple[str, ...]:
    """List deterministic ordinary Cluster members; Supernodes are excluded."""
    if not isinstance(snapshot, SnapshotModel):
        raise TypeError("snapshot must be a SnapshotModel")
    return tuple(
        holder.address
        for cluster in snapshot.clusters
        for holder in cluster.members
        if not holder.is_supernode
    )


def _cluster_rank(snapshot: SnapshotModel, member_address: str) -> int:
    for cluster in snapshot.clusters:
        if any(holder.address == member_address for holder in cluster.members):
            return cluster.cluster_rank
    raise ValueError("transfer member is not a Cluster member")


def _transfer_payload_for_strict_parser(payload: object) -> object:
    try:
        normalized = copy.deepcopy(payload)
    except Exception:
        raise ValueError("transfer payload could not be copied safely") from None
    if type(normalized) is not list:
        return normalized
    for row in normalized:
        if type(row) is not dict:
            continue
        data = row.get("data")
        if type(data) is not dict:
            continue
        value = data.get("value")
        if type(value) is not float:
            continue
        if math.isfinite(value):
            data["value"] = repr(value)
    return normalized


def clean_transfers(
    payload: object,
    target: TargetToken,
    member_address: str,
    snapshot: SnapshotModel,
) -> list[dict]:
    """Keep exact-target transfers involving one ordinary Cluster member."""
    if snapshot.target != target:
        raise ValueError("transfer snapshot target does not match")
    canonical_member = canonicalize_address(target.chain, member_address)
    if canonical_member != member_address:
        raise ValueError("transfer member address must be canonical")
    if canonical_member not in ordinary_cluster_members(snapshot):
        raise ValueError("transfers are only available for ordinary Cluster members")
    normalized = _transfer_payload_for_strict_parser(_payload(payload))
    filtered = _filter_member_payload(
        normalized,
        capture_member=canonical_member,
        target=target,
    )
    return [copy.deepcopy(transfer.raw_record) for transfer in filtered]


def read_all_clean_snapshot(
    staging: Path,
    target: TargetToken,
    unavailable_members: set[str] | frozenset[str] = frozenset(),
) -> tuple[SnapshotModel, dict[str, str]]:
    """Read a clean snapshot and bind every ordinary member to its clean file."""
    snapshot = read_clean_snapshot(staging, target)
    transfer_files: dict[str, str] = {}
    for member_address in ordinary_cluster_members(snapshot):
        if member_address in unavailable_members:
            continue
        path = Path(staging) / _clean_relative(
            target,
            f"transfers/{member_address}",
        )
        document = _read_json(path)
        if (
            type(document) is not dict
            or document.get("schema_version") != "v3"
            or document.get("canonical_chain") != target.chain
            or document.get("canonical_token_address") != target.token_address
            or document.get("member_address") != member_address
            or document.get("cluster_rank") != _cluster_rank(snapshot, member_address)
            or type(document.get("transfers")) is not list
            or document.get("transfer_count") != len(document["transfers"])
        ):
            raise ValueError("clean member transfer document is invalid")
        transfer_files[member_address] = str(path)
    return snapshot, transfer_files


def _transfer_payloads(transfer_files: dict[str, str]) -> dict[str, list]:
    if type(transfer_files) is not dict:
        raise TypeError("transfer_files must be a dictionary")
    payloads: dict[str, list] = {}
    for member_address, path_text in transfer_files.items():
        if type(member_address) is not str or type(path_text) is not str:
            raise ValueError("transfer file bindings must contain text")
        document = _read_json(Path(path_text))
        if type(document) is not dict or type(document.get("transfers")) is not list:
            raise ValueError("clean member transfer document is invalid")
        payloads[member_address] = document["transfers"]
    return payloads


def _assembled_result(
    snapshot: SnapshotModel,
    transfer_files: dict[str, str],
    unavailable_members: set[str] | frozenset[str] = frozenset(),
):
    return build_transfer_result(
        _transfer_payloads(transfer_files),
        target=snapshot.target,
        clusters=snapshot.clusters,
        edges=snapshot.edges,
        captured_at=snapshot.captured_at,
        unavailable_members=unavailable_members,
    )


def assemble_token(
    snapshot: SnapshotModel,
    transfer_files: dict[str, str],
    unavailable_members: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """Assemble a token summary that references, but does not embed, member files."""
    if not isinstance(snapshot, SnapshotModel):
        raise TypeError("snapshot must be a SnapshotModel")
    return _assembled_result(
        snapshot,
        transfer_files,
        unavailable_members,
    ).token_document


def _write_assembled_member_transfers(
    staging: Path,
    target: TargetToken,
    result: TransferResult,
) -> None:
    for member_address, document in result.member_documents.items():
        write_clean_member_transfers(
            staging,
            target,
            member_address,
            document["transfers"],
            cluster_rank=document["cluster_rank"],
        )


def _failure_record(
    target: TargetToken | None,
    *,
    stage: str,
    error: BaseException,
    member_address: str | None = None,
) -> dict:
    attempt_count = getattr(error, "attempts", 1)
    if type(attempt_count) is not int or attempt_count < 0:
        attempt_count = 1
    record = {
        "stage": stage,
        "type": type(error).__name__,
        "message": "capture failed",
        "attempt_count": attempt_count,
        "captured_at": _utc_now(),
    }
    if target is not None:
        record.update(
            {
                "chain": target.requested_chain,
                "token_address": target.requested_token_address,
            }
        )
    if member_address is not None:
        record["member_address"] = member_address
    http_status = getattr(error, "http_status", None)
    if type(http_status) is int and 100 <= http_status <= 599:
        record["http_status"] = http_status
    return record


def _raise_stage_failure(
    target: TargetToken | None,
    stage: str,
    error: BaseException,
    *,
    member_address: str | None = None,
) -> None:
    if isinstance(error, BatchFailure):
        raise error
    raise BatchFailure(
        _failure_record(
            target,
            stage=stage,
            error=error,
            member_address=member_address,
        )
    ) from None


def _skipped_entry(
    target: TargetToken,
    error: TopHoldersUnavailableError,
) -> dict:
    return {
        "requested_chain": target.requested_chain,
        "requested_token_address": target.requested_token_address,
        "canonical_chain": target.chain,
        "canonical_token_address": target.token_address,
        "stage": "holders",
        "http_status": error.http_status,
        "attempt_count": error.attempts,
        "reason": "top_holders_not_available",
        "captured_at": _utc_now(),
        "status": "skipped",
    }


def _capture_failed_entry(target: TargetToken, error_record: dict) -> dict:
    return {
        "requested_chain": target.requested_chain,
        "requested_token_address": target.requested_token_address,
        "canonical_chain": target.chain,
        "canonical_token_address": target.token_address,
        "stage": error_record["stage"],
        "http_status": error_record.get("http_status"),
        "attempt_count": error_record["attempt_count"],
        "reason": "capture_failed",
        "captured_at": error_record["captured_at"],
        "status": "skipped",
    }


def _snapshot_drift_records(
    target: TargetToken,
    count_drifts: tuple[dict, ...],
) -> list[dict]:
    records = []
    for drift in count_drifts:
        is_subgraph_omission = drift["edge_last_date"] is None
        records.append(
            {
                "chain": target.requested_chain,
                "token_address": target.requested_token_address,
                "stage": "final",
                "type": (
                    "TransferSubgraphOmission"
                    if is_subgraph_omission
                    else "TransferSnapshotDrift"
                ),
                "message": (
                    "transfer pair absent from subgraph response"
                    if is_subgraph_omission
                    else "new transfers captured after subgraph snapshot"
                ),
                "attempt_count": 0,
                "captured_at": _utc_now(),
                **drift,
            }
        )
    return records


def _entry(
    snapshot: SnapshotModel,
    transfer_files: dict[str, str],
    unavailable_members: set[str] | frozenset[str],
    *,
    result: TransferResult,
    token_file: str,
    raw_files: list[str],
    clean_files: list[str],
) -> dict:
    members = [holder for cluster in snapshot.clusters for holder in cluster.members]
    ordinary_count = sum(not holder.is_supernode for holder in members)
    supernode_count = sum(holder.is_supernode for holder in members)
    relative_member_files = [
        str(_clean_relative(snapshot.target, f"transfers/{member}"))
        for member in transfer_files
    ]
    return {
        "requested_chain": snapshot.target.requested_chain,
        "requested_token_address": snapshot.target.requested_token_address,
        "canonical_chain": snapshot.target.chain,
        "canonical_token_address": snapshot.target.token_address,
        "captured_at": snapshot.captured_at,
        "token_file": token_file,
        "member_files": relative_member_files,
        "raw_files": raw_files,
        "clean_files": clean_files,
        "cluster_count": len(snapshot.clusters),
        "ranked_holder_count": len(snapshot.holders),
        "clustered_member_count": len(members),
        "ordinary_member_count": ordinary_count,
        "supernode_count": supernode_count,
        "unique_transfer_count": result.unique_transfer_count,
        "transfer_view_count": result.transfer_view_count,
        "status": "success",
    }


async def run_generation(args: argparse.Namespace) -> dict:
    business_date = _china_today()
    generation_id = uuid.uuid4().hex
    paths = RunPaths.create(args.output_root, business_date, generation_id)
    args._run_paths = paths
    client = BubblemapsApiClient(
        timeout=args.api_timeout,
        max_attempts=args.api_max_attempts,
        retry_delay=args.api_retry_delay,
        min_request_interval=args.api_min_interval,
    )

    with generation_lock(paths, shared=False):
        recover_interrupted_publish(paths)
        try:
            settings = load_pg_settings()
            if args.symbols is None:
                database_targets = load_targets(settings)
            else:
                database_targets = load_targets(
                    settings,
                    symbols=args.symbols,
                )
            selected = select_targets(
                database_targets,
                args.limit,
                args.chain,
                args.token_address,
            )
            selected_targets = targets_to_dict(selected)
            write_targets(paths.staging, selected_targets)
            reloaded_targets = read_targets(paths.staging)
            selected = select_targets(
                reloaded_targets,
                limit=None,
                chain=None,
                token_address=None,
            )
        except Exception as error:
            _raise_stage_failure(None, "targets", error)

        raw_files: dict[tuple[str, str], list[str]] = {}
        clean_files: dict[tuple[str, str], list[str]] = {}
        captured_targets: list[TargetToken] = []
        skipped_entries: list[dict] = []
        errors: list[dict] = []
        unavailable_members: dict[tuple[str, str], set[str]] = {}

        for target in selected:
            identity = (target.chain, target.token_address)
            raw_files[identity] = []
            clean_files[identity] = []
            unavailable_members[identity] = set()
            try:
                holder_result = await client.get_top_holders(target)
            except TopHoldersUnavailableError as error:
                skipped_entries.append(_skipped_entry(target, error))
                continue
            except Exception as error:
                record = _failure_record(target, stage="holders", error=error)
                errors.append(record)
                skipped_entries.append(_capture_failed_entry(target, record))
                continue
            try:
                holder_clean = clean_holders(holder_result, target)
            except Exception as error:
                record = _failure_record(target, stage="holders", error=error)
                errors.append(record)
                skipped_entries.append(_capture_failed_entry(target, record))
                continue

            try:
                ranked_addresses = [row["address"] for row in holder_clean]
                subgraph_result = await client.get_subgraph(target, ranked_addresses)
            except Exception as error:
                record = _failure_record(target, stage="subgraph", error=error)
                errors.append(record)
                skipped_entries.append(_capture_failed_entry(target, record))
                continue
            try:
                relationship_clean = clean_relationships(
                    subgraph_result,
                    target,
                    holder_clean,
                )
            except Exception as error:
                record = _failure_record(target, stage="subgraph", error=error)
                errors.append(record)
                skipped_entries.append(_capture_failed_entry(target, record))
                continue

            try:
                raw_files[identity].append(
                    write_raw_response(paths.staging, target, "holders", holder_result)
                )
                clean_files[identity].append(
                    write_clean_response(
                        paths.staging,
                        target,
                        "holders",
                        holder_clean,
                    )
                )
                raw_files[identity].append(
                    write_raw_response(
                        paths.staging,
                        target,
                        "subgraph",
                        subgraph_result,
                    )
                )
                clean_files[identity].append(
                    write_clean_response(
                        paths.staging,
                        target,
                        "relationships",
                        relationship_clean,
                    )
                )
            except Exception as error:
                _raise_stage_failure(target, "write", error)
            captured_targets.append(target)

        for target in captured_targets:
            identity = (target.chain, target.token_address)
            try:
                snapshot = read_clean_snapshot(paths.staging, target)
            except Exception as error:
                _raise_stage_failure(target, "snapshot", error)
            for member_address in ordinary_cluster_members(snapshot):
                try:
                    transfer_result = await client.get_transfers(
                        target,
                        member_address,
                    )
                except Exception as error:
                    errors.append(
                        _failure_record(
                            target,
                            stage="transfers",
                            error=error,
                            member_address=member_address,
                        )
                    )
                    unavailable_members[identity].add(member_address)
                    continue
                try:
                    cleaned = clean_transfers(
                        transfer_result,
                        target,
                        member_address,
                        snapshot,
                    )
                except Exception as error:
                    errors.append(
                        _failure_record(
                            target,
                            stage="transfers",
                            error=error,
                            member_address=member_address,
                        )
                    )
                    unavailable_members[identity].add(member_address)
                    continue
                try:
                    raw_files[identity].append(
                        write_raw_response(
                            paths.staging,
                            target,
                            f"transfers/{member_address}",
                            transfer_result,
                        )
                    )
                    clean_files[identity].append(
                        write_clean_member_transfers(
                            paths.staging,
                            target,
                            member_address,
                            cleaned,
                            cluster_rank=_cluster_rank(snapshot, member_address),
                        )
                    )
                except Exception as error:
                    _raise_stage_failure(
                        target,
                        "write",
                        error,
                        member_address=member_address,
                    )

        entries: list[dict] = []
        for target in captured_targets:
            identity = (target.chain, target.token_address)
            unavailable = unavailable_members[identity]
            try:
                snapshot, transfer_files = read_all_clean_snapshot(
                    paths.staging,
                    target,
                    unavailable,
                )
                result = _assembled_result(
                    snapshot,
                    transfer_files,
                    unavailable,
                )
                _write_assembled_member_transfers(
                    paths.staging,
                    target,
                    result,
                )
                token_file = write_final_token(
                    paths.staging,
                    target,
                    result.token_document,
                )
                entries.append(
                    _entry(
                        snapshot,
                        transfer_files,
                        unavailable,
                        result=result,
                        token_file=token_file,
                        raw_files=raw_files[identity],
                        clean_files=clean_files[identity],
                    )
                )
                errors.extend(
                    _snapshot_drift_records(target, result.count_drifts)
                )
            except Exception as error:
                _raise_stage_failure(target, "final", error)

        try:
            captured_at = _utc_now()
            if errors:
                write_error_report(paths.staging, errors)
            manifest = build_api_manifest(
                paths.staging,
                generation_id=generation_id,
                business_date=business_date,
                captured_at=captured_at,
                targets=reloaded_targets,
                entries=entries,
                skipped_entries=skipped_entries,
                errors=errors,
            )
            write_success_manifest(paths.staging, manifest)
            validate_staging_generation(paths.staging)
            publish_success(paths)
            return manifest
        except Exception as error:
            _raise_stage_failure(None, "publish", error)


async def _run_and_preserve(args: argparse.Namespace) -> dict:
    try:
        return await run_generation(args)
    except BatchFailure as error:
        paths = getattr(args, "_run_paths", None)
        if isinstance(paths, RunPaths):
            # Reacquire the same generation lock before moving this run's staging.
            with generation_lock(paths, shared=False):
                preserve_failed_run(paths, error.error_record)
        raise


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parse_args(argv)
        asyncio.run(_run_and_preserve(arguments))
    except (
        BatchFailure,
        MarketGenerationLockError,
        PublicationRecoveryError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        print("capture failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
