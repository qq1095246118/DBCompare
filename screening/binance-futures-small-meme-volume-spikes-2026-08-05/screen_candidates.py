#!/usr/bin/env python3
"""Screen Binance USD-M futures for small/meme projects with volume-backed moves."""

from __future__ import annotations

import csv
import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
OUTPUT_JSON = ROOT / "screen-results.json"
OUTPUT_CSV = ROOT / "eligible-candidates.csv"
TOP100_JSON = ROOT / "top-100-additional.json"
PIPELINE_TOP100_JSON = ROOT / "top-100-eth-bsc.json"
REPORT_MD = ROOT / "report.md"

FUTURES_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"
WEB3_SEARCH = (
    "https://web3.binance.com/bapi/defi/v5/public/wallet-direct/"
    "buw/wallet/market/token/search/ai"
)
HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "binance-web3/2.0 (research-screen)",
}

LOOKBACK_DAYS = 365
BASELINE_DAYS = 30
KLINE_LIMIT = 400
MIN_HISTORY_DAYS = 180
MIN_VOLUME_RATIO = 5.0
MIN_ABS_RETURN_PCT = 10.0
MIN_INTRADAY_RANGE_PCT = 15.0
MIN_MARKET_CAP_USD = 3_000_000
MAX_SMALL_MARKET_CAP_USD = 300_000_000
MAX_IDENTITY_PRICE_RATIO = 3.0
MAX_WORKERS_KLINES = 10
MAX_WORKERS_WEB3 = 6

EXCLUDED_BASE_ASSETS = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "TRX", "LINK", "AVAX",
    "DOT", "LTC", "BCH", "TON", "SUI", "APT", "NEAR", "ATOM", "UNI",
    "ETC", "FIL", "ICP", "XLM", "HBAR", "AAVE", "MKR", "USDC", "USDT",
    "FDUSD", "TUSD", "DAI", "USDE", "USDP", "PYUSD", "EUR", "XAUT",
    "PAXG",
}
MEME_TAG_TERMS = (
    "meme", "pumpfun", "pump.fun", "fourmeme", "four.meme", "moonshot",
    "bonk", "flap",
)
MEME_NAME_TERMS = (
    "doge", "shib", "pepe", "floki", "inu", "memecoin", "meme coin",
    "cat coin", "dog coin", "frog coin",
)


def request_json(url: str, attempts: int = 4, timeout: int = 30) -> Any:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request failed: {url}") from last


def finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def normalized_identity_symbol(base_asset: str) -> str:
    if base_asset.startswith("1000") and len(base_asset) > 4:
        return base_asset[4:]
    if base_asset.startswith("1000000") and len(base_asset) > 7:
        return base_asset[7:]
    return base_asset


def contract_multiplier(base_asset: str) -> int:
    if base_asset.startswith("1000000") and len(base_asset) > 7:
        return 1_000_000
    if base_asset.startswith("1000") and len(base_asset) > 4:
        return 1_000
    return 1


def load_existing_symbols() -> set[str]:
    original = {
        "SIREN", "RAVE", "BIRB", "VELVET", "DEXE", "SOON", "ESPORTS",
        "KOMA", "CYS", "BULLA", "EVAA", "GWEI", "CLO",
    }
    expanded_path = (
        PROJECT_ROOT
        / "analysis/binance-bubblemaps-expanded-universe-2026-08-03/"
        "expanded_universe_config.json"
    )
    expanded = json.loads(expanded_path.read_text(encoding="utf-8"))
    return original | set(expanded["symbols"])


