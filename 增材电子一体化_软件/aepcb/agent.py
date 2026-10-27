"""设计 Agent：规则化"检查—修改—复检"循环，以及可复现重放。

首版不接云端模型（AGT-010 断网降级），策略是确定性的：
    每轮取扣分最重的问题 → 用界面等价操作修一处 → 复检 → 记一步
步骤里存**修改后的绝对状态**（宽度/点序），因此重放不依赖随机性，也不重新推理。
"""
from __future__ import annotations

import hashlib
import json

from . import mrc, routing
from .board import load_route_points
from .routing import min_gap, set_points
from .store import DB, now

TARGET_SCORE = 95.0
MAX_ROUNDS = 8
WEIGHT = mrc.WEIGHT


def start(db: DB, project_id: str, base_version_id: str,
          goal: str = "把 MRC 分数提到 95 以上") -> str:
    base_digest = hashlib.sha256(str(db.scalar(
        "SELECT COALESCE(SUM(LENGTH(geom_blob_hash)),0) FROM route "
        "WHERE circuit_id IN (SELECT id FROM circuit WHERE project_id=?)",
        (project_id,), 0)).encode()).hexdigest()
    return db.ins("plan", project_id=project_id, base_version_id=base_version_id,
                  base_digest=base_digest, agent_role="design",
                  goal=goal, constraints_json=json.dumps(
                      {"forbid_topology_change": True, "allowed_ops": ["route.set_width",
                                                                       "route.set_points"]},
                      ensure_ascii=False),
                  status="running", auth_level="step_confirm", model_id="rule-engine/local",
                  degraded=1, toolchain_version="aepcb-1.0.0", random_seed=0,
                  rounds_used=0, round_limit=MAX_ROUNDS,
                  budget_json=json.dumps({"max_rounds": MAX_ROUNDS}),
                  created_at=now())


def _pick(issues: list[dict]) -> dict | None:
    fixable = [i for i in issues if i["rule_code"] in ("R1", "R2", "R7", "R4", "R5")]
    if not fixable:
        return None
    return max(fixable, key=lambda i: WEIGHT[i["severity"]])


def _apply(db: DB, circuit_id: str, issue: dict, override: dict | None = None) -> dict | None:
    sug = json.loads(issue["suggestion_json"])
    if override:
        sug.update(override)
    if issue["rule_code"] == "R1":
        rid = issue["target_id"]
        row = db.one("SELECT * FROM route WHERE id=?", (rid,))
        target = max(0.6, float(sug.get("to", 0.6)))
        routing.set_width(db, circuit_id, rid, target)
        return {"tool": "route.set_width", "params": {"route_id": rid, "width_mm": target},
                "intent": f"把走线加宽到 {target} mm", "route_id": rid,
                "before": {"width_mm": row["width_mm"]}}
    rid = sug.get("route_id") or issue["target_id"]
    row = db.one("SELECT * FROM route WHERE id=?", (rid,))
    pts = load_route_points(db, row)
    delta = float(sug.get("delta_mm", 0.5))
    axis = sug.get("axis", "y")
    if len(pts) < 3:
        return None          # 没有中间拐点，移动端点会拉断焊盘连接
    if axis == "y":
        moved = [pts[0]] + [(x, y + delta) for x, y in pts[1:-1]] + [pts[-1]]
    else:
        moved = [pts[0]] + [(x - delta, y) for x, y in pts[1:-1]] + [pts[-1]]
    set_points(db, rid, moved)
    return {"tool": "route.set_points",
            "params": {"route_id": rid,
                       "points": [[round(x, 4), round(y, 4)] for x, y in moved]},
            "intent": f"拖动走线中间段使间距达标", "route_id": rid,
            "before": {"points": [[round(x, 4), round(y, 4)] for x, y in pts]}}


def _undo(db: DB, action: dict) -> None:
    """把一步操作恢复成执行前的几何（回滚与候选试算共用）。"""
    if action["before"].get("width_mm") is not None:
        db.c.execute("UPDATE route SET width_mm=? WHERE id=?",
                     (action["before"]["width_mm"], action["route_id"]))
    else:
        set_points(db, action["route_id"],
                   [(float(x), float(y)) for x, y in action["before"]["points"]])
    db.commit()


def _candidate_overrides(issue: dict) -> list[dict]:
    """为同一问题生成多个可选改法（参数不同，不是重新推理）。"""
    sug = json.loads(issue["suggestion_json"])
    if issue["rule_code"] == "R1":
        base = float(sug.get("to", 0.6))
        return [{"to": round(base * k, 3)} for k in (0.9, 1.0, 1.15, 1.3)]
    delta = float(sug.get("delta_mm", 0.5))
    return [{"delta_mm": round(delta * k, 3)} for k in (0.6, 0.8, 1.0, 1.3)]


def _rank_candidates(db: DB, circuit_id: str, issue: dict, bundle: dict) -> dict | None:
    """代理粗排：逐个试算 → 收集特征 → 排序 → 回滚。

    只决定"先试哪个"；应用后仍由规则引擎复检，不改进就回滚。
    排序用成对模型（高分区候选差异小，回归分不出）。
    """
    from . import surrogate as sg
    trials: list[tuple[dict, list[float]]] = []
    for ov in _candidate_overrides(issue):
        action = _apply(db, circuit_id, issue, override=ov)
        if action is None:
            continue
        try:
            trials.append((ov, sg.features(db, circuit_id)))
        except Exception:
            pass
        finally:
            _undo(db, action)
    if not trials:
        return None
    order = sg.rank(bundle, [f for _, f in trials])
    ov, _ = trials[order[0]]
    return {"override": ov}


