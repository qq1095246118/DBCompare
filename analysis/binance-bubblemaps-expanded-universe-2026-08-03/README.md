# 第二批20币扩展池

专用数据 Agent 的标准操作流程见
[`DATA_ACQUISITION_AND_CEX_LABELING_SOP.md`](DATA_ACQUISITION_AND_CEX_LABELING_SOP.md)。

> 2026-08-05 扩池更新：新的 233 个候选已全部写入
> `screening/binance-futures-small-meme-volume-spikes-2026-08-05/all-233-expansion-registry.json`。
> 新批次按 SOP 2.1 执行：K 线优先读取 SSH Binance Vision，Bubblemaps 结构和 Transfer
> 优先读取 PostgreSQL；本目录原 `expanded_universe_config.json` 仍只是旧 20 币配置。
> 统一研究窗口已前推到 `2025-01-01 UTC`；各币从 `max(2025-01-01, 实际合约首根日K)`
> 开始，上市前不补值。当前统一截止日为 `2026-08-03 UTC`。

## 结果

- 原观测池：13个币。
- 本次新增：20个币。
- 扩展后目标池：33个币。
- 筛选日期：2026-08-03；市值和24小时成交额沿用2026-07-30候选池截面。
- 新增币均能通过币安合约日K接口返回历史数据，并通过 Bubblemaps Top Holders 与 Subgraph 探测。

## 筛选标准

1. 排除现有13币：SIREN、RAVE、BIRB、VELVET、DEXE、SOON、ESPORTS、KOMA、CYS、BULLA、EVAA、GWEI、CLO。
2. 来自现有币安小市值、高波动候选库，合约日K历史至少180天。
3. 历史上至少出现4段“单日收盘涨跌绝对值达到20%”的事件。
4. Bubblemaps Top Holders 和 Subgraph 可用。
5. Subgraph至少50条关系边，普通Cluster成员至少50个，避免只因API返回成功就把覆盖极弱的币加入池中。

## 新增20币

| # | 币种 | 项目 | 链 | 候选池市值 | 日K数 | ≥20%事件段 | Subgraph边 | 普通Cluster成员 |
|---:|---|---|---|---:|---:|---:|---:|---:|
| 1 | KGEN | KGeN | BSC | $14.21M | 300 | 4 | 152 | 88 |
| 2 | SAFE | Safe Token | Ethereum | $95.73M | 647 | 7 | 308 | 158 |
| 3 | BTR | BTR token | BSC | $3.16M | 341 | 7 | 190 | 136 |
| 4 | BLESS | Bless | BSC | $6.84M | 314 | 7 | 146 | 104 |
| 5 | SENT | Sentient | BSC | $5.60M | 262 | 4 | 144 | 135 |
| 6 | TAG | Tagger | BSC | $468.51M | 374 | 14 | 218 | 203 |
| 7 | AIA | DeAgentAI | BSC | $12.96M | 195 | 7 | 200 | 141 |
| 8 | LIGHT | Bitlight Labs | BSC | $53.35M | 310 | 7 | 253 | 132 |
| 9 | LAB | LAB | BSC | $146.70M | 290 | 10 | 305 | 151 |
| 10 | JCT | Janction | BSC | $40.49M | 266 | 10 | 188 | 138 |
| 11 | COLLECT | Collect on Fanable | BSC | $153.61M | 215 | 8 | 171 | 145 |
| 12 | BLUAI | Bluwhale | BSC | $118.28M | 286 | 10 | 424 | 187 |
| 13 | GUA | SUPERFORTUNE | BSC | $48.82M | 225 | 6 | 297 | 199 |
| 14 | HEMI | Hemi | BSC | $3.40M | 339 | 6 | 193 | 138 |
| 15 | AIO | OLAXBT | BSC | $97.79M | 355 | 12 | 264 | 122 |
| 16 | IDOL | MEET48 Token | BSC | $72.43M | 395 | 7 | 81 | 65 |
| 17 | B2 | BSquared Network | BSC | $98.29M | 454 | 7 | 248 | 179 |
| 18 | FF | Falcon Finance | BSC | $33.61M | 308 | 4 | 167 | 89 |
| 19 | HUMA | Huma Finance | BSC | $9.22M | 434 | 7 | 229 | 156 |
| 20 | ZBT | ZEROBASE | BSC | $13.22M | 290 | 6 | 217 | 159 |

## 被继续排除的候选

| 币种 | 原因 |
|---|---|
| ZAMA | 只有2段≥20%事件，历史事件不足 |
| UB | 虽支持Bubblemaps，但只有46条边、34个普通Cluster成员 |
| SKYAI | 虽支持Bubblemaps，但只有22条边、10个普通Cluster成员 |
| HOLO | 只有2段≥20%事件，且Cluster普通成员覆盖弱 |
| NIGHT | 只有3段≥20%事件 |
| TURTLE | 只有2段≥20%事件 |
| OPEN | 只有3段≥20%事件 |

## 当前完成度

扩池后的数据流水线已经建立并开始落盘：

1. 20币币安日K已完成并校验到2026-08-02；
2. 20币 Bubblemaps Holder、Subgraph和Cluster结构已完成；
3. Cluster成员历史转账使用可断点续跑采集器持续补齐，并遵守服务端429返回的冷却时间；
4. 已生成全部已观测地址、高影响路径和Arkham网页复核队列；
5. 已生成直接CEX边界事件与逐日CEX流入、流出和净流表；
6. Arkham多跳结果只有在保存最终CEX边界交易哈希后才纳入，路径中间跳不重复计量。

精确覆盖率和标签进度见 `DATA_PIPELINE_STATUS.md`。在该文件显示 transfers
覆盖100%、多跳队列完成前，阶段性CEX流量只能视为已观测下界，不能进入正式IC或回测。

不能把当前13币的 `+190.94%` 结果直接视为33币扩展池结果。

## 文件

- `expanded_universe_config.json`：后续Bubblemaps采集使用的20币目标配置。
- `expanded-pool-probe-results.json`：全部探测结果和被排除原因。
- `probe_expanded_pool.py`：可复现的扩池探测脚本。
- `fetch_daily_klines.py` / `klines-1d/`：币安日K采集器和20币CSV。
- `export_pg_bubblemaps_snapshots.py`：按精确链与 Token 地址导出 PostgreSQL 最新结构快照。
- `import_pg_transfers.py`：优先导入 PostgreSQL Cluster 成员 Transfer，并与已有事件去重。
- `validate_pg_bubblemaps_exports.py` / `pg-all233-validation-report.json`：233 目标 PG-only 文件级验证工具和结果。
- `capture_bubblemaps_structures.py`：Holder、Subgraph和Cluster结构采集器。
- `capture_bubblemaps_transfers.py`：按成员余额优先、跨币轮询、可断点续跑的历史转账采集器。
- `build_address_inventory.py`：全部已观测转账地址和高影响路径生成器。
- `build_arkham_label_queue.py` / `update_arkham_label.py`：Arkham复核队列及单地址审计更新工具。
- `compute_cex_net_flows.py`：直接CEX事件和逐日净流生成器。
- `build_data_status.py`：生成当前采集/标签覆盖状态。