def futures_universe(exchange: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for row in exchange.get("symbols", []):
        if (
            row.get("status") != "TRADING"
            or row.get("contractType") != "PERPETUAL"
            or row.get("quoteAsset") != "USDT"
        ):
            continue
        base = str(row.get("baseAsset", "")).upper()
        identity_symbol = normalized_identity_symbol(base)
        if (
            not base
            or identity_symbol in EXCLUDED_BASE_ASSETS
            or len(identity_symbol) < 2
            or not identity_symbol.isascii()
            or not identity_symbol.isalnum()
        ):
            continue
        result.append(
            {
                "symbol": row["symbol"],
                "base_asset": base,
                "identity_symbol": identity_symbol,
                "onboard_date_ms": int(row.get("onboardDate") or 0),
            }
        )
    return result


def fetch_klines(market: dict[str, Any], server_time: int) -> dict[str, Any]:
    params = urllib.parse.urlencode(
        {"symbol": market["symbol"], "interval": "1d", "limit": KLINE_LIMIT}
    )
    rows = request_json(f"{FUTURES_KLINES}?{params}")
    completed = [row for row in rows if int(row[6]) < server_time]
    events = []
    cutoff_ms = server_time - LOOKBACK_DAYS * 86_400_000
    for index in range(BASELINE_DAYS, len(completed)):
        row = completed[index]
        if int(row[0]) < cutoff_ms:
            continue
        baseline = [float(x[7]) for x in completed[index - BASELINE_DAYS:index]]
        median_quote_volume = statistics.median(baseline)
        if median_quote_volume <= 0:
            continue
        open_price = float(row[1])
        high_price = float(row[2])
        low_price = float(row[3])
        close_price = float(row[4])
        quote_volume = float(row[7])
        volume_ratio = quote_volume / median_quote_volume
        abs_return_pct = abs(close_price / open_price - 1) * 100 if open_price else 0
        intraday_range_pct = (high_price / low_price - 1) * 100 if low_price else 0
        if (
            volume_ratio >= MIN_VOLUME_RATIO
            and (
                abs_return_pct >= MIN_ABS_RETURN_PCT
                or intraday_range_pct >= MIN_INTRADAY_RANGE_PCT
            )
        ):
            events.append(
                {
                    "date": datetime.fromtimestamp(
                        int(row[0]) / 1000, tz=timezone.utc
                    ).date().isoformat(),
                    "volume_ratio": volume_ratio,
                    "abs_return_pct": abs_return_pct,
                    "intraday_range_pct": intraday_range_pct,
                    "quote_volume_usd": quote_volume,
                    "baseline_median_quote_volume_usd": median_quote_volume,
                }
            )
    result = {**market, "bar_count": len(completed), "events": events}
    if completed:
        result["last_close"] = float(completed[-1][4])
        result["normalized_token_price"] = (
            result["last_close"] / contract_multiplier(market["base_asset"])
        )
        result["first_bar_date"] = datetime.fromtimestamp(
            int(completed[0][0]) / 1000, tz=timezone.utc
        ).date().isoformat()
        result["last_bar_date"] = datetime.fromtimestamp(
            int(completed[-1][0]) / 1000, tz=timezone.utc
        ).date().isoformat()
    if events:
        peak = max(events, key=lambda event: event["volume_ratio"])
        result.update(
            {
                "event_count": len(events),
                "max_volume_ratio": peak["volume_ratio"],
                "peak_event_date": peak["date"],
                "peak_abs_return_pct": peak["abs_return_pct"],
                "peak_intraday_range_pct": peak["intraday_range_pct"],
            }
        )
    else:
        result["event_count"] = 0
    return result


def tag_names(item: dict[str, Any]) -> list[str]:
    tags = item.get("tagsInfo")
    if not isinstance(tags, dict):
        return []
    result: list[str] = []
    for values in tags.values():
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict) and isinstance(value.get("tagName"), str):
                    result.append(value["tagName"])
    return result


def identity_recognition(item: dict[str, Any]) -> int:
    tags = tag_names(item)
    if "Alpha" in tags:
        return 3
    if "TGE" in tags:
        return 2
    if "Community Recognized" in tags:
        return 1
    return 0


