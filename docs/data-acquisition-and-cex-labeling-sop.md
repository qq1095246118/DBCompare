# Bubblemaps 数据获取与 CEX 地址标签 SOP

> 版本：1.0
> 更新日期：2026-08-06
> 适用项目：`/Users/rayer/Documents/DBCompare`
> 目的：供独立数据 Agent 执行 Bubblemaps 原始采集、PostgreSQL 数据复用、Arkham API 标签复核和 Arkham 网页兜底复核。

## 1. 交付目标

一个完整数据批次需要交付：

1. 每个币的 Holder、关系边、Cluster 与普通 Cluster 成员；
2. 每个普通 Cluster 成员的历史 Transfer；
3. 高影响 Transfer 的 `from/to` 地址清单；
4. 地址实体、用途标签以及 `is_cex` 分类；
5. 去重后的直接和多跳 CEX 边界事件；
6. 每日 CEX 净流入；
7. 可追溯的 manifest、状态报告和错误记录。

完成分为两个等级：

- **阶段完成**：允许个别币、成员 Transfer 或地址标签未完成，但必须写明 `partial`，CEX Flow 只能解释为已观测下界。
- **正式完成**：结构、普通成员 Transfer、高影响地址标签和质量门禁全部通过。

## 2. 数据源优先级

必须按以下顺序读取，只有上一层缺失或校验失败才允许回退：

| 数据 | 第一优先 | 第二优先 | 最后回退 |
|---|---|---|---|
| Bubblemaps Holder／关系／Cluster | PostgreSQL 最新精确快照 | 已校验本地快照 | Bubblemaps 原始 API |
| Cluster 成员 Transfer | PostgreSQL member view | 已校验本地成员文件 | Bubblemaps 原始 API |
| 地址实体和用途标签 | PostgreSQL 地址标签快照 | Arkham 官方只读 API | 已登录 Arkham 网页只读复核 |

同一批次允许不同币使用不同上游来源，但每个币必须记录实际来源。禁止把 PG、旧本地文件和新 API 数据混合后统一标为“API”或“数据库”。

## 3. 安全与数据边界

### 3.1 凭证

- PostgreSQL 凭证只能由 `Factor_Factory` 的数据库配置加载，不进入命令行或文档。
- Arkham API Key 只能从仓库根目录 `.env` 的 `Arkm_API_KEY` 或 `ARKHAM_API_KEY` 读取。
- API Key 只进入 HTTP `API-Key` 请求头，禁止写入日志、CSV、JSON、报告或命令行。
- 不保存或共享浏览器 Cookie、OTP、access token、登录会话或密码。
- Bubblemaps 客户端只使用仓库已有的只读采集器；不得硬编码、导出或持久化前端校验材料。

### 3.2 请求和网站限制

- Bubblemaps、Arkham API 和 Arkham 网页均为只读。
- HTTP 401/403 立即停止并报告。
- HTTP 429 必须遵守 `Retry-After` 和退避，不得提高并发绕过。
- 网页出现 CAPTCHA、登录失效或安全限制时立即停止，不得规避。
- 失败页面不能标成“无标签”。

### 3.3 地址与事件主键

- Token、快照和 Transfer 查询必须使用精确 `chain + token_address`，禁止只按 symbol 查询。
- 地址标签按 `chain + canonical address` 全局去重。
- EVM 地址小写；Solana Base58、TON 等大小写敏感地址保留链原生形式。
- 新增待复核地址只能来自高影响 Transfer 的 `from_address` 和 `to_address`。
- 多跳只计首次到达的 CEX 边界；直接边与多跳边不得重复计量。

## 4. 环境准备

从项目根目录执行。以下变量仅作当前项目示例，创建新批次时应更换 `RUN_ROOT` 和 `CONFIG`：

