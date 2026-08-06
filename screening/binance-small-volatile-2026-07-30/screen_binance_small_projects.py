#!/usr/bin/env python3
"""Build a point-in-time Binance small/young/high-volatility monitoring universe."""

from __future__ import annotations

import csv
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
JSON_PATH = ROOT / "candidates.json"
CSV_PATH = ROOT / "candidates.csv"
REPORT_PATH = ROOT / "candidates.md"

FUTURES_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
FUTURES_TICKER_24H = "https://fapi.binance.com/fapi/v1/ticker/24hr"
SPOT_EXCHANGE_INFO = "https://api.binance.com/api/v3/exchangeInfo"
WEB3_SEARCH = (
    "https://web3.binance.com/bapi/defi/v5/public/wallet-direct/"
    "buw/wallet/market/token/search/ai"
)
HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "binance-web3/2.0 (Skill)",
}

MAX_LISTING_AGE_DAYS = 730
MIN_QUOTE_VOLUME_USD = 1_000_000
MAX_QUOTE_VOLUME_USD = 120_000_000
MIN_INTRADAY_RANGE_PCT = 8
MIN_ABS_CHANGE_PCT = 5
MIN_MARKET_CAP_USD = 3_000_000
MAX_MARKET_CAP_USD = 500_000_000
EXCLUDED_BASE_ASSETS = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "LINK",
    "AVAX", "DOT", "LTC", "BCH", "TON", "SUI", "APT", "NEAR", "ATOM",
    "UNI", "ETC", "FIL", "ICP", "XLM", "HBAR", "AAVE", "MKR", "USDC",
    "USDT", "FDUSD", "TUSD", "DAI", "ENA", "ONDO",
}


def request_json(url: str, attempts: int = 3) -> Any:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    raise RuntimeError(f"request failed: {url}") from last


def finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def tag_names(item: dict[str, Any]) -> list[str]:
    tags = item.get("tagsInfo")
    if not isinstance(tags, dict):
        return []
    result = []
    for entries in tags.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("tagName"), str):
                result.append(entry["tagName"])
    return result


def identity_priority(item: dict[str, Any]) -> tuple[int, float, float]:
    tags = tag_names(item)
    if "Alpha" in tags:
        recognition = 3
    elif "TGE" in tags:
        recognition = 2
    elif "Community Recognized" in tags:
        recognition = 1
    else:
        recognition = 0
    liquidity = finite_float(item.get("liquidity")) or 0
    market_cap = finite_float(item.get("marketCap")) or 0
    return recognition, liquidity, market_cap


def web3_identity(symbol: str) -> dict[str, Any] | None:
    url = f"{WEB3_SEARCH}?{urllib.parse.urlencode({'keyword': symbol})}"
    payload = request_json(url)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    exact = [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("symbol", "")).upper() == symbol
        and finite_float(row.get("marketCap")) is not None
    ]
    if not exact:
        return None
    chosen = max(exact, key=identity_priority)
    return {
        "project_name": chosen.get("name"),
        "chain_id": chosen.get("chainId"),
        "contract_address": chosen.get("contractAddress"),
        "market_cap_usd": finite_float(chosen.get("marketCap")),
        "web3_liquidity_usd": finite_float(chosen.get("liquidity")),
        "web3_tags": tag_names(chosen),
    }


def preliminary_score(row: dict[str, Any]) -> float:
    low_volume = max(0.0, 120 - row["quote_volume_usd"] / 1_000_000)
    newness = max(0.0, MAX_LISTING_AGE_DAYS - row["listing_age_days"])
    return (
        row["intraday_range_pct"] * 0.45
        + abs(row["change_24h_pct"]) * 0.35
        + low_volume * 0.08
        + newness / MAX_LISTING_AGE_DAYS * 10
    )


def final_score(row: dict[str, Any]) -> float:
    market_cap = row["market_cap_usd"]
    smallness = max(0.0, math.log10(MAX_MARKET_CAP_USD / market_cap))
    alpha_bonus = 4 if "Alpha" in row["web3_tags"] else 0
    return row["preliminary_score"] + smallness * 3 + alpha_bonus


