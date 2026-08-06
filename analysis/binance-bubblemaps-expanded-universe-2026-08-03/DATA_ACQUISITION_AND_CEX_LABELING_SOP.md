# Bubblemaps 数据采集与 CEX 标签复核 SOP

版本：2.1（2026-08-05，统一研究窗口前推至 2025-01-01 UTC）
适用目录：`analysis/binance-bubblemaps-expanded-universe-2026-08-03`
用途：交给专用数据 Agent，独立维护币安日 K、Bubblemaps 结构/Transfer、高影响地址标签、直接及多跳 CEX 边界与逐日净流。

## 0. 数据源优先级与扩池范围

数据源顺序是强制口径，不是建议：

| 数据 | 第一优先 | 第二优先 | 最后回退 |
|---|---|---|---|
| 币安日 K | SSH 主机上的 Binance Vision 归档 | 已校验的本地缓存 | Binance 公共 API |
| Bubblemaps Holder/关系/Cluster | PostgreSQL 最新精确 `chain + token_address` 快照 | 已有且通过校验的本地快照 | Bubblemaps API |
| Cluster 成员 Transfer | PostgreSQL `bubblemaps_transfer_member_view` | 已有本地成员 Transfer | Bubblemaps API |
| 地址实体与 CEX 标签 | PostgreSQL `address_entity_label_snapshot` | Arkham 官方只读 API | 已登录 Arkham 网页只读复核 |

本次全部扩选结果以
`screening/binance-futures-small-meme-volume-spikes-2026-08-05/all-233-expansion-registry.json`
为机器可读注册表。233 个候选全部启用，但“启用”不等于 Bubblemaps 数据已齐：

- SSH 最新日 K manifest 命中：233/233；
- PG 结构及 Transfer 可直接复用：169；
- PG 目标缺失：43；
- PG 最新批次失败：2；
- PG 尚无对应链：19。

因此有 64 个币仍需 API 回退或新增链适配。不得把它们记为 PG 完成。PG 最新批次是失败状态时，不得静默改用更老的成功批次；应保留失败状态、记录原因，再按回退路径处理。

每条数据必须在 manifest 中记录实际来源、远端路径或 PG 批次、抓取/导出 UTC、校验状态与回退原因。同一批回测不得混淆“PG 最新”“旧本地快照”和“API 新抓取”。

统一研究窗口为 `2025-01-01 UTC` 至批次约定的最后完整 UTC 日；当前审计截止为 `2026-08-03`。币种有效起点为 `max(2025-01-01, 币安合约实际首根日 K)`，上市前不得补零、前向填充或借用现货价格。Transfer、CEX 事件和因子均按同一闭区间过滤。

## 1. 目标与完成定义

本流程交付四类可审计数据：

1. 币安合约日 K；
2. Bubblemaps Holder、关系边、Cluster 及 Cluster 普通成员历史 Transfer；
3. 仅针对高影响 Transfer `from/to` 端点的实体、用途与 CEX 边界标签；
4. 去重后的直接/多跳 CEX 边界事件及逐日 CEX 净流。

完成分两级：

- **阶段完成**：允许 Transfer 或地址复核未满，但必须标记 `partial`，所有 CEX 流仅视为已观测下界。
- **正式完成**：日 K 到统一截止日、Holder/Cluster 全部完成、Transfer 100%、高影响地址无 `pending_web_review`、多跳审计完成、质量门禁全部通过。

任何未知地址都不能因为“未找到标签”而被当成确定非 CEX。

## 2. 强制安全边界

- 只处理 `high-impact-path-seeds.csv` 中高影响 Transfer 的 `from_address`、`to_address`。不得把普通地址加入新增待复核队列。
- 地址主键固定为 `lower(chain) + chain-native canonical address`，跨币全局去重；EVM/十六进制地址转小写，Solana Base58、TON 等大小写敏感地址必须保留原始规范形式。同一地址的 `symbols` 只做关联列表，禁止对所有链无条件 `.lower()`。
- Arkham API 与网页仅做只读核对。API 固定使用官方
  `GET https://api.arkm.com/intelligence/address/{address}/all`；不得调用创建、修改或删除用户标签的端点。
- API Key 只能从仓库根目录 `.env` 的 `Arkm_API_KEY`（兼容
  `ARKHAM_API_KEY`）读取并放入 `API-Key` 请求头；不得打印、写入命令行、CSV、JSON、日志或报告。
- API 遇到 HTTP 401/403 必须立即停止；遇到 429 必须遵守 `Retry-After`
  和退避，不得通过提高并发绕过。网页不得绕过 CAPTCHA、登录、HTTP 429 或网站安全限制。
- 浏览器并发只用于读取页面；`arkham-label-queue.csv` 和 `web-review-attempts.json` 必须逐条串行写入，禁止多个进程同时修改。
- 非 CEX 标签也必须原样保留。`is_cex=false` 只是分类，不等于丢弃实体或用途标签。
- Arkham API 对目标链没有返回实体/用途标签时，状态必须是
  `reviewed_arkham_api_unlabeled`、CEX 为 `unknown`；网页只有纯地址标题时同理使用
  `reviewed_web_unlabeled`。两者都不得写成确定非 CEX。