```bash
cd /Users/rayer/Documents/DBCompare

PROJECT_ROOT=/Users/rayer/Documents/DBCompare
RUN_ROOT=/Users/rayer/Documents/DBCompare/analysis/binance-bubblemaps-expanded-universe-2026-08-03
PYTHON=/Users/rayer/Documents/DBCompare/.venv/bin/python
FACTOR_FACTORY=/Users/rayer/Documents/Factor_Factory
CONFIG="$RUN_ROOT/expanded_universe_config.json"
SNAPSHOT_ROOT="$RUN_ROOT/bubblemaps-snapshot"
REVIEW_ROOT="$RUN_ROOT/arkham-review"
WINDOW_START=2025-01-01
WINDOW_END=2026-08-03
```

配置文件最低要求：

```json
{
  "symbols": {
    "TOKEN": {
      "targets": {
        "bsc": ["0x..."]
      }
    }
  }
}
```

每个 symbol 在一个批次内必须对应一个明确的 `chain + token_address`。多链 Token 应拆成独立目标或在批次设计中显式说明，不得让采集器猜链。

运行前校验：

```bash
test -x "$PYTHON"
test -f "$CONFIG"
test -f "$PROJECT_ROOT/.env"

"$PYTHON" -m py_compile \
  "$RUN_ROOT/export_pg_bubblemaps_snapshots.py" \
  "$RUN_ROOT/import_pg_transfers.py" \
  "$RUN_ROOT/capture_bubblemaps_structures.py" \
  "$RUN_ROOT/capture_bubblemaps_transfers.py" \
  "$RUN_ROOT/build_address_inventory.py" \
  "$RUN_ROOT/build_arkham_label_queue.py" \
  "$RUN_ROOT/batch_review_pg_labels.py" \
  "$RUN_ROOT/batch_review_arkham_api.py" \
  "$RUN_ROOT/update_arkham_label.py" \
  "$RUN_ROOT/web_review_cooldown.py" \
  "$RUN_ROOT/compute_cex_net_flows.py" \
  "$RUN_ROOT/build_data_status.py"
```

## 5. 批次初始化

### 5.1 冻结输入

记录：

- 批次目录；
- 配置文件路径及 SHA-256；
- 币种数；
- 研究开始和结束 UTC；
- 各币 `chain + token_address`；
- 运行开始 UTC。

新批次应创建新日期目录，不覆盖旧批次。已有标签队列必须先备份：

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

if test -f "$REVIEW_ROOT/arkham-label-queue.csv"; then
  cp "$REVIEW_ROOT/arkham-label-queue.csv" \
    "$REVIEW_ROOT/arkham-label-queue.$STAMP.bak.csv"
fi

if test -f "$REVIEW_ROOT/arkham-api-review-state.json"; then
  cp "$REVIEW_ROOT/arkham-api-review-state.json" \
    "$REVIEW_ROOT/arkham-api-review-state.$STAMP.bak.json"
fi

if test -f "$REVIEW_ROOT/web-review-attempts.json"; then
  cp "$REVIEW_ROOT/web-review-attempts.json" \
    "$REVIEW_ROOT/web-review-attempts.$STAMP.bak.json"
