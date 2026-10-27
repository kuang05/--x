"""撤销/重做：PRJ-005 —— 撤销只往 operation 追加记录，**不产生版本**。

语义（与数据模型一致）：
    - 一次有效修改 = 一条 operation（正向 payload + 反向 inverse）
    - 撤销 = 追加 op_type='undo'，应用 inverse，version_no 全程不变
    - 重做 = 追加 op_type='redo'，重新应用 payload
    - 版本只在“保存/提交”时产生，与撤销栈无关

反向操作只覆盖本软件允许的三个界面等价动作：改宽度、改几何、新建走线。
"""
from __future__ import annotations

import json

from . import routing
from .store import DB, now


def ensure_session(db: DB, project_id: str) -> str:
    row = db.one("SELECT id FROM edit_session WHERE project_id=? AND ended_at IS NULL "
                 "ORDER BY started_at DESC, rowid DESC LIMIT 1", (project_id,))
    if row:
        return row["id"]
    version = db.scalar("SELECT id FROM version WHERE project_id=? ORDER BY version_no DESC LIMIT 1",
                        (project_id,))
    sid = db.ins("edit_session", project_id=project_id, start_version_id=version,
                 app_version="1.2.0", machine_id="local", started_at=now(),
                 exit_kind="open", recovered=0)
    db.commit()
    return sid


def _next_seq(db: DB, session_id: str) -> int:
    return int(db.scalar("SELECT COALESCE(MAX(seq),0)+1 FROM operation WHERE session_id=?",
                         (session_id,), 1))


def record(db: DB, session_id: str, op_type: str, target_kind: str, target_id: str,
           payload: dict, inverse: dict, author_kind: str = "human",
           undo_of: str | None = None) -> str:
    return db.ins("operation", session_id=session_id, seq=_next_seq(db, session_id),
                  op_type=op_type, target_kind=target_kind, target_id=target_id,
                  payload_json=json.dumps(payload, ensure_ascii=False),
                  inverse_json=json.dumps(inverse, ensure_ascii=False),
                  undo_of_op_id=undo_of, author_kind=author_kind, created_at=now())


# ---------------- 带日志的编辑动作 ----------------

def change_width(db: DB, project_id: str, circuit_id: str, route_id: str, width_mm: float) -> dict:
    session = ensure_session(db, project_id)
    out = routing.set_width(db, circuit_id, route_id, width_mm)
    record(db, session, "update", "route", route_id,
           {"kind": "width", "route_id": route_id, "width_mm": out["after"]},
           {"kind": "width", "route_id": route_id, "width_mm": out["before"]})
    db.commit()
    return out


def component_op(db: DB, project_id: str, circuit_id: str, component_id: str,
                 kind: str, **kw) -> dict:
    """器件操作统一入口：先快照，再执行，再记一条可撤销操作。

    反操作存**整器件快照**而不是增量：旋转与翻面叠加后，
    增量回滚会把焊盘算错，而快照直接恢复现场。
    """
    session = ensure_session(db, project_id)
    before = routing.component_snapshot(db, component_id)
    if kind == "move":
        routing.move_component(db, circuit_id, component_id, float(kw["dx"]), float(kw["dy"]),
                               snap_mm=kw.get("snap_mm"))
    elif kind == "rotate":
        routing.rotate_component(db, circuit_id, component_id, int(kw["deg"]))
    elif kind == "flip":
        routing.flip_component(db, circuit_id, component_id)
    elif kind == "lock":
        routing.lock_component(db, circuit_id, component_id, bool(kw["locked"]))
    else:
        raise ValueError(f"未知器件操作 {kind}")
    after = routing.component_snapshot(db, component_id)
    record(db, session, "update", "component", component_id,
           {"kind": "component", **after}, {"kind": "component", **before})
    db.commit()
    return {"kind": kind, "before": before, "after": after}


def delete_route(db: DB, project_id: str, route_id: str) -> dict:
    """删走线（软删除），并落下可撤销的反向操作。"""
    session = ensure_session(db, project_id)
    before = db.one("SELECT deleted_at FROM route WHERE id=?", (route_id,))
    if not before:
        raise ValueError(f"走线不存在：{route_id}")
    db.c.execute("UPDATE route SET deleted_at=? WHERE id=?", (now(), route_id))
    record(db, session, "delete", "route", route_id,
           {"kind": "delete", "route_id": route_id},
           {"kind": "create", "route_id": route_id})
    db.commit()
    return {"route_id": route_id, "deleted": True}


def change_routes_width(db: DB, project_id: str, circuit_id: str,
                        route_ids: list[str], width_mm: float) -> dict:
    """框选批量改宽：逐条落操作，可逐条撤销。"""
    done = 0
    for rid in route_ids:
        change_width(db, project_id, circuit_id, rid, width_mm)
        done += 1
    return {"routes": done, "width_mm": width_mm}


def change_net_width(db: DB, project_id: str, circuit_id: str, net_id: str,
                     width_mm: float) -> dict:
    """整网批量改宽：对同一网络的每段走线分别落一条操作，可逐段撤销。"""
    rows = db.q("SELECT id FROM route WHERE net_id=? AND deleted_at IS NULL", (net_id,))
    for r in rows:
        change_width(db, project_id, circuit_id, r["id"], width_mm)
    return {"net_id": net_id, "routes": len(rows), "width_mm": width_mm}


