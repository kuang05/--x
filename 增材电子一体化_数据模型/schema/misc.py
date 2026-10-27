"""misc 域：受控字典、验收任务结果、工程包清单。"""
from __future__ import annotations

from .base import C, CREATED, ID, Index, T

TABLES = [
    # ------------------------------------------------------------------ dictionary_entry
    T("dictionary_entry", "受控字典", "misc",
      "枚举中文标签、诊断码解释、术语表三合一；离线可查（UI-008/UI-010），也是复合外键的枚举域。",
      ["UI-005", "UI-008", "UI-010", "STR-002", "SLC-012"],
      [
          C("domain", "text", "字典域", nn=True,
            check="domain IN ('enum','diagnostic','glossary','mrc_rule','fault','defect')"),
          C("code", "text", "编码", nn=True, check="length(trim(code))>0"),
          C("label_zh", "text", "中文标签", nn=True),
          C("label_en", "text", "英文标签", default="''"),
          C("description", "text", "解释（错误码的“为什么”与“怎么办”）", default="''"),
          C("parent_code", "text", "父级编码（树形术语表）", default="''"),
          C("sort_order", "int", "排序号", nn=True, default="0"),
          C("is_active", "bool", "启用状态", nn=True, default="1"),
      ],
      pk=["domain", "code"],
      idx=[
          Index(["domain", "parent_code"], note="按域取树"),
          Index(["label_zh"], note="术语检索（FTS5 外部内容表的来源）"),
      ],
      note="枚举只存英文 code，中文标签放这里；UI-008 要求“错误信息不以内部代号作为唯一解释”，靠 description 兜底。"),

    # ------------------------------------------------------------------ cloud_call
    T("cloud_call", "云端调用记录", "misc",
      "云端智能体的调用证据：用途、开关、请求/响应摘要、耗时与降级去向。",
      ["AGT-012", "5.4 云端接口", "9-5 待确认"],
      [
          ID("调用 ID"),
          C("project_id", "text", "所属工程", fk="project.id", on_delete="CASCADE"),
          C("purpose", "text", "用途", nn=True,
            check="purpose IN ('explain','rank','defect_detect','train','inference','other')"),
          C("enabled", "bool", "调用时云功能是否开启", nn=True, default="1"),
          C("request_digest", "hash", "请求摘要（不落原文）"),
          C("response_digest", "hash", "响应摘要"),
          C("model_id", "text", "模型标识", default="''"),
          C("latency_ms", "int", "耗时（毫秒）", check="latency_ms IS NULL OR latency_ms>=0"),
          C("status", "text", "结果", nn=True, check="status IN ('ok','failed','degraded','skipped')"),
          C("degraded_to", "text", "降级去向（本地规则集标识）", default="''"),
          C("detail_json", "json", "详情（错误、重试次数）"),
          C("at", "time", "调用时间", nn=True),
      ],
      idx=[
          Index(["project_id", "at"], note="按工程查云端调用"),
          Index(["purpose", "status"], note="比赛材料的“真实调用”证据"),
      ],
      note="只存摘要不存原文：符合 DAT-007 隐私要求；关闭云功能后 status 只会是 skipped。"),

    # ------------------------------------------------------------------ accept_result
    T("accept_result", "验收任务结果", "misc",
      "7.1 的 20 条验收任务逐条留证：输入哈希、成功与否、轮数、耗时、初始分/收敛分、G-code 哈希。",
      ["7.1 验收任务", "7.2 验收口径", "T19", "T20"],
      [
          ID("结果 ID"),
          C("task_no", "text", "任务号", nn=True, check="task_no GLOB 'T[0-9][0-9]'"),
          C("project_id", "text", "所用工程", fk="project.id", on_delete="SET NULL"),
          C("input_blob_hash", "hash", "输入文件哈希", nn=True,
            note="T19 要求固定验收板哈希随文档入库，本列即载体"),
          C("passed", "bool", "是否通过", nn=True, default="0"),
          C("rounds", "int", "Agent 收敛轮数", check="rounds IS NULL OR rounds>=0"),
          C("duration_s", "real", "耗时（秒）", check="duration_s IS NULL OR duration_s>=0"),
          C("score_initial", "real", "初始分数", check="score_initial IS NULL OR (score_initial>=0 AND score_initial<=100)"),
          C("score_final", "real", "收敛分数", check="score_final IS NULL OR (score_final>=0 AND score_final<=100)"),
          C("gcode_hash", "hash", "G-code 内容哈希"),
          C("diagnostic_code", "text", "失败诊断码", default="''"),
          C("notes", "text", "备注", default="''"),
          C("ran_at", "time", "执行时间", nn=True),
      ],
      idx=[
          Index(["task_no", "ran_at"], note="按任务看历次结果"),
          Index(["input_blob_hash"], note="按输入比对可复现性"),
      ],
      note="这张表让“P0 全过、P1 允许失败但记录原因”的验收口径可被机器核对，而不是靠人写报告。"),

    # ------------------------------------------------------------------ package_manifest
    T("package_manifest", "工程包清单", "misc",
      "导出/导入的工程包：内容清单、整体哈希、是否内嵌 blob、导入冲突记录。",
      ["PRJ-006", "DAT-004", "NFR-008"],
      [
          ID("清单 ID"),
          C("project_id", "text", "所属工程（空=整库备份包）", fk="project.id", on_delete="CASCADE",
            note="导出单个工程时指向该工程；整库备份时为空"),
          C("exported_at", "time", "导出时间", nn=True),
          C("package_hash", "hash", "整包哈希", nn=True),
          C("schema_version", "int", "数据模式版本", nn=True, check="schema_version>=1"),
          C("include_blobs", "bool", "是否内嵌 blob（跨机必需）", nn=True, default="1"),
          C("entries_json", "json", "清单（表名→行数、blob 哈希列表）"),
          C("import_result", "text", "导入结果", default="''",
            check="import_result IN ('','ok','conflict','failed')"),
          C("conflict_json", "json", "冲突明细（同源工程、哈希不一致）"),
          CREATED(),
      ],
      idx=[Index(["project_id", "exported_at"], note="按工程列工程包")],
      note="工程包一律内嵌被引用 blob（冲突 C6）：跨机导入不能假设对方机器已有同样的 CAS 文件。"),
]
