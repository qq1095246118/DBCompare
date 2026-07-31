# CEX 净流入/净流出因子设计

日期：2026-07-31

## 目标

在现有 13 个样本、逐日日 K 与 Bubblemaps 转账数据上，把 CEX 净流入和净流出独立建模为可比较、可高亮、可计算 T+N IC 的方向因子，并加入现有自包含 HTML。

本次新增两个因子：

- `CEX-I`：CEX 净流入强度，代表代币净流向交易所。
- `CEX-O`：CEX 净流出强度，代表代币净流出交易所。

## 数据与方向分类

沿用现有 CEX 优先规则：

1. 任一端被标记为 CEX 时，优先划为 CEX 相关转账。
2. 非 CEX 地址转入 CEX 为 `流入CEX`。
3. CEX 转向非 CEX 地址为 `流出CEX`。
4. CEX 到 CEX 为 `CEX间转移`，只计入 CEX 相关笔数，不计入流入或流出。
5. 两端都不是 CEX 时，才继续判断 Cluster 内部、跨 Cluster 或 Cluster 与外部。

每日 D 的全部因子只使用 D-7 至 D-1 的转账。历史基线使用再往前的四个互不重叠七日窗口，禁止使用 D 日及以后数据。

## 原始统计量

对每个币种、每个交易日 D 计算：

- `cex_inflow_7d`：D-7 至 D-1 的 CEX 流入总量。
- `cex_outflow_7d`：D-7 至 D-1 的 CEX 流出总量。
- `cex_net_signed_7d = cex_inflow_7d - cex_outflow_7d`。
- `cex_net_inflow_7d = max(cex_net_signed_7d, 0)`。
- `cex_net_outflow_7d = max(-cex_net_signed_7d, 0)`。
- `cex_transfer_count_7d`：同窗口内的全部 CEX 相关转账笔数，包括 CEX 间转移。
- `cex_labels_7d`：窗口内命中的 CEX 标签集合。

原始代币数量只用于单币解释，不直接用于跨币 IC。

## 因子公式

设当前快照中的 Cluster 合计余额为 `cluster_amount`。

### CEX-I 净流入强度

- 规模占比：
  `cex_i_share = cex_net_inflow_7d / cluster_amount`
- 前四周基线：
  `cex_i_baseline = median(previous_4_week_cex_net_inflow)`
- 历史放大：
  `cex_i_burst = cex_net_inflow_7d / cex_i_baseline`

### CEX-O 净流出强度

- 规模占比：
  `cex_o_share = cex_net_outflow_7d / cluster_amount`
- 前四周基线：
  `cex_o_baseline = median(previous_4_week_cex_net_outflow)`
- 历史放大：
  `cex_o_burst = cex_net_outflow_7d / cex_o_baseline`

### 零基线

如果历史中位数为零：

- 当前值也为零：放大值记为 `0`，不触发。
- 当前值大于零：标记为“从零启动”；连续放大值在数据中保留为零基线状态，不伪造有限倍数。

## 异常高亮

`CEX-I` 或 `CEX-O` 分别满足以下条件时高亮：

1. 当前方向净流占 Cluster 合计余额至少 `0.1%`；
2. 同时满足下列任一项：
   - 历史基线大于零，当前值达到历史中位数的 `3×`；
   - 历史基线为零且当前值大于零，即“从零启动”。

流入与流出分别判断，因此同一天最多只有一个净方向因子触发；原始总流入和总流出仍可同时非零。

## HTML 展示

在现有页面中：

1. 日 K 悬停详情新增 `CEX-I` 与 `CEX-O` 卡片。
2. 卡片显示：
   - 方向净流量；
   - Cluster 占比；
   - 历史放大倍数或“从零启动”；
   - 异常阈值是否命中；
   - CEX 标签和转账笔数。
3. 命中时使用与现有因子一致的异常边框和徽标。
4. 日线总信号标记把 CEX 因子纳入“当日存在异常”的判断。
5. 页面底部新增两个因子的公式、解释、阈值和误读边界。
6. 页面继续显示总 CEX 流入、总 CEX 流出和有符号净流。

## IC 统计

新增独立 CSV 和 Markdown 报告，覆盖以下因子变体：

- `share`：Cluster 占比连续值。
- `burst`：历史放大连续值；零基线从零启动单独编码，避免把无穷大直接参与排序。
- `trigger`：异常触发二元值。

分别统计：

- `CEX-I` 和 `CEX-O`；
- 样本内、样本外、全部样本；
- T+1、T+3、T+5、T+7、T+14、T+30；
- 平均 Rank IC、中位 Rank IC、IC 标准差、Newey-West t 值、正 IC 比率、有效 IC 天数和观测数。

前瞻收益为：

`close[D+N] / close[D] - 1`

IC 保留原始正负方向，不预设 CEX 净流入必跌或 CEX 净流出必涨。

## 样本分组

- 样本内：SIREN、RAVE、BIRB、VELVET、DEXE。
- 样本外：SOON、ESPORTS、KOMA、CYS、BULLA、EVAA、GWEI、CLO。
- 全部样本：以上 13 个币种。

## 测试与验收

实现采用测试先行，至少覆盖：

1. 非 CEX → CEX 正确计为流入。
2. CEX → 非 CEX 正确计为流出。
3. CEX → CEX 不计入流入或流出。
4. 同窗口同时有流入和流出时，净方向和正值拆分正确。
5. 前四周基线只读取 D-35 至 D-8。
6. 零基线的未触发和从零启动逻辑正确。
7. 占比与 `3× + 0.1%` 双重阈值正确。
8. 13 个样本全部进入 HTML 和 IC 分组。
9. HTML 数据仍然只使用 D-1 及以前的链上信息。
10. 生成脚本、IC 脚本、CSV、Markdown 与 HTML 可重复生成。

## 已知限制

- Cluster 合计余额来自现有快照，并非逐日历史余额；因此占比是基于当前可用分母的近似值。
- CEX 地址标签覆盖不完整，未被识别的交易所地址会落入未知地址。
- 地址标签来自当前标签快照；报告需注明其回填性质。
- CEX 净流入通常可解释为潜在卖压，但也可能是做市、跨平台调拨或托管迁移，不能仅凭该因子直接下交易结论。
- CEX 净流出也可能流向 DEX、项目方或其他托管地址，不必然代表买入或持币。
