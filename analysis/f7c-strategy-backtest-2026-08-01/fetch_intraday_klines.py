#!/usr/bin/env python3
"""Fetch official Binance futures 5m archives and resample intraday bars."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "intraday-data"
RAW_DIR = DATA_DIR / "raw"
MANIFEST = DATA_DIR / "manifest.json"
SOURCE_TEMPLATE = (
    "https://data.binance.vision/data/futures/um/monthly/klines/"
    "{pair}/5m/{pair}-5m-{month}.zip"
)
CASES = (
    {"symbol": "CYS", "pair": "CYSUSDT", "entry": "2026-03-22", "months": ["2026-03", "2026-04"]},
    {"symbol": "BULLA", "pair": "BULLAUSDT", "entry": "2026-04-12", "months": ["2026-04"]},
    {"symbol": "VELVET", "pair": "VELVETUSDT", "entry": "2026-06-07", "months": ["2026-06"]},
)
INTERVALS = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


def timestamp_ms(raw: str) -> int:
    value = int(raw)
    return value // 1000 if value > 10**14 else value


def download(url: str, destination: Path) -> bytes:
    if destination.exists():
        return destination.read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": "DBCompare/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
    destination.write_bytes(payload)
    return payload


def parse_archive(payload: bytes) -> list[dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in archive, got {names}")
        text = archive.read(names[0]).decode("utf-8")
    rows = []
    for raw in csv.reader(io.StringIO(text)):
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
    return rows


def resample(rows: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    bucket_ms = minutes * 60_000
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["open_time_ms"] // bucket_ms * bucket_ms].append(row)
    output = []
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
                "taker_buy_quote_volume": sum(
                    item["taker_buy_quote_volume"] for item in items
                ),
            }
        )
    return output


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "open_time_utc",
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


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {"source": "Binance Futures UM official data archive", "cases": []}
    for case in CASES:
        monthly_rows = []
        sources = []
        for month in case["months"]:
            url = SOURCE_TEMPLATE.format(pair=case["pair"], month=month)
            archive_path = RAW_DIR / Path(url).name
            payload = download(url, archive_path)
            monthly_rows.extend(parse_archive(payload))
            sources.append(
                {
                    "month": month,
                    "url": url,
                    "archive": str(archive_path.relative_to(HERE)),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        start = datetime.fromisoformat(case["entry"]).replace(tzinfo=timezone.utc)
        end = start + timedelta(days=15)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        five_minute = [
            row
            for row in monthly_rows
            if start_ms <= row["open_time_ms"] < end_ms
        ]
        case_manifest = {
            **case,
            "sources": sources,
            "window_start_utc": start.isoformat(),
            "window_end_utc": end.isoformat(),
            "intervals": {},
        }
        for interval, minutes in INTERVALS.items():
            rows = five_minute if minutes == 5 else resample(five_minute, minutes)
            output = DATA_DIR / f"{case['symbol']}-{interval}.csv"
            write_rows(output, rows)
            case_manifest["intervals"][interval] = {
                "file": output.name,
                "bars": len(rows),
                "first_open_utc": (
                    datetime.fromtimestamp(
                        rows[0]["open_time_ms"] / 1000, timezone.utc
                    ).isoformat()
                    if rows
                    else None
                ),
                "last_open_utc": (
                    datetime.fromtimestamp(
                        rows[-1]["open_time_ms"] / 1000, timezone.utc
                    ).isoformat()
                    if rows
                    else None
                ),
            }
        manifest["cases"].append(case_manifest)
    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {MANIFEST}")


if __name__ == "__main__":
    main()
