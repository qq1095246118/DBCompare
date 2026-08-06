# 20币扩展池数据采集与CEX标注状态

生成时间：2026-08-05T03:33:32.753792Z

## 总览

- 币安日K：20/20 个币完成，共 6600 根，截止 2026-08-02。
- Bubblemaps Holder/Cluster：20/20 完成。
- Bubblemaps 成员历史 transfers：2797/2831，覆盖率 98.80%；快照状态 `partial_success`。
- 高影响 Arkham 地址队列：761 个地址；本地已知 CEX 35，API/网页已复核 724（API 528，API CEX 70），确认 CEX 121，待复核 0。
- 当前直接 CEX 边界：6675 笔，覆盖 1914 个币种—日期；因 transfers 未全量，均为阶段性下界。

## Bubblemaps逐币覆盖

| 币种 | Holder | 边 | 成员历史 | 普通成员 | 覆盖率 | 唯一转账 |
|---|---:|---:|---:|---:|---:|---:|
| KGEN | 300 | 162 | 102 | 102 | 100.00% | 1513 |
| SAFE | 300 | 308 | 154 | 158 | 97.47% | 11729 |
| BTR | 300 | 190 | 136 | 136 | 100.00% | 1981 |
| BLESS | 300 | 146 | 104 | 104 | 100.00% | 2725 |
| SENT | 300 | 146 | 136 | 136 | 100.00% | 392 |
| TAG | 300 | 218 | 203 | 203 | 100.00% | 1045 |
| AIA | 300 | 200 | 140 | 141 | 99.29% | 3007 |
| LIGHT | 300 | 253 | 131 | 132 | 99.24% | 3374 |
| LAB | 300 | 305 | 150 | 151 | 99.34% | 2835 |
| JCT | 300 | 188 | 138 | 138 | 100.00% | 1930 |
| COLLECT | 300 | 171 | 145 | 145 | 100.00% | 955 |
| BLUAI | 300 | 424 | 160 | 187 | 85.56% | 8745 |
| GUA | 300 | 287 | 189 | 189 | 100.00% | 9299 |
| HEMI | 300 | 190 | 138 | 138 | 100.00% | 1014 |
| AIO | 300 | 266 | 122 | 122 | 100.00% | 9890 |
| IDOL | 300 | 86 | 70 | 70 | 100.00% | 969 |
| B2 | 300 | 251 | 179 | 179 | 100.00% | 6837 |
| FF | 300 | 168 | 92 | 92 | 100.00% | 426 |
| HUMA | 300 | 214 | 149 | 149 | 100.00% | 2202 |
| ZBT | 300 | 217 | 159 | 159 | 100.00% | 7670 |

## 口径

- 日K来自币安官方合约连续K线接口，保存OHLC、成交量、成交额、成交笔数和主动买入量。
- Holder、Subgraph与成员历史转账来自Bubblemaps官方网页使用的接口；采集器按服务端429的`retry_after`自动冷却并断点续跑。
- Arkham只用于地址身份与实体标签。`reviewed_web_unlabeled`与`reviewed_arkham_api_unlabeled`表示对应来源未返回实体标签，不等于确定非CEX。
- CEX净流为`转入CEX - 从CEX转出`；直接边按交易哈希、方向、金额去重。多跳只有在保存最终CEX边界交易哈希后才可纳入，路径中间跳不计金额。
- transfers覆盖未达到100%前，事件数量和CEX流量只能作为已观测下界，不得用于正式IC或回测结论。

## 主要产物

- `klines-1d/`：20币日K CSV与manifest。
- `bubblemaps-snapshot/`：Holder、关系边、Cluster与逐成员历史转账。
- `arkham-review/all-transfer-addresses.csv`：所有已观测转账地址。
- `arkham-review/high-impact-path-seeds.csv`：Cluster余额0.1%以上的路径种子。
- `arkham-review/arkham-label-queue.csv`：可审计Arkham标签队列。
- `arkham-review/arkham-api-review-state.json`：脱敏的Arkham API逐地址查询审计状态。
- `cex-flow/direct-cex-events.csv`：直接CEX边界事件。
- `cex-flow/daily-cex-net-flows.csv`：逐日CEX流入、流出、净流。