fi
```

缺少源文件时跳过对应备份，不能创建空文件冒充历史状态。

## 6. 方法A：从 PostgreSQL 获取 Bubblemaps 数据

PG 是结构与 Transfer 的第一优先来源。连接通过 `Factor_Factory` 的 `build_postgresql_engine()` 建立，当前项目脚本使用隔离 helper，把查询结果通过标准输入／输出传递，不暴露数据库凭证。

### 6.1 PG 表和查询口径

结构数据：

| 表 | 内容 |
|---|---|
| `public.bubblemaps_membership_snapshot` | 每个 Token 的采集批次、状态和总体计数 |
| `public.bubblemaps_token_holder` | Holder 原始记录 |
| `public.bubblemaps_holder_relationship_snapshot` | Holder 关系边原始记录 |

Transfer 数据：

| 表／视图 | 内容 |
|---|---|
| `public.bubblemaps_transfer_member_view` | Token Cluster 成员与 Transfer event 的关联 |
| `public.token_transfer_event` | tx hash、from、to、amount 和事件时间 |

地址标签：

| 表 | 内容 |
|---|---|
| `public.address_entity_label_snapshot` | `chain + address` 的实体、用途和 `is_cex` 快照 |

结构查询规则：

1. 按 `chain + lower(token_address)` 精确匹配；
2. 先读取最新一条 membership snapshot；
3. 最新状态必须为 `success`；
4. 不允许因为最新批次失败而静默回退到更老成功批次；
5. 再用同一 `batch_id` 读取 Holder 和 Relationship；
6. Holder 或 Relationship 为空时记为 `pg_snapshot_incomplete`。

Transfer 查询规则：

1. 使用已经构建出的普通 Cluster 成员集合；
2. 按 `chain + token_address + member_address` 过滤；
3. 使用 UTC 半开区间 `[start_at, end_exclusive)`；
4. `DISTINCT ON (member_address, event_id)` 取最新关联；
5. 与现有本地/API Transfer 按 tx、端点、金额、时间幂等合并。

### 6.2 导出 PG Holder、关系和 Cluster

```bash
"$PYTHON" "$RUN_ROOT/export_pg_bubblemaps_snapshots.py" \
  --config "$CONFIG" \
  --snapshot-root "$SNAPSHOT_ROOT" \
  --factor-factory-root "$FACTOR_FACTORY" \
  --report "$RUN_ROOT/pg-structure-export-report.json"
```

正常状态包括：

- `exported_from_pg`：从最新成功 PG 批次导出；
- `existing_local_reused`：已有结构通过基本校验，未覆盖；
- `missing_in_pg`：PG 无目标；
- `latest_pg_snapshot_failed`：最新 PG 批次失败；
- `pg_snapshot_incomplete`：批次缺 Holder 或关系边。

默认不覆盖有效本地结构。只有完成来源审计并明确需要替换时才使用 `--overwrite`。

### 6.3 导入 PG Transfer

先确定 PG-ready 币种，再显式传入 `--symbols`：

```bash
"$PYTHON" "$RUN_ROOT/import_pg_transfers.py" \
  --config "$CONFIG" \
  --snapshot-root "$SNAPSHOT_ROOT" \
  --symbols 'TOKEN1,TOKEN2' \
  --start-date "$WINDOW_START" \
  --end-date "$WINDOW_END" \
  --factor-factory-root "$FACTOR_FACTORY" \
  --report "$RUN_ROOT/pg-transfer-import-report.json"
```

PG Transfer 输出必须保留：

```json
{
  "from_address": "...",
  "to_address": "...",
  "rel_type": "TRANSFER",
  "data": {
    "value": "123.45",
    "date": 1760000000000,
    "tx_hash": "...",
    "token_ref": {"chain": "bsc", "address": "0x..."},
    "event_id": "...",
    "source": "postgresql"
  }
}
```

导入完成后，从已验证文件重建 manifest，避免旧 manifest 漏掉新增 PG 目标：

```bash
"$PYTHON" "$RUN_ROOT/rebuild_snapshot_manifest.py" \
  --config "$CONFIG" \
  --snapshot-root "$SNAPSHOT_ROOT" \
  --preserve-unconfigured
```

## 7. 方法B：从 Bubblemaps 原始接口获取

仅对 PG 和有效本地快照缺失的目标使用此路径。仓库客户端位于：

- `getMarket/bubblemaps/tool/bubblemaps_api.py`
- `analysis/binance-bubblemaps-out-of-sample-2026-07-30/capture_bubblemaps.py`
- 当前批次包装器 `capture_bubblemaps_structures.py` 与 `capture_bubblemaps_transfers.py`

客户端从 Bubblemaps 官方地图前端发现当前只读 API 配置，然后通过统一客户端调用：

| 阶段 | HTTP | 逻辑端点 | 用途 |
|---|---|---|---|
| Top Holders | POST | `/addresses/token-top-holders?count=300&nocache=false` | 获取排名 Holder |
| 关系子图 | POST | `/relationships/subgraph` | 获取 Holder 间关系边 |
| 成员 Transfer | GET | `/relationships/transfers` | 获取单个普通 Cluster 成员历史 Transfer |

不得绕过官方前端配置发现、硬编码内部 host 或持久化前端校验材料。客户端无法发现当前配置、返回安全限制或目标链不受支持时，应标记失败并停止该目标，不得自行构造替代认证。

### 7.1 获取 Holder 和关系结构

```bash
"$PYTHON" "$RUN_ROOT/capture_bubblemaps_structures.py" \
  --config "$CONFIG" \
  --snapshot-root "$SNAPSHOT_ROOT" \
  --timeout 20 \
  --max-attempts 5 \
  --retry-delay 3 \
  --min-interval 2
