"""单页界面：stdlib http.server + JSON API + 原生 JS，无外部依赖。

能力：2D/3D 切换、问题列表、层预览、点击画线（写回 route 表）、走线属性面板。
前端只做渲染与输入，所有写操作都走 aepcb 模块，不引入第二套存储。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import agent, gcode, history, jobs, mrc, printer, report, routing, slicing
from .board import load_route_points
from .store import DB, now

INDEX = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>增材电子一体化软件</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Noto Sans CJK SC",sans-serif;background:#0F172A;color:#E2E8F0}
header{padding:12px 20px;background:#1E293B;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
header h1{font-size:15px;margin:0;font-weight:600}
header .meta{font-size:12px;color:#94A3B8}
.tabs{display:flex;gap:6px;margin-left:auto}
.tabs button{background:#0F172A;border:1px solid #334155;color:#CBD5E1;padding:5px 12px;border-radius:6px;font-size:12px;cursor:pointer}
.tabs button.on{background:#2563EB;border-color:#2563EB;color:#fff}
main{display:grid;grid-template-columns:132px 1fr 390px;grid-template-areas:"nav canvas aside";gap:12px;padding:12px;height:calc(100vh - 104px)}
#navPanel{grid-area:nav}
#canvasPanel{grid-area:canvas}
#asidePanel{grid-area:aside}
nav.panel button{display:block;width:100%;text-align:left;background:#0F172A;border:1px solid #334155;color:#CBD5E1;padding:7px 9px;border-radius:6px;font-size:12px;margin-bottom:6px;cursor:pointer}
nav.panel button.on{background:#1E3A8A;border-color:#2563EB;color:#fff}
footer{display:flex;align-items:center;gap:14px;padding:8px 16px;background:#1E293B;border-top:1px solid #26344A;font-size:12px;color:#94A3B8}
#progressWrap{flex:1;height:6px;background:#0F172A;border-radius:3px;overflow:hidden}
#progressBar{height:100%;width:0;background:#2563EB;transition:width .2s}
.panel{background:#1E293B;border-radius:10px;padding:12px;overflow:auto}
.panel h2{font-size:12px;margin:0 0 8px;color:#93C5FD;font-weight:600;letter-spacing:.04em}
.btns{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
button.act{background:#2563EB;border:0;color:#fff;padding:7px 10px;border-radius:6px;font-size:12px;cursor:pointer}
button.act:hover{background:#1D4ED8}
polyline.route-line{transition:stroke-opacity .12s}
polyline.route-line:hover{stroke-opacity:1;filter:drop-shadow(0 0 1px #0EA5E9)}
polyline.route-hit:hover{cursor:move}
button.ghost{background:#0F172A;border:1px solid #334155;color:#CBD5E1}
button.ghost:hover{background:#172033}
#view{position:relative}
#view svg{width:100%;height:auto;display:block}
.kv{display:grid;grid-template-columns:96px 1fr;gap:5px;font-size:12px;margin-bottom:8px}
.kv b{color:#93C5FD;font-weight:500}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:4px 6px;border-bottom:1px solid #26344A}
th{color:#94A3B8;font-weight:500}
tr.sel{background:#1E3A8A}
.sev-major{color:#FCA5A5}.sev-minor{color:#FCD34D}
#log{font-family:ui-monospace,monospace;font-size:11px;color:#94A3B8;white-space:pre-wrap;max-height:96px;overflow:auto}
input,select{background:#0F172A;border:1px solid #334155;color:#E2E8F0;border-radius:5px;padding:4px 6px;font-size:12px}
.row{display:flex;gap:6px;align-items:center;margin-bottom:6px}
</style></head><body>
<header>
  <h1>增材电子一体化软件</h1>
  <select id="projSel" onchange="switchProject(this.value)" style="min-width:160px"></select>
  <button class="act ghost" onclick="newProject()">新建工程</button>
  <span class="meta" id="meta">加载中…</span>
  <span class="meta" id="dev">设备 —</span>
  <div class="tabs">
    <button id="tab2d" class="on" onclick="setTab('2d')">2D 板面</button>
    <button id="tab3d" onclick="setTab('3d')">3D 等轴</button>
  </div>
</header>
<main>
  <nav class="panel" id="navPanel">
    <h2>四域</h2>
    <button onclick="jump('hRoutes',this)">走线</button>
    <button onclick="jump('hIssues',this)">可打性</button>
    <button onclick="jump('hPlan',this)">计划</button>
    <button onclick="jump('hPrint',this)">打印</button>
    <button onclick="jump('hChat',this)">对话</button>
    <button onclick="jump('hLog',this)">日志</button>
  </nav>
  <div class="panel" id="canvasPanel">
    <div class="btns">
      <button class="act" onclick="act('check')">重新检查</button>
      <button class="act" onclick="sliceAsync()">切片</button>
      <button class="act ghost" onclick="cancelJob()">取消</button>
      <button class="act" onclick="act('export')">导出 G-code</button>
      <button class="act" onclick="act('print')">虚拟打印</button>
      <button class="act" onclick="act('agent')">Agent 优化</button>
      <button class="act ghost" onclick="act('report')">生成报告</button>
    </div>
    <div class="btns">
      <button class="act ghost" onclick="act('undo')">撤销</button>
      <button class="act ghost" onclick="act('redo')">重做</button>
      <button class="act ghost" onclick="reshape()">画线应用到选中走线</button>
    </div>
    <div class="row">
      <label><input type="checkbox" id="drawMode"> 画线模式</label>
      <label><input type="checkbox" id="measureMode"> 测量</label>
      <span id="measureOut" style="color:#FBBF24">—</span>
      <select id="layerSel"><option value="0">层 0</option><option value="1">层 1</option></select>
      <select id="netSel"></select>
      <input id="widthInput" type="number" step="0.1" value="0.6" style="width:64px"> mm
      <button class="act" onclick="saveRoute()">保存走线</button>
      <button class="act ghost" onclick="clearDraw()">清空</button>
    </div>
    <div class="row"><label>剝切</label>
      <input id="cutZ" type="range" min="0" max="1.2" step="0.05" value="1.2"
             style="flex:1" oninput="render()">
      <span id="cutOut">全显示</span></div>
    <div id="view">加载中…</div>
    <h2 style="margin-top:12px">层预览</h2>
    <div id="layers" style="display:flex;gap:8px;flex-wrap:wrap"></div>
  </div>
  <div class="panel" id="asidePanel">
    <h2 id="hRoutes">走线（点击选中）</h2>
    <table id="routes"><thead><tr><th>网络</th><th>层</th><th>宽度</th><th>长度</th></tr></thead><tbody></tbody></table>
    <h2 id="hComp" style="margin-top:10px">器件（点击选中）</h2>
    <table id="comps"><thead><tr><th>位号</th><th>X</th><th>Y</th><th>旋转</th><th>面</th><th>锁</th></tr></thead><tbody></tbody></table>
    <div class="row">
      <input id="compDx" type="number" step="0.5" value="5" style="width:56px">
      <input id="compDy" type="number" step="0.5" value="0" style="width:56px">
      <label title="位移吸附到栅格"><input id="compSnap" type="number" step="0.1" value="0.5"
        style="width:48px"> mm</label>
      <button class="act" onclick="compMove()">移动</button>
      <button class="act" onclick="compRotate()">旋转90°</button>
      <button class="act" onclick="compFlip()">翻面</button>
      <button class="act ghost" onclick="compLock()">锁定/解锁</button>
    </div>
    <h2 style="margin-top:10px">属性</h2>
    <div class="kv" id="prop"><b>选中</b><span>—</span></div>
    <div class="row"><input id="newWidth" type="number" step="0.1" style="width:70px">
      <button class="act" onclick="applyWidth()">应用宽度</button>
      <button class="act" onclick="netWidth()">整网设宽度</button>
      <button class="act" onclick="batchWidth()">批量改选中</button>
      <button class="act" onclick="suggestWidth()">建议宽度</button>
      <button class="act ghost" onclick="deleteRoute()">删除</button></div>
    <h2 style="margin-top:10px">操作栈（可撤销）</h2>
    <table id="hist"><thead><tr><th>#</th><th>类型</th><th>对象</th><th>状态</th></tr></thead><tbody></tbody></table>
    <h2 id="hPlan" style="margin-top:10px">计划步骤</h2>
    <div class="kv" id="planKv"><b>计划</b><span>—</span></div>
    <table id="steps"><thead><tr><th>#</th><th>工具</th><th>意图</th><th>状态</th></tr></thead><tbody></tbody></table>
    <h2 style="margin-top:10px">对比卡片</h2>
    <div class="kv" id="diffs"><b>对比</b><span>—</span></div>
    <h2 id="hPrint" style="margin-top:10px">打印</h2>
    <div class="kv" id="printKv"><b>打印</b><span>未开始</span></div>
    <h2 id="hChat" style="margin-top:10px">对话</h2>
    <div id="chat" style="font-size:12px;max-height:180px;overflow:auto">
      <span style="color:#94A3B8">暂无对话</span></div>
    <h2 style="margin-top:10px">规则版本</h2>
    <div class="row"><input id="ruleLabel" placeholder="v2" style="width:70px">
      <input id="ruleChanges" placeholder="R1.min_width_mm=0.4" style="flex:1">
      <button class="act" onclick="newRule()">新建并重评</button></div>
    <h2 id="hIssues" style="margin-top:10px">问题</h2>
    <table id="issues"><thead><tr><th>规则</th><th>严重度</th><th>说明</th></tr></thead><tbody></tbody></table>
    <h2 id="hLog" style="margin-top:10px">日志</h2><div id="log"></div>
  </div>
</main>
<footer>
  <label><input type="checkbox" id="layer0" checked onchange="render()"> 层 0</label>
  <label><input type="checkbox" id="layer1" checked onchange="render()"> 层 1</label>
  <span>时间轴</span><span id="timeline">—</span>
  <div id="progressWrap"><div id="progressBar"></div></div>
  <span id="progressText">就绪</span>
</footer>
<script>
let S = null, tab = '2d', sel = null, drawing = [], drawMode = false, multiSel = [], measurePts = [], compSel = null;
function log(s){const e=document.getElementById('log');e.textContent=(s+'\n'+e.textContent).slice(0,1500);}
function jump(id, btn){
  const el=document.getElementById(id);
  if(el) el.scrollIntoView({block:'start',behavior:'smooth'});
  document.querySelectorAll('#navPanel button').forEach(b=>b.classList.toggle('on', b===btn));
}
function setTab(t){tab=t;document.getElementById('tab2d').classList.toggle('on',t==='2d');
  document.getElementById('tab3d').classList.toggle('on',t==='3d');render();}
async function act(name){log('执行 '+name+' …');
  const r=await fetch('/api/'+name,{method:'POST'});const j=await r.json();
  log(name+'：'+(j.message||''));await refresh();}
async function refresh(){
  S=await (await fetch('/api/state')).json();
  document.getElementById('meta').textContent = S.project?`${S.project} · v${S.version_no} · MRC ${S.score}`:'没有工程';
  document.getElementById('dev').textContent = '设备 ' + (S.device || '—');
  document.getElementById('timeline').textContent = ((S.history||[]).length) + ' 步';
  document.getElementById('kv');
  const sel2=document.getElementById('netSel');
  sel2.innerHTML=(S.nets||[]).map(n=>`<option>${n}</option>`).join('');
  const ps=document.getElementById('projSel');
  ps.innerHTML=(S.projects||[]).map(p=>`<option value="${p.id}" ${p.name===S.project?'selected':''}>${p.name}</option>`).join('');
  document.querySelector('#issues tbody').innerHTML=S.issues.map(i=>
    `<tr><td>${i.rule}</td><td class="sev-${i.severity}">${i.severity}</td><td>${i.message}</td></tr>`).join('');
  document.querySelector('#routes tbody').innerHTML=(S.routes||[]).map(r=>
    `<tr data-id="${r.id}" class="${sel===r.id?'sel':''}" onclick="pick('${r.id}', event.shiftKey)">
      <td>${r.net}</td><td>${r.layer}</td><td>${r.width}</td><td>${r.length}</td></tr>`).join('');
  document.getElementById('layers').innerHTML=S.layers.map(l=>
    `<figure style="margin:0"><figcaption style="font-size:10px;color:#94A3B8">${l.title}</figcaption>${l.svg}</figure>`).join('');
  document.querySelector('#comps tbody').innerHTML=(S.components||[]).map(c=>
    `<tr data-id="${c.id}" class="${compSel===c.id?'sel':''}" onclick="pickComp('${c.id}')">
      <td>${c.ref}</td><td>${c.x}</td><td>${c.y}</td><td>${c.rot}°</td><td>${c.side}</td>
      <td>${c.locked?'是':''}</td></tr>`).join('');
  document.querySelector('#hist tbody').innerHTML=(S.history||[]).map(h=>
    `<tr style="cursor:pointer" onclick="undoTo(${h.seq})"><td>${h.seq}</td><td>${h.kind}</td><td>${h.target}</td>
      <td>${h.undone?'已撤销':'有效'}</td></tr>`).join('');
  const pl=S.plan;
  document.getElementById('planKv').innerHTML = pl
    ? `<b>状态</b><span>${pl.status}</span><b>分数</b><span>${pl.before} → ${pl.after}</span>
       <b>轮次</b><span>${pl.rounds}</span><b>降级</b><span>${pl.degraded?'是（离线规则）':'否'}</span>
       <b>目标</b><span>${pl.goal||''}</span>`
    : '<b>计划</b><span>未运行</span>';
  document.querySelector('#steps tbody').innerHTML=(S.steps||[]).map(s=>
    `<tr><td>${s.no}</td><td>${s.tool}</td><td>${s.intent||''}</td><td>${s.status}</td></tr>`).join('');
  document.getElementById('diffs').innerHTML = (S.diffs&&S.diffs.length)
    ? S.diffs.map(d=>`<b>分数</b><span>${d.before} → ${d.after}</span>
       <b>理由</b><span>${d.reason||''}</span><b>决定</b><span>${d.decision||''}</span>`).join('')
    : '<b>对比</b><span>—</span>';
  document.getElementById('printKv').innerHTML = S.print
    ? `<b>状态</b><span>${S.print.status}</span><b>阶段</b><span>${S.print.phase||'—'}</span>
       <b>层</b><span>${S.print.layer}</span><b>耗时</b><span>${S.print.duration_s||0} s</span>`
    : '<b>打印</b><span>未开始</span>';
  document.getElementById('chat').innerHTML = (S.chat&&S.chat.length)
    ? S.chat.map(m=>`<div style="margin:4px 0;padding:4px 6px;border-radius:6px;background:${
        m.role==='user'?'#1E3A8A':(m.role==='system'?'#0F172A':'#172033')}">
        <b style="color:#93C5FD;font-size:11px">${m.role}</b><div>${m.text}</div></div>`).join('')
    : '<span style="color:#94A3B8">暂无对话</span>';
  render();
}
let isoView={az:Math.PI/4, el:Math.PI/6, zoom:1, px:0, py:0};
let curJob=null;
async function sliceAsync(){
  const r=await fetch('/api/job/slice',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const j=await r.json();
  if(!j.ok){log('提交失败：'+(j.message||j.error||''));return;}
  curJob=j.job_id; log('切片任务 '+curJob+' 已提交');
  pollJob(curJob);
}
function pollJob(id){
  const timer=setInterval(async()=>{
    const j=await (await fetch('/api/job/'+id)).json();
    document.getElementById('progressBar').style.width=(j.progress||0)+'%';
    document.getElementById('progressText').textContent =
      j.status==='running' ? (j.progress||0)+'%' : j.status;
    if(j.status!=='running'){
      clearInterval(timer); curJob=null;
      log('切片 '+j.status+'（'+(j.progress||0)+'%）');
      await refresh();
    }
  },300);
}
async function cancelJob(){
  if(!curJob){log('没有运行中的任务');return;}
  const j=await (await fetch('/api/job/'+curJob+'/cancel',{method:'POST'})).json();
  log(j.message||'');
}
function isoProj(x,y,z,W,H){
  const ca=Math.cos(isoView.az), sa=Math.sin(isoView.az);
  const x1=x*ca-y*sa, y1=x*sa+y*ca;
  const ce=Math.cos(isoView.el), se=Math.sin(isoView.el);
  const b=(S.iso&&S.iso.board)||[60,40];
  const k=(W/(b[0]+b[1]))*1.55*isoView.zoom;
  return [W*0.5+x1*k+isoView.px, H*0.5+(y1*se-z*22*ce)*k+isoView.py];
}
function renderIso(){
  const g=S.iso;
  if(!g||!g.board) return '<p style="color:#94A3B8;padding:20px">没有电路数据</p>';
  const W=760, H=470, b=g.board;
  const cutEl=document.getElementById('cutZ');
  const cutZ=cutEl?parseFloat(cutEl.value):1.2;
  const cutOut=document.getElementById('cutOut');
  if(cutOut) cutOut.textContent = cutZ>=1.2 ? '全显示' : ('z ≤ '+cutZ.toFixed(2)+' mm');
  const show0=document.getElementById('layer0')?.checked ?? true;
  const show1=document.getElementById('layer1')?.checked ?? true;
  const P=(x,y,z)=>{const q=isoProj(x,y,z,W,H);return q[0].toFixed(1)+','+q[1].toFixed(1);};
  let out=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" style="background:#0B1220;border-radius:8px;cursor:grab;touch-action:none">`;
  out+=`<polygon points="${P(0,0,0)} ${P(b[0],0,0)} ${P(b[0],b[1],0)} ${P(0,b[1],0)}" fill="#132033" stroke="#3B82F6" stroke-opacity="0.6"/>`;
  for(const c of (g.comps||[])){
    const x=c.x-c.w/2, y=c.y-c.h/2;
    out+=`<polygon points="${P(x,y,1.2)} ${P(x+c.w,y,1.2)} ${P(x+c.w,y+c.h,1.2)} ${P(x,y+c.h,1.2)}" fill="#334155" stroke="#94A3B8" stroke-width="0.6"/>`;
  }
  for(const r of (g.routes||[])){
    if(r.layer===0&&!show0) continue;
    if(r.layer===1&&!show1) continue;
    if(r.pts.length && r.pts[0][2] > cutZ + 1e-9) continue;   // 该层被剝掉
    const pts=r.pts.map(p=>P(p[0],p[1],p[2])).join(' ');
    out+=`<polyline points="${pts}" fill="none" stroke="${r.color}" stroke-width="${Math.max(1.2,r.w*3).toFixed(1)}" stroke-opacity="0.9"/>`;
  }
  for(const p of (g.pads||[])){
    const q=isoProj(p[0],p[1],p[2],W,H);
    out+=`<rect x="${(q[0]-1.5).toFixed(1)}" y="${(q[1]-1.5).toFixed(1)}" width="3" height="3" fill="#FBBF24"/>`;
  }
  for(const v of (g.vias||[])){
    const q=isoProj(v[0],v[1],0.05,W,H);
    out+=`<circle cx="${q[0].toFixed(1)}" cy="${q[1].toFixed(1)}" r="${Math.max(2,v[2]*4).toFixed(1)}" fill="none" stroke="#60A5FA" stroke-width="1.2"/>`;
  }
  out+=`<text x="10" y="${H-10}" font-size="10" fill="#64748B">拖拽旋转 · 滚轮缩放 · Shift+拖拽平移 · 双击复位</text></svg>`;
  return out;
}
function render(){
  const host=document.getElementById('view');
  host.innerHTML = tab==='2d'? S.board_svg : renderIso();
  const show0 = document.getElementById('layer0')?.checked ?? true;
  const show1 = document.getElementById('layer1')?.checked ?? true;
  host.querySelectorAll('polyline').forEach(el=>{
    const c=(el.getAttribute('stroke')||'').toUpperCase();
    if(c==='#C62828' && !show0) el.style.display='none';
    if(c==='#2E7D32' && !show1) el.style.display='none';
  });
  if(tab==='2d' && measurePts.length){
    const svg=host.querySelector('svg');
    if(svg){ for(const p of measurePts){
      const c=document.createElementNS('http://www.w3.org/2000/svg','circle');
      c.setAttribute('cx',p[0]); c.setAttribute('cy',p[1]); c.setAttribute('r','1.2');
      c.setAttribute('fill','#FBBF24'); svg.appendChild(c); } }
  }
  if(tab==='2d' && compSel && S.components){
    const svg=host.querySelector('svg');
    const me=(S.components||[]).find(c=>c.id===compSel);
    if(svg && me){
      const vb=svg.viewBox.baseVal;
      const dash=(x1,y1,x2,y2)=>{
        const el=document.createElementNS('http://www.w3.org/2000/svg','line');
        el.setAttribute('x1',x1); el.setAttribute('y1',y1);
        el.setAttribute('x2',x2); el.setAttribute('y2',y2);
        el.setAttribute('stroke','#38BDF8'); el.setAttribute('stroke-width','0.15');
        el.setAttribute('stroke-dasharray','1.2,0.8'); el.setAttribute('pointer-events','none');
        svg.appendChild(el);
      };
      for(const o of S.components){
        if(o.id===me.id) continue;
        if(Math.abs(o.x-me.x)<0.5) dash(o.x, 0, o.x, vb.height);   // 同列
        if(Math.abs(o.y-me.y)<0.5) dash(0, o.y, vb.width, o.y);   // 同行
      }
    }
  }
  if(tab==='2d' && drawing.length){
    const svg=host.querySelector('svg');
    if(svg){const vb=svg.viewBox.baseVal;const pts=drawing.map(p=>`${p[0]},${p[1]}`).join(' ');
      const el=document.createElementNS('http://www.w3.org/2000/svg','polyline');
      el.setAttribute('points',pts);el.setAttribute('fill','none');
      el.setAttribute('stroke','#FBBF24');el.setAttribute('stroke-width','0.6');
      svg.appendChild(el);}
  }
}
document.getElementById('view').addEventListener('click',e=>{
  if(tab!=='2d') return;
  const svg=e.target.closest('svg'); if(!svg) return;
  const r=svg.getBoundingClientRect(); const vb=svg.viewBox.baseVal;
  const x=(e.clientX-r.left)*vb.width/r.width, y=(e.clientY-r.top)*vb.height/r.height;
  const px=Math.round(x*100)/100, py=Math.round(y*100)/100;
  if(document.getElementById('drawMode').checked){ drawing.push([px,py]); render(); return; }
  if(document.getElementById('measureMode').checked){
    measurePts.push([px,py]);
    if(measurePts.length>=2){
      const d=Math.hypot(measurePts[1][0]-measurePts[0][0], measurePts[1][1]-measurePts[0][1]);
      document.getElementById('measureOut').textContent=d.toFixed(2)+' mm';
      measurePts=[measurePts[1]];   // 保留后一点作为下一段起点
    } else {
      document.getElementById('measureOut').textContent='再点一点';
    }
    render();
  }
});
function clearDraw(){drawing=[];render();}
async function saveRoute(){
  if(!drawing.length){log('没有点可保存');return;}
  const net=document.getElementById('netSel').value;
  const width=parseFloat(document.getElementById('widthInput').value||'0.6');
  const layer=parseInt(document.getElementById('layerSel').value||'0');
  const r=await fetch('/api/route',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({net:net,points:drawing,width:width,layer:layer})});
  const j=await r.json(); log('画线：'+(j.message||'')); if(j.ok){drawing=[];await refresh();}
}
async function compOp(name, body){
  const r=await fetch('/api/component/'+name,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const j=await r.json(); log('器件-'+name+'：'+(j.message||j.error||'')); await refresh();
}
function pickComp(id){
  compSel=id;
  document.querySelectorAll('#comps tbody tr').forEach(tr=>tr.classList.toggle('sel', tr.dataset.id===id));
}
function compMove(){
  if(!compSel){log('先选一个器件');return;}
  compOp('move',{component_id:compSel,
    dx:parseFloat(document.getElementById('compDx').value||0),
    dy:parseFloat(document.getElementById('compDy').value||0),
    snap_mm:parseFloat(document.getElementById('compSnap').value||0)||null});
}
function compRotate(){ if(!compSel){log('先选一个器件');return;} compOp('rotate',{component_id:compSel,deg:90}); }
function compFlip(){ if(!compSel){log('先选一个器件');return;} compOp('flip',{component_id:compSel}); }
function compLock(){
  if(!compSel){log('先选一个器件');return;}
  const c=(S.components||[]).find(x=>x.id===compSel);
  compOp('lock',{component_id:compSel,locked:!(c&&c.locked)});
}
function pick(id, multi){
  if(multi){ const i=multiSel.indexOf(id); if(i>=0) multiSel.splice(i,1); else multiSel.push(id); }
  else { multiSel=[id]; }
  sel=id;const r=(S.routes||[]).find(x=>x.id===id);
  document.getElementById('prop').innerHTML=`<b>网络</b><span>${r.net}</span><b>层</b><span>${r.layer}</span>
    <b>宽度</b><span>${r.width} mm</span><b>长度</b><span>${r.length} mm</span>
    <b>多选</b><span>${multiSel.length} 条</span>`;
  document.getElementById('newWidth').value=r.width; render();
  document.querySelectorAll('#routes tbody tr').forEach(tr=>tr.classList.toggle('sel',multiSel.includes(tr.dataset.id)));}
async function applyWidth(){
  if(!sel){log('先选中一条走线');return;}
  const w=parseFloat(document.getElementById('newWidth').value);
  const r=await fetch('/api/route/width',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({route_id:sel,width_mm:w})});
  log('改宽度：'+(await r.json()).message); await refresh();
}
async function reshape(){
  if(!sel){log('先选中一条走线');return;}
  if(drawing.length<2){log('先画至少两个点');return;}
  const r=await fetch('/api/route/points',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({route_id:sel,points:drawing})});
  log('改道：'+(await r.json()).message); drawing=[]; await refresh();
}
async function newProject(){
  const name=prompt('工程名','新工程'); if(!name) return;
  const r=await fetch('/api/new-project',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:name,w:60,h:40})});
  log('新建：'+(await r.json()).message); await refresh();
}
async function newRule(){
  const label=document.getElementById('ruleLabel').value||'v-next';
  const raw=document.getElementById('ruleChanges').value.split(',').map(s=>s.trim()).filter(Boolean);
  if(!raw.length){log('先填一个阈值变更，如 R1.min_width_mm=0.4');return;}
  const r=await fetch('/api/rules',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({label:label,changes:raw})});
  log('规则：'+(await r.json()).message); await refresh();
}
async function switchProject(id){
  const r=await fetch('/api/select',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project_id:id})});
  log('切换工程：'+(await r.json()).message); sel=null; drawing=[]; await refresh();
}
async function undoTo(seq){
  if(!confirm('回退到第 '+seq+' 步之后？')) return;
  const r=await fetch('/api/undo-to',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({seq:seq})});
  log('回退：'+(await r.json()).message); await refresh();
}
async function batchWidth(){
  if(!multiSel.length){log('先选中走线（Shift+点击可多选）');return;}
  const w=parseFloat(document.getElementById('newWidth').value);
  const r=await fetch('/api/routes/width',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({route_ids:multiSel,width_mm:w})});
  log('批量改宽：'+(await r.json()).message); await refresh();
}
async function suggestWidth(){
  if(!multiSel.length){log('先选中走线（Shift+点击可多选）');return;}
  const r=await fetch('/api/width/suggest',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({route_ids:multiSel})});
  const d=await r.json();
  if(d.ok){
    document.getElementById('newWidth').value=d.width_mm;
    log(`建议 ${d.width_mm} mm（规则 ${d.score} 分，${d.mode}，${d.ms} ms；未改几何，点「批量改选中」才应用）`);
  } else { log('建议失败：'+(d.error||'未知')); }
}
async function netWidth(){
  if(!sel){log('先选中一条走线');return;}
  const w=parseFloat(document.getElementById('newWidth').value);
  const net=(S.routes||[]).find(x=>x.id===sel)?.net;
  const r=await fetch('/api/net/width',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({net:net,width_mm:w})});
  log('整网改宽：'+(await r.json()).message); await refresh();
}
async function deleteRoute(){
  if(!sel){log('先选中一条走线');return;}
  const r=await fetch('/api/route/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({route_id:sel})});
  log('删除：'+(await r.json()).message); sel=null; await refresh();
}
refresh(); setInterval(refresh, 10000);
(function(){
  const host=document.getElementById('view');
  let drag=null;
  host.addEventListener('mousedown',e=>{
    if(tab!=='3d') return;
    drag={x:e.clientX,y:e.clientY,pan:e.shiftKey||e.button===1};
    e.preventDefault();
  });
  window.addEventListener('mousemove',e=>{
    if(!drag) return;
    const dx=e.clientX-drag.x, dy=e.clientY-drag.y;
    drag.x=e.clientX; drag.y=e.clientY;
    if(drag.pan){ isoView.px+=dx; isoView.py+=dy; }
    else {
      isoView.az+=dx*0.012;
      isoView.el=Math.max(0.05,Math.min(1.45, isoView.el+dy*0.012));
    }
    if(tab==='3d') render();
  });
  window.addEventListener('mouseup',()=>{drag=null;});
  host.addEventListener('wheel',e=>{
    if(tab!=='3d') return;
    e.preventDefault();
    isoView.zoom=Math.max(0.3,Math.min(6, isoView.zoom*Math.exp(-e.deltaY*0.0012)));
    render();
  },{passive:false});
  host.addEventListener('dblclick',()=>{
    if(tab!=='3d') return;
    isoView={az:Math.PI/4, el:Math.PI/6, zoom:1, px:0, py:0}; render();
  });

  // ---- 2D 拖拽改道：只允许拖中间拐点（首尾接焊盘，拖了会拉断） ----
  let dragRoute=null;
  function svgPt(svg, e){
    const rect=svg.getBoundingClientRect(), vb=svg.viewBox.baseVal;
    return [(e.clientX-rect.left)*vb.width/rect.width,
            (e.clientY-rect.top)*vb.height/rect.height];
  }
  host.addEventListener('mousedown',e=>{
    if(tab!=='2d'||document.getElementById('drawMode').checked) return;
    const el=e.target.closest('polyline.route-hit');
    if(!el) return;
    const r=(S.routes||[]).find(x=>x.id===el.dataset.id);
    if(!r||!r.pts||r.pts.length<3) return;
    const svg=el.closest('svg');
    const [mx,my]=svgPt(svg,e);
    let idx=-1, best=1e9;
    r.pts.forEach((p,i)=>{
      if(i===0||i===r.pts.length-1) return;
      const d=Math.hypot(p[0]-mx,p[1]-my);
      if(d<best){best=d;idx=i;}
    });
    if(idx<0||best>4) return;
    dragRoute={id:r.id, pts:r.pts.map(p=>[p[0],p[1]]), idx, svg};
    e.preventDefault(); e.stopPropagation();
  });
  window.addEventListener('mousemove',e=>{
    if(!dragRoute) return;
    const [mx,my]=svgPt(dragRoute.svg,e);
    dragRoute.pts[dragRoute.idx]=[Math.round(mx*100)/100, Math.round(my*100)/100];
    const d=dragRoute.pts.map(p=>p.join(',')).join(' ');
    document.querySelectorAll(`polyline[data-id="${dragRoute.id}"]`)
      .forEach(el=>el.setAttribute('points',d));
  });
  window.addEventListener('mouseup',async ()=>{
    if(!dragRoute) return;
    const d=dragRoute; dragRoute=null;
    const r=await fetch('/api/route/points',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({route_id:d.id, points:d.pts})});
    const j=await r.json();
    log('拖拽改道：'+(j.message||'')); await refresh();
  });

  // ---- 快捷键（UI-006）：输入框聚焦时不拦截 ----
  window.addEventListener('keydown',e=>{
    const t=e.target;
    if(t && (t.tagName==='INPUT'||t.tagName==='SELECT'||t.tagName==='TEXTAREA')) return;
    if(e.ctrlKey||e.metaKey){
      if((e.key||'').toLowerCase()==='z'){
        e.preventDefault();
        act(e.shiftKey?'redo':'undo');
      }
      return;
    }
    if(e.key==='Delete'){ e.preventDefault(); deleteRoute(); }
    else if(e.key==='Tab'){ e.preventDefault(); setTab(tab==='2d'?'3d':'2d'); }
    else if((e.key||'').toLowerCase()==='f'){
      isoView={az:Math.PI/4, el:Math.PI/6, zoom:1, px:0, py:0}; render();
      log('视图已适配');
    }
    else if(e.key==='Escape'){
      document.getElementById('drawMode').checked=false;
      drawing=[]; render(); log('已退出当前工具');
    }
  });
})();
</script></body></html>"""


