"""core 域：工程、版本、会话、操作日志、快照、内容寻址存储、外部文件、设置。"""
from __future__ import annotations

from .base import C, CREATED, ID, ISO_CHECK, Index, T, UPDATED

TABLES = [
    # ------------------------------------------------------------------ project
    T("project", "工程", "core",
      "一个项目的根容器；指向当前提交版本，并记录数据目录与导入来源。",
      ["PRJ-001", "PRJ-002", "PRJ-007", "PRJ-008", "PRJ-009", "DAT-001", "DAT-006"],
      [
          ID("工程 ID（UUIDv7）"),
          C("name", "text", "工程名称", nn=True, check="length(trim(name)) BETWEEN 1 AND 120"),
          C("unit", "text", "显示单位", nn=True, default="'mm'", check="unit IN ('mm','inch')",
            note="全库统一按 mm 存储；inch 仅作显示与导入换算（PRJ-008）"),
          C("sheet_w_mm", "real", "幅面宽（mm）", nn=True, default="200", check="sheet_w_mm>0 AND sheet_w_mm<=400"),
          C("sheet_h_mm", "real", "幅面高（mm）", nn=True, default="200", check="sheet_h_mm>0 AND sheet_h_mm<=400"),
          C("origin_kind", "text", "坐标原点定义", nn=True, default="'board_bottom_left'",
            check="origin_kind IN ('board_bottom_left','custom')"),
          C("origin_x_mm", "real", "自定义原点 X（mm）", default="0"),
          C("origin_y_mm", "real", "自定义原点 Y（mm）", default="0"),
          C("data_schema_version", "int", "数据模式版本号", nn=True, default="1", check="data_schema_version>=1"),
          C("current_version_id", "text", "当前版本（指向 version.id）", fk="version.id", on_delete="SET NULL",
            note="新建工程时先插 project 再插 v0，随后回填；避免循环非空外键"),
          C("dir_rel_path", "text", "工程目录（相对数据根）", nn=True),
          C("package_origin_id", "text", "工程包来源标识", note="跨机导入时识别同源工程，避免误合并（PRJ-006）"),
          C("deleted_at", "time", "回收站删除时间", note="非空即在回收站；历史版本不物理删除（数据规则 4）"),
          CREATED(),
          UPDATED(),
      ],
      idx=[
          Index(["deleted_at"], name="idx_project_alive", where="deleted_at IS NULL",
                note="最近工程列表只取未删除"),
          Index(["updated_at"], note="按最近使用排序（PRJ-002）"),
      ],
      chk=[("deleted_at IS NULL OR deleted_at >= created_at", "删除时间晚于创建时间")],
      trg=[
          ("trg_project_touch", """
CREATE TRIGGER trg_project_touch
AFTER UPDATE OF name, unit, sheet_w_mm, sheet_h_mm, current_version_id ON project
FOR EACH ROW
BEGIN
  UPDATE project SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;"""),
      ],
      note="单库存全部工程（app.db），大文件走 CAS 目录；这样最近工程列表与跨工程去重都不必跨库查询。"),

    # ------------------------------------------------------------------ version
    T("version", "提交版本", "core",
      "用户可见的提交点：只追加、不可篡改。撤销不产生版本，只有保存/提交/应用计划/回滚才产生。",
      ["PRJ-004", "PRJ-005", "DAT-002", "AGT-005", "SLC-010"],
      [
          ID("版本 ID（UUIDv7）"),
          C("project_id", "text", "所属工程", nn=True, fk="project.id", on_delete="CASCADE"),
          C("version_no", "int", "工程内版本号（从 0 递增）", nn=True, check="version_no>=0",
            note="排序只用 version_no，不用时间戳（同秒或时钟回拨会乱序）"),
          C("parent_version_id", "text", "父版本", fk="version.id", on_delete="RESTRICT"),
          C("kind", "text", "版本产生方式", nn=True, default="'manual'",
            check="kind IN ('initial','manual','agent','rollback','import')"),
          C("author_kind", "text", "作者类型", nn=True, default="'human'",
            check="author_kind IN ('human','agent','system')"),
          C("author_name", "text", "作者标识", nn=True, default="'local'",
            note="单用户软件不建用户表，只区分人/Agent/系统（见冲突 C14）"),
          C("reason", "text", "修改原因（用户可读）", nn=True, default="''"),
          C("score_before", "real", "提交前 MRC 分数", check="score_before IS NULL OR (score_before>=0 AND score_before<=100)"),
          C("score_after", "real", "提交后 MRC 分数", check="score_after IS NULL OR (score_after>=0 AND score_after<=100)"),
          C("rule_version_id", "text", "评分所用规则版本", fk="mrc_rule_version.id", on_delete="SET NULL",
            note="分数快照的规则版本；重评分不改这里，另写 mrc_run（MRC-011）"),
          C("summary", "text", "变更摘要", default="''"),
          CREATED(),
      ],
      idx=[
          Index(["project_id", "version_no"], unique=True, note="版本号在工程内唯一（PRJ-004）"),
          Index(["parent_version_id"], note="版本树回溯"),
          Index(["project_id", "created_at"], note="按时间列历史"),
      ],
      chk=[
          ("parent_version_id IS NULL OR parent_version_id <> id", "版本不能以自身为父"),
          ("kind <> 'initial' OR version_no = 0", "初始版本必须是 0 号"),
      ],
      trg=[
          ("trg_version_no_update", """
CREATE TRIGGER trg_version_no_update
BEFORE UPDATE ON version
BEGIN
  SELECT RAISE(ABORT, 'DAT-002: version 表不可篡改（禁止 UPDATE）');
END;"""),
          ("trg_version_no_delete", """
CREATE TRIGGER trg_version_no_delete
BEFORE DELETE ON version
BEGIN
  SELECT RAISE(ABORT, 'DAT-002: version 表不可删除（历史只读）');
END;"""),
      ],
      note="版本内容不存全量快照：对象级差异在 version_item，几何/网格/路径在 blob，周期快照在 snapshot。"),

    # ------------------------------------------------------------------ version_item
    T("version_item", "版本变更项", "core",
      "一次提交里，每个被改对象一条记录，供“改了哪些对象、分数变化多少”直接查询。",
      ["PRJ-004", "DAT-002", "AGT-011"],
      [
          ID("变更项 ID"),
          C("version_id", "text", "所属版本", nn=True, fk="version.id", on_delete="CASCADE"),
          C("target_kind", "text", "对象类型", nn=True,
            check="target_kind IN ('structure','feature','board_outline','circuit','component','pad','net','route','jumper','keepout','slice_set','process_profile','material','plan','defect','attribution','setting')"),
          C("target_id", "text", "对象 ID", nn=True,
            note="多态引用，无法建外键；用 target_kind 白名单 + 校验脚本兜底"),
          C("change_kind", "text", "变更类型", nn=True, check="change_kind IN ('create','update','delete')"),
          C("before_json", "json", "变更前状态（对象级差分）"),
          C("after_json", "json", "变更后状态"),
          C("diff_summary", "text", "人类可读差异摘要", default="''"),
      ],
      idx=[
          Index(["version_id", "target_kind"], note="查看某版本改了哪些对象（PRJ-004）"),
          Index(["target_kind", "target_id"], note="反查某对象被哪些版本改过"),
      ],
      trg=[
          ("trg_version_item_no_update", """
CREATE TRIGGER trg_version_item_no_update
BEFORE UPDATE ON version_item
BEGIN
  SELECT RAISE(ABORT, 'DAT-002: 版本变更项不可修改，只能随版本追加');
END;"""),
          ("trg_version_item_no_delete", """
CREATE TRIGGER trg_version_item_no_delete
BEFORE DELETE ON version_item
BEGIN
  SELECT RAISE(ABORT, 'DAT-002: 版本变更项不可删除（历史只读）');
END;"""),
      ],
      note="before/after 只存对象级字段差异，不存全量文档；单条建议 < 32 KB，超出走 blob。"),

    # ------------------------------------------------------------------ edit_session
    T("edit_session", "编辑会话", "core",
      "一次打开到关闭的过程；撤销栈与操作日志的归属容器，也是异常恢复的边界。",
      ["PRJ-002", "PRJ-003", "PRJ-005", "DAT-001"],
      [
          ID("会话 ID"),
          C("project_id", "text", "所属工程", nn=True, fk="project.id", on_delete="CASCADE"),
          C("start_version_id", "text", "会话起始版本", fk="version.id"),
          C("end_version_id", "text", "会话结束版本", fk="version.id"),
          C("app_version", "text", "软件版本", nn=True),
          C("machine_id", "text", "机器标识", nn=True, note="同机生成，不参与云同步"),
          C("started_at", "time", "开始时间", nn=True),
          C("ended_at", "time", "结束时间"),
          C("exit_kind", "text", "退出方式", nn=True, default="'open'",
            check="exit_kind IN ('open','normal','crash','forced')",
            note="非 normal 时下次打开给恢复提示（PRJ-003）"),
          C("recovered", "bool", "是否已恢复", nn=True, default="0"),
      ],
      idx=[Index(["project_id", "started_at"], note="按工程查会话")],
      chk=[("ended_at IS NULL OR ended_at >= started_at", "结束不早于开始")],
      note="表名用 edit_session 而非 session，避开 SQL 关键字与函数名歧义。"),

    # ------------------------------------------------------------------ operation
    T("operation", "操作日志（撤销/重做载体）", "core",
      "每次成功修改追加一条；撤销/重做也是追加一条指向原操作。版本号全程不变。",
      ["PRJ-003", "PRJ-005", "PRJ-004", "NFR-002"],
      [
          ID("操作 ID"),
          C("session_id", "text", "所属会话", nn=True, fk="edit_session.id", on_delete="CASCADE"),
          C("seq", "int", "会话内序号（从 1 递增）", nn=True, check="seq>=1"),
          C("op_type", "text", "操作类型", nn=True,
            check="op_type IN ('create','update','delete','undo','redo','import','apply_plan','rollback')"),
          C("target_kind", "text", "对象类型", nn=True),
          C("target_id", "text", "对象 ID"),
          C("payload_json", "json", "正向操作内容"),
          C("inverse_json", "json", "反向操作内容（撤销用）"),
          C("inverse_blob_hash", "hash", "反向操作过大时存 blob", fk="blob.hash", on_delete="SET NULL"),
          C("undo_of_op_id", "text", "撤销/重做指向的原操作", fk="operation.id", on_delete="RESTRICT"),
          C("version_id", "text", "该操作最终落入的版本", fk="version.id", on_delete="SET NULL",
            note="操作发生时通常为空，提交时回填"),
          C("author_kind", "text", "发起者类型", nn=True, default="'human'",
            check="author_kind IN ('human','agent','system')"),
          CREATED("发生时间"),
      ],
      idx=[
          Index(["session_id", "seq"], unique=True, note="撤销栈按序号回放（PRJ-005）"),
          Index(["undo_of_op_id"], note="查找某操作的撤销记录"),
          Index(["target_kind", "target_id"], note="对象历史"),
      ],
      trg=[
          ("trg_operation_append_only", """
CREATE TRIGGER trg_operation_append_only
BEFORE UPDATE ON operation
BEGIN
  SELECT RAISE(ABORT, 'PRJ-005: 操作日志只追加，不允许改写');
END;"""),
          ("trg_operation_no_delete", """
CREATE TRIGGER trg_operation_no_delete
BEFORE DELETE ON operation
BEGIN
  SELECT RAISE(ABORT, 'PRJ-005: 操作日志不可删除（撤销靠追加，不靠删行）');
END;"""),
      ],
      note="撤销栈可能超过 20 步（PRJ-005 要求至少 20），保留全量；会话结束时按策略归档。"),

    # ------------------------------------------------------------------ snapshot
    T("snapshot", "快照", "core",
      "会话恢复快照与版本快照。快照不是版本：只用于加速恢复与备份，不进入版本链。",
      ["PRJ-002", "PRJ-003", "DAT-004", "NFR-002"],
      [
          ID("快照 ID"),
          C("project_id", "text", "所属工程", nn=True, fk="project.id", on_delete="CASCADE"),
          C("session_id", "text", "所属会话（scope=session 时必填）", fk="edit_session.id", on_delete="CASCADE"),
          C("scope", "text", "快照用途", nn=True, check="scope IN ('session','version','backup')"),
          C("version_id", "text", "对应版本（scope=version/backup）", fk="version.id", on_delete="SET NULL"),
          C("op_seq", "int", "快照覆盖到的操作序号", check="op_seq IS NULL OR op_seq>=0"),
          C("blob_hash", "hash", "快照内容（CAS）", nn=True, fk="blob.hash"),
          C("byte_size", "int", "字节数", nn=True, check="byte_size>=0"),
          CREATED("生成时间"),
      ],
      idx=[
          Index(["session_id", "op_seq"], note="恢复时取最近快照再重放后续操作"),
          Index(["project_id", "scope"], note="按工程列快照"),
      ],
      chk=[("scope <> 'session' OR session_id IS NOT NULL", "会话快照必须绑定会话")],
      note="恢复算法：取 op_seq 最大的快照 → 重放 operation 到目标点。快照缺失时从初始版本重放（PRJ-002）。"),

    # ------------------------------------------------------------------ blob
    T("blob", "内容寻址存储索引", "core",
      "模型、网格、路径、证据、G-code、快照都按 sha256 存文件系统，本表只登记索引与校验状态。",
      ["DAT-004", "DAT-005", "DAT-002", "SLC-008"],
      [
          C("hash", "hash", "sha256（小写十六进制）", nn=True, pk=True,
            note="主键即哈希，天然去重"),
          C("algo", "text", "摘要算法", nn=True, default="'sha256'", check="algo IN ('sha256')"),
          C("kind", "text", "内容类别", nn=True,
            check="kind IN ('step','mesh','route','path','evidence','gcode','snapshot','preview','package','other')"),
          C("byte_size", "int", "字节数", nn=True, check="byte_size>=0"),
          C("rel_path", "text", "相对路径（blobs/ab/cd/<hash>）", nn=True),
          C("ref_count", "int", "引用计数（缓存值）", nn=True, default="0", check="ref_count>=0",
            note="可随时重算；正确性不依赖它，只用于 GC 筛选"),
          C("verify_status", "text", "校验状态", nn=True, default="'unverified'",
            check="verify_status IN ('unverified','ok','missing','mismatch')"),
          C("last_verified_at", "time", "最近校验时间"),
          C("is_pinned", "bool", "钉住（不参与 GC）", nn=True, default="0"),
          C("gc_graced_until", "time", "GC 宽限截止"),
          CREATED("写入时间"),
      ],
      idx=[
          Index(["verify_status"], name="idx_blob_bad", where="verify_status<>'ok'",
                note="完整性巡检只扫异常项（DAT-005）"),
          Index(["kind", "created_at"], note="按类别清理/备份"),
      ],
      note="写入顺序：先落文件并 fsync，再插库；避免库有行而盘无文件。物理删除需显式触发并写 blob_gc_log。"),

    # ------------------------------------------------------------------ blob_gc_log
    T("blob_gc_log", "存储回收日志", "core",
      "内容寻址存储的回收记录：扫描数、实际删除数、释放字节；默认 dry-run，物理删除需显式触发。",
      ["DAT-004", "4.2 数据规则 4", "4.2 数据规则 5"],
      [
          ID("日志 ID"),
          C("scanned", "int", "扫描 blob 数", nn=True, default="0", check="scanned>=0"),
          C("deleted", "int", "实际删除数", nn=True, default="0", check="deleted>=0"),
          C("freed_bytes", "int", "释放字节数", nn=True, default="0", check="freed_bytes>=0"),
          C("dry_run", "bool", "是否仅演练", nn=True, default="1"),
          C("deleted_hashes_json", "json", "被删除的哈希清单"),
          C("note", "text", "说明（触发方式、宽限期）", default="''"),
          CREATED("执行时间"),
      ],
      chk=[("deleted <= scanned", "删除数不能超过扫描数")],
      note="历史默认不物理删除（数据规则 4）：只有 ref_count=0、未钉住、超过宽限期且工程不在回收站才允许删。"),

    # ------------------------------------------------------------------ migration_log
    T("migration_log", "迁移记录", "core",
      "数据模式迁移：从哪个版本到哪个版本、迁移前备份、结果；旧工程必须能打开。",
      ["DAT-006", "NFR-002"],
      [
          ID("迁移 ID"),
          C("project_id", "text", "所属工程（空=全局迁移）", fk="project.id", on_delete="CASCADE"),
          C("from_schema", "int", "源模式版本", nn=True, check="from_schema>=1"),
          C("to_schema", "int", "目标模式版本", nn=True, check="to_schema>=1"),
          C("backup_blob_hash", "hash", "迁移前备份", fk="blob.hash", on_delete="SET NULL"),
          C("result", "text", "结果", nn=True, check="result IN ('ok','failed','partial')"),
          C("detail_json", "json", "逐表迁移明细与失败原因"),
          C("applied_at", "time", "执行时间", nn=True),
      ],
      idx=[Index(["project_id", "applied_at"], note="按工程看迁移历史")],
      chk=[("to_schema > from_schema", "只允许向上迁移")],
      note="迁移前必须备份（DAT-006）；失败要能停在源版本，不允许半迁移。"),

    # ------------------------------------------------------------------ external_file
    T("external_file", "外部文件指纹", "core",
      "用户导入/导出的外部文件路径与哈希；用于“STEP 被外部改动后提示不一致”（DAT-005）。",
      ["DAT-005", "STR-001", "CIR-001", "PRJ-006", "SLC-008"],
      [
          ID("记录 ID"),
          C("project_id", "text", "所属工程", nn=True, fk="project.id", on_delete="CASCADE"),
          C("role", "text", "文件角色", nn=True,
            check="role IN ('step','board','netlist','coord','gcode','package','report','other')"),
          C("path", "text", "文件路径（工程内相对或绝对）", nn=True),
          C("size_bytes", "int", "字节数", check="size_bytes IS NULL OR size_bytes>=0"),
          C("mtime", "time", "文件修改时间"),
          C("hash_at_import", "hash", "导入时哈希", nn=True),
          C("hash_last_seen", "hash", "最近一次校验看到的哈希"),
          C("format", "text", "格式标识", default="''", note="如 AP214 / kicad_pcb / gerber-x2"),
          C("verify_status", "text", "校验状态", nn=True, default="'ok'",
            check="verify_status IN ('ok','missing','mismatch','unverified')"),
          C("imported_at", "time", "导入时间", nn=True),
      ],
      idx=[
          Index(["project_id", "role"], note="按角色取源文件"),
          Index(["project_id", "verify_status"], name="idx_extfile_bad",
                where="verify_status<>'ok'", note="打开工程时只校验异常项"),
      ],
      chk=[("path NOT LIKE '/%' AND path NOT LIKE '../%' AND path NOT LIKE '%/../%' AND path NOT LIKE '%/..'"
            " AND path NOT LIKE '\\%' AND path NOT LIKE '..\\%' AND path NOT LIKE '%\\..\\%'"
            " AND path NOT LIKE '%\\..' AND path NOT LIKE '_:%'",
            "禁止绝对路径、盘符与路径穿越（NFR-005）；\"rev..2/board.step\" 这类合法名不被误伤")],
      note="外部文件不是 blob：它归用户所有、可被外部改动，因此单独记录“当时哈希/最近哈希”。"),

    # ------------------------------------------------------------------ app_setting
    T("app_setting", "应用设置", "core",
      "键值设置：默认单位、自动保存间隔、云开关、数据目录、日志级别。",
      ["PRJ-009", "AGT-012", "DAT-007", "NFR-006"],
      [
          C("setting_key", "text", "设置键", nn=True, pk=True, check="length(setting_key) BETWEEN 1 AND 64"),
          C("value_json", "json", "设置值（JSON）", nn=True),
          C("scope", "text", "作用域", nn=True, default="'global'", check="scope IN ('global','project')"),
          C("updated_at", "time", "修改时间", nn=True),
      ],
      note="不建 secrets 表：云端密钥走系统凭据库，禁止落工程与日志（DAT-007）。"),
]
