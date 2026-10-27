"""布线层：合法性校验、连通性（断路/短路）、界面等价的改宽度/移线操作。"""
from __future__ import annotations

import itertools
import json

from .board import load_route_points
from .geometry import point_seg_dist, polyline_len, seg_intersect
from .store import DB, now


def _segments(pts):
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def min_gap(pts_a, pts_b) -> float:
    best = float("inf")
    for (a1, a2) in _segments(pts_a):
        for (b1, b2) in _segments(pts_b):
            best = min(best, point_seg_dist(a1, b1, b2), point_seg_dist(a2, b1, b2),
                       point_seg_dist(b1, a1, a2), point_seg_dist(b2, a1, a2))
    return best


def crosses(pts_a, pts_b) -> bool:
    for (a1, a2) in _segments(pts_a):
        for (b1, b2) in _segments(pts_b):
            if seg_intersect(a1, a2, b1, b2):
                return True
    return False


def validate_all(db: DB, circuit_id: str) -> list[str]:
    """CIR-011：零长度、重复点、越出板框。返回诊断 id 列表。"""
    out = []
    board = db.one("SELECT board_w_mm, board_h_mm FROM circuit WHERE id=?", (circuit_id,))
    w, h = board["board_w_mm"], board["board_h_mm"]
    for r in db.q("SELECT * FROM route WHERE circuit_id=? AND deleted_at IS NULL", (circuit_id,)):
        pts = load_route_points(db, r)
        code = None
        if len(pts) < 2:
            code, msg = "CIR-ROUTE-SHORT", "走线点数不足 2"
        elif polyline_len(pts) < 1e-6:
            code, msg = "CIR-ROUTE-ZERO", "走线长度为零"
        elif any(pts[i] == pts[i + 1] for i in range(len(pts) - 1)):
            code, msg = "CIR-ROUTE-DUP", "相邻点重复"
        elif any(x < 0 or y < 0 or x > w or y > h for x, y in pts):
            code, msg = "CIR-ROUTE-OOB", "走线越出板框"
        if code:
            out.append(db.add_diagnostic(
                db.scalar("SELECT project_id FROM circuit WHERE id=?", (circuit_id,)),
                "import", code, msg, severity="error",
                target_kind="route", target_id=r["id"]))
    return out