- 多跳只记录首次到达的 CEX 边界，且必须保存最终边界交易哈希。路径中间跳不计金额；直接边与多跳边不得重复计数。
- 采集进程存活必须用系统 `ps` 核对。受限环境里的 `kill -0`/`pgrep` 权限错误不能当作进程死亡。
- 经 `ps` 确认后台进程意外停止时，先报告，不自动重启。只有任务授权明确包含重启时才恢复。
- 不在日志、CSV、命令行或报告中写数据库密码、Cookie、Token、OTP 或浏览器会话内容。

## 3. 环境与运行变量

从仓库根目录执行：

```bash
cd /Users/rayer/Documents/DBCompare
RUN_ROOT=/Users/rayer/Documents/DBCompare/analysis/binance-bubblemaps-expanded-universe-2026-08-03
PYTHON=/Users/rayer/Documents/DBCompare/.venv/bin/python
FACTOR_FACTORY=/Users/rayer/Documents/Factor_Factory
FACTOR_PYTHON=/Users/rayer/Documents/Factor_Factory/.venv/bin/python
REGISTRY=/Users/rayer/Documents/DBCompare/screening/binance-futures-small-meme-volume-spikes-2026-08-05/all-233-expansion-registry.json
KLINE_HOST=rayer@192.168.32.153
```

运行前确认：

```bash
test -x "$PYTHON"
test -f "$RUN_ROOT/expanded_universe_config.json"
test -f /Users/rayer/Documents/DBCompare/.env
"$PYTHON" -m py_compile \
  "$RUN_ROOT/fetch_daily_klines.py" \
  "$RUN_ROOT/export_pg_bubblemaps_snapshots.py" \
  "$RUN_ROOT/query_pg_bubblemaps_structures.py" \
  "$RUN_ROOT/import_pg_transfers.py" \
  "$RUN_ROOT/query_pg_transfer_member_view.py" \
  "$RUN_ROOT/audit_pg_transfer_window.py" \
  "$RUN_ROOT/validate_pg_bubblemaps_exports.py" \
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

`fetch_daily_klines.py` 是公共 API 回退工具，必须显式传 `--start-date`、`--cutoff` 和本批注册表。不得把它作为首选数据源，也不得混用不同截止日。

### 3.1 Lark 数据目录与 rayer_mcp_bot 鉴权

数据目录说明的唯一入口是 Lark Wiki：
`https://jjp1ynw9z1yy.jp.larksuite.com/wiki/LAcxw09xKiMB9LkvYYXjNDrapVg`。
读取实现参考 `Factor_Factory/scripts/feishu_pull_wiki.cjs`，不得把 App Secret、用户 Token 或 Cookie 写入 SOP、命令历史或日志。

标准只读流程：

1. 用 `@larksuiteoapi/lark-mcp/dist/auth` 的 `authStore` 初始化本地凭证；
2. 用户 OAuth 可用时优先 `user_access_token`；
3. 用户 Token 过期（例如 Lark 错误码 `99991677`）时，不把它当文档不存在；改用已授权应用的 tenant 模式；
4. 调用 `wiki.space.getNode` 将 Wiki token 解析为 Docx token；
5. 调用 `docx.document.rawContent` 只读获取正文，并保存带 UTC 时间的本地快照；
6. rawContent 若丢失表格结构，需改用文档导出或登录后的只读浏览器核对，不得凭经验补全路径。

参考命令（Secret 通过安全环境注入，不能写字面值）：

```bash
cd "$FACTOR_FACTORY"
FEISHU_WIKI_TOKEN=LAcxw09xKiMB9LkvYYXjNDrapVg \
OUTPUT_PATH="$RUN_ROOT/lark-data-directory-snapshot.txt" \
AUTH_MODE=tenant \
node scripts/feishu_pull_wiki.cjs
```

如需恢复用户 OAuth，使用 `@larksuiteoapi/lark-mcp` 的 login 流程重新授权；只记录成功/失败、App ID 和 UTC，不保存 access token。当前已核验 tenant 只读模式可访问该文档，本地快照为 `lark-data-directory-snapshot.txt`。

## 4. 标准执行顺序

### 4.1 冻结配置与初始状态

1. 检查本批配置的 symbols、链和 Token 地址。233 币扩池必须使用 `$REGISTRY`，旧 20 币任务才使用 `expanded_universe_config.json`。
2. 为新批次使用新的日期目录，禁止覆盖旧批次作为“更新”。
3. 记录运行开始 UTC、配置文件 SHA-256、目标币种数和统一数据截止日。
4. 如果目录已有标签结果，先保存可恢复副本：

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
if test -f "$RUN_ROOT/arkham-review/arkham-label-queue.csv"; then
  cp "$RUN_ROOT/arkham-review/arkham-label-queue.csv" \
    "$RUN_ROOT/arkham-review/arkham-label-queue.$STAMP.bak.csv"
fi
if test -f "$RUN_ROOT/arkham-review/web-review-attempts.json"; then
  cp "$RUN_ROOT/arkham-review/web-review-attempts.json" \
    "$RUN_ROOT/arkham-review/web-review-attempts.$STAMP.bak.json"