def _pid(db: DB) -> str:
    """当前工程：优先界面选中的，其次最近创建的。"""
    chosen = db.scalar("SELECT value_json FROM app_setting WHERE setting_key='ui.current_project'")
    if chosen:
        try:
            pid = json.loads(chosen)
        except Exception:
            pid = chosen
        if isinstance(pid, str) and db.one("SELECT 1 FROM project WHERE id=?", (pid,)):
            return pid
    return db.scalar("SELECT id FROM project ORDER BY created_at DESC, rowid DESC LIMIT 1")


def _latest(db: DB, table: str, pid: str, order: str = "created_at"):
    return db.one(f"SELECT * FROM {table} WHERE project_id=? ORDER BY {order} DESC, rowid DESC LIMIT 1", (pid,))


def state(db: DB) -> dict:
    p = db.one("SELECT * FROM project WHERE id=?", (_pid(db),)) if _pid(db) else None
    if not p:
        return {"project": None, "kv": {}, "issues": [], "board_svg": "", "iso_svg": "",
                "routes": [], "nets": [], "layers": [], "version_no": 0, "score": "—",
                "projects": []}
    pid = p["id"]
    v = _latest(db, "version", pid, "version_no")
    run = _latest(db, "mrc_run", pid)
    sl = _latest(db, "slice_set", pid)
    plan = _latest(db, "plan", pid)
    job = _latest(db, "print_job", pid)
    issues = db.q("SELECT * FROM mrc_issue WHERE run_id=? ORDER BY rule_code", (run["id"],)) if run else []
    circuit = db.one("SELECT * FROM circuit WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (pid,))
    routes, nets = [], []
    if circuit:
        nets = [r["name"] for r in db.q("SELECT name FROM net WHERE circuit_id=? ORDER BY name", (circuit["id"],))]
        for r in db.q("SELECT r.*, n.name FROM route r JOIN net n ON n.id=r.net_id "
                      "WHERE r.circuit_id=? AND r.deleted_at IS NULL ORDER BY n.name", (circuit["id"],)):
            routes.append({"id": r["id"], "net": r["name"], "layer": r["layer_no"],
                           "width": r["width_mm"], "length": r["length_mm"],
                           "pts": [[round(x, 3), round(y, 3)]
                                   for x, y in load_route_points(db, r)]})
    layers = []
    comps = []
    if circuit:
        for c in db.q("SELECT * FROM component WHERE circuit_id=? ORDER BY ref_des",
                      (circuit["id"],)):
            comps.append({"id": c["id"], "ref": c["ref_des"],
                          "x": round(float(c["x_mm"]), 2), "y": round(float(c["y_mm"]), 2),
                          "rot": int(c["rot_deg"]), "side": c["side"],
                          "locked": int(c["locked"])})
    if sl:
        for idx, title in ((3, "电路层介电坝"), (4, "电路层银浆")):
            layers.append({"title": title, "svg": report._svg_pass(db, sl["id"], idx, width=300)})
    kv = {
        "工程": p["name"], "版本": f"v{v['version_no']}" if v else "—",
        "分数": run["total_score"] if run else "—", "问题数": len(issues),
        "切片": f"{sl['pass_count']} 道次 / {sl['total_thickness_mm']} mm" if sl else "未切片",
        "Agent": (f"{plan['score_before']}→{plan['score_after']}（{plan['rounds_used']} 轮）"
                  if plan and plan["score_after"] is not None else "未运行"),
        "打印": job["status"] if job else "未开始",
        "规则版本": next((v["label"] for v in mrc.list_rule_versions(db) if v["is_active"]), "—"),
    }
    steps, diffs, plan_info, print_info = [], [], None, None
    if plan:
        steps = [{"no": s["step_no"], "tool": s["tool_name"], "intent": s["intent"],
                  "status": s["status"], "risk": s["risk_level"]}
                 for s in db.q("SELECT * FROM plan_step WHERE plan_id=? ORDER BY step_no",
                               (plan["id"],))]
        diffs = [{"before": d["score_before"], "after": d["score_after"],
                  "reason": d["reason"], "decision": d["decision"]}
                 for d in db.q("SELECT * FROM diff_card WHERE plan_id=? ORDER BY created_at",
                               (plan["id"],))]
        plan_info = {"status": plan["status"], "goal": plan["goal"],
                     "before": plan["score_before"], "after": plan["score_after"],
                     "rounds": f"{plan['rounds_used']}/{plan['round_limit']}",
                     "degraded": plan["degraded"],
                     "stop": plan["stop_reason"] or ""}
    if job:
        print_info = {"status": job["status"], "phase": job["phase"],
                      "layer": f"{job['layer_current']}/{job['layer_total']}",
                      "duration_s": job["duration_s"]}
    dev = db.one("SELECT * FROM device ORDER BY created_at DESC, rowid DESC LIMIT 1")
    # 对话面板（UI-001 右栏）：无外部聊天服务，内容由计划/步骤/对比/问题合成
    chat = []
    if plan:
        chat.append({"role": "user", "text": plan["goal"] or "把 MRC 分数提到 95 以上"})
        chat.append({"role": "assistant",
                     "text": f"当前 {plan['score_before']} 分，共 {len(issues)} 个问题"})
        for s in steps:
            chat.append({"role": "assistant",
                         "text": f"第 {s['no']} 步：{s['intent']}（{s['status']}）"})
        for d in diffs:
            chat.append({"role": "assistant",
                         "text": f"结果 {d['before']} → {d['after']}：{d['reason'] or ''}"})
    for i in issues[:5]:
        chat.append({"role": "system", "text": f"{i['rule_code']}：{i['message']}"})
    return {"project": p["name"], "version_no": v["version_no"] if v else 0,
            "score": run["total_score"] if run else "—", "kv": kv,
            "plan": plan_info, "steps": steps, "diffs": diffs, "print": print_info,
            "chat": chat,
            "device": dev["name"] if dev else None,
            "issues": [{"rule": i["rule_code"], "severity": i["severity"],
                        "message": i["message"]} for i in issues],
            "board_svg": report._svg_board(db, pid, width=760),
            "iso_svg": report._svg_iso(db, pid, width=760),
            "routes": routes, "nets": nets, "layers": layers,
            "components": comps,
            "iso": report.iso_geometry(db, pid),
            "history": history.current_stack(db, pid),
            "rules": mrc.list_rule_versions(db),
            "projects": [{"id": r["id"], "name": r["name"]}
                         for r in db.q("SELECT id,name FROM project WHERE deleted_at IS NULL "
                                       "ORDER BY created_at DESC, rowid DESC")]}


class Handler(BaseHTTPRequestHandler):
    db: DB = None
    workspace: Path = None
    jobs: jobs.Registry = jobs.Registry()

    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json", code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(INDEX.encode(), "text/html; charset=utf-8")
        if path.startswith("/api/job/"):
            job = self.jobs.get(path.rsplit("/", 1)[-1])
            if not job:
                return self._json({"ok": False, "error": "任务不存在"}, 404)
            return self._json({"ok": True, **job.snapshot()})
        if path == "/api/state":
            try:
                return self._json(state(self.db))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path.lstrip("/api/")
        try:
            if path == "route":
                return self._json({"ok": True, "message": self._create_route(self._body())})
            if path == "route/width":
                return self._json({"ok": True, "message": self._set_width(self._body())})
            if path == "route/delete":
                return self._json({"ok": True, "message": self._delete_route(self._body())})
            if path == "route/points":
                return self._json({"ok": True, "message": self._reshape(self._body())})
            if path == "net/width":
                return self._json({"ok": True, "message": self._net_width(self._body())})
            if path == "routes/width":
                body = self._body()
                c = self._circuit()
                out = history.change_routes_width(self.db, _pid(self.db), c["id"],
                                                  list(body.get("route_ids", [])),
                                                  float(body["width_mm"]))
                return self._json({"ok": True,
                                  "message": f"{out['routes']} 条走线改为 {out['width_mm']} mm"})
            if path == "width/suggest":
                return self._json({"ok": True, **self._width_suggest(self._body())})
            if path.startswith("component/"):
                return self._json({"ok": True, **self._component(path.split("/", 1)[1], self._body())})
            if path == "job/slice":
                return self._json({"ok": True, **self._submit_slice(self._body())})
            if path.startswith("job/") and path.endswith("/cancel"):
                jid = path.split("/")[1]
                ok = self.jobs.cancel(jid)
                return self._json({"ok": True, "cancelled": ok,
                                   "message": "已请求取消（道次边界生效）" if ok else "任务不在运行中"})
            if path == "rules":
                body = self._body()
                changes = {}
                for item in body.get("changes", []):
                    k, v = item.split("=", 1)
                    changes[k.strip()] = float(v)
                if not changes:
                    raise ValueError("没有阈值变更")
                out = mrc.create_rule_version(self.db, body.get("label", "v-next"), changes)
                pid = _pid(self.db)
                ver = self.db.scalar("SELECT id FROM version WHERE project_id=? "
                                     "ORDER BY version_no DESC LIMIT 1", (pid,))
                run = mrc.run(self.db, pid, ver, rule_version_id=out["rule_version_id"])
                return self._json({"ok": True,
                                  "message": f"规则版本 {out['label']}：分数 {run['score']}"})
            if path == "new-project":
                body = self._body()
                name = body.get("name") or "新工程"
                pid = self.db.ins("project", name=name, unit="mm",
                                  sheet_w_mm=float(body.get("w", 60)),
                                  sheet_h_mm=float(body.get("h", 40)),
                                  origin_kind="board_bottom_left", data_schema_version=1,
                                  dir_rel_path=f"projects/{name}",
                                  created_at=now(), updated_at=now())
                v0 = self.db.ins("version", project_id=pid, version_no=0, kind="initial",
                                 author_kind="human", author_name="local",
                                 reason="新建工程", created_at=now())
                self.db.c.execute("UPDATE project SET current_version_id=? WHERE id=?", (v0, pid))
                self.db.set_setting("ui.current_project", pid)
                self.db.commit()
                return self._json({"ok": True, "message": f"已新建工程 {name}"})
            if path == "select":
                body = self._body()
                pid = body.get("project_id")
                if not self.db.one("SELECT 1 FROM project WHERE id=?", (pid,)):
                    raise ValueError("工程不存在")
                self.db.set_setting("ui.current_project", pid)
                self.db.commit()
                return self._json({"ok": True, "message": "已切换工程"})
            if path in ("undo", "redo"):
                return self._json({"ok": True, "message": self._undo_redo(path)})
            if path == "undo-to":
                body = self._body()
                sid = history.ensure_session(self.db, _pid(self.db))
                out = history.undo_to(self.db, sid, int(body["seq"]))
                return self._json({"ok": True,
                                  "message": f"回退 {out['undone']} 步到 seq {out['target_seq']}，版本号未变"})
            return self._json({"ok": True, "message": self._action(path)})
        except Exception as exc:
            return self._json({"ok": False, "message": f"{type(exc).__name__}: {exc}"}, 500)

    # ---- 走线编辑（与界面等价） ----
    def _circuit(self) -> dict:
        return self.db.one("SELECT * FROM circuit WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                           (_pid(self.db),))

    def _create_route(self, body: dict) -> str:
        c = self._circuit()
        net = self.db.one("SELECT * FROM net WHERE circuit_id=? AND name=?", (c["id"], body["net"]))
        if not net:
            raise ValueError(f"网络不存在：{body.get('net')}")
        pts = [(float(x), float(y)) for x, y in body["points"]]
        if len(pts) < 2:
            raise ValueError("至少两个点")
        w = float(body.get("width", 0.6))
        rid = history.create_route(self.db, _pid(self.db), c["id"], net["id"], pts, w,
                                   int(body.get("layer", 0)))
        return f"已新增走线 {rid[:8]}（{net['name']}，{w} mm）"

    def _reshape(self, body: dict) -> str:
        rid = body["route_id"]
        pts = [(float(x), float(y)) for x, y in body["points"]]
        if len(pts) < 2:
            raise ValueError("至少两个点")
        out = history.change_points(self.db, _pid(self.db), rid, pts)
        return f"改道完成，{out['points']} 个拐点（可撤销）"

    def _undo_redo(self, name: str) -> str:
        sid = history.ensure_session(self.db, _pid(self.db))
        out = history.undo(self.db, sid) if name == "undo" else history.redo(self.db, sid)
        if not out:
            return "没有可撤销/重做的操作"
        return "已撤销一步，版本号未变" if name == "undo" else "已重做一步"

    def _submit_slice(self, body: dict) -> dict:
        """UI-005：切片丢给后台任务，返回 job_id，不阻塞请求。

        取消后由 rollback 删掉本次 slice_set（子表 CASCADE），
        保证“取消的切片不会在库里留下半成品”。
        """
        db = self.db
        pid = _pid(db)
        vid = _latest(db, "version", pid, "version_no")["id"]
        base = mrc.ensure_baseline(db)
        prof = body.get("profile_id") or base["process_profile_id"]
        matb = body.get("material_batch_id") or base["material_batch_id"]
        created: dict = {}

        def work(job):
            def progress(i, total):
                job.progress, job.total = i, total
            out = slicing.run(db, pid, vid, prof, matb,
                              progress=progress, should_cancel=job.cancelled)
            created["slice_id"] = out["slice_id"]
            return out

        def rollback():
            sid = created.get("slice_id")
            if sid:
                db.c.execute("DELETE FROM slice_set WHERE id=?", (sid,))
                db.commit()

        jid = self.jobs.submit("slice", work, rollback=rollback)
        return {"job_id": jid, "message": "切片任务已提交"}

    def _component(self, kind: str, body: dict) -> dict:
        """CIR-002 器件操作：锁定后拒绝移动/旋转/翻面。"""
        c = self._circuit()
        cid = body.get("component_id") or ""
        kw = {}
        if kind == "move":
            kw = {"dx": float(body.get("dx", 0)), "dy": float(body.get("dy", 0)),
                  "snap_mm": float(body["snap_mm"]) if body.get("snap_mm") else None}
        elif kind == "rotate":
            kw = {"deg": int(body.get("deg", 90))}
        elif kind == "lock":
            kw = {"locked": bool(body.get("locked"))}
        elif kind != "flip":
            raise ValueError(f"未知器件操作：{kind}")
        out = history.component_op(self.db, _pid(self.db), c["id"], cid, kind, **kw)
        label = {"move": "移动", "rotate": "旋转", "flip": "翻面", "lock": "锁定"}[kind]
        return {"message": f"{label} {out['after']['component_id'][:8]} 完成（可撤销）",
                "before": out["before"], "after": out["after"]}

    def _width_suggest(self, body: dict) -> dict:
        """宽度扫描：代理粗排 + 规则确认。只给建议，不改几何。"""
        from . import surrogate as sg, sweep as sweep_mod
        c = self._circuit()
        if not c:
            raise ValueError("当前工程还没有电路，无法给宽度建议")
        pid = _pid(self.db)
        v = _latest(self.db, "version", pid, "version_no")
        bundle = sg.load(self.db.root)
        out = sweep_mod.width_sweep(self.db, pid, v["id"], c["id"],
                                    route_ids=list(body.get("route_ids", [])),
                                    bundle=bundle)
        best = out.get("best_surrogate") or out["best_exact"]
        ms = out.get("ms_surrogate", out["ms_exact"])
        return {"width_mm": best["width_mm"], "score": best["score"],
                "mode": "代理粗排" if bundle else "穷举", "ms": ms,
                "agree": out.get("agree")}

    def _net_width(self, body: dict) -> str:
        c = self._circuit()
        net = self.db.one("SELECT * FROM net WHERE circuit_id=? AND name=?", (c["id"], body["net"]))
        if not net:
            raise ValueError(f"网络不存在：{body.get('net')}")
        out = history.change_net_width(self.db, _pid(self.db), c["id"], net["id"],
                                       float(body["width_mm"]))
        return f"{net['name']}：{out['routes']} 条走线改为 {out['width_mm']} mm（可逐段撤销）"

    def _set_width(self, body: dict) -> str:
        c = self._circuit()
        out = history.change_width(self.db, _pid(self.db), c["id"],
                                   body["route_id"], float(body["width_mm"]))
        return f"宽度 {out['before']} → {out['after']} mm（可撤销）"

    def _delete_route(self, body: dict) -> str:
        out = history.delete_route(self.db, _pid(self.db), body["route_id"])
        return f"已删除 {out['route_id'][:8]}（软删除，可撤销）"

    def _action(self, name: str) -> str:
        db = self.db
        pid = _pid(db)
        if not pid:
            raise ValueError("没有工程")
        v = db.scalar("SELECT id FROM version WHERE project_id=? ORDER BY version_no DESC LIMIT 1", (pid,))
        if name == "check":
            out = mrc.run(db, pid, v)
            return f"分数 {out['score']}，问题 {len(out['issues'])} 条"
        if name == "slice":
            base = mrc.ensure_baseline(db)
            out = slicing.run(db, pid, v, base["process_profile_id"], base["material_batch_id"])
            return f"切片 {out['passes']} 道次，{out['paths']} 段，{out['length_mm']} mm"
        if name == "export":
            sl = db.scalar("SELECT id FROM slice_set WHERE project_id=? AND status='ready' "
                           "ORDER BY created_at DESC LIMIT 1", (pid,))
            if not sl:
                raise ValueError("没有可用切片，请先切片")
            out = gcode.build(db, sl)
            return f"G-code {out['content_hash'][:16]}，{out['bytes'] // 1024} KB"
        if name == "print":
            sl = db.scalar("SELECT id FROM slice_set WHERE project_id=? AND status='ready' "
                           "ORDER BY created_at DESC LIMIT 1", (pid,))
            exp = db.scalar("SELECT id FROM gcode_export WHERE slice_set_id=? "
                            "ORDER BY created_at DESC LIMIT 1", (sl,)) if sl else None
            if not (sl and exp):
                raise ValueError("请先切片并导出 G-code")
            out = printer.run_demo(db, pid, sl, exp, fault_layer=2)
            return f"作业 {out['job_id'][:8]}，{out['replay']['result']}，缺陷已归因"
        if name == "agent":
            out = agent.iterate(db, pid, v)
            return f"{out['score_before']} → {out['score_after']}，{out['rounds']} 轮，{out['status']}"
        if name == "report":
            path = report.build(db, pid, self.workspace / "report.html")
            return f"报告已生成：{path.name}"
        raise ValueError(f"未知操作 {name}")


def serve(workspace: str | Path, port: int = 8765) -> None:
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    Handler.db = DB(ws / "app.db")
    Handler.workspace = ws
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"界面已启动：http://127.0.0.1:{port}  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        Handler.db.close()
