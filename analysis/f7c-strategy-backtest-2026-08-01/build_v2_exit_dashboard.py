#!/usr/bin/env python3
"""Build an interactive HTML dashboard for the V2 intraday exit strategy."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import backtest_multitimeframe_exit as model


HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "intraday-all-data"
OUTPUT = HERE / "v2-exit-strategy-dashboard.html"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def number(value: float) -> float:
    return float(f"{value:.8g}")


def build_case_payload(
    case: dict[str, Any],
    v2_trade: dict[str, str],
    v1_trade: dict[str, str],
    old_trade: dict[str, str],
    events: list[dict[str, str]],
) -> dict[str, Any]:
    context = model.prepare_context(case)
    rows = context["rows"]
    five = rows["5m"]
    entry_time = datetime.fromisoformat(case["entry_date"]).replace(tzinfo=timezone.utc)
    entry_ms = int(entry_time.timestamp() * 1000)
    end_ms = int((entry_time + timedelta(days=14)).timestamp() * 1000)
    entry_index = next(i for i, row in enumerate(five) if row["open_time_ms"] >= entry_ms)
    pointers = {"15m": -1, "1h": -1, "4h": -1}
    for interval in pointers:
        pointers[interval] = model.latest_completed(rows[interval], -1, entry_ms - 1)
    events_at: dict[int, list[dict[str, str]]] = {}
    for event in events:
        events_at.setdefault(int(event["execution_open_time_ms"]), []).append(event)
    cumulative_sold = 0.0
    running_high = float(case["entry_price"])
    bars = []
    for index in range(entry_index, len(five)):
        bar = five[index]
        time_ms = int(bar["open_time_ms"])
        if time_ms >= end_ms:
            break
        for event in events_at.get(time_ms, []):
            cumulative_sold = float(event["cumulative_sold_fraction"])
        running_high = max(running_high, bar["high"])
        features = model.multi_timeframe_features(context, pointers, index, running_high)
        bars.append(
            [
                time_ms,
                number(bar["open"]),
                number(bar["close"]),
                number(bar["low"]),
                number(bar["high"]),
                number(bar["volume"]),
                number(running_high / float(case["entry_price"]) - 1),
                number(features["drawdown"]),
                number(features["score"]),
                int(features["failure15"]),
                int(features["weak1h"]),
                {"weak": -1, "neutral": 0, "strong": 1}[features["regime4h"]],
                int(features["weak4h_confirmed"]),
                number(cumulative_sold),
            ]
        )
    peak_bar = max(bars, key=lambda row: row[4])
    return {
        "id": case["case_id"],
        "symbol": case["symbol"],
        "group": old_trade["group"],
        "signalDate": case["signal_date"],
        "entryDate": case["entry_date"],
        "entryMs": entry_ms,
        "entryPrice": float(case["entry_price"]),
        "exitMs": max(int(event["execution_open_time_ms"]) for event in events),
        "peakMs": peak_bar[0],
        "peakPrice": peak_bar[4],
        "mfe": float(v2_trade["theoretical_14d_mfe_1x"]),
        "capture": float(v2_trade["mfe_capture_ratio"]),
        "v2Return2x": float(v2_trade["net_return_2x"]),
        "v1Return2x": float(v1_trade["net_return_2x"]),
        "oldReturn2x": float(v2_trade["old_net_return_2x"]),
        "oldExitMs": int(datetime.fromisoformat(old_trade["exit_date"]).replace(tzinfo=timezone.utc).timestamp() * 1000),
        "oldExitPrice": float(old_trade["exit_price"]),
        "events": [
            {
                "decisionMs": int(event["decision_close_time_ms"]) if event["decision_close_time_ms"] else None,
                "executionMs": int(event["execution_open_time_ms"]),
                "price": float(event["execution_price"]),
                "sold": float(event["sold_fraction"]),
                "cumulative": float(event["cumulative_sold_fraction"]),
                "reason": event["reason"],
                "score": float(event["score"]) if event["score"] else None,
                "drawdown": float(event["drawdown"]) if event["drawdown"] else None,
                "mfe": float(event["mfe_at_decision"]) if event["mfe_at_decision"] else None,
                "failure15": event["failure15"] == "True",
                "weak1h": event["weak1h"] == "True",
                "regime4h": event["regime4h"],
                "weak4hConfirmed": event.get("weak4h_confirmed") == "True",
            }
            for event in events
        ],
        "bars": bars,
    }


def build_payload() -> dict[str, Any]:
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    old_trades = read_csv(HERE / "trades.csv")
    v1_trades = {row["case_id"]: row for row in read_csv(HERE / "multitimeframe-exit-trades.csv")}
    v2_trades = {row["case_id"]: row for row in read_csv(HERE / "multitimeframe-exit-v2-trades.csv")}
    grouped_events: dict[str, list[dict[str, str]]] = {}
    for event in read_csv(HERE / "multitimeframe-exit-v2-events.csv"):
        grouped_events.setdefault(event["case_id"], []).append(event)
    cases = []
    for case, old_trade in zip(manifest["cases"], old_trades, strict=True):
        cases.append(
            build_case_payload(
                case,
                v2_trades[case["case_id"]],
                v1_trades[case["case_id"]],
                old_trade,
                grouped_events[case["case_id"]],
            )
        )

    portfolio: dict[str, list[list[float]]] = {}
    grouped_curve: dict[str, list[dict[str, str]]] = {}
    for row in read_csv(HERE / "weighted-portfolio-v2-equity.csv"):
        grouped_curve.setdefault(row["strategy"], []).append(row)
    for strategy, rows in grouped_curve.items():
        sampled = rows[::12]
        if sampled[-1] is not rows[-1]:
            sampled.append(rows[-1])
        portfolio[strategy] = [
            [int(datetime.fromisoformat(row["time_utc"]).timestamp() * 1000), number(float(row["equity"]))]
            for row in sampled
        ]

    def curve_stats(strategy: str) -> dict[str, float | int]:
        rows = grouped_curve[strategy]
        values = [float(row["equity"]) for row in rows]
        peak = values[0]
        peak_row = rows[0]
        max_drawdown = 0.0
        mdd_peak_row = rows[0]
        trough_row = rows[0]
        for row, value in zip(rows, values):
            if value > peak:
                peak = value
                peak_row = row
            drawdown = value / peak - 1
            if drawdown < max_drawdown:
                max_drawdown = drawdown
                mdd_peak_row = peak_row
                trough_row = row
        return {
            "return": values[-1] - 1,
            "maxDrawdown": max_drawdown,
            "peakMs": int(datetime.fromisoformat(mdd_peak_row["time_utc"]).timestamp() * 1000),
            "troughMs": int(datetime.fromisoformat(trough_row["time_utc"]).timestamp() * 1000),
            "peakEquity": float(mdd_peak_row["equity"]),
            "troughEquity": float(trough_row["equity"]),
        }

    old_stats = curve_stats("old_daily")
    v1_stats = curve_stats("v1_multitimeframe")
    v2_stats = curve_stats("v2_multitimeframe")
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "cases": cases,
        "portfolio": portfolio,
        "summary": {
            "oldReturn": number(old_stats["return"]),
            "v1Return": number(v1_stats["return"]),
            "v2Return": number(v2_stats["return"]),
            "v2MaxDrawdown": number(v2_stats["maxDrawdown"]),
            "v2MddPeakMs": v2_stats["peakMs"],
            "v2MddTroughMs": v2_stats["troughMs"],
            "v2MddPeakEquity": number(v2_stats["peakEquity"]),
            "v2MddTroughEquity": number(v2_stats["troughEquity"]),
        },
    }


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>V2多周期退出策略 · 5m复盘</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js"></script>
  <style>
    :root{color-scheme:dark;--bg:#081019;--panel:#101b27;--panel2:#142231;--text:#e9f1f8;--muted:#8fa3b7;--line:#26394b;--up:#22c993;--down:#ff647c;--blue:#61a8ff;--gold:#f6c85f;--purple:#ad8cff}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 12% -10%,#193651 0,transparent 32%),var(--bg);color:var(--text);font:14px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.app{max-width:1600px;margin:auto;padding:22px}.top{display:flex;justify-content:space-between;align-items:end;gap:16px;flex-wrap:wrap}.eyebrow{color:var(--blue);font-size:12px;letter-spacing:.13em;text-transform:uppercase}.title{font-size:27px;margin:2px 0;font-weight:650}.sub{color:var(--muted)}
    .metrics{display:grid;grid-template-columns:repeat(4,minmax(145px,1fr));gap:10px;margin:18px 0}.metric,.panel{background:linear-gradient(145deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:14px}.metric{padding:12px 14px}.metric span{display:block;color:var(--muted);font-size:12px}.metric strong{display:block;font-size:21px;margin-top:3px}.positive{color:var(--up)}.negative{color:var(--down)}
    .controls{display:flex;align-items:end;gap:10px;flex-wrap:wrap;margin:13px 0}.field{display:grid;gap:4px}.field label{font-size:12px;color:var(--muted)}select,button{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit}button{cursor:pointer}button:hover{border-color:var(--blue)}button.active{background:var(--blue);color:#07111c;border-color:var(--blue)}
    .panel{overflow:hidden}.panel-head{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:10px 13px;border-bottom:1px solid var(--line);flex-wrap:wrap}.legend{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:12px}.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}.chart{height:720px;width:100%}.equity{height:270px;width:100%}.section{margin-top:20px}.detail{padding:10px 13px;border-top:1px solid var(--line);display:flex;gap:16px;flex-wrap:wrap;color:var(--muted)}.detail b{color:var(--text)}
    .table-wrap{overflow:auto;border:1px solid var(--line);border-radius:13px;background:rgba(16,27,39,.88)}table{border-collapse:collapse;width:100%;min-width:1100px}th,td{padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:left}th{font-size:12px;color:var(--muted);font-weight:500;background:var(--panel);position:sticky;top:0}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}tbody tr{cursor:pointer}tbody tr:hover,tbody tr.selected{background:rgba(97,168,255,.09)}.tag{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:1px 7px;font-size:11px}.help{color:var(--muted);font-size:12px}
    @media(max-width:800px){.app{padding:12px}.metrics{grid-template-columns:repeat(2,1fr)}.chart{height:620px}.title{font-size:22px}}
  </style>
</head>
<body>
<main class="app">
  <header class="top"><div><div class="eyebrow">Causal 5m next-open execution</div><h1 class="title">V2多周期退出策略 · 5m复盘</h1><div class="sub">5m执行 · 15m/1h确认 · 4h趋势 · 分层减仓 · 极端MFE runner动态保护</div></div><div class="help">滚轮缩放 · 拖动平移 · 悬停查看评分</div></header>
  <section class="metrics"><div class="metric"><span>所选交易V2收益 · 2×</span><strong id="mV2"></strong></div><div class="metric"><span>V1 / 原日线 · 2×</span><strong id="mCompare"></strong></div><div class="metric"><span>14日MFE / 捕获率</span><strong id="mMfe"></strong></div><div class="metric"><span>退出批次</span><strong id="mEvents"></strong></div></section>
  <div class="controls"><div class="field"><label for="caseSelect">交易样本</label><select id="caseSelect"></select></div><div class="field"><label>显示范围</label><div><button id="btnAll" class="active">完整14日</button> <button id="btnHold">入场至退出</button> <button id="btnPeak">峰值前后48h</button></div></div></div>
  <section class="panel"><div class="panel-head"><div><b id="caseTitle"></b><div class="help" id="caseSub"></div></div><div class="legend"><span><i style="background:var(--blue)"></i>入场</span><span><i style="background:var(--down)"></i>V2分批卖出</span><span><i style="background:var(--gold)"></i>原日线卖出</span><span><i style="background:var(--purple)"></i>事后最高点</span></div></div><div id="mainChart" class="chart"></div><div id="detail" class="detail"></div></section>
  <section class="section panel"><div class="panel-head"><div><b>非等权组合权益</b><div class="help" id="equitySub">每次30%保证金 · 单仓2× · 最多3仓 · 1小时采样展示</div></div></div><div id="equityChart" class="equity"></div></section>
  <section class="section"><div class="panel-head" style="border:0;padding-left:0"><b>全部26笔交易</b><span class="help">点击定位</span></div><div class="table-wrap"><table><thead><tr><th>#</th><th>币种</th><th>信号日</th><th>退出时刻 UTC</th><th class="num">批次</th><th class="num">V2 2×</th><th class="num">V1 2×</th><th class="num">原日线 2×</th><th class="num">14日MFE</th><th class="num">捕获率</th></tr></thead><tbody id="tradeRows"></tbody></table></div></section>
</main>
<script>
const DATA=__PAYLOAD__;
const caseMap=new Map(DATA.cases.map(c=>[c.id,c]));
let selected=caseMap.has("20-VELVET-2026-06-06")?"20-VELVET-2026-06-06":DATA.cases[0].id;
let rangeMode="all";
const main=echarts.init(document.getElementById("mainChart"));
const equity=echarts.init(document.getElementById("equityChart"));
const $=id=>document.getElementById(id);
const pct=(v,d=2)=>`${v>=0?"+":""}${(v*100).toFixed(d)}%`;
const num=(v,d=6)=>Number(v).toLocaleString(undefined,{maximumFractionDigits:d});
const dt=ms=>new Date(ms).toISOString().replace("T"," ").slice(0,16);
function regime(v){return v===1?"强":v===-1?"弱":"中性"}
function init(){
  DATA.cases.forEach(c=>{const o=document.createElement("option");o.value=c.id;o.textContent=`${c.symbol} · ${c.signalDate} · V2 ${pct(c.v2Return2x)}`;$('caseSelect').appendChild(o)});$('caseSelect').value=selected;
  $('caseSelect').addEventListener('change',e=>{selected=e.target.value;rangeMode='all';setButtons();render()});
  [['btnAll','all'],['btnHold','hold'],['btnPeak','peak']].forEach(([id,mode])=>$(id).addEventListener('click',()=>{rangeMode=mode;setButtons();renderMain()}));
  renderTable();renderEquity();render();window.addEventListener('resize',()=>{main.resize();equity.resize()});
}
function setButtons(){[['btnAll','all'],['btnHold','hold'],['btnPeak','peak']].forEach(([id,m])=>$(id).classList.toggle('active',m===rangeMode))}
function rangeFor(c){if(rangeMode==='hold')return [c.entryMs-6*3600000,c.exitMs+6*3600000];if(rangeMode==='peak')return [c.peakMs-48*3600000,c.peakMs+48*3600000];return [c.bars[0][0],c.bars[c.bars.length-1][0]]}
function render(){const c=caseMap.get(selected);$('mV2').textContent=pct(c.v2Return2x);$('mV2').className=c.v2Return2x>=0?'positive':'negative';$('mCompare').textContent=`${pct(c.v1Return2x)} / ${pct(c.oldReturn2x)}`;$('mMfe').textContent=`${pct(c.mfe)} / ${pct(c.capture)}`;$('mEvents').textContent=`${c.events.length} 次`;$('caseTitle').textContent=`${c.symbol} · ${c.signalDate}`;$('caseSub').textContent=`${c.group} · 入场 ${dt(c.entryMs)} @ ${num(c.entryPrice)} · V2退出 ${dt(c.exitMs)}`;renderMain();document.querySelectorAll('#tradeRows tr').forEach(tr=>tr.classList.toggle('selected',tr.dataset.id===selected))}
function renderMain(){
  const c=caseMap.get(selected),bars=c.bars,times=bars.map(b=>b[0]),ohlc=bars.map(b=>[b[1],b[2],b[3],b[4]]),vol=bars.map(b=>b[5]),score=bars.map(b=>b[8]),dd=bars.map(b=>-b[7]*100),sold=bars.map(b=>b[13]*100),r=rangeFor(c);
  const exitData=c.events.map((e,i)=>({value:[e.executionMs,e.price],symbol:'triangle',symbolRotate:180,symbolSize:13,itemStyle:{color:'#ff647c'},label:{show:true,formatter:`卖${Math.round(e.sold*100)}%`,position:'top',color:'#ff9aaa',fontSize:10},event:e}));
  const marks=[{coord:[c.entryMs,c.entryPrice],value:'入场',itemStyle:{color:'#61a8ff'},label:{show:true,formatter:'入场',color:'#8fc0ff'}},{coord:[c.oldExitMs,c.oldExitPrice],value:'原退出',symbol:'diamond',itemStyle:{color:'#f6c85f'},label:{show:true,formatter:'原退出',color:'#f6c85f'}},{coord:[c.peakMs,c.peakPrice],value:'MFE峰值',symbol:'pin',itemStyle:{color:'#ad8cff'},label:{show:true,formatter:'MFE峰值',color:'#c8b7ff'}}];
  main.setOption({animation:false,backgroundColor:'transparent',axisPointer:{link:[{xAxisIndex:'all'}]},tooltip:{trigger:'axis',axisPointer:{type:'cross'},backgroundColor:'#0b1621',borderColor:'#26394b',textStyle:{color:'#e9f1f8'},formatter:ps=>{const i=ps[0]?.dataIndex??0,b=bars[i],ev=c.events.filter(e=>e.executionMs===b[0]);return `<b>${c.symbol} · ${dt(b[0])} UTC</b><br>O ${num(b[1])} · H ${num(b[4])} · L ${num(b[3])} · C ${num(b[2])}<br>量 ${num(b[5],2)} · MFE ${pct(b[6])} · 峰值回撤 ${pct(-b[7])}<br>退出评分 <b>${b[8].toFixed(3)}</b> · 15m失败 ${b[9]?'是':'否'} · 1h弱 ${b[10]?'是':'否'}<br>4h ${regime(b[11])}${b[12]?'（连续确认）':''} · 已卖 ${Math.round(b[13]*100)}%${ev.map(e=>`<br><span style="color:#ff9aaa">卖${Math.round(e.sold*100)}%：${e.reason}</span>`).join('')}`}},grid:[{left:65,right:72,top:32,height:'49%'},{left:65,right:72,top:'57%',height:'12%'},{left:65,right:72,top:'73%',height:'18%'}],xAxis:[0,1,2].map((_,i)=>({type:'time',gridIndex:i,min:r[0],max:r[1],axisLabel:{show:i===2,color:'#8fa3b7'},axisLine:{lineStyle:{color:'#26394b'}},splitLine:{show:false}})),yAxis:[{scale:true,gridIndex:0,position:'right',axisLabel:{color:'#8fa3b7'},splitLine:{lineStyle:{color:'#1c2d3d'}}},{scale:true,gridIndex:1,position:'right',axisLabel:{color:'#8fa3b7'},splitLine:{show:false}},{min:-35,max:105,gridIndex:2,position:'right',axisLabel:{color:'#8fa3b7',formatter:'{value}%'},splitLine:{lineStyle:{color:'#1c2d3d'}}}],dataZoom:[{type:'inside',xAxisIndex:[0,1,2],filterMode:'none'},{type:'slider',xAxisIndex:[0,1,2],bottom:4,height:18,borderColor:'#26394b',backgroundColor:'#101b27',fillerColor:'rgba(97,168,255,.15)',textStyle:{color:'#8fa3b7'},startValue:r[0],endValue:r[1]}],series:[{name:'5m K线',type:'candlestick',xAxisIndex:0,yAxisIndex:0,data:times.map((t,i)=>[t,...ohlc[i]]),itemStyle:{color:'#22c993',color0:'#ff647c',borderColor:'#22c993',borderColor0:'#ff647c'},markPoint:{data:marks}},{name:'V2卖出',type:'scatter',xAxisIndex:0,yAxisIndex:0,data:exitData,z:8},{name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:times.map((t,i)=>[t,vol[i]]),itemStyle:{color:p=>bars[p.dataIndex][2]>=bars[p.dataIndex][1]?'rgba(34,201,147,.48)':'rgba(255,100,124,.48)'}},{name:'退出评分',type:'line',xAxisIndex:2,yAxisIndex:2,data:times.map((t,i)=>[t,score[i]*100]),showSymbol:false,lineStyle:{color:'#ad8cff',width:1.5}},{name:'峰值回撤',type:'line',xAxisIndex:2,yAxisIndex:2,data:times.map((t,i)=>[t,dd[i]]),showSymbol:false,lineStyle:{color:'#ff647c',width:1.2}},{name:'累计卖出',type:'line',step:'end',xAxisIndex:2,yAxisIndex:2,data:times.map((t,i)=>[t,sold[i]]),showSymbol:false,lineStyle:{color:'#f6c85f',width:2}}]},true);
  $('detail').innerHTML=c.events.map(e=>`<span><b>${dt(e.executionMs)}</b> 卖${Math.round(e.sold*100)}% · ${e.reason} · 分数 ${e.score?.toFixed(3)??'—'} · 回撤 ${e.drawdown==null?'—':pct(-e.drawdown)}</span>`).join('');
}
function renderEquity(){const s=DATA.summary,names={old_daily:'原日线',v1_multitimeframe:'V1硬回撤',v2_multitimeframe:'V2动态runner'},colors={old_daily:'#f6c85f',v1_multitimeframe:'#61a8ff',v2_multitimeframe:'#22c993'};$('equitySub').textContent=`V2总收益 ${pct(s.v2Return)} · 最大回撤 ${pct(s.v2MaxDrawdown)} · 窗口 ${dt(s.v2MddPeakMs)} → ${dt(s.v2MddTroughMs)} UTC`;equity.setOption({animation:false,tooltip:{trigger:'axis',backgroundColor:'#0b1621',borderColor:'#26394b',textStyle:{color:'#e9f1f8'}},legend:{top:8,textStyle:{color:'#8fa3b7'}},grid:{left:58,right:28,top:42,bottom:42},xAxis:{type:'time',axisLabel:{color:'#8fa3b7'},axisLine:{lineStyle:{color:'#26394b'}}},yAxis:{type:'value',scale:true,axisLabel:{color:'#8fa3b7',formatter:v=>v.toFixed(1)+'×'},splitLine:{lineStyle:{color:'#1c2d3d'}}},dataZoom:[{type:'inside'},{type:'slider',bottom:5,height:18,borderColor:'#26394b',backgroundColor:'#101b27',fillerColor:'rgba(97,168,255,.15)'}],series:Object.entries(DATA.portfolio).map(([k,v])=>{const series={name:names[k],type:'line',data:v,showSymbol:false,lineStyle:{width:k==='v2_multitimeframe'?2.4:1.5,color:colors[k]}};if(k==='v2_multitimeframe')series.markArea={silent:true,itemStyle:{color:'rgba(255,100,124,.08)'},label:{show:true,color:'#ff9aaa',formatter:'V2最大回撤窗口'},data:[[{xAxis:s.v2MddPeakMs},{xAxis:s.v2MddTroughMs}]]};return series})})}
function renderTable(){$('tradeRows').innerHTML=DATA.cases.map((c,i)=>`<tr data-id="${c.id}"><td>${i+1}</td><td><b>${c.symbol}</b><div class="help">${c.group}</div></td><td>${c.signalDate}</td><td>${dt(c.exitMs)}</td><td class="num">${c.events.length}</td><td class="num ${c.v2Return2x>=0?'positive':'negative'}">${pct(c.v2Return2x)}</td><td class="num ${c.v1Return2x>=0?'positive':'negative'}">${pct(c.v1Return2x)}</td><td class="num ${c.oldReturn2x>=0?'positive':'negative'}">${pct(c.oldReturn2x)}</td><td class="num positive">${pct(c.mfe)}</td><td class="num">${pct(c.capture)}</td></tr>`).join('');document.querySelectorAll('#tradeRows tr').forEach(tr=>tr.addEventListener('click',()=>{selected=tr.dataset.id;$('caseSelect').value=selected;rangeMode='all';setButtons();render();window.scrollTo({top:0,behavior:'smooth'})}))}
init();
</script>
</body>
</html>'''


def main() -> None:
    payload = json.dumps(build_payload(), ensure_ascii=False, separators=(",", ":"))
    OUTPUT.write_text(HTML_TEMPLATE.replace("__PAYLOAD__", payload), encoding="utf-8")
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size / 1024 / 1024:.2f} MiB)")


if __name__ == "__main__":
    main()