fi
if test -f "$RUN_ROOT/arkham-review/arkham-api-review-state.json"; then
  cp "$RUN_ROOT/arkham-review/arkham-api-review-state.json" \
    "$RUN_ROOT/arkham-review/arkham-api-review-state.$STAMP.bak.json"
fi
```

缺少源文件时跳过对应备份，不创建空文件冒充历史状态。

233 币注册表由筛选结果、服务器 K 线 manifest 和 PG 只读审计共同生成。源筛选结果或 manifest 变化时重新构建：

```bash
cd /Users/rayer/Documents/DBCompare/screening/binance-futures-small-meme-volume-spikes-2026-08-05
"$FACTOR_PYTHON" build_all_candidate_expansion.py
```

重建后检查：候选数为 233、`chain + contract_address` 唯一、所有外部 chain ID 已显式映射、K 线/PG 状态计数之和均等于 233。还必须抽查 Solana/TON 等非 EVM 地址未被小写化。PG 连接失败时脚本仍可生成注册表，但会标记 `pg_not_checked`；这种注册表只能用于诊断，不能据此声称 PG 覆盖完成。

### 4.2 SSH-first 拉取币安日 K

远端主机：`rayer@192.168.32.153`。目录定义以 Lark 文档快照为准：

- 正式新盘：`/data2/shares/raw/binance/vision`；
- 历史旧盘：`/data/shares/raw/binance/vision`；
- 未正式校验区：`/data2/shares/raw_tmp/binance/vision`；
- 采集代码：`/data2/shares/code/nextalpha-tardis-data`。

币安 U 本位合约日 K 的单文件路径约定为：

```text
{root}/futures-um/klines/USDT/1d/{YYYY-MM-DD}/{SYMBOL}-1d-{YYYY-MM-DD}.zip
```

其中 `{SYMBOL}` 已含 `USDT`，例如 `ZEREBROUSDT`。只读取完整 UTC 日，不读取当天未收盘文件。

读取顺序按日期执行：正式新盘 → 历史旧盘 → 经校验的 `raw_tmp`。`raw_tmp` 不能仅因文件存在就进入回测，必须通过 ZIP CRC、CSV header、日期、OHLC 和重复时间戳校验，并在 manifest 标记 `staging_validated`。历史分界日以最新 Lark 文档为准；当前说明为 2026-06-26 及以前主要保留在旧盘。

为了吞吐与审计，不得为每个文件单独建立 SSH 连接。优先在远端用一次批任务生成 manifest/聚合结果，或按 manifest 一次 `rsync` 所需文件。先用只读命令核验主机和目录：

```bash
ssh -B en0 "$KLINE_HOST" hostname
ssh -B en0 "$KLINE_HOST" test -d /data2/shares/raw/binance/vision
ssh -B en0 "$KLINE_HOST" test -d /data/shares/raw/binance/vision
ssh -B en0 "$KLINE_HOST" test -d /data2/shares/raw_tmp/binance/vision
```

2025 窗口审计见 `kline-2025-window-audit.json`：233/233 均有远端文件；117 个在 2025-01-01 已有合约日 K，112 个在 2025 年上市后开始，`ACU`、`ELSA`、`SKR`、`FIGHT` 直到 2026 年才开始。新批次必须重新生成审计，不能长期复用该结论。

只有远端缺口才运行公共 API 回退：

```bash
"$PYTHON" "$RUN_ROOT/fetch_daily_klines.py" \
  --config "$REGISTRY" \
  --output-dir "$RUN_ROOT/klines-1d" \
  --start-date 2025-01-01 \
  --cutoff 2026-08-03
```

验收：

- `klines-1d/manifest.json` 包含配置中的全部币，并逐段记录 `ssh_canonical`、`ssh_legacy`、`ssh_staging_validated`、`local_cache` 或 `binance_public_api`；
- 每个币 `complete_through_cutoff=true`；
- OHLC 为正、`high >= max(open, close)`、`low <= min(open, close)`；
- `open_time_ms` 无重复，日期连续性和合约上市日前空缺解释清楚；
- 报告总 K 线数、首日、末日、截止日与各来源行数。

### 4.3 PG-first 获取 Bubblemaps Holder、关系边和 Cluster

PostgreSQL 的结构真源是 `public.bubblemaps_membership_snapshot`、
`public.bubblemaps_token_holder`、`public.bubblemaps_holder_relationship_snapshot`。
必须用精确的规范化 `chain + lower(token_address)` 查询，禁止只按 symbol 匹配。

先把 PG 最新成功快照导出为现有采集器兼容结构：

```bash
"$PYTHON" "$RUN_ROOT/export_pg_bubblemaps_snapshots.py" \
  --config "$REGISTRY" \
  --snapshot-root "$RUN_ROOT/bubblemaps-snapshot" \
  --factor-factory-root "$FACTOR_FACTORY" \
  --report "$RUN_ROOT/pg-structure-export-report.json"
```

该工具通过 `query_pg_bubblemaps_structures.py` 使用 Factor_Factory 的数据库配置只读查询；数据库凭证不得输出。默认保留已存在且有效的本地结构，只有显式审计后才能使用 `--overwrite`。每币报告必须区分：`exported_from_pg`、`preserved_local`、`pg_missing`、`pg_latest_failed`、`pg_chain_not_present` 和导出错误。

PG/本地仍缺失的币才进入 Bubblemaps API 回退：

```bash
"$PYTHON" "$RUN_ROOT/capture_bubblemaps_structures.py" \
  --config "$REGISTRY" \
  --snapshot-root "$RUN_ROOT/bubblemaps-snapshot" \
  --timeout 20 \
  --max-attempts 5 \
  --retry-delay 3 \
  --min-interval 2
