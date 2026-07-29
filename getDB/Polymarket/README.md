# Polymarket 数据库导出

该工具只读取 PostgreSQL 中已经保存的 Polymarket 选择结果，不访问 Gamma API，
不重新分类、排名或去重。日期按 `created_at` 的 `Asia/Shanghai` 自然日计算，SQL
使用对应的 UTC 半开区间。

## 运行

```bash
.venv/bin/python -m getDB.Polymarket.tool.export_polymarket_db \
  --date 2026-07-29
```

试运行必须指定独立输出目录：

```bash
.venv/bin/python -m getDB.Polymarket.tool.export_polymarket_db \
  --date 2026-07-29 \
  --output-root /tmp/dbcompare-polymarket-db-smoke
```

`--date` 默认使用上海当天。数据库连接读取 `.env` 中的 `PGHOST`、`PGPORT`、
`PGDATABASE`、`PGUSER` 和 `PGPASSWORD`。

## 产物

```text
getDB/Polymarket/db/YYYY-MM-DD/
|-- .generation.lock
|-- polymarket_db.json
|-- errors.json
`-- manifest.json
```

`polymarket_db.json` 的顶层是 `{"records": [...]}`。每个合法数据库行保留为
一条独立记录；相同市场出现在多个大类、快照或批次时不会合并。排序依次使用
大类、`rank`、`created_at` 和数据库 `id`。

manifest 状态和退出码：

| 状态 | 含义 | 退出码 |
| --- | --- | --- |
| `success` | 至少一条合法记录且无异常 | 0 |
| `partial` | 有合法记录，但部分行被隔离到 `errors.json` | 1 |
| `failed` | 查询失败、零记录或全部记录非法 | 1 |

`manifest.json` 是最终提交标记，并包含两个 JSON 产物的 SHA-256。消费者应使用
`read_validated_generation()` 在共享锁内读取，并在使用业务记录前检查 `status`。

## 测试

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_*.py -q
```
