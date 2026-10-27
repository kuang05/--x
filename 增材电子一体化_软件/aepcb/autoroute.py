"""自动布线：0.5 mm 网格上的 A*，避让焊盘、已布走线与禁布区。

策略是"单网络单层布通"，与 SRS CIR-006 的首版口径一致：
    失败时返回被谁挡住（哪一类障碍占了路径），而不是静默返回 None。
"""
from __future__ import annotations

import heapq
import json
import math

from .board import load_route_points
from .geometry import bbox
from .store import DB, now

RES = 0.5


def _grid(bbox_mm, res=RES):
    w, h = bbox_mm
    return max(1, int(w / res)), max(1, int(h / res))


def _obstacles(db: DB, circuit: dict, net_id: str, layer: int = 0) -> tuple[set[tuple[int, int]], dict]:
    """返回占用格与被占原因，便于给出"被谁挡住"。"""
    nx, ny = _grid((circuit["board_w_mm"], circuit["board_h_mm"]))
    blocked: set[tuple[int, int]] = set()
    reason: dict[tuple[int, int], str] = {}

    def mark(x0, y0, x1, y1, why):
        i0, j0 = int(x0 / RES), int(y0 / RES)
        i1, j1 = int(x1 / RES), int(y1 / RES)
        for i in range(max(0, i0), min(nx - 1, i1) + 1):
            for j in range(max(0, j0), min(ny - 1, j1) + 1):
                blocked.add((i, j))
                reason.setdefault((i, j), why)

    # 板边 0.8 mm 安全区
    mark(0, 0, circuit["board_w_mm"], 0.8, "板边")
    mark(0, circuit["board_h_mm"] - 0.8, circuit["board_w_mm"], circuit["board_h_mm"], "板边")
    mark(0, 0, 0.8, circuit["board_h_mm"], "板边")
    mark(circuit["board_w_mm"] - 0.8, 0, circuit["board_w_mm"], circuit["board_h_mm"], "板边")

    for r in db.q("SELECT * FROM route WHERE circuit_id=? AND deleted_at IS NULL", (circuit["id"],)):
        if r["net_id"] == net_id or r["layer_no"] != layer:
            continue
        pts = load_route_points(db, r)
        pad = r["width_mm"] / 2 + 0.5
        for i in range(len(pts) - 1):
            (x0, y0), (x1, y1) = pts[i], pts[i + 1]
            mark(min(x0, x1) - pad, min(y0, y1) - pad,
                 max(x0, x1) + pad, max(y0, y1) + pad, "已布走线")

    for p in db.q("SELECT pad.* FROM pad JOIN component c ON c.id=pad.component_id "
                  "WHERE c.circuit_id=?", (circuit["id"],)):
        if p["net_id"] == net_id or layer != 0:
            continue
        r = max(p["w_mm"], p["h_mm"]) / 2 + 0.5
        mark(p["x_mm"] - r, p["y_mm"] - r, p["x_mm"] + r, p["y_mm"] + r, "焊盘")

    for k in db.q("SELECT * FROM keepout WHERE circuit_id=?", (circuit["id"],)):
        pts = json.loads(db.get_blob(k["geometry_blob_hash"]))
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        mark(min(xs), min(ys), max(xs), max(ys), "禁布区")
    return blocked, reason


def _astar(nx, ny, start, goal, blocked):
    if start in blocked or goal in blocked:
        return None
    openq = [(0.0, start)]
    came: dict[tuple[int, int], tuple[int, int]] = {}
    cost = {start: 0.0}
    while openq:
        _, cur = heapq.heappop(openq)
        if cur == goal:
            path = [cur]
            while cur in came:
                cur = came[cur]
                path.append(cur)
            return path[::-1]
        cx, cy = cur
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nb = (cx + dx, cy + dy)
                if not (0 <= nb[0] < nx and 0 <= nb[1] < ny) or nb in blocked:
                    continue
                step = math.hypot(dx, dy)
                ng = cost[cur] + step
                if ng < cost.get(nb, 1e9):
                    cost[nb] = ng
                    came[nb] = cur
                    h = math.hypot(nb[0] - goal[0], nb[1] - goal[1])
                    heapq.heappush(openq, (ng + h, nb))
    return None