```

API 回退必须按支持链分批；当前客户端尚不支持的链记录为 `adapter_required`，不能因为一个不支持链导致已支持链结果丢失，也不能伪造为空数据。

此步骤可断点续跑：PG 导出或 API 已生成的有效 `holders.json`、`relationships.json` 会复用。验收每币均有：

- `bubblemaps-snapshot/clean/{SYMBOL}/holders.json`；
- `bubblemaps-snapshot/clean/{SYMBOL}/relationships.json`；
- `bubblemaps-snapshot/data/{SYMBOL}/token.json`；
- Holder、边、普通 Cluster 成员数非零，且达到该批次的入池门槛；
- token manifest 写明 PG batch/completed_at 或 API 证据时间及实际来源。

### 4.4 PG-first 获取 Cluster 成员历史 Transfer

先从 `public.bubblemaps_transfer_member_view` 导入。导入器严格用
`chain + token_address + ordinary member` 过滤，并与已有本地/API 事件按交易哈希、端点、金额和时间去重合并：

```bash
"$PYTHON" "$RUN_ROOT/import_pg_transfers.py" \
  --config "$REGISTRY" \
  --snapshot-root "$RUN_ROOT/bubblemaps-snapshot" \
  --symbols '<从 all-233-expansion-registry.csv 筛出的 pg_ready/structure_ready 币种，逗号分隔>' \
  --start-date 2025-01-01 \
  --end-date 2026-08-03 \
  --factor-factory-root "$FACTOR_FACTORY" \
  --report "$RUN_ROOT/pg-transfer-import-report.json"
```

PG 结构或 Transfer 批量落入正式快照后，必须从已验证文件重建 manifest；
否则旧 manifest 不包含新币，后续 inventory 会把已导入目标误报为缺失：

```bash
"$PYTHON" "$RUN_ROOT/rebuild_snapshot_manifest.py" \
  --config "$REGISTRY" \
  --snapshot-root "$RUN_ROOT/bubblemaps-snapshot" \
  --preserve-unconfigured
```

必须显式传 `--symbols`；脚本默认值只用于旧批次诊断，不适用于 233 币。PG 导入后先比较每币普通成员、已有文件、PG 命中成员和事件数。只有 PG/本地缺口才交给可断点续跑的余额优先、跨币轮询 API 采集器：

PG 时间覆盖先用以下命令审计：

```bash
"$FACTOR_PYTHON" "$RUN_ROOT/audit_pg_transfer_window.py" \
  --registry "$REGISTRY" \
  --start-date 2025-01-01 \
  --end-date 2026-08-03 \
  --factor-factory-root "$FACTOR_FACTORY" \
  --output "$RUN_ROOT/pg-transfer-2025-window-audit.json"
```

当前结果为 169 个 PG-ready 币全部在窗口内有事件，共 1,069,735 个去重事件；其中 103 个历史早于或等于窗口起点，66 个在项目上线后才开始。其余 64 个没有 PG Transfer，继续走本地/API 回退。

```bash
"$PYTHON" "$RUN_ROOT/capture_bubblemaps_transfers.py" \
  --config "$REGISTRY" \
  --snapshot-root "$RUN_ROOT/bubblemaps-snapshot" \
  --timeout 20 \
  --max-attempts 100 \
  --retry-delay 3 \
  --min-interval 1 \
  --checkpoint-every 20
```

规则：

- 已存在且结构有效的 PG/本地/API 成员 Transfer 文件必须复用；失败成员留待后续重跑。
- PG 行必须保留 `source=postgresql`；API 回退必须保留自身 source，禁止合并后抹掉 lineage。
- API 可能返回窗口外历史；原始层可保留，但生成高影响路径、CEX 事件和回测输入时必须过滤到 `2025-01-01..2026-08-03`，不得让 2025 年前事件进入信号。
- 遵守服务端 429 与 `retry_after`，不得通过增加并发绕过限流。
- 每个 checkpoint 更新 `bubblemaps-snapshot/manifest.json`。
- 需要缩小诊断范围时可用 `--symbols SAFE,BLUAI` 或 `--max-members N`，但交付时必须重新跑全配置检查。

进度读取：

```bash
"$PYTHON" - <<'PY'
import json
from pathlib import Path
p = Path('/Users/rayer/Documents/DBCompare/analysis/binance-bubblemaps-expanded-universe-2026-08-03/bubblemaps-snapshot/manifest.json')
m = json.loads(p.read_text())
available = sum(int(x['available_member_count']) for x in m['tokens'])
total = sum(int(x['ordinary_member_count']) for x in m['tokens'])
errors = sum(int(x['transfer_error_count']) for x in m['tokens'])
print({'status': m['status'], 'available': available, 'total': total,
       'coverage_pct': round(available / total * 100, 2) if total else 0,
       'errors': errors})
