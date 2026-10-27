#!/usr/bin/env python3
"""UI 性能实测（UI-009）：选择响应、非几何操作、三维帧率。

口径说明（避免拿好看的数字骗自己）：
- 选择响应：`web.state()` 一次完整返回的墙钟时间（300 条走线），
  近似"点选后属性面板刷新"的服务端成本；不含浏览器渲染。
- 非几何操作：一次 `change_width`（含落库与操作栈记录）。
- 三维帧率：**只测投影计算**。用 node 跑与 `renderIso()` 同款的投影公式，
  对 20 万顶点算一遍的耗时折算 fps；**不含 SVG/DOM 渲染**。
  这个口径对 SVG 天然有利——真实浏览器里逐元素更新只会更慢。

用法：python3 tools/ui_bench.py [--repeat 5]
输出：控制台 + workspace/ui_bench.md
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aepcb import board, history, mrc, step, web  # noqa: E402
from aepcb.store import DB, now  # noqa: E402


def _new_project(db: DB, name: str, w: float, h: float):
    pid = db.ins("project", name=name, unit="mm", sheet_w_mm=w, sheet_h_mm=h,
                 origin_kind="board_bottom_left", data_schema_version=1,
                 dir_rel_path=f"projects/{name}", created_at=now(), updated_at=now())
    v0 = db.ins("version", project_id=pid, version_no=0, kind="initial",
                author_kind="human", author_name="local", reason="bench", created_at=now())
    db.c.execute("UPDATE project SET current_version_id=? WHERE id=?", (v0, pid))
    db.commit()
    return pid, v0


def build_board(db: DB, nets: int = 300, w: float = 200.0, h: float = 200.0):
    """300 网络板：按 SRS UI-009 的验收条件造数据。"""
    pid, v0 = _new_project(db, f"ui-{nets}", w, h)
    sid, _ = board.import_step(db, pid, step.make_fixture_step(w, h, 1.2), step)
    imp = board.import_board(db, pid, sid, {
        "board": {"name": "ui-bench", "w": w, "h": h, "min_width": 0.5, "min_spacing": 0.5},
        "components": [], "pads": [], "nets": [], "routes": [], "keepouts": []})
    cid = imp.circuit_id
    cls = db.scalar("SELECT id FROM net_class LIMIT 1")
    for i in range(nets):
        nid = db.ins("net", circuit_id=cid, name=f"N{i:03d}", net_class_id=cls,
                     created_at=now())
        x0, y0 = 5 + (i % 20) * 9, 5 + (i // 20) * 9
        pts = [[x0, y0], [x0 + 6, y0], [x0 + 6, y0 + 4]]
        hsh = db.put_blob(json.dumps([[round(x, 3), round(y, 3)] for x, y in pts],
                                     separators=(",", ":")).encode(), "route")
        db.ins("route", circuit_id=cid, net_id=nid, layer_no=i % 2, width_mm=0.6,
               geom_blob_hash=hsh, start_x_mm=pts[0][0], start_y_mm=pts[0][1],
               end_x_mm=pts[-1][0], end_y_mm=pts[-1][1], point_count=len(pts),
               length_mm=10.0, created_at=now())
    db.commit()
    mrc.ensure_baseline(db)
    mrc.run(db, pid, v0)
    db.set_setting("ui.current_project", pid)
    db.commit()
    return pid, v0, cid


def _med(fn, repeat: int) -> tuple[float, list[float], float]:
    """返回（中位数, 全部样本, MAD）。

    只给中位数会让人以为误差很小；MAD（中位数绝对偏差）才能说明同机抖动。
    """
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    med = times[len(times) // 2]
    mad = statistics.median([abs(t - med) for t in times])
    return med, times, mad


ISO_JS = """
const N = %d;
const verts = new Float64Array(N * 3);
for (let i = 0; i < N; i++) {
  verts[i*3] = (i * 37 %% 2000) / 10;
  verts[i*3+1] = (i * 91 %% 2000) / 10;
  verts[i*3+2] = (i %% 2) * 0.1;
}
const az = Math.PI/4, el = Math.PI/6, zoom = 1;
const ca = Math.cos(az), sa = Math.sin(az), ce = Math.cos(el), se = Math.sin(el);
const W = 760, H = 470, bw = 200, bh = 200;
const k = (W/(bw+bh))*1.55*zoom;
const out = new Float64Array(N * 2);
function frame() {
  for (let i = 0; i < N; i++) {
    const x = verts[i*3], y = verts[i*3+1], z = verts[i*3+2];
    const x1 = x*ca - y*sa, y1 = x*sa + y*ca;
    out[i*2]   = W*0.5 + x1*k;
    out[i*2+1] = H*0.5 + (y1*se - z*22*ce)*k;
  }
}
frame();  // 预热
const REP = 10, t0 = process.hrtime.bigint();
for (let r = 0; r < REP; r++) frame();
const t1 = process.hrtime.bigint();
console.log(JSON.stringify({n: N, frames: REP, ms_per_frame: Number(t1 - t0) / 1e6 / REP}));
"""


def bench_iso(vertices: int, repeat: int = 10) -> dict:
    js = ISO_JS % vertices
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        path = f.name
    out = subprocess.run(["node", path], capture_output=True, text=True, check=True)
    data = json.loads(out.stdout.strip().splitlines()[-1])
    data["fps"] = round(1000.0 / data["ms_per_frame"], 1) if data["ms_per_frame"] else None
    return data


def machine_info() -> dict:
    import os
    import platform
    cpu = "unknown"
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return {"cpu": cpu, "cores": os.cpu_count(), "python": platform.python_version(),
            "platform": platform.platform()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--nets", type=int, default=300)
    ap.add_argument("--vertices", type=int, default=200_000)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        db = DB(Path(td) / "ui.db")
        pid, v0, cid = build_board(db, nets=args.nets)
        routes = db.scalar("SELECT COUNT(*) FROM route WHERE circuit_id=?", (cid,))
        sel_ms, sel_all, sel_mad = _med(lambda: web.state(db), args.repeat)
        rid = db.scalar("SELECT id FROM route WHERE circuit_id=? LIMIT 1", (cid,))
        write_ms, write_all, write_mad = _med(
            lambda: history.change_width(db, pid, cid, rid, 0.65), args.repeat)
        db.close()

    info = machine_info()
    iso = bench_iso(args.vertices)

    def verdict(value: float, target: float) -> str:
        return "PASS" if value <= target else "FAIL"

    rows = [
        ("选择响应", "≤50 ms", f"{sel_ms:.1f} ± {sel_mad:.1f} ms", verdict(sel_ms, 50)),
        ("非几何操作（改宽）", "≤120 ms", f"{write_ms:.1f} ± {write_mad:.1f} ms",
         verdict(write_ms, 120)),
        ("三维投影（20 万顶点）", "≥30 fps",
         f"{iso['ms_per_frame']:.2f} ms/帧 → {iso['fps']} fps",
         "PASS" if iso["fps"] and iso["fps"] >= 30 else "FAIL"),
    ]

    lines = ["# UI 性能实测（UI-009）", "",
             f"- 机器：{info['cpu']} / {info['cores']} 核 / Python {info['python']}",
             f"- 数据：{routes} 条走线、{args.nets} 个网络；每项重复 {args.repeat} 次取中位数",
             "",
             "| 指标 | 目标 | 实测 | 结论 |", "|---|---|---|---|"]
    lines += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows]
    lines += ["", "## 口径与诚实说明", "",
              "0. 表中为**中位数 ± MAD**（中位数绝对偏差）；MAD 接近 0 说明同机抖动小，"
              "远大于 0 时不要引用单个数字作结论。",
              "1. **选择响应**只测服务端 `web.state()` 返回耗时，**不含浏览器渲染**；"
              "真实点选的体感还包括 DOM 更新。",
              f"2. **三维帧率**只测投影计算（{args.vertices} 顶点，node 循环 {iso['frames']} 帧取均值），"
              "**不含 SVG/DOM 渲染**。当前实现是 SVG 逐元素更新，这个口径对它天然有利；"
              "真实浏览器里会更慢。",
              "3. 若三维不达标，正确做法是**换 canvas/WebGL 渲染**，而不是改阈值。",
              "", "## 明细", ""]
    lines += [f"- 选择响应原始值（ms）：{', '.join(f'{v:.1f}' for v in sel_all)}",
              f"- 改宽原始值（ms）：{', '.join(f'{v:.1f}' for v in write_all)}",
              f"- 投影：{iso['ms_per_frame']:.3f} ms/帧（{args.vertices} 顶点）"]
    report = ROOT / "workspace" / "ui_bench.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for a, b, c, d in rows:
        print(f"{a:>18}  目标 {b:<8} 实测 {c:<28} {d}")
    print(f"报告 -> {report}")


if __name__ == "__main__":
    main()
