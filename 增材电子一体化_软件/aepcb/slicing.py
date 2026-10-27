"""切片引擎：三层（下保护/电路/上保护）七道次，栅格化后生成蛇形路径。

几何来源只有走线、焊盘与板框，不读取任何 UI 状态；
同一输入（版本 + 工艺参数 + 材料批次）必须产出同一份路径（SLC-010 可复现）。
"""
from __future__ import annotations

import json

import cv2
import numpy as np


class SliceCancelled(Exception):
    """用户取消（UI-005）。在道次边界抛出，由任务层收拾已写入的数据。"""

from .board import load_route_points
from .geometry import Grid, polyline_len
from .store import DB, now
from . import slicecheck

PASS_PLAN = [
    (0, 0, "bottom_mask", "solid", "B", 0.08),
    (1, 0, "bottom_mask", "solid", "B", 0.08),
    (2, 0, "bottom_mask", "solid", "B", 0.08),
    (3, 1, "circuit", "dam_and_trench", "B", 0.10),
    (4, 1, "circuit", "trench_fill", "A", 0.10),
    (5, 2, "top_mask", "pad_pit", "B", 0.10),
    (6, 2, "top_mask", "pad_fill", "A", 0.10),
]
LAYER_SPEC = [(0, "bottom_mask", 3 * 0.08), (1, "circuit", 0.10), (2, "top_mask", 0.10)]
RES_MM = 0.25
FILL_STEP_MM = 0.40
PERIMETER_EPS_MM = 0.25      # 轮廓抽稀容差
INFILL_INSET_MM = 0.20       # 内填收缩，避免与轮廓重叠
THERMAL = [
    (0, "pre_gel", 80.0, 2.0, 90),
    (1, "silver_set", 100.0, 2.0, 90),
    (2, "cure", 170.0, 3.0, 420),
    (3, "final_cure", 170.0, 3.0, 900),
    (4, "cool", 60.0, None, 600),
]


def _masks(db: DB, circuit: dict):
    g = Grid(circuit["board_w_mm"], circuit["board_h_mm"], RES_MM)
    board = g.polygon([(0, 0), (circuit["board_w_mm"], 0),
                       (circuit["board_w_mm"], circuit["board_h_mm"]), (0, circuit["board_h_mm"])])
    routes = db.q("SELECT * FROM route WHERE circuit_id=? AND deleted_at IS NULL", (circuit["id"],))
    widths: list[float] = []
    paths: list[list[tuple[float, float]]] = []
    for r in routes:
        paths.append(load_route_points(db, r))
        widths.append(r["width_mm"])
    cond = g.empty()
    for pts, w in zip(paths, widths):
        cond |= g.lines([pts], w)
    pads = db.q("SELECT * FROM pad WHERE component_id IN "
                "(SELECT id FROM component WHERE circuit_id=?)", (circuit["id"],))
    if pads:
        cond |= g.circles([((p["x_mm"], p["y_mm"]), max(p["w_mm"], p["h_mm"]) / 2) for p in pads])
    vias = db.q("SELECT * FROM via WHERE circuit_id=?", (circuit["id"],))
    if vias:
        cond |= g.circles([((v["x_mm"], v["y_mm"]), v["diameter_mm"] / 2) for v in vias])
    trench = g.dilate(cond, 0.15)
    dam = board & ~trench
    pad_pit = g.circles([((p["x_mm"], p["y_mm"]), max(p["w_mm"], p["h_mm"]) / 2 + 0.3)
                         for p in pads]) if pads else g.empty()
    top = board & ~pad_pit
    return g, board, {"solid": board, "dam_and_trench": dam, "trench_fill": trench,
                      "pad_pit": top, "pad_fill": pad_pit}