PY
```

如果使用后台进程，先读取 PID 文件，再用系统 `ps`：

```bash
PID=$(tr -d '[:space:]' < "$RUN_ROOT/pipeline-background.pid")
/bin/ps -p "$PID" -o pid=,ppid=,stat=,etime=,command=
```

`pipeline-background.log` 只用于判断进度/异常，不能代替 `ps` 的进程存活结论。`run_full_pipeline_background.py` 是本批次旧总控，含特定币种参数；新 Agent 不得未经审核直接把它当通用入口。

### 4.5 仅在 Transfer 新增时刷新地址清单和标签队列

先比较 Transfer 文件数或 manifest 的 `available_member_count`。只有出现新增才执行：

```bash
"$PYTHON" "$RUN_ROOT/build_address_inventory.py" \
  --snapshot "$RUN_ROOT/bubblemaps-snapshot" \
  --config "$REGISTRY" \
  --output-dir "$RUN_ROOT/arkham-review" \
  --allow-missing-targets

"$PYTHON" "$RUN_ROOT/build_arkham_label_queue.py"
```

队列刷新必须满足：

- 新 pending 仅来自高影响路径端点；阈值为单笔金额达到对应 Cluster 余额的 0.1%；
- 按 `chain + address` 全局去重；
- 已有 `reviewed_*` 结果、证据、时间和 notes 保留；
- 已复核但后来不再高影响的地址只作为审计行保留，`path_count=0`，不得重新 pending；
- 零地址固定为 `confirmed_system_address`，不属于 CEX；
- 本地元数据确认的 CEX 固定为 `confirmed_from_local_metadata`。

刷新前后必须核对 reviewed 数量没有下降，除非有明确的人工修正记录。

### 4.6 PostgreSQL 批量标签复用

每次 API/网页复核前先扫完所有尚未查过 PG 的 pending：

```bash
cd "$RUN_ROOT"
"$PYTHON" batch_review_pg_labels.py --limit 0
```

数据源为 `public.address_entity_label_snapshot`。分类规则：

- 数据库 `is_cex=true`：直接确认 CEX；
- `Deposit`、`Hot Wallet`、`Cold Wallet`、`Prime Custody`、`Exchange Wallet`、`CEX Wallet` 等明确边界语义：即使旧 `is_cex=false` 也按 CEX，并在 notes 写“语义覆盖”；
- 仅与交易所实体有关但用途是 `Airdrop Distribution`：不得计 CEX；
- DEX/LP、Bridge、锁仓、质押、多签、Proxy、普通合约等标签必须保留，`is_cex=false`；
- 没有命中 PG 的地址保持 `pending_web_review`，不得改成 unlabeled 或非 CEX。

`--recheck-pending` 只在数据库标签源已明确更新时使用，避免每轮重复扫描无结果地址。
如需补充 Arkham API 已返回无标签的 unknown 地址，先运行
`batch_review_pg_labels.py --include-api-unlabeled --limit 0`；只允许高影响端点，命中前自动备份队列，未命中行保持原状态。

### 4.7 Arkham 官方 API 批量复核

API 端点、鉴权头与链名以 Arkham 官方文档为准（机器可读索引：
`https://arkm.com/llms.txt`）：根 URL 为
`https://api.arkm.com`，鉴权头为 `API-Key`；链名映射固定为：
`eth -> ethereum`、`bsc -> bsc`、`solana -> solana`、`base -> base`、
`arbitrum -> arbitrum_one`、`polygon -> polygon`、
`avalanche -> avalanche`。不得猜测或切换到旧域名。

先用 dry-run 核对候选数量，再执行全量只读查询：

```bash
cd /Users/rayer/Documents/DBCompare
"$PYTHON" "$RUN_ROOT/batch_review_arkham_api.py" \
  --include-web-unlabeled \
  --limit 0 \
  --workers 8 \
  --min-interval 0.10 \
  --timeout 30 \
  --max-attempts 4 \
  --checkpoint-every 20 \
  --dry-run

"$PYTHON" "$RUN_ROOT/batch_review_arkham_api.py" \
  --include-web-unlabeled \
  --limit 0 \
  --workers 8 \
  --min-interval 0.10 \
  --timeout 30 \
  --max-attempts 4 \
  --checkpoint-every 20
```

规则：

- 默认只处理 `pending_web_review`；`--include-web-unlabeled` 还会重查
  `reviewed_web_unlabeled`，用于用正式 API 替换网页 PoC 的“无标签”结论；
- 仍只允许处理高影响路径端点，`high_impact_transfer_count` 必须大于 0；
- `--workers` 只并发只读 HTTP 查询；请求开始时间仍由全局
  `--min-interval` 限速，队列和审计状态始终由主线程串行原子写入；
- `Deposit`、`Hot Wallet`、`Cold Wallet`、`Prime Custody`、
  `Exchange Wallet`、`CEX Wallet`、`deposit_exchange_id` 或明确的中心化
  `entity_type` 判为 CEX；
- `Airdrop Distribution`、DEX/Router、LP、Bridge、Staking、Vesting、
  Multisig/Safe Proxy 等冲突用途优先判为非 CEX，并保留原实体与标签；
- 只有实体/用途标签但没有 CEX 边界语义时，状态为
  `reviewed_arkham_api`、`is_cex=false`；
