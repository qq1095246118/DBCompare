#!/usr/bin/env python3
"""Review high-impact address labels through the authorized Arkham Intel API.

The API key is read from ``.env`` and is never printed or persisted. Only
pending high-impact endpoints (plus optional previously web-unlabeled rows)
are queried. Queue and checkpoint writes are serial and atomic.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
DEFAULT_QUEUE = ROOT / "arkham-review/arkham-label-queue.csv"
DEFAULT_STATE = ROOT / "arkham-review/arkham-api-review-state.json"
BASE_URL = "https://api.arkm.com"
CHAIN_MAP = {
    "eth": "ethereum",
    "ethereum": "ethereum",
    "bsc": "bsc",
    "solana": "solana",
    "base": "base",
    "arbitrum": "arbitrum_one",
    "arbitrum_one": "arbitrum_one",
    "polygon": "polygon",
    "avalanche": "avalanche",
}

CEX_BOUNDARY_TERMS = (
    "deposit",
    "hot wallet",
    "cold wallet",
    "prime custody",
    "exchange wallet",
    "cex wallet",
)
NON_CEX_USAGE_TERMS = (
    "airdrop distribution",
    "dex router",
    "dex aggregator",
    "decentralized exchange",
    "liquidity pool",
    "bridge",
    "staking",
    "vesting",
    "multisig",
    "safe proxy",
)
CEX_ENTITY_TYPES = {"cex", "exchange", "centralized_exchange", "centralized exchange"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--limit", type=int, default=0, help="0 means all candidates")
    parser.add_argument("--include-web-unlabeled", action="store_true")
    parser.add_argument("--min-interval", type=float, default=0.10)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--checkpoint-every", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


class RequestStartLimiter:
    """Globally space request starts while allowing response waits in parallel."""

    def __init__(self, minimum_interval: float) -> None:
        self.minimum_interval = minimum_interval
        self.next_start = 0.0
        self.lock = Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.next_start:
                time.sleep(self.next_start - now)
            self.next_start = time.monotonic() + self.minimum_interval


def load_api_key(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"environment file not found: {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    key = values.get("Arkm_API_KEY") or values.get("ARKHAM_API_KEY") or ""
    if not key:
        raise ValueError("Arkm_API_KEY/ARKHAM_API_KEY is missing or empty")
    return key


def read_queue(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    if not rows or not fieldnames:
        raise ValueError("Arkham label queue is empty")
    return rows, fieldnames


def atomic_write_queue(
    path: Path, rows: list[dict[str, str]], fieldnames: list[str]
) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def atomic_write_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def first_dict(item: dict[str, Any], *names: str) -> dict[str, Any]:
    for name in names:
        value = item.get(name)
        if isinstance(value, dict):
            return value
    return {}


def first_text(item: dict[str, Any], *names: str) -> str:
    for name in names:
        value = item.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def response_summary(chain: str, item: dict[str, Any]) -> dict[str, Any]:
    entity = first_dict(item, "arkhamEntity", "entity")
    label = first_dict(item, "arkhamLabel", "label")
    deposit = first_dict(item, "depositExchange", "deposit_exchange")
    return {
        "chain": first_text(item, "chain") or CHAIN_MAP.get(chain, chain),
        "entity_id": first_text(entity, "id", "entity_id"),
        "entity_name": first_text(entity, "name", "entity_name"),
        "entity_type": first_text(entity, "type", "entity_type"),
        "entity_service": entity.get("service"),
        "label": first_text(label, "name", "label"),
        "deposit_exchange_id": (
            first_text(item, "depositExchangeID", "depositExchangeId", "deposit_exchange_id")
            or first_text(deposit, "id", "entity_id")
        ),
        "deposit_exchange_name": first_text(deposit, "name", "entity_name"),
        "contract": item.get("contract"),
    }


def classify_summary(summary: dict[str, Any]) -> dict[str, str]:
    entity_name = str(summary.get("entity_name") or "").strip()
    entity_id = str(summary.get("entity_id") or "").strip()
    entity_type = str(summary.get("entity_type") or "").strip()
    label = str(summary.get("label") or "").strip()
    deposit_id = str(summary.get("deposit_exchange_id") or "").strip()
    deposit_name = str(summary.get("deposit_exchange_name") or "").strip()
    combined = " ".join((entity_name, entity_id, entity_type, label, deposit_id, deposit_name)).lower()
    non_cex_usage = any(term in combined for term in NON_CEX_USAGE_TERMS)
    explicit_boundary = bool(deposit_id) or any(term in combined for term in CEX_BOUNDARY_TERMS)
    exchange_entity = entity_type.lower() in CEX_ENTITY_TYPES

    if non_cex_usage:
        is_cex = "false"
        reason = "explicit non-CEX usage semantics"
    elif explicit_boundary:
        is_cex = "true"
        reason = "explicit CEX-boundary semantics"
    elif exchange_entity:
        is_cex = "true"
        reason = "Arkham entity_type identifies a centralized exchange"
    elif entity_name or entity_id or label:
        is_cex = "false"
        reason = "labeled entity/address without CEX-boundary semantics"
    else:
        is_cex = "unknown"
        reason = "no Arkham entity or usage label for requested chain"

    entity = entity_name or deposit_name or entity_id or deposit_id
    if not entity and label:
        entity = label.split(" ", 1)[0]
    if not entity and summary.get("contract") is True:
        entity = "Contract"
    output_label = label or deposit_name or entity
    if is_cex == "unknown":
        entity = "Unknown"
        output_label = "No Arkham API entity or usage label for requested chain"
    elif not output_label:
        output_label = "Contract" if summary.get("contract") is True else entity

    return {
        "entity": entity or "Unknown",
        "label": output_label or "Unspecified Arkham label",
        "is_cex": is_cex,
        "reason": reason,
        "status": (
            "reviewed_arkham_api_unlabeled"
            if is_cex == "unknown"
            else "reviewed_arkham_api"
        ),
    }


def fetch_address(
    key: str,
    chain: str,
    address: str,
    timeout: float,
    max_attempts: int,
) -> tuple[dict[str, Any], int]:
    api_chain = CHAIN_MAP.get(chain.lower())
    if not api_chain:
        raise ValueError(f"unsupported Arkham chain mapping: {chain}")
    quoted_address = urllib.parse.quote(address, safe="")
    url = f"{BASE_URL}/intelligence/address/{quoted_address}/all"
    request = urllib.request.Request(
        url,
        headers={
            "API-Key": key,
            "Accept": "application/json",
            "User-Agent": "DBCompare-Arkham-Label-Audit/1.0",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
                if not isinstance(payload, dict):
                    raise ValueError("Arkham API response is not a JSON object")
                item = payload.get(api_chain, {})
                if not isinstance(item, dict):
                    item = {}
                return item, int(response.status)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in {401, 403}:
                raise RuntimeError(f"Arkham API authorization failed with HTTP {exc.code}") from exc
            if exc.code == 429 or 500 <= exc.code < 600:
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    delay = max(float(retry_after), float(attempt))
                except ValueError:
                    delay = float(attempt)
                if attempt < max_attempts:
                    time.sleep(delay)
                    continue
            raise
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < max_attempts:
                time.sleep(float(attempt))
                continue
            raise
    raise RuntimeError(f"Arkham API request failed: {type(last_error).__name__}")


def candidate_rows(
    rows: list[dict[str, str]], include_web_unlabeled: bool
) -> list[dict[str, str]]:
    statuses = {"pending_web_review"}
    if include_web_unlabeled:
        statuses.add("reviewed_web_unlabeled")
    selected = [
        row
        for row in rows
        if row.get("arkham_status") in statuses
        and int(float(row.get("high_impact_transfer_count") or 0)) > 0
        and row.get("chain", "").lower() in CHAIN_MAP
    ]
    return sorted(
        selected,
        key=lambda row: float(row.get("max_cluster_share_pct") or 0),
        reverse=True,
    )


def update_row(
    row: dict[str, str], classification: dict[str, str], summary: dict[str, Any]
) -> None:
    address = row["address"].lower()
    chain = row["chain"].lower()
    is_cex = classification["is_cex"]
    row.update(
        {
            "arkham_status": classification["status"],
            "arkham_entity": classification["entity"],
            "arkham_label": classification["label"],
            "arkham_is_cex": "" if is_cex == "unknown" else is_cex,
            "arkham_reviewed_at": utc_now(),
            "minimum_cex_hops": "0" if is_cex == "true" else "",
            "cex_destination": classification["label"] if is_cex == "true" else "",
            "evidence": (
                f"{BASE_URL}/intelligence/address/{address}/all#chain="
                f"{CHAIN_MAP.get(chain, chain)}"
            ),
            "notes": (
                f"Arkham Intel API read-only lookup; {classification['reason']}; "
                f"entity_type={summary.get('entity_type') or ''}; "
                f"service={summary.get('entity_service')}; "
                f"contract={summary.get('contract')}."
            ),
        }
    )


def main() -> None:
    args = parse_args()
    if (
        args.min_interval < 0
        or args.max_attempts < 1
        or args.checkpoint_every < 1
        or args.workers < 1
    ):
        raise ValueError("invalid interval/attempt/checkpoint option")
    queue, fieldnames = read_queue(args.queue)
    candidates = candidate_rows(queue, args.include_web_unlabeled)
    if args.limit > 0:
        candidates = candidates[: args.limit]
    print(json.dumps({
        "candidate_count": len(candidates),
        "candidate_chains": dict(sorted(Counter(
            row.get("chain", "").lower() for row in candidates
        ).items())),
        "workers": args.workers,
        "include_web_unlabeled": args.include_web_unlabeled,
        "dry_run": args.dry_run,
    }, ensure_ascii=False))
    if args.dry_run or not candidates:
        return

    api_key = load_api_key(args.env_file)
    state = load_state(args.state)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.queue.with_name(f"{args.queue.stem}.{stamp}.bak{args.queue.suffix}")
    backup.write_bytes(args.queue.read_bytes())

    outcomes: Counter[str] = Counter()
    cex_hits: list[dict[str, str]] = []
    started = time.monotonic()
    completed = 0
    limiter = RequestStartLimiter(args.min_interval)

    def query(row: dict[str, str]) -> dict[str, Any]:
        limiter.wait()
        item, http_status = fetch_address(
            api_key, row["chain"], row["address"], args.timeout, args.max_attempts
        )
        summary = response_summary(row["chain"], item)
        classification = classify_summary(summary)
        return {
            "row": row,
            "http_status": http_status,
            "summary": summary,
            "classification": classification,
        }

    executor = ThreadPoolExecutor(max_workers=args.workers)
    try:
        batch_size = max(args.workers * 4, args.checkpoint_every)
        for offset in range(0, len(candidates), batch_size):
            batch = candidates[offset : offset + batch_size]
            futures: dict[Future[dict[str, Any]], dict[str, str]] = {
                executor.submit(query, row): row for row in batch
            }
            for future in as_completed(futures):
                row = futures[future]
                state_key = f"{row['chain'].lower()}:{row['address'].lower()}"
                try:
                    result = future.result()
                    summary = result["summary"]
                    classification = result["classification"]
                    update_row(row, classification, summary)
                    outcome = classification["status"]
                    outcomes[outcome] += 1
                    if classification["is_cex"] == "true":
                        cex_hits.append({
                            "chain": row["chain"].lower(),
                            "address": row["address"].lower(),
                            "entity": classification["entity"],
                            "label": classification["label"],
                        })
                    state[state_key] = {
                        "chain": row["chain"].lower(),
                        "address": row["address"].lower(),
                        "last_attempt_at": utc_now(),
                        "http_status": result["http_status"],
                        "outcome": outcome,
                        "summary": summary,
                    }
                except RuntimeError as exc:
                    if "authorization failed" in str(exc):
                        for pending in futures:
                            pending.cancel()
                        raise
                    outcomes["error"] += 1
                    state[state_key] = {
                        "chain": row["chain"].lower(),
                        "address": row["address"].lower(),
                        "last_attempt_at": utc_now(),
                        "outcome": "error",
                        "error_type": type(exc).__name__,
                    }
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                    outcomes["error"] += 1
                    state[state_key] = {
                        "chain": row["chain"].lower(),
                        "address": row["address"].lower(),
                        "last_attempt_at": utc_now(),
                        "outcome": "error",
                        "error_type": type(exc).__name__,
                    }
                completed += 1
                if completed % args.checkpoint_every == 0:
                    atomic_write_queue(args.queue, queue, fieldnames)
                    atomic_write_state(args.state, state)
                    print(f"checkpoint {completed}/{len(candidates)}")
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        atomic_write_queue(args.queue, queue, fieldnames)
        atomic_write_state(args.state, state)

    status_counts = Counter(row.get("arkham_status", "") for row in queue)
    print(json.dumps({
        "processed": completed,
        "outcomes": dict(outcomes),
        "cex_hit_count": len(cex_hits),
        "cex_hits": cex_hits,
        "pending": status_counts["pending_web_review"],
        "api_unlabeled": status_counts["reviewed_arkham_api_unlabeled"],
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "backup": str(backup),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
