# Bubblemaps 市场数据提取器

`getMarket` 从 PostgreSQL 读取目标 token（源表为 `binance_address_metadata`），
再通过 Bubblemaps API 获取当前的 holders、subgraph 和 Cluster 普通成员 transfers。
目标查询只选择支持链中 `is_active = 1` 且 `token_address` 非空白的行；PostgreSQL
只决定本次采集哪些 `chain + token_address`。holder、关系、Cluster 和 transfer 数据
均来自 Bubblemaps API，不从历史 `getDB` 文件读取。

当前入口不使用浏览器自动化。API 客户端会按公开的动态 validation flow 获取
请求所需材料，不要求手工提供 token，也不持久化请求或响应 header、validation
值、数据库密码等秘密。

## 环境准备

```bash
cd /Users/wrh/Downloads/DBCompare
cp .env.example .env
PYENV_VERSION=3.12.0 python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

在 `.env` 或进程环境中设置 `PGHOST`、`PGPORT`、`PGDATABASE`、`PGUSER` 和
`PGPASSWORD`。数据库查询在只读、可重复读事务中执行。

## 运行

使用 `Asia/Shanghai` 的运行当天作为输出日期。以下是单目标排查命令，必须使用
独立输出根目录：

```bash
.venv/bin/python -m getMarket.bubblemaps.tool.export_bubblemaps_market \
  --limit 1 \
  --output-root /tmp/bubblemaps-one-token-smoke
```

`--limit` 按规范化后的 `chain + token_address` 确定性顺序截取目标。也可将
`--chain` 和 `--token-address` 成对提供，以选择数据库查询结果中已经存在的
单一目标。任何 `--limit` 或 `--chain`/`--token-address` 排查均不得使用正式的
默认输出根目录。`--output-root` 可指定独立输出根目录；API 超时和有限重试可通过
`--api-timeout`、`--api-max-attempts`、`--api-retry-delay` 调整；默认通过
`--api-min-interval 2.1` 对正式 API 请求节流。

正式全量采集才使用默认输出根目录：

```bash
.venv/bin/python -m getMarket.bubblemaps.tool.export_bubblemaps_market
```

只采集指定币种及其所有支持链时，使用逗号分隔的 `--symbols`：

```bash
.venv/bin/python -m getMarket.bubblemaps.tool.export_bubblemaps_market \
  --symbols M,BEAT,B,DEXE
```

同日补采必须以完整命令重新运行，并原子替换该日期的完整 generation；不能将
单个 token 的结果合并进既有日期目录。排查单个 token 时，必须提供独立的
`--output-root`，避免替换正式的当日完整输出。

## 数据处理

每个目标依次执行以下步骤：

1. 获取 top holders，只保留具有 rank 的规范化 holder；
2. 获取 subgraph，逐行限制为同一 token 且两端均为 ranked holder 的关系；
3. 将关系作为无向边重建 Cluster；孤立 holder 不生成单成员 Cluster；
4. 为每个非 Supernode 的普通 Cluster 成员获取 transfers，只保留 exact
   chain + token 且该成员位于 `from_address` 或 `to_address` 任一端的正式
   `TRANSFER` 记录。另一端可以是 external、unranked 或其他 Cluster 地址；
   不会为外部对端生成 member 文档；
5. 从 clean 文件重新读取并生成最终 token 文档，验证 manifest、引用和哈希后
   原子发布。

仅当 top holders 请求返回 HTTP 400，且 JSON 响应体为对象并且 `detail` 字段精确
等于 `Top holders not available for this token.` 时，目标才会记录为 manifest 的
`skipped_tokens` 条目；其他无关响应字段不影响该匹配。随后不会再对该目标发请求，
亦不会产生 raw、clean 或 data 产物。

其他 holders 或 subgraph 请求及清洗失败会写入根目录 `error.json`，将当前目标以
`capture_failed` 记录到 `skipped_tokens`，然后继续下一个目标。普通 Cluster 成员的
transfers 请求或清洗失败同样写入 `error.json`，但保留 Token、Cluster 和成员；该
成员标记为 `transfer_details_available: false`、`transfer_details_reason:
"capture_failed"`，且不生成成员 transfer 文件。数据库目标读取、文件写入、产物
校验和发布错误仍会终止整批运行。

Supernode 参与 Cluster、持仓聚合和排序，但不获取成员 transfer。没有 Cluster
或没有普通成员 transfer 是合法结果；此时 raw、clean、manifest 与最终 token
文档中的集合必须保持一致。

## 成功产物

成功 generation 位于：

```text
getMarket/bubblemaps/market/YYYY-MM-DD/
  targets.json
  manifest.json
  error.json                          # 仅部分失败时存在
  raw/<safe-chain>/<safe-token>/
    holders.json
    subgraph.json
    transfers/<safe-member>.json
  clean/<safe-chain>/<safe-token>/
    holders.json
    relationships.json
    transfers/<safe-member>.json
  data/<safe-chain>/<safe-token>/
    token.json
```

raw 文件包含非空的官方请求 provenance；clean 文件是经过目标绑定和过滤后的
中间契约。最终 `token.json` 保存 token 身份、Cluster 和成员摘要，普通成员只
通过根目录相对的 `transfer_file` 引用 `clean/<safe-chain>/<safe-token>/
transfers/<safe-member>.json`，不内嵌完整 transfer 数组。`data` 层只保存
`token.json`。`manifest.json` 的 `source` 为 `bubblemaps_api`，并记录所有非
manifest 产物的 SHA-256。没有跳过或采集错误时 manifest 的 `status` 为 `success`；
存在 `skipped_tokens` 或 `error.json` 时为 `partial_success`，其 `tokens` 保留所有
能够完成 Token 级采集的目标。

通过校验的 `success` 或 `partial_success` generation 才会成为日期目录。失败运行
保存在 `_failed/YYYY-MM-DD/<generation-id>/`，不会覆盖同日已发布输出。

## 测试

默认测试是离线的，不访问 PostgreSQL 或 Bubblemaps：

```bash
.venv/bin/python -m pytest -q
```

真实 smoke test 还要求完整 PostgreSQL 环境及两个显式 opt-in：
`BUBBLEMAPS_LIVE_SMOKE=1` 和
`BUBBLEMAPS_LIVE_API_CONFIRM=READ_ONLY_ONE_TARGET`。执行方式如下：

```bash
(
  set -a
  source .env
  set +a
  BUBBLEMAPS_LIVE_SMOKE=1 \
  BUBBLEMAPS_LIVE_API_CONFIRM=READ_ONLY_ONE_TARGET \
  ./.venv/bin/python -m pytest -q -m live_bubblemaps tests/test_market_live_smoke.py
)
```

该测试始终限制为一个目标，并使用 pytest 的临时输出目录。它会访问外部数据库和
Bubblemaps API，不属于默认测试。只有 live 测试报告为 passed 才算成功验证；
报告 skipped 表示凭据或 opt-in 未被应用，不能视为成功的 live 验证。
