#!/usr/bin/env python3
"""Fetch exact-contract/CEX daily bars and calculate event returns."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
EVENTS_PATH = ROOT / "events.json"
PRICE_EVENTS_PATH = ROOT / "price_events.json"
RESULTS_PATH = ROOT / "price-results.json"
REPORT_PATH = ROOT / "price-event-report.md"

WEB3_URL = "https://dquery.sintral.io/u-kline/v1/k-line/candles"
SPOT_URL = "https://data-api.binance.vision/api/v3/klines"
LOCAL_KLINE_URL = "http://43.167.178.66:9527/api/v1/market-data/klines"
HEADERS = {
    "Accept-Encoding": "identity",
    "User-Agent": "binance-web3/2.0 (Skill)",
}

CHAIN_PLATFORM = {
    "eth": "ethereum",
    "bsc": "bsc",
    "base": "base",
    "solana": "solana",
}


def utc_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def pct(numerator: float, denominator: float) -> float | None:
    if not denominator or not math.isfinite(denominator):
        return None
    return (numerator / denominator - 1.0) * 100.0


def request_json(url: str, attempts: int = 4) -> Any:
    last: Exception | None = None
    request = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request failed after {attempts} attempts: {url}") from last


def web3_bars(platform: str, address: str, start: date, end: date) -> list[dict[str, Any]]:
    params = {
        "platform": platform,
        "address": address,
        "interval": "1d",
        "from": utc_ms(start),
        "to": utc_ms(end + timedelta(days=1)) - 1,
        "pm": "p",
    }
    payload = request_json(f"{WEB3_URL}?{urllib.parse.urlencode(params)}")
    status = payload.get("status") or {}
    if str(status.get("error_code", "0")) not in {"0", "000000", "None"}:
        return []
    rows = payload.get("data") or []
    bars = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        bars.append(
            {
                "date": datetime.fromtimestamp(int(row[5]) / 1000, tz=timezone.utc).date().isoformat(),
                "open": float(row[0]),
                "high": float(row[1]),
                "low": float(row[2]),
                "close": float(row[3]),
                "volume": float(row[4]),
                "trade_count": int(row[6]) if len(row) > 6 and row[6] is not None else None,
            }
        )
    return sorted(bars, key=lambda bar: bar["date"])


def spot_bars(symbol: str, start: date, end: date) -> list[dict[str, Any]]:
    params = {
        "symbol": f"{symbol}USDT",
        "interval": "1d",
        "startTime": utc_ms(start),
        "endTime": utc_ms(end + timedelta(days=1)) - 1,
        "limit": 1000,
    }
    try:
        payload = request_json(f"{SPOT_URL}?{urllib.parse.urlencode(params)}")
    except RuntimeError:
        return local_bars(symbol, start, end, market="spot")
    if not isinstance(payload, list):
        return []
    return [
        {
            "date": datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).date().isoformat(),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "trade_count": int(row[8]),
        }
        for row in payload
        if isinstance(row, list) and len(row) >= 9
    ]


def local_bars(symbol: str, start: date, end: date, market: str) -> list[dict[str, Any]]:
    params = {
        "market": market,
        "symbol": f"{symbol}USDT",
        "interval": "1d",
        "start_time": utc_ms(start),
        "end_time": utc_ms(end + timedelta(days=1)),
        "limit": 1000,
        "order": "asc",
    }
    try:
        payload = request_json(f"{LOCAL_KLINE_URL}?{urllib.parse.urlencode(params)}")
    except RuntimeError:
        return []
    items = ((payload.get("data") or {}).get("items") or []) if isinstance(payload, dict) else []
    return [
        {
            "date": datetime.fromtimestamp(int(row["open_time"]) / 1000, tz=timezone.utc).date().isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume") or 0),
            "trade_count": int(row.get("trade_count") or 0),
        }
        for row in items
    ]


def select_price_series(
    symbol: str,
    token_spec: dict[str, Any],
    start: date,
    end: date,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for chain, addresses in token_spec.get("targets", {}).items():
        platform = CHAIN_PLATFORM.get(chain)
        if not platform:
            continue
        for address in addresses:
            try:
                bars = web3_bars(platform, address, start, end)
            except RuntimeError as exc:
                errors.append(f"{chain}:{address[:10]}… {exc.__cause__ or exc}")
                continue
            if bars:
                candidates.append(
                    (
                        {
                            "kind": "Binance Web3 DEX",
                            "chain": chain,
                            "platform": platform,
                            "contract": address,
                            "quote": "USD",
                        },
                        bars,
                    )
                )

    if candidates:
        source, bars = max(candidates, key=lambda item: len(item[1]))
        expected_days = (end - start).days + 1
        coverage = len(bars) / expected_days
        source["date_coverage"] = coverage
        if coverage >= 0.80:
            return source, bars, errors

        spot = spot_bars(symbol, start, end)
        if len(spot) > len(bars):
            return (
                {
                    "kind": "Binance Spot",
                    "market": "spot",
                    "pair": f"{symbol}USDT",
                    "quote": "USDT",
                    "fallback_reason": (
                        f"精确合约 DEX 日 K 仅覆盖 {len(bars)}/{expected_days} 天，"
                        "改用主市场现货序列"
                    ),
                },
                spot,
                errors,
            )
        return source, bars, errors

    bars = spot_bars(symbol, start, end)
    if bars:
        return (
            {
                "kind": "Binance Spot",
                "market": "spot",
                "pair": f"{symbol}USDT",
                "quote": "USDT",
            },
            bars,
            errors,
        )

    bars = local_bars(symbol, start, end, market="um")
    if bars:
        return (
            {
                "kind": "Binance USDⓈ-M",
                "market": "um",
                "pair": f"{symbol}USDT",
                "quote": "USDT",
            },
            bars,
            errors,
        )
    return None, [], errors


def event_metrics(
    symbol: str,
    event: dict[str, Any],
    signal: dict[str, Any] | None,
    bars: list[dict[str, Any]],
    source: dict[str, Any] | None,
) -> dict[str, Any]:
    event_day = parse_day(event["date"])
    by_day = {parse_day(bar["date"]): bar for bar in bars}
    event_bar = by_day.get(event_day)
    row: dict[str, Any] = {
        "symbol": symbol,
        "event_date": event["date"],
        "event_end_date": event.get("end_date"),
        "document_rating": event.get("rating"),
        "signal": signal,
        "available": event_bar is not None,
    }
    if event_bar is None:
        row["reason"] = "事件日没有可用日 K"
        return row

    row["event_prices"] = event_bar
    row["event_day_open_close_return_pct"] = pct(event_bar["close"], event_bar["open"])
    previous_bar = by_day.get(event_day - timedelta(days=1))
    row["event_day_return_pct"] = (
        pct(event_bar["close"], previous_bar["close"]) if previous_bar else None
    )
    if source and source.get("kind") == "Binance Web3 DEX":
        event_volume = float(event_bar.get("volume") or 0)
        event_trades = int(event_bar.get("trade_count") or 0)
        if event_volume < 10_000 or event_trades < 50:
            row["price_quality"] = "低流动性"
        elif event_volume < 100_000 or event_trades < 100:
            row["price_quality"] = "中等流动性"
        else:
            row["price_quality"] = "正常"
    else:
        row["price_quality"] = "主市场"

    if signal:
        signal_day = parse_day(signal["date"])
        signal_bar = by_day.get(signal_day)
        if signal_bar:
            row["signal_price"] = signal_bar["close"]
            row["signal_to_event_close_pct"] = pct(event_bar["close"], signal_bar["close"])
            row["signal_lead_days"] = (event_day - signal_day).days
        else:
            row["signal_to_event_close_pct"] = None
            row["signal_price_reason"] = "信号日没有可用日 K"

    post_bars = []
    row["post_event_close_returns_pct"] = {}
    for horizon in (1, 3, 7):
        target = event_day + timedelta(days=horizon)
        bar = by_day.get(target)
        row["post_event_close_returns_pct"][f"d_plus_{horizon}"] = (
            pct(bar["close"], event_bar["close"]) if bar else None
        )
    for offset in range(1, 8):
        bar = by_day.get(event_day + timedelta(days=offset))
        if bar:
            post_bars.append(bar)
    row["post_7d_max_up_pct"] = (
        pct(max(bar["high"] for bar in post_bars), event_bar["close"]) if post_bars else None
    )
    row["post_7d_max_down_pct"] = (
        pct(min(bar["low"] for bar in post_bars), event_bar["close"]) if post_bars else None
    )
    if post_bars:
        highest = max(post_bars, key=lambda bar: bar["high"])
        lowest = min(post_bars, key=lambda bar: bar["low"])
        row["post_7d_max_high"] = {
            "date": highest["date"],
            "price": highest["high"],
            "return_from_event_close_pct": pct(highest["high"], event_bar["close"]),
        }
        row["post_7d_min_low"] = {
            "date": lowest["date"],
            "price": lowest["low"],
            "return_from_event_close_pct": pct(lowest["low"], event_bar["close"]),
        }

    daily_moves = []
    for offset in range(1, 8):
        current_day = event_day + timedelta(days=offset)
        current = by_day.get(current_day)
        previous = by_day.get(current_day - timedelta(days=1))
        if current and previous:
            daily_moves.append(
                {
                    "date": current_day.isoformat(),
                    "close_to_close_pct": pct(current["close"], previous["close"]),
                }
            )
    if daily_moves:
        row["post_7d_largest_daily_gain"] = max(
            daily_moves, key=lambda item: item["close_to_close_pct"]
        )
        row["post_7d_largest_daily_drop"] = min(
            daily_moves, key=lambda item: item["close_to_close_pct"]
        )
    return row


def fmt(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.2f}%"


def dated_move(item: dict[str, Any], *, decline: bool = False) -> str:
    if not item:
        return "N/A"
    value = item.get("close_to_close_pct")
    if value is None:
        return "N/A"
    if decline and value >= 0:
        return f"无下跌（最小 {item['date']} {fmt(value)}）"
    return f"{item['date']} {fmt(value)}"


def build_report(results: dict[str, Any]) -> str:
    available_events = [
        event
        for token in results["tokens"]
        for event in token["events"]
        if event.get("available")
    ]
    signal_events = [
        event
        for event in available_events
        if event.get("signal_to_event_close_pct") is not None
    ]
    signal_positive = sum(event["signal_to_event_close_pct"] > 0 for event in signal_events)
    d7_events = [
        event
        for event in signal_events
        if (event.get("post_event_close_returns_pct") or {}).get("d_plus_7") is not None
    ]
    d7_positive = sum(
        event["post_event_close_returns_pct"]["d_plus_7"] > 0 for event in d7_events
    )
    lines = [
        "# 链上异常后的价格涨跌复核",
        "",
        f"- 生成时间：{results['generated_at']}",
        "- 价格源优先级：文档精确合约对应的 Binance Web3 DEX 日 K；缺失时回退 Binance Spot/USDT，再回退 USDⓈ-M。",
        "- 时区：UTC；加密市场按自然日连续计算。",
        "- `信号→事件`：信号日收盘到事件日收盘；窗口级信号用 W-1 起点，仅表示整段持有结果。",
        "- `事件日涨跌`：事件日 UTC 收盘相对前一日 UTC 收盘；`事件后 D+n`：事件日收盘到第 n 个自然日收盘。",
        "",
        "## 汇总结论",
        "",
        f"- 可计算事件日涨跌的样本为 {len(available_events)} 个，事件日本身上涨 "
        f"{sum(event['event_day_return_pct'] > 0 for event in available_events)} 个、下跌 "
        f"{sum(event['event_day_return_pct'] < 0 for event in available_events)} 个。",
        f"- 有明确日期或窗口起点的前置信号为 {len(signal_events)} 个；信号日至事件日收盘上涨 "
        f"{signal_positive} 个、下跌 {len(signal_events) - signal_positive} 个。",
        f"- 同一批前置信号在事件后 7 日收盘上涨 {d7_positive} 个、下跌 "
        f"{len(d7_events) - d7_positive} 个：事件前后上涨很一致，但事件后的延续性并不一致。",
        "",
        "## 逐事件结果",
        "",
        "| Token | 事件日 | 价格源 | 信号提前 | 信号→事件 | 事件日 | 后1日 | 后3日 | 后7日 | 后7日最高 | 后7日最低 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for token in results["tokens"]:
        source = token.get("source") or {}
        if source.get("kind") == "Binance Web3 DEX":
            source_label = f"Web3/{source.get('chain')}"
        elif source.get("kind"):
            source_label = source["kind"].replace("Binance ", "")
        else:
            source_label = "无行情"
        for event in token["events"]:
            post = event.get("post_event_close_returns_pct") or {}
            event_source = source_label
            if event.get("price_quality") in {"低流动性", "中等流动性"}:
                event_source += f"（{event['price_quality']}）"
            lines.append(
                "| {symbol} | {date} | {source} | {lead} | {sig} | {day} | {d1} | {d3} | {d7} | {up} | {down} |".format(
                    symbol=token["symbol"],
                    date=event["event_date"],
                    source=event_source,
                    lead=(f"{event.get('signal_lead_days')}天" if event.get("signal_lead_days") is not None else "—"),
                    sig=fmt(event.get("signal_to_event_close_pct")),
                    day=fmt(event.get("event_day_return_pct")),
                    d1=fmt(post.get("d_plus_1")),
                    d3=fmt(post.get("d_plus_3")),
                    d7=fmt(post.get("d_plus_7")),
                    up=fmt(event.get("post_7d_max_up_pct")),
                    down=fmt(event.get("post_7d_max_down_pct")),
                )
            )

    lines.extend(["", "## 信号解释", ""])
    for token in results["tokens"]:
        for event in token["events"]:
            signal = event.get("signal")
            if not signal:
                continue
            lines.append(
                f"- **{token['symbol']} {event['event_date']}**：{signal['signal']}。"
                f"信号口径为{signal['basis']}（{signal['date']}）；"
                f"信号日至事件日收盘 {fmt(event.get('signal_to_event_close_pct'))}，"
                f"事件后 7 日收盘 {fmt((event.get('post_event_close_returns_pct') or {}).get('d_plus_7'))}。"
            )

    lines.extend(["", "## 事件后最大单日涨跌日期", ""])
    lines.extend(
        [
            "| Token | 事件日 | 后7日最大单日上涨 | 后7日最大单日下跌 | 期间最高点日期 | 期间最低点日期 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for token in results["tokens"]:
        for event in token["events"]:
            gain = event.get("post_7d_largest_daily_gain") or {}
            drop = event.get("post_7d_largest_daily_drop") or {}
            high = event.get("post_7d_max_high") or {}
            low = event.get("post_7d_min_low") or {}
            lines.append(
                f"| {token['symbol']} | {event['event_date']} | "
                f"{dated_move(gain)} | "
                f"{dated_move(drop, decline=True)} | "
                f"{high.get('date', 'N/A')} {fmt(high.get('return_from_event_close_pct'))} | "
                f"{low.get('date', 'N/A')} {fmt(low.get('return_from_event_close_pct'))} |"
            )

    lines.extend(
        [
            "",
            "## 不能建立链上信号—价格联系的样本",
            "",
        ]
    )
    for symbol, reason in results["excluded_linkages"].items():
        lines.append(f"- **{symbol}**：{reason}")
    lines.extend(
        [
            "",
            "## 限制",
            "",
            "- 日 K 只能说明时间上的先后与幅度，不能证明链上转账导致价格变化。",
            "- 事件只有日期、没有精确时刻；事件日涨跌采用 UTC 开盘到收盘，可能稀释盘中尖峰。",
            "- DEX 日 K 是精确合约的聚合美元价格；跨链流动性差异可能导致与 CEX 成交价不同。",
            "- `后7日最高/最低`使用日线 high/low，表示期间触及值，不代表收盘可实现收益。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    event_config = json.loads(EVENTS_PATH.read_text())
    price_config = json.loads(PRICE_EVENTS_PATH.read_text())
    signals = price_config["signal_definitions"]
    output: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology": {
            "timezone": "UTC",
            "signal_to_event": "signal-date close to event-date close",
            "event_day": "event-date open to event-date close",
            "post_event": "event-date close to D+1/D+3/D+7 close",
            "post_7d_extremes": "D+1 through D+7 high/low relative to event-date close",
        },
        "excluded_linkages": price_config["excluded_linkages"],
        "tokens": [],
    }

    for symbol, spec in event_config["symbols"].items():
        events = spec.get("events") or []
        if not events:
            output["tokens"].append(
                {"symbol": symbol, "source": None, "bars": 0, "events": [], "note": spec.get("note")}
            )
            continue

        signal_days = [
            parse_day(signals[key]["date"])
            for key in signals
            if key.startswith(f"{symbol}|")
        ]
        event_days = [parse_day(event["date"]) for event in events]
        start = min(signal_days + event_days) - timedelta(days=2)
        end = max(event_days) + timedelta(days=8)

        if symbol == "H":
            source = None
            bars: list[dict[str, Any]] = []
            errors: list[str] = []
            spot = spot_bars(symbol, start, end)
            if spot:
                source = {
                    "kind": "Binance Spot",
                    "market": "spot",
                    "pair": "HUSDT",
                    "quote": "USDT",
                    "linkage_warning": price_config["excluded_linkages"]["H"],
                }
                bars = spot
        else:
            source, bars, errors = select_price_series(symbol, spec, start, end)

        output["tokens"].append(
            {
                "symbol": symbol,
                "source": source,
                "bars": len(bars),
                "range": {"start": start.isoformat(), "end": end.isoformat()},
                "errors": errors,
                "events": [
                    event_metrics(
                        symbol,
                        event,
                        signals.get(f"{symbol}|{event['date']}"),
                        bars,
                        source,
                    )
                    for event in events
                ],
            }
        )

    RESULTS_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    REPORT_PATH.write_text(build_report(output))
    print(f"wrote {RESULTS_PATH}")
    print(f"wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
