#!/usr/bin/env python3
"""Build causal 5m/15m/1h/4h trade windows from SSH-side Binance 1m archives.

The script is intended to run on the market-data host.  It reads only closed
entries from the supplied daily-strategy trade file, prefers canonical over
legacy over validated staging archives, resamples 1m bars to 5m, and then
derives every higher interval from the same 5m source.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOTS = (
    ("ssh_canonical", Path("/data2/shares/raw/binance/vision/futures-um/klines/USDT/1m")),
    ("ssh_legacy", Path("/data/shares/raw/binance/vision/futures-um/klines/USDT/1m")),
    ("ssh_staging_validated", Path("/data2/shares/raw_tmp/binance/vision/futures-um/klines/USDT/1m")),
)
LOOKBACK_DAYS = 7
MAX_HOLD_DAYS = 14
INTERVALS = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


def timestamp_ms(raw: str) -> int:
    value = int(raw)
    return value // 1000 if value > 10**14 else value


def locate_archive(pair: str, day: date) -> tuple[str, Path] | None:
    name = f"{pair}-1m-{day.isoformat()}.zip"
    for source, root in ROOTS:
        path = root / day.isoformat() / name
        if path.is_file():
            return source, path
    return None


def parse_archive(path: Path) -> tuple[list[dict[str, Any]], str]:
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad is not None:
            raise ValueError(f"CRC failure in {path}: {bad}")
        names = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in {path}, got {names}")
        payload = archive.read(names[0])
    rows: list[dict[str, Any]] = []
    text = payload.decode("utf-8")
    for raw in csv.reader(text.splitlines()):
        if not raw or not raw[0].isdigit():
            continue
        rows.append(
            {
                "open_time_ms": timestamp_ms(raw[0]),
                "open": float(raw[1]),
                "high": float(raw[2]),
                "low": float(raw[3]),
                "close": float(raw[4]),
                "volume": float(raw[5]),
                "close_time_ms": timestamp_ms(raw[6]),
                "quote_volume": float(raw[7]),
                "trades": int(raw[8]),
                "taker_buy_volume": float(raw[9]),
                "taker_buy_quote_volume": float(raw[10]),
            }
        )
    rows.sort(key=lambda row: row["open_time_ms"])
    if len({row["open_time_ms"] for row in rows}) != len(rows):
        raise ValueError(f"duplicate timestamps in {path}")
    return rows, hashlib.sha256(path.read_bytes()).hexdigest()


def resample(rows: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    bucket_ms = minutes * 60_000
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["open_time_ms"] // bucket_ms * bucket_ms].append(row)
    output: list[dict[str, Any]] = []
    for bucket in sorted(groups):
        items = sorted(groups[bucket], key=lambda row: row["open_time_ms"])
        output.append(
            {
                "open_time_ms": bucket,
                "open": items[0]["open"],
                "high": max(item["high"] for item in items),
                "low": min(item["low"] for item in items),
                "close": items[-1]["close"],
                "volume": sum(item["volume"] for item in items),
                "close_time_ms": bucket + bucket_ms - 1,
                "quote_volume": sum(item["quote_volume"] for item in items),
                "trades": sum(item["trades"] for item in items),
                "taker_buy_volume": sum(item["taker_buy_volume"] for item in items),
                "taker_buy_quote_volume": sum(item["taker_buy_quote_volume"] for item in items),
            }
        )
    return output


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = (
        "open_time_utc", "open_time_ms", "open", "high", "low", "close",
        "volume", "quote_volume", "trades", "taker_buy_volume",
        "taker_buy_quote_volume", "close_time_ms",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "open_time_utc": datetime.fromtimestamp(
                        row["open_time_ms"] / 1000, timezone.utc
                    ).isoformat(),
                    **row,
                }
            )


def dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cutoff-exclusive", type=date.fromisoformat, default=date(2026, 8, 4))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    with args.trades.open(encoding="utf-8", newline="") as handle:
        trades = [row for row in csv.DictReader(handle) if row["status"] == "closed"]

    cases: list[dict[str, Any]] = []
    cases_by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for number, trade in enumerate(trades, start=1):
        entry = date.fromisoformat(trade["entry_date"])
        start = entry - timedelta(days=LOOKBACK_DAYS)
        desired_end = entry + timedelta(days=MAX_HOLD_DAYS + 1)
        end = min(desired_end, args.cutoff_exclusive)
        pair = f"{trade['symbol']}USDT"
        case = {
            "trade_number": number,
            "case_id": f"{number:03d}-{trade['symbol']}-{trade['signal_date']}",
            "symbol": trade["symbol"],
            "pair": pair,
            "signal_date": trade["signal_date"],
            "entry_date": trade["entry_date"],
            "entry_price": float(trade["entry_price"]),
            "fetch_start_utc": start.isoformat(),
            "fetch_end_utc": end.isoformat(),
            "desired_end_utc": desired_end.isoformat(),
            "right_censored": end < desired_end,
            "intervals": {},
        }
        cases.append(case)
        cases_by_pair[pair].append(case)

    missing_archives: list[dict[str, str]] = []
    source_counts: dict[str, int] = defaultdict(int)
    for pair_index, pair in enumerate(sorted(cases_by_pair), start=1):
        pair_cases = cases_by_pair[pair]
        needed = sorted(
            {
                day
                for case in pair_cases
                for day in dates(
                    date.fromisoformat(case["fetch_start_utc"]),
                    date.fromisoformat(case["fetch_end_utc"]),
                )
            }
        )
        minute_rows: list[dict[str, Any]] = []
        sources: dict[str, dict[str, str]] = {}
        for day in needed:
            located = locate_archive(pair, day)
            if located is None:
                missing_archives.append({"pair": pair, "date": day.isoformat()})
                continue
            source, path = located
            rows, digest = parse_archive(path)
            minute_rows.extend(rows)
            source_counts[source] += 1
            sources[day.isoformat()] = {
                "source": source,
                "archive": str(path),
                "sha256": digest,
                "one_minute_bars": str(len(rows)),
            }
        minute_rows.sort(key=lambda row: row["open_time_ms"])
        five_all = resample(minute_rows, 5)
        for case in pair_cases:
            start = datetime.fromisoformat(case["fetch_start_utc"]).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(case["fetch_end_utc"]).replace(tzinfo=timezone.utc)
            start_ms = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            five = [row for row in five_all if start_ms <= row["open_time_ms"] < end_ms]
            expected = int((end - start).total_seconds() // 300)
            case["five_minute_expected_bars"] = expected
            case["five_minute_actual_bars"] = len(five)
            case["coverage_ratio"] = len(five) / expected if expected else 0.0
            case["sources"] = [sources[d.isoformat()] for d in dates(start.date(), end.date()) if d.isoformat() in sources]
            for interval, minutes in INTERVALS.items():
                rows = five if minutes == 5 else resample(five, minutes)
                output = args.output / f"{case['case_id']}-{interval}.csv"
                write_rows(output, rows)
                case["intervals"][interval] = {
                    "file": output.name,
                    "bars": len(rows),
                    "first_open_utc": datetime.fromtimestamp(rows[0]["open_time_ms"] / 1000, timezone.utc).isoformat() if rows else None,
                    "last_open_utc": datetime.fromtimestamp(rows[-1]["open_time_ms"] / 1000, timezone.utc).isoformat() if rows else None,
                }
        print(f"[{pair_index}/{len(cases_by_pair)}] {pair}: {len(needed)} archives, {len(pair_cases)} cases", flush=True)

    manifest = {
        "source": "SSH Binance Futures UM 1m daily archives, resampled causally to 5m",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_priority": [source for source, _ in ROOTS],
        "lookback_days": LOOKBACK_DAYS,
        "max_hold_days": MAX_HOLD_DAYS,
        "data_cutoff_exclusive": args.cutoff_exclusive.isoformat(),
        "closed_input_trades": len(trades),
        "source_archive_counts": dict(source_counts),
        "missing_archives": missing_archives,
        "cases": cases,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"cases": len(cases), "missing_archives": len(missing_archives), "source_archive_counts": dict(source_counts)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
