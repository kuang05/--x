"""slice 域：材料与批次、工艺配置、切片、物理层、道次、路径、热历程、G-code 导出。"""
from __future__ import annotations

from .base import C, CREATED, ID, Index, T

TABLES = [
    # ------------------------------------------------------------------ material
    T("material", "材料", "slice",
      "材料型号与配方：可焊银浆（A）与热固化介电环氧（B）；停用后不再出现在切片选择列表。",
      ["SLC-013", "SLC-011", "1.7 首版工艺与硬件基线"],
      [
          ID("材料 ID"),
          C("code", "text", "材料代号（A/B 或自定义）", nn=True, check="length(trim(code)) BETWEEN 1 AND 32"),
          C("name", "text", "材料名称", nn=True),
          C("kind", "text", "类别", nn=True, check="kind IN ('silver_paste','dielectric','other')"),
          C("formula_json", "json", "配方（组分、比例、黏度）"),
          C("is_active", "bool", "启用状态", nn=True, default="1"),
          C("note", "text", "说明", default="''"),
          CREATED(),
      ],
      idx=[Index(["code"], unique=True, note="材料代号唯一"),
           Index(["is_active"], name="idx_material_active", where="is_active=1", note="可选材料列表")],
      note="首版不用 UV 材料（约束 3）；配方缺失时拒绝切片（SLC-011/013）。"),

    # ------------------------------------------------------------------ material_batch
    T("material_batch", "材料批次", "slice",
      "材料型号 + 批次号 + 到货日期 + 启用状态；批次号进 G-code 头，缺陷可回溯到批次。",
      ["SLC-008", "SLC-013", "PRT-009", "T16"],
      [
          ID("批次 ID"),
          C("material_id", "text", "所属材料", nn=True, fk="material.id", on_delete="CASCADE"),
          C("batch_no", "text", "批次号", nn=True, check="length(trim(batch_no))>0"),
          C("arrived_at", "time", "到货日期"),
          C("is_active", "bool", "启用状态", nn=True, default="1"),
          C("note", "text", "说明", default="''"),
          CREATED(),
      ],
      idx=[
          Index(["material_id", "batch_no"], unique=True, note="同材料批次号唯一"),
          Index(["is_active"], name="idx_batch_active", where="is_active=1"),
      ],
      note="批次在切片提交时冻结进 deposit_pass，导出只读冻结值；换批次必须显式重切（冲突 C12）。"),

    # ------------------------------------------------------------------ process_profile
    T("process_profile", "工艺配置", "slice",
      "可保存/加载的工艺参数集：层高、补偿、速度、出料系数、固化曲线；带参数摘要供重放比对。",
      ["SLC-003", "SLC-011", "SLC-005", "AGT-008"],
      [
          ID("配置 ID"),
          C("project_id", "text", "所属工程（空=全局模板）", fk="project.id", on_delete="CASCADE"),
          C("name", "text", "配置名", nn=True),
          C("params_json", "json", "参数集（含三层厚度、温度、时间、斜率）", nn=True),
          C("params_hash", "hash", "参数摘要（重放一致性）", nn=True),
          C("is_default", "bool", "是否默认配置", nn=True, default="0"),
          C("is_valid", "bool", "是否通过校验", nn=True, default="1"),
          C("validation_json", "json", "校验失败明细"),
          CREATED(),
      ],
      idx=[
          Index(["project_id", "name"], unique=True, name="uq_profile_project_name",
                where="project_id IS NOT NULL", note="工程内配置名唯一"),
          Index(["name"], unique=True, name="uq_profile_global_name",
                where="project_id IS NULL", note="全局模板名唯一"),
      ],
      note="参数变更必须生成新切片，不原地改旧切片（SLC-003）；配置不合法时拒绝切片并给原因（SLC-011）。"),

    # ------------------------------------------------------------------ slice_set
    T("slice_set", "切片", "slice",
      "一次切片结果的头：绑定设计版本与工艺/规则/工具，记录层道次数、总厚、估算与过期标记。",
      ["SLC-001", "SLC-002", "SLC-010", "SLC-012", "SLC-009", "T13", "T14"],
      [
          ID("切片 ID"),
          C("project_id", "text", "所属工程", nn=True, fk="project.id", on_delete="CASCADE"),
          C("design_version_id", "text", "绑定的设计版本", nn=True, fk="version.id", on_delete="RESTRICT",
            note="绑定不可变；设计变更后置 stale，但不删除旧切片（SLC-010）"),
          C("process_profile_id", "text", "工艺配置", nn=True, fk="process_profile.id", on_delete="RESTRICT"),
          C("rule_version_id", "text", "规则版本（切片时生效）", fk="mrc_rule_version.id", on_delete="SET NULL"),
          C("tool_geometry_id", "text", "工具几何", fk="tool_geometry.id", on_delete="SET NULL"),
          C("engine_version", "text", "切片引擎版本", nn=True),
          C("status", "text", "状态", nn=True, default="'computing'",
            check="status IN ('computing','ready','failed','stale')"),
          C("stale_since", "time", "过期时间", note="显式记录；不靠“是否等于当前版本”实时推导"),
          C("total_thickness_mm", "real", "层厚合计（mm）", check="total_thickness_mm IS NULL OR total_thickness_mm>0"),
          C("target_thickness_min_mm", "real", "目标总厚下限", nn=True, default="0.35", check="target_thickness_min_mm>0"),
          C("target_thickness_max_mm", "real", "目标总厚上限", nn=True, default="0.40", check="target_thickness_max_mm>=target_thickness_min_mm"),
          C("layer_count", "int", "物理层数", nn=True, default="3", check="layer_count=3"),
          C("pass_count", "int", "道次数", nn=True, default="7", check="pass_count IN (7,8)"),
          C("estimate_json", "json", "估算（路径长/时间/浆料/成本/假设/首次升温拆分）"),
          C("input_digest", "hash", "输入摘要（版本+参数+材料批次）"),
          CREATED("创建时间"),
          C("finished_at", "time", "完成时间"),
      ],
      idx=[
          Index(["project_id", "status"], note="工程内可用/过期切片"),
          Index(["design_version_id"], note="按版本查切片"),
          Index(["process_profile_id"], note="按配置查切片"),
      ],
      chk=[("status <> 'stale' OR stale_since IS NOT NULL", "过期切片必须有 stale_since")],
      trg=[
          ("trg_slice_stale_on_version", """
CREATE TRIGGER trg_slice_stale_on_version
AFTER INSERT ON version
FOR EACH ROW
BEGIN
  UPDATE slice_set
     SET status = 'stale',
         stale_since = strftime('%Y-%m-%dT%H:%M:%fZ','now')
   WHERE project_id = NEW.project_id
     AND status = 'ready'
     AND design_version_id <> NEW.id;
END;"""),
          ("trg_slice_ready_guard", """
CREATE TRIGGER trg_slice_ready_guard
BEFORE UPDATE OF status ON slice_set
FOR EACH ROW
WHEN NEW.status = 'ready'
 AND NEW.design_version_id <> (SELECT current_version_id FROM project WHERE id = NEW.project_id)
BEGIN
  SELECT RAISE(ABORT, 'SLC-010: 绑定版本已不是当前版本，不得置为 ready；请先回滚或重新切片');
END;"""),
      ],
      note="总厚口径冲突（0.35–0.40 vs 层厚相加 0.44）处理：层厚为唯一真值，合计写入 total_thickness_mm，超目标区间只告警不报错（冲突 C2）。"),

    # ------------------------------------------------------------------ physical_layer
    T("physical_layer", "物理层", "slice",
      "下保护层、电路层、上保护层三层：层序、厚度、材料。",
      ["SLC-001", "T13", "1.7 物理层"],
      [
          ID("物理层 ID"),
          C("slice_set_id", "text", "所属切片", nn=True, fk="slice_set.id", on_delete="CASCADE"),
          C("layer_index", "int", "层序（0 起）", nn=True, check="layer_index>=0"),
          C("kind", "text", "层类别", nn=True,
            check="kind IN ('bottom_mask','circuit','top_mask')",
            note="下保护 / 电路 / 上保护"),
          C("thickness_mm", "real", "层厚（mm）", nn=True, check="thickness_mm>0"),
          C("material_id", "text", "主材料", fk="material.id", on_delete="SET NULL"),
          C("note", "text", "说明", default="''"),
      ],
      idx=[Index(["slice_set_id", "layer_index"], unique=True, note="层序唯一（T13 三层）")],
      note="层与道次必须拆两张表：道次 0–2 同属“下保护”，只建一张表无法表达“同一高度两个道次”。"),

    # ------------------------------------------------------------------ deposit_pass
    T("deposit_pass", "道次", "slice",
      "一次材料沉积：同一高度可有介电道次与银浆道次，典型 7 个、银浆分两次填则 8 个。",
      ["SLC-002", "SLC-004", "SLC-008", "T13", "T14"],
      [
          ID("道次 ID"),
          C("slice_set_id", "text", "所属切片", nn=True, fk="slice_set.id", on_delete="CASCADE"),
          C("physical_layer_id", "text", "所属物理层", nn=True, fk="physical_layer.id", on_delete="CASCADE"),
          C("pass_index", "int", "道次序（0 起，全局）", nn=True, check="pass_index>=0"),
          C("material_id", "text", "材料", nn=True, fk="material.id", on_delete="RESTRICT"),
          C("material_batch_id", "text", "材料批次（切片时冻结）", fk="material_batch.id", on_delete="RESTRICT"),
          C("pattern", "text", "图案类型", nn=True,
            check="pattern IN ('solid','dam_and_trench','trench_fill','pad_pit','pad_fill','support','other')"),
          C("thickness_mm", "real", "道次厚度（mm）", nn=True, check="thickness_mm>0"),
          C("z_top_mm", "real", "道次顶面 Z（mm）"),
          C("note", "text", "说明", default="''"),
      ],
      idx=[
          Index(["slice_set_id", "pass_index"], unique=True, note="道次序在切片内唯一"),
          Index(["physical_layer_id"], note="按层取道次"),
          Index(["material_batch_id"], note="批次反查（SLC-008）"),
      ],
      note="层:道次 = 1:N，必须两张表；ER v0.1 只建 Layer 是硬伤（同一高度两个道次无法表达）。"),

    # ------------------------------------------------------------------ slice_path
    T("slice_path", "路径段", "slice",
      "一段可执行轨迹：挤出或空移，几何放 blob，行上保留长度/包围盒/层高供过滤。",
      ["SLC-004", "SLC-006", "SLC-007", "SLC-008", "NFR-001"],
      [
          C("id", "int64", "行号主键（整型，性能例外）", nn=True, pk=True),
          C("deposit_pass_id", "text", "所属道次", nn=True, fk="deposit_pass.id", on_delete="CASCADE"),
          C("seq", "int", "道次内序号（0 起）", nn=True, check="seq>=0"),
          C("path_kind", "text", "路径类型", nn=True, check="path_kind IN ('extrude','travel')",
            note="空移与挤出必须可区分（SLC-004）"),
          C("geom_blob_hash", "hash", "点序（CAS，float32 x,y,z）", nn=True, fk="blob.hash"),
          C("point_count", "int", "点数", nn=True, check="point_count>=2"),
          C("length_mm", "real", "长度（mm）", nn=True, check="length_mm>=0"),
          C("bbox_min_x_mm", "real", "包围盒 X 最小"),
          C("bbox_min_y_mm", "real", "包围盒 Y 最小"),
          C("bbox_max_x_mm", "real", "包围盒 X 最大"),
          C("bbox_max_y_mm", "real", "包围盒 Y 最大"),
          C("z_mm", "real", "所在高度（mm）", check="z_mm IS NULL OR z_mm>=0"),
          C("speed_mm_s", "real", "速度（mm/s）", check="speed_mm_s IS NULL OR speed_mm_s>0"),
          C("flow_factor", "real", "出料系数", check="flow_factor IS NULL OR flow_factor>=0"),
      ],
      idx=[
          Index(["deposit_pass_id", "seq"], unique=True, note="回放顺序（SLC-006）"),
          Index(["geom_blob_hash"], note="几何去重"),
      ],
      note="唯一使用整型主键的表：单切片可达数万段，36 字节 UUID 会显著放大索引；对外仍以 (deposit_pass, seq) 定位。控制点绝不拆行。"),

    # ------------------------------------------------------------------ thermal_step
    T("thermal_step", "热历程阶段", "slice",
      "排程时间轴：预凝胶、银浆定型、层固化、整板终固化、冷却；按道次或按物理层。",
      ["SLC-005", "PRT-004", "SLC-009", "T14"],
      [
          ID("阶段 ID"),
          C("slice_set_id", "text", "所属切片", nn=True, fk="slice_set.id", on_delete="CASCADE"),
          C("step_index", "int", "阶段序（0 起）", nn=True, check="step_index>=0"),
          C("stage", "text", "阶段类型", nn=True,
            check="stage IN ('pre_gel','silver_set','cure','final_cure','cool')"),
          C("target_kind", "text", "作用范围", nn=True, default="'layer'",
            check="target_kind IN ('layer','pass','board')",
            note="固化按道次还是按物理层可配，默认按物理层（待确认 9-2）"),
          C("target_id", "text", "目标层/道次 ID", default="''"),
          C("temp_c", "real", "目标温度（℃）", nn=True),
          C("ramp_c_per_min", "real", "升温斜率（℃/min）", check="ramp_c_per_min IS NULL OR ramp_c_per_min>0"),
          C("hold_s", "int", "保温时长（秒）", nn=True, default="0", check="hold_s>=0"),
          C("note", "text", "说明", default="''"),
      ],
      idx=[Index(["slice_set_id", "step_index"], unique=True, note="时间轴顺序（T14）")],
      chk=[
          ("temp_c BETWEEN 0 AND 300", "温度在工艺范围内"),
          ("stage <> 'cure' OR ramp_c_per_min IS NULL OR ramp_c_per_min <= 3", "层固化升温不超过 3 ℃/min（1.7）"),
      ],
      note="固化中断恢复要按当前温度重算剩余保温时间，因此 hold_s 必须可查、不可只存文本（PRT-004）。"),

    # ------------------------------------------------------------------ gcode_export
    T("gcode_export", "G-code 导出", "slice",
      "一次导出：内容哈希（规范化）与文件哈希（落盘字节）双记录；批次清单摘要供反查。",
      ["SLC-008", "SLC-007", "T16", "AGT-008", "T20"],
      [
          ID("导出 ID"),
          C("slice_set_id", "text", "所属切片", nn=True, fk="slice_set.id", on_delete="CASCADE"),
          C("content_hash", "hash", "规范化内容哈希（验收口径）", nn=True,
            note="剔除时间戳/会话 ID/绝对路径，浮点统一 %.3f"),
          C("file_hash", "hash", "落盘文件字节哈希", nn=True),
          C("blob_hash", "hash", "G-code 文件（CAS）", fk="blob.hash", on_delete="SET NULL"),
          C("byte_size", "int", "字节数", check="byte_size IS NULL OR byte_size>=0"),
          C("header_json", "json", "G-code 头（版本、材料、批次、层/道次、烘箱阶段）"),
          C("material_manifest_hash", "hash", "材料批次清单摘要（一次查询反查批次）"),
          C("engine_version", "text", "导出器版本", nn=True),
          CREATED("导出时间"),
      ],
      idx=[
          Index(["slice_set_id", "content_hash"], unique=True, note="同一内容哈希只记一次（T16 两次导出一致）"),
          Index(["content_hash"], note="跨工程比对相同 G-code"),
      ],
      trg=[
          ("trg_gcode_append_only", """
CREATE TRIGGER trg_gcode_append_only
BEFORE UPDATE ON gcode_export
BEGIN
  SELECT RAISE(ABORT, 'SLC-008: 导出记录只追加，哈希不得被改写');
END;"""),
          ("trg_gcode_no_delete", """
CREATE TRIGGER trg_gcode_no_delete
BEFORE DELETE ON gcode_export
BEGIN
  SELECT RAISE(ABORT, 'SLC-008: 导出记录不可删除（哈希是追溯证据）');
END;"""),
      ],
      note="只存 file_hash 会让“两次导出哈希一致”恒假（文件内常有时间戳）；验收一律看 content_hash。"),
]