def connectivity(db: DB, project_id: str, version_id: str, circuit_id: str,
                 scope: str = "circuit", slice_set_id: str | None = None,
                 engine_version: str = "1.0.0") -> dict:
    """CIR-009：每个网络是否连通、异网是否短路。用并查集在焊盘层面判定。"""
    routes = db.q("SELECT * FROM route WHERE circuit_id=? AND deleted_at IS NULL", (circuit_id,))
    pads = db.q("SELECT * FROM pad WHERE component_id IN "
                "(SELECT id FROM component WHERE circuit_id=?)", (circuit_id,))
    pts = {r["id"]: load_route_points(db, r) for r in routes}

    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # 同网络、同层的分段端点相接才算连上；跨层必须过过孔（见下）
    by_net: dict[str, list] = {}
    for r in routes:
        by_net.setdefault(r["net_id"], []).append(r)
    for net_routes in by_net.values():
        for a, b in itertools.combinations(net_routes, 2):
            if a["layer_no"] != b["layer_no"]:
                continue          # 跨层必须靠过孔，不能凭端点相邻就当成连通
            pa, pb = pts[a["id"]], pts[b["id"]]
            near = min(
                (pa[0][0] - pb[0][0]) ** 2 + (pa[0][1] - pb[0][1]) ** 2,
                (pa[0][0] - pb[-1][0]) ** 2 + (pa[0][1] - pb[-1][1]) ** 2,
                (pa[-1][0] - pb[0][0]) ** 2 + (pa[-1][1] - pb[0][1]) ** 2,
                (pa[-1][0] - pb[-1][0]) ** 2 + (pa[-1][1] - pb[-1][1]) ** 2)
            if near <= 0.3 ** 2:
                union(f"r:{a['id']}", f"r:{b['id']}")

    # 已应用的 0Ω 跳线把两个焊盘直接连起来（CIR-007）
    for j in db.q("SELECT * FROM jumper WHERE circuit_id=? AND status='applied'", (circuit_id,)):
        union(f"pad:{j['pad_a_id']}", f"pad:{j['pad_b_id']}")

    # 过孔把同一网络在层区间内的走线接通（跨层唯一的合法通路）
    for v in db.q("SELECT * FROM via WHERE circuit_id=?", (circuit_id,)):
        for r in routes:
            if r["net_id"] != v["net_id"]:
                continue
            if not (v["from_layer"] <= r["layer_no"] <= v["to_layer"]):
                continue
            p = pts[r["id"]]
            if len(p) < 2:
                continue
            near = min(point_seg_dist((v["x_mm"], v["y_mm"]), p[i], p[i + 1])
                       for i in range(len(p) - 1))
            if near <= 0.4:
                union(f"via:{v['id']}", f"r:{r['id']}")
    for pad in pads:
        px, py = pad["x_mm"], pad["y_mm"]
        attached = False
        for r in routes:
            if r["net_id"] != pad["net_id"]:
                continue
            p = pts[r["id"]]
            if min((px - p[0][0]) ** 2 + (py - p[0][1]) ** 2,
                   (px - p[-1][0]) ** 2 + (py - p[-1][1]) ** 2) <= 0.25 ** 2:
                union(f"pad:{pad['id']}", f"r:{r['id']}")
                attached = True
        if not attached:
            union(f"pad:{pad['id']}", f"pad:{pad['id']}")

    findings, opens = [], 0
    for net in db.q("SELECT * FROM net WHERE circuit_id=?", (circuit_id,)):
        members = [p for p in pads if p["net_id"] == net["id"]]
        if len(members) < 2:
            continue
        roots = {find(f"pad:{p['id']}") for p in members}
        if len(roots) > 1:
            opens += 1
            findings.append({"kind": "open", "net": net["name"],
                             "pads": [p["pad_no"] for p in members],
                             "groups": len(roots)})

    shorts = 0
    by_id = {r["id"]: r for r in routes}
    for a, b in itertools.combinations(routes, 2):
        if a["layer_no"] != b["layer_no"] or a["net_id"] == b["net_id"]:
            continue
        if crosses(pts[a["id"]], pts[b["id"]]):
            shorts += 1
            findings.append({"kind": "short", "layer": a["layer_no"],
                             "nets": [db.scalar("SELECT name FROM net WHERE id=?", (a["net_id"],)),
                                      db.scalar("SELECT name FROM net WHERE id=?", (b["net_id"],))]})

    result = "fail" if (opens or shorts) else "pass"
    cid = db.ins("connectivity_check", project_id=project_id, scope=scope,
                 version_id=version_id, slice_set_id=slice_set_id, result=result,
                 open_count=opens, short_count=shorts,
                 findings_json=json.dumps(findings, ensure_ascii=False),
                 engine_version=engine_version,
                 input_digest=db.put_blob(json.dumps(
                     sorted([(r["id"], r["width_mm"]) for r in routes])).encode(), "other"),
                 ran_at=now())
    return {"id": cid, "result": result, "opens": opens, "shorts": shorts, "findings": findings}


# ---------------- 界面等价操作（Agent 只能用这些） ----------------

def set_width(db: DB, circuit_id: str, route_id: str, width_mm: float) -> dict:
    """CIR-010：低于工艺下限拒绝，并说明下限来源。"""
    net_class = db.one(
        "SELECT nc.min_width_mm FROM route r JOIN net n ON n.id=r.net_id "
        "JOIN net_class nc ON nc.id=n.net_class_id WHERE r.id=?", (route_id,))
    floor = net_class["min_width_mm"] if net_class else 0.5
    if width_mm < floor:
        raise ValueError(f"线宽 {width_mm} mm 低于网络类下限 {floor} mm")
    before = db.one("SELECT width_mm FROM route WHERE id=?", (route_id,))["width_mm"]
    db.c.execute("UPDATE route SET width_mm=? WHERE id=?", (width_mm, route_id))
    return {"route_id": route_id, "before": before, "after": width_mm}


def shift_route(db: DB, route_id: str, dx: float, dy: float) -> dict:
    row = db.one("SELECT * FROM route WHERE id=?", (route_id,))
    pts = load_route_points(db, row)
    moved = [(x + dx, y + dy) for x, y in pts]
    geom = json.dumps([[round(x, 4), round(y, 4)] for x, y in moved],
                      separators=(",", ":")).encode()
    h = db.put_blob(geom, "route")
    db.c.execute("UPDATE route SET geom_blob_hash=?, start_x_mm=?, start_y_mm=?, "
                 "end_x_mm=?, end_y_mm=? WHERE id=?",
                 (h, moved[0][0], moved[0][1], moved[-1][0], moved[-1][1], route_id))
    return {"route_id": route_id, "dx": dx, "dy": dy}


