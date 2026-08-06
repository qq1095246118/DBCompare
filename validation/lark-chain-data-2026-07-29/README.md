# Lark 链上文档验证

本目录用于复算 `docs/lark-chain-data-2026-07-29` 中的事件窗口结论。

采集使用项目原有入口，原始快照写入独立的 `/tmp/dbcompare-doc-validation-2026-07-29`
目录，避免覆盖项目默认正式产物。`events.json` 固化文档涉及的 16 个币种、33 个
链上目标和事件日期；`analyze_market_snapshot.py` 对成员视图中的重复转账去重，
统一计算 B28、W-1、事件和事件后窗口。

运行：

```bash
.venv/bin/python validation/lark-chain-data-2026-07-29/analyze_market_snapshot.py \
  --market-root /tmp/dbcompare-doc-validation-2026-07-29/full-market
```

生成的 `results.json` 保存机器可读明细，`report.md` 保存汇总表。金额均为 gross
transfer amount；它不等同净流入、真实成交量或新增资金。

`conclusion-validation.md` 在统一窗口结果之上，逐项核对文档中的关键数字，并按
“强支持、部分支持、无法完整复现”给出结论。

## 价格复核

`price_events.json` 固化有明确日期的链上异常信号；`analyze_price_events.py`
按文档中的精确合约地址读取 Binance Web3 DEX 日 K，覆盖不足时回退 Binance
Spot/USDT 或 USDⓈ-M。运行：

```bash
python3 validation/lark-chain-data-2026-07-29/analyze_price_events.py
```

生成的 `price-results.json` 保存行情、来源和机器可读收益，`price-event-report.md`
汇总信号日至事件日、事件日，以及事件后 1/3/7 天的涨跌。价格统一按 UTC 自然日；
日 K 只能验证时间先后和价格幅度，不能单独证明链上转账导致行情变化。

## 成交量缩小离场回测

`backtest_volume_peak.py` 回测“异常确认后下一日开盘以 2×杠杆买入；成交量创持仓
新高后首次缩小，于再下一日开盘卖出”的可执行日线版本：

```bash
python3 validation/lark-chain-data-2026-07-29/backtest_volume_peak.py
```

生成的 `backtest-volume-results.json` 保存逐笔交易与参数，
`backtest-volume-report.md` 保存汇总和逐笔表。回测使用固定 2×名义杠杆、每边
0.20% 手续费与滑点，并以盘中最低价触及入场价 50% 作为理想化强平条件。
该样本来自已知价格事件，未包含假阳性，属于条件事件回测而非样本外业绩。
