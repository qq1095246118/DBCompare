#!/usr/bin/env python3
"""Select Arkham web-review candidates with a retry cooldown.

The review queue remains the source of truth.  This helper only records
transient browser outcomes so generic/error pages do not monopolize every
heartbeat run.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "arkham-review/arkham-label-queue.csv"
STATE = ROOT / "arkham-review/web-review-attempts.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_state() -> dict[str, dict[str, object]]:
    if not STATE.exists():
        return {}
    with STATE.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, dict[str, object]]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(STATE)


def address_key(chain: str, address: str) -> str:
    return f"{chain.lower()}:{address.lower()}"


def select_candidates(limit: int, cooldown_minutes: int, include_api_unlabeled: bool) -> None:
    with QUEUE.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    state = load_state()
    cutoff = utc_now() - timedelta(minutes=cooldown_minutes)
    seen: set[str] = set()
    selected: list[dict[str, object]] = []
    cooling = 0
    statuses = {"pending_web_review"}
    if include_api_unlabeled:
        statuses.add("reviewed_arkham_api_unlabeled")
    pending = sorted(
        (
            row for row in rows
            if row.get("arkham_status") in statuses
            and int(float(row.get("high_impact_transfer_count") or 0)) > 0
        ),
        key=lambda row: float(row.get("max_cluster_share_pct") or 0),
        reverse=True,
    )
    for row in pending:
        key = address_key(row.get("chain", ""), row.get("address", ""))
        if key in seen:
            continue
        seen.add(key)
        attempt = state.get(key, {})
        attempted_at = parse_time(str(attempt.get("last_attempt_at", "")))
        outcome = str(attempt.get("last_outcome", ""))
        if outcome in {"generic", "error"} and attempted_at and attempted_at > cutoff:
            cooling += 1
            continue
        selected.append(
            {
                "chain": row.get("chain", ""),
                "address": row.get("address", ""),
                "symbols": row.get("symbols", ""),
                "max_cluster_share_pct": float(row.get("max_cluster_share_pct") or 0),
                "source_status": row.get("arkham_status", ""),
            }
        )
        if len(selected) >= limit:
            break
    print(
        json.dumps(
            {
                "selected": selected,
                "selected_count": len(selected),
                "cooling_count": cooling,
                "pending_unique": len(seen),
                "cooldown_minutes": cooldown_minutes,
                "include_api_unlabeled": include_api_unlabeled,
            },
            ensure_ascii=False,
        )
    )


def record_attempt(chain: str, address: str, outcome: str, note: str) -> None:
    state = load_state()
    key = address_key(chain, address)
    previous = state.get(key, {})
    state[key] = {
        "chain": chain.lower(),
        "address": address.lower(),
        "last_attempt_at": utc_now().isoformat().replace("+00:00", "Z"),
        "last_outcome": outcome,
        "attempt_count": int(previous.get("attempt_count", 0)) + 1,
        "note": note,
    }
    save_state(state)
    print(f"recorded {key} -> {outcome}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    select = subparsers.add_parser("select")
    select.add_argument("--limit", type=int, default=24)
    select.add_argument("--cooldown-minutes", type=int, default=45)
    select.add_argument("--include-api-unlabeled", action="store_true")
    record = subparsers.add_parser("record")
    record.add_argument("--chain", required=True)
    record.add_argument("--address", required=True)
    record.add_argument(
        "--outcome",
        choices=("success", "generic", "error", "security"),
        required=True,
    )
    record.add_argument("--note", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "select":
        select_candidates(args.limit, args.cooldown_minutes, args.include_api_unlabeled)
    else:
        record_attempt(args.chain, args.address, args.outcome, args.note)


if __name__ == "__main__":
    main()
