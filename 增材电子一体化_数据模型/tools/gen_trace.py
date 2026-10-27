#!/usr/bin/env python3
"""生成需求追溯矩阵：SRS 需求编号 ↔ 表/字段，并显式登记无表承接的条目。

用法：python3 tools/gen_trace.py
输出：04_追溯/需求追溯矩阵.md
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from schema import model  # noqa: E402

SRS = ROOT / "00_inputs" / "SRS_v1.1.md"
OUT = ROOT / "04_追溯" / "需求追溯矩阵.md"

MODULES = [
    ("PRJ", "工程与版本"), ("STR", "结构"), ("CIR", "走线"), ("MRC", "可打性"),
    ("SLC", "切片"), ("PRT", "打印"), ("AGT", "智能体"), ("UI", "界面"),
    ("DAT", "数据与文件"), ("NFR", "非功能"),
]

REQ_RE = re.compile(r"\b(PRJ|STR|CIR|MRC|SLC|PRT|AGT|UI|DAT|NFR)-\d{3}\b")
TASK_RE = re.compile(r"\bT(\d{2})\b")

# 没有物理表承接的条目：状态 + 落点 + 理由（必须逐条解释，不允许空着）
MANUAL: dict[str, tuple[str, str, str]] = {
    "STR-003": ("会话态", "视图状态（不入库）",
                "旋转/缩放/剖切/层显隐属会话态；模型本身在 structure.mesh_blob_hash"),
    "STR-004": ("派生", "structure.mesh_blob_hash（面分析可缓存为 blob）",
                "面类型/法向/面积由网格实时计算，缓存按网格哈希失效即可，不需要实体表"),
    "UI-001": ("会话态", "project / version / mrc_run / device（上下文来源）",
               "布局是渲染状态；顶栏显示的工程、版本、分数、设备分别取自这四张表"),
    "UI-002": ("会话态", "选中对象 ID（指向 circuit.route 等）",
               "视图模式与选取不入库，避免每次点击都写盘"),
    "UI-003": ("会话态", "选中/悬停对象 ID",
               "属性面板内容是查询结果，不是持久对象"),
    "UI-004": ("会话态", "各域表（面板数据源）",
               "面板的空态/错态由查询结果决定，无需单独存储"),
    "UI-006": ("落库（弱）", "app_setting(setting_key='ui.shortcuts')",
               "快捷键映射可配置，作为一条 JSON 设置即可"),
    "UI-007": ("会话态", "diagnostic（错误原因）",
               "空态/断网引导是界面分支，原因文本来自 dictionary_entry"),
    "UI-009": ("非数据", "accept_result（性能验收记录）",
               "50 ms / 120 ms / 30 fps 是验收指标，测量结果落 accept_result"),
    "NFR-001": ("非数据", "accept_result(duration_s)",
                "性能指标属验收测量，不作为业务实体"),
    "NFR-003": ("非数据", "—", "易用性靠 3 名新用户测试，无可持久化对象"),
    "NFR-004": ("非数据", "—", "兼容性属安装包与环境声明"),
    "NFR-007": ("非数据", "plan.degraded / cloud_call.status='skipped'",
                "离线能力矩阵是文档；落库证据是降级运行记录"),
    "NFR-005": ("落库", "external_file.path 的 CHECK + app_setting(setting_key='security.*')",
                "路径穿越由 CHECK 直接拒绕；上传 200 MB 上限与会话令牌是运行时策略，令牌不落库（DAT-007）"),
}

CONFLICTS = [
    ("C1", "PRJ-005 / DAT-002", "撤销到底产不产版本：正文定为“不产”，DAT-002 验收要点仍写“撤销通过新版本实现”",
     "以 PRJ-005 为准：version 只在提交时产生，撤销写 operation"),
    ("C2", "1.7 / SLC-001 / SLC-012", "层厚相加 0.44 mm，目标总厚写 0.35–0.40 mm",
     "层厚为唯一真值；total_thickness_mm 只记录并告警，不判错"),
    ("C3", "PRJ-002 / PRJ-003", "“恢复到关闭前状态”与“恢复到最近一次成功版本”不一致",
     "工作副本（operation + snapshot）与 version 分离，恢复时二选一"),
    ("C4", "T19 / 7.2", "称“T19-A 文件哈希随本文入库”，正文与附件均无该哈希",
     "accept_result.input_blob_hash 预留；交付前必须补 T19-A 与其哈希"),
    ("C5", "PRT-009 / SLC-010", "缺陷归因到设计版本，作业却绑定切片，中间无直接环",
     "defect 同时固化 print_job/slice_set/design_version，触发器校验一致"),
    ("C6", "DAT-004 / PRJ-006 / 10.2", "要求内容寻址，工时说明又写“不做内容寻址”",
     "比赛版做哈希去重 + 引用计数；工程包一律内嵌 blob"),
    ("C7", "MRC-011 / T12 / 10.2", "规则版本是 P1 且被后置，T12 又要求阈值可配置",
     "规则版本表进 P0 最小范围，只把“多版本切换 UI”留作 P1"),
    ("C8", "MRC-011 / DAT-002", "“旧分数标注规则版本”与“版本不可篡改”冲突",
     "分数下沉到 mrc_run 只追加；version 上只留快照列"),
    ("C9", "4.1 / 3.4", "“规则”和“规则版本”都声称持有阈值，默认阈值表无键",
     "阈值只属于 mrc_rule_version 下的明细行"),
    ("C10", "STR-005 / STR-009 / T05", "布尔只预览不执行，却要求“共面布尔失败可回滚”",
     "失败回滚按通用事务语义实现，不必开放布尔执行"),
    ("C11", "AGT-008 / AGT-009 / 4.1", "“工具”一词混用：工具几何 / 工具版本 / 命令能力",
     "拆成 tool_geometry、tool_registry、tool_version 三处，命名不复用"),
    ("C12", "SLC-008 / SLC-013 / T16", "批次号何时写入 G-code 未定；换批次会让“同输入哈希一致”失效",
     "批次在切片提交时冻结进 deposit_pass，导出只读冻结值"),
    ("C13", "9-3 / PRT-008 / PRT-009", "待确认事项仍把“打印作业/缺陷完整记录”列为未决，但它们是 P0",
     "按 P0 处理，9-3 标记为已决"),
    ("C14", "1.6-6 / DAT-002 / AGT-011", "单用户软件却要记“作者”，无用户实体",
     "只建 author_kind（human/agent/system）+ author_name，不建用户表"),
    ("C15", "CIR-004 / CIR-008 / 4.1", "走线被定义为“三维路径”，首版却是二维布线 + 预留层号",
     "存二维几何 + layer_no/side，z 由层与工艺推导，保留 route.z_mm 覆盖"),
    ("C16", "DAT-001 / 4.1", "DAT-001 列 21 类对象，4.1 有 25 行，二者不一致",
     "以 4.1 为权威，DAT-001 改为引用 4.1 + 本文附录"),
]

NEW_TABLES = [
    ("operation", "PRJ-005 撤销栈与操作日志；没有它“撤销动作可追溯”无法举证"),
    ("version_item", "PRJ-004 要看“改了哪些对象”，版本头只有一个分数"),
    ("snapshot", "PRJ-003 异常恢复到最近成功点；PRJ-002 快照缺失时重建"),
    ("blob / blob_gc_log", "DAT-004 内容寻址；DAT-005 哈希校验；数据规则 4 的删除约束"),
    ("external_file", "DAT-005 “外部改动后提示不一致”针对的是用户文件，不是 blob"),
    ("board_outline", "STR-007 板框来源（自动/手绘）与有效性；无板框禁止切片"),
    ("bool_candidate", "STR-005/T03/T04 布尔建议“只预览不执行”，需要独立的候选态"),
    ("layout_candidate", "CIR-005 自动布局候选需排序、可比较、可拒绝"),
    ("keepout", "CIR-006 “被谁挡住”要指到具体遮挡对象"),
    ("layout/nets 相关：net_class", "CIR-010 “线宽改到 0.3 mm 被拒绝并说明下限”需要约束来源"),
    ("connectivity_check", "CIR-009/SLC-007 断路/短路的逐条定位与阻断导出证据"),
    ("mrc_run", "MRC-008/MRC-011 分数只追加；分数不能是 version 的可变列"),
    ("mrc_issue", "MRC-009/010 问题列表要按规则/严重度筛选并定位到对象"),
    ("mrc_rule_version / mrc_rule_threshold", "MRC-011 阈值版本化；旧分数不被覆盖"),
    ("tool_geometry", "MRC-004 可达性必须记录用的是哪套喷嘴几何"),
    ("material_batch", "SLC-008 批次号进 G-code；PRT-009 缺陷可回溯批次"),
    ("process_profile", "SLC-003/011 参数集可保存/加载，并带 params_hash 供重放"),
    ("thermal_step", "SLC-005 时间轴顺序可查；PRT-004 固化中断要重算剩余保温"),
    ("gcode_export", "SLC-008 双哈希（content_hash / file_hash）"),
    ("print_event / print_interruption", "PRT-003/004 两种暂停语义、PRT-005 故障、PRT-011 急停、PRT-014 续打"),
    ("fault_injection", "PRT-007 六类故障可指定层与位置，且要可复现（随机种子）"),
    ("attribution", "PRT-009 归因会复议，塞进 defect 会覆盖历史"),
    ("plan_step / tool_call", "AGT-004/008 步骤与调用可回放；重放不重新推理"),
    ("tool_registry", "AGT-009 人工等效要能抽查“工具 → 界面动作”"),
    ("diff_card", "AGT-005 对比卡片与“应用/拒绝/只应用一步”的留痕"),
    ("replay_run", "AGT-008/T20 重放一致率 100% 需要证据行"),
    ("dictionary_entry", "UI-005/008/010 错误码与术语的中文解释、离线检索"),
    ("accept_result", "7.2 每条验收任务要记录初始分/收敛分/轮数/哈希"),
    ("package_manifest", "PRJ-006 工程包跨机导入的清单与冲突"),
    ("cloud_call", "AGT-012 云端调用记录；比赛版需保留真实调用证据"),
    ("migration_log", "DAT-006 迁移前备份、旧工程可打开"),
]


def parse_srs() -> tuple[dict[str, dict], set[str]]:
    reqs: dict[str, dict] = {}
    tasks: set[str] = set()
    for line in SRS.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        m = REQ_RE.fullmatch(cells[0])
        if m and cells[0] not in reqs:
            prio = next((c for c in cells[1:] if c in ("P0", "P1", "P2")), "—")
            reqs[cells[0]] = {
                "text": cells[1] if len(cells) > 1 else "",
                "prio": prio,
                "accept": cells[3] if len(cells) > 3 else (cells[2] if len(cells) > 2 else ""),
            }
            continue
        tm = re.fullmatch(r"T\d{2}", cells[0])
        if tm:
            tasks.add(cells[0])
    return reqs, tasks


def status_of(rid: str, tables: list[str]) -> tuple[str, str, str]:
    if rid in MANUAL:
        return MANUAL[rid]
    if tables:
        return ("落库", "、".join(f"`{t}`" for t in tables), "")
    return ("⚠ 缺口", "—", "未找到承接表，需要在下次迭代补")


def main() -> None:
    reqs, tasks = parse_srs()
    index = model.requirement_index()
    rows, missing = [], []
    per_module = defaultdict(lambda: defaultdict(int))
    for rid, meta in sorted(reqs.items()):
        module = rid.split("-")[0]
        tables = index.get(rid, [])
        st, where, why = status_of(rid, tables)
        per_module[module][st] += 1
        if st.startswith("⚠"):
            missing.append(rid)
        rows.append((module, rid, meta, st, where, why))

    out = ["# 需求追溯矩阵（SRS v1.1 → 数据模型 v1.0）", "",
           "| 项 | 内容 |", "|---|---|",
           f"| 需求条目 | {len(reqs)} 条（SRS 第 3 章 + 第 6 章）|",
           f"| 验收任务 | {len(tasks)} 条（T01–T20）|",
           f"| 承载表 | {model.stats()['tables']} 张、{model.stats()['columns']} 字段 |",
           f"| 缺口 | {len(missing)} 条{('：' + '、'.join(missing)) if missing else ''} |",
           "| 生成方式 | `tools/gen_trace.py` 解析 SRS 编号 + `schema/*.py` 的需求标注，勿手改 |", "",
           "## 0. 怎么读", "",
           "- **落库**：有明确的表/字段承接，可直接建库验证；",
           "- **会话态**：只在运行时存在（选中、视图、进度），刻意不落盘；",
           "- **派生**：由已存数据实时算出（飞线、面分析），避免两份真相；",
           "- **非数据**：性能、兼容性、易用性等验收条款；",
           "- **⚠ 缺口**：既无表承接也未解释 —— 本文件必须为 0 条。", "",
           "## 1. 覆盖总览", "",
           "| 模块 | 需求数 | 落库 | 会话态 | 派生 | 非数据 | 缺口 |", "|---|---|---|---|---|---|---|"]
    for code, cn in MODULES:
        d = per_module[code]
        total = sum(d.values())
        out.append(f"| {cn}（{code}）| {total} | {d['落库']} | {d['会话态']} | {d['派生']} | {d['非数据']} | {d['⚠ 缺口']} |")
    total_all = sum(sum(d.values()) for d in per_module.values())
    out.append(f"| **合计** | **{total_all}** | "
               f"**{sum(d['落库'] for d in per_module.values())}** | "
               f"**{sum(d['会话态'] for d in per_module.values())}** | "
               f"**{sum(d['派生'] for d in per_module.values())}** | "
               f"**{sum(d['非数据'] for d in per_module.values())}** | "
               f"**{len(missing)}** |")

    for code, cn in MODULES:
        out += ["", f"## 2.{MODULES.index((code, cn)) + 1} {cn}（{code}）", "",
                "| 编号 | 需求要点 | 优先级 | 状态 | 落点 | 说明 |",
                "|---|---|---|---|---|---|"]
        for module, rid, meta, st, where, why in rows:
            if module != code:
                continue
            text = meta["text"].replace("|", "\\|")
            if len(text) > 90:
                text = text[:90] + "…"
            out.append(f"| `{rid}` | {text} | {meta['prio']} | {st} | {where} | {why.replace('|', '/')} |")

    out += ["", "## 3. 验收任务 T01–T20", "",
            "全部落在 `accept_result`（输入哈希、通过与否、轮数、耗时、初始分/收敛分、G-code 哈希、诊断码），",
            "使 7.2 的“每个任务输出结果记录”可被 SQL 直接核对，而不是靠人写报告。", "",
            "| 任务 | 域 | 留证字段 |", "|---|---|---|"]
    dom = {"01": "结构", "02": "结构", "03": "结构", "04": "结构", "05": "结构",
           "06": "走线", "07": "走线", "08": "走线", "09": "走线",
           "10": "MRC", "11": "MRC", "12": "MRC",
           "13": "切片", "14": "切片", "15": "切片", "16": "切片",
           "17": "打印", "18": "打印", "19": "智能体", "20": "全链"}
    for t in sorted(tasks):
        out.append(f"| `{t}` | {dom.get(t[1:], '—')} | input_blob_hash / passed / rounds / duration_s / "
                   f"score_initial / score_final / gcode_hash / diagnostic_code |")

    out += ["", "## 4. SRS 第 4.1 节实体 → 表", "",
            "| SRS 实体 | 落点 | 差异说明 |", "|---|---|---|"]
    ent = [
        ("工程", "`project`", "补幅面、原点、数据模式版本、回收站"),
        ("会话", "`edit_session`", "改名以避开 SQL 关键字"),
        ("版本", "`version` + `version_item`", "头 + 对象级差异，支持“改了哪些对象”"),
        ("计划", "`plan` + `plan_step` + `tool_call`", "拆三层才能重放"),
        ("结构", "`structure`", "补网格 blob、容差、包围盒"),
        ("特征", "`feature` + `bool_candidate`", "已接受与待处置分开"),
        ("电路", "`circuit`", "补源文件指纹与计数"),
        ("器件", "`component`", "位姿、锁定、所在面"),
        ("焊盘", "`pad`", "net_id 可为空（纯网表）"),
        ("网络", "`net` + `net_class`", "网络与约束组分离"),
        ("走线", "`route`", "二维几何 + 层号；z 由层推导"),
        ("跳线", "`jumper`", "候选/已应用"),
        ("切片", "`slice_set`", "绑定设计版本、显式过期"),
        ("工艺参数", "`process_profile`", "带 params_hash"),
        ("材料", "`material` + `material_batch`", "配方与批次分离"),
        ("层/道次", "`physical_layer` + `deposit_pass`", "1:N，同高度可两道次"),
        ("路径", "`slice_path`", "整型行号主键，几何进 CAS"),
        ("打印作业", "`print_job` + `print_event`", "status 与 phase 双字段"),
        ("设备", "`device` + `device_calibration`", "标定是事件"),
        ("缺陷", "`defect` + `attribution`", "三元绑定 + 可复议归因"),
        ("规则", "`mrc_rule_version` + `mrc_rule_threshold`", "阈值只属于版本"),
        ("规则版本", "`mrc_rule_version`", "只追加，is_active 切换"),
        ("工具几何", "`tool_geometry`", "行长即版本"),
        ("材料批次", "`material_batch`", "批次号进 G-code"),
        ("问题", "`mrc_issue`", "冗余 version/rule_version 换热索引"),
    ]
    for a, b, c in ent:
        out.append(f"| {a} | {b} | {c} |")

    out += ["", "## 5. 新增表及依据（SRS 4.1 未列）", "",
            "| 表 | 为什么必须建 |", "|---|---|"]
    for t, why in NEW_TABLES:
        out.append(f"| {t} | {why} |")

    out += ["", "## 6. 冲突与缺口登记", "",
            "SRS v1.1 内部有 16 处需要裁定的地方。逐条登记，避免验收时被追问“到底按哪个”。", "",
            "| # | 涉及编号 | 冲突/缺口 | 本文裁定 |", "|---|---|---|---|"]
    for cid, ids, issue, decision in CONFLICTS:
        out.append(f"| {cid} | {ids} | {issue} | {decision} |")
    out += ["", "> C4 属**未关闭缺口**：T19-A 固定验收板的文件与哈希必须在提交前补齐，"
            "否则 T19“分数提升可复现”无法验证。其余 15 条已在模型中落地。", ""]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"追溯矩阵 -> {OUT}（{len(out)} 行，需求 {len(reqs)} 条，缺口 {len(missing)}）")
    if missing:
        print("⚠ 缺口：", missing)


if __name__ == "__main__":
    main()
