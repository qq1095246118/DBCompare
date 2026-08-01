# Polymarket 市场采集

该采集器从 Polymarket Gamma `/markets/keyset` 获取活跃且未关闭的市场。大类
完全由 Polymarket 返回市场的官方 Tag 流决定，不根据题目、标题、描述或市场
含义重新分类。每个请求都带 `include_tag=true`，并完整翻页后再排行。

## 分类和加密过滤

| 分类 | Tag ID |
| --- | ---: |
| politics | 2 |
| geopolitics | 100265 |
| economy | 100328 |
| finance | 120 |
| technology | 1401 |
| crypto | 21 |

只有 crypto 需要二次过滤。过滤只精确比较官方 `market.tags[].slug`，不读取
description、question、title 或 Tag label：

- `regulation`：`crypto-policy`、`crypto-legal`、`regulation`、`regulations`、
  `sec`、`cftc`、`legal`、`legal-proceedings`、`ban`；
- `etf`：`etf`、`etfs`、`etf-approval`；
- `exchange_risk`：必须同时有 `exchange|exchanges` 之一和
  `bankruptcy|insolvency|hack|hacking|exploit|exploits|cybersecurity|data-breach`
  之一；
- `stablecoin`：`stablecoins`、`tether`、`usdt`、`usdc`、`depeg`；
- `protocol_security`：`protocol-risk`、`protocol-upgrade`、`hack`、`hacking`、
  `hacker`、`exploit`、`exploits`、`cybersecurity`、`data-breach`、`bybit-hack`。

一个 crypto 市场可以记录多个主题，但在 crypto 大类内仍是一个候选。缺失或
格式错误的官方 Tags 会使整次运行失败；合法但没有命中主题的 crypto 市场只会
被正常过滤掉。

## 排行和去重

每个大类固定生成三个独立排行：

1. `liquidity`，优先级 1，最多 10 条；
2. `dominant_probability`，优先级 2，最多 10 条；
3. `volume24hr`，优先级 3，最多 10 条。

低优先级排行先排除高优先级已选 `market_id`，再从完整排序结果中取最多 10
条。因此发生重复时会继续使用原始第 11 名及后续候选补位；候选不足时按实际
数量成功输出。每个排行的 `rank` 都从 1 开始。同一大类内不重复，不同大类之间
不去重。单类最多 30 条，六类最多 180 条。

## 运行

```bash
.venv/bin/python -m getMarket.Polymarket.tool.export_polymarket_market \
  --business-date 2026-07-31 \
  --page-limit 20 \
  --timeout 20 \
  --max-attempts 3 \
  --retry-delay 0.25
```

可用参数：`--output-root`、`--business-date`、`--timeout`、
`--max-attempts`、`--retry-delay`、`--page-limit`。业务日期默认使用
`Asia/Shanghai`。`--page-limit` 只控制每个 API 请求的页大小，允许范围是
1–20；它不限制总采集量或最终排行数量。项目不创建 cron 或其他系统定时任务。

## 产物

每次运行创建独立目录 `market/YYYY-MM-DD_HHMMSS_<随机后缀>/`：

- `raw/tag-*/page-*.json`：每页收到后立即原子写入的原始响应；
- `clean.json`：完整候选集合，包含大类归属、规范化指标、官方 market Tags、
  crypto 主题和精确命中 slug 证据；候选按 `(category, market_id)` 独立，
  同一 market ID 属于多个大类时会保留多行及各自 source；
- `final.json`：顶层为 `{"records": [...]}`，按大类、排行优先级和榜内名次
  排序；
- `error.json`：失败运行的脱敏错误，本次不会同时写出 clean/final。

`final.json` 每条记录有 14 个外层字段。非 crypto `content` 有 19 个字段，
crypto 另有 `crypto_topics`，`extra_data` 有 28 个字段。
`ranking_metric`、`ranking_priority` 和 `rank` 在 content 与 extra_data 中值完全
相同。API 无法可靠提供的 DB 值使用 JSON `null`；这只保证最终文件结构对齐，
不是 DB generation，也不读取 PostgreSQL。

直接查看最终问题和排行：

```bash
jq -r '.records[] | [.content.category, .content.ranking_metric,
  (.content.rank|tostring), .content.market_question] | @tsv' \
  getMarket/Polymarket/market/<运行目录>/final.json
```

直接查看候选分类、排序后的官方 Tag slug 和问题：

```bash
jq -r '.[] | [.categories|join(","),
  (.source.tags|map(.slug)|sort|join(",")), .source.question] | @tsv' \
  getMarket/Polymarket/market/<运行目录>/clean.json
```

任一 Tag 未完整采集、响应契约无效或处理失败时，不会写出 `clean.json` 和
`final.json`。已经写入的 raw 页面会保留，且失败运行不会覆盖之前的成功目录。

## 测试

离线测试：

```bash
.venv/bin/python -m pytest -m "not live_bubblemaps and not live_polymarket" -q
```

只读在线契约测试：

```bash
.venv/bin/python -m pytest tests/test_polymarket_live_smoke.py \
  -m live_polymarket -q
```