def set_points(db: DB, route_id: str, pts: list[tuple[float, float]]) -> dict:
    geom = json.dumps([[round(x, 4), round(y, 4)] for x, y in pts],
                      separators=(",", ":")).encode()
    h = db.put_blob(geom, "route")
    db.c.execute("UPDATE route SET geom_blob_hash=?, start_x_mm=?, start_y_mm=?, "
                 "end_x_mm=?, end_y_mm=?, point_count=?, length_mm=? WHERE id=?",
                 (h, pts[0][0], pts[0][1], pts[-1][0], pts[-1][1], len(pts),
                  round(polyline_len(pts), 3), route_id))
    return {"route_id": route_id, "points": [[round(x, 4), round(y, 4)] for x, y in pts]}


def add_route(db: DB, circuit_id: str, net_id: str, pts: list[tuple[float, float]],
              width_mm: float = 0.6, layer: int = 0, with_vias: bool = False) -> str:
    """界面里“画一条新线”：校验线宽下限与板框，然后落库。"""
    c = db.one("SELECT * FROM circuit WHERE id=?", (circuit_id,))
    if not c:
        raise ValueError("电路不存在")
    if len(pts) < 2:
        raise ValueError("走线至少需要两个点")
    for x, y in pts:
        if not (0 <= x <= c["board_w_mm"] and 0 <= y <= c["board_h_mm"]):
            raise ValueError(f"点 ({x:.2f},{y:.2f}) 超出板框")
    floor = db.scalar("SELECT nc.min_width_mm FROM net n JOIN net_class nc ON nc.id=n.net_class_id "
                      "WHERE n.id=?", (net_id,), 0.5)
    if width_mm < floor:
        raise ValueError(f"线宽 {width_mm} mm 低于网络类下限 {floor} mm")
    geom = json.dumps([[round(x, 4), round(y, 4)] for x, y in pts],
                      separators=(",", ":")).encode()
    h = db.put_blob(geom, "route")
    rid = db.ins("route", circuit_id=circuit_id, net_id=net_id, layer_no=layer, side="top",
                  width_mm=width_mm, geom_blob_hash=h,
                  length_mm=round(polyline_len(pts), 3), point_count=len(pts),
                  start_x_mm=pts[0][0], start_y_mm=pts[0][1],
                  end_x_mm=pts[-1][0], end_y_mm=pts[-1][1], created_at=now())
    if with_vias or layer != 0:
        for x, y in (pts[0], pts[-1]):
            db.ins("via", circuit_id=circuit_id, net_id=net_id, x_mm=x, y_mm=y,
                   diameter_mm=0.6, drill_mm=0.3, from_layer=0, to_layer=max(1, layer),
                   created_at=now())
    return rid


def suggest_jumper(db: DB, circuit_id: str, net_id: str) -> str | None:
    """CIR-007：为断开的网络提出 0Ω 跳线候选（两个焊盘直接短接）。"""
    pads = db.q("SELECT * FROM pad WHERE net_id=? ORDER BY pad_no", (net_id,))
    if len(pads) < 2:
        return None
    return db.ins("jumper", circuit_id=circuit_id, net_id=net_id,
                  pad_a_id=pads[0]["id"], pad_b_id=pads[1]["id"],
                  status="candidate", created_at=now())


def apply_jumper(db: DB, jumper_id: str) -> None:
    db.c.execute("UPDATE jumper SET status='applied' WHERE id=?", (jumper_id,))


# ---------------- 器件放置（CIR-002） ----------------

def component_snapshot(db: DB, component_id: str) -> dict:
    """整器件快照：坐标/旋转/翻面/锁定 + 全部焊盘。

    撤销与试算都以它为准：只存增量的话，旋转+翻面叠加后回滚会错位。
    """
    c = db.one("SELECT * FROM component WHERE id=?", (component_id,))
    if not c:
        raise ValueError(f"器件不存在：{component_id}")
    pads = db.q("SELECT id, x_mm, y_mm, side FROM pad WHERE component_id=?", (component_id,))
    return {"component_id": component_id,
            "x_mm": float(c["x_mm"]), "y_mm": float(c["y_mm"]),
            "rot_deg": int(c["rot_deg"]), "side": c["side"], "locked": int(c["locked"]),
            "pads": [{"id": p["id"], "x_mm": float(p["x_mm"]),
                      "y_mm": float(p["y_mm"]), "side": p["side"]} for p in pads]}