```

执行逻辑：

1. 把配置中的链和 Token 地址规范化为目标；
2. 读取 Top 300 Holders；
3. 用 Holder 地址请求关系子图；
4. 清洗 Holder 和 Relationship；
5. 构建 Cluster；
6. 排除 supernode，得到普通 Cluster 成员；
7. 立即落盘结构文件。

结构输出：

```text
bubblemaps-snapshot/
├── clean/{SYMBOL}/holders.json
├── clean/{SYMBOL}/relationships.json
└── data/{SYMBOL}/token.json
```

已有合法 `holders.json` 或 `relationships.json` 时复用，避免重复请求。若只存在其中一个，补缺失部分并重新构建 `token.json`。

### 7.2 获取普通 Cluster 成员 Transfer

```bash
"$PYTHON" "$RUN_ROOT/capture_bubblemaps_transfers.py" \
  --config "$CONFIG" \
  --snapshot-root "$SNAPSHOT_ROOT" \
  --timeout 20 \
  --max-attempts 100 \
  --retry-delay 3 \
  --min-interval 1 \
  --checkpoint-every 20
```

调度规则：

- 按成员 Token 余额从大到小排列；
- 跨币 round-robin，避免一个大 Cluster 长时间阻塞全部币；
- 已存在且 `member_address` 与文件名匹配、`transfers` 为数组的文件直接复用；
- 每个成员单独保存，因此中断后可续跑；
- 每 20 个尝试更新 manifest；
- 429 按服务端退避，不增加并发绕过。

成员输出：

```text
bubblemaps-snapshot/clean/{SYMBOL}/transfers/{MEMBER_ADDRESS}.json
```

文件必须包含：

- requested/canonical chain；
- requested/canonical token address；
- `member_address`；
- `transfers` 数组；
- `transfer_count`。

manifest 至少包含：

- `ordinary_member_count`；
- `available_member_count`；
- `unique_transfer_count`；
- `transfer_error_count`；
- `status=success|partial_success`；
- 每个币的实际数据来源。

### 7.3 Bubblemaps 采集验收

每币必须检查：

- Holder 数大于 0；
- Relationship 数大于 0，或有明确的低关系边解释；
- Cluster 和普通成员数大于 0；
- 每个普通成员均有合法 Transfer 文件；
- `available_member_count == ordinary_member_count` 才能记为完整；
- `unique_transfer_count` 按 tx、from、to、value、timestamp 去重；
- API 和 PG 的 `source` lineage 未丢失；
- 窗口外 Transfer 在原始层可保留，但进入因子前必须过滤到研究窗口。

## 8. 生成高影响地址队列

只有 Transfer 文件数或 manifest 的 `available_member_count` 有新增时，才刷新 inventory 和标签队列：

```bash
"$PYTHON" "$RUN_ROOT/build_address_inventory.py" \
  --snapshot "$SNAPSHOT_ROOT" \
  --config "$CONFIG" \
  --output-dir "$REVIEW_ROOT" \
  --allow-missing-targets

