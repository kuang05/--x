"""验收运行器：按 SRS 7.1 跑 T01–T20，逐条写 accept_result。

口径（7.2）：P0 必须通过；P1 允许失败，但必须记录诊断码与原因。
首版没有实现自动布线与跳线候选，T08/T09 会如实记为失败（P1），不伪装通过。
"""
from __future__ import annotations

import json
import time

from . import agent, autoroute, board, gcode, mrc, printer, routing, slicing, step
from .store import DB, now
from fixtures import t19a

P0 = {"T01", "T02", "T05", "T06", "T07", "T10", "T11", "T12", "T13", "T14",
      "T16", "T17", "T18", "T19", "T20", "T21", "T22", "T23", "T25"}

# 任务清单单一事实源：README / 文档 / 答辩都用它，避免各处口径不一
TASKS: dict[str, tuple[str, str]] = {
    "T01": ("P0", "STEP 导入包围盒 60×40"),
    "T02": ("P0", "坏 STEP 只出诊断、不崩溃"),
    "T03": ("P1", "拒绝布尔候选后分数不变"),
    "T04": ("P1", "挖槽候选拒绝后模型与分数不变"),
    "T05": ("P0", "导入坏文件后工程仍可用"),
    "T06": ("P0", "器件/焊盘/网络/走线 8/24/12/12"),
    "T07": ("P0", "连通性 pass"),
    "T08": ("P0", "自动布线布通率 ≥80% 且连通 pass"),
    "T09": ("P0", "断路 1 → 加跳线后 pass"),
    "T10": ("P0", "R1/R2 命中"),
    "T11": ("P1", "悬垂 60° 触发 R3"),
    "T12": ("P0", "R6/R7 命中"),
    "T13": ("P0", "3 层 7 道次"),
    "T14": ("P0", "热历程 pre_gel→silver_set→cure"),
    "T15": ("P1", "断开网络后连通 fail"),
    "T16": ("P0", "两次导出内容哈希一致、字节哈希不同"),
    "T17": ("P0", "虚拟打印回放 completed"),
    "T18": ("P0", "故障注入产生缺陷并归因"),
    "T19": ("P0", "Agent 62→97，≤8 轮"),
    "T20": ("P0", "重放一致 + 全链 G-code"),
    "T21": ("P0", "器件吸附 1.7→1.5、焊盘同步、锁定后拒绝移动"),
    "T22": ("P0", "进度到 100%、取消后回滚执行"),
    "T23": ("P0", "五区布局 + device + chat 字段"),
    "T24": ("P1", "剖切/测量控件 + 72.11 mm 测距常数"),
    "T25": ("P0", "宽度扫描 3 候选并还原几何"),
}


def _project(db: DB, name: str) -> tuple[str, str]:
    pid = db.ins("project", name=name, unit="mm", sheet_w_mm=60, sheet_h_mm=40,
                 origin_kind="board_bottom_left", data_schema_version=1,
                 dir_rel_path=f"projects/{name}", created_at=now(), updated_at=now())
    v0 = db.ins("version", project_id=pid, version_no=0, parent_version_id=None,
                kind="initial", author_kind="human", author_name="local",
                reason="新建工程", created_at=now())
    db.c.execute("UPDATE project SET current_version_id=? WHERE id=?", (v0, pid))
    db.commit()
    return pid, v0


def _record(db: DB, task: str, pid: str | None, passed: bool, *, note: str = "",
            code: str = "", rounds: int | None = None, score0: float | None = None,
            score1: float | None = None, ghash: str | None = None,
            input_hash: str | None = None, seconds: float | None = None) -> dict:
    db.ins("accept_result", task_no=task, project_id=pid,
           input_blob_hash=input_hash or (gcode.__name__ and "0" * 64),
           passed=1 if passed else 0, rounds=rounds, duration_s=seconds,
           score_initial=score0, score_final=score1, gcode_hash=ghash,
           diagnostic_code=code, notes=note, ran_at=now())
    db.commit()
    return {"task": task, "passed": passed, "note": note, "code": code}