- 目标链无实体且无用途标签时，状态为
  `reviewed_arkham_api_unlabeled`、`is_cex=unknown`；API 无命中不等于非 CEX；
- AI 预测实体不得单独用于确认 CEX；本流程只使用目标链返回的确认实体、标签、
  `entity_type` 与 deposit 语义；
- 结果写入 `arkham-label-queue.csv`，脱敏摘要与请求结果写入
  `arkham-api-review-state.json`；二者串行、原子 checkpoint，均不得包含 API Key；
- 每次实跑自动创建队列 `.bak.csv`。401/403 立即停止；429/5xx 按服务端提示及退避重试。

验收：

- `arkham-api-review-state.json` 的条数与本轮成功/失败尝试一致；
- 本轮 `error` 必须为 0，或逐条说明且对应队列仍保持原状态；
- API 复核后如仍有 `pending_web_review`，才进入网页兜底；
- 任一新增 `is_cex=true` 后必须执行 4.10 复算。

### 4.8 Arkham 网页兜底复核

候选只能从冷却工具取得：

```bash
cd "$RUN_ROOT"
"$PYTHON" web_review_cooldown.py select \
  --limit 30 \
  --cooldown-minutes 45 \
  --include-api-unlabeled
```

处理顺序按 `max_cluster_share_pct` 降序。`--include-api-unlabeled` 只把高影响且仍为 unknown 的 API 无标签地址加入网页兜底，不把它们预先改成 pending。推荐每组最多 5 个后台标签页并发读取，最多 6 组；若网站不稳定可降为 3 并发。网页读取完成后再逐条串行写 CSV。

页面判定：

- 明确 `Deposit`、`Hot Wallet`、`Cold Wallet`、`Prime Custody`、`Exchange Wallet` 等：`is_cex=true`；
- 页面明确归属 Binance、Gate、KuCoin 等中心化交易所，且没有 Airdrop Distribution 等冲突用途：`is_cex=true`；
- `Airdrop Distribution`、DEX/LP、Bridge、锁仓、质押、多签、普通合约：保留实体/原标签，`is_cex=false`；
- 纯地址标题且无实体/用途：`status=reviewed_web_unlabeled`、`is_cex=unknown`；
- Arkham 通用标题、无标题或导航错误：不改标签队列，记录 `generic` 或 `error`；
- 明确 HTTP 429、CAPTCHA、登录失效或安全限制：记录 `security` 并立即停止本轮，不得绕过；
- 浏览器自身 Statsig/统计上报告警不属于 Arkham 限流；只要 Arkham URL 与页面标题明确返回，仍可判定。

有标签示例：

```bash
"$PYTHON" update_arkham_label.py \
  --chain bsc \
  --address 0x0000000000000000000000000000000000000001 \
  --entity 'Example Exchange' \
  --label 'Deposit Address' \
  --is-cex true \
  --status reviewed_web \
  --evidence 'https://arkm.com/explorer/address/0x0000000000000000000000000000000000000001' \
  --notes 'Arkham entity and explicit deposit-boundary label; read-only review.'

"$PYTHON" web_review_cooldown.py record \
  --chain bsc \
  --address 0x0000000000000000000000000000000000000001 \
  --outcome success \
  --note 'label persisted serially'
```

无标签示例：

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

页面失败只记录冷却，不调用 `update_arkham_label.py`：

```bash
"$PYTHON" web_review_cooldown.py record \
  --chain bsc \
  --address 0x0000000000000000000000000000000000000003 \
  --outcome generic \
  --note 'Arkham generic title; no classification made'
```

### 4.9 多跳 CEX 边界

只有找到首次 CEX 边界且具备完整证据时，才写 `confirmed_cex_boundary`。必须记录：根 Transfer、完整路径地址/交易哈希、边界日期与时间、方向、边界金额、CEX 地址/标签和最终 `boundary_tx_hash`。

```bash
"$PYTHON" "$RUN_ROOT/record_multihop_review.py" \
  --symbol SYMBOL \
  --chain bsc \
  --token-address TOKEN_ADDRESS \
  --root-date YYYY-MM-DD \
  --root-amount AMOUNT \
  --root-from-address FROM \
  --root-to-address TO \
  --root-tx-hash ROOT_TX \
  --status confirmed_cex_boundary \
  --path-addresses 'A>B>CEX' \
  --path-tx-hashes 'TX1>BOUNDARY_TX' \
  --boundary-date YYYY-MM-DD \
  --boundary-timestamp-ms TIMESTAMP_MS \
  --direction 流入CEX \
  --cex-amount AMOUNT_AT_BOUNDARY \
  --cex-address CEX_ADDRESS \
  --cex-label 'Exchange Deposit' \
  --boundary-tx-hash BOUNDARY_TX \
  --notes 'First confirmed CEX boundary; intermediate hops excluded.'
```

若未找到边界，可保存审计状态，但不得提供伪造的 CEX 金额或边界哈希。

### 4.10 复算 CEX 事件与状态报告

以下任一变化后必须复算：新增 `is_cex=true`、已有 CEX 标签更正、确认多跳边界、新增 Transfer。

