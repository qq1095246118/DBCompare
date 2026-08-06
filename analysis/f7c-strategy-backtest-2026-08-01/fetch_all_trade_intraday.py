#!/usr/bin/env python3
"""Fetch 5m Binance futures archives for every existing strategy trade.

Monthly archives are preferred.  If a monthly archive is not published yet,
the script falls back to the required daily archives.  Higher intervals are
always resampled from the same 5m source.
"""

from __future__ import annotations

import csv
import hashlib
import json
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fetch_intraday_klines import parse_archive, resample, write_rows


HERE = Path(__file__).resolve().parent
TRADES_CSV = HERE / "trades.csv"
DATA_DIR = HERE / "intraday-all-data"
RAW_DIR = DATA_DIR / "raw"
MANIFEST = DATA_DIR / "manifest.json"
MONTHLY_TEMPLATE = (
    "https://data.binance.vision/data/futures/um/monthly/klines/"
    "{pair}/5m/{pair}-5m-{month}.zip"
)
DAILY_TEMPLATE = (
    "https://data.binance.vision/data/futures/um/daily/klines/"
    "{pair}/5m/{pair}-5m-{day}.zip"
)
LOOKBACK_DAYS = 7
MAX_HOLD_DAYS = 14
DATA_CUTOFF = date(2026, 8, 1)
INTERVALS = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


def daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days)]


def download(url: str, destination: Path) -> bytes | None:
    if destination.exists():
        return destination.read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": "DBCompare/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return payload


def month_key(day: date) -> str:
    return day.strftime("%Y-%m")


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with TRADES_CSV.open(encoding="utf-8", newline="") as handle:
        trades = list(csv.DictReader(handle))

    requirements: dict[tuple[str, str], set[date]] = defaultdict(set)
    cases = []
    for number, trade in enumerate(trades, start=1):
        entry_day = date.fromisoformat(trade["entry_date"])
        fetch_start = entry_day - timedelta(days=LOOKBACK_DAYS)
        desired_end = entry_day + timedelta(days=MAX_HOLD_DAYS + 1)
        fetch_end = min(desired_end, DATA_CUTOFF)
        pair = f"{trade['symbol']}USDT"
        case_id = f"{number:02d}-{trade['symbol']}-{trade['signal_date']}"
        case = {
            "trade_number": number,
            "case_id": case_id,
            "symbol": trade["symbol"],
            "pair": pair,
            "signal_date": trade["signal_date"],
            "entry_date": trade["entry_date"],
            "entry_price": float(trade["entry_price"]),
            "fetch_start_utc": fetch_start.isoformat(),
            "fetch_end_utc": fetch_end.isoformat(),
            "desired_end_utc": desired_end.isoformat(),
            "right_censored": fetch_end < desired_end,
            "intervals": {},
        }
        cases.append(case)
        for day in daterange(fetch_start, fetch_end):
            requirements[(pair, month_key(day))].add(day)

    archive_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    archive_sources: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (pair, month), required_days in sorted(requirements.items()):
        monthly_url = MONTHLY_TEMPLATE.format(pair=pair, month=month)
        monthly_path = RAW_DIR / "monthly" / Path(monthly_url).name
        payload = download(monthly_url, monthly_path)
        if payload is not None:
            archive_rows[(pair, month)] = parse_archive(payload)
            archive_sources[(pair, month)] = [
                {
                    "kind": "monthly",
                    "url": monthly_url,
                    "archive": str(monthly_path.relative_to(HERE)),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ]
            print(f"monthly {pair} {month}")
            continue

        rows = []
        sources = []
        for day in sorted(required_days):
            day_text = day.isoformat()
            daily_url = DAILY_TEMPLATE.format(pair=pair, day=day_text)
            daily_path = RAW_DIR / "daily" / pair / Path(daily_url).name
            daily_payload = download(daily_url, daily_path)
            if daily_payload is None:
                sources.append({"kind": "daily_missing", "url": daily_url})
                continue
            rows.extend(parse_archive(daily_payload))
            sources.append(
                {
                    "kind": "daily",
                    "url": daily_url,
                    "archive": str(daily_path.relative_to(HERE)),
                    "sha256": hashlib.sha256(daily_payload).hexdigest(),
                }
            )
        archive_rows[(pair, month)] = rows
        archive_sources[(pair, month)] = sources
        print(f"daily fallback {pair} {month}: {len(rows)} bars")

    for case in cases:
        fetch_start = datetime.fromisoformat(case["fetch_start_utc"]).replace(tzinfo=timezone.utc)
        fetch_end = datetime.fromisoformat(case["fetch_end_utc"]).replace(tzinfo=timezone.utc)
        start_ms = int(fetch_start.timestamp() * 1000)
        end_ms = int(fetch_end.timestamp() * 1000)
        months = sorted({month_key(day) for day in daterange(fetch_start.date(), fetch_end.date())})
        combined = []
        sources = []
        for month in months:
            combined.extend(archive_rows.get((case["pair"], month), []))
            sources.extend(archive_sources.get((case["pair"], month), []))
        unique = {int(row["open_time_ms"]): row for row in combined}
        five_minute = [
            unique[key]
            for key in sorted(unique)
            if start_ms <= key < end_ms
        ]
        case["sources"] = sources
        expected_bars = int((fetch_end - fetch_start).total_seconds() // 300)
        case["five_minute_expected_bars"] = expected_bars
        case["five_minute_actual_bars"] = len(five_minute)
        case["coverage_ratio"] = len(five_minute) / expected_bars if expected_bars else 0
        for interval, minutes in INTERVALS.items():
            rows = five_minute if minutes == 5 else resample(five_minute, minutes)
            output = DATA_DIR / f"{case['case_id']}-{interval}.csv"
            if rows:
                write_rows(output, rows)
            case["intervals"][interval] = {
                "file": output.name,
                "bars": len(rows),
                "first_open_utc": (
                    datetime.fromtimestamp(rows[0]["open_time_ms"] / 1000, timezone.utc).isoformat()
                    if rows
                    else None
                ),
                "last_open_utc": (
                    datetime.fromtimestamp(rows[-1]["open_time_ms"] / 1000, timezone.utc).isoformat()
                    if rows
                    else None
                ),
            }

    manifest = {
        "source": "Binance Futures UM official data archive",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "max_hold_days": MAX_HOLD_DAYS,
        "data_cutoff_exclusive": DATA_CUTOFF.isoformat(),
        "cases": cases,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST}")


if __name__ == "__main__":
    main()
