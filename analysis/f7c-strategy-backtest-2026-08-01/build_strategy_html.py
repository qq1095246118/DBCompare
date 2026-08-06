#!/usr/bin/env python3
"""Build a standalone interactive HTML for F7c signals and trades."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
ANALYSIS = HERE.parent
DASHBOARD_DIR = ANALYSIS / "binance-bubblemaps-factor-kline-2026-07-30"
sys.path.insert(0, str(DASHBOARD_DIR))
import calculate_f5_subfactor_ic as dashboard  # noqa: E402


OUTPUT = HERE / "f7c-strategy-signals-trades.html"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def optional_float(value: str) -> float | None:
    return None if value == "" else float(value)


def build_payload() -> dict[str, Any]:
    dataset = dashboard.load_dataset()
    tokens = []
    for token in dataset["tokens"]:
        cluster_amount = float(token["cluster_amount"])
        bars = []
        for bar in token["bars"]:
            cex = bar.get("cex") or {}
            bars.append(
                {
                    "d": bar["d"],
                    "o": float(bar["o"]),
                    "h": float(bar["h"]),
                    "l": float(bar["l"]),
                    "c": float(bar["c"]),
                    "v": float(bar["v"]),
                    "f7c": float(cex.get("net_7d") or 0) / cluster_amount,
                    "cexIn": float(cex.get("in_7d") or 0),
                    "cexOut": float(cex.get("out_7d") or 0),
                    "cexTx": int(cex.get("tx_7d") or 0),
                }
            )
        tokens.append(
            {
                "symbol": token["symbol"],
                "group": token["group"],
                "clusterAmount": cluster_amount,
                "bars": bars,
            }
        )

    signals = []
    for row in read_csv(HERE / "signals.csv"):
        signals.append(
            {
                **row,
                "signal_id": int(row["signal_id"]),
                "f7c_share": float(row["f7c_share"]),
                "rank": int(row["rank"]),
                "universe_count": int(row["universe_count"]),
            }
        )

    trades = []
    for row in read_csv(HERE / "trades.csv"):
        trades.append(
            {
                **row,
                "signal_f7c_share": float(row["signal_f7c_share"]),
                "signal_rank": int(row["signal_rank"]),
                "entry_price": float(row["entry_price"]),
                "exit_price": optional_float(row["exit_price"]),
                "holding_days": int(row["holding_days"]),
                "gross_return": float(row["gross_return"]),
                "net_return": optional_float(row["net_return"]),
                "mae": float(row["mae"]),
                "mfe": float(row["mfe"]),
            }
        )

    daily = []
    for row in read_csv(HERE / "daily-equity.csv"):
        daily.append(
            {
                "date": row["date"],
                "equity": float(row["equity"]),
                "exposure": float(row["exposure"]),
                "positions": int(row["positions"]),
                "symbols": row["symbols"],
            }
        )

    equities = [row["equity"] for row in daily]
    peak = equities[0]
    max_drawdown = 0.0
    for equity in equities:
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    closed = [trade for trade in trades if trade["net_return"] is not None]
    summary = {
        "signals": len(signals),
        "entered": sum(signal["status"] == "entered" for signal in signals),
        "closed": len(closed),
        "winRate": (
            sum(float(trade["net_return"]) > 0 for trade in closed) / len(closed)
            if closed
            else 0
        ),
        "netReturn": equities[-1] - 1,
        "maxDrawdown": max_drawdown,
    }
    return {
        "generatedAt": dataset["generated_at"],
        "tokens": tokens,
        "signals": signals,
        "trades": trades,
        "daily": daily,
        "summary": summary,
    }


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>F7c策略 · 信号与交易复盘</title>
  <style>
    :root{color-scheme:dark;--bg:#081019;--panel:#101b27;--panel2:#142231;--text:#e8f0f7;--muted:#8fa3b7;--line:#26394b;--grid:#1c2d3d;--up:#27d69b;--down:#ff647c;--signal:#f6c85f;--blue:#61a8ff;--purple:#b497ff;--shadow:0 18px 50px rgba(0,0,0,.25)}
    *{box-sizing:border-box} body{margin:0;background:radial-gradient(circle at 15% -20%,#193651 0,transparent 38%),var(--bg);color:var(--text);font:14px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    .app{max-width:1500px;margin:auto;padding:24px}.top{display:flex;align-items:end;justify-content:space-between;gap:18px;flex-wrap:wrap}.eyebrow{color:var(--blue);font-size:12px;letter-spacing:.14em;text-transform:uppercase}.title{font-size:28px;font-weight:650;margin:2px 0}.subtitle{color:var(--muted)}
    .metrics{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:10px;margin:20px 0}.metric{background:linear-gradient(145deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:14px;padding:13px 15px;box-shadow:var(--shadow)}.metric span{display:block;color:var(--muted);font-size:12px}.metric strong{display:block;font-size:22px;margin-top:2px}.negative{color:var(--down)}.positive{color:var(--up)}
    .controls{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:16px 0}.field{display:grid;gap:5px}.field label{color:var(--muted);font-size:12px}.field select,.field button{appearance:none;background:var(--panel);border:1px solid var(--line);border-radius:9px;color:var(--text);padding:8px 34px 8px 10px;font:inherit}.field button{padding-right:10px;cursor:pointer}.field button:hover{border-color:var(--blue)}
    .chart-card{background:rgba(16,27,39,.88);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);overflow:hidden}.chart-head{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:12px 15px;border-bottom:1px solid var(--line);flex-wrap:wrap}.legend{display:flex;gap:14px;color:var(--muted);font-size:12px;flex-wrap:wrap}.legend span{display:flex;align-items:center;gap:6px}.dot{width:9px;height:9px;border-radius:50%;display:inline-block}.chart-wrap{position:relative;overflow:hidden}.chart{display:block;width:100%;height:auto;min-height:500px;touch-action:none}.tooltip{position:absolute;pointer-events:none;display:none;z-index:3;background:#0b1621;border:1px solid var(--line);border-radius:10px;padding:9px 11px;box-shadow:var(--shadow);min-width:230px;font-size:12px}.detail{display:flex;gap:16px;flex-wrap:wrap;padding:10px 15px;border-top:1px solid var(--line);color:var(--muted)}.detail b{color:var(--text);font-weight:550}.help{font-size:12px;color:var(--muted)}
    .section{margin-top:24px}.section-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 10px}.section-title h2{font-size:18px;margin:0}.table-wrap{border:1px solid var(--line);border-radius:14px;overflow:auto;background:rgba(16,27,39,.82)}table{border-collapse:collapse;width:100%;min-width:980px}th,td{padding:9px 11px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-size:12px;font-weight:500;background:var(--panel);position:sticky;top:0;z-index:1}tbody tr{cursor:pointer}tbody tr:hover{background:rgba(97,168,255,.08)}td.num{text-align:right;font-variant-numeric:tabular-nums}.badge{display:inline-flex;padding:2px 7px;border-radius:99px;font-size:11px;border:1px solid var(--line)}.entered{color:var(--up);border-color:rgba(39,214,155,.4)}.ignored_already_held,.unexecuted_sample_end{color:var(--signal);border-color:rgba(246,200,95,.4)}
    .equity-card{background:rgba(16,27,39,.82);border:1px solid var(--line);border-radius:14px;padding:10px 12px}.equity{display:block;width:100%;height:auto}
    @media(max-width:800px){.app{padding:14px}.metrics{grid-template-columns:repeat(2,1fr)}.title{font-size:23px}.chart{min-height:420px}.chart-head{align-items:flex-start}}
  </style>
</head>
<body>
<main class="app">
  <header class="top"><div><div class="eyebrow">Causal next-open backtest</div><h1 class="title">F7c策略 · 信号与交易复盘</h1><div class="subtitle">信号日确认 → 次日开盘执行 · 鼠标滚轮缩放，拖动平移，点击表格定位</div></div><div class="help" id="generated"></div></header>
  <section class="metrics" aria-label="回测摘要">
    <div class="metric"><span>信号 / 实际入场</span><strong id="signalMetric"></strong></div>
    <div class="metric"><span>已平仓胜率</span><strong id="winMetric"></strong></div>
    <div class="metric"><span>组合净收益</span><strong id="returnMetric"></strong></div>
    <div class="metric"><span>最大回撤</span><strong id="drawdownMetric"></strong></div>
  </section>
  <div class="controls">
    <div class="field"><label for="symbolSelect">币种</label><select id="symbolSelect"></select></div>
    <div class="field"><label for="rangeSelect">显示范围</label><select id="rangeSelect"><option value="45">45日</option><option value="90" selected>90日</option><option value="180">180日</option><option value="all">全部</option></select></div>
    <div class="field"><label for="signalFilter">信号表</label><select id="signalFilter"><option value="all">全部信号</option><option value="entered">实际入场</option><option value="not-entered">未执行/忽略</option></select></div>
    <div class="field"><label>&nbsp;</label><button type="button" id="resetBtn">重置视图</button></div>
  </div>
  <section class="chart-card">
    <div class="chart-head"><div><b id="chartTitle"></b><div class="help" id="chartSub"></div></div><div class="legend"><span><i class="dot" style="background:var(--signal)"></i>S 信号</span><span><i class="dot" style="background:var(--up)"></i>B 买入</span><span><i class="dot" style="background:var(--down)"></i>X 卖出</span><span><i class="dot" style="background:var(--purple)"></i>F7c</span></div></div>
    <div class="chart-wrap" id="chartWrap"><svg class="chart" id="chart" viewBox="0 0 1180 620" role="img" aria-label="日K、成交量、F7c及买卖信号"></svg><div class="tooltip" id="tooltip"></div></div>
    <div class="detail" id="detail"></div>
  </section>
  <section class="section"><div class="section-title"><h2>组合权益曲线</h2><span class="help">收盘盯市，包含0.20%每边摩擦</span></div><div class="equity-card"><svg class="equity" id="equity" viewBox="0 0 1180 190" role="img" aria-label="组合每日权益曲线"></svg></div></section>
  <section class="section"><div class="section-title"><h2>全部信号</h2><span class="help" id="signalCount"></span></div><div class="table-wrap"><table><thead><tr><th>ID</th><th>币种</th><th>分组</th><th>信号日</th><th class="num">F7c</th><th class="num">横截面排名</th><th>状态</th><th>入场日</th></tr></thead><tbody id="signalRows"></tbody></table></div></section>
  <section class="section"><div class="section-title"><h2>全部交易</h2><span class="help">点击任一交易定位K线</span></div><div class="table-wrap"><table><thead><tr><th>币种</th><th>分组</th><th>信号日</th><th>买入</th><th>卖出</th><th class="num">持有</th><th class="num">净收益</th><th class="num">MAE</th><th class="num">MFE</th><th>退出原因</th></tr></thead><tbody id="tradeRows"></tbody></table></div></section>
</main>
<script>
const DATA=__PAYLOAD__;
const $=id=>document.getElementById(id), NS="http://www.w3.org/2000/svg";
const tokenMap=new Map(DATA.tokens.map(t=>[t.symbol,t]));
const state={symbol:tokenMap.has("VELVET")?"VELVET":DATA.tokens[0].symbol,start:0,end:0,hover:null,drag:null};
const colors={up:"#27d69b",down:"#ff647c",signal:"#f6c85f",blue:"#61a8ff",purple:"#b497ff",grid:"#1c2d3d",line:"#26394b",muted:"#8fa3b7",text:"#e8f0f7"};
function svg(name,attrs={}){const el=document.createElementNS(NS,name);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,v));return el}
function fmt(v,d=4){if(v==null||!Number.isFinite(v))return "—";if(Math.abs(v)>=1e9)return (v/1e9).toFixed(2)+"B";if(Math.abs(v)>=1e6)return (v/1e6).toFixed(2)+"M";if(Math.abs(v)>=1e3)return (v/1e3).toFixed(2)+"K";return v.toLocaleString(undefined,{maximumFractionDigits:d})}
function pct(v,d=2){return v==null?"—":`${v>=0?"+":""}${(v*100).toFixed(d)}%`}
function statusLabel(s){return {entered:"已入场",ignored_already_held:"持仓中忽略",unexecuted_sample_end:"样本末未执行",skipped_no_slot:"满仓跳过",skipped_no_open:"无开盘价"}[s]||s}
function init(){
  $("generated").textContent=`数据 ${DATA.generatedAt}`;
  $("signalMetric").textContent=`${DATA.summary.signals} / ${DATA.summary.entered}`;
  $("winMetric").textContent=pct(DATA.summary.winRate);
  $("returnMetric").textContent=pct(DATA.summary.netReturn); $("returnMetric").className=DATA.summary.netReturn>=0?"positive":"negative";
  $("drawdownMetric").textContent=pct(DATA.summary.maxDrawdown); $("drawdownMetric").className="negative";
  DATA.tokens.slice().sort((a,b)=>a.symbol.localeCompare(b.symbol)).forEach(t=>{const o=document.createElement("option");o.value=t.symbol;o.textContent=`${t.symbol} · ${t.group}`;$("symbolSelect").appendChild(o)});$("symbolSelect").value=state.symbol;
  $("symbolSelect").addEventListener("change",e=>{state.symbol=e.target.value;resetRange();renderAll()});
  $("rangeSelect").addEventListener("change",()=>{resetRange();renderChart()});
  $("signalFilter").addEventListener("change",renderSignals);
  $("resetBtn").addEventListener("click",()=>{resetRange();renderChart()});
  resetRange();renderAll();renderEquity();renderSignals();renderTrades();
}
function resetRange(){const n=tokenMap.get(state.symbol).bars.length;const v=$("rangeSelect").value;const len=v==="all"?n:Math.min(n,+v);state.end=n;state.start=n-len;state.hover=null}
function renderAll(){renderChart();renderSignals();renderTrades()}
function current(){return tokenMap.get(state.symbol)}
function focusDate(symbol,date){state.symbol=symbol;$("symbolSelect").value=symbol;const bars=current().bars;const idx=Math.max(0,bars.findIndex(b=>b.d===date));const len=Math.min(90,bars.length);state.start=Math.max(0,Math.min(bars.length-len,idx-Math.floor(len/2)));state.end=Math.min(bars.length,state.start+len);state.hover=idx;renderAll();$("chartWrap").scrollIntoView({behavior:"smooth",block:"center"})}
function renderChart(){
  const chart=$("chart"),token=current(),all=token.bars,bars=all.slice(state.start,state.end);chart.innerHTML="";if(!bars.length)return;
  $("chartTitle").textContent=`${token.symbol} · 日K / 成交量 / F7c`;
  $("chartSub").textContent=`${token.group} · ${bars[0].d} — ${bars[bars.length-1].d} · Cluster ${fmt(token.clusterAmount)}`;
  const W=1180,H=620,L=62,R=86,T=28,priceB=348,volT=385,volB=472,fT=515,fB=585,plotW=W-L-R,step=plotW/bars.length,body=Math.max(2,Math.min(10,step*.62));
  const pMin=Math.min(...bars.map(b=>b.l)),pMax=Math.max(...bars.map(b=>b.h)),pad=(pMax-pMin||pMax*.05)*.08,lo=pMin-pad,hi=pMax+pad;
  const y=v=>priceB-(v-lo)/(hi-lo)*(priceB-T),x=i=>L+(i+.5)*step;const vMax=Math.max(...bars.map(b=>b.v),1),vy=v=>volB-v/vMax*(volB-volT);
  const fAbs=Math.max(.0012,...bars.map(b=>Math.abs(b.f7c)))*1.08,fy=v=>fB-(v+fAbs)/(2*fAbs)*(fB-fT);
  const trades=DATA.trades.filter(t=>t.symbol===token.symbol),signals=DATA.signals.filter(s=>s.symbol===token.symbol);
  trades.forEach(t=>{const a=bars.findIndex(b=>b.d===t.entry_date),z=bars.findIndex(b=>b.d===(t.exit_date||bars[bars.length-1].d));if(a>=0||z>=0){const aa=Math.max(0,a),zz=z<0?bars.length-1:z;chart.appendChild(svg("rect",{x:L+aa*step,y:T,width:Math.max(step,(zz-aa+1)*step),height:priceB-T,fill:"rgba(97,168,255,.07)"}))}});
  for(let i=0;i<5;i++){const yy=T+i*(priceB-T)/4;chart.appendChild(svg("line",{x1:L,y1:yy,x2:W-R,y2:yy,stroke:colors.grid}));const tx=svg("text",{x:W-R+8,y:yy+4,fill:colors.muted,"font-size":11});tx.textContent=fmt(hi-i*(hi-lo)/4,6);chart.appendChild(tx)}
  chart.appendChild(svg("line",{x1:L,y1:volT-16,x2:W-R,y2:volT-16,stroke:colors.line}));chart.appendChild(svg("line",{x1:L,y1:fT-16,x2:W-R,y2:fT-16,stroke:colors.line}));
  const zero=fy(0),thr=fy(.001);chart.appendChild(svg("line",{x1:L,y1:zero,x2:W-R,y2:zero,stroke:colors.line,"stroke-dasharray":"3 4"}));chart.appendChild(svg("line",{x1:L,y1:thr,x2:W-R,y2:thr,stroke:colors.signal,"stroke-dasharray":"4 4",opacity:.65}));
  bars.forEach((b,i)=>{const c=b.c>=b.o?colors.up:colors.down;chart.appendChild(svg("line",{x1:x(i),y1:y(b.h),x2:x(i),y2:y(b.l),stroke:c,"stroke-width":Math.max(1,body*.18)}));chart.appendChild(svg("rect",{x:x(i)-body/2,y:Math.min(y(b.o),y(b.c)),width:body,height:Math.max(1,Math.abs(y(b.o)-y(b.c))),fill:c,rx:.5}));chart.appendChild(svg("rect",{x:x(i)-body/2,y:vy(b.v),width:body,height:volB-vy(b.v),fill:c,opacity:.42}))});
  const path=bars.map((b,i)=>`${i?"L":"M"}${x(i)},${fy(b.f7c)}`).join(" ");chart.appendChild(svg("path",{d:path,fill:"none",stroke:colors.purple,"stroke-width":2}));
  signals.forEach(s=>{const i=bars.findIndex(b=>b.d===s.signal_date);if(i<0)return;const yy=Math.max(T+12,y(bars[i].h)-15),xx=x(i),sz=6;chart.appendChild(svg("path",{d:`M${xx},${yy-sz} L${xx+sz},${yy} L${xx},${yy+sz} L${xx-sz},${yy}Z`,fill:colors.signal,stroke:s.status==="entered"?colors.text:colors.muted,"stroke-width":1.2}));const tx=svg("text",{x:xx+8,y:yy+4,fill:colors.signal,"font-size":10});tx.textContent="S";chart.appendChild(tx)});
  trades.forEach(t=>{let i=bars.findIndex(b=>b.d===t.entry_date);if(i>=0){const xx=x(i),yy=Math.min(priceB-8,y(bars[i].l)+17);chart.appendChild(svg("path",{d:`M${xx},${yy-8} l7,12 h-14Z`,fill:colors.up}));const tx=svg("text",{x:xx+9,y:yy+3,fill:colors.up,"font-size":10});tx.textContent="B";chart.appendChild(tx)}i=bars.findIndex(b=>b.d===t.exit_date);if(i>=0){const xx=x(i),yy=Math.max(T+8,y(bars[i].h)-17);chart.appendChild(svg("path",{d:`M${xx},${yy+8} l7,-12 h-14Z`,fill:colors.down}));const tx=svg("text",{x:xx+9,y:yy+3,fill:colors.down,"font-size":10});tx.textContent="X";chart.appendChild(tx)}});
  [0,.25,.5,.75,1].forEach(q=>{const i=Math.min(bars.length-1,Math.floor(q*(bars.length-1))),tx=svg("text",{x:x(i),y:607,fill:colors.muted,"font-size":11,"text-anchor":"middle"});tx.textContent=bars[i].d.slice(5);chart.appendChild(tx)});
  [["PRICE",T+11],["VOL",volT+10],["F7c",fT+10]].forEach(([label,yy])=>{const tx=svg("text",{x:10,y:yy,fill:colors.muted,"font-size":11});tx.textContent=label;chart.appendChild(tx)});const th=svg("text",{x:W-R+6,y:thr+4,fill:colors.signal,"font-size":10});th.textContent="0.10%";chart.appendChild(th);
  const cross=svg("line",{x1:L,y1:T,x2:L,y2:fB,stroke:colors.muted,"stroke-dasharray":"3 4",opacity:0});chart.appendChild(cross);
  const hit=svg("rect",{x:L,y:T,width:plotW,height:fB-T,fill:"transparent",style:"cursor:crosshair"});
  function localIndex(evt){const pt=chart.createSVGPoint();pt.x=evt.clientX;pt.y=evt.clientY;const p=pt.matrixTransform(chart.getScreenCTM().inverse());return Math.max(0,Math.min(bars.length-1,Math.floor((p.x-L)/step)))}
  hit.addEventListener("pointermove",evt=>{if(state.drag){const idx=localIndex(evt),delta=state.drag.idx-idx;if(delta){pan(delta);state.drag.idx=idx}return}const i=localIndex(evt);cross.setAttribute("x1",x(i));cross.setAttribute("x2",x(i));cross.setAttribute("opacity",.75);showHover(state.start+i,evt)});
  hit.addEventListener("pointerleave",()=>{if(!state.drag){cross.setAttribute("opacity",0);$("tooltip").style.display="none"}});
  hit.addEventListener("pointerdown",evt=>{hit.setPointerCapture(evt.pointerId);state.drag={idx:localIndex(evt)}});hit.addEventListener("pointerup",()=>state.drag=null);hit.addEventListener("pointercancel",()=>state.drag=null);
  hit.addEventListener("wheel",evt=>{evt.preventDefault();zoom(evt.deltaY<0?.78:1.28,localIndex(evt))},{passive:false});hit.addEventListener("dblclick",()=>{resetRange();renderChart()});chart.appendChild(hit);
  if(state.hover!=null&&state.hover>=state.start&&state.hover<state.end)updateDetail(state.hover);
}
function showHover(globalIndex,evt){state.hover=globalIndex;updateDetail(globalIndex);const token=current(),b=token.bars[globalIndex],s=DATA.signals.filter(x=>x.symbol===token.symbol&&x.signal_date===b.d),t=DATA.trades.filter(x=>x.symbol===token.symbol&&(x.entry_date===b.d||x.exit_date===b.d));const tip=$("tooltip");tip.innerHTML=`<b>${token.symbol} · ${b.d}</b><br>O ${fmt(b.o,6)} · H ${fmt(b.h,6)} · L ${fmt(b.l,6)} · C ${fmt(b.c,6)}<br>成交量 ${fmt(b.v)} · F7c ${pct(b.f7c,3)}<br>CEX流入 ${fmt(b.cexIn)} / 流出 ${fmt(b.cexOut)} · ${b.cexTx}笔${s.length?`<br><span style="color:var(--signal)">信号：${s.map(x=>statusLabel(x.status)).join("、")}</span>`:""}${t.length?`<br><span style="color:var(--blue)">交易：${t.map(x=>x.entry_date===b.d?"买入":"卖出").join("、")}</span>`:""}`;tip.style.display="block";const box=$("chartWrap").getBoundingClientRect();let left=evt.clientX-box.left+14,top=evt.clientY-box.top+12;if(left+250>box.width)left-=270;if(top+150>box.height)top-=165;tip.style.left=Math.max(4,left)+"px";tip.style.top=Math.max(4,top)+"px"}
function updateDetail(i){const token=current(),b=token.bars[i];if(!b)return;const sig=DATA.signals.find(x=>x.symbol===token.symbol&&x.signal_date===b.d),trade=DATA.trades.find(x=>x.symbol===token.symbol&&(x.entry_date===b.d||x.exit_date===b.d));$("detail").innerHTML=`<span><b>${b.d}</b></span><span>收盘 <b>${fmt(b.c,6)}</b></span><span>成交量 <b>${fmt(b.v)}</b></span><span>F7c <b class="${b.f7c>=0?"positive":"negative"}">${pct(b.f7c,3)}</b></span>${sig?`<span>信号 <b>${statusLabel(sig.status)} · #${sig.rank}/${sig.universe_count}</b></span>`:""}${trade?`<span>交易 <b>${trade.entry_date===b.d?"买入":"卖出"}${trade.net_return!=null?" · "+pct(trade.net_return):""}</b></span>`:""}`}
function zoom(factor,anchorLocal){const token=current(),n=token.bars.length,len=state.end-state.start,newLen=Math.max(20,Math.min(n,Math.round(len*factor))),ratio=(anchorLocal+.5)/len,anchor=state.start+anchorLocal+.5;let start=Math.round(anchor-ratio*newLen);start=Math.max(0,Math.min(n-newLen,start));state.start=start;state.end=start+newLen;renderChart()}
function pan(delta){const n=current().bars.length,len=state.end-state.start,start=Math.max(0,Math.min(n-len,state.start+delta));if(start===state.start)return;state.start=start;state.end=start+len;renderChart()}
function renderEquity(){const el=$("equity"),rows=DATA.daily,W=1180,H=190,L=45,R=20,T=16,B=164,min=Math.min(...rows.map(r=>r.equity)),max=Math.max(...rows.map(r=>r.equity)),x=i=>L+i/(rows.length-1)*(W-L-R),y=v=>B-(v-min)/(max-min||1)*(B-T);el.innerHTML="";[0,.5,1].forEach(q=>{const yy=T+q*(B-T);el.appendChild(svg("line",{x1:L,y1:yy,x2:W-R,y2:yy,stroke:colors.grid}));const tx=svg("text",{x:5,y:yy+4,fill:colors.muted,"font-size":11});tx.textContent=(max-q*(max-min)).toFixed(2)+"×";el.appendChild(tx)});const path=rows.map((r,i)=>`${i?"L":"M"}${x(i)},${y(r.equity)}`).join(" ");el.appendChild(svg("path",{d:path,fill:"none",stroke:colors.blue,"stroke-width":2.2}));const base=y(1);el.appendChild(svg("line",{x1:L,y1:base,x2:W-R,y2:base,stroke:colors.signal,"stroke-dasharray":"4 4",opacity:.7}))}
function renderSignals(){const f=$("signalFilter").value;const rows=DATA.signals.filter(s=>f==="all"||(f==="entered"&&s.status==="entered")||(f==="not-entered"&&s.status!=="entered"));$("signalCount").textContent=`${rows.length} 条`;$("signalRows").innerHTML=rows.map(s=>`<tr data-symbol="${s.symbol}" data-date="${s.signal_date}"><td>#${s.signal_id}</td><td><b>${s.symbol}</b></td><td>${s.group}</td><td>${s.signal_date}</td><td class="num">${pct(s.f7c_share,3)}</td><td class="num">${s.rank}/${s.universe_count}</td><td><span class="badge ${s.status}">${statusLabel(s.status)}</span> <span class="help">${s.status_reason}</span></td><td>${s.entry_date||"—"}</td></tr>`).join("");$("signalRows").querySelectorAll("tr").forEach(tr=>tr.addEventListener("click",()=>focusDate(tr.dataset.symbol,tr.dataset.date)))}
function renderTrades(){const rows=DATA.trades.slice().sort((a,b)=>a.signal_date.localeCompare(b.signal_date));$("tradeRows").innerHTML=rows.map(t=>`<tr data-symbol="${t.symbol}" data-date="${t.signal_date}"><td><b>${t.symbol}</b></td><td>${t.group}</td><td>${t.signal_date}</td><td>${t.entry_date}<div class="help">${fmt(t.entry_price,6)}</div></td><td>${t.exit_date||"—"}<div class="help">${fmt(t.exit_price,6)}</div></td><td class="num">${t.holding_days}日</td><td class="num ${t.net_return>=0?"positive":"negative"}">${pct(t.net_return)}</td><td class="num negative">${pct(t.mae)}</td><td class="num positive">${pct(t.mfe)}</td><td>${t.exit_reason}</td></tr>`).join("");$("tradeRows").querySelectorAll("tr").forEach(tr=>tr.addEventListener("click",()=>focusDate(tr.dataset.symbol,tr.dataset.date)))}
init();
</script>
</body>
</html>'''


def main() -> None:
    payload = json.dumps(build_payload(), ensure_ascii=False, separators=(",", ":"))
    OUTPUT.write_text(HTML_TEMPLATE.replace("__PAYLOAD__", payload), encoding="utf-8")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
