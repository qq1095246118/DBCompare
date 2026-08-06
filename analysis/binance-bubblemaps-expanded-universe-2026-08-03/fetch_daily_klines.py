#!/usr/bin/env python3
"""Fetch and validate completed Binance futures daily bars for the expanded pool."""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
CONFIG = HERE / "expanded_universe_config.json"
OUTPUT_DIR = HERE / "klines-1d"
MANIFEST = OUTPUT_DIR / "manifest.json"
ENDPOINT = "https://fapi.binance.com/fapi/v1/klines"
CUTOFF = date(2026, 8, 3)
START = date(2025, 1, 1)
HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "DBCompare-expanded-pool/1.0",
}


def fetch(futures_symbol: str, attempts: int = 4) -> list[list[Any]]:
    url = f"{ENDPOINT}?{urllib.parse.urlencode({'symbol': futures_symbol, 'interval': '1d', 'limit': 1500})}"
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, list):
                raise ValueError(f"unexpected Binance response for {futures_symbol}")
            return payload
        except Exception as error:
            last = error
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    raise RuntimeError(f"failed to fetch {futures_symbol}") from last


def normalize(raw: list[list[Any]], start: date, cutoff: date) -> list[dict[str, Any]]:
    rows = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 11:
            continue
        open_ms = int(item[0])
        day = datetime.fromtimestamp(open_ms / 1000, timezone.utc).date()
        if day < start or day > cutoff:
            continue
        row = {
            "date": day.isoformat(),
            "open_time_ms": open_ms,
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5]),
            "close_time_ms": int(item[6]),
            "quote_volume": float(item[7]),
            "trades": int(item[8]),
            "taker_buy_volume": float(item[9]),
            "taker_buy_quote_volume": float(item[10]),
        }
        prices = [row[name] for name in ("open", "high", "low", "close")]
        if not all(math.isfinite(value) and value > 0 for value in prices):
            raise ValueError(f"invalid price row on {day}")
        if row["high"] < max(row["open"], row["close"]) or row["low"] > min(
            row["open"], row["close"]
        ):
            raise ValueError(f"invalid OHLC ordering on {day}")
        rows.append(row)
    rows.sort(key=lambda row: row["open_time_ms"])
    if len({row["open_time_ms"] for row in rows}) != len(rows):
        raise ValueError("duplicate daily open timestamp")
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--start-date", type=date.fromisoformat, default=START)
    parser.add_argument("--cutoff", type=date.fromisoformat, default=CUTOFF)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.start_date > args.cutoff:
        raise ValueError("start-date must not be after cutoff")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "manifest.json"
    manifest = {
        "source": ENDPOINT,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_start_utc": args.start_date.isoformat(),
        "cutoff_utc": args.cutoff.isoformat(),
        "pre_listing_policy": "no_backfill_no_forward_fill",
        "symbols": {},
    }
    fields = [
        "date",
        "open_time_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trades",
        "taker_buy_volume",
        "taker_buy_quote_volume",
        "close_time_ms",
    ]
    for symbol, spec in config["symbols"].items():
        futures_symbol = str(spec.get("futures_symbol") or (symbol + "USDT")).upper()
        rows = normalize(fetch(futures_symbol), args.start_date, args.cutoff)
        if not rows:
            raise ValueError(f"no completed daily bars for {symbol}")
        output = args.output_dir / f"{symbol}-1d.csv"
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        manifest["symbols"][symbol] = {
            "file": output.name,
            "futures_symbol": futures_symbol,
            "bars": len(rows),
            "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
            "effective_start_date": rows[0]["date"],
            "started_after_requested_start": rows[0]["date"] > args.start_date.isoformat(),
            "complete_through_cutoff": rows[-1]["date"] == args.cutoff.isoformat(),
        }
        print(
            f"{symbol}: {len(rows)} bars {rows[0]['date']}..{rows[-1]['date']}",
            flush=True,
        )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {manifest_path}")


if __name__ == "__main__":
    main()