def iterate(db: DB, project_id: str, version_id: str, plan_id: str | None = None,
            max_rounds: int = MAX_ROUNDS, target: float = TARGET_SCORE,
            candidates: int = 1, surrogate_bundle: dict | None = None) -> dict:
    plan_id = plan_id or start(db, project_id, version_id)
    circuit = db.one("SELECT * FROM circuit WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                     (project_id,))
    first = mrc.run(db, project_id, version_id)
    score = first["score"]
    db.c.execute("UPDATE plan SET score_before=? WHERE id=?", (score, plan_id))
    history, step_no, rounds = [score], 0, 0
    tried: set[tuple[str, str]] = set()
    while rounds < max_rounds and score < target:
        run = mrc.run(db, project_id, version_id)
        issue = _pick([i for i in run["issues"]
                       if (i["rule_code"], i["target_id"]) not in tried])
        if not issue:
            break
        tried.add((issue["rule_code"], issue["target_id"]))
        rounds += 1
        step_no += 1
        ranked = None
        if candidates > 1 and surrogate_bundle is not None:
            ranked = _rank_candidates(db, circuit["id"], issue, surrogate_bundle)
        action = _apply(db, circuit["id"], issue,
                        override=ranked["override"] if ranked else None)
        if action is None:
            continue
        db.commit()
        after = mrc.run(db, project_id, version_id)
        improved = after["score"] > score
        if not improved:
            # 回滚：没有改进的步骤不能留在几何里，否则越修越糟
            _undo(db, action)
            after = mrc.run(db, project_id, version_id)
        step_id = db.ins("plan_step", plan_id=plan_id, step_no=step_no,
                         intent=action["intent"], tool_name=action["tool"],
                         tool_version="1.0.0",
                         params_json=json.dumps({**action["params"],
                                                 "ranked_by": "surrogate" if ranked else "rule",
                                                 "rank_candidates": candidates if ranked else 1},
                                                ensure_ascii=False),
                         precondition_json=json.dumps({"issue": issue["rule_code"]}),
                         expected_json=json.dumps({"score_delta": ">=0"}),
                         status="applied" if improved else "failed",
                         risk_level="low", requires_confirm=1, confirm_decision="approved",
                         confirmed_by="local", confirmed_at=now(), created_at=now())
        db.ins("tool_call", step_id=step_id, seq=1, tool_name=action["tool"],
               tool_version="1.0.0", args_json=json.dumps(action["params"], ensure_ascii=False),
               result_status="ok" if improved else "error",
               result_digest=after["digest"], duration_ms=1, at=now())
        score = after["score"]
        history.append(score)
        db.c.execute("UPDATE plan SET rounds_used=? WHERE id=?", (rounds, plan_id))
        db.commit()

    status = "converged" if score >= target else ("stopped_no_improvement" if rounds else "rejected")
    db.c.execute("UPDATE plan SET status=?, score_after=?, ended_at=?, stop_reason=? WHERE id=?",
                 (status, score, now(), "" if status == "converged" else "无可用界面操作可继续改进",
                  plan_id))
    db.ins("diff_card", plan_id=plan_id, before_version_id=version_id, after_version_id=None,
           score_before=history[0], score_after=score,
           changed_objects_json=json.dumps([h for h in history]),
           command_list_json=json.dumps([f"step{i + 1}" for i in range(rounds)]),
           reason="按规则逐条修复 R1/R2/R7 违规", decision="apply",
           auth_level_at_time="step_confirm", decided_by="local", decided_at=now(), created_at=now())
    db.commit()
    return {"plan_id": plan_id, "score_before": history[0], "score_after": score,
            "rounds": rounds, "status": status, "history": history}


def replay(db: DB, plan_id: str, version_id: str) -> dict:
    """回放：按步骤记录的绝对状态重写几何，比对分数与几何摘要。"""
    steps = db.q("SELECT * FROM plan_step WHERE plan_id=? ORDER BY step_no", (plan_id,))
    if not steps:
        return {"matched": False, "reason": "计划没有步骤"}
    circuit = db.scalar("SELECT id FROM circuit WHERE project_id=(SELECT project_id FROM plan WHERE id=?)",
                        (plan_id,))
    for st in steps:
        params = json.loads(st["params_json"])
        if params.get("width_mm") is not None:
            routing.set_width(db, circuit, params["route_id"], float(params["width_mm"]))
        elif params.get("points"):
            set_points(db, params["route_id"], [(float(x), float(y)) for x, y in params["points"]])
    db.commit()
    plan = db.one("SELECT * FROM plan WHERE id=?", (plan_id,))
    after = mrc.run(db, project_id=plan["project_id"], version_id=version_id)
    matched = abs(after["score"] - float(plan["score_after"])) < 1e-6
    db.ins("replay_run", plan_id=plan_id, attempt_no=db.scalar(
        "SELECT COALESCE(MAX(attempt_no),0)+1 FROM replay_run WHERE plan_id=?", (plan_id,), 1),
        start_version_id=version_id, toolchain_version="aepcb-1.0.0",
        rule_version_id=None, gcode_content_hash=None, matched=1 if matched else 0,
        diff_summary_json=json.dumps({"score": after["score"], "plan_after": plan["score_after"]}),
        ran_at=now())
    db.commit()
    return {"matched": matched, "score": after["score"], "plan_score": plan["score_after"]}
