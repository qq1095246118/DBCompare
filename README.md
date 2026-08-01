# DBCompare 全局命令使用指南

本文档按业务区分项目命令。所有命令默认在项目根目录执行：

```bash
cd /Users/wrh/Downloads/DBCompare
```

项目使用 `.venv` 运行 Python。不要使用当前已损坏的 `venv` 目录。

## 0. 环境准备

首次创建虚拟环境并安装项目及测试依赖：

```bash
PYENV_VERSION=3.12.0 python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

复制数据库环境变量模板：

```bash
cp .env.example .env
```

编辑 `.env`，至少设置：

```text
PGHOST=
PGPORT=15432
PGDATABASE=
PGUSER=
PGPASSWORD=
```

不要把真实密码写入命令行、代码或提交到版本库。

## 1. getDB：数据库日快照

业务用途：从 PostgreSQL 的 `bubblemaps` 相关表读取指定日期数据，生成数据库
侧的 holders、clusters 和 transfer 汇总快照。

正式采集指定业务日期：

```bash
.venv/bin/python -m getDB.bubblemaps.tool.export_bubblemaps_db \
  --date 2026-07-28
```

指定独立输出目录进行排查：

```bash
.venv/bin/python -m getDB.bubblemaps.tool.export_bubblemaps_db \
  --date 2026-07-28 \
  --output-root /tmp/dbcompare-db-smoke
```

参数：

- `--date YYYY-MM-DD`：业务日期，默认使用 Asia/Shanghai 当天。
- `--output-root PATH`：输出根目录；排查和试运行必须指定独立目录。

默认输出目录：

```text
getDB/bubblemaps/db/YYYY-MM-DD/
```

## 2. getMarket/Bubblemaps：代币关系采集

业务用途：从 PostgreSQL 读取活跃 token 目标，再调用 Bubblemaps API 获取：

- top holders；
- subgraph grouped transfers；
- cluster 普通成员的 transfers。

### 2.1 单目标排查

只取数据库结果中的一个目标：

```bash
.venv/bin/python -m getMarket.bubblemaps.tool.export_bubblemaps_market \
  --limit 1 \
  --output-root /tmp/dbcompare-market-one-token
```

指定单个目标时，`--chain` 和 `--token-address` 必须同时提供：

```bash
.venv/bin/python -m getMarket.bubblemaps.tool.export_bubblemaps_market \
  --chain bsc \
  --token-address 0x1111111111111111111111111111111111111111 \
  --output-root /tmp/dbcompare-market-one-token
```

按币种筛选目标，并覆盖所有支持链：

```bash
.venv/bin/python -m getMarket.bubblemaps.tool.export_bubblemaps_market \
  --symbols M,BEAT,B,DEXE \
  --output-root /tmp/dbcompare-market-symbols
```

### 2.2 正式全量采集

只有确认数据库和 API 配置无误后才使用默认输出目录：

```bash
.venv/bin/python -m getMarket.bubblemaps.tool.export_bubblemaps_market
```

常用 API 参数：

```bash
.venv/bin/python -m getMarket.bubblemaps.tool.export_bubblemaps_market \
  --api-timeout 20 \
  --api-max-attempts 3 \
  --api-retry-delay 0.25 \
  --api-min-interval 2.1
```

排查命令不要使用正式默认目录。Bubblemaps 输出位于：

```text
getMarket/bubblemaps/market/YYYY-MM-DD/
```

## 3. getMarket/Polymarket：预测市场采集

Polymarket API 采集固定读取六个官方分类 Tag 流，完整翻页，并只对 crypto 使用
官方 `market.tags[].slug` 白名单规则。每个大类分别输出 `liquidity`、
`dominant_probability`、`volume24hr` 三个最多 10 条的排行；按优先级在类内去重并
向后补位，不跨大类去重，因此最终最多 180 条分类记录。

六个大类依次为 politics、geopolitics、economy、finance、technology、crypto，
对应 Tag ID 为 2、100265、100328、120、1401、21。分类归属只由返回市场的官方
Tag 流决定，不从题目、标题或描述推断。

### 3.1 正式运行

```bash
.venv/bin/python -m getMarket.Polymarket.tool.export_polymarket_market
```

完整运行命令：

```bash
.venv/bin/python -m getMarket.Polymarket.tool.export_polymarket_market \
  --business-date 2026-07-31 \
  --page-limit 20 \
  --timeout 20 \
  --max-attempts 3 \
  --retry-delay 0.25