def fetch_identity(market: dict[str, Any]) -> dict[str, Any]:
    keyword = market["identity_symbol"]
    url = f"{WEB3_SEARCH}?{urllib.parse.urlencode({'keyword': keyword})}"
    payload = request_json(url)
    rows = payload.get("data") if isinstance(payload, dict) else None
    exact = [
        row
        for row in rows or []
        if isinstance(row, dict)
        and str(row.get("symbol", "")).upper() == keyword
        and finite_float(row.get("marketCap")) is not None
        and (finite_float(row.get("price")) or 0) > 0
    ]
    if not exact:
        return {**market, "identity_status": "unmatched"}
    reference_price = market["normalized_token_price"]
    ranked = []
    for row in exact:
        web3_price = float(row["price"])
        price_ratio = max(web3_price / reference_price, reference_price / web3_price)
        ranked.append(
            (
                price_ratio,
                -identity_recognition(row),
                -(finite_float(row.get("liquidity")) or 0),
                row,
            )
        )
    price_ratio, _, _, chosen = min(ranked, key=lambda item: item[:3])
    if price_ratio > MAX_IDENTITY_PRICE_RATIO:
        return {
            **market,
            "identity_status": "price_mismatch",
            "closest_web3_price": finite_float(chosen.get("price")),
            "identity_price_ratio": price_ratio,
        }
    tags = tag_names(chosen)
    project_name = str(chosen.get("name") or keyword)
    search_text = " ".join([project_name, keyword, *tags]).lower()
    is_meme = any(term in search_text for term in (*MEME_TAG_TERMS, *MEME_NAME_TERMS))
    return {
        **market,
        "identity_status": "matched",
        "project_name": project_name,
        "chain_id": chosen.get("chainId"),
        "contract_address": chosen.get("contractAddress"),
        "market_cap_usd": finite_float(chosen.get("marketCap")),
        "web3_price_usd": finite_float(chosen.get("price")),
        "identity_price_ratio": price_ratio,
        "web3_liquidity_usd": finite_float(chosen.get("liquidity")),
        "web3_tags": tags,
        "is_meme": is_meme,
    }


def candidate_score(row: dict[str, Any]) -> float:
    cap = row.get("market_cap_usd") or MAX_SMALL_MARKET_CAP_USD
    smallness = max(0.0, math.log10(MAX_SMALL_MARKET_CAP_USD / max(cap, 1)))
    return (
        min(row["max_volume_ratio"], 50) * 2.0
        + min(row["event_count"], 12) * 4.0
        + min(row["peak_abs_return_pct"], 100) * 0.25
        + min(row["peak_intraday_range_pct"], 150) * 0.15
        + smallness * 8.0
        + (10.0 if row.get("is_meme") else 0.0)
    )


