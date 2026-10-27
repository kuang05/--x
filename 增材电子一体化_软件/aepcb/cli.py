"""命令行入口：python3 -m aepcb.cli <command>"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import accept as accept_mod
from . import agent, autoroute, board, gcode, kicad, mrc, printer, report, routing, slicing, step
from .store import DB, now

WS = Path(__file__).resolve().parents[1] / "workspace"


def _open(ws: Path) -> DB:
    ws.mkdir(parents=True, exist_ok=True)
    return DB(ws / "app.db")


def _project(db: DB, name: str, w: float, h: float) -> tuple[str, str]:
    pid = db.ins("project", name=name, unit="mm", sheet_w_mm=w, sheet_h_mm=h,
                 origin_kind="board_bottom_left", data_schema_version=1,
                 dir_rel_path=f"projects/{name}", created_at=now(), updated_at=now())
    v0 = db.ins("version", project_id=pid, version_no=0, parent_version_id=None, kind="initial",
                author_kind="human", author_name="local", reason="新建工程", created_at=now())
    db.c.execute("UPDATE project SET current_version_id=? WHERE id=?", (v0, pid))
    db.commit()
    return pid, v0


def cmd_init(args) -> None:
    db = _open(Path(args.workspace))
    pid, v0 = _project(db, "空工程", 60, 40)
    print(f"已初始化：{db.path}\n工程 {pid} 版本 {v0}")
    db.close()


def cmd_new(args) -> None:
    db = _open(Path(args.workspace))
    pid = db.ins("project", name=args.name, unit="mm", sheet_w_mm=args.w, sheet_h_mm=args.h,
                 origin_kind="board_bottom_left", data_schema_version=1,
                 dir_rel_path=f"projects/{args.name}", created_at=now(), updated_at=now())
    v0 = db.ins("version", project_id=pid, version_no=0, kind="initial", author_kind="human",
                author_name="local", reason="新建工程", created_at=now())
    db.c.execute("UPDATE project SET current_version_id=? WHERE id=?", (v0, pid))
    db.commit()
    print(f"工程已创建 {pid[:8]}（{args.name}，{args.w}×{args.h} mm）")
    db.close()


def cmd_import(args) -> None:
    import json as _json
    ws = Path(args.workspace)
    db = _open(ws)
    pid = db.scalar("SELECT id FROM project ORDER BY created_at DESC, rowid DESC LIMIT 1")
    if not pid:
        raise SystemExit("没有工程，先跑 `aepcb new`")
    v = db.scalar("SELECT id FROM version WHERE project_id=? ORDER BY version_no DESC LIMIT 1", (pid,))
    step_text = Path(args.step).read_text(encoding="utf-8")
    sid, model = board.import_step(db, pid, step_text, step)
    print(f"STEP：{model.schema} {model.units} bbox={tuple(round(x, 2) for x in model.bbox)}")
    if args.board:
        spec = _json.loads(Path(args.board).read_text(encoding="utf-8"))
        res = board.import_board(db, pid, sid, spec)
        print(f"板文件：{res.counts}")
        conn = routing.connectivity(db, pid, v, res.circuit_id)
        print(f"连通性：{conn['result']}（断路 {conn['opens']} / 短路 {conn['shorts']}）")
    db.close()


def cmd_slice(args) -> None:
    db = _open(Path(args.workspace))
    pid = db.scalar("SELECT id FROM project ORDER BY created_at DESC, rowid DESC LIMIT 1")
    v = db.scalar("SELECT id FROM version WHERE project_id=? ORDER BY version_no DESC LIMIT 1", (pid,))
    base = mrc.ensure_baseline(db)
    out = slicing.run(db, pid, v, base["process_profile_id"], base["material_batch_id"])
    print(f"切片 {out['passes']} 道次 {out['paths']} 段 {out['length_mm']} mm；"
          f"连通性 {out['connectivity']}（断路 {out['opens']}）")
    db.close()


def cmd_export(args) -> None:
    db = _open(Path(args.workspace))
    pid = db.scalar("SELECT id FROM project ORDER BY created_at DESC, rowid DESC LIMIT 1")
    sl = db.scalar("SELECT id FROM slice_set WHERE project_id=? AND status='ready' "
                   "ORDER BY created_at DESC LIMIT 1", (pid,))
    if not sl:
        raise SystemExit("没有可用切片，先跑 `aepcb slice`")
    out = gcode.build(db, sl)
    print(f"G-code content={out['content_hash'][:16]} file={out['file_hash'][:12]} "
          f"{out['bytes'] // 1024} KB -> {out['path']}")
    db.close()


def cmd_print(args) -> None:
    db = _open(Path(args.workspace))
    pid = db.scalar("SELECT id FROM project ORDER BY created_at DESC, rowid DESC LIMIT 1")
    sl = db.scalar("SELECT id FROM slice_set WHERE project_id=? AND status='ready' "
                   "ORDER BY created_at DESC LIMIT 1", (pid,))
    exp = db.scalar("SELECT id FROM gcode_export WHERE slice_set_id=? "
                    "ORDER BY created_at DESC LIMIT 1", (sl,)) if sl else None
    if not (sl and exp):
        raise SystemExit("先切片并导出 G-code")
    out = printer.run_demo(db, pid, sl, exp, fault_layer=args.fault_layer)
    print(f"作业 {out['job_id'][:8]}：{out['replay']['result']}；"
          f"缺陷 {out['defect_id'][:8]} 已归因")
    db.close()


def cmd_agent(args) -> None:
    from . import surrogate
    db = _open(Path(args.workspace))
    pid = db.scalar("SELECT id FROM project ORDER BY created_at DESC, rowid DESC LIMIT 1")
    v = db.scalar("SELECT id FROM version WHERE project_id=? ORDER BY version_no DESC LIMIT 1", (pid,))
    bundle = surrogate.load(Path(args.workspace)) if args.candidates > 1 else None
    if args.candidates > 1 and bundle is None:
        print("提示：没有 surrogate.pkl，退回单候选规则路径（先跑 aepcb surrogate）")
    out = agent.iterate(db, pid, v, max_rounds=args.rounds,
                        candidates=args.candidates, surrogate_bundle=bundle)
    rank = "代理粗排" if bundle else "规则"
    print(f"Agent {out['score_before']} → {out['score_after']}，{out['rounds']} 轮，"
          f"{out['status']}（{rank}，{args.candidates} 候选）")
    rep = agent.replay(db, out["plan_id"], v)
    print(f"重放一致：{rep['matched']}（{rep['score']}）")
    db.close()


def cmd_sweep(args) -> None:
    from . import sweep
    widths = tuple(float(x) for x in args.widths.split(",") if x.strip())
    out = sweep.run(Path(args.workspace), widths=widths, net_name=args.net or None)
    print(f"扫描 {out['routes']} 条走线 × {len(widths)} 个宽度（{out['mode']}）")
    for r in out["results"]:
        pred = f"，代理 {r['pred']}" if r["pred"] is not None else ""
        print(f"  {r['width_mm']:.2f} mm → {r['score']:.0f} 分（{r['issues']} 个问题）{pred}")
    print(f"规则最优：{out['best_exact']['width_mm']:.2f} mm / {out['best_exact']['score']:.0f} 分")
    if "best_surrogate" in out:
        print(f"代理粗排：{out['best_surrogate']['width_mm']:.2f} mm，"
              f"与规则一致={out['agree']}")
        print(f"耗时：穷举 {out['ms_exact']} ms → 粗排+确认 {out['ms_surrogate']} ms"
              f"（{out['speedup']}×）")
    else:
        print(f"耗时：穷举 {out['ms_exact']} ms（无模型，未启用粗排）")


def cmd_surrogate(args) -> None:
    from . import surrogate
    if getattr(args, "cross_board", False):
        out = surrogate.run_cross_board(Path(args.workspace), boards=args.boards,
                                        groups=args.groups, per_group=args.per_group)
        print(f"跨板验证：{out['boards']} 块板 / {out['n']} 样本")
        print(f"同板内（上界）ρ {out['in_sample_spearman']}；"
              f"跨板平均 ρ {out['mean_lobo_spearman']}，Top-1 {out['mean_lobo_top1']}")
        for p in out["per_board"]:
            print(f"  留出板 {p['board']}：n={p['n']} ρ={p['spearman']} "
                  f"MAE={p['mae']} Top-1={p['top1']}")
        print(f"报告：{out['report_path']}")
        return
    rep = surrogate.run(Path(args.workspace), groups=args.groups, per_group=args.per_group)
    a = rep["group_split"]
    print(f"样本 {rep['n']}（训练 {rep['train']} / 留出 {rep['test']}），"
          f"分数区间 {a['score_range']}，分布 {a['score_bands']}")
    print(f"分组留出：ρ {a['spearman']}，MAE {a['mae']}，Top-1 {a['top1_regressor']}"
          f"（成对 {a['top1_pairwise']}）")
    print(f"延迟：规则 {a['e2e_rule_ms']} ms → 代理 {a['e2e_surrogate_ms']} ms"
          f"（纯评分 {a['speedup']}×，端到端 {a['speedup_e2e']}×）")
    print(f"报告：{rep['report_path']}")


def cmd_pitch(args) -> None:
    """比赛一键：清库 → 跑完整验收（含全链）→ 出报告与证据包。"""
    ws = Path(args.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = ws / f"app.db{suffix}"
        if p.exists():
            p.unlink()
    db = _open(ws)
    results = accept_mod.run_all(db)
    s = accept_mod.summary(results)
    pid = db.scalar("SELECT id FROM project ORDER BY created_at DESC, rowid DESC LIMIT 1")
    rep = report.build(db, pid, ws / "report.html")
    ev = ws / "evidence.md"
    sections = [__import__("re").sub(r"^\d+\.\s*", "", ln[3:].strip())
                for ln in ev.read_text(encoding="utf-8").splitlines()
                if ln.startswith("## ")]
    print(f"验收：{s['passed']}/{s['total']} 通过，P0 失败 {s['p0_fail'] or '无'} → {s['gate']}")
    print(f"报告：{rep}")
    print(f"证据：{ev}（{len(sections)} 节 / {max(1, ev.stat().st_size // 1024)} KB）")
    for i, name in enumerate(sections, 1):
        print(f"  {i}. {name}")
    db.close()


def cmd_evidence(args) -> None:
    from . import evidence
    db = _open(Path(args.workspace))
    pid = db.scalar("SELECT id FROM project ORDER BY created_at DESC, rowid DESC LIMIT 1")
    out = evidence.write(db, pid, args.out)
    print(f"证据包：{out['bytes'] // 1024} KB -> {out['markdown']}（+ JSON）")
    db.close()


def cmd_package(args) -> None:
    from . import package
    db = _open(Path(args.workspace))
    pid = db.scalar("SELECT id FROM project ORDER BY created_at DESC, rowid DESC LIMIT 1")
    out = package.export_project(db, pid, args.out)
    print(f"工程包 {out['bytes'] // 1024} KB，{out['blobs']} 个 blob -> {out['path']}")
    db.close()


def cmd_restore(args) -> None:
    from . import package
    ws = Path(args.workspace)
    db = _open(ws)
    out = package.import_package(db, args.file)
    print(f"导入完成：{out['counts'].get('route', 0)} 条走线，"
          f"{out['counts'].get('project', 0)} 个工程")
    db.close()


def cmd_demo(args) -> None:
    from fixtures import t19a as fx

    ws = Path(args.workspace)
    db = _open(ws)
    pid, v0 = _project(db, "T19-A 演示工程", 60, 40)
    sid, model = board.import_step(db, pid, step.make_fixture_step(60, 40, 1.2), step)
    print(f"[导入] STEP {model.schema} 单位 {model.units} 尺寸 {model.size} 面 {len(model.faces)}")
    res = board.import_board(db, pid, sid, fx.t19a())
    print(f"[导入] 板文件 {res.counts}")
    routing.validate_all(db, res.circuit_id)
    conn = routing.connectivity(db, pid, v0, res.circuit_id)
    print(f"[走线] 连通性 {conn['result']}（断路 {conn['opens']} / 短路 {conn['shorts']}）")
    base = mrc.ensure_baseline(db)
    run = mrc.run(db, pid, v0)
    print(f"[检查] 分数 {run['score']}，问题 {len(run['issues'])}："
          f"{sorted({i['rule_code'] for i in run['issues']})}")
    sl = slicing.run(db, pid, v0, base["process_profile_id"], base["material_batch_id"])
    print(f"[切片] {sl['passes']} 道次 {sl['paths']} 段 {sl['length_mm']} mm 厚 {sl['thickness_mm']} mm")
    g1 = gcode.build(db, sl["slice_id"])
    g2 = gcode.build(db, sl["slice_id"])
    print(f"[导出] content={g1['content_hash'][:16]} 一致={g1['content_hash'] == g2['content_hash']} "
          f"file 不同={g1['file_hash'] != g2['file_hash']}")
    demo = printer.run_demo(db, pid, sl["slice_id"], g1["export_id"], fault_layer=2)
    print(f"[打印] 作业 {demo['job_id'][:8]} 结果 {demo['replay']['result']} "
          f"缺陷 {demo['defect_id'][:8]} 归因 {demo['attribution_id'][:8]}")
    ag = agent.iterate(db, pid, v0)
    print(f"[Agent] {ag['score_before']} → {ag['score_after']}，{ag['rounds']} 轮，{ag['status']}")
    rp = agent.replay(db, ag["plan_id"], v0)
    print(f"[重放] 一致={rp['matched']}（分数 {rp['score']}）")
    out = report.build(db, pid, ws / "report.html")
    print(f"[报告] {out}")
    db.close()


def cmd_accept(args) -> None:
    if getattr(args, "list", False):
        for tid, (level, desc) in accept_mod.TASKS.items():
            print(f"{tid}  {level}  {desc}")
        print(f"\n共 {len(accept_mod.TASKS)} 项；P0 "
              f"{sum(1 for lv, _ in accept_mod.TASKS.values() if lv == 'P0')} 项")
        return
    db = _open(Path(args.workspace))
    results = accept_mod.run_all(db)
    s = accept_mod.summary(results)
    for r in results:
        print(f"  {r['task']} {'✅' if r['passed'] else '❌'} {r['note'][:60]}")
    print(f"\n验收：{s['passed']}/{s['total']} 通过，P0 失败 {s['p0_fail'] or '无'} → {s['gate']}")
    db.close()
    sys.exit(0 if s["gate"] == "PASS" else 1)


def cmd_check(args) -> None:
    db = _open(Path(args.workspace))
    pid = db.scalar("SELECT id FROM project ORDER BY created_at DESC LIMIT 1")
    v = db.scalar("SELECT id FROM version WHERE project_id=? ORDER BY version_no DESC LIMIT 1", (pid,))
    out = mrc.run(db, pid, v)
    print(json.dumps({"score": out["score"], "issues": [
        {"rule": i["rule_code"], "severity": i["severity"], "msg": i["message"]}
        for i in out["issues"]]}, ensure_ascii=False, indent=2))
    db.close()


def cmd_report(args) -> None:
    ws = Path(args.workspace)
    db = _open(ws)
    pid = db.scalar("SELECT id FROM project ORDER BY created_at DESC LIMIT 1")
    print(report.build(db, pid, ws / "report.html"))
    db.close()


def cmd_serve(args) -> None:
    from .web import serve
    serve(Path(args.workspace), port=args.port)


def cmd_kicad(args) -> None:
    ws = Path(args.workspace)
    db = _open(ws)
    pid, v0 = _project(db, "KiCad 导入", 60, 40)
    spec = kicad.parse_file(args.file)
    sid, _ = board.import_step(db, pid, step.make_fixture_step(
        spec["board"]["w"], spec["board"]["h"], 1.2), step)
    res = board.import_board(db, pid, sid, spec)
    print(f"导入 {args.file}：{res.counts}")
    conn = routing.connectivity(db, pid, v0, res.circuit_id)
    print(f"连通性 {conn['result']}（断路 {conn['opens']} / 短路 {conn['shorts']}）")
    print(f"MRC {mrc.run(db, pid, v0)['score']}")
    db.close()


def cmd_rules(args) -> None:
    db = _open(Path(args.workspace))
    if args.set:
        changes = {}
        for item in args.set:
            if "=" not in item:
                raise SystemExit(f"参数应为 R2.min_spacing_mm=0.9，得到 {item}")
            k, v = item.split("=", 1)
            changes[k.strip()] = float(v)
        out = mrc.create_rule_version(db, args.label, changes, args.note or "")
        print(f"已创建规则版本 {out['label']}（{out['thresholds']} 个阈值，覆盖 {len(changes)} 个）")
    for v in mrc.list_rule_versions(db):
        mark = "●" if v["is_active"] else "○"
        print(f"{mark} {v['label']:12} {v['id'][:8]}  {v['created_at']}")
    db.close()


def cmd_doctor(args) -> None:
    from . import doctor
    checks = doctor.run(Path(args.workspace))
    ok, total, bad = doctor.summary(checks)
    for c in checks:
        mark = "✅" if c["ok"] else ("❌" if c["required"] else "⚪")
        hint = f"  → {c['hint']}" if (not c["ok"] and c.get("hint")) else ""
        print(f"{mark} {c['name']:10} {c['detail']}{hint}")
    print(f"\n{ok}/{total} 通过，必需项失败 {bad} 个")
    sys.exit(0 if bad == 0 else 1)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aepcb", description="增材电子一体化软件")
    ap.add_argument("--workspace", default=str(WS))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("demo").set_defaults(func=cmd_demo)
    np = sub.add_parser("new")
    np.add_argument("--name", default="新工程")
    np.add_argument("--w", type=float, default=60.0)
    np.add_argument("--h", type=float, default=40.0)
    np.set_defaults(func=cmd_new)
    ip = sub.add_parser("import")
    ip.add_argument("--step", required=True)
    ip.add_argument("--board", default="")
    ip.set_defaults(func=cmd_import)
    sub.add_parser("slice").set_defaults(func=cmd_slice)
    sub.add_parser("export").set_defaults(func=cmd_export)
    pp = sub.add_parser("print")
    pp.add_argument("--fault-layer", type=int, default=2)
    pp.set_defaults(func=cmd_print)
    ag = sub.add_parser("agent")
    ag.add_argument("--rounds", type=int, default=8)
    ag.add_argument("--candidates", type=int, default=1,
                    help="每轮候选数；>1 时用代理模型粗排（需先训练 surrogate）")
    ag.set_defaults(func=cmd_agent)
    pk = sub.add_parser("package")
    pk.add_argument("--out", default="workspace/project.aepkg")
    pk.set_defaults(func=cmd_package)
    ev = sub.add_parser("evidence")
    ev.add_argument("--out", default="workspace/evidence.md")
    ev.set_defaults(func=cmd_evidence)
    sub.add_parser("pitch").set_defaults(func=cmd_pitch)
    sg = sub.add_parser("surrogate")
    sg.add_argument("--groups", type=int, default=60)
    sg.add_argument("--per-group", type=int, default=6)
    sg.add_argument("--cross-board", action="store_true")
    sg.add_argument("--boards", type=int, default=3)
    sg.set_defaults(func=cmd_surrogate)
    sw = sub.add_parser("sweep")
    sw.add_argument("--widths", default="0.5,0.6,0.7,0.8")
    sw.add_argument("--net", default="")
    sw.set_defaults(func=cmd_sweep)
    rs = sub.add_parser("restore")
    rs.add_argument("file")
    rs.set_defaults(func=cmd_restore)
    rl = sub.add_parser("rules")
    rl.add_argument("--set", nargs="*", default=[], help="R2.min_spacing_mm=0.9")
    rl.add_argument("--label", default="v-next")
    rl.add_argument("--note", default="")
    rl.set_defaults(func=cmd_rules)
    ac = sub.add_parser("accept")
    ac.add_argument("--list", action="store_true", help="只列出验收项与级别，不执行")
    ac.set_defaults(func=cmd_accept)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("report").set_defaults(func=cmd_report)
    sp = sub.add_parser("serve")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_serve)
    kp = sub.add_parser("kicad")
    kp.add_argument("file")
    kp.set_defaults(func=cmd_kicad)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