```

`--page-limit` 允许范围是 `1–20`，只控制 API 请求页大小，不限制完整翻页或固定
排行数量。

其它参数：

- `--output-root PATH`：指定输出根目录，适合试运行。
- `--timeout SECONDS`：单次请求超时，默认 20。
- `--max-attempts N`：请求最大尝试次数，默认 3。
- `--retry-delay SECONDS`：重试间隔，默认 0.25。
- `--business-date YYYY-MM-DD`：目录日期前缀。

每次运行都会创建新目录，不覆盖历史运行：

```text
getMarket/Polymarket/market/YYYY-MM-DD_HHMMSS_<random>/
├── raw/tag-*/page-*.json
├── clean.json
├── final.json
└── error.json                 # 失败运行才有
```

`raw` 页面会边采集边写入；`final.json` 不存在表示本次没有完整成功。
`clean.json` 保留完整分类候选和官方 Tag 证据；相同 market ID 属于多个大类时
保留多行及各自 source。`final.json` 顶层为 `{"records": [...]}`，每条记录有
14 个外层字段，非 crypto `content` 有 19 个字段，crypto 有 20 个字段，
`extra_data` 有 28 个字段。API 无法可靠提供的 DB 值为 JSON `null`；这只保证
结构对齐，不读取 PostgreSQL，也不是 DB generation。失败运行只写脱敏
`error.json`，不会同时写出 clean/final。

### 3.2 Polymarket 只读在线检查

只验证当前公开 API 和配置 Tag 的响应结构，不生成业务产物：

```bash
.venv/bin/python -m pytest \
  tests/test_polymarket_live_smoke.py \
  -m live_polymarket -q
```

## 4. 测试与验证

运行全部离线测试：

```bash
.venv/bin/python -m pytest \
  -m "not live_bubblemaps and not live_polymarket" -q
```

只运行某个业务的测试：

```bash
.venv/bin/python -m pytest tests/test_polymarket_*.py -q
.venv/bin/python -m pytest tests/test_market_*.py tests/test_transfer_transform.py -q
.venv/bin/python -m pytest tests/test_db_source.py tests/test_contract.py -q
```

编译检查：

```bash
.venv/bin/python -m compileall -q common getDB getMarket
```

查看任意 CLI 的参数帮助：

```bash
.venv/bin/python -m getDB.bubblemaps.tool.export_bubblemaps_db --help
.venv/bin/python -m getMarket.bubblemaps.tool.export_bubblemaps_market --help
.venv/bin/python -m getMarket.Polymarket.tool.export_polymarket_market --help
```

## 5. 常见操作边界

### 试运行

始终使用 `/tmp` 或其它独立目录：

```bash
--output-root /tmp/dbcompare-smoke
```

### 正式运行

只有需要更新正式业务目录时才省略 `--output-root`。Bubblemaps 和数据库采集会
读取数据库；Polymarket 采集会访问公网 Gamma API。

### 失败排查

先查看对应运行目录中的 `error.json`，再检查：

1. `.env` 中数据库连接参数是否正确；
2. 网络是否能访问对应 API；
3. 请求是否被限流或超时；
4. 是否误用了正式输出目录进行试运行。

不要删除已有正式数据来“重试”。为排查创建新的 `--output-root`，确认原因后再
重新执行正式命令。