```bash
"$PYTHON" "$RUN_ROOT/compute_cex_net_flows.py"
"$PYTHON" "$RUN_ROOT/build_data_status.py"
```

输出：

- `cex-flow/direct-cex-events.csv`：包含直接边及确认的 `multi_hop_boundary`；
- `cex-flow/daily-cex-net-flows.csv`：`流入 CEX - 从 CEX 流出`；
- `DATA_PIPELINE_STATUS.md`：数据与标签覆盖报告。

## 5. 质量门禁

每轮结束前全部执行。

### 5.1 队列唯一性与范围

```bash
cd "$RUN_ROOT"
"$PYTHON" - <<'PY'
import csv
from collections import Counter
rows = list(csv.DictReader(open('arkham-review/arkham-label-queue.csv')))
keys = [(r['chain'].lower(), r['address'].lower()) for r in rows]
assert len(keys) == len(set(keys)), 'duplicate chain+address in label queue'
bad = [r for r in rows if r['arkham_status'] == 'pending_web_review'
       and int(float(r.get('high_impact_transfer_count') or 0)) <= 0]
assert not bad, 'non-high-impact address entered pending queue'
print(Counter(r['arkham_status'] for r in rows))
print('confirmed_cex', sum(r.get('arkham_is_cex') == 'true' for r in rows))
PY
```

### 5.2 CEX 事件去重

```bash
"$PYTHON" - <<'PY'
import csv
from collections import Counter
rows = list(csv.DictReader(open('cex-flow/direct-cex-events.csv')))
keys = [(r['chain'].lower(), r['tx_hash'].lower(), r['direction'],
         r['cex_address'].lower(), r['path_type']) for r in rows]
dups = [k for k, n in Counter(keys).items() if n > 1]
assert not dups, f'duplicate CEX boundary events: {len(dups)}'
print({'events': len(rows), 'path_types': Counter(r['path_type'] for r in rows)})
PY
```

### 5.3 状态一致性

- `reviewed_web_unlabeled` 与 `reviewed_arkham_api_unlabeled` 的
  `arkham_is_cex` 必须为空；
- `reviewed_arkham_api*` 必须有目标链、官方 API evidence URL、复核时间，
  且 `arkham-api-review-state.json` 不得含 API Key；
- `arkham_is_cex=true` 必须有实体/标签、证据 URL 或 PG 证据、复核时间；
- `confirmed_cex_boundary` 必须有合法方向、正金额、边界日期、边界哈希；
- `manifest.status != success` 时所有日报 `coverage_status=partial`；
- 新旧队列对比时，reviewed 地址数不得无故下降；
- CSV 不得出现并发写入造成的空文件、半行或重复表头。

### 5.4 正式交付门槛

- 日 K：全部币到统一截止日；
- Holder/Cluster：全部币成功；
- Transfer：`available_member_count == ordinary_member_count`；
- 地址：`pending_web_review == 0`；
- 错误：无未解释的 API 401/403/429、网页登录/安全限制或写入错误；
- 事件：直接/多跳去重通过；
- 来源：每币日 K、结构和 Transfer 均能追溯到 SSH 路径/PG 批次/本地快照/API 证据；
- 报告：`DATA_PIPELINE_STATUS.md` 与 manifest/CSV 数量一致，233 币扩池还必须与 `all-233-expansion-registry.csv` 一致。

未达到时必须在交付中写“阶段性下界”，不能写“完整 CEX 流”或“正式可回测数据”。

## 6. 中断与恢复

1. 先判断中断发生在哪一阶段；不要从头重建全部目录。
2. Transfer 采集依赖单成员 JSON 与 manifest，可直接按原命令续跑。
3. API 复核依赖队列、自动 `.bak.csv` 与
   `arkham-api-review-state.json`；中断后按原命令重跑，已改为
   `reviewed_arkham_api*` 的地址不会重复进入默认候选。
4. API 401/403 不自动重试；429/5xx 只按退避重试，不增加并发。
5. 网页复核依赖队列和冷却状态；重新运行 `select`，不得绕过 45 分钟冷却。
6. 浏览器失败页面不写标签；下一轮由冷却器重新选择。
7. CSV/JSON 写入中断后先检查 `.tmp` 与正式文件；正式文件完整时不使用临时文件覆盖。
8. 发现 reviewed 结果丢失时，立即停止后续复算，从最近 `.bak.csv` 恢复并查明刷新步骤。
9. 后台进程意外死亡时先用 `ps` 证实并报告 PID、最后日志、当前 step 和落盘覆盖；不自动重启。

## 7. 每轮汇报模板

只有以下变化才主动汇报：Transfer 覆盖变化、新增标签、命中 CEX、待复核下降、币种完成、真实错误/限流、全部完成。

```text
数据批次：<目录/日期>
日K：<完成币>/<总币>，截止 <UTC日期>
Bubblemaps：结构 <完成币>/<总币>；Transfer <available>/<total>（<pct>%）
本轮PG：处理 <n>，命中 <n>，其中CEX <n>
本轮API：处理 <n>，明确标签 <n>，新增CEX <n>，无标签unknown <n>，错误 <n>
本轮网页：成功 <n>，新增CEX <n>，非CEX标签 <n>，无标签 <n>，错误/冷却 <n>
地址总进度：已复核 <reviewed>/<high-impact unique>，剩余 <pending>
CEX事件：直接 <n>，多跳边界 <n>，币种—日期 <n>
异常：<无/429/登录/安全限制/采集错误>
口径：<complete/partial，是否仅为已观测下界>
```