"$PYTHON" "$RUN_ROOT/build_arkham_label_queue.py"
```

输出：

| 文件 | 内容 |
|---|---|
| `all-transfer-addresses.csv` | 所有观测到的币种—地址关系 |
| `high-impact-path-seeds.csv` | 单笔达到 Cluster 余额 0.1% 的 Transfer |
| `arkham-label-queue.csv` | 高影响 Transfer 端点的全局去重标签队列 |

队列规则：

- 新 pending 只能来自 `high-impact-path-seeds.csv` 的 `from/to`；
- 按 `chain + address` 全局去重，币种写入 `symbols` 关联列表；
- 已有 `reviewed_*` 的实体、标签、证据、时间和 notes 必须保留；
- 已复核但已不再高影响的地址保留审计行，`path_count=0`，不能重新 pending；
- 零地址标记 `confirmed_system_address`、`is_cex=false`；
- Bubblemaps 本地元数据明确为 CEX 的地址标记 `confirmed_from_local_metadata`。

刷新前后必须比较 reviewed 数量；没有人工修正记录时，reviewed 数量不得下降。

## 9. CEX 标签方法A：PostgreSQL 地址标签

每轮 Arkham API 或网页复核前，先一次性扫描所有尚未查过 PG 的高影响 pending：

```bash
cd "$RUN_ROOT"
"$PYTHON" batch_review_pg_labels.py --limit 0
```

数据源：`public.address_entity_label_snapshot`。

分类规则：

| PG 实体／标签 | 分类 |
|---|---|
| `is_cex=true` | CEX |
| Deposit、Hot Wallet、Cold Wallet、Prime Custody、Exchange Wallet、CEX Wallet | CEX；旧 `is_cex=false` 时在 notes 记录语义覆盖 |
| 交易所实体＋Airdrop Distribution | 非 CEX 边界，保留实体和用途 |
| DEX、LP、Bridge、Staking、Vesting、Multisig、Gnosis Safe、Proxy、普通合约 | `is_cex=false`，保留原标签 |
| 无命中 | 维持 `pending_web_review` |

只有数据库标签源确认更新后才使用 `--recheck-pending`。如需重新查询 Arkham API 已返回无标签的 unknown 地址：

```bash
"$PYTHON" batch_review_pg_labels.py \
  --include-api-unlabeled \
  --limit 0
```

## 10. CEX 标签方法B：Arkham 官方 API

当前只读端点：

```text
GET https://api.arkm.com/intelligence/address/{address}/all
Header: API-Key: <从 .env 安全读取>
```

链映射：

| 本地链 | Arkham chain key |
|---|---|
| `eth` | `ethereum` |
| `bsc` | `bsc` |
| `solana` | `solana` |
| `base` | `base` |
| `arbitrum` | `arbitrum_one` |
| `polygon` | `polygon` |
| `avalanche` | `avalanche` |

不支持的链保持 pending/unknown，禁止猜测链名。

### 10.1 Dry-run

```bash
cd "$PROJECT_ROOT"

"$PYTHON" "$RUN_ROOT/batch_review_arkham_api.py" \
  --include-web-unlabeled \
  --limit 0 \
  --workers 8 \
  --min-interval 0.10 \
  --timeout 30 \
  --max-attempts 4 \
  --checkpoint-every 20 \
  --dry-run
```

Dry-run 只报告候选数、链分布和并发参数，不读取或打印 API Key。

### 10.2 正式查询

```bash
"$PYTHON" "$RUN_ROOT/batch_review_arkham_api.py" \
  --include-web-unlabeled \
  --limit 0 \
  --workers 8 \
  --min-interval 0.10 \
  --timeout 30 \
  --max-attempts 4 \
  --checkpoint-every 20
```

并发只用于只读 HTTP 请求。请求起始时间仍受全局 `min-interval` 控制；队列与审计状态只能由主线程串行、原子写入。

API 返回字段优先读取：

- `arkhamEntity` / `entity`；
- `arkhamLabel` / `label`；
- `depositExchange`；
- `depositExchangeID`；
- `entity_type`；
- `contract`。

判定顺序：

1. Airdrop Distribution、DEX/Router、LP、Bridge、Staking、Vesting、Multisig/Safe Proxy 等明确冲突用途优先判为非 CEX；
2. deposit exchange ID 或 Deposit/Hot Wallet/Cold Wallet/Prime Custody/Exchange Wallet/CEX Wallet 判为 CEX；
3. `entity_type` 明确为中心化交易所判为 CEX；
4. 有实体或用途标签但没有 CEX 边界语义，保留标签、`is_cex=false`；
5. 目标链没有实体和用途标签，标记 `reviewed_arkham_api_unlabeled`、`is_cex=unknown`。

AI 预测实体不能单独用于确认 CEX。

输出：

- `arkham-label-queue.csv`：最终实体、标签、分类、证据和复核时间；
- `arkham-api-review-state.json`：脱敏请求摘要和结果；
- 自动创建的 `.bak.csv`：实跑前队列备份。

401/403 必须立即停止；429/5xx 按 `Retry-After` 和退避重试。失败地址保持原队列状态，不能写成 unlabeled。

## 11. CEX 标签方法C：已登录 Arkham 网页

网页只处理 PG 和 Arkham API 之后仍未知的高影响端点。推荐使用 Codex 内置浏览器的现有登录会话，不导出 Cookie。

### 11.1 选择候选

```bash
cd "$RUN_ROOT"