def build_report(output: dict[str, Any]) -> str:
    counts = output["counts"]
    lines = [
        "# Binance 小项目/Meme 历史放量异动筛选",
        "",
        f"- 抓取时间：{output['captured_at']}",
        f"- 当前 Binance USDⓈ-M USDT 永续母集：{counts['raw_usdt_perpetual']} 个。",
        f"- 排除主流/稳定/非加密身份后扫描：{counts['screened_markets']} 个。",
        f"- 至少 180 根完整日K且命中放量异动：{counts['volume_move_pass']} 个。",
        f"- 完成 Binance Web3 身份匹配：{counts['identity_matched']} 个。",
        f"- 满足“小项目或 Meme + 放量异动”：{counts['eligible_total']} 个。",
        f"- 排除已有 33 币后可新增：{counts['eligible_additional']} 个。",
        f"- 其中 Ethereum/BSC、可直接接入现有 Bubblemaps 管线：{counts['eligible_additional_eth_bsc']} 个。",
        f"- 可直接组成额外 100 币：{'是' if counts['eligible_additional'] >= 100 else '否'}。",
        "",
        "## 可复现口径",
        "",
        f"- 小项目：Binance Web3 参考市值 ${MIN_MARKET_CAP_USD/1e6:.0f}M—${MAX_SMALL_MARKET_CAP_USD/1e6:.0f}M；Meme 标签/名称命中时不受上限限制。",
        f"- 历史窗口：最近 {LOOKBACK_DAYS} 天，至少 {MIN_HISTORY_DAYS} 根完整日K。",
        f"- 异动：绝对日涨跌 ≥{MIN_ABS_RETURN_PCT:.0f}% 或日内振幅 ≥{MIN_INTRADAY_RANGE_PCT:.0f}%。",
        f"- 放量：异动日 USDT 成交额 ≥此前 {BASELINE_DAYS} 日中位数的 {MIN_VOLUME_RATIO:.0f} 倍。",
        "- 仅说明符合监控条件，不代表项目存在操盘或不当行为。",
        "",
        "## 现有 ETH/BSC 管线推荐新增 100",
        "",
        "| 排名 | 币种 | 项目 | 市值 | Meme | 异动次数 | 最大量比 | 峰值日 | 峰值涨跌 | 峰值振幅 |",
        "|---:|---|---|---:|:---:|---:|---:|---|---:|---:|",
    ]
    for index, row in enumerate(output["top_100_eth_bsc"], 1):
        cap = row.get("market_cap_usd")
        cap_text = f"${cap/1e6:.1f}M" if cap else "—"
        lines.append(
            f"| {index} | {row['base_asset']} | {row.get('project_name', '—')} | "
            f"{cap_text} | {'是' if row.get('is_meme') else '否'} | "
            f"{row['event_count']} | {row['max_volume_ratio']:.1f}x | "
            f"{row['peak_event_date']} | {row['peak_abs_return_pct']:.1f}% | "
            f"{row['peak_intraday_range_pct']:.1f}% |"
        )
    lines.extend(
        [
            "",
            "## 限制",
            "",
            "- Binance Web3 同符号身份可能存在重名或跨链供应误匹配，进入链上采集前仍需逐项核对合约地址。",
            "- Meme 判定采用 Binance Web3 标签及保守名称词表，可能漏掉未标注的 Meme 币。",
            "- 本筛选未要求 Bubblemaps 覆盖；正式进入 100 币采集前需再做链与 Top Holders/Subgraph 可用性门禁。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    exchange = request_json(FUTURES_EXCHANGE_INFO)
    server_time = int(exchange["serverTime"])
    raw_usdt_perpetual = sum(
        row.get("status") == "TRADING"
        and row.get("contractType") == "PERPETUAL"
        and row.get("quoteAsset") == "USDT"
        for row in exchange.get("symbols", [])
    )
    markets = futures_universe(exchange)
    print(f"universe={raw_usdt_perpetual} screened={len(markets)}", flush=True)

    kline_rows: list[dict[str, Any]] = []
    kline_errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS_KLINES) as pool:
        future_map = {
            pool.submit(fetch_klines, market, server_time): market for market in markets
        }
        for index, future in enumerate(as_completed(future_map), 1):
            market = future_map[future]
            try:
                kline_rows.append(future.result())
            except Exception as exc:
                kline_errors.append(
                    {"symbol": market["symbol"], "error": f"{type(exc).__name__}: {exc}"}
                )
            if index % 50 == 0 or index == len(markets):
                print(f"klines {index}/{len(markets)} errors={len(kline_errors)}", flush=True)

    volume_pass = [
        row
        for row in kline_rows
        if row["bar_count"] >= MIN_HISTORY_DAYS and row["event_count"] >= 1
    ]
    print(f"volume_move_pass={len(volume_pass)}", flush=True)

    identity_rows: list[dict[str, Any]] = []
    identity_errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS_WEB3) as pool:
        future_map = {pool.submit(fetch_identity, row): row for row in volume_pass}
        for index, future in enumerate(as_completed(future_map), 1):
            market = future_map[future]
            try:
                identity_rows.append(future.result())
            except Exception as exc:
                identity_errors.append(
                    {"symbol": market["symbol"], "error": f"{type(exc).__name__}: {exc}"}
                )
            if index % 25 == 0 or index == len(volume_pass):
                print(
                    f"identity {index}/{len(volume_pass)} errors={len(identity_errors)}",
                    flush=True,
                )

    matched = [row for row in identity_rows if row.get("identity_status") == "matched"]
    eligible = []
    for row in matched:
        cap = row.get("market_cap_usd")
        small = cap is not None and MIN_MARKET_CAP_USD <= cap <= MAX_SMALL_MARKET_CAP_USD
        eligible_meme = (
            cap is not None and cap >= MIN_MARKET_CAP_USD and row.get("is_meme")
        )
        if small or eligible_meme:
            row["is_small_project"] = small
            row["candidate_score"] = candidate_score(row)
            eligible.append(row)
    eligible.sort(key=lambda row: row["candidate_score"], reverse=True)

    existing = load_existing_symbols()
    additional = [
        row
        for row in eligible
        if row["base_asset"] not in existing
        and row["identity_symbol"] not in existing
    ]
    top_100 = additional[:100]
    eth_bsc_additional = [
        row for row in additional if str(row.get("chain_id")) in {"1", "56"}
    ]
    top_100_eth_bsc = eth_bsc_additional[:100]
    captured_at = datetime.fromtimestamp(server_time / 1000, tz=timezone.utc).isoformat()
    output = {
        "captured_at": captured_at,
        "sources": {
            "futures_exchange_info": FUTURES_EXCHANGE_INFO,
            "futures_klines": FUTURES_KLINES,
            "binance_web3_search": WEB3_SEARCH,
        },
        "parameters": {
            "lookback_days": LOOKBACK_DAYS,
            "baseline_days": BASELINE_DAYS,
            "minimum_history_days": MIN_HISTORY_DAYS,
            "minimum_volume_ratio": MIN_VOLUME_RATIO,
            "minimum_absolute_return_pct": MIN_ABS_RETURN_PCT,
            "minimum_intraday_range_pct": MIN_INTRADAY_RANGE_PCT,
            "small_project_market_cap_usd": [
                MIN_MARKET_CAP_USD,
                MAX_SMALL_MARKET_CAP_USD,
            ],
            "maximum_identity_price_ratio": MAX_IDENTITY_PRICE_RATIO,
        },
        "counts": {
            "raw_usdt_perpetual": raw_usdt_perpetual,
            "screened_markets": len(markets),
            "kline_success": len(kline_rows),
            "kline_errors": len(kline_errors),
            "volume_move_pass": len(volume_pass),
            "identity_matched": len(matched),
            "identity_errors": len(identity_errors),
            "eligible_total": len(eligible),
            "eligible_additional": len(additional),
            "eligible_additional_eth_bsc": len(eth_bsc_additional),
            "top_100_count": len(top_100),
            "top_100_eth_bsc_count": len(top_100_eth_bsc),
        },
        "existing_symbols_excluded": sorted(existing),
        "top_100_additional": top_100,
        "top_100_eth_bsc": top_100_eth_bsc,
        "eligible_additional_eth_bsc": eth_bsc_additional,
        "eligible_additional": additional,
        "eligible_total": eligible,
        "unmatched_identity": [
            row for row in identity_rows if row.get("identity_status") != "matched"
        ],
        "errors": {"klines": kline_errors, "identity": identity_errors},
    }
    OUTPUT_JSON.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    TOP100_JSON.write_text(
        json.dumps(
            {
                "captured_at": captured_at,
                "criteria": output["parameters"],
                "candidates": top_100,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    PIPELINE_TOP100_JSON.write_text(
        json.dumps(
            {
                "captured_at": captured_at,
                "criteria": output["parameters"],
                "chain_gate": {"chain_ids": ["1", "56"], "names": ["eth", "bsc"]},
                "candidates": top_100_eth_bsc,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    fields = [
        "candidate_score", "project_name", "symbol", "base_asset",
        "identity_symbol", "market_cap_usd", "is_small_project", "is_meme",
        "event_count", "max_volume_ratio", "peak_event_date",
        "peak_abs_return_pct", "peak_intraday_range_pct", "bar_count",
        "first_bar_date", "last_bar_date", "chain_id", "contract_address",
        "last_close", "normalized_token_price", "web3_price_usd",
        "identity_price_ratio", "web3_tags",
    ]
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in additional:
            exported = {**row, "web3_tags": "|".join(row.get("web3_tags", []))}
            writer.writerow(exported)
    REPORT_MD.write_text(build_report(output), encoding="utf-8")
    print(json.dumps(output["counts"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
