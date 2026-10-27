"""宽度扫描：给一批走线选目标宽度。

这是代理模型**唯一说得通的落点**：
    Agent 每轮只有 1 个候选，代理只会更慢；
    但"整网改宽选 0.5 还是 0.6"天然是 N 个候选比一次分，
    逐个跑完整规则是 N × 22 ms，先用代理粗排再对头部跑规则才划算。

诚实约定：
    - 代理只用于**排序**，最终分数与选择依据仍由规则给出；
    - 没有模型或模型不可用时，自动退回"逐个跑规则"的穷举路径。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import mrc, routing
from .board import load_route_points
from .store import DB, now


def _targets(db: DB, circuit_id: str, net_id: str | None, route_ids: list[str] | None):
    if route_ids:
        marks = ",".join("?" for _ in route_ids)
        return db.q(f"SELECT * FROM route WHERE id IN ({marks}) AND deleted_at IS NULL",
                    tuple(route_ids))
    if net_id:
        return db.q("SELECT * FROM route WHERE net_id=? AND deleted_at IS NULL", (net_id,))
    return db.q("SELECT * FROM route WHERE circuit_id=? AND deleted_at IS NULL", (circuit_id,))


def width_sweep(db: DB, project_id: str, version_id: str, circuit_id: str,
                widths: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8),
                net_id: str | None = None, route_ids: list[str] | None = None,
                bundle: dict | None = None) -> dict:
    """对一批走线扫宽度：代理粗排 → 对头部跑规则确认 → 全部结果留档。

    返回里同时给"穷举全部跑规则"与"代理粗排 + 1 次确认"的耗时，
    让收益可以被验证，而不是靠宣称。
    """
    rows = _targets(db, circuit_id, net_id, route_ids)
    if not rows:
        raise ValueError("没有可改宽的走线")
    original = {r["id"]: float(r["width_mm"]) for r in rows}

    def apply_all(width: float) -> None:
        for rid in original:
            db.c.execute("UPDATE route SET width_mm=? WHERE id=?", (width, rid))
        db.commit()

    def restore() -> None:
        for rid, w in original.items():
            db.c.execute("UPDATE route SET width_mm=? WHERE id=?", (w, rid))
        db.commit()

    from . import surrogate as sg

    results: list[dict] = []
    t_exact = 0.0
    t_sur = 0.0
    for w in widths:
        apply_all(w)
        t0 = time.perf_counter()
        run = mrc.run(db, project_id, version_id)
        t_exact += (time.perf_counter() - t0) * 1000
        item = {"width_mm": w, "score": run["score"], "issues": len(run["issues"]),
                "pred": None}
        if bundle is not None:
            t1 = time.perf_counter()
            item["pred"] = round(sg.predict(bundle, sg.features(db, circuit_id)), 3)
            t_sur += (time.perf_counter() - t1) * 1000
        results.append(item)
        restore()

    best_exact = max(results, key=lambda r: (r["score"], -r["width_mm"]))
    out = {
        "routes": len(rows),
        "widths": list(widths),
        "results": results,
        "best_exact": {"width_mm": best_exact["width_mm"], "score": best_exact["score"]},
        "ms_exact": round(t_exact, 1),
        "mode": "rule" if bundle is None else "surrogate+rule",
    }
    if bundle is not None:
        # 粗排：按代理预测取前 1，再跑规则确认（最终依据仍是规则）
        top = max(results, key=lambda r: (r["pred"], -r["width_mm"]))
        confirm_ms = 0.0
        apply_all(top["width_mm"])
        t0 = time.perf_counter()
        confirmed = mrc.run(db, project_id, version_id)
        confirm_ms = (time.perf_counter() - t0) * 1000
        restore()
        out.update({
            "best_surrogate": {"width_mm": top["width_mm"], "score": confirmed["score"]},
            "agree": top["width_mm"] == best_exact["width_mm"],
            "ms_surrogate": round(t_sur + confirm_ms, 1),
            "speedup": round(t_exact / (t_sur + confirm_ms), 2) if (t_sur + confirm_ms) else None,
        })
    return out


def run(workspace: Path, widths: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8),
        net_name: str | None = None) -> dict:
    """在最新工程上跑一次宽度扫描（含工程包留痕需要的开销测量）。"""
    from . import surrogate as sg

    ws = Path(workspace)
    db = DB(ws / "app.db")
    pid = db.scalar("SELECT id FROM project ORDER BY created_at DESC, rowid DESC LIMIT 1")
    vid = db.scalar("SELECT id FROM version WHERE project_id=? ORDER BY version_no DESC LIMIT 1",
                    (pid,))
    cid = db.scalar("SELECT id FROM circuit WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                    (pid,))
    net_id = db.scalar("SELECT id FROM net WHERE circuit_id=? AND name=?", (cid, net_name)) \
        if net_name else None
    bundle = sg.load(ws)
    out = width_sweep(db, pid, vid, cid, widths=widths, net_id=net_id, bundle=bundle)
    db.close()
    return out