"$PYTHON" web_review_cooldown.py select \
  --limit 30 \
  --cooldown-minutes 45 \
  --include-api-unlabeled
```

候选按 `max_cluster_share_pct` 降序，且自动跳过 45 分钟内返回通用标题或导航错误的地址。不得绕过冷却重新选择。

### 11.2 浏览器读取

页面格式：

```text
https://arkm.com/explorer/address/{address}
```

建议：

- 每组最多 5 个后台标签页并发；
- 最多 6 组，即每轮最多 30 个地址；
- 页面不稳定时降到 3 并发；
- 每组等待 URL 和标题均返回后统一收集；
- 收集完成后关闭该组页面；
- 浏览器只并发读，CSV 永远串行写。

页面判定：

| 页面结果 | 队列状态 | `is_cex` |
|---|---|---|
| 明确 Deposit、Hot/Cold Wallet、Prime Custody、Exchange Wallet | `reviewed_web` | `true` |
| 明确属于 Binance、Gate、KuCoin 等 CEX，且无冲突用途 | `reviewed_web` | `true` |
| Airdrop Distribution、DEX/LP、Bridge、Staking、Multisig、普通合约 | `reviewed_web` | `false`，保留原标签 |
| 只有纯地址标题，没有实体或用途 | `reviewed_web_unlabeled` | `unknown` |
| 通用标题、无标题、导航错误 | 不改队列 | 记录 `generic` 或 `error` |
| HTTP 429、CAPTCHA、登录失效、安全限制 | 不改队列并停止本轮 | 记录 `security` |

浏览器 Statsig 或统计上报告警不等于 Arkham 限流。只要 Arkham URL、标题和标签正常返回，可以继续判定。

### 11.3 串行落盘

CEX 示例：

```bash
"$PYTHON" update_arkham_label.py \
  --chain bsc \
  --address 0x0000000000000000000000000000000000000001 \
  --entity 'Example Exchange' \
  --label 'Deposit Address' \
  --is-cex true \
  --status reviewed_web \
  --evidence 'https://arkm.com/explorer/address/0x0000000000000000000000000000000000000001' \
  --notes 'Arkham explicit deposit boundary; read-only review.'

"$PYTHON" web_review_cooldown.py record \
  --chain bsc \
  --address 0x0000000000000000000000000000000000000001 \
  --outcome success \
  --note 'label persisted serially'
```

纯地址无标签示例：

```bash
"$PYTHON" update_arkham_label.py \
  --chain bsc \
  --address 0x0000000000000000000000000000000000000002 \
  --entity Unknown \
  --label 'No Arkham entity or usage label displayed' \
  --is-cex unknown \
  --status reviewed_web_unlabeled \
  --evidence 'https://arkm.com/explorer/address/0x0000000000000000000000000000000000000002' \
  --notes 'Pure address title only; not classified as non-CEX.'
```

失败页面只写冷却状态，不调用 `update_arkham_label.py`：

```bash
"$PYTHON" web_review_cooldown.py record \
  --chain bsc \
  --address 0x0000000000000000000000000000000000000003 \
  --outcome generic \
  --note 'Arkham generic title; no classification made'
