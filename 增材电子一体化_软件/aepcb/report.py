"""HTML 报告：SVG 板图 + 问题列表 + 层预览 + 验收结果，单文件、离线可看。"""
from __future__ import annotations

import html
import json
from pathlib import Path

from .board import load_route_points
from .store import DB

LAYER_COLORS = {"bottom_mask": "#B0BEC5", "circuit": "#1565C0", "top_mask": "#EF6C00"}


def _svg_board(db: DB, pid: str, width: int = 900) -> str:
    circuit = db.one("SELECT * FROM circuit WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (pid,))
    if not circuit:
        return "<p>没有电路数据</p>"
    w, h = circuit["board_w_mm"], circuit["board_h_mm"]
    scale = width / w
    parts = [f'<svg viewBox="0 0 {w} {h + 2}" width="{width}" height="{int((h + 2) * scale)}" '
             f'style="background:#FAFAFA;border:1px solid #CFD8DC">']
    parts.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="#FFFFFF" stroke="#263238" stroke-width="0.3"/>')
    for c in db.q("SELECT * FROM component WHERE circuit_id=?", (circuit["id"],)):
        parts.append(f'<rect x="{c["x_mm"] - 2:.2f}" y="{c["y_mm"] - 2:.2f}" width="4" height="4" '
                     f'fill="#ECEFF1" stroke="#607D8B" stroke-width="0.2"/>')
        parts.append(f'<text x="{c["x_mm"]:.2f}" y="{c["y_mm"] + 0.7:.2f}" font-size="1.4" '
                     f'text-anchor="middle" fill="#37474F">{html.escape(c["ref_des"])}</text>')
    for p in db.q("SELECT * FROM pad WHERE component_id IN (SELECT id FROM component WHERE circuit_id=?)",
                  (circuit["id"],)):
        parts.append(f'<rect x="{p["x_mm"] - p["w_mm"]/2:.2f}" y="{p["y_mm"] - p["h_mm"]/2:.2f}" '
                     f'width="{p["w_mm"]:.2f}" height="{p["h_mm"]:.2f}" fill="#FFD54F" stroke="#F57F17" stroke-width="0.1"/>')
    for r in db.q("SELECT * FROM route WHERE circuit_id=? AND deleted_at IS NULL", (circuit["id"],)):
        pts = load_route_points(db, r)
        d = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
        color = "#C62828" if r["layer_no"] == 0 else "#2E7D32"
        parts.append(f'<polyline class="route-line" data-id="{r["id"]}" points="{d}" fill="none" '
                     f'stroke="{color}" stroke-width="{r["width_mm"]:.2f}" stroke-opacity="0.75"/>')
        # 透明命中线：实际线宽只有 0.4–0.8 mm，直接点几乎点不中
        parts.append(f'<polyline class="route-hit" data-id="{r["id"]}" points="{d}" fill="none" '
                     f'stroke="transparent" stroke-width="{max(1.6, r["width_mm"] * 2.5):.2f}" '
                     f'style="pointer-events:stroke;cursor:move"/>')
    parts.append(f'<text x="1" y="{h + 1.4:.2f}" font-size="1.3" fill="#546E7A">'
                 f'{w:.0f}×{h:.0f} mm；红=层0 绿=层1；黄=焊盘</text>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_pass(db: DB, slice_id: str, pass_index: int, width: int = 380, limit: int = 400) -> str:
    p = db.one("SELECT * FROM deposit_pass WHERE slice_set_id=? AND pass_index=?", (slice_id, pass_index))
    if not p:
        return ""
    sl = db.one("SELECT * FROM slice_set WHERE id=?", (slice_id,))
    circuit = db.one("SELECT * FROM circuit WHERE project_id=?", (sl["project_id"],))
    w, h = circuit["board_w_mm"], circuit["board_h_mm"]
    parts = [f'<svg viewBox="0 0 {w} {h}" width="{width}" height="{int(width * h / w)}" '
             f'style="background:#0B1021;border:1px solid #233">']
    paths = db.q("SELECT * FROM slice_path WHERE deposit_pass_id=? AND path_kind='extrude' "
                 "ORDER BY seq LIMIT ?", (p["id"], limit))
    for row in paths:
        geom = json.loads(db.get_blob(row["geom_blob_hash"]))
        d = " ".join(f"{pt[0]:.2f},{pt[1]:.2f}" for pt in geom)
        color = "#64B5F6" if p["pattern"] in ("solid", "dam_and_trench", "pad_pit") else "#FFB74D"
        parts.append(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="0.12" '
                     f'stroke-opacity="0.9"/>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_iso(db: DB, pid: str, width: int = 900) -> str:
    """2.5D 等轴视图：把两层走线、焊盘、板框按高度错开绘制。"""
    circuit = db.one("SELECT * FROM circuit WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (pid,))
    if not circuit:
        return "<p>没有电路数据</p>"
    w, h = circuit["board_w_mm"], circuit["board_h_mm"]
    z0, z1 = 0.10, 0.0
    s = width / (w + h) * 1.55
    cx, cy = width * 0.5, width * 0.42

    def proj(x, y, z):
        return (cx + (x - y) * 0.866 * s, cy + ((x + y) * 0.5 - z * 22) * s)

    parts = [f'<svg viewBox="0 0 {width} {int(width * 0.62)}" width="{width}" '
             f'height="{int(width * 0.62)}" style="background:#0B1220;border-radius:8px">']
    corners = [proj(0, 0, 0), proj(w, 0, 0), proj(w, h, 0), proj(0, h, 0)]
    parts.append('<polygon points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in corners) +
                 '" fill="#132033" stroke="#3B82F6" stroke-width="1" stroke-opacity="0.6"/>')
    for i in range(6):
        x = w * i / 5
        a, b = proj(x, 0, 0), proj(x, h, 0)
        parts.append(f'<line x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}" '
                     f'stroke="#1E3A5F" stroke-width="0.5"/>')
    for c in db.q("SELECT * FROM component WHERE circuit_id=?", (circuit["id"],)):
        x, y = c["x_mm"] - 2, c["y_mm"] - 2
        pts = [proj(x, y, 1.2), proj(x + 4, y, 1.2), proj(x + 4, y + 4, 1.2), proj(x, y + 4, 1.2)]
        parts.append('<polygon points="' + " ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in pts) +
                     '" fill="#334155" stroke="#94A3B8" stroke-width="0.6"/>')
        t = proj(c["x_mm"], c["y_mm"], 1.6)
        parts.append(f'<text x="{t[0]:.1f}" y="{t[1]:.1f}" font-size="9" fill="#CBD5E1" '
                     f'text-anchor="middle">{html.escape(c["ref_des"])}</text>')
    for r in db.q("SELECT * FROM route WHERE circuit_id=? AND deleted_at IS NULL ORDER BY layer_no",
                  (circuit["id"],)):
        pts = load_route_points(db, r)
        z = z0 if r["layer_no"] == 0 else z1
        color = "#F87171" if r["layer_no"] == 0 else "#34D399"
        d = " ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in (proj(x, y, z) for x, y in pts))
        parts.append(f'<polyline points="{d}" fill="none" stroke="{color}" '
                     f'stroke-width="{max(1.2, r["width_mm"] * s * 1.4):.1f}" stroke-opacity="0.85"/>')
    for p in db.q("SELECT * FROM pad WHERE component_id IN (SELECT id FROM component WHERE circuit_id=?)",
                  (circuit["id"],)):
        q = proj(p["x_mm"], p["y_mm"], 0.35)
        parts.append(f'<rect x="{q[0] - 2:.1f}" y="{q[1] - 2:.1f}" width="4" height="4" fill="#FBBF24"/>')
    parts.append(f'<text x="10" y="{int(width * 0.62) - 10}" font-size="10" fill="#64748B">'
                 f'等轴视图：红=层 0（z≈0.10）绿=层 1（z≈0.00），灰块=器件本体</text>')
    parts.append("</svg>")
    return "".join(parts)


def iso_geometry(db: DB, pid: str) -> dict:
    """导出等轴视图所需的 3D 几何（不含屏幕坐标）。

    投影放到前端做：拖拽旋转时不必回服务端重算，帧率才上得去。
    坐标单位 mm，z 为层高（层 0 高 0.10，层 1 为 0）。
    """
    circuit = db.one("SELECT * FROM circuit WHERE project_id=? "
                     "ORDER BY created_at DESC, rowid DESC LIMIT 1", (pid,))
    if not circuit:
        return {"board": None, "routes": [], "comps": [], "pads": [], "vias": []}
    cid = circuit["id"]
    z_of = {0: 0.10, 1: 0.0}
    routes = []
    for r in db.q("SELECT * FROM route WHERE circuit_id=? AND deleted_at IS NULL "
                  "ORDER BY layer_no, rowid", (cid,)):
        pts = load_route_points(db, r)
        routes.append({
            "pts": [[round(x, 3), round(y, 3), z_of.get(r["layer_no"], 0.0)] for x, y in pts],
            "color": "#F87171" if r["layer_no"] == 0 else "#34D399",
            "w": round(float(r["width_mm"]), 3),
            "layer": int(r["layer_no"]),
        })
    comps = []
    for c in db.q("SELECT * FROM component WHERE circuit_id=?", (cid,)):
        keys = c.keys()
        comps.append({
            "x": float(c["x_mm"]), "y": float(c["y_mm"]),
            "w": float(c["body_w_mm"]) if "body_w_mm" in keys else 4.0,
            "h": float(c["body_h_mm"]) if "body_h_mm" in keys else 4.0,
            "ref": c["ref_des"],
        })
    pads = [[float(p["x_mm"]), float(p["y_mm"]), 0.35]
            for p in db.q("SELECT * FROM pad WHERE component_id IN "
                          "(SELECT id FROM component WHERE circuit_id=?)", (cid,))]
    vias = [[float(v["x_mm"]), float(v["y_mm"]), float(v["diameter_mm"])]
            for v in db.q("SELECT * FROM via WHERE circuit_id=?", (cid,))]
    return {"board": [float(circuit["board_w_mm"]), float(circuit["board_h_mm"])],
            "routes": routes, "comps": comps, "pads": pads, "vias": vias}


def _svg_trend(db: DB, pid: str, width: int = 900, height: int = 160) -> str:
    runs = db.q("SELECT total_score FROM mrc_run WHERE project_id=? "
                "ORDER BY created_at, rowid", (pid,))[-20:]
    if len(runs) < 2:
        return "<p style='color:#78909C;font-size:13px'>评分历史不足两次，暂无趋势</p>"
    pad = 28
    xs = [pad + i * (width - 2 * pad) / (len(runs) - 1) for i in range(len(runs))]
    ys = [height - pad - (float(r["total_score"]) / 100.0) * (height - 2 * pad) for r in runs]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#1E88E5"/>'
                   for x, y in zip(xs, ys))
    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'style="background:#FAFAFA;border:1px solid #CFD8DC;border-radius:8px">'
            f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" '
            f'stroke="#CFD8DC"/>'
            f'<polyline points="{pts}" fill="none" stroke="#1E88E5" stroke-width="2"/>{dots}'
            f'<text x="{pad}" y="{height - 6}" font-size="11" fill="#78909C">第 1 次</text>'
            f'<text x="{width - pad - 40}" y="{height - 6}" font-size="11" fill="#78909C">'
            f'第 {len(runs)} 次</text></svg>')


def build(db: DB, pid: str, out_path: str | Path) -> Path:
    project = db.one("SELECT * FROM project WHERE id=?", (pid,))
    version = db.one("SELECT * FROM version WHERE project_id=? ORDER BY version_no DESC LIMIT 1", (pid,))
    run = db.one("SELECT * FROM mrc_run WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (pid,))
    issues = db.q("SELECT * FROM mrc_issue WHERE run_id=? ORDER BY severity, rule_code", (run["id"],)) if run else []
    sl = db.one("SELECT * FROM slice_set WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (pid,))
    acc = db.q("SELECT * FROM accept_result WHERE project_id=? ORDER BY task_no", (pid,))
    plan = db.one("SELECT * FROM plan WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (pid,))

    rows = "".join(
        f"<tr><td>{html.escape(i['rule_code'])}</td><td>{html.escape(i['severity'])}</td>"
        f"<td>{html.escape(i['message'])}</td>"
        f"<td>{'' if i['measured_num'] is None else round(i['measured_num'], 3)}</td></tr>" for i in issues)
    acc_rows = "".join(
        f"<tr><td>{r['task_no']}</td><td>{'✅' if r['passed'] else '❌'}</td>"
        f"<td>{html.escape(r['notes'] or '')}</td></tr>" for r in acc)

    previews = ""
    if sl:
        for idx, kind in ((3, "电路层介电坝"), (4, "电路层银浆填充"), (5, "上保护层")):
            previews += (f"<figure><figcaption>{kind}（道次 {idx}）</figcaption>"
                         f"{_svg_pass(db, sl['id'], idx)}</figure>")

    # 审计：规则版本 / 计划步骤 / 对比卡片
    rules = db.q("SELECT * FROM mrc_rule_version ORDER BY created_at DESC, rowid DESC")
    rule_rows = "".join(
        f"<tr><td>{'●' if r['is_active'] else '○'} {html.escape(r['label'])}</td>"
        f"<td>{html.escape(r['note'] or '')}</td><td>{r['created_at']}</td></tr>" for r in rules)
    steps = db.q("SELECT * FROM plan_step WHERE plan_id=? ORDER BY step_no", (plan["id"],)) if plan else []
    step_rows = "".join(
        f"<tr><td>{s['step_no']}</td><td>{html.escape(s['intent'])}</td>"
        f"<td>{html.escape(s['tool_name'])}</td><td>{s['status']}</td></tr>" for s in steps)
    card = db.one("SELECT * FROM diff_card WHERE plan_id=? ORDER BY created_at DESC LIMIT 1",
                  (plan["id"],)) if plan else None
    card_html = (f"<p style='font-size:13px;color:#37474F'>处置：<b>{card['decision']}</b>；"
                 f"分数 {card['score_before']} → {card['score_after']}；"
                 f"理由：{html.escape(card['reason'] or '')}</p>") if card else "<p>暂无对比卡片</p>"

    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(project['name'])} · 增材电子一体化软件</title>
<style>
body{{font-family:-apple-system,"Noto Sans CJK SC",sans-serif;margin:0;background:#F5F7FA;color:#1A237E}}
header{{background:#1A237E;color:#fff;padding:18px 28px}}
h1{{margin:0;font-size:20px}} .sub{{opacity:.8;font-size:13px;margin-top:6px}}
main{{padding:20px 28px;max-width:1280px}}
.cards{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:18px}}
.card{{background:#fff;border-radius:10px;padding:14px 18px;box-shadow:0 1px 4px rgba(0,0,0,.08);min-width:150px}}
.card b{{display:block;font-size:26px;color:#0D47A1}}
table{{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden;
box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:18px}}
th,td{{padding:8px 10px;border-bottom:1px solid #ECEFF1;font-size:13px;text-align:left}}
th{{background:#E8EAF6;color:#283593}}
.preview{{display:flex;gap:18px;flex-wrap:wrap}}
figure{{margin:0;background:#fff;padding:10px;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
figcaption{{font-size:12px;color:#546E7A;margin-bottom:6px}}
</style></head><body>
<header><h1>{html.escape(project['name'])} · 四域链路报告</h1>
<div class="sub">版本 v{version['version_no'] if version else 0} · 由 aepcb 1.0.0 生成 · 离线可看</div></header>
<main>
<div class="cards">
  <div class="card">MRC 分数<b>{run['total_score'] if run else '—'}</b></div>
  <div class="card">问题数<b>{len(issues)}</b></div>
  <div class="card">切片道次<b>{sl['pass_count'] if sl else 0}</b></div>
  <div class="card">总厚 mm<b>{sl['total_thickness_mm'] if sl else '—'}</b></div>
  <div class="card">Agent 轮数<b>{plan['rounds_used'] if plan else 0}</b></div>
  <div class="card">验收通过<b>{sum(1 for r in acc if r['passed'])}/{len(acc)}</b></div>
</div>
<h2>板面（2D）</h2>{_svg_board(db, pid)}
<h2 style="margin-top:18px">等轴视图（3D）</h2>{_svg_iso(db, pid, width=900)}
<h2>分数趋势</h2>{_svg_trend(db, pid)}
<h2>问题列表</h2>
<table><tr><th>规则</th><th>严重度</th><th>说明</th><th>实测</th></tr>{rows}</table>
<h2>层预览</h2><div class="preview">{previews}</div>
<h2>规则版本</h2>
<table><tr><th>版本</th><th>说明</th><th>创建时间</th></tr>{rule_rows}</table>
<h2>Agent 计划与对比卡片</h2>
<table><tr><th>#</th><th>意图</th><th>工具</th><th>状态</th></tr>{step_rows}</table>
{card_html}
<h2>验收（T01–T20）</h2>
<table><tr><th>任务</th><th>结果</th><th>说明</th></tr>{acc_rows}</table>
</main></body></html>"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(page, encoding="utf-8")
    return p
