# Polymarket 市场采集

该采集器从 Polymarket 公共 Gamma API 获取活跃且未关闭的市场，按配置标签
形成全局候选池，并依次选择最多 30 个不同市场：

1. `liquidity` 前 10；
2. 排除已选市场后，`dominant_probability` 前 10；
3. 再排除已选市场后，`volume24hr` 前 10。

`dominant_probability` 是 `outcomePrices` 中的最大值。同一事件的不同市场
可以同时入选，但同一 `market_id` 不会重复入选。

## 分类

| 分类 | Tag ID |
| --- | --- |
| politics | 2 |
| geopolitics | 100265 |
| economy | 100328 |
| finance | 120 |
| technology | 105582、1401、22 |
| crypto | 21 |

crypto 市场还必须在 `description` 中命中配置关键词。匹配不区分大小写，
不会检查 `question` 或事件标题。市场同时命中其他分类时，crypto 未通过只会
移除 crypto 归属，不会移除其他归属。

## 运行

```bash
.venv/bin/python -m getMarket.Polymarket.tool.export_polymarket_market
```

指定日期和分页大小：

```bash
.venv/bin/python -m getMarket.Polymarket.tool.export_polymarket_market \
  --business-date 2026-07-28 \
  --page-limit 20
```

可用参数：`--output-root`、`--business-date`、`--timeout`、
`--max-attempts`、`--retry-delay`、`--page-limit`。业务日期默认使用
`Asia/Shanghai`。项目不创建 cron 或其他系统定时任务。

## 产物

每次运行创建独立目录 `market/YYYY-MM-DD_HHMMSS_<随机后缀>/`：

- `raw/tag-*/page-*.json`：收到一页就立即写入的原始 API 响应；
- `clean.json`：分类过滤和指标规范化后的全部候选市场；
- `final.json`：按三级优先级选出的市场；
- `error.json`：本次运行失败时的脱敏错误信息。

任一 Tag 未完整采集时不会写出 `final.json`。每次运行使用不同目录，因此失败
运行不会覆盖先前结果，也不需要发布锁、备份或回滚流程。逐页写 raw 避免把
完整 API 响应留在内存；每个 JSON 文件都先写临时文件，再原子替换为目标文件。

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