```

## 12. 多跳 CEX 边界

多跳路径只有在找到首次 CEX 边界且证据完整时才进入因子。必须保存：

- 根 Transfer；
- 完整路径地址；
- 完整路径交易哈希；
- 首次 CEX 边界的时间、方向、金额；
- CEX 地址、实体和用途标签；
- 最终 `boundary_tx_hash`。

规则：

- 路径中间跳不重复计算金额；
- 同一根 Transfer 已直接命中 CEX 时，不再计算多跳；
- 多条路径到达同一首次边界时按边界事件键去重；
- 未找到边界可以记录审计状态，但不能构造 CEX 金额。

## 13. 复算 CEX Flow

以下任一变化后都要复算：

- 新增 `is_cex=true`；
- CEX 标签更正；
- 新增确认多跳边界；
- 新增 Transfer。

```bash
"$PYTHON" "$RUN_ROOT/compute_cex_net_flows.py"
"$PYTHON" "$RUN_ROOT/build_data_status.py"
```

主要输出：

| 文件 | 内容 |
|---|---|
| `cex-flow/direct-cex-events.csv` | 直接 CEX 边与确认的多跳边界 |
| `cex-flow/daily-cex-net-flows.csv` | 每日 `流入CEX - 从CEX流出` |
| `DATA_PIPELINE_STATUS.md` | 数据、Transfer、标签和 CEX 事件覆盖 |

## 14. 质量门禁

### 14.1 地址队列

- `chain + canonical address` 唯一；
- 所有 pending 的 `high_impact_transfer_count > 0`；
- `reviewed_web_unlabeled` 和 `reviewed_arkham_api_unlabeled` 的 `arkham_is_cex` 为空；
- `is_cex=true` 必须有实体／标签、证据和复核时间；
- 已复核数量不能在无人工修正记录时下降；
- CSV 不得有空文件、半行、重复表头或并发覆盖。

### 14.2 CEX 事件

建议唯一键：

```text
chain + tx_hash + direction + cex_address + path_type
```

还需检查：

- 直接与多跳不重复；
- 多跳边界金额大于 0；
- `boundary_tx_hash` 非空；
- 时间在研究窗口内；
- 方向只能是约定的流入／流出枚举。

### 14.3 数据覆盖

正式完成必须满足：

- 所有配置币有 Holder、Relationship 和 Cluster；
- `available_member_count == ordinary_member_count`；
- 高影响地址无 `pending_web_review`；
- API 与网页没有未解释的认证、限流或安全错误；
- manifest 与实际文件数一致；
- 每个币都可追溯到 PG 批次、本地快照或 Bubblemaps API 捕获时间。

未满足时，报告必须明确写“阶段性下界”。

## 15. 后台进程与恢复

如果运行后台采集：

1. 从 PID 文件读取 PID；
2. 必须使用系统 `ps` 判断是否存活；
3. `kill -0` 或 `pgrep` 在受限环境中出现权限错误，不能据此判断进程死亡；
4. 日志只判断进度和异常，不能代替 `ps`；
5. 经 `ps` 确认意外停止时，先报告，不自动重启。

示例：

```bash
PID=$(tr -d '[:space:]' < "$RUN_ROOT/pipeline-background.pid")
/bin/ps -p "$PID" -o pid=,ppid=,stat=,etime=,command=
tail -n 100 "$RUN_ROOT/pipeline-background.log"
```

恢复原则：

- 结构文件合法时复用；
- Transfer 按单成员文件断点续跑；
- Arkham API 按 state 和 `reviewed_arkham_api*` 状态续跑；
- 网页失败地址遵守冷却后再选；
- 发现 reviewed 结果丢失时立即停止，从最近 `.bak.csv` 恢复；
- 不从头覆盖整个批次。

## 16. 每轮汇报模板

```text
数据批次：<目录/日期>
Bubblemaps结构：<完成币>/<总币>
Transfer：<available_member>/<ordinary_member>（<pct>%），新增文件 <n>
PG标签：处理 <n>，命中 <n>，CEX <n>
Arkham API：处理 <n>，标签 <n>，新增CEX <n>，无标签unknown <n>，错误 <n>
Arkham网页：成功 <n>，新增CEX <n>，非CEX标签 <n>，无标签 <n>，错误/冷却 <n>
地址总进度：已复核 <reviewed>/<high-impact unique>，剩余 <pending>
CEX事件：直接 <n>，多跳边界 <n>
异常：<无/认证/429/登录/安全限制/采集错误>
口径：<complete/partial；是否只是已观测下界>
```

只在覆盖变化、新增标签、命中 CEX、待复核下降、错误或全部完成时汇报。

## 17. 专用数据 Agent 提示词

```text
你负责维护DBCompare指定批次的Bubblemaps和CEX标签数据。严格执行
docs/data-acquisition-and-cex-labeling-sop.md。