## 8. 专用 Agent 启动提示词

将下面内容作为专用 Agent 的长期任务说明，并把实际运行目录替换成目标批次：

```text
你负责维护指定目录的数据采集和CEX标签流水线。严格执行目录内
DATA_ACQUISITION_AND_CEX_LABELING_SOP.md。

核心要求：
1. 日K优先从SSH主机的Binance Vision正式/历史目录读取，raw_tmp必须校验，公共API只补缺口；
2. Bubblemaps结构和Transfer优先按精确chain+token_address复用PostgreSQL最新快照，再保留有效本地数据，API只补缺口；
3. 只处理高影响Transfer的from/to端点，chain+address全局去重；
4. Transfer有新增时才刷新inventory与Arkham队列，并保留全部reviewed_*结果；
5. 地址标签每轮先复用PostgreSQL，再用.env中的Arkham API Key只读批量复核，最后才用网页兜底；
6. API Key只进API-Key请求头，绝不输出或落盘；API无标签只能标reviewed_arkham_api_unlabeled/unknown；
7. 非CEX标签原样保留；网页纯地址无标签只能标reviewed_web_unlabeled/unknown；
8. API与浏览器只读，不绕过401/403/429、CAPTCHA、登录或安全限制；
9. API checkpoint及浏览器结果都必须串行原子写入；
10. 多跳只计首次CEX边界，保存边界交易哈希，直接与多跳不重复；
11. Lark目录文档只读；用户OAuth过期时按SOP使用已授权tenant模式，不输出任何Secret/Token；
12. 进程存活用系统ps核对；意外停止先报告，不自动重启；
13. 每轮执行质量门禁，未100%覆盖时明确标为阶段性下界；
14. 仅在进度、标签、CEX命中、错误或完成发生变化时简洁中文汇报。
```

## 9. 主要文件职责

| 文件 | 职责 |
|---|---|
| `expanded_universe_config.json` | 币种、链、Token 地址配置 |
| `screening/.../all-233-expansion-registry.json` | 233 币统一配置、链映射、数据源优先级与逐币就绪状态 |
| `screening/.../all-233-expansion-registry.csv` | 233 币覆盖审计和批处理筛选表 |
| `screening/.../build_all_candidate_expansion.py` | 从筛选结果、SSH manifest 和 PG 只读审计重建注册表 |
| `lark-data-directory-snapshot.txt` | Lark 主机数据目录文档的只读本地快照 |
| `server-binance-vision-daily-*.csv` | SSH Binance Vision 文件级 manifest |
| `fetch_daily_klines.py` | 币安公共 API 日 K 回退与校验，不是第一数据源 |
| `kline-2025-window-audit.json` | SSH 233 币从 2025-01-01 起的文件覆盖与实际上市起点审计 |
| `export_pg_bubblemaps_snapshots.py` | 将 PG 最新结构导出为本地兼容 Holder/关系/Cluster 快照 |
| `query_pg_bubblemaps_structures.py` | 使用 Factor_Factory 配置执行结构只读查询的隔离 helper |
| `import_pg_transfers.py` | 从 PG member view 严格导入并去重成员 Transfer |
| `audit_pg_transfer_window.py` | 只读审计 PG Transfer 在指定开始/结束日期内的覆盖 |
| `pg-transfer-2025-window-audit.json` | 233 币 PG Transfer 的 2025 窗口逐币覆盖结果 |
| `pg-transfer-2025-sample-validation-report.json` | Solana/EVM 日期过滤、边界和幂等实测 |
| `validate_pg_bubblemaps_exports.py` | 文件级校验 PG 导出的目标身份、JSON、计数和低关系边警告 |
| `pg-all233-validation-report.json` | 233 目标 PG-only 验证结果与逐币异常/警告 |
| `pg-transfer-sample-validation-report.json` | Solana/EVM PG Transfer 完整覆盖、幂等与大小写保持验证 |
| `capture_bubblemaps_structures.py` | PG/本地缺口的 Bubblemaps API 结构回退 |
| `capture_bubblemaps_transfers.py` | PG/本地缺口的普通成员历史 Transfer API 回退，断点续跑 |
| `build_address_inventory.py` | 全地址清单与 0.1% 高影响路径种子 |
| `build_arkham_label_queue.py` | 高影响端点队列、reviewed 结果保留 |
| `batch_review_pg_labels.py` | PostgreSQL 标签批量复用与 CEX 语义覆盖 |
| `batch_review_arkham_api.py` | 使用 `.env` API Key 只读批量复核地址标签、原子 checkpoint 与脱敏审计 |
| `web_review_cooldown.py` | 网页候选选择、失败冷却与审计 |
| `update_arkham_label.py` | 单地址串行原子落盘 |
| `record_multihop_review.py` | 多跳首次 CEX 边界审计记录 |
| `compute_cex_net_flows.py` | CEX 边界事件去重与逐日净流 |
| `build_data_status.py` | 覆盖率与标签状态报告 |