def change_points(db: DB, project_id: str, route_id: str, pts: list[tuple[float, float]]) -> dict:
    from .board import load_route_points
    session = ensure_session(db, project_id)
    row = db.one("SELECT * FROM route WHERE id=?", (route_id,))
    before = load_route_points(db, row)
    routing.set_points(db, route_id, pts)
    record(db, session, "update", "route", route_id,
           {"kind": "points", "route_id": route_id,
            "points": [[round(x, 4), round(y, 4)] for x, y in pts]},
           {"kind": "points", "route_id": route_id,
            "points": [[round(x, 4), round(y, 4)] for x, y in before]})
    db.commit()
    return {"route_id": route_id, "points": len(pts)}


def create_route(db: DB, project_id: str, circuit_id: str, net_id: str,
                 pts: list[tuple[float, float]], width_mm: float, layer: int = 0) -> str:
    session = ensure_session(db, project_id)
    rid = routing.add_route(db, circuit_id, net_id, pts, width_mm, layer)
    record(db, session, "create", "route", rid,
           {"kind": "create", "route_id": rid},
           {"kind": "delete", "route_id": rid})
    db.commit()
    return rid


# ---------------- 撤销 / 重做 ----------------

def _apply(db: DB, action: dict) -> None:
    kind = action.get("kind")
    if kind == "width":
        db.c.execute("UPDATE route SET width_mm=? WHERE id=?",
                     (float(action["width_mm"]), action["route_id"]))
    elif kind == "points":
        routing.set_points(db, action["route_id"],
                           [(float(x), float(y)) for x, y in action["points"]])
    elif kind == "delete":
        db.c.execute("UPDATE route SET deleted_at=? WHERE id=?", (now(), action["route_id"]))
    elif kind == "create":
        db.c.execute("UPDATE route SET deleted_at=NULL WHERE id=?", (action["route_id"],))
    elif kind == "component":
        routing.apply_component_snapshot(db, action)
    else:
        raise ValueError(f"未知反向动作 {kind}")


def _ops(db: DB, session_id: str) -> list:
    return db.q("SELECT * FROM operation WHERE session_id=? ORDER BY seq", (session_id,))


def undo(db: DB, session_id: str) -> dict | None:
    ops = _ops(db, session_id)
    undone = {o["undo_of_op_id"] for o in ops if o["op_type"] == "undo"}
    candidates = [o for o in ops if o["op_type"] in ("update", "create", "delete")
                  and o["id"] not in undone]
    if not candidates:
        return None
    target = candidates[-1]
    _apply(db, json.loads(target["inverse_json"]))
    record(db, session_id, "undo", target["target_kind"], target["target_id"],
           json.loads(target["inverse_json"]), json.loads(target["payload_json"]),
           undo_of=target["id"])
    db.commit()
    return {"undone": target["id"], "target": target["target_kind"],
            "version_unchanged": True}


def redo(db: DB, session_id: str) -> dict | None:
    ops = _ops(db, session_id)
    redone = {o["undo_of_op_id"] for o in ops if o["op_type"] == "redo"}
    undos = [o for o in ops if o["op_type"] == "undo" and o["id"] not in redone]
    if not undos:
        return None
    last = undos[-1]
    payload = json.loads(last["inverse_json"])     # undo 的 inverse 就是原操作
    _apply(db, payload)
    record(db, session_id, "redo", last["target_kind"], last["target_id"],
           payload, json.loads(last["payload_json"]), undo_of=last["id"])
    db.commit()
    return {"redone": last["id"], "target": last["target_kind"]}


def undo_to(db: DB, session_id: str, target_seq: int) -> dict:
    """回退到指定步之后：把 seq > target_seq 的所有未撤销操作依次撤销。"""
    count = 0
    while True:
        ops = _ops(db, session_id)
        undone = {o["undo_of_op_id"] for o in ops if o["op_type"] == "undo"}
        pending = [o for o in ops if o["op_type"] in ("update", "create", "delete")
                   and o["id"] not in undone and o["seq"] > target_seq]
        if not pending or not undo(db, session_id):
            break
        count += 1
    return {"undone": count, "target_seq": target_seq}


def current_stack(db: DB, project_id: str) -> list[dict]:
    """当前会话的撤销栈（只读，不创建会话；界面用）。"""
    row = db.one("SELECT id FROM edit_session WHERE project_id=? AND ended_at IS NULL "
                 "ORDER BY started_at DESC, rowid DESC LIMIT 1", (project_id,))
    if not row:
        return []
    return stack(db, row["id"])


def stack(db: DB, session_id: str) -> list[dict]:
    ops = _ops(db, session_id)
    undone = {o["undo_of_op_id"] for o in ops if o["op_type"] == "undo"}
    out = []
    for o in ops:
        if o["op_type"] not in ("update", "create", "delete"):
            continue
        out.append({"id": o["id"], "seq": o["seq"], "kind": o["op_type"],
                    "target": o["target_kind"], "undone": o["id"] in undone})
    return out