执行顺序：
1. 冻结chain+token_address配置、研究窗口和批次目录；
2. Bubblemaps结构优先从PostgreSQL最新成功快照导出，本地有效文件其次，原始API只补缺；
3. Transfer优先从PG member view按chain+token+ordinary member导入，再用原始API补缺并断点保存；
4. 只有Transfer新增时刷新地址inventory与标签队列，保留全部reviewed_*；
5. 新待复核只允许高影响Transfer的from/to端点，按chain+address全局去重；
6. 标签顺序固定为PG快照、Arkham官方API、已登录Arkham网页；
7. 非CEX实体和用途标签必须原样保留；无标签只能记unknown，不能记确定非CEX；
8. API与网页只读，不输出凭证，不绕过401/403/429、CAPTCHA、登录或安全限制；
9. HTTP读取可以有限并发，标签队列和审计状态必须串行原子写入；
10. 多跳只计首次CEX边界并保存边界交易哈希，直接与多跳不重复；
11. 每轮执行唯一性、覆盖、状态与事件去重门禁；
12. 后台进程存活只用系统ps确认，意外停止先报告，不自动重启；
13. 未达到正式完成门槛时明确标记partial和已观测下界。
```

## 18. 实现文件索引

- 完整历史版本：[`analysis/binance-bubblemaps-expanded-universe-2026-08-03/DATA_ACQUISITION_AND_CEX_LABELING_SOP.md`](../analysis/binance-bubblemaps-expanded-universe-2026-08-03/DATA_ACQUISITION_AND_CEX_LABELING_SOP.md)
- Bubblemaps API 客户端：[`getMarket/bubblemaps/tool/bubblemaps_api.py`](../getMarket/bubblemaps/tool/bubblemaps_api.py)
- 结构采集：[`capture_bubblemaps_structures.py`](../analysis/binance-bubblemaps-expanded-universe-2026-08-03/capture_bubblemaps_structures.py)
- Transfer 采集：[`capture_bubblemaps_transfers.py`](../analysis/binance-bubblemaps-expanded-universe-2026-08-03/capture_bubblemaps_transfers.py)
- PG 结构导出：[`export_pg_bubblemaps_snapshots.py`](../analysis/binance-bubblemaps-expanded-universe-2026-08-03/export_pg_bubblemaps_snapshots.py)
- PG Transfer 导入：[`import_pg_transfers.py`](../analysis/binance-bubblemaps-expanded-universe-2026-08-03/import_pg_transfers.py)
- PG 地址标签：[`batch_review_pg_labels.py`](../analysis/binance-bubblemaps-expanded-universe-2026-08-03/batch_review_pg_labels.py)
- Arkham API 标签：[`batch_review_arkham_api.py`](../analysis/binance-bubblemaps-expanded-universe-2026-08-03/batch_review_arkham_api.py)
- 网页候选冷却：[`web_review_cooldown.py`](../analysis/binance-bubblemaps-expanded-universe-2026-08-03/web_review_cooldown.py)
- 单地址更新：[`update_arkham_label.py`](../analysis/binance-bubblemaps-expanded-universe-2026-08-03/update_arkham_label.py)
- CEX Flow：[`compute_cex_net_flows.py`](../analysis/binance-bubblemaps-expanded-universe-2026-08-03/compute_cex_net_flows.py)