def apply_component_snapshot(db: DB, snap: dict) -> None:
    db.c.execute("UPDATE component SET x_mm=?, y_mm=?, rot_deg=?, side=?, locked=? WHERE id=?",
                 (snap["x_mm"], snap["y_mm"], snap["rot_deg"], snap["side"],
                  snap["locked"], snap["component_id"]))
    for p in snap["pads"]:
        db.c.execute("UPDATE pad SET x_mm=?, y_mm=?, side=? WHERE id=?",
                     (p["x_mm"], p["y_mm"], p["side"], p["id"]))


def _require_unlocked(db: DB, component_id: str):
    c = db.one("SELECT * FROM component WHERE id=?", (component_id,))
    if not c:
        raise ValueError(f"器件不存在：{component_id}")
    if int(c["locked"]):
        raise ValueError(f"器件 {c['ref_des']} 已锁定，先解锁再改")
    return c


def move_component(db: DB, circuit_id: str, component_id: str,
                   dx: float, dy: float, snap_mm: float | None = None) -> dict:
    """移动器件：本体与全部焊盘同步偏移。

    `snap_mm` 不为空时先把位移吸附到栅格（CIR-002 的栅格吸附）。
    在服务端吸附而不是前端：否则同一操作从 API 进来会与界面不一致。
    """
    c = _require_unlocked(db, component_id)
    if snap_mm and snap_mm > 0:
        dx = round(round(dx / snap_mm) * snap_mm, 4)
        dy = round(round(dy / snap_mm) * snap_mm, 4)
    db.c.execute("UPDATE component SET x_mm=x_mm+?, y_mm=y_mm+? WHERE id=?",
                 (dx, dy, component_id))
    db.c.execute("UPDATE pad SET x_mm=x_mm+?, y_mm=y_mm+? WHERE component_id=?",
                 (dx, dy, component_id))
    return {"component_id": component_id, "ref": c["ref_des"],
            "dx": dx, "dy": dy, "snap_mm": snap_mm}


def rotate_component(db: DB, circuit_id: str, component_id: str, deg: int) -> dict:
    """绕器件中心旋转 0/90/180/270：焊盘跟着转，rot_deg 累加。"""
    import math
    c = _require_unlocked(db, component_id)
    deg = int(deg) % 360
    if deg % 90:
        raise ValueError("只支持 0/90/180/270 度旋转")
    cx, cy = float(c["x_mm"]), float(c["y_mm"])
    ca, sa = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    for p in db.q("SELECT id, x_mm, y_mm FROM pad WHERE component_id=?", (component_id,)):
        dx, dy = float(p["x_mm"]) - cx, float(p["y_mm"]) - cy
        nx, ny = dx * ca - dy * sa, dx * sa + dy * ca
        db.c.execute("UPDATE pad SET x_mm=?, y_mm=? WHERE id=?",
                     (round(cx + nx, 4), round(cy + ny, 4), p["id"]))
    db.c.execute("UPDATE component SET rot_deg=(rot_deg+?)%360 WHERE id=?", (deg, component_id))
    return {"component_id": component_id, "ref": c["ref_des"], "deg": deg}


def flip_component(db: DB, circuit_id: str, component_id: str) -> dict:
    """翻面：side 取反，焊盘关于器件中心 Y 镜像。"""
    c = _require_unlocked(db, component_id)
    cy = float(c["y_mm"])
    new_side = "bottom" if c["side"] == "top" else "top"
    db.c.execute("UPDATE component SET side=? WHERE id=?", (new_side, component_id))
    for p in db.q("SELECT id, y_mm FROM pad WHERE component_id=?", (component_id,)):
        db.c.execute("UPDATE pad SET y_mm=?, side=? WHERE id=?",
                     (round(2 * cy - float(p["y_mm"]), 4), new_side, p["id"]))
    return {"component_id": component_id, "ref": c["ref_des"], "side": new_side}


def lock_component(db: DB, circuit_id: str, component_id: str, locked: bool) -> dict:
    db.c.execute("UPDATE component SET locked=? WHERE id=?",
                 (1 if locked else 0, component_id))
    return {"component_id": component_id, "locked": bool(locked)}
