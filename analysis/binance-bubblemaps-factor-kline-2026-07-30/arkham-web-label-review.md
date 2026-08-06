# Arkham 网页地址标签与巨额转账路径复核

复核日期：2026-07-31
数据来源：已登录的 Arkham 网页地址页、交易页、Exchange Usage 与可见转账列表。
明细文件：`arkham-web-label-queue.csv`

## 汇总结论

- 共复核 13 条事件，全部完成地址级标签。
- 3 条事件确认存在中心化交易所流入：
  - RAVE：20,000,000 RAVE 一跳进入 Bitget。
  - BIRB：至少 852,188 BIRB 一跳进入 Bitget/Bybit。
  - ESPORTS：1,526,000 ESPORTS 两跳进入 Kraken。
- 其余 10 条事件没有观察到对应代币进入中心化交易所：
  - 2 条 BIRB 出现大额原路返回。
  - 2 条 DEXE 属于 DeXe 合约与 Gnosis Safe 内部路径。
  - 1 条 DEXE 出现 Wormhole 跨链桥路径。
  - 3 条 ESPORTS 主要表现为继续分层转账和少量 PancakeSwap 交易。
  - 1 条 ESPORTS 目标地址只观察到少量 PancakeSwap 交易。
  - 1 条 EVAA 主要使用 PancakeSwap/PinkLock，未发现中心化交易所入金。

因此，单独出现“Cluster 向外巨额转账”不能直接解释为卖出。只有继续追踪到 Arkham 已标记的交易所充值地址，才能认定为入所；进入 Gnosis Safe、项目合约、跨链桥、DEX 或未标记中转地址应分别建模。

## 逐事件结果

| 代币 | 日期 | 原始金额 | 路径结论 | 已确认入所金额 |
|---|---:|---:|---|---:|
| RAVE | 2026-04-21 | 20,000,000 | 未标记地址 → Bitget Deposit | 20,000,000 RAVE |
| BIRB | 2026-03-04 | 6,705,935.51 | 未标记地址；未观察到交易所标签 | 0 |
| BIRB | 2026-05-06 | 9,354,005.2763 | 后续 4,136,000 原路返回 | 0 |
| BIRB | 2026-06-05 | 9,191,366.5 | 后续 4,802,000 原路返回 | 0 |
| BIRB | 2026-07-10 | 6,600,000 | 未标记地址 → Bitget/Bybit Deposit | 至少 852,188 BIRB |
| DEXE | 2026-02-06 | 493,771 | DeXe GovUserKeeper → 中转地址 → Gnosis Safe | 0 |
| DEXE | 2026-02-13 | 490,412 | DeXe GovUserKeeper → 中转地址 → Gnosis Safe | 0 |
| DEXE | 2026-07-27 | 403,184 | 281,327 转往未标记地址；136,066 进入 Wormhole | 0 |
| ESPORTS | 2026-03-14 | 68,900,000 | 目标地址仅观察到约 129,581 ESPORTS 的 PancakeSwap 交易 | 0 |
| ESPORTS | 2026-05-27 | 26,333,333 | 整额分成 10M + 16.333M 继续转出；下游仅观察到 4,913 ESPORTS 的 PancakeSwap 交易 | 0 |
| ESPORTS | 2026-06-21 | 13,333,333 | 整额中转并进一步拆分，其中 1.526M 进入 Kraken Deposit | 1,526,000 ESPORTS |
| ESPORTS | 2026-07-21 | 13,569,999 | 整额中转后继续分散；约 36K ESPORTS 进入 PancakeSwap | 0 |
| EVAA | 2026-04-21 | 5,000,000 | 地址使用 PancakeSwap/PinkLock；未观察到中心化交易所入金 | 0 |

## 口径与限制

- “入所金额”只统计对应事件代币转入 Arkham 已标记的中心化交易所充值地址或热钱包的金额。
- 地址存在 Binance、Bitget、OKX 等标签，不代表目标代币入所；必须核对具体转账资产。例如部分 ESPORTS 地址的交易所记录实际是 USDC、USDT 或 BNB。
- “未观察到入所”不等于链上绝对不存在。资金可能继续经过未标记地址、跨链桥或超过当前可见追踪层数。
- EVAA 队列中的原始交易哈希未出现在 Arkham 当前过滤后的地址列表中，因此该条是地址级行为复核，不是原始交易逐笔闭环。
- 网页标签会更新，建议数据中台保存每次标签查询时间、标签来源、路径层数和证据交易哈希。
