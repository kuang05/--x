"""agent 域：计划、步骤、工具调用、能力注册表、对比卡片、重放。"""
from __future__ import annotations

from .base import C, CREATED, ID, Index, T

TABLES = [
    # ------------------------------------------------------------------ plan
    T("plan", "执行计划", "agent",
      "一次 Agent 运行的计划头：目标、约束、基线版本、授权级别、预算与终止原因。",
      ["AGT-001", "AGT-002", "AGT-003", "AGT-004", "AGT-006", "AGT-007", "AGT-011", "T19"],
      [
          ID("计划 ID"),
          C("project_id", "text", "所属工程", nn=True, fk="project.id", on_delete="CASCADE"),
          C("base_version_id", "text", "起始版本（重放基准）", nn=True, fk="version.id", on_delete="RESTRICT"),
          C("base_digest", "hash", "起始版本内容摘要", nn=True,
            note="同版本号不同内容 = 不可比；重放必须校这个"),
          C("agent_role", "text", "Agent 角色", nn=True,
            check="agent_role IN ('design','review','arbiter','planner')",
            note="设计/审查/裁决/计划"),
          C("goal", "text", "目标（用户可读）", nn=True),
          C("constraints_json", "json", "约束（禁改网络拓扑、最小线宽等）"),
          C("status", "text", "状态", nn=True, default="'proposed'",
            check="status IN ('proposed','running','awaiting_confirm','converged','stopped_no_improvement','stopped_budget','rejected','failed','replayed')"),
          C("auth_level", "text", "授权级别", nn=True, default="'step_confirm'",
            check="auth_level IN ('suggest_only','step_confirm','auto')",
            note="默认每步确认（AGT-006）"),
          C("model_id", "text", "模型标识（审计用，不参与重放）", default="''"),
          C("degraded", "bool", "是否降级运行（无云端模型）", nn=True, default="0",
            note="AGT-010：断网时用本地规则集跑通检查—修改—复检"),
          C("prompt_digest", "hash", "提示摘要（审计用）"),
          C("toolchain_version", "text", "工具链版本（引擎+切片器）", nn=True),
          C("random_seed", "int", "随机种子", nn=True, default="0",
            note="自动布局/布线/故障注入的随机性来源"),
          C("rounds_used", "int", "已用轮数", nn=True, default="0", check="rounds_used>=0"),
          C("round_limit", "int", "轮数上限", nn=True, default="8", check="round_limit>0"),
          C("budget_json", "json", "预算（耗时、调用次数上限）"),
          C("stop_reason", "text", "终止原因", default="''"),
          C("score_before", "real", "起始分数", check="score_before IS NULL OR (score_before>=0 AND score_before<=100)"),
          C("score_after", "real", "收敛分数", check="score_after IS NULL OR (score_after>=0 AND score_after<=100)"),
          CREATED(),
          C("ended_at", "time", "结束时间"),
      ],
      idx=[
          Index(["project_id", "status"], note="计划列表"),
          Index(["base_version_id"], note="按基线版本查计划"),
      ],
      chk=[("rounds_used <= round_limit", "轮数不得超过上限（AGT-007）")],
      note="重放不重新推理，只回放 tool_call 序列；model_id/prompt_digest 仅作审计留痕。"),

    # ------------------------------------------------------------------ plan_step
    T("plan_step", "计划步骤", "agent",
      "计划里的一步：意图、工具与版本、解析后的参数、前置条件、期望结果、确认与执行状态。",
      ["AGT-004", "AGT-005", "AGT-006", "AGT-008", "T19"],
      [
          ID("步骤 ID"),
          C("plan_id", "text", "所属计划", nn=True, fk="plan.id", on_delete="CASCADE"),
          C("step_no", "int", "步骤序号（从 1 递增）", nn=True, check="step_no>=1"),
          C("intent", "text", "这一步想干什么（人类可读）", nn=True),
          C("tool_name", "text", "工具名", nn=True),
          C("tool_version", "text", "工具版本", nn=True, note="重放可比性依赖它（AGT-008）"),
          C("params_json", "json", "解析后的具体命令（含对象 ID）", nn=True,
            note="必须存具体命令，不能只存自然语言意图"),
          C("precondition_json", "json", "前置条件"),
          C("expected_json", "json", "期望结果"),
          C("status", "text", "状态", nn=True, default="'pending'",
            check="status IN ('pending','awaiting_confirm','applied','skipped','rejected','failed')"),
          C("risk_level", "text", "风险级别", nn=True, default="'low'",
            check="risk_level IN ('low','medium','high','dangerous')",
            note="dangerous 需人工确认，黑名单来自 tool_registry"),
          C("requires_confirm", "bool", "是否需要确认", nn=True, default="1"),
          C("confirm_decision", "text", "确认结论", default="''",
            check="confirm_decision IN ('','approved','rejected')"),
          C("confirmed_by", "text", "确认人", default="''"),
          C("confirmed_at", "time", "确认时间"),
          C("applied_op_id", "text", "执行后产生的操作", fk="operation.id", on_delete="SET NULL"),
          CREATED(),
      ],
      idx=[
          Index(["plan_id", "step_no"], unique=True, note="步骤顺序（AGT-004）"),
          Index(["tool_name", "tool_version"], note="按工具统计"),
      ],
      trg=[
          ("trg_review_step_readonly_ins", """
CREATE TRIGGER trg_review_step_readonly_ins
BEFORE INSERT ON plan_step
FOR EACH ROW
WHEN NEW.status = 'applied'
 AND (SELECT agent_role FROM plan WHERE id = NEW.plan_id) = 'review'
BEGIN
  SELECT RAISE(ABORT, 'AGT-002: 审查 Agent 只读，不得执行步骤');
END;"""),
          ("trg_review_step_readonly_upd", """
CREATE TRIGGER trg_review_step_readonly_upd
BEFORE UPDATE ON plan_step
FOR EACH ROW
WHEN NEW.status = 'applied'
 AND (SELECT agent_role FROM plan WHERE id = NEW.plan_id) = 'review'
BEGIN
  SELECT RAISE(ABORT, 'AGT-002: 审查 Agent 只读，不得执行步骤');
END;"""),
      ],
      note="“只应用一步”落在 diff_card.decision='apply_one' + 该步 status='applied'，不再单建审批表。"),

    # ------------------------------------------------------------------ tool_call
    T("tool_call", "工具调用", "agent",
      "每次实际调用工具的明细：参数、结果、耗时、错误码；重放按它逐条回放。",
      ["AGT-008", "AGT-009", "AGT-011", "AGT-010"],
      [
          ID("调用 ID"),
          C("step_id", "text", "所属步骤", nn=True, fk="plan_step.id", on_delete="CASCADE"),
          C("seq", "int", "步骤内序号（从 1 递增）", nn=True, check="seq>=1"),
          C("tool_name", "text", "工具名", nn=True),
          C("tool_version", "text", "工具版本", nn=True),
          C("args_json", "json", "调用参数", nn=True),
          C("result_status", "text", "结果", nn=True, check="result_status IN ('ok','error','rejected')"),
          C("result_digest", "hash", "结果摘要（一致性比对）"),
          C("duration_ms", "int", "耗时（毫秒）", check="duration_ms IS NULL OR duration_ms>=0"),
          C("error_code", "text", "错误码", default="''"),
          C("at", "time", "调用时间", nn=True),
      ],
      idx=[
          Index(["step_id", "seq"], unique=True, note="回放顺序"),
          Index(["tool_name", "tool_version"], note="工具使用统计"),
      ],
      note="重放按 (step_id, seq) 逐条回放；必须存实际调用参数与工具版本，不能只留意图。"),

    # ------------------------------------------------------------------ tool_registry
    T("tool_registry", "能力注册表", "agent",
      "工具 → 界面动作的映射：Agent 能做的事人也能做，没有隐藏能力。",
      ["AGT-009", "AGT-007", "AGT-006"],
      [
          C("tool_name", "text", "工具名", nn=True),
          C("tool_version", "text", "工具版本", nn=True),
          C("human_ui_action", "text", "对应的界面操作路径", nn=True,
            note="如“走线域 > 选择网络 > 手动布线”，人工等效抽检的判据"),
          C("is_dangerous", "bool", "是否危险操作", nn=True, default="0",
            note="危险操作禁止在 suggest_only/auto 下执行（AGT-007）"),
          C("min_auth_level", "text", "最低授权级别", nn=True, default="'step_confirm'",
            check="min_auth_level IN ('suggest_only','step_confirm','auto')"),
          C("note", "text", "说明", default="''"),
      ],
      pk=["tool_name", "tool_version"],
      idx=[Index(["is_dangerous"], name="idx_tool_danger", where="is_dangerous=1",
                  note="危险操作黑名单")],
      note="没有这张表，AGT-009“随机抽 5 条 Agent 操作人工可复现”只能靠文档声明。"),

    # ------------------------------------------------------------------ diff_card
    T("diff_card", "对比卡片", "agent",
      "修改前后的分数、改动对象、命令清单与理由；处置为应用/拒绝/只应用一步。",
      ["AGT-005", "AGT-006", "PRJ-004", "AGT-002", "T19"],
      [
          ID("卡片 ID"),
          C("plan_id", "text", "所属计划", nn=True, fk="plan.id", on_delete="CASCADE"),
          C("before_version_id", "text", "修改前版本", fk="version.id", on_delete="SET NULL"),
          C("after_version_id", "text", "修改后版本", fk="version.id", on_delete="SET NULL"),
          C("score_before", "real", "修改前分数", check="score_before IS NULL OR (score_before>=0 AND score_before<=100)"),
          C("score_after", "real", "修改后分数", check="score_after IS NULL OR (score_after>=0 AND score_after<=100)"),
          C("changed_objects_json", "json", "改动对象清单"),
          C("command_list_json", "json", "命令列表（可逐条应用）"),
          C("reason", "text", "理由（人类可读）", nn=True, default="''"),
          C("decision", "text", "处置", nn=True, default="'pending'",
            check="decision IN ('pending','apply','reject','apply_one')"),
          C("applied_step_id", "text", "只应用一步时的那一步", fk="plan_step.id", on_delete="SET NULL"),
          C("auth_level_at_time", "text", "处置时的授权级别", nn=True, default="'step_confirm'",
            check="auth_level_at_time IN ('suggest_only','step_confirm','auto')",
            note="授权级别中途可改，必须留快照（AGT-006）"),
          C("decided_by", "text", "决策者", default="''"),
          C("decided_at", "time", "决策时间"),
          CREATED(),
      ],
      idx=[
          Index(["plan_id"], note="一个计划的对比卡片"),
          Index(["decision"], note="待处置卡片"),
      ],
      chk=[("decision <> 'apply_one' OR applied_step_id IS NOT NULL", "只应用一步必须记录是哪一步")],
      trg=[
          ("trg_review_card_readonly_ins", """
CREATE TRIGGER trg_review_card_readonly_ins
BEFORE INSERT ON diff_card
FOR EACH ROW
WHEN NEW.decision IN ('apply','apply_one')
 AND (SELECT agent_role FROM plan WHERE id = NEW.plan_id) = 'review'
BEGIN
  SELECT RAISE(ABORT, 'AGT-002: 审查 Agent 只读，不得应用修改');
END;"""),
          ("trg_review_card_readonly_upd", """
CREATE TRIGGER trg_review_card_readonly_upd
BEFORE UPDATE ON diff_card
FOR EACH ROW
WHEN NEW.decision IN ('apply','apply_one')
 AND (SELECT agent_role FROM plan WHERE id = NEW.plan_id) = 'review'
BEGIN
  SELECT RAISE(ABORT, 'AGT-002: 审查 Agent 只读，不得应用修改');
END;"""),
      ],
      note="拒绝后模型与分数不变：decision='reject' 时不写 version_item、不产生新版本（AGT-005）。"),

    # ------------------------------------------------------------------ replay_run
    T("replay_run", "重放记录", "agent",
      "同一计划、同一起始版本、同一工具版本的重放证据：输出哈希与一致性结论。",
      ["AGT-008", "T20", "AGT-004"],
      [
          ID("重放 ID"),
          C("plan_id", "text", "被重放计划", nn=True, fk="plan.id", on_delete="CASCADE"),
          C("attempt_no", "int", "第几次重放", nn=True, check="attempt_no>=1"),
          C("start_version_id", "text", "起始版本", nn=True, fk="version.id", on_delete="RESTRICT"),
          C("toolchain_version", "text", "工具链版本", nn=True),
          C("rule_version_id", "text", "规则版本", fk="mrc_rule_version.id", on_delete="SET NULL"),
          C("tool_geometry_id", "text", "工具几何", fk="tool_geometry.id", on_delete="SET NULL"),
          C("process_profile_id", "text", "工艺配置", fk="process_profile.id", on_delete="SET NULL"),
          C("material_manifest_hash", "hash", "材料批次清单摘要", note="批次进 G-code 头，换批次即换哈希"),
          C("gcode_content_hash", "hash", "重放产出 G-code 内容哈希"),
          C("matched", "bool", "是否与基准一致", nn=True, default="0"),
          C("diff_summary_json", "json", "不一致时的差异摘要"),
          C("ran_at", "time", "重放时间", nn=True),
      ],
      idx=[
          Index(["plan_id", "attempt_no"], unique=True, note="同一计划的重放序列"),
          Index(["gcode_content_hash"], note="跨计划比对相同产出"),
      ],
      note="七要素缺一不可：base_version(+digest)、tool_version、rule_version、tool_geometry、process_profile(+hash)、material_batch、random_seed。"),
]
