# Polymarket 市场采集

该采集器从 Polymarket 公共 Gamma API 获取活跃且未关闭的市场。分类完全依据
Polymarket Tag 归属，不根据题目、标题或描述重新判断。默认每个大类独立选择
最多 20 条记录，可通过 `--per-category` 调整。三级指标按顺序补足同一个大类
限额：

1. 先按 `liquidity` 降序填充；
2. 未达到限额时，从尚未入选的市场中按 `dominant_probability` 降序补足；
3. 仍未达到限额时，再按 `volume24hr` 降序补足。

`dominant_probability` 是 `outcomePrices` 中的最大有效值。指标相同时按
`market_id` 升序。同一市场在一个大类中最多出现一次；若 Polymarket 将它放入
多个配置大类，它会在每个符合的大类中各保留一条。默认配置下，`final.json`
最多包含 120 条分类记录；使用 `--per-category N` 时最多为 `6 * N` 条，但全局
不同的 `market_id` 数量可能少于分类记录数。

## 分类

| 分类 | Tag ID |
| --- | --- |
| politics | 2 |
| geopolitics | 100265 |
| economy | 100328 |
| finance | 120 |
| technology | 105582、1401、22 |
| crypto | 21 |

technology 的三个 Tag 合并为一个候选池，并在该大类内按 `market_id` 去重。

crypto 市场还必须在 `description` 中命中配置关键词。匹配不区分大小写，
不会检查 `question` 或事件标题。市场同时命中其他分类时，crypto 未通过只会
移除 crypto 归属，不会移除其他归属。

## 运行

```bash
.venv/bin/python -m getMarket.Polymarket.tool.export_polymarket_market
```

指定日期、每类数量和分页大小：

```bash
.venv/bin/python -m getMarket.Polymarket.tool.export_polymarket_market \
  --business-date 2026-07-28 \
  --per-category 10 \
  --page-limit 20
```

可用参数：`--output-root`、`--business-date`、`--timeout`、
`--max-attempts`、`--retry-delay`、`--per-category`、`--page-limit`。业务日期
默认使用 `Asia/Shanghai`。项目不创建 cron 或其他系统定时任务。

`--per-category` 控制每个大类写入 `final.json` 的最大条数，默认 20，接受任意
正整数。它不会减少 Gamma API 翻页，也不会截断 `clean.json`。`--page-limit`
只控制每页请求数量，允许范围仍为 1–20。

## 产物

每次运行创建独立目录 `market/YYYY-MM-DD_HHMMSS_<随机后缀>/`：

- `raw/tag-*/page-*.json`：收到一页就立即写入的原始 API 响应；
- `clean.json`：按 `market_id` 全局唯一的候选市场，保留全部分类归属、匹配
  Tag、规范化指标和压缩后的 API `source`；
- `final.json`：顶层为 `{"records": [...]}`，每条记录使用与 DB 处理结果
  相同的 14 个外层字段、17 个 `content` 字段和 8 个 `extra_data` 字段；
- `error.json`：本次运行失败时的脱敏错误信息。

`final.json` 只保证结构与 DB 处理结果对齐，不保证字段值相同，也不是有效的 DB
generation。API 无法可靠提供的字段使用 `null`，包括数据库 `id`、
`content_hash` 和窗口字段，因此不能交给 DB 侧严格记录校验器。
`selected_category` 和 `rank_in_category` 分别映射为 `content.category` 和
`content.rank`；`selected_by` 与 `priority` 不再写入最终产物。需要 API 候选细节时
读取结构保持不变的 `clean.json`。
这是破坏性格式变更；原先直接遍历顶层数组的调用方必须改为读取
`payload["records"]`。

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