def build_preliminary(exchange: dict[str, Any], tickers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    server_time = int(exchange["serverTime"])
    ticker_by_symbol = {
        row.get("symbol"): row for row in tickers if isinstance(row, dict)
    }
    rows = []
    for market in exchange.get("symbols", []):
        if (
            market.get("status") != "TRADING"
            or market.get("contractType") != "PERPETUAL"
            or market.get("quoteAsset") != "USDT"
        ):
            continue
        symbol = market.get("symbol")
        base = market.get("baseAsset")
        if (
            not isinstance(symbol, str)
            or not isinstance(base, str)
            or base in EXCLUDED_BASE_ASSETS
            or len(base) < 2
            or not base.isascii()
            or not base.isalnum()
            or base.startswith("1000")
        ):
            continue
        ticker = ticker_by_symbol.get(symbol)
        if not isinstance(ticker, dict):
            continue
        quote_volume = finite_float(ticker.get("quoteVolume"))
        change = finite_float(ticker.get("priceChangePercent"))
        high = finite_float(ticker.get("highPrice"))
        low = finite_float(ticker.get("lowPrice"))
        onboard = int(market.get("onboardDate") or 0)
        if None in (quote_volume, change, high, low) or low <= 0 or onboard <= 0:
            continue
        listing_age_days = max(0.0, (server_time - onboard) / 86_400_000)
        intraday_range = (high / low - 1) * 100
        if (
            listing_age_days > MAX_LISTING_AGE_DAYS
            or not MIN_QUOTE_VOLUME_USD <= quote_volume <= MAX_QUOTE_VOLUME_USD
            or (
                intraday_range < MIN_INTRADAY_RANGE_PCT
                and abs(change) < MIN_ABS_CHANGE_PCT
            )
        ):
            continue
        row = {
            "symbol": symbol,
            "base_asset": base,
            "listing_date_utc": datetime.fromtimestamp(
                onboard / 1000, tz=timezone.utc
            ).date().isoformat(),
            "listing_age_days": round(listing_age_days, 1),
            "quote_volume_usd": quote_volume,
            "change_24h_pct": change,
            "intraday_range_pct": intraday_range,
            "trade_count_24h": int(ticker.get("count") or 0),
        }
        row["preliminary_score"] = preliminary_score(row)
        rows.append(row)
    return sorted(rows, key=lambda item: item["preliminary_score"], reverse=True)


def build_report(output: dict[str, Any]) -> str:
    lines = [
        "# Binance 小流动性、高波动项目监控候选",
        "",
        f"- 数据时间：{output['captured_at']}",
        f"- Binance Futures 候选母集：{output['universe_count']} 个当前 TRADING 的 USDT 永续。",
        f"- 初筛通过：{output['preliminary_count']} 个；完成全部 Web3 身份/市值校验后保留 {len(output['candidates'])} 个。",
        "- 这是一份监控候选池，不代表项目存在操盘、欺诈或其他不当行为。",
        "",
        "## 筛选口径",
        "",
        f"- 上线不超过 {MAX_LISTING_AGE_DAYS} 天。",
        f"- 24小时成交额在 ${MIN_QUOTE_VOLUME_USD / 1e6:.0f}M—${MAX_QUOTE_VOLUME_USD / 1e6:.0f}M。",
        f"- 日内振幅至少 {MIN_INTRADAY_RANGE_PCT}% 或24小时绝对涨跌至少 {MIN_ABS_CHANGE_PCT}%。",
        f"- Binance Web3 同符号项目参考市值在 ${MIN_MARKET_CAP_USD / 1e6:.0f}M—${MAX_MARKET_CAP_USD / 1e6:.0f}M。",
        "- 排除主流币、稳定币、1000倍计价币、单字符符号和无法可靠匹配的非ASCII符号。",
        "",
        "## 候选名单",
        "",
        "| 排名 | 项目 | Symbol | 上线日 | 24h成交额 | 24h涨跌 | 日内振幅 | 参考市值 | 标签 |",
        "|---:|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for index, row in enumerate(output["candidates"], 1):
        tags = ", ".join(row["web3_tags"]) or "—"
        lines.append(
            f"| {index} | {row['project_name']} | {row['symbol']} "
            f"({'现货+合约' if row['spot_usdt_trading'] else '仅合约'}) | "
            f"{row['listing_date_utc']} | ${row['quote_volume_usd'] / 1e6:.2f}M | "
            f"{row['change_24h_pct']:+.2f}% | {row['intraday_range_pct']:.2f}% | "
            f"${row['market_cap_usd'] / 1e6:.2f}M | {tags} |"
        )
    lines.extend(
        [
            "",
            "## 使用限制",
            "",
            "- Binance Futures 不提供流通市值；参考市值来自 Binance Web3 同符号身份匹配，重名或跨链供应可能造成偏差。",
            "- 24小时成交额和振幅会持续变化，这份名单只代表抓取时点。",
            "- 真正用于链上监控前，必须再核对币安交易对、项目名称和目标合约地址三者一致。",
            "- “容易异动”不等于“存在操盘”；归因仍需结合 Holder、Cluster、Transfer、Swap、池储备和交易所成交数据。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    exchange = request_json(FUTURES_EXCHANGE_INFO)
    tickers = request_json(FUTURES_TICKER_24H)
    spot_exchange = request_json(SPOT_EXCHANGE_INFO)
    spot_usdt_symbols = {
        row.get("symbol")
        for row in spot_exchange.get("symbols", [])
        if row.get("status") == "TRADING"
        and row.get("quoteAsset") == "USDT"
        and row.get("isSpotTradingAllowed") is not False
    }
    preliminary = build_preliminary(exchange, tickers)
    enriched = []
    for row in preliminary:
        identity = web3_identity(row["base_asset"])
        time.sleep(0.08)
        if not identity or identity["market_cap_usd"] is None:
            continue
        if not MIN_MARKET_CAP_USD <= identity["market_cap_usd"] <= MAX_MARKET_CAP_USD:
            continue
        candidate = {**row, **identity}
        candidate["spot_usdt_trading"] = candidate["symbol"] in spot_usdt_symbols
        candidate["risk_score"] = final_score(candidate)
        enriched.append(candidate)
    candidates = sorted(
        enriched, key=lambda item: item["risk_score"], reverse=True
    )

    universe_count = sum(
        row.get("status") == "TRADING"
        and row.get("contractType") == "PERPETUAL"
        and row.get("quoteAsset") == "USDT"
        for row in exchange.get("symbols", [])
    )
    captured_at = datetime.fromtimestamp(
        int(exchange["serverTime"]) / 1000, tz=timezone.utc
    ).isoformat()
    output = {
        "captured_at": captured_at,
        "sources": {
            "futures_exchange_info": FUTURES_EXCHANGE_INFO,
            "futures_ticker_24h": FUTURES_TICKER_24H,
            "spot_exchange_info": SPOT_EXCHANGE_INFO,
            "web3_search": WEB3_SEARCH,
        },
        "parameters": {
            "max_listing_age_days": MAX_LISTING_AGE_DAYS,
            "quote_volume_usd": [MIN_QUOTE_VOLUME_USD, MAX_QUOTE_VOLUME_USD],
            "min_intraday_range_pct": MIN_INTRADAY_RANGE_PCT,
            "min_abs_change_pct": MIN_ABS_CHANGE_PCT,
            "market_cap_usd": [MIN_MARKET_CAP_USD, MAX_MARKET_CAP_USD],
            "preliminary_limit": None,
            "final_limit": None,
        },
        "universe_count": universe_count,
        "preliminary_count": len(preliminary),
        "enriched_count": len(enriched),
        "candidates": candidates,
    }
    JSON_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    REPORT_PATH.write_text(build_report(output))
    with CSV_PATH.open("w", newline="") as handle:
        fields = [
            "project_name", "symbol", "base_asset", "listing_date_utc",
            "listing_age_days", "quote_volume_usd", "change_24h_pct",
            "intraday_range_pct", "trade_count_24h", "market_cap_usd",
            "web3_liquidity_usd", "chain_id", "contract_address",
            "web3_tags", "spot_usdt_trading", "risk_score",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in candidates:
            document = {field: row.get(field) for field in fields}
            document["web3_tags"] = "|".join(row["web3_tags"])
            writer.writerow(document)
    print(f"wrote {JSON_PATH}")
    print(f"wrote {CSV_PATH}")
    print(f"wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