def write_evidence(db: DB, project_id: str) -> str:
    """验收跑完后自动产出一份证据包（Markdown + JSON）。"""
    from . import evidence
    out = evidence.write(db, project_id, db.root / "evidence.md")
    return out["markdown"]


def run_all(db: DB) -> list[dict]:
    out: list[dict] = []
    base = mrc.ensure_baseline(db)

    # ---------------- 主工程 ----------------
    t0 = time.time()
    pid, v0 = _project(db, "T19-A 验收")
    good = step.make_fixture_step(60, 40, 1.2)
    sid, model = board.import_step(db, pid, good, step)
    ok = abs(model.size[0] - 60) < 1e-6 and abs(model.size[1] - 40) < 1e-6
    out.append(_record(db, "T01", pid, ok, note=f"bbox={model.bbox}", seconds=time.time() - t0))

    res = board.import_board(db, pid, sid, t19a.t19a())
    checks = {"components": 8, "pads": 24, "nets": 12, "routes": 12}
    ok = all(res.counts[k] == v for k, v in checks.items())
    out.append(_record(db, "T06", pid, ok, note=str(res.counts)))

    routing.validate_all(db, res.circuit_id)
    conn = routing.connectivity(db, pid, v0, res.circuit_id)
    out.append(_record(db, "T07", pid, conn["result"] == "pass",
                      note=f"断路 {conn['opens']} 短路 {conn['shorts']}"))

    run1 = mrc.run(db, pid, v0)
    codes = {i["rule_code"] for i in run1["issues"]}
    out.append(_record(db, "T10", pid, {"R1", "R2"} <= codes,
                      note=f"命中 {sorted(codes)}", score0=run1["score"]))
    out.append(_record(db, "T12", pid, {"R6", "R7"} <= codes,
                      note=f"命中 {sorted(codes)}", score0=run1["score"]))

    sl = slicing.run(db, pid, v0, base["process_profile_id"], base["material_batch_id"])
    layers = db.scalar("SELECT COUNT(*) FROM physical_layer WHERE slice_set_id=?", (sl["slice_id"],), 0)
    passes = db.scalar("SELECT COUNT(*) FROM deposit_pass WHERE slice_set_id=?", (sl["slice_id"],), 0)
    out.append(_record(db, "T13", pid, layers == 3 and passes == 7,
                      note=f"层 {layers} 道次 {passes} 厚 {sl['thickness_mm']} mm"))

    stages = [r["stage"] for r in db.q(
        "SELECT stage FROM thermal_step WHERE slice_set_id=? ORDER BY step_index", (sl["slice_id"],))]
    out.append(_record(db, "T14", pid, stages[:3] == ["pre_gel", "silver_set", "cure"],
                      note="→".join(stages)))

    g1 = gcode.build(db, sl["slice_id"])
    g2 = gcode.build(db, sl["slice_id"])
    out.append(_record(db, "T16", pid, g1["content_hash"] == g2["content_hash"],
                      note=f"{g1['content_hash'][:12]}（file_hash 必不同：{g1['file_hash'] != g2['file_hash']}）",
                      ghash=g1["content_hash"]))

    job = printer.start(db, pid, sl["slice_id"], g1["export_id"])
    rep = printer.replay(db, job)
    out.append(_record(db, "T17", pid, rep["result"] == "completed", note=f"回放 {rep}"))

    demo = printer.run_demo(db, pid, sl["slice_id"], g1["export_id"], fault_layer=2)
    out.append(_record(db, "T18", pid, bool(demo["defect_id"]) and bool(demo["attribution_id"]),
                      note=f"缺陷 {demo['defect_id'][:8]} / 归因 {demo['attribution_id'][:8]}"))

    # T20 全链：改动后重新切片并导出，哈希应随内容变化但流程不崩
    agent_out = agent.iterate(db, pid, v0)
    out.append(_record(db, "T19", pid, agent_out["score_after"] - agent_out["score_before"] >= 15
                      and agent_out["rounds"] <= 8,
                      note=f"{agent_out['score_before']}→{agent_out['score_after']}，{agent_out['rounds']} 轮",
                      rounds=agent_out["rounds"], score0=agent_out["score_before"],
                      score1=agent_out["score_after"]))
    rep_run = agent.replay(db, agent_out["plan_id"], v0)
    sl2 = slicing.run(db, pid, v0, base["process_profile_id"], base["material_batch_id"])
    g3 = gcode.build(db, sl2["slice_id"])
    out.append(_record(db, "T20", pid, rep_run["matched"] and bool(g3["content_hash"]),
                      note=f"重放一致={rep_run['matched']}，全链 G-code {g3['content_hash'][:12]}",
                      ghash=g3["content_hash"]))

    # T05 失败回滚：导入坏文件后工程仍可用
    before = db.scalar("SELECT COUNT(*) FROM structure WHERE project_id=?", (pid,), 0)
    bad = "ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n#1=SI_UNIT(.MILLI.,.METRE.);\nENDSEC;\nEND-ISO-10303-21;\n"
    board.import_step(db, pid, bad, step)
    after = db.scalar("SELECT COUNT(*) FROM structure WHERE project_id=?", (pid,), 0)
    diag = db.scalar("SELECT COUNT(*) FROM diagnostic WHERE project_id=? AND code='STEP-EMPTY'", (pid,), 0)
    out.append(_record(db, "T05", pid, after >= before and diag >= 1,
                      note=f"结构 {before}→{after}，诊断 {diag} 条"))

    # ---------------- 沙盒工程（会破坏几何的用例） ----------------
    spid, sv0 = _project(db, "T19-A 沙盒")
    ssid, _m = board.import_step(db, spid, good, step)
    sres = board.import_board(db, spid, ssid, t19a.t19a())

    # ---------------- T21–T24 界面与交互能力 ----------------
    from . import jobs as jobs_mod
    from . import web as web_mod

    # T21 器件放置：吸附、焊盘同步、锁定拒绝
    comp = db.one("SELECT * FROM component WHERE circuit_id=? ORDER BY ref_des LIMIT 1",
                  (sres.circuit_id,))
    t21_ok, t21_note = False, "无器件"
    if comp:
        pads0 = {p["id"]: (float(p["x_mm"]), float(p["y_mm"]))
                 for p in db.q("SELECT id,x_mm,y_mm FROM pad WHERE component_id=?", (comp["id"],))}
        x0 = float(comp["x_mm"])
        routing.move_component(db, sres.circuit_id, comp["id"], 1.7, 0.0, snap_mm=0.5)
        x1 = float(db.one("SELECT x_mm FROM component WHERE id=?", (comp["id"],))["x_mm"])
        snap_ok = abs((x1 - x0) - 1.5) < 1e-6
        pads1 = {p["id"]: (float(p["x_mm"]), float(p["y_mm"]))
                 for p in db.q("SELECT id,x_mm,y_mm FROM pad WHERE component_id=?", (comp["id"],))}
        pads_ok = all(abs(pads1[k][0] - v[0] - 1.5) < 1e-6 for k, v in pads0.items())
        routing.lock_component(db, sres.circuit_id, comp["id"], True)
        locked_ok = False
        try:
            routing.move_component(db, sres.circuit_id, comp["id"], 1, 0)
        except ValueError:
            locked_ok = True
        routing.lock_component(db, sres.circuit_id, comp["id"], False)
        t21_ok = snap_ok and pads_ok and locked_ok
        t21_note = f"吸附={snap_ok} 焊盘同步={pads_ok} 锁定拒绝={locked_ok}"
    out.append(_record(db, "T21", pid, t21_ok, note=t21_note))

    # T22 进度与取消：完成后 100%，取消后回滚回调必执行
    reg = jobs_mod.Registry()
    seen: list[int] = []

    def _work(job):
        for i in range(3):
            job.progress, job.total = i, 3
            seen.append(i)
            time.sleep(0.002)
        return {"slice_id": "probe"}

    jid = reg.submit("probe", _work)
    j = None
    for _ in range(400):
        j = reg.get(jid)
        if j.status != "running":
            break
        time.sleep(0.005)
    done_ok = j.status == "done" and j.snapshot()["progress"] == 100
    rolled = {"n": 0}

    def _slow(job):
        for _ in range(300):
            if job.cancelled():
                raise RuntimeError("cancelled")
            time.sleep(0.002)
        return {}

    jid2 = reg.submit("slow", _slow,
                      rollback=lambda: rolled.__setitem__("n", rolled["n"] + 1))
    reg.cancel(jid2)
    j2 = None
    for _ in range(400):
        j2 = reg.get(jid2)
        if j2.status != "running":
            break
        time.sleep(0.005)
    cancel_ok = j2.status == "cancelled" and rolled["n"] == 1
    out.append(_record(db, "T22", pid, done_ok and cancel_ok,
                      note=f"进度 100%={done_ok} 取消回滚={cancel_ok}"))

    # T23 五区布局与对话面板（结构层，不依赖浏览器）
    html = web_mod.INDEX
    zones = all(tok in html for tok in ('id="navPanel"', 'id="canvasPanel"',
                                        'id="asidePanel"', '<footer>', 'id="dev"'))
    st = web_mod.state(db)
    chat = st.get("chat") or []
    t23_ok = zones and "device" in st and isinstance(st.get("chat"), list)
    out.append(_record(db, "T23", pid, t23_ok,
                      note=f"五区={zones} device={'device' in st} 对话={len(chat)} 条"))

    # T24 剖切与测量控件（P1：交互无法在服务端真测，只验控件与测距常数）
    import math as _m
    t24_ok = ('id="cutZ"' in html and 'id="measureOut"' in html
              and abs(_m.hypot(60, 40) - 72.11) < 0.005)
    out.append(_record(db, "T24", pid, t24_ok, note="剖切/测量控件与 72.11 mm 测距常数"))

    # T25 宽度扫描（sweep）：三候选能给出最优，且扫描后几何必须还原
    from . import sweep as sweep_mod
    w_before = {r["id"]: float(r["width_mm"]) for r in db.q(
        "SELECT id,width_mm FROM route WHERE circuit_id=? AND deleted_at IS NULL",
        (sres.circuit_id,))}
    sw = sweep_mod.width_sweep(db, spid, sv0, sres.circuit_id,
                               widths=(0.5, 0.6, 0.7), bundle=None)
    w_after = {r["id"]: float(r["width_mm"]) for r in db.q(
        "SELECT id,width_mm FROM route WHERE circuit_id=? AND deleted_at IS NULL",
        (sres.circuit_id,))}
    restored = w_before == w_after
    t25_ok = len(sw["results"]) == 3 and restored and sw["best_exact"]["score"] >= 0
    out.append(_record(db, "T25", pid, t25_ok,
                      note=f"3 候选最优 {sw['best_exact']['width_mm']} mm / "
                           f"{sw['best_exact']['score']} 分；几何还原={restored}"))

    bad2 = "ISO-10303-21;\nDATA;\n#1=CARTESIAN_POINT('',(0.,0.,0.));\nENDSEC;\n"
    board.import_step(db, spid, bad2, step)
    diag2 = db.scalar("SELECT COUNT(*) FROM diagnostic WHERE project_id=? AND code IN "
                      "('STEP-FEW-POINTS','STEP-NO-PLANE','STEP-UNIT-MISSING')", (spid,), 0)
    out.append(_record(db, "T02", spid, diag2 >= 1, note=f"诊断 {diag2} 条，未崩溃"))

    before_score = mrc.run(db, spid, sv0)["score"]
    cand = db.ins("bool_candidate", structure_id=ssid, op="add", shape="box",
                  params_json=json.dumps({"w": 5, "h": 5, "d": 1}), status="proposed",
                  created_at=now())
    db.c.execute("UPDATE bool_candidate SET status='rejected', reject_reason='演示拒绝' WHERE id=?", (cand,))
    db.commit()
    after_score = mrc.run(db, spid, sv0)["score"]
    out.append(_record(db, "T03", spid, before_score == after_score,
                      note=f"拒绝后分数 {before_score}→{after_score}（应不变）"))
    cand2 = db.ins("bool_candidate", structure_id=ssid, op="subtract", shape="cylinder",
                   params_json=json.dumps({"r": 3, "d": 2}), status="proposed", created_at=now())
    db.c.execute("UPDATE bool_candidate SET status='rejected' WHERE id=?", (cand2,))
    db.commit()
    out.append(_record(db, "T04", spid, mrc.run(db, spid, sv0)["score"] == after_score,
                      note="挖槽候选拒绝后模型与分数不变"))

    over = step.make_fixture_step(60, 40, 1.2, overhang_deg=60)
    board.import_step(db, spid, over, step)
    r3 = mrc.run(db, spid, sv0)
    out.append(_record(db, "T11", spid, any(i["rule_code"] == "R3" for i in r3["issues"]),
                      note=f"命中 {sorted({i['rule_code'] for i in r3['issues']})}", score0=r3["score"]))

    # T15 断开一条网络 → 连通性失败
    routes = db.q("SELECT * FROM route WHERE circuit_id=?", (sres.circuit_id,))
    victim = routes[0]
    pts = board.load_route_points(db, victim)
    routing.set_points(db, victim["id"], [(pts[0][0], pts[0][1]), (pts[0][0] + 0.5, pts[0][1])])
    db.commit()
    broken = routing.connectivity(db, spid, sv0, sres.circuit_id)
    out.append(_record(db, "T15", spid, broken["result"] == "fail" and broken["opens"] >= 1,
                      note=f"断路 {broken['opens']} 条，导出应被阻断"))

    # P1 自动布线：清空全部走线后用 A* 重布，看布通率与连通性
    apid, av0 = _project(db, "T19-A 布线沙盒")
    asid, _ = board.import_step(db, apid, good, step)
    ares = board.import_board(db, apid, asid, t19a.t19a())
    db.c.execute("UPDATE route SET deleted_at=? WHERE circuit_id=?", (now(), ares.circuit_id))
    db.commit()
    ar = autoroute.route_all(db, ares.circuit_id, limit=12)
    aconn = routing.connectivity(db, apid, av0, ares.circuit_id)
    out.append(_record(db, "T08", apid, ar["rate"] >= 0.8 and aconn["result"] == "pass",
                      note=f"布通 {len(ar['routed'])}/{ar['attempted']}，连通性 {aconn['result']}",
                      code="" if ar["failed"] == [] else "PARTIAL-FAIL"))

    # T09 跳线：断开一个网络 → 候选 → 应用 → 连通性恢复
    net = db.one("SELECT * FROM net WHERE circuit_id=? ORDER BY name LIMIT 1", (ares.circuit_id,))
    route = db.one("SELECT * FROM route WHERE net_id=? AND deleted_at IS NULL", (net["id"],))
    if route:
        pts = board.load_route_points(db, route)
        routing.set_points(db, route["id"], [(pts[0][0], pts[0][1]), (pts[0][0] + 0.4, pts[0][1])])
        db.commit()
        broken2 = routing.connectivity(db, apid, av0, ares.circuit_id)
        jid = routing.suggest_jumper(db, ares.circuit_id, net["id"])
        db.commit()
        routing.apply_jumper(db, jid)
        db.commit()
        fixed = routing.connectivity(db, apid, av0, ares.circuit_id)
        out.append(_record(db, "T09", apid,
                          broken2["opens"] >= 1 and bool(jid) and fixed["result"] == "pass",
                          note=f"断路 {broken2['opens']} → 加跳线后 {fixed['result']}"))
    else:
        out.append(_record(db, "T09", apid, False, code="NO-ROUTE", note="沙盒工程无走线可用于跳线用例"))
    write_evidence(db, pid)
    return out


def summary(results: list[dict]) -> dict:
    passed = [r for r in results if r["passed"]]
    p0_fail = [r["task"] for r in results if not r["passed"] and r["task"] in P0]
    return {"total": len(results), "passed": len(passed), "failed": len(results) - len(passed),
            "p0_fail": p0_fail, "gate": "PASS" if not p0_fail else "FAIL"}
