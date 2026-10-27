"""mrc 域：规则版本、阈值明细、工具几何、评分运行、违规条目。"""
from __future__ import annotations

from .base import C, CREATED, ID, Index, T

TABLES = [
    # ------------------------------------------------------------------ mrc_rule_version
    T("mrc_rule_version", "规则版本", "mrc",
      "一组阈值的快照头：谁在什么时候改了哪套规则。阈值只从属于版本，不放在规则本体上。",
      ["MRC-011", "MRC-008", "3.4 首版默认阈值"],
      [
          ID("规则版本 ID"),
          C("label", "text", "版本标签（如 v1 首版基线）", nn=True, check="length(trim(label))>0"),
          C("note", "text", "变更说明", default="''"),
          C("created_by", "text", "创建者", nn=True, default="'local'"),
          C("supersedes_id", "text", "被替代的上一版", fk="mrc_rule_version.id", on_delete="SET NULL"),
          C("snapshot_hash", "hash", "阈值集合摘要（重放可比性）"),
          C("is_active", "bool", "是否当前生效", nn=True, default="1"),
          CREATED("生效时间"),
      ],
      idx=[
          Index(["is_active"], name="idx_rulever_active", where="is_active=1",
                note="取当前生效规则版本"),
          Index(["supersedes_id"], note="版本链回溯"),
      ],
      chk=[("supersedes_id IS NULL OR supersedes_id <> id", "规则版本不能自我替代")],
      note="新增规则版本 = 插入新行 + 把旧行 is_active 置 0；旧行不修改阈值，保证历史分数可解释（MRC-011）。"),

    # ------------------------------------------------------------------ mrc_rule_threshold
    T("mrc_rule_threshold", "规则阈值明细", "mrc",
      "规则版本下逐条阈值：R1–R7 的判定参数、数值、单位与启用状态。",
      ["MRC-001", "MRC-002", "MRC-003", "MRC-004", "MRC-005", "MRC-006", "MRC-007", "MRC-011"],
      [
          C("rule_version_id", "text", "所属规则版本", nn=True, fk="mrc_rule_version.id", on_delete="CASCADE"),
          C("rule_code", "text", "规则码", nn=True,
            check="rule_code IN ('R1','R2','R3','R4','R5','R6','R7')",
            note="R1 线宽 / R2 间距 / R3 坡度 / R4 可达性 / R5 遮挡 / R6 温度场 / R7 介电流道"),
          C("param_key", "text", "参数名", nn=True, check="length(trim(param_key))>0"),
          C("value_num", "real", "阈值数值", nn=True),
          C("unit", "text", "单位", nn=True, default="'mm'",
            check="unit IN ('mm','deg','c','count','ratio')"),
          C("enabled", "bool", "是否启用", nn=True, default="1"),
          C("note", "text", "说明", default="''"),
      ],
      pk=["rule_version_id", "rule_code", "param_key"],
      idx=[Index(["rule_code"], note="按规则取全部参数")],
      note="首版默认值：R1 0.5 mm、R2 0.5 mm、R3 45°、R4 0.5 mm、R5 无遮挡、R6 5 ℃、R7 0.5 mm。"),

    # ------------------------------------------------------------------ tool_geometry
    T("tool_geometry", "工具几何", "mrc",
      "喷嘴几何与安全间隙；喷嘴一换就是新行，可达性检查必须记录用的是哪一行。",
      ["MRC-004", "1.7 喷头安全间隙", "PRT-013"],
      [
          ID("工具几何 ID"),
          C("name", "text", "名称（如 0.25 针头）", nn=True),
          C("nozzle_inner_d_mm", "real", "针头内径（mm）", nn=True, check="nozzle_inner_d_mm BETWEEN 0.1 AND 1"),
          C("nozzle_outer_d_mm", "real", "针尖外径（mm）", nn=True, check="nozzle_outer_d_mm>=nozzle_inner_d_mm"),
          C("safety_gap_mm", "real", "安全间隙（mm）", nn=True, default="0.5", check="safety_gap_mm>=0"),
          C("envelope_json", "json", "喷头包络（多段圆柱/包围盒）"),
          C("is_active", "bool", "是否在用", nn=True, default="1"),
          C("note", "text", "说明", default="''"),
          CREATED(),
      ],
      idx=[Index(["is_active"], name="idx_toolgeom_active", where="is_active=1", note="当前在用喷嘴")],
      note="工具几何本身就是版本（行长即版本），不另建版本表；mrc_run 固定引用某一行即可保证重放可比（AGT-008）。"),

    # ------------------------------------------------------------------ mrc_run
    T("mrc_run", "评分运行", "mrc",
      "一次可打性检查：分数权威来源。同一版本可被不同规则版本/工具几何重复评，旧结果只追加不改写。",
      ["MRC-008", "MRC-011", "PRJ-004", "T19"],
      [
          ID("运行 ID"),
          C("project_id", "text", "所属工程", nn=True, fk="project.id", on_delete="CASCADE"),
          C("version_id", "text", "被评版本", nn=True, fk="version.id", on_delete="CASCADE"),
          C("rule_version_id", "text", "规则版本", nn=True, fk="mrc_rule_version.id", on_delete="RESTRICT"),
          C("tool_geometry_id", "text", "工具几何", nn=True, fk="tool_geometry.id", on_delete="RESTRICT"),
          C("engine_version", "text", "检查引擎版本", nn=True),
          C("input_digest", "hash", "输入摘要（几何+单位+网格容差+规则版本）", nn=True),
          C("total_score", "real", "总分（0–100）", nn=True, check="total_score>=0 AND total_score<=100"),
          C("issue_count", "int", "问题条数", nn=True, default="0", check="issue_count>=0"),
          C("duration_ms", "int", "耗时（毫秒）", check="duration_ms IS NULL OR duration_ms>=0"),
          CREATED("开始时间"),
      ],
      idx=[
          Index(["version_id", "created_at"], note="某版本的评分历史（MRC-011）"),
          Index(["version_id", "rule_version_id", "tool_geometry_id", "engine_version", "input_digest"],
                unique=True, name="uq_mrc_run_fingerprint", note="同一指纹只允许一次（重跑即新行）"),
          Index(["project_id", "created_at"], note="按工程看评分趋势"),
      ],
      trg=[
          ("trg_mrc_run_append_only", """
CREATE TRIGGER trg_mrc_run_append_only
BEFORE UPDATE ON mrc_run
BEGIN
  SELECT RAISE(ABORT, 'MRC-011: 评分结果只追加，旧分数不得被覆盖');
END;"""),
          ("trg_mrc_run_no_delete", """
CREATE TRIGGER trg_mrc_run_no_delete
BEFORE DELETE ON mrc_run
BEGIN
  SELECT RAISE(ABORT, 'MRC-011: 评分运行不可删除（会级联删掉违规记录）');
END;"""),
      ],
      note="分数不存 version.score 列（会破坏不可篡改）；version.score_after 只是提交瞬间的快照，用于列表展示。"),

    # ------------------------------------------------------------------ mrc_issue
    T("mrc_issue", "违规条目", "mrc",
      "一条违规：规则、对象、实测值、阈值、严重度、可读原因与建议。",
      ["MRC-001", "MRC-008", "MRC-009", "MRC-010", "T10", "T11", "T12"],
      [
          ID("问题 ID"),
          C("run_id", "text", "所属运行", nn=True, fk="mrc_run.id", on_delete="CASCADE"),
          C("version_id", "text", "被评版本（冗余）", nn=True, fk="version.id", on_delete="CASCADE",
            note="冗余用于问题列表热查询，避免每次都 join mrc_run"),
          C("rule_version_id", "text", "规则版本（冗余）", nn=True, fk="mrc_rule_version.id", on_delete="RESTRICT"),
          C("rule_code", "text", "规则码", nn=True, check="rule_code IN ('R1','R2','R3','R4','R5','R6','R7')"),
          C("severity", "text", "严重度", nn=True, check="severity IN ('blocker','major','minor','info')"),
          C("target_kind", "text", "违规对象类型", nn=True,
            check="target_kind IN ('route','pad','net','component','structure','face','board_outline','slice_path','deposit_pass','other')"),
          C("target_id", "text", "违规对象 ID", default="''"),
          C("measured_num", "real", "实测值"),
          C("threshold_num", "real", "阈值"),
          C("unit", "text", "单位", default="'mm'", check="unit IN ('mm','deg','c','count','ratio')"),
          C("message", "text", "为什么违规（中文）", nn=True),
          C("suggestion_json", "json", "建议动作（不自动执行）"),
          C("layer_no", "int", "层号"),
          C("x_mm", "real", "位置 X"),
          C("y_mm", "real", "位置 Y"),
          CREATED("产生时间"),
      ],
      idx=[
          Index(["version_id", "rule_version_id", "rule_code", "severity"],
                name="idx_issue_hot", note="问题列表按版本+规则+严重度筛选排序（MRC-010）"),
          Index(["run_id"], note="单次检查详情"),
          Index(["target_kind", "target_id"], note="点列表项定位到对象"),
      ],
      trg=[
          ("trg_issue_run_consistency", """
CREATE TRIGGER trg_issue_run_consistency
BEFORE INSERT ON mrc_issue
FOR EACH ROW
WHEN NEW.version_id <> (SELECT version_id FROM mrc_run WHERE id = NEW.run_id)
  OR NEW.rule_version_id <> (SELECT rule_version_id FROM mrc_run WHERE id = NEW.run_id)
BEGIN
  SELECT RAISE(ABORT, 'MRC-008: 违规条目的 version/rule_version 必须与所属运行一致');
END;"""),
          ("trg_issue_no_update", """
CREATE TRIGGER trg_issue_no_update
BEFORE UPDATE ON mrc_issue
BEGIN
  SELECT RAISE(ABORT, 'MRC-011: 违规条目不可改写，重新评分应追加新运行');
END;"""),
          ("trg_issue_no_delete", """
CREATE TRIGGER trg_issue_no_delete
BEFORE DELETE ON mrc_issue
BEGIN
  SELECT RAISE(ABORT, 'MRC-011: 违规条目不可删除（旧结果必须可解释）');
END;"""),
      ],
      note="建议只写文本+动作类型，不落执行状态；执行由 Agent 计划/对比卡片承载（MRC-009）。"),
]
