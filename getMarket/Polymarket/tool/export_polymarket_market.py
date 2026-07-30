"""Collect, rank, and save configured Polymarket markets."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timezone
import math
from pathlib import Path
import sys
import uuid
from zoneinfo import ZoneInfo


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if __package__ in (None, ""):
    sys.path.insert(0, str(_PROJECT_ROOT))

from common.artifacts import write_json_atomic
from getMarket.Polymarket.tool.final_contract import build_db_aligned_final
from getMarket.Polymarket.tool.market_filter import (
    TAG_CATEGORIES,
    TaggedMarket,
    compact_market,
    merge_markets,
)
from getMarket.Polymarket.tool.market_ranking import select_ranked_markets
from getMarket.Polymarket.tool.polymarket_api import (
    PolymarketApiClient,
    PolymarketApiError,
)


_CHINA_ZONE = ZoneInfo("Asia/Shanghai")
_DEFAULT_OUTPUT_ROOT = _PROJECT_ROOT / "getMarket" / "Polymarket" / "market"


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


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("business date must use YYYY-MM-DD") from None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=_DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--business-date", type=_date)
    parser.add_argument("--timeout", type=_positive_float, default=20.0)
    parser.add_argument("--max-attempts", type=_positive_int, default=3)
    parser.add_argument("--retry-delay", type=_nonnegative_float, default=0.25)
    parser.add_argument("--page-limit", type=_positive_int, default=20)
    arguments = parser.parse_args(argv)
    if arguments.page_limit > 20:
        parser.error("--page-limit must not exceed 20")
    return arguments


def _business_today() -> date:
    return datetime.now(_CHINA_ZONE).date()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_name(business_date: date) -> str:
    now = datetime.now(_CHINA_ZONE)
    return f"{business_date.isoformat()}_{now:%H%M%S}_{uuid.uuid4().hex[:8]}"


def _safe_error(error: BaseException, *, captured_at: str) -> dict[str, object]:
    if isinstance(error, PolymarketApiError):
        return {
            "stage": "request",
            "tag_id": error.tag_id,
            "cursor": error.cursor,
            "attempt_count": error.attempts,
            "http_status": error.status,
            "type": type(error).__name__,
            "message": "request failed",
            "captured_at": captured_at,
        }
    return {
        "stage": "processing",
        "tag_id": None,
        "cursor": None,
        "attempt_count": 0,
        "http_status": None,
        "type": type(error).__name__,
        "message": "processing failed",
        "captured_at": captured_at,
    }


def _raw_page(page) -> dict[str, object]:
    return {
        "tag_id": page.tag_id,
        "cursor": page.cursor,
        "captured_at": page.captured_at,
        "request_url": page.request_url,
        "http_status": page.status,
        "attempt_count": page.attempts,
        "payload": page.payload,
    }


async def run_async(
    args: argparse.Namespace,
    *,
    client: PolymarketApiClient | None = None,
) -> int:
    business_date = args.business_date or _business_today()
    captured_at = _utc_now()
    run_directory = args.output_root / _run_name(business_date)
    run_directory.mkdir(parents=True, exist_ok=False)
    api = client or PolymarketApiClient(
        timeout=args.timeout,
        max_attempts=args.max_attempts,
        retry_delay=args.retry_delay,
    )
    try:
        tagged: list[TaggedMarket] = []
        for tag_id in TAG_CATEGORIES:
            page_index = 0
            async for page in api.iter_tag(tag_id, page_limit=args.page_limit):
                page_index += 1
                write_json_atomic(
                    run_directory / "raw" / f"tag-{tag_id}" / f"page-{page_index:04d}.json",
                    _raw_page(page),
                )
                tagged.extend(
                    TaggedMarket(tag_id, compact_market(row))
                    for row in page.payload["markets"]
                )
        merged = merge_markets(tagged)
        ranked = select_ranked_markets(merged.markets)
        final_payload = build_db_aligned_final(
            ranked.selected,
            business_date=business_date,
            captured_at=datetime.fromisoformat(captured_at.replace("Z", "+00:00")),
        )
        write_json_atomic(run_directory / "clean.json", ranked.candidates)
        write_json_atomic(run_directory / "final.json", final_payload)
        return 0
    except Exception as error:
        write_json_atomic(
            run_directory / "error.json",
            _safe_error(error, captured_at=captured_at),
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
