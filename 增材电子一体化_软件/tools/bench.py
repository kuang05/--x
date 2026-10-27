#!/usr/bin/env python3
"""切片性能基准：60×40（T19-A）与 200×200（SRS 首版幅面上限）。

用法：python3 tools/bench.py [--only 200]
输出：每块板的路径段数、耗时、是否满足 NFR-001 的 120 s 目标。
"""
from __future__ import annotations

import argparse
import math
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aepcb import board, mrc, slicing, step  # noqa: E402
from aepcb.store import DB, now  # noqa: E402
from fixtures import t19a  # noqa: E402


def grid_spec(w: float, h: float, cols: int = 8, rows: int = 6) -> dict:
    """生成一块规则网格板：器件按网格摆放，网络连相邻器件。"""
    comps, pads, nets, routes = [], [], [], []
    step_x, step_y = w / (cols + 1), h / (rows + 1)
    idx = 0
    for r in range(rows):
        for c in range(cols):
            idx += 1
            ref = f"U{idx}"
            x, y = step_x * (c + 1), step_y * (r + 1)
            comps.append({"ref": ref, "x": round(x, 2), "y": round(y, 2), "rot": 0,
                          "side": "top", "footprint": "QFN", "h": 1.2})
            for no, (dx, dy) in (("1", (-2, 0)), ("2", (2, 0)), ("3", (0, 2))):
                pads.append({"comp": ref, "no": no, "x": round(x + dx, 2),
                             "y": round(y + dy, 2), "w": 1.5, "h": 1.0,
                             "net": f"N{idx:03d}" if no == "1" else None})
    net_id = 0
    for r in range(rows):
        for c in range(cols - 1):
            net_id += 1
            a = pads[((r * cols + c) * 3) + 0]
            b = pads[((r * cols + c + 1) * 3) + 0]
            a["net"] = b["net"] = f"L{net_id:03d}"
            routes.append({"net": f"L{net_id:03d}", "layer": r % 2, "width": 0.6,
                           "points": [[a["x"], a["y"]], [a["x"], a["y"] - 6],
                                      [b["x"], b["y"] - 6], [b["x"], b["y"]]]})
    for p in pads:
        if not p["net"]:
            p["net"] = f"NC{abs(hash(p['comp'] + p['no'])) % 999:03d}"
    net_names = sorted({p["net"] for p in pads} | {r["net"] for r in routes})
    return {"board": {"name": f"bench-{w:.0f}", "w": w, "h": h,
                      "min_width": 0.5, "min_spacing": 0.5},
            "components": comps, "pads": pads,
            "nets": [{"name": n, "class": "default"} for n in net_names],
            "routes": routes, "keepouts": []}


def run_case(tmp: Path, name: str, spec: dict, repeat: int = 3) -> dict:
    """重复跑多次取中位数。

    同一台机器上两次切片能差一倍（沙箱 CPU 抖动），单次数字不能当性能结论。
    """
    times: list[float] = []
    paths = length_mm = 0
    for i in range(max(1, repeat)):
        db = DB(tmp / f"{name}-{i}.db")
        pid = db.ins("project", name=name, unit="mm", sheet_w_mm=spec["board"]["w"],
                     sheet_h_mm=spec["board"]["h"], origin_kind="board_bottom_left",
                     data_schema_version=1, dir_rel_path=f"p/{name}", created_at=now(),
                     updated_at=now())
        v0 = db.ins("version", project_id=pid, version_no=0, kind="initial",
                    author_kind="human", author_name="local", reason="init", created_at=now())
        db.c.execute("UPDATE project SET current_version_id=? WHERE id=?", (v0, pid))
        db.commit()
        sid, _ = board.import_step(db, pid, step.make_fixture_step(
            spec["board"]["w"], spec["board"]["h"], 1.2), step)
        board.import_board(db, pid, sid, spec)
        base = mrc.ensure_baseline(db)
        t0 = time.perf_counter()
        out = slicing.run(db, pid, v0, base["process_profile_id"], base["material_batch_id"])
        times.append(time.perf_counter() - t0)
        paths, length_mm = out["paths"], out["length_mm"]
        db.close()
    times.sort()
    med = times[len(times) // 2]
    return {"case": name, "w": spec["board"]["w"], "h": spec["board"]["h"],
            "paths": paths, "seconds": round(med, 2),
            "min": round(times[0], 2), "max": round(times[-1], 2), "repeat": len(times),
            "target_120s": med <= 120, "length_mm": length_mm}


def machine_info() -> dict:
    import os
    import platform
    model = "unknown"
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return {"platform": platform.platform(), "machine": platform.machine(),
            "cpu": model, "cores": os.cpu_count(),
            "python": platform.python_version()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--repeat", type=int, default=3, help="每个用例重复次数（取中位数）")
    args = ap.parse_args()
    info = machine_info()
    print("== 机器口径 ==")
    for k, v in info.items():
        print(f"  {k:9}: {v}")
    print("  NFR-001 : 200×200 单片 ≤120 s（SRS 基准机 i5-12400）")
    print()
    cases = [("t19a-60x40", t19a.t19a()), ("grid-200x200", grid_spec(200, 200))]
    if args.only:
        cases = [c for c in cases if args.only in c[0]]
    with tempfile.TemporaryDirectory() as td:
        for name, spec in cases:
            r = run_case(Path(td), name, spec, repeat=args.repeat)
            print(f"{r['case']:>14}  {r['w']:.0f}×{r['h']:.0f} mm  "
                  f"路径 {r['paths']:>6} 段  长度 {r['length_mm']:>9.0f} mm  "
                  f"中位数 {r['seconds']:>6.2f} s（{r['min']:.2f}–{r['max']:.2f}，n={r['repeat']}）  "
                  f"≤120s: {r['target_120s']}")


if __name__ == "__main__":
    main()