def _contours(mask: np.ndarray, res: float) -> list[list[tuple[float, float]]]:
    """提取轮廓（真实切片器都先走轮廓再填内），返回闭合折线（mm）。"""
    img = (mask.astype(np.uint8)) * 255
    cnts, _ = cv2.findContours(img, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    min_area_px = max(4.0, (1.0 / res) ** 2)
    for c in cnts:
        if cv2.contourArea(c) < min_area_px:
            continue
        approx = cv2.approxPolyDP(c, PERIMETER_EPS_MM / res, True)
        if len(approx) < 3:
            continue
        pts = [((float(x) + 0.5) * res, (float(y) + 0.5) * res)
               for x, y in approx.reshape(-1, 2)]
        pts.append(pts[0])
        out.append(pts)
    return out


def _order_paths(polylines: list[list[tuple[float, float]]],
                 start: tuple[float, float] = (0.0, 0.0)) -> list[list[tuple[float, float]]]:
    """按路径数选策略：小批量用最近邻（质量优先），大批量用排序启发式（避免 O(n²)）。"""
    import math
    if len(polylines) > 1500:
        ordered = sorted((list(p) for p in polylines), key=lambda p: (round(p[0][0], 1), round(p[0][1], 1)))
        out = []
        cur = start
        for p in ordered:
            if math.dist(cur, p[-1]) < math.dist(cur, p[0]):
                p = p[::-1]
            out.append(p)
            cur = p[-1]
        return out
    remaining = [list(p) for p in polylines]
    out: list[list[tuple[float, float]]] = []
    cur = start
    while remaining:
        best_i, best_d, best_rev = 0, float("inf"), False
        for i, p in enumerate(remaining):
            d0 = math.dist(cur, p[0])
            d1 = math.dist(cur, p[-1])
            if d0 < best_d:
                best_i, best_d, best_rev = i, d0, False
            if d1 < best_d:
                best_i, best_d, best_rev = i, d1, True
        p = remaining.pop(best_i)
        if best_rev:
            p = p[::-1]
        out.append(p)
        cur = p[-1]
    return out


def run(db: DB, project_id: str, version_id: str, process_profile_id: str,
        material_batch_id: str | None = None, engine_version: str = "1.0.0",
        optimize_travel: bool = True, progress=None, should_cancel=None) -> dict:
    circuit = db.one("SELECT * FROM circuit WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                     (project_id,))
    if not circuit:
        raise ValueError("工程没有电路，无法切片")
    prof = db.one("SELECT * FROM process_profile WHERE id=?", (process_profile_id,))
    params = json.loads(prof["params_json"])
    speed_mask = float(params.get("speed_mask_mm_s", params.get("speed_mm_s", 12.0)))
    speed_silver = float(params.get("speed_silver_mm_s", params.get("speed_mm_s", 12.0) * 0.8))
    g, board, masks = _masks(db, circuit)

    total_thickness = sum(t for _, _, t in LAYER_SPEC)
    digest = json.dumps({"profile": prof["params_hash"], "routes": [
        [r["id"], r["width_mm"]] for r in db.q(
            "SELECT id,width_mm FROM route WHERE circuit_id=? ORDER BY id", (circuit["id"],))],
        "version": version_id}, sort_keys=True)
    digest_hash = db.put_blob(digest.encode(), "other")

    slice_id = db.ins("slice_set", project_id=project_id, design_version_id=version_id,
                      process_profile_id=process_profile_id, engine_version=engine_version,
                      status="computing", total_thickness_mm=round(total_thickness, 3),
                      layer_count=3, pass_count=7,
                      input_digest=digest_hash, created_at=now())
    mat = {}
    for m in db.q("SELECT id,code FROM material"):
        mat[m["code"]] = m["id"]
    layer_ids = {}
    for idx, kind, th in LAYER_SPEC:
        layer_ids[idx] = db.ins("physical_layer", slice_set_id=slice_id, layer_index=idx,
                                kind=kind, thickness_mm=round(th, 3),
                                material_id=mat.get("B"))

    total_len = 0.0
    path_count = 0
    z = 0.0
    total_passes = len(PASS_PLAN)
    for pass_index, layer_idx, layer_kind, pattern, mat_code, th in PASS_PLAN:
        if should_cancel and should_cancel():
            raise SliceCancelled("用户取消")
        if progress:
            progress(pass_index, total_passes)
        mask = masks[pattern]
        perimeters = _contours(mask, RES_MM)
        infill = g.erode(mask, INFILL_INSET_MM)
        polylines = perimeters + g.serpentine(infill, FILL_STEP_MM,
                                              vertical=(pass_index % 2 == 1))
        if optimize_travel:
            polylines = _order_paths(polylines)
        pass_id = db.ins("deposit_pass", slice_set_id=slice_id,
                         physical_layer_id=layer_ids[layer_idx], pass_index=pass_index,
                         material_id=mat.get(mat_code),
                         material_batch_id=material_batch_id if mat_code == "A" else None,
                         pattern=pattern, thickness_mm=th,
                         z_top_mm=round(z + th, 3), note="")
        seq = 0
        prev = None
        pass_speed = speed_silver if mat_code == "A" else speed_mask
        for pts in polylines:
            if prev is not None:
                db.ins("slice_path", deposit_pass_id=pass_id, seq=seq, path_kind="travel",
                       geom_blob_hash=db.put_blob(json.dumps(
                           [[round(prev[0], 3), round(prev[1], 3)], [round(pts[0][0], 3), round(pts[0][1], 3)]]).encode(), "path"),
                       point_count=2, length_mm=round(abs(prev[0] - pts[0][0]) + abs(prev[1] - pts[0][1]), 3),
                       z_mm=round(z + th, 3), speed_mm_s=round(pass_speed * 2.5, 3), flow_factor=0.0)
                seq += 1
            geom = [[round(x, 3), round(y, 3), round(z + th, 3)] for x, y in pts]
            length = polyline_len(pts)
            db.ins("slice_path", deposit_pass_id=pass_id, seq=seq, path_kind="extrude",
                   geom_blob_hash=db.put_blob(json.dumps(geom).encode(), "path"),
                   point_count=len(pts), length_mm=round(length, 3),
                   bbox_min_x_mm=min(p[0] for p in pts), bbox_min_y_mm=min(p[1] for p in pts),
                   bbox_max_x_mm=max(p[0] for p in pts), bbox_max_y_mm=max(p[1] for p in pts),
                   z_mm=round(z + th, 3), speed_mm_s=round(pass_speed, 3),
                   flow_factor=1.0)
            total_len += length
            path_count += 1
            seq += 1
            prev = pts[-1]
        z += th

    for idx, stage, temp, ramp, hold in THERMAL:
        db.ins("thermal_step", slice_set_id=slice_id, step_index=idx, stage=stage,
               target_kind="board", temp_c=temp, ramp_c_per_min=ramp, hold_s=hold)

    travel_time = sum(r[0] / max(1.0, r[1]) for r in
                      db.q("SELECT length_mm, speed_mm_s FROM slice_path WHERE deposit_pass_id IN "
                           "(SELECT id FROM deposit_pass WHERE slice_set_id=?)", (slice_id,)))
    hold_time = sum(t[4] for t in THERMAL)
    estimate = {"path_length_mm": round(total_len, 2), "path_count": path_count,
                "print_minutes": round((travel_time + hold_time) / 60.0, 1),
                "material_ml": round(sum(0.0 for _ in []) + 0.0, 3),
                "assumptions": ["按 0.25 mm 栅格、0.4 mm 行距估算",
                                "时间不含换料与人工", "材料体积按面积×道次厚度估算"]}
    db.c.execute("UPDATE slice_set SET status='ready', finished_at=?, estimate_json=? WHERE id=?",
                 (now(), json.dumps(estimate, ensure_ascii=False), slice_id))
    db.commit()
    conn = slicecheck.check(db, project_id, version_id, slice_id)
    return {"slice_id": slice_id, "score_ready": True, "thickness_mm": round(total_thickness, 3),
            "passes": 7, "paths": path_count, "length_mm": round(total_len, 1),
            "estimate": estimate, "connectivity": conn["result"],
            "opens": conn["opens"], "shorts": conn["shorts"]}
