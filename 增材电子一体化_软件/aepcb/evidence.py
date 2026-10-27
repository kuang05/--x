"""验收证据包（SRS 7.2）：把一次工程的关键事实汇总成可提交的 Markdown + JSON。

不是"再写一份报告"，而是把库里已有的证据串起来：
    工程/版本 → 规则版本与阈值 → 评分历史与问题分布 → 切片与 G-code 双哈希
    → Agent 计划/步骤/对比卡片 → 连通性检查 → T01–T20 验收结果
"""
from __future__ import annotations

import json
from pathlib import Path

from .store import DB, now


def collect(db: DB, project_id: str) -> dict:
    project = db.one("SELECT * FROM project WHERE id=?", (project_id,))
    if not project:
        raise ValueError("工程不存在")
    version = db.one("SELECT * FROM version WHERE project_id=? ORDER BY version_no DESC LIMIT 1",
                     (project_id,))
    rules = [dict(r) for r in db.q("SELECT * FROM mrc_rule_version ORDER BY created_at DESC, rowid DESC")]
    active = next((r for r in rules if r["is_active"]), None)
    thresholds = []
    if active:
        thresholds = [dict(r) for r in db.q(
            "SELECT rule_code,param_key,value_num,unit FROM mrc_rule_threshold "
            "WHERE rule_version_id=? ORDER BY rule_code,param_key", (active["id"],))]
    runs = [dict(r) for r in db.q(
        "SELECT id,total_score,issue_count,rule_version_id,created_at FROM mrc_run "
        "WHERE project_id=? ORDER BY created_at, rowid", (project_id,))]
    issues = [dict(r) for r in db.q(
        "SELECT m.rule_code, m.severity, COUNT(*) AS n FROM mrc_issue m "
        "JOIN mrc_run r ON r.id=m.run_id WHERE r.project_id=? GROUP BY m.rule_code, m.severity "
        "ORDER BY m.rule_code", (project_id,))]
    slice_set = db.one("SELECT * FROM slice_set WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                       (project_id,))
    exports = [dict(r) for r in db.q(
        "SELECT content_hash,file_hash,byte_size,created_at FROM gcode_export WHERE slice_set_id=? "
        "ORDER BY created_at", (slice_set["id"],))] if slice_set else []
    conn = [dict(r) for r in db.q(
        "SELECT scope,result,open_count,short_count,ran_at FROM connectivity_check "
        "WHERE project_id=? ORDER BY ran_at", (project_id,))]
    plan = db.one("SELECT * FROM plan WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                  (project_id,))
    steps = [dict(r) for r in db.q(
        "SELECT step_no,intent,tool_name,tool_version,status FROM plan_step WHERE plan_id=? "
        "ORDER BY step_no", (plan["id"],))] if plan else []
    card = db.one("SELECT * FROM diff_card WHERE plan_id=? ORDER BY created_at DESC LIMIT 1",
                  (plan["id"],)) if plan else None
    accept = [dict(r) for r in db.q(
        "SELECT task_no,passed,rounds,score_initial,score_final,gcode_hash,notes "
        "FROM accept_result WHERE project_id=? ORDER BY task_no", (project_id,))]
    return {"generated_at": now(), "project": dict(project),
            "version": dict(version) if version else None,
            "rule": {"active": active, "versions": len(rules), "thresholds": thresholds},
            "runs": runs, "issues": issues,
            "slice": dict(slice_set) if slice_set else None,
            "exports": exports, "connectivity": conn,
            "plan": dict(plan) if plan else None, "steps": steps,
            "card": dict(card) if card else None, "accept": accept}


def _runtime() -> list[str]:
    """运行环境：性能与可复现结论都需要机器口径，不能只给数字。"""
    import os
    import platform
    import sqlite3
    from importlib import metadata

    cpu = "unknown"
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    deps = {}
    # OpenCV 的发行包名有两三种（headless / 完整版），逐个试；都没有再用模块版本兜底
    for label, names in (("numpy", ("numpy",)), ("scipy", ("scipy",)),
                         ("opencv", ("opencv-python-headless", "opencv-python")),
                         ("pillow", ("pillow",)), ("scikit-learn", ("scikit-learn",)),
                         ("pyserial", ("pyserial",))):
        for name in names:
            try:
                deps[label] = metadata.version(name)
                break
            except Exception:
                continue
        else:
            deps[label] = "未安装"
    if deps.get("opencv") == "未安装":
        try:
            import cv2
            deps["opencv"] = getattr(cv2, "__version__", "已安装")
        except Exception:
            pass
    return [f"- CPU：{cpu} / {os.cpu_count()} 核",
            f"- 平台：{platform.platform()}",
            f"- Python：{platform.python_version()}",
            f"- SQLite：{sqlite3.sqlite_version}",
            f"- 依赖：{'，'.join(f'{k} {v}' for k, v in deps.items())}"]


def _bullets(path: Path, limit: int = 8) -> list[str]:
    """从指标报告里取要点行（以“- ”开头），供证据包引用。"""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("- "):
            out.append(line)
        if len(out) >= limit:
            break
    return out


def _table(path: Path, limit: int = 5) -> list[str]:
    """从报告里取 Markdown 表格行（表头+分隔+数据）。"""
    if not path.exists():
        return []
    rows = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("| ")]
    return rows[:limit]


def render(ev: dict) -> str:
    p, v = ev["project"], ev["version"]
    lines = [f"# 验收证据包 · {p['name']}", "",
             f"- 生成时间：{ev['generated_at']}",
             f"- 工程：{p['name']}（{p['sheet_w_mm']}×{p['sheet_h_mm']} mm，单位 {p['unit']}）",
             f"- 当前版本：v{v['version_no'] if v else '—'}",
             f"- 数据模式版本：{p['data_schema_version']}", ""]

    lines += ["## 0. 运行环境", ""] + ev["runtime"] + [""] if ev.get("runtime") else []
    lines += ["## 1. 可打性", ""]
    rule = ev["rule"]["active"]
    lines.append(f"规则版本：**{rule['label'] if rule else '—'}**（共 {ev['rule']['versions']} 个版本）")
    lines.append("")
    if ev["rule"]["thresholds"]:
        lines += ["| 规则 | 参数 | 阈值 | 单位 |", "|---|---|---|---|"]
        for t in ev["rule"]["thresholds"]:
            lines.append(f"| {t['rule_code']} | {t['param_key']} | {t['value_num']} | {t['unit']} |")
        lines.append("")
    if ev["runs"]:
        scores = [r["total_score"] for r in ev["runs"]]
        lines.append(f"评分历史：{len(scores)} 次，首次 **{scores[0]}**，最新 **{scores[-1]}**，"
                     f"最高 **{max(scores)}**")
        lines.append("")
    if ev["issues"]:
        lines += ["| 规则 | 严重度 | 条数 |", "|---|---|---|"]
        for i in ev["issues"]:
            lines.append(f"| {i['rule_code']} | {i['severity']} | {i['n']} |")
        lines.append("")

    lines += ["## 2. 切片与导出", ""]
    sl = ev["slice"]
    if sl:
        lines.append(f"切片：{sl['layer_count']} 层 / {sl['pass_count']} 道次 / "
                     f"总厚 {sl['total_thickness_mm']} mm / 状态 {sl['status']}")
    else:
        lines.append("未切片")
    lines.append("")
    if ev["exports"]:
        lines += ["| 内容哈希 | 文件哈希 | 字节 | 导出时间 |", "|---|---|---|---|"]
        for e in ev["exports"]:
            lines.append(f"| `{e['content_hash'][:16]}` | `{e['file_hash'][:16]}` | {e['byte_size']} | "
                         f"{e['created_at']} |")
        lines.append("")
        if len(ev["exports"]) >= 2:
            same = len({e["content_hash"] for e in ev["exports"]}) == 1
            lines.append(f"两次导出内容哈希一致：**{same}**（T16 口径）")
            lines.append("")
        else:
            lines.append("导出记录按内容哈希去重：同一内容只记一条（重复导出幂等，字节哈希不同）")
            lines.append("")

    lines += ["## 3. 连通性检查", ""]
    if ev["connectivity"]:
        lines += ["| 范围 | 结果 | 断路 | 短路 | 时间 |", "|---|---|---|---|---|"]
        for c in ev["connectivity"]:
            lines.append(f"| {c['scope']} | {c['result']} | {c['open_count']} | "
                         f"{c['short_count']} | {c['ran_at']} |")
        lines.append("")
    else:
        lines += ["没有连通性记录", ""]

    lines += ["## 4. Agent 计划", ""]
    if ev["plan"]:
        lines.append(f"计划：{ev['plan']['goal']}；状态 **{ev['plan']['status']}**；"
                     f"分数 {ev['plan']['score_before']} → {ev['plan']['score_after']}；"
                     f"{ev['plan']['rounds_used']} 轮")
        lines.append("")
        if ev["steps"]:
            lines += ["| # | 意图 | 工具 | 版本 | 状态 |", "|---|---|---|---|---|"]
            for s in ev["steps"]:
                lines.append(f"| {s['step_no']} | {s['intent']} | {s['tool_name']} | "
                             f"{s['tool_version']} | {s['status']} |")
            lines.append("")
        if ev["card"]:
            c = ev["card"]
            lines.append(f"对比卡片：处置 **{c['decision']}**；{c['score_before']} → "
                         f"{c['score_after']}；理由：{c['reason']}")
            lines.append("")
    else:
        lines += ["没有 Agent 计划", ""]

    lines += ["## 5. 验收结果（T01–T25）", ""]
    if ev["accept"]:
        lines += ["| 任务 | 结果 | 轮数 | 初始分 | 收敛分 | 备注 |", "|---|---|---|---|---|---|"]
        for a in ev["accept"]:
            lines.append(f"| {a['task_no']} | {'✅' if a['passed'] else '❌'} | "
                         f"{a['rounds'] if a['rounds'] is not None else '—'} | "
                         f"{a['score_initial'] if a['score_initial'] is not None else '—'} | "
                         f"{a['score_final'] if a['score_final'] is not None else '—'} | "
                         f"{(a['notes'] or '')[:40]} |")
        passed = sum(1 for a in ev["accept"] if a["passed"])
        lines += ["", f"合计：**{passed}/{len(ev['accept'])}** 通过"]
    else:
        lines += ["没有验收记录"]
    lines.append("")

    lines += ["## 6. UI 性能（UI-009）", ""]
    if ev.get("ui_bench"):
        lines += ev["ui_bench"] + [""]
    else:
        lines += ["未运行 `tools/ui_bench.py`", ""]

    lines += ["## 7. 代理模型（规则蒸馏）", ""]
    if ev.get("surrogate"):
        lines += ev["surrogate"] + [""]
    else:
        lines += ["未训练代理模型（`aepcb surrogate`）", ""]
    return "\n".join(lines)


def write(db: DB, project_id: str, out_path: str | Path) -> dict:
    ev = collect(db, project_id)
    # 指标报告不在库里，直接取 workspace 下两份报告的关键行拼进证据包
    ev["ui_bench"] = _table(db.root / "ui_bench.md", 5)
    ev["surrogate"] = _bullets(db.root / "surrogate_report.md", 6)
    ev["runtime"] = _runtime()
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(ev), encoding="utf-8")
    j = p.with_suffix(".json")
    j.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"markdown": str(p), "json": str(j), "bytes": p.stat().st_size}
