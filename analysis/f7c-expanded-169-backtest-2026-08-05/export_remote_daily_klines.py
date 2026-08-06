#!/usr/bin/env python3
"""Export and validate Binance Vision daily futures bars on the data server."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOTS = (
    ("ssh_canonical", Path("/data2/shares/raw/binance/vision")),
    ("ssh_legacy", Path("/data/shares/raw/binance/vision")),
    ("ssh_staging_validated", Path("/data2/shares/raw_tmp/binance/vision")),
)
FIELDS = (
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
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--cutoff", type=date.fromisoformat, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--allow-missing-after-listing",
        action="store_true",
        help="keep sparse history and record missing days instead of aborting",
    )
    return parser.parse_args()


def days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def selected_symbols(registry: dict) -> dict[str, str]:
    output = {}
    for symbol, spec in registry["symbols"].items():
        state = spec["source_readiness"]["postgresql"]["state"]
        if state != "pg_ready":
            continue
        output[symbol] = str(spec.get("futures_symbol") or f"{symbol}USDT").upper()
    return output


def source_zip(futures_symbol: str, day: date) -> tuple[str, Path] | None:
    day_text = day.isoformat()
    suffix = Path(
        f"futures-um/klines/USDT/1d/{day_text}/"
        f"{futures_symbol}-1d-{day_text}.zip"
    )
    for source, root in ROOTS:
        candidate = root / suffix
        if candidate.is_file():
            return source, candidate
    return None


def read_bar(path: Path, expected_day: date) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        corrupt = archive.testzip()
        if corrupt:
            raise ValueError(f"CRC failure in {path}: {corrupt}")
        names = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in {path}, found {len(names)}")
        text = archive.read(names[0]).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    if len(rows) != 1:
        raise ValueError(f"expected one daily row in {path}, found {len(rows)}")
    raw = rows[0]
    open_time_ms = int(raw["open_time"])
    actual_day = datetime.fromtimestamp(open_time_ms / 1000, timezone.utc).date()
    if actual_day != expected_day:
        raise ValueError(f"date mismatch in {path}: {actual_day} != {expected_day}")
    result: dict[str, object] = {
        "date": actual_day.isoformat(),
        "open_time_ms": open_time_ms,
        "open": float(raw["open"]),
        "high": float(raw["high"]),
        "low": float(raw["low"]),
        "close": float(raw["close"]),
        "volume": float(raw["volume"]),
        "quote_volume": float(raw["quote_volume"]),
        "trades": int(raw.get("count") or raw.get("trades") or 0),
        "taker_buy_volume": float(raw["taker_buy_volume"]),
        "taker_buy_quote_volume": float(raw["taker_buy_quote_volume"]),
        "close_time_ms": int(raw["close_time"]),
    }
    prices = [float(result[name]) for name in ("open", "high", "low", "close")]
    if not all(math.isfinite(value) and value > 0 for value in prices):
        raise ValueError(f"invalid OHLC values in {path}")
    if result["high"] < max(result["open"], result["close"]):
        raise ValueError(f"invalid high in {path}")
    if result["low"] > min(result["open"], result["close"]):
        raise ValueError(f"invalid low in {path}")
    return result


def main() -> None:
    args = parse_args()
    if args.start_date > args.cutoff:
        raise ValueError("start-date must not be after cutoff")
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    symbols = selected_symbols(registry)
    if len(symbols) != 169:
        raise ValueError(f"expected 169 pg_ready symbols, found {len(symbols)}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "source": "Binance Vision via SSH source-priority export",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_start_utc": args.start_date.isoformat(),
        "cutoff_utc": args.cutoff.isoformat(),
        "pre_listing_policy": "no_backfill_no_forward_fill",
        "source_priority": [source for source, _ in ROOTS],
        "symbols": {},
    }
    def export_one(symbol: str, futures_symbol: str) -> tuple[str, dict]:
        rows = []
        row_sources = Counter()
        first_seen: date | None = None
        missing_after_listing = []
        for day in days(args.start_date, args.cutoff):
            located = source_zip(futures_symbol, day)
            if located is None:
                if first_seen is not None:
                    missing_after_listing.append(day.isoformat())
                continue
            source, path = located
            if missing_after_listing and not args.allow_missing_after_listing:
                raise ValueError(
                    f"{symbol} has missing days after listing: {missing_after_listing[:5]}"
                )
            first_seen = first_seen or day
            rows.append(read_bar(path, day))
            row_sources[source] += 1
        if not rows:
            raise ValueError(f"no daily bars found for {symbol}")
        if rows[-1]["date"] != args.cutoff.isoformat():
            raise ValueError(
                f"{symbol} incomplete through cutoff: last={rows[-1]['date']}"
            )
        if len({int(row["open_time_ms"]) for row in rows}) != len(rows):
            raise ValueError(f"duplicate open_time_ms for {symbol}")
        output_path = args.output_dir / f"{symbol}-1d.csv"
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return symbol, {
            "file": output_path.name,
            "futures_symbol": futures_symbol,
            "bars": len(rows),
            "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
            "started_after_requested_start": rows[0]["date"] > args.start_date.isoformat(),
            "complete_through_cutoff": True,
            "missing_after_listing": missing_after_listing,
            "source_rows": dict(sorted(row_sources.items())),
        }

    if args.workers < 1:
        raise ValueError("workers must be positive")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(export_one, symbol, futures_symbol): symbol
            for symbol, futures_symbol in symbols.items()
        }
        completed = 0
        for future in as_completed(futures):
            symbol, item = future.result()
            manifest["symbols"][symbol] = item
            completed += 1
            print(
                f"[{completed:03d}/{len(symbols)}] {symbol}: {item['bars']} "
                f"{item['first_date']}..{item['last_date']}",
                flush=True,
            )

    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "symbols": len(symbols),
        "bars": sum(item["bars"] for item in manifest["symbols"].values()),
        "source_rows": dict(sorted(sum(
            (Counter(item["source_rows"]) for item in manifest["symbols"].values()),
            Counter(),
        ).items())),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