def _simplify(path):
    pts = [((i + 0.5) * RES, (j + 0.5) * RES) for i, j in path]
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        (x0, y0), (x1, y1), (x2, y2) = out[-1], pts[i], pts[i + 1]
        if abs((x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1)) > 1e-9:
            out.append(pts[i])
    out.append(pts[-1])
    return out


def route_net(db: DB, circuit: dict, net_id: str, width: float = 0.6,
              net_name: str = "", layer: int = 0) -> dict:
    pads = db.q("SELECT * FROM pad WHERE net_id=? ORDER BY pad_no", (net_id,))
    if len(pads) < 2:
        return {"ok": False, "reason": "该网络焊盘不足 2 个"}
    a, b = (pads[0]["x_mm"], pads[0]["y_mm"]), (pads[-1]["x_mm"], pads[-1]["y_mm"])
    nx, ny = _grid((circuit["board_w_mm"], circuit["board_h_mm"]))
    blocked, reason = _obstacles(db, circuit, net_id, layer)
    start = (int(a[0] / RES), int(a[1] / RES))
    goal = (int(b[0] / RES), int(b[1] / RES))
    blocked.discard(start)
    blocked.discard(goal)
    path = _astar(nx, ny, start, goal, blocked)
    if not path:
        hit = reason.get(start) or reason.get(goal) or "路径被完全封闭"
        return {"ok": False, "reason": f"布不通：{hit}"}
    pts = _simplify(path)
    pts[0], pts[-1] = a, b          # 端点吸附到焊盘中心，否则连通性判定接不上
    geom = json.dumps([[round(x, 3), round(y, 3)] for x, y in pts],
                      separators=(",", ":")).encode()
    gh = db.put_blob(geom, "route")
    x0, y0, x1, y1 = bbox(pts)
    rid = db.ins("route", circuit_id=circuit["id"], net_id=net_id, layer_no=layer, side="top",
                 width_mm=width, geom_blob_hash=gh,
                 length_mm=round(sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)), 3),
                 point_count=len(pts), start_x_mm=pts[0][0], start_y_mm=pts[0][1],
                 end_x_mm=pts[-1][0], end_y_mm=pts[-1][1], created_at=now())
    db.commit()
    via_count = 0
    if layer != 0:
        # 换层布线必须打过孔，否则“图上连了、电学上没连”
        for x, y in (pts[0], pts[-1]):
            db.ins("via", circuit_id=circuit["id"], net_id=net_id, x_mm=x, y_mm=y,
                   diameter_mm=0.6, drill_mm=0.3, from_layer=0, to_layer=layer,
                   created_at=now())
            via_count += 1
        db.commit()
    return {"ok": True, "route_id": rid, "points": len(pts), "net": net_name,
            "layer": layer, "vias": via_count}


def route_all(db: DB, circuit_id: str, limit: int = 12) -> dict:
    circuit = db.one("SELECT * FROM circuit WHERE id=?", (circuit_id,))
    routed, failed = [], []
    nets = db.q("SELECT n.* FROM net n WHERE n.circuit_id=? AND NOT EXISTS "
                "(SELECT 1 FROM route r WHERE r.net_id=n.id AND r.deleted_at IS NULL) "
                "ORDER BY n.name LIMIT ?", (circuit_id, limit))
    for n in nets:
        out = route_net(db, circuit, n["id"], width=0.6, net_name=n["name"], layer=0)
        if not out["ok"]:
            # 层 0 被挡就换层，与“顶面/底面走线”的首版设定一致
            out = route_net(db, circuit, n["id"], width=0.6, net_name=n["name"], layer=1)
            out["retried_layer"] = 1
        (routed if out["ok"] else failed).append({"net": n["name"], **out})
    rate = len(routed) / max(1, len(routed) + len(failed))
    return {"routed": routed, "failed": failed, "rate": round(rate, 3),
            "attempted": len(nets)}
