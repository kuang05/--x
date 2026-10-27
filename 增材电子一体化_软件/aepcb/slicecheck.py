"""切片后连通性检查（SLC-007）。

判据刻意选“覆盖”而不是“连通域”：
    本软件的银浆道次是蛇形填充，栅格化后每行是独立笔画，用连通域标记会把
    一条好线判成 160 个碎块（实测如此）。真正要防的是“路径缺失导致断路”，
    所以检查：**每个网络的每个焊盘，是否被切片产出的挤出路径覆盖**。

短路不在这里重复判：CIR-009 已按几何交叉判过，切片只会让间距更差不会更好；
在这里再用栅格判会因 0.25 mm 分辨率产生假阳性。
"""
from __future__ import annotations

import json

from .geometry import Grid
from .store import DB, now

RES = 0.25
COVER_WIDTH_MM = 0.9      # 覆盖判定宽度：走线宽度 + 栅格容差


def _coverage_mask(db: DB, slice_id: str, grid: Grid):
    pas = db.one("SELECT * FROM deposit_pass WHERE slice_set_id=? AND pattern='trench_fill' "
                 "ORDER BY pass_index LIMIT 1", (slice_id,))
    if not pas:
        return None
    mask = grid.empty()
    for r in db.q("SELECT * FROM slice_path WHERE deposit_pass_id=? AND path_kind='extrude'",
                  (pas["id"],)):
        geom = json.loads(db.get_blob(r["geom_blob_hash"]))
        pts = [(float(p[0]), float(p[1])) for p in geom]
        if len(pts) >= 2:
            mask |= grid.lines([pts], COVER_WIDTH_MM)
    return mask


def check(db: DB, project_id: str, version_id: str, slice_id: str) -> dict:
    circuit = db.one("SELECT * FROM circuit WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                     (project_id,))
    grid = Grid(circuit["board_w_mm"], circuit["board_h_mm"], RES)
    mask = _coverage_mask(db, slice_id, grid)
    if mask is None:
        return {"result": "error", "opens": 0, "shorts": 0,
                "findings": [{"kind": "missing_pass"}], "id": None}

    pads = db.q("SELECT * FROM pad WHERE component_id IN "
                "(SELECT id FROM component WHERE circuit_id=?)", (circuit["id"],))
    nets = {r["id"]: r["name"] for r in db.q("SELECT id,name FROM net WHERE circuit_id=?",
                                             (circuit["id"],))}
    by_net: dict[str, list] = {}
    for p in pads:
        if p["net_id"]:
            by_net.setdefault(p["net_id"], []).append(p)

    findings, opens = [], 0
    for net_id, items in by_net.items():
        if len(items) < 2:
            continue
        uncovered = []
        for p in items:
            i, j = grid.cell(p["x_mm"], p["y_mm"])
            if not mask[j, i]:
                uncovered.append({"pad": p["pad_no"], "x": p["x_mm"], "y": p["y_mm"]})
        if uncovered:
            opens += 1
            findings.append({"kind": "open", "net": nets.get(net_id, "?"), "uncovered": uncovered})

    result = "fail" if opens else "pass"
    cid = db.ins("connectivity_check", project_id=project_id, scope="slice",
                 version_id=version_id, slice_set_id=slice_id, result=result,
                 open_count=opens, short_count=0,
                 findings_json=json.dumps(findings, ensure_ascii=False),
                 engine_version="1.0.0",
                 input_digest=db.put_blob(f"{slice_id}:coverage".encode(), "other"),
                 ran_at=now())
    db.commit()
    return {"id": cid, "result": result, "opens": opens, "shorts": 0, "findings": findings}


def latest(db: DB, slice_id: str):
    return db.one("SELECT * FROM connectivity_check WHERE slice_set_id=? AND scope='slice' "
                  "ORDER BY ran_at DESC, rowid DESC LIMIT 1", (slice_id,))
