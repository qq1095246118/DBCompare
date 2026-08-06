#!/usr/bin/env python3
"""Write a reproducible status report for the expanded data pipeline."""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def csv_rows(path: Path):
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    klines = json.loads((ROOT / "klines-1d/manifest.json").read_text(encoding="utf-8"))
    snapshot = json.loads(
        (ROOT / "bubblemaps-snapshot/manifest.json").read_text(encoding="utf-8")
    )
    queue = csv_rows(ROOT / "arkham-review/arkham-label-queue.csv")
    direct_events = csv_rows(ROOT / "cex-flow/direct-cex-events.csv")
    daily_flows = csv_rows(ROOT / "cex-flow/daily-cex-net-flows.csv")
    label_status = Counter(row["arkham_status"] for row in queue)
    reviewed = sum(
        count for status, count in label_status.items() if status.startswith("reviewed_")
    )
    api_reviewed = sum(
        count
        for status, count in label_status.items()
        if status.startswith("reviewed_arkham_api")
    )
    confirmed_cex = sum(row.get("arkham_is_cex") == "true" for row in queue)
    api_cex = sum(
        row.get("arkham_status") == "reviewed_arkham_api"
        and row.get("arkham_is_cex") == "true"
        for row in queue
    )
    available = sum(int(row["available_member_count"]) for row in snapshot["tokens"])
    total = sum(int(row["ordinary_member_count"]) for row in snapshot["tokens"])
    coverage = available / total * 100 if total else 0
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    lines = [
        "# 20币扩展池数据采集与CEX标注状态",
        "",
        f"生成时间：{now}",
        "",
        "## 总览",
        "",
        f"- 币安日K：{len(klines['symbols'])}/20 个币完成，共 "
        f"{sum(int(row['bars']) for row in klines['symbols'].values())} 根，截止 "
        f"{max(row['last_date'] for row in klines['symbols'].values())}。",
        "- Bubblemaps Holder/Cluster：20/20 完成。",
        f"- Bubblemaps 成员历史 transfers：{available}/{total}，覆盖率 "
        f"{coverage:.2f}%；快照状态 `{snapshot['status']}`。",
        f"- 高影响 Arkham 地址队列：{len(queue)} 个地址；本地已知 CEX "
        f"{label_status.get('confirmed_from_local_metadata', 0)}，API/网页已复核 "
        f"{reviewed}（API {api_reviewed}，API CEX {api_cex}），确认 CEX "
        f"{confirmed_cex}，待复核 {label_status.get('pending_web_review', 0)}。",
        f"- 当前直接 CEX 边界：{len(direct_events)} 笔，覆盖 "
        f"{len(daily_flows)} 个币种—日期；因 transfers 未全量，均为阶段性下界。",
        "",
        "## Bubblemaps逐币覆盖",
        "",
        "| 币种 | Holder | 边 | 成员历史 | 普通成员 | 覆盖率 | 唯一转账 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in snapshot["tokens"]:
        symbol = Path(row["token_file"]).parent.name
        holder_path = ROOT / "bubblemaps-snapshot/clean" / symbol / "holders.json"
        edge_path = ROOT / "bubblemaps-snapshot/clean" / symbol / "relationships.json"
        holders = len(json.loads(holder_path.read_text(encoding="utf-8")))
        edges = len(json.loads(edge_path.read_text(encoding="utf-8")))
        have = int(row["available_member_count"])
        members = int(row["ordinary_member_count"])
        lines.append(
            f"| {symbol} | {holders} | {edges} | {have} | {members} | "
            f"{have / members * 100:.2f}% | {int(row['unique_transfer_count'])} |"
        )
    lines.extend(
        [
            "",
            "## 口径",
            "",
            "- 日K来自币安官方合约连续K线接口，保存OHLC、成交量、成交额、成交笔数和主动买入量。",
            "- Holder、Subgraph与成员历史转账来自Bubblemaps官方网页使用的接口；采集器按服务端429的`retry_after`自动冷却并断点续跑。",
            "- Arkham只用于地址身份与实体标签。`reviewed_web_unlabeled`与`reviewed_arkham_api_unlabeled`表示对应来源未返回实体标签，不等于确定非CEX。",
            "- CEX净流为`转入CEX - 从CEX转出`；直接边按交易哈希、方向、金额去重。多跳只有在保存最终CEX边界交易哈希后才可纳入，路径中间跳不计金额。",
            "- transfers覆盖未达到100%前，事件数量和CEX流量只能作为已观测下界，不得用于正式IC或回测结论。",
            "",
            "## 主要产物",
            "",
            "- `klines-1d/`：20币日K CSV与manifest。",
            "- `bubblemaps-snapshot/`：Holder、关系边、Cluster与逐成员历史转账。",
            "- `arkham-review/all-transfer-addresses.csv`：所有已观测转账地址。",
            "- `arkham-review/high-impact-path-seeds.csv`：Cluster余额0.1%以上的路径种子。",
            "- `arkham-review/arkham-label-queue.csv`：可审计Arkham标签队列。",
            "- `arkham-review/arkham-api-review-state.json`：脱敏的Arkham API逐地址查询审计状态。",
            "- `cex-flow/direct-cex-events.csv`：直接CEX边界事件。",
            "- `cex-flow/daily-cex-net-flows.csv`：逐日CEX流入、流出、净流。",
        ]
    )
    (ROOT / "DATA_PIPELINE_STATUS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote status: transfers {available}/{total}, labels {len(queue)}")


if __name__ == "__main__":
    main()
