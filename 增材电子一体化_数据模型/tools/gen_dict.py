#!/usr/bin/env python3
"""生成数据字典（设计说明书正文）：schema/preamble.md + 自动生成的逐表字典。

用法：python3 tools/gen_dict.py
输出：02_设计/数据模型设计说明书.md
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from schema import model                                    # noqa: E402
from schema.base import DOMAINS, Table, column_checks       # noqa: E402

OUT = ROOT / "02_设计" / "数据模型设计说明书.md"

TYPE_CN = {"text": "TEXT", "int": "INTEGER", "int64": "INTEGER", "real": "REAL",
           "bool": "INTEGER(0/1)", "json": "TEXT(JSON)", "time": "TEXT(ISO8601)",
           "hash": "TEXT(sha256)", "blob": "BLOB"}


def esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def overview() -> str:
    lines = ["| # | 表名 | 中文 | 域 | 字段 | 主键 | 需求数 | 职责 |",
             "|---|---|---|---|---|---|---|---|"]
    for i, t in enumerate(model.TABLES, 1):
        lines.append(
            f"| {i:02d} | `{t.name}` | {t.cn} | {model.DOMAIN_CN[t.domain]} | {len(t.cols)} | "
            f"`{', '.join(t.pk_cols)}` | {len(t.req)} | {esc(t.desc)} |")
    s = model.stats()
    lines.append(f"| — | **合计** | {model.stats()['tables']} 张表 | 8 个域 | **{s['columns']}** | — | — | "
                 f"{s['indexes']} 索引 / {s['fks']} 外键 / {s['checks']} 约束 / {s['triggers']} 触发器 |")
    return "\n".join(lines)


def column_table(t: Table) -> str:
    lines = ["| 字段 | 类型 | 可空 | 默认 | 约束 | 说明 |",
             "|---|---|---|---|---|---|"]
    for c in t.cols:
        cons = []
        if c.pk:
            cons.append("PK")
        if c.uq:
            cons.append("UNIQUE")
        if c.fk:
            cons.append(f"FK→{c.fk}")
        for expr, _cn in column_checks(c):
            if c.type in ("text", "int", "int64", "real") or expr.endswith(")"):
                cons.append(esc(expr))
        default = c.default if c.default is not None else "—"
        nn = "否" if c.nn else "**是**"
        desc = c.cn + (f"（{c.note}）" if c.note else "")
        lines.append(f"| `{c.name}` | {TYPE_CN[c.type]} | {nn} | {esc(default)} | "
                     f"{esc('; '.join(cons)) or '—'} | {esc(desc)} |")
    return "\n".join(lines)


def table_section(t: Table, num: str) -> str:
    out = [f"#### {num} {t.cn} `{t.name}`", "", f"> {t.desc}", "",
           f"- **依据需求**：{', '.join(t.req)}",
           f"- **主键**：`{', '.join(t.pk_cols)}`"]
    if t.note:
        out.append(f"- **建模说明**：{t.note}")
    out += ["", column_table(t), ""]

    if t.idx:
        out += ["**索引**", "", "| 索引名 | 列 | 唯一 | 条件 | 用途 |", "|---|---|---|---|---|"]
        for i in t.idx:
            name = i.name or f"idx_{t.name}_{'_'.join(i.cols)}"
            out.append(f"| `{name}` | {', '.join(f'`{c}`' for c in i.cols)} | "
                       f"{'是' if i.unique else '否'} | {esc(i.where) or '—'} | {esc(i.note) or '—'} |")
        out.append("")

    if t.chk:
        out += ["**表级约束**", ""]
        for expr, cn in t.chk:
            out.append(f"- `{expr}` —— {cn}")
        out.append("")

    if t.fks():
        out += ["**外键与删除策略**", "", "| 字段 | 指向 | ON DELETE |", "|---|---|---|"]
        for c in t.fks():
            out.append(f"| `{c.name}` | `{c.fk}` | {c.on_delete} |")
        out.append("")

    if t.trg:
        out += ["**触发器**", ""]
        for name, _sql in t.trg:
            out.append(f"- `{name}`")
        out.append("")
    return "\n".join(out)


def dictionary() -> str:
    out = ["---", "", "## 9. 数据字典（逐表）", "",
           "每个表给出：职责、依据需求、主键、字段清单、索引、表级约束、外键删除策略、触发器。",
           "字段的“可空”列加粗表示允许 NULL —— 这不是随手写的：允许为空的字段基本都是"
           "“尚未发生的事实”（结束时间、过期时间、驳回原因），而不是“可以不知道的值”。", ""]
    sec = 1
    for key, cn, desc in DOMAINS:
        tables = [t for t in model.TABLES if t.domain == key]
        out += [f"### 9.{sec} {cn}（{len(tables)} 张表）", "", f"{desc}。", ""]
        for j, t in enumerate(tables, 1):
            out.append(table_section(t, f"9.{sec}.{j}"))
        sec += 1
    return "\n".join(out)


def appendix() -> str:
    s = model.stats()
    out = ["---", "", "## 10. 附录", "", "### 10.1 规模统计", "",
           "| 域 | 表数 | 字段数 | 索引数 | 外键数 |", "|---|---|---|---|---|"]
    for key, cn, _ in DOMAINS:
        ts = [t for t in model.TABLES if t.domain == key]
        out.append(f"| {cn} | {len(ts)} | {sum(len(t.cols) for t in ts)} | "
                   f"{sum(len(t.idx) for t in ts)} | {sum(len(t.fks()) for t in ts)} |")
    out.append(f"| **合计** | **{s['tables']}** | **{s['columns']}** | **{s['indexes']}** | **{s['fks']}** |")

    out += ["", "### 10.2 触发器清单", "", "| 触发器 | 表 | 作用 |", "|---|---|---|"]
    effect = {
        "trg_project_touch": "工程修改后自动更新 updated_at",
        "trg_version_no_update": "禁止修改版本行（DAT-002）",
        "trg_version_no_delete": "禁止删除版本行（DAT-002）",
        "trg_version_item_no_update": "版本变更项只能随版本追加（DAT-002）",
        "trg_version_item_no_delete": "版本变更项不可删除（DAT-002）",
        "trg_operation_append_only": "操作日志不可改写（PRJ-005）",
        "trg_operation_no_delete": "操作日志不可删除（PRJ-005）",
        "trg_mrc_run_append_only": "评分只追加，旧分数不被覆盖（MRC-011）",
        "trg_mrc_run_no_delete": "评分运行不可删除（MRC-011）",
        "trg_issue_no_update": "违规条目不可改写（MRC-011）",
        "trg_issue_no_delete": "违规条目不可删除（MRC-011）",
        "trg_issue_run_consistency": "违规条目必须与所属运行的版本/规则版本一致（MRC-008）",
        "trg_slice_stale_on_version": "新版本产生后，同工程其他 ready 切片置 stale（SLC-010）",
        "trg_slice_ready_guard": "绑定版本非当前版本时禁止置 ready（堵住 computing→ready 的状态机泄漏）",
        "trg_defect_binding": "缺陷的设计版本必须与切片绑定版本一致（PRT-009）",
        "trg_gcode_append_only": "导出记录不可改写（SLC-008）",
        "trg_gcode_no_delete": "导出记录不可删除（SLC-008）",
        "trg_review_step_readonly_ins": "审查 Agent 不得执行步骤（AGT-002）",
        "trg_review_step_readonly_upd": "审查 Agent 不得把步骤改成 applied（AGT-002）",
        "trg_review_card_readonly_ins": "审查 Agent 不得应用修改（AGT-002）",
        "trg_review_card_readonly_upd": "审查 Agent 不得把对比卡片改成 apply（AGT-002）",
    }
    for t in model.TABLES:
        for name, _sql in t.trg:
            out.append(f"| `{name}` | `{t.name}` | {effect.get(name, '—')} |")

    out += ["", "### 10.3 与上游的对应关系", "",
            "| SRS 第 4.1 节实体 | 本文落点 | 说明 |", "|---|---|---|"]
    mapping = [
        ("工程", "`project`", "含幅面、单位、原点、当前版本指针"),
        ("会话", "`edit_session`", "改名以避开 SQL 关键字"),
        ("版本", "`version` + `version_item`", "头 + 对象级差异"),
        ("计划", "`plan` + `plan_step` + `tool_call`", "SRS 只写“计划”，步骤与调用必须拆表才可重放"),
        ("结构", "`structure`", "含网格 blob、容差、包围盒"),
        ("特征", "`feature` + `bool_candidate`", "已接受特征与待处置候选分开"),
        ("电路", "`circuit`", "含源文件指纹与对象计数"),
        ("器件", "`component`", "位姿、锁定、所在面"),
        ("焊盘", "`pad`", "允许 net_id 为空（纯网表场景）"),
        ("网络", "`net` + `net_class`", "网络与约束组分离"),
        ("走线", "`route`", "二维几何 + 层号，z 由层推导"),
        ("跳线", "`jumper`", "候选/已应用两态"),
        ("切片", "`slice_set` + `physical_layer` + `deposit_pass`", "SRS 的“层/道次”一行拆成三张表"),
        ("工艺参数", "`process_profile`", "带 params_hash，供重放比对"),
        ("材料", "`material` + `material_batch`", "配方与批次分离"),
        ("层/道次", "`physical_layer` / `deposit_pass`", "1:N，同高度可两道次"),
        ("路径", "`slice_path`", "整型行号主键，几何进 CAS"),
        ("打印作业", "`print_job` + `print_event`", "状态与阶段双字段"),
        ("设备", "`device` + `device_calibration`", "标定是事件，不是属性"),
        ("缺陷", "`defect` + `attribution`", "三元绑定 + 可复议归因"),
        ("规则", "`mrc_rule_version` + `mrc_rule_threshold`", "阈值只属于版本"),
        ("规则版本", "`mrc_rule_version`", "只追加，is_active 切换"),
        ("工具几何", "`tool_geometry`", "行长即版本"),
        ("材料批次", "`material_batch`", "批次号进 G-code 头"),
        ("问题", "`mrc_issue`", "冗余 version/rule_version 换热索引"),
    ]
    for a, b, c in mapping:
        out.append(f"| {a} | {b} | {c} |")
    out += ["", "另新增 22 张 SRS 未列但验收必需的表（操作日志、快照、外部文件指纹、板框、"
            "布尔候选、禁布区、连通性、热历程、G-code 导出、暂停续打、故障注入、能力注册表、"
            "对比卡片、重放、字典、验收结果、工程包清单等），逐条依据见 `04_追溯/需求追溯矩阵.md`。"]
    return "\n".join(out)


def main() -> None:
    pre = (ROOT / "schema" / "preamble.md").read_text(encoding="utf-8")
    s = model.stats()
    pre = pre.replace("{{STATS}}", f"{s['tables']} 张表、{s['columns']} 个字段、{s['indexes']} 个索引、{s['triggers']} 个触发器")
    pre = pre.replace("{{TABLES}}", str(s['tables']))
    doc = pre.replace("<!--TABLES_OVERVIEW-->", overview())
    doc += "\n\n" + dictionary() + "\n\n" + appendix() + "\n"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8")
    print(f"字典 -> {OUT}（{len(doc.splitlines())} 行）")


if __name__ == "__main__":
    main()
