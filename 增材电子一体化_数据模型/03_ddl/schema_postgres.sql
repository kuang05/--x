-- ============================================================================
-- 增材电子一体化软件 · 数据模型 DDL（PostgreSQL 14+）
-- 由 tools/gen_ddl.py 从 schema/ 自动生成，请勿手改。
-- 生成日期：2026-10-26
-- 说明：时间戳用 timestamptz（写入方必须传 UTC）；JSON 用 jsonb；
--       循环外键（project↔version）用 ALTER TABLE 追加，其余内联。
-- ============================================================================

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '%', TG_ARGV[0];
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END; $$ LANGUAGE plpgsql;


-- ==========================================================================
-- 表 mrc_rule_version｜规则版本
-- 一组阈值的快照头：谁在什么时候改了哪套规则。阈值只从属于版本，不放在规则本体上。
-- 依据需求：MRC-011, MRC-008, 3.4 首版默认阈值
-- 建模说明：新增规则版本 = 插入新行 + 把旧行 is_active 置 0；旧行不修改阈值，保证历史分数可解释（MRC-011）。
-- ==========================================================================
CREATE TABLE mrc_rule_version (
    -- 规则版本 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 版本标签（如 v1 首版基线）
    label text NOT NULL CHECK (length(trim(label))>0),
    -- 变更说明
    note text DEFAULT '',
    -- 创建者
    created_by text NOT NULL DEFAULT 'local',
    -- 被替代的上一版
    supersedes_id text REFERENCES mrc_rule_version(id) ON DELETE SET NULL,
    -- 阈值集合摘要（重放可比性）
    snapshot_hash char(64) CHECK (length(snapshot_hash)=64),
    -- 是否当前生效
    is_active boolean NOT NULL DEFAULT true,
    -- 生效时间
    created_at timestamptz NOT NULL,
    -- 规则版本不能自我替代
    CHECK (supersedes_id IS NULL OR supersedes_id <> id)
);

-- ==========================================================================
-- 表 version｜提交版本
-- 用户可见的提交点：只追加、不可篡改。撤销不产生版本，只有保存/提交/应用计划/回滚才产生。
-- 依据需求：PRJ-004, PRJ-005, DAT-002, AGT-005, SLC-010
-- 建模说明：版本内容不存全量快照：对象级差异在 version_item，几何/网格/路径在 blob，周期快照在 snapshot。
-- ==========================================================================
CREATE TABLE version (
    -- 版本 ID（UUIDv7）
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text NOT NULL,
    -- 工程内版本号（从 0 递增）（排序只用 version_no，不用时间戳（同秒或时钟回拨会乱序））
    version_no integer NOT NULL CHECK (version_no>=0),
    -- 父版本
    parent_version_id text REFERENCES version(id) ON DELETE RESTRICT,
    -- 版本产生方式
    kind text NOT NULL DEFAULT 'manual' CHECK (kind IN ('initial','manual','agent','rollback','import')),
    -- 作者类型
    author_kind text NOT NULL DEFAULT 'human' CHECK (author_kind IN ('human','agent','system')),
    -- 作者标识（单用户软件不建用户表，只区分人/Agent/系统（见冲突 C14））
    author_name text NOT NULL DEFAULT 'local',
    -- 修改原因（用户可读）
    reason text NOT NULL DEFAULT '',
    -- 提交前 MRC 分数
    score_before double precision CHECK (score_before IS NULL OR (score_before>=0 AND score_before<=100)),
    -- 提交后 MRC 分数
    score_after double precision CHECK (score_after IS NULL OR (score_after>=0 AND score_after<=100)),
    -- 评分所用规则版本（分数快照的规则版本；重评分不改这里，另写 mrc_run（MRC-011））
    rule_version_id text REFERENCES mrc_rule_version(id) ON DELETE SET NULL,
    -- 变更摘要
    summary text DEFAULT '',
    -- 创建时间
    created_at timestamptz NOT NULL,
    -- 版本不能以自身为父
    CHECK (parent_version_id IS NULL OR parent_version_id <> id),
    -- 初始版本必须是 0 号
    CHECK (kind <> 'initial' OR version_no = 0)
);

-- ==========================================================================
-- 表 project｜工程
-- 一个项目的根容器；指向当前提交版本，并记录数据目录与导入来源。
-- 依据需求：PRJ-001, PRJ-002, PRJ-007, PRJ-008, PRJ-009, DAT-001, DAT-006
-- 建模说明：单库存全部工程（app.db），大文件走 CAS 目录；这样最近工程列表与跨工程去重都不必跨库查询。
-- ==========================================================================
CREATE TABLE project (
    -- 工程 ID（UUIDv7）
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 工程名称
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    -- 显示单位（全库统一按 mm 存储；inch 仅作显示与导入换算（PRJ-008））
    unit text NOT NULL DEFAULT 'mm' CHECK (unit IN ('mm','inch')),
    -- 幅面宽（mm）
    sheet_w_mm double precision NOT NULL DEFAULT 200 CHECK (sheet_w_mm>0 AND sheet_w_mm<=400),
    -- 幅面高（mm）
    sheet_h_mm double precision NOT NULL DEFAULT 200 CHECK (sheet_h_mm>0 AND sheet_h_mm<=400),
    -- 坐标原点定义
    origin_kind text NOT NULL DEFAULT 'board_bottom_left' CHECK (origin_kind IN ('board_bottom_left','custom')),
    -- 自定义原点 X（mm）
    origin_x_mm double precision DEFAULT 0,
    -- 自定义原点 Y（mm）
    origin_y_mm double precision DEFAULT 0,
    -- 数据模式版本号
    data_schema_version integer NOT NULL DEFAULT 1 CHECK (data_schema_version>=1),
    -- 当前版本（指向 version.id）（新建工程时先插 project 再插 v0，随后回填；避免循环非空外键）
    current_version_id text REFERENCES version(id) ON DELETE SET NULL,
    -- 工程目录（相对数据根）
    dir_rel_path text NOT NULL,
    -- 工程包来源标识（跨机导入时识别同源工程，避免误合并（PRJ-006））
    package_origin_id text,
    -- 回收站删除时间（非空即在回收站；历史版本不物理删除（数据规则 4））
    deleted_at timestamptz,
    -- 创建时间
    created_at timestamptz NOT NULL,
    -- 最后修改时间
    updated_at timestamptz NOT NULL,
    -- 删除时间晚于创建时间
    CHECK (deleted_at IS NULL OR deleted_at >= created_at)
);

-- ==========================================================================
-- 表 version_item｜版本变更项
-- 一次提交里，每个被改对象一条记录，供“改了哪些对象、分数变化多少”直接查询。
-- 依据需求：PRJ-004, DAT-002, AGT-011
-- 建模说明：before/after 只存对象级字段差异，不存全量文档；单条建议 < 32 KB，超出走 blob。
-- ==========================================================================
CREATE TABLE version_item (
    -- 变更项 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属版本
    version_id text NOT NULL REFERENCES version(id) ON DELETE CASCADE,
    -- 对象类型
    target_kind text NOT NULL CHECK (target_kind IN ('structure','feature','board_outline','circuit','component','pad','net','route','jumper','keepout','slice_set','process_profile','material','plan','defect','attribution','setting')),
    -- 对象 ID（多态引用，无法建外键；用 target_kind 白名单 + 校验脚本兜底）
    target_id text NOT NULL,
    -- 变更类型
    change_kind text NOT NULL CHECK (change_kind IN ('create','update','delete')),
    -- 变更前状态（对象级差分）
    before_json jsonb,
    -- 变更后状态
    after_json jsonb,
    -- 人类可读差异摘要
    diff_summary text DEFAULT ''
);

-- ==========================================================================
-- 表 edit_session｜编辑会话
-- 一次打开到关闭的过程；撤销栈与操作日志的归属容器，也是异常恢复的边界。
-- 依据需求：PRJ-002, PRJ-003, PRJ-005, DAT-001
-- 建模说明：表名用 edit_session 而非 session，避开 SQL 关键字与函数名歧义。
-- ==========================================================================
CREATE TABLE edit_session (
    -- 会话 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 会话起始版本
    start_version_id text REFERENCES version(id) ON DELETE RESTRICT,
    -- 会话结束版本
    end_version_id text REFERENCES version(id) ON DELETE RESTRICT,
    -- 软件版本
    app_version text NOT NULL,
    -- 机器标识（同机生成，不参与云同步）
    machine_id text NOT NULL,
    -- 开始时间
    started_at timestamptz NOT NULL,
    -- 结束时间
    ended_at timestamptz,
    -- 退出方式（非 normal 时下次打开给恢复提示（PRJ-003））
    exit_kind text NOT NULL DEFAULT 'open' CHECK (exit_kind IN ('open','normal','crash','forced')),
    -- 是否已恢复
    recovered boolean NOT NULL DEFAULT false,
    -- 结束不早于开始
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

-- ==========================================================================
-- 表 blob｜内容寻址存储索引
-- 模型、网格、路径、证据、G-code、快照都按 sha256 存文件系统，本表只登记索引与校验状态。
-- 依据需求：DAT-004, DAT-005, DAT-002, SLC-008
-- 建模说明：写入顺序：先落文件并 fsync，再插库；避免库有行而盘无文件。物理删除需显式触发并写 blob_gc_log。
-- ==========================================================================
CREATE TABLE blob (
    -- sha256（小写十六进制）（主键即哈希，天然去重）
    hash char(64) PRIMARY KEY NOT NULL CHECK (length(hash)=64),
    -- 摘要算法
    algo text NOT NULL DEFAULT 'sha256' CHECK (algo IN ('sha256')),
    -- 内容类别
    kind text NOT NULL CHECK (kind IN ('step','mesh','route','path','evidence','gcode','snapshot','preview','package','other')),
    -- 字节数
    byte_size integer NOT NULL CHECK (byte_size>=0),
    -- 相对路径（blobs/ab/cd/<hash>）
    rel_path text NOT NULL,
    -- 引用计数（缓存值）（可随时重算；正确性不依赖它，只用于 GC 筛选）
    ref_count integer NOT NULL DEFAULT 0 CHECK (ref_count>=0),
    -- 校验状态
    verify_status text NOT NULL DEFAULT 'unverified' CHECK (verify_status IN ('unverified','ok','missing','mismatch')),
    -- 最近校验时间
    last_verified_at timestamptz,
    -- 钉住（不参与 GC）
    is_pinned boolean NOT NULL DEFAULT false,
    -- GC 宽限截止
    gc_graced_until timestamptz,
    -- 写入时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 operation｜操作日志（撤销/重做载体）
-- 每次成功修改追加一条；撤销/重做也是追加一条指向原操作。版本号全程不变。
-- 依据需求：PRJ-003, PRJ-005, PRJ-004, NFR-002
-- 建模说明：撤销栈可能超过 20 步（PRJ-005 要求至少 20），保留全量；会话结束时按策略归档。
-- ==========================================================================
CREATE TABLE operation (
    -- 操作 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属会话
    session_id text NOT NULL REFERENCES edit_session(id) ON DELETE CASCADE,
    -- 会话内序号（从 1 递增）
    seq integer NOT NULL CHECK (seq>=1),
    -- 操作类型
    op_type text NOT NULL CHECK (op_type IN ('create','update','delete','undo','redo','import','apply_plan','rollback')),
    -- 对象类型
    target_kind text NOT NULL,
    -- 对象 ID
    target_id text,
    -- 正向操作内容
    payload_json jsonb,
    -- 反向操作内容（撤销用）
    inverse_json jsonb,
    -- 反向操作过大时存 blob
    inverse_blob_hash char(64) CHECK (length(inverse_blob_hash)=64) REFERENCES blob(hash) ON DELETE SET NULL,
    -- 撤销/重做指向的原操作
    undo_of_op_id text REFERENCES operation(id) ON DELETE RESTRICT,
    -- 该操作最终落入的版本（操作发生时通常为空，提交时回填）
    version_id text REFERENCES version(id) ON DELETE SET NULL,
    -- 发起者类型
    author_kind text NOT NULL DEFAULT 'human' CHECK (author_kind IN ('human','agent','system')),
    -- 发生时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 snapshot｜快照
-- 会话恢复快照与版本快照。快照不是版本：只用于加速恢复与备份，不进入版本链。
-- 依据需求：PRJ-002, PRJ-003, DAT-004, NFR-002
-- 建模说明：恢复算法：取 op_seq 最大的快照 → 重放 operation 到目标点。快照缺失时从初始版本重放（PRJ-002）。
-- ==========================================================================
CREATE TABLE snapshot (
    -- 快照 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 所属会话（scope=session 时必填）
    session_id text REFERENCES edit_session(id) ON DELETE CASCADE,
    -- 快照用途
    scope text NOT NULL CHECK (scope IN ('session','version','backup')),
    -- 对应版本（scope=version/backup）
    version_id text REFERENCES version(id) ON DELETE SET NULL,
    -- 快照覆盖到的操作序号
    op_seq integer CHECK (op_seq IS NULL OR op_seq>=0),
    -- 快照内容（CAS）
    blob_hash char(64) NOT NULL CHECK (length(blob_hash)=64) REFERENCES blob(hash) ON DELETE RESTRICT,
    -- 字节数
    byte_size integer NOT NULL CHECK (byte_size>=0),
    -- 生成时间
    created_at timestamptz NOT NULL,
    -- 会话快照必须绑定会话
    CHECK (scope <> 'session' OR session_id IS NOT NULL)
);

-- ==========================================================================
-- 表 blob_gc_log｜存储回收日志
-- 内容寻址存储的回收记录：扫描数、实际删除数、释放字节；默认 dry-run，物理删除需显式触发。
-- 依据需求：DAT-004, 4.2 数据规则 4, 4.2 数据规则 5
-- 建模说明：历史默认不物理删除（数据规则 4）：只有 ref_count=0、未钉住、超过宽限期且工程不在回收站才允许删。
-- ==========================================================================
CREATE TABLE blob_gc_log (
    -- 日志 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 扫描 blob 数
    scanned integer NOT NULL DEFAULT 0 CHECK (scanned>=0),
    -- 实际删除数
    deleted integer NOT NULL DEFAULT 0 CHECK (deleted>=0),
    -- 释放字节数
    freed_bytes integer NOT NULL DEFAULT 0 CHECK (freed_bytes>=0),
    -- 是否仅演练
    dry_run boolean NOT NULL DEFAULT true,
    -- 被删除的哈希清单
    deleted_hashes_json jsonb,
    -- 说明（触发方式、宽限期）
    note text DEFAULT '',
    -- 执行时间
    created_at timestamptz NOT NULL,
    -- 删除数不能超过扫描数
    CHECK (deleted <= scanned)
);

-- ==========================================================================
-- 表 migration_log｜迁移记录
-- 数据模式迁移：从哪个版本到哪个版本、迁移前备份、结果；旧工程必须能打开。
-- 依据需求：DAT-006, NFR-002
-- 建模说明：迁移前必须备份（DAT-006）；失败要能停在源版本，不允许半迁移。
-- ==========================================================================
CREATE TABLE migration_log (
    -- 迁移 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程（空=全局迁移）
    project_id text REFERENCES project(id) ON DELETE CASCADE,
    -- 源模式版本
    from_schema integer NOT NULL CHECK (from_schema>=1),
    -- 目标模式版本
    to_schema integer NOT NULL CHECK (to_schema>=1),
    -- 迁移前备份
    backup_blob_hash char(64) CHECK (length(backup_blob_hash)=64) REFERENCES blob(hash) ON DELETE SET NULL,
    -- 结果
    result text NOT NULL CHECK (result IN ('ok','failed','partial')),
    -- 逐表迁移明细与失败原因
    detail_json jsonb,
    -- 执行时间
    applied_at timestamptz NOT NULL,
    -- 只允许向上迁移
    CHECK (to_schema > from_schema)
);

-- ==========================================================================
-- 表 external_file｜外部文件指纹
-- 用户导入/导出的外部文件路径与哈希；用于“STEP 被外部改动后提示不一致”（DAT-005）。
-- 依据需求：DAT-005, STR-001, CIR-001, PRJ-006, SLC-008
-- 建模说明：外部文件不是 blob：它归用户所有、可被外部改动，因此单独记录“当时哈希/最近哈希”。
-- ==========================================================================
CREATE TABLE external_file (
    -- 记录 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 文件角色
    role text NOT NULL CHECK (role IN ('step','board','netlist','coord','gcode','package','report','other')),
    -- 文件路径（工程内相对或绝对）
    path text NOT NULL,
    -- 字节数
    size_bytes integer CHECK (size_bytes IS NULL OR size_bytes>=0),
    -- 文件修改时间
    mtime timestamptz,
    -- 导入时哈希
    hash_at_import char(64) NOT NULL CHECK (length(hash_at_import)=64),
    -- 最近一次校验看到的哈希
    hash_last_seen char(64) CHECK (length(hash_last_seen)=64),
    -- 格式标识（如 AP214 / kicad_pcb / gerber-x2）
    format text DEFAULT '',
    -- 校验状态
    verify_status text NOT NULL DEFAULT 'ok' CHECK (verify_status IN ('ok','missing','mismatch','unverified')),
    -- 导入时间
    imported_at timestamptz NOT NULL,
    -- PG 注意：含反斜杠的路径校验在应用层执行（LIKE 默认转义差异）
    -- 禁止绝对路径、盘符与路径穿越（NFR-005）；"rev..2/board.step" 这类合法名不被误伤
    CHECK (path NOT LIKE '/%' AND path NOT LIKE '../%' AND path NOT LIKE '%/../%' AND path NOT LIKE '%/..' AND path NOT LIKE '_:%')
);

-- ==========================================================================
-- 表 app_setting｜应用设置
-- 键值设置：默认单位、自动保存间隔、云开关、数据目录、日志级别。
-- 依据需求：PRJ-009, AGT-012, DAT-007, NFR-006
-- 建模说明：不建 secrets 表：云端密钥走系统凭据库，禁止落工程与日志（DAT-007）。
-- ==========================================================================
CREATE TABLE app_setting (
    -- 设置键
    setting_key text PRIMARY KEY NOT NULL CHECK (length(setting_key) BETWEEN 1 AND 64),
    -- 设置值（JSON）
    value_json jsonb NOT NULL,
    -- 作用域
    scope text NOT NULL DEFAULT 'global' CHECK (scope IN ('global','project')),
    -- 修改时间
    updated_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 structure｜结构模型
-- 一次 STEP 导入的结果：源文件指纹、单位换算、网格 blob、包围盒与导入状态。
-- 依据需求：STR-001, STR-003, STR-008, STR-009, DAT-005
-- 建模说明：容差变更即产生新网格 blob（内容寻址），旧网格仍可追溯；STR-008 要求显示与切片同源。
-- ==========================================================================
CREATE TABLE structure (
    -- 结构 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 源 STEP 文件指纹
    source_file_id text REFERENCES external_file(id) ON DELETE SET NULL,
    -- STEP 协议
    step_schema text NOT NULL DEFAULT 'other' CHECK (step_schema IN ('AP203','AP214','AP242','other')),
    -- 实体形态
    entity_kind text NOT NULL DEFAULT 'single' CHECK (entity_kind IN ('single','assembly')),
    -- 装配体是否已展平
    flattened boolean NOT NULL DEFAULT false,
    -- 源文件单位
    unit_in text NOT NULL DEFAULT 'unknown' CHECK (unit_in IN ('mm','inch','unknown')),
    -- 到 mm 的换算系数
    unit_scale double precision NOT NULL DEFAULT 1 CHECK (unit_scale>0),
    -- 网格（CAS）（显示与切片共用同一份网格（STR-008））
    mesh_blob_hash char(64) CHECK (length(mesh_blob_hash)=64) REFERENCES blob(hash) ON DELETE RESTRICT,
    -- 网格容差（默认 0.02）
    mesh_tolerance_mm double precision NOT NULL DEFAULT 0.02 CHECK (mesh_tolerance_mm>0 AND mesh_tolerance_mm<=1),
    -- 三角面数
    triangle_count integer CHECK (triangle_count IS NULL OR triangle_count>=0),
    -- 包围盒 X 最小
    bbox_min_x_mm double precision,
    -- 包围盒 Y 最小
    bbox_min_y_mm double precision,
    -- 包围盒 Z 最小
    bbox_min_z_mm double precision,
    -- 包围盒 X 最大
    bbox_max_x_mm double precision,
    -- 包围盒 Y 最大
    bbox_max_y_mm double precision,
    -- 包围盒 Z 最大
    bbox_max_z_mm double precision,
    -- 导入状态
    import_status text NOT NULL DEFAULT 'pending' CHECK (import_status IN ('pending','ok','failed','partial')),
    -- 导入耗时（毫秒）
    import_ms integer CHECK (import_ms IS NULL OR import_ms>=0),
    -- 导入时间
    imported_at timestamptz,
    -- 创建时间
    created_at timestamptz NOT NULL,
    -- 包围盒 X 有序
    CHECK (bbox_min_x_mm IS NULL OR bbox_max_x_mm IS NULL OR bbox_min_x_mm <= bbox_max_x_mm),
    -- 包围盒 Y 有序
    CHECK (bbox_min_y_mm IS NULL OR bbox_max_y_mm IS NULL OR bbox_min_y_mm <= bbox_max_y_mm),
    -- 包围盒 Z 有序
    CHECK (bbox_min_z_mm IS NULL OR bbox_max_z_mm IS NULL OR bbox_min_z_mm <= bbox_max_z_mm)
);

-- ==========================================================================
-- 表 board_outline｜板框
-- 板框几何与来源；无板框时禁止切片（STR-007）。
-- 依据需求：STR-007, SLC-004, CIR-004
-- 建模说明：识别失败允许手绘矩形/多边形，kind 记录来源；切片前必须 is_valid=1。
-- ==========================================================================
CREATE TABLE board_outline (
    -- 板框 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属结构
    structure_id text NOT NULL REFERENCES structure(id) ON DELETE CASCADE,
    -- 来源方式
    kind text NOT NULL CHECK (kind IN ('auto','manual_rect','manual_polygon')),
    -- 板框几何（CAS，折线点序）
    geometry_blob_hash char(64) NOT NULL CHECK (length(geometry_blob_hash)=64) REFERENCES blob(hash) ON DELETE RESTRICT,
    -- 面积（mm²）
    area_mm2 double precision CHECK (area_mm2 IS NULL OR area_mm2>0),
    -- 是否闭合有效
    is_valid boolean NOT NULL DEFAULT true,
    -- 说明/失败原因
    note text DEFAULT '',
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 feature｜特征（布尔结果）
-- 已接受或待接受的加/减体特征；启用/停用、排序、软删除。
-- 依据需求：STR-005, STR-006, SLC-012
-- 建模说明：首版只存特征与预览，不执行布尔；执行/重建为 V1（STR-005）。
-- ==========================================================================
CREATE TABLE feature (
    -- 特征 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属结构
    structure_id text NOT NULL REFERENCES structure(id) ON DELETE CASCADE,
    -- 加体或减体
    kind text NOT NULL CHECK (kind IN ('add','subtract')),
    -- 体类型
    shape text NOT NULL CHECK (shape IN ('box','cylinder','prism','other')),
    -- 几何参数
    params_json jsonb NOT NULL,
    -- 预览网格
    preview_blob_hash char(64) CHECK (length(preview_blob_hash)=64) REFERENCES blob(hash) ON DELETE SET NULL,
    -- 状态（pending 时切片须给提示（STR-006））
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','disabled')),
    -- 排序号
    sort_order integer NOT NULL DEFAULT 0,
    -- 说明
    note text DEFAULT '',
    -- 软删除时间
    deleted_at timestamptz,
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 bool_candidate｜布尔建议候选
-- 面分析产生的加/减体候选：参数可调、可预览、可接受或拒绝；拒绝后模型与分数不变。
-- 依据需求：STR-005, STR-004, T03, T04
-- 建模说明：与 feature 分开：候选是“建议”，feature 是“已接受”的特征；避免把未接受的东西混进特征树。
-- ==========================================================================
CREATE TABLE bool_candidate (
    -- 候选 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属结构
    structure_id text NOT NULL REFERENCES structure(id) ON DELETE CASCADE,
    -- 运算
    op text NOT NULL CHECK (op IN ('add','subtract')),
    -- 体类型
    shape text NOT NULL CHECK (shape IN ('box','cylinder','prism','other')),
    -- 候选参数（位置/尺寸/朝向）
    params_json jsonb NOT NULL,
    -- 预览网格
    preview_blob_hash char(64) CHECK (length(preview_blob_hash)=64) REFERENCES blob(hash) ON DELETE SET NULL,
    -- 处置状态
    status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','previewed','accepted','rejected')),
    -- 拒绝原因
    reject_reason text DEFAULT '',
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 diagnostic｜诊断记录
-- 结构/网格/切片/导出/规则各环节的统一诊断：诊断码、严重度、定位与上下文。
-- 依据需求：STR-002, SLC-012, UI-005, UI-010, NFR-002
-- 建模说明：诊断码不建物理字典表，统一进 dictionary_entry(domain='diagnostic')，便于 UI-010 离线检索。
-- ==========================================================================
CREATE TABLE diagnostic (
    -- 诊断 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 发生环节
    scope text NOT NULL CHECK (scope IN ('structure','mesh','import','slice','gcode','export','rule','print')),
    -- 对象类型（如 structure/route/slice_set；用于定位到对象）
    target_kind text DEFAULT '',
    -- 对象 ID
    target_id text,
    -- 诊断码（对应 dictionary_entry(domain='diagnostic')）
    code text NOT NULL,
    -- 严重度
    severity text NOT NULL DEFAULT 'error' CHECK (severity IN ('info','warn','error','fatal')),
    -- 中文说明
    message text NOT NULL,
    -- 上下文（数值、部位、建议）
    detail_json jsonb,
    -- 层号（切片相关）
    layer_no integer,
    -- 位置 X
    x_mm double precision,
    -- 位置 Y
    y_mm double precision,
    -- 发生时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 circuit｜电路（板文件导入结果）
-- 一次板文件导入的结果集合：源文件、单位、板尺寸、对象计数与导入摘要哈希。
-- 依据需求：CIR-001, CIR-011, DAT-003, T06
-- 建模说明：纯网表无焊盘坐标时 source_format='netlist+coord'，只做连通性检查，不允许直接布线（约束 4）。
-- ==========================================================================
CREATE TABLE circuit (
    -- 电路 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 板文件指纹
    board_file_id text REFERENCES external_file(id) ON DELETE SET NULL,
    -- 网表指纹（纯网表场景）
    netlist_file_id text REFERENCES external_file(id) ON DELETE SET NULL,
    -- 坐标表指纹（纯网表场景）
    coord_file_id text REFERENCES external_file(id) ON DELETE SET NULL,
    -- 源格式
    source_format text NOT NULL DEFAULT 'kicad_pcb' CHECK (source_format IN ('kicad_pcb','netlist','netlist+coord','other')),
    -- 源单位
    unit_in text NOT NULL DEFAULT 'mm' CHECK (unit_in IN ('mm','inch')),
    -- 板宽（mm）
    board_w_mm double precision CHECK (board_w_mm IS NULL OR board_w_mm>0),
    -- 板高（mm）
    board_h_mm double precision CHECK (board_h_mm IS NULL OR board_h_mm>0),
    -- 器件数
    component_count integer NOT NULL DEFAULT 0 CHECK (component_count>=0),
    -- 焊盘数
    pad_count integer NOT NULL DEFAULT 0 CHECK (pad_count>=0),
    -- 网络数
    net_count integer NOT NULL DEFAULT 0 CHECK (net_count>=0),
    -- 导入结果摘要（复核数量一致性）
    import_hash char(64) CHECK (length(import_hash)=64),
    -- 导入时间
    imported_at timestamptz,
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 net_class｜网络类 / 约束组
-- 线宽与间距下限的载体；约束修改的拒绝依据指向这里。
-- 依据需求：CIR-010, MRC-001, MRC-002
-- 建模说明：工艺下限的最终裁定仍以规则版本阈值 + 工艺基线为准；这里存设计侧约束，二者冲突时取更严者。
-- ==========================================================================
CREATE TABLE net_class (
    -- 网络类 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属电路
    circuit_id text NOT NULL REFERENCES circuit(id) ON DELETE CASCADE,
    -- 名称（如 default/power/rf）
    name text NOT NULL,
    -- 线宽下限（mm）
    min_width_mm double precision NOT NULL DEFAULT 0.6 CHECK (min_width_mm>0),
    -- 间距下限（mm）
    min_spacing_mm double precision NOT NULL DEFAULT 0.6 CHECK (min_spacing_mm>0),
    -- 显示色（#RRGGBB）
    color text DEFAULT '#1E88E5',
    -- 说明
    note text DEFAULT '',
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 component｜器件
-- 器件封装与位姿：坐标、旋转、所在面、锁定；飞线由此派生。
-- 依据需求：CIR-002, CIR-003, CIR-005, MRC-004, MRC-005, T06, T07
-- 建模说明：器件移动必须更新 x/y/rot，飞线为派生数据不入库（CIR-003）。
-- ==========================================================================
CREATE TABLE component (
    -- 器件 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属电路
    circuit_id text NOT NULL REFERENCES circuit(id) ON DELETE CASCADE,
    -- 位号（如 R1、U3）
    ref_des text NOT NULL,
    -- 封装名
    footprint text DEFAULT '',
    -- X 坐标（mm）
    x_mm double precision NOT NULL,
    -- Y 坐标（mm）
    y_mm double precision NOT NULL,
    -- 旋转角
    rot_deg integer NOT NULL DEFAULT 0 CHECK (rot_deg IN (0,90,180,270)),
    -- 所在面
    side text NOT NULL DEFAULT 'top' CHECK (side IN ('top','bottom')),
    -- 器件高度（mm）（R4 可达性与 R5 遮挡要用：机头从器件上方经过会碰撞）
    height_mm double precision NOT NULL DEFAULT 1.2 CHECK (height_mm>=0 AND height_mm<=50),
    -- 本体宽（mm）（R4/R5 用真实本体尺寸，不用全局常量）
    body_w_mm double precision NOT NULL DEFAULT 4.0 CHECK (body_w_mm>0 AND body_w_mm<=60),
    -- 本体高（mm）（与 body_w_mm 一起决定遮挡与间隙）
    body_h_mm double precision NOT NULL DEFAULT 4.0 CHECK (body_h_mm>0 AND body_h_mm<=60),
    -- 锁定（禁止移动）
    locked boolean NOT NULL DEFAULT false,
    -- 所属网络类
    net_class_id text REFERENCES net_class(id) ON DELETE SET NULL,
    -- 说明
    note text DEFAULT '',
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 net｜网络
-- 电气连接；网络名在电路内唯一。
-- 依据需求：CIR-003, CIR-009, T07
-- 建模说明：飞线不入库：由焊盘坐标与网络关系实时派生，避免每次移动器件都要重写几何。
-- ==========================================================================
CREATE TABLE net (
    -- 网络 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属电路
    circuit_id text NOT NULL REFERENCES circuit(id) ON DELETE CASCADE,
    -- 网络名
    name text NOT NULL CHECK (length(trim(name))>0),
    -- 网络类
    net_class_id text REFERENCES net_class(id) ON DELETE SET NULL,
    -- 电源网络标记
    is_power boolean NOT NULL DEFAULT false,
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 pad｜焊盘
-- 器件与网络的连接点；可属于网络，纯网表场景允许 net_id 为空。
-- 依据需求：CIR-001, CIR-007, CIR-009, T06
-- 建模说明：焊盘尺寸默认取自工艺基线 1.5×1.0 mm，导入后以源文件为准。
-- ==========================================================================
CREATE TABLE pad (
    -- 焊盘 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属器件
    component_id text NOT NULL REFERENCES component(id) ON DELETE CASCADE,
    -- 所属网络
    net_id text REFERENCES net(id) ON DELETE SET NULL,
    -- 焊盘编号
    pad_no text NOT NULL,
    -- X（mm）
    x_mm double precision NOT NULL,
    -- Y（mm）
    y_mm double precision NOT NULL,
    -- 焊盘宽
    w_mm double precision NOT NULL DEFAULT 1.5 CHECK (w_mm>0),
    -- 焊盘高
    h_mm double precision NOT NULL DEFAULT 1.0 CHECK (h_mm>0),
    -- 形状
    shape text NOT NULL DEFAULT 'rect' CHECK (shape IN ('rect','circle','roundrect','other')),
    -- 所在面
    side text NOT NULL DEFAULT 'top' CHECK (side IN ('top','bottom')),
    -- 是否金属化
    plated boolean NOT NULL DEFAULT false
);

-- ==========================================================================
-- 表 route｜走线
-- 一段走线：二维折线几何 + 层号/面 + 线宽；z 由层与工艺推导，保留覆盖字段供 V1。
-- 依据需求：CIR-004, CIR-008, CIR-010, CIR-011, CIR-012, T07, T08
-- 建模说明：几何不拆点表：一块板可达数十万控制点，拆表会直接打爆切片与界面性能（NFR-001/UI-009）。
-- ==========================================================================
CREATE TABLE route (
    -- 走线 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属电路
    circuit_id text NOT NULL REFERENCES circuit(id) ON DELETE CASCADE,
    -- 所属网络
    net_id text NOT NULL REFERENCES net(id) ON DELETE CASCADE,
    -- 层号（首版 0/1，预留）
    layer_no integer NOT NULL DEFAULT 0 CHECK (layer_no>=0 AND layer_no<=15),
    -- 所在面
    side text NOT NULL DEFAULT 'top' CHECK (side IN ('top','bottom')),
    -- 线宽（mm）
    width_mm double precision NOT NULL CHECK (width_mm>=0.1 AND width_mm<=5),
    -- 折线点序（CAS，float32 x,y）
    geom_blob_hash char(64) NOT NULL CHECK (length(geom_blob_hash)=64) REFERENCES blob(hash) ON DELETE RESTRICT,
    -- 长度（mm）
    length_mm double precision CHECK (length_mm IS NULL OR length_mm>=0),
    -- 点数
    point_count integer CHECK (point_count IS NULL OR point_count>=2),
    -- 起点 X
    start_x_mm double precision,
    -- 起点 Y
    start_y_mm double precision,
    -- 终点 X
    end_x_mm double precision,
    -- 终点 Y
    end_y_mm double precision,
    -- Z 覆盖值（V1 用）
    z_mm double precision,
    -- 软删除时间
    deleted_at timestamptz,
    -- 创建时间
    created_at timestamptz NOT NULL,
    -- 线宽必须为正（CIR-010 拒绝 0.3 mm 以下的判定在应用层）
    CHECK (width_mm>0)
);

-- ==========================================================================
-- 表 jumper｜跳线
-- 同网络两焊盘之间的 0Ω 连接；候选先存为 candidate，应用后转 applied。
-- 依据需求：CIR-007, T09
-- 建模说明：候选不自动应用（CIR-007）；应用后才写 version_item。
-- ==========================================================================
CREATE TABLE jumper (
    -- 跳线 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属电路
    circuit_id text NOT NULL REFERENCES circuit(id) ON DELETE CASCADE,
    -- 所属网络
    net_id text NOT NULL REFERENCES net(id) ON DELETE CASCADE,
    -- 焊盘 A
    pad_a_id text NOT NULL REFERENCES pad(id) ON DELETE CASCADE,
    -- 焊盘 B
    pad_b_id text NOT NULL REFERENCES pad(id) ON DELETE CASCADE,
    -- 等效电阻（0Ω）
    resistance_ohm double precision NOT NULL DEFAULT 0 CHECK (resistance_ohm>=0),
    -- 状态
    status text NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','applied','rejected')),
    -- 创建时间
    created_at timestamptz NOT NULL,
    -- 跳线两端不能是同一焊盘
    CHECK (pad_a_id <> pad_b_id)
);

-- ==========================================================================
-- 表 keepout｜禁布区
-- 自动布线必须避开的区域；失败时需说明被谁挡住。
-- 依据需求：CIR-006, CIR-004
-- 建模说明：自动布线的失败原因要能指到具体遮挡对象：禁布区、焊盘或已布走线（CIR-006）。
-- ==========================================================================
CREATE TABLE keepout (
    -- 禁布区 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属电路
    circuit_id text NOT NULL REFERENCES circuit(id) ON DELETE CASCADE,
    -- 形状
    kind text NOT NULL CHECK (kind IN ('rect','polygon','circle')),
    -- 几何（CAS）
    geometry_blob_hash char(64) NOT NULL CHECK (length(geometry_blob_hash)=64) REFERENCES blob(hash) ON DELETE RESTRICT,
    -- 作用层（all 或层号字符串，如 '0,1'）
    applies_layer text NOT NULL DEFAULT 'all',
    -- 来源说明
    reason text DEFAULT '',
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 copper_zone｜铺铜区
-- 整片铜箔（地/电源平面）：同网络可连，异网络贴得太近即短路。
-- 依据需求：CIR-006, MRC-002
-- 建模说明：铺铜与异网走线重叠即短路；间隔不足归 R2。轮廓按多边形点序列存 blob。
-- ==========================================================================
CREATE TABLE copper_zone (
    -- 铺铜区 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属电路
    circuit_id text NOT NULL REFERENCES circuit(id) ON DELETE CASCADE,
    -- 所属网络
    net_id text NOT NULL REFERENCES net(id) ON DELETE CASCADE,
    -- 层号
    layer_no integer NOT NULL CHECK (layer_no>=0 AND layer_no<=31),
    -- 轮廓多边形（CAS）
    geometry_blob_hash char(64) NOT NULL CHECK (length(geometry_blob_hash)=64) REFERENCES blob(hash) ON DELETE RESTRICT,
    -- 优先级（KiCad zone 的 priority，叠层顺序用）
    priority integer NOT NULL DEFAULT 0,
    -- 已填充（KiCad filled_polygon：0 表示只有轮廓、未铺铜）
    filled boolean NOT NULL DEFAULT true,
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 via｜过孔
-- 层间互连：把两层走线在同一点接通。首版只建电学连接，不建孔壁金属化工艺模型。
-- 依据需求：CIR-004, CIR-008, SLC-004, MRC-002
-- 建模说明：没有过孔时，两层走线只能在焊盘处被视为连通；那是简化，不是工艺事实。
-- ==========================================================================
CREATE TABLE via (
    -- 过孔 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属电路
    circuit_id text NOT NULL REFERENCES circuit(id) ON DELETE CASCADE,
    -- 所属网络
    net_id text NOT NULL REFERENCES net(id) ON DELETE CASCADE,
    -- 位置 X（mm）
    x_mm double precision NOT NULL,
    -- 位置 Y（mm）
    y_mm double precision NOT NULL,
    -- 外径（mm）
    diameter_mm double precision NOT NULL DEFAULT 0.6 CHECK (diameter_mm>=0.2 AND diameter_mm<=3),
    -- 钻孔直径（mm）
    drill_mm double precision NOT NULL DEFAULT 0.3 CHECK (drill_mm>0 AND drill_mm<=2),
    -- 起始层号
    from_layer integer NOT NULL DEFAULT 0 CHECK (from_layer>=0),
    -- 目标层号
    to_layer integer NOT NULL DEFAULT 1 CHECK (to_layer<=15),
    -- 创建时间
    created_at timestamptz NOT NULL,
    -- 目标层必须大于起始层
    CHECK (to_layer > from_layer),
    -- 钻孔必须小于外径（否则没有孔环）
    CHECK (drill_mm < diameter_mm)
);

-- ==========================================================================
-- 表 layout_candidate｜布局候选
-- 自动布局给出的器件摆放方案：可打性分、飞线总长、排序位次；应用后可撤销。
-- 依据需求：CIR-005, T08
-- 建模说明：候选只描述摆放，不写 component 行；应用时才产生新版本并落 version_item（CIR-005 要求可撤销）。
-- ==========================================================================
CREATE TABLE layout_candidate (
    -- 候选 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属电路
    circuit_id text NOT NULL REFERENCES circuit(id) ON DELETE CASCADE,
    -- 排序位次（1 最优）
    rank_no integer NOT NULL CHECK (rank_no>=1),
    -- 可打性分（0–100）
    score double precision CHECK (score IS NULL OR (score>=0 AND score<=100)),
    -- 飞线总长（mm）
    wire_len_mm double precision CHECK (wire_len_mm IS NULL OR wire_len_mm>=0),
    -- 摆放方案（器件 ID → x/y/rot/side）
    placements_json jsonb NOT NULL,
    -- 处置状态
    status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','applied','rejected')),
    -- 应用后产生的版本
    applied_version_id text REFERENCES version(id) ON DELETE SET NULL,
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 process_profile｜工艺配置
-- 可保存/加载的工艺参数集：层高、补偿、速度、出料系数、固化曲线；带参数摘要供重放比对。
-- 依据需求：SLC-003, SLC-011, SLC-005, AGT-008
-- 建模说明：参数变更必须生成新切片，不原地改旧切片（SLC-003）；配置不合法时拒绝切片并给原因（SLC-011）。
-- ==========================================================================
CREATE TABLE process_profile (
    -- 配置 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程（空=全局模板）
    project_id text REFERENCES project(id) ON DELETE CASCADE,
    -- 配置名
    name text NOT NULL,
    -- 参数集（含三层厚度、温度、时间、斜率）
    params_json jsonb NOT NULL,
    -- 参数摘要（重放一致性）
    params_hash char(64) NOT NULL CHECK (length(params_hash)=64),
    -- 是否默认配置
    is_default boolean NOT NULL DEFAULT false,
    -- 是否通过校验
    is_valid boolean NOT NULL DEFAULT true,
    -- 校验失败明细
    validation_json jsonb,
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 tool_geometry｜工具几何
-- 喷嘴几何与安全间隙；喷嘴一换就是新行，可达性检查必须记录用的是哪一行。
-- 依据需求：MRC-004, 1.7 喷头安全间隙, PRT-013
-- 建模说明：工具几何本身就是版本（行长即版本），不另建版本表；mrc_run 固定引用某一行即可保证重放可比（AGT-008）。
-- ==========================================================================
CREATE TABLE tool_geometry (
    -- 工具几何 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 名称（如 0.25 针头）
    name text NOT NULL,
    -- 针头内径（mm）
    nozzle_inner_d_mm double precision NOT NULL CHECK (nozzle_inner_d_mm BETWEEN 0.1 AND 1),
    -- 针尖外径（mm）
    nozzle_outer_d_mm double precision NOT NULL CHECK (nozzle_outer_d_mm>=nozzle_inner_d_mm),
    -- 安全间隙（mm）
    safety_gap_mm double precision NOT NULL DEFAULT 0.5 CHECK (safety_gap_mm>=0),
    -- 喷头包络（多段圆柱/包围盒）
    envelope_json jsonb,
    -- 是否在用
    is_active boolean NOT NULL DEFAULT true,
    -- 说明
    note text DEFAULT '',
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 slice_set｜切片
-- 一次切片结果的头：绑定设计版本与工艺/规则/工具，记录层道次数、总厚、估算与过期标记。
-- 依据需求：SLC-001, SLC-002, SLC-010, SLC-012, SLC-009, T13, T14
-- 建模说明：总厚口径冲突（0.35–0.40 vs 层厚相加 0.44）处理：层厚为唯一真值，合计写入 total_thickness_mm，超目标区间只告警不报错（冲突 C2）。
-- ==========================================================================
CREATE TABLE slice_set (
    -- 切片 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 绑定的设计版本（绑定不可变；设计变更后置 stale，但不删除旧切片（SLC-010））
    design_version_id text NOT NULL REFERENCES version(id) ON DELETE RESTRICT,
    -- 工艺配置
    process_profile_id text NOT NULL REFERENCES process_profile(id) ON DELETE RESTRICT,
    -- 规则版本（切片时生效）
    rule_version_id text REFERENCES mrc_rule_version(id) ON DELETE SET NULL,
    -- 工具几何
    tool_geometry_id text REFERENCES tool_geometry(id) ON DELETE SET NULL,
    -- 切片引擎版本
    engine_version text NOT NULL,
    -- 状态
    status text NOT NULL DEFAULT 'computing' CHECK (status IN ('computing','ready','failed','stale')),
    -- 过期时间（显式记录；不靠“是否等于当前版本”实时推导）
    stale_since timestamptz,
    -- 层厚合计（mm）
    total_thickness_mm double precision CHECK (total_thickness_mm IS NULL OR total_thickness_mm>0),
    -- 目标总厚下限
    target_thickness_min_mm double precision NOT NULL DEFAULT 0.35 CHECK (target_thickness_min_mm>0),
    -- 目标总厚上限
    target_thickness_max_mm double precision NOT NULL DEFAULT 0.40 CHECK (target_thickness_max_mm>=target_thickness_min_mm),
    -- 物理层数
    layer_count integer NOT NULL DEFAULT 3 CHECK (layer_count=3),
    -- 道次数
    pass_count integer NOT NULL DEFAULT 7 CHECK (pass_count IN (7,8)),
    -- 估算（路径长/时间/浆料/成本/假设/首次升温拆分）
    estimate_json jsonb,
    -- 输入摘要（版本+参数+材料批次）
    input_digest char(64) CHECK (length(input_digest)=64),
    -- 创建时间
    created_at timestamptz NOT NULL,
    -- 完成时间
    finished_at timestamptz,
    -- 过期切片必须有 stale_since
    CHECK (status <> 'stale' OR stale_since IS NOT NULL)
);

-- ==========================================================================
-- 表 connectivity_check｜连通性检查
-- 电路或切片阶段的连通性结果：断路/短路计数与逐条定位。
-- 依据需求：CIR-009, SLC-007, T15
-- 建模说明：失败记录也保留：判定为 error 时不覆盖上次 pass，但会阻断导出并提示原因。
-- ==========================================================================
CREATE TABLE connectivity_check (
    -- 检查 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 检查阶段
    scope text NOT NULL CHECK (scope IN ('circuit','slice')),
    -- 被检设计版本
    version_id text NOT NULL REFERENCES version(id) ON DELETE CASCADE,
    -- 被检切片（scope=slice）
    slice_set_id text REFERENCES slice_set(id) ON DELETE CASCADE,
    -- 结论
    result text NOT NULL CHECK (result IN ('pass','fail','error')),
    -- 断路数
    open_count integer NOT NULL DEFAULT 0 CHECK (open_count>=0),
    -- 短路数
    short_count integer NOT NULL DEFAULT 0 CHECK (short_count>=0),
    -- 逐条定位（网络、层、坐标、对象引用）
    findings_json jsonb,
    -- 检查引擎版本
    engine_version text NOT NULL,
    -- 输入摘要（几何+参数）
    input_digest char(64) NOT NULL CHECK (length(input_digest)=64),
    -- 检查时间
    ran_at timestamptz NOT NULL,
    -- 切片检查必须绑定切片
    CHECK (scope <> 'slice' OR slice_set_id IS NOT NULL)
);

-- ==========================================================================
-- 表 mrc_rule_threshold｜规则阈值明细
-- 规则版本下逐条阈值：R1–R7 的判定参数、数值、单位与启用状态。
-- 依据需求：MRC-001, MRC-002, MRC-003, MRC-004, MRC-005, MRC-006, MRC-007, MRC-011
-- 建模说明：首版默认值：R1 0.5 mm、R2 0.5 mm、R3 45°、R4 0.5 mm、R5 无遮挡、R6 5 ℃、R7 0.5 mm。
-- ==========================================================================
CREATE TABLE mrc_rule_threshold (
    -- 所属规则版本
    rule_version_id text NOT NULL REFERENCES mrc_rule_version(id) ON DELETE CASCADE,
    -- 规则码（R1 线宽 / R2 间距 / R3 坡度 / R4 可达性 / R5 遮挡 / R6 温度场 / R7 介电流道）
    rule_code text NOT NULL CHECK (rule_code IN ('R1','R2','R3','R4','R5','R6','R7')),
    -- 参数名
    param_key text NOT NULL CHECK (length(trim(param_key))>0),
    -- 阈值数值
    value_num double precision NOT NULL,
    -- 单位
    unit text NOT NULL DEFAULT 'mm' CHECK (unit IN ('mm','deg','c','count','ratio')),
    -- 是否启用
    enabled boolean NOT NULL DEFAULT true,
    -- 说明
    note text DEFAULT '',
    PRIMARY KEY (rule_version_id, rule_code, param_key)
);

-- ==========================================================================
-- 表 mrc_run｜评分运行
-- 一次可打性检查：分数权威来源。同一版本可被不同规则版本/工具几何重复评，旧结果只追加不改写。
-- 依据需求：MRC-008, MRC-011, PRJ-004, T19
-- 建模说明：分数不存 version.score 列（会破坏不可篡改）；version.score_after 只是提交瞬间的快照，用于列表展示。
-- ==========================================================================
CREATE TABLE mrc_run (
    -- 运行 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 被评版本
    version_id text NOT NULL REFERENCES version(id) ON DELETE CASCADE,
    -- 规则版本
    rule_version_id text NOT NULL REFERENCES mrc_rule_version(id) ON DELETE RESTRICT,
    -- 工具几何
    tool_geometry_id text NOT NULL REFERENCES tool_geometry(id) ON DELETE RESTRICT,
    -- 检查引擎版本
    engine_version text NOT NULL,
    -- 输入摘要（几何+单位+网格容差+规则版本）
    input_digest char(64) NOT NULL CHECK (length(input_digest)=64),
    -- 总分（0–100）
    total_score double precision NOT NULL CHECK (total_score>=0 AND total_score<=100),
    -- 问题条数
    issue_count integer NOT NULL DEFAULT 0 CHECK (issue_count>=0),
    -- 耗时（毫秒）
    duration_ms integer CHECK (duration_ms IS NULL OR duration_ms>=0),
    -- 开始时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 mrc_issue｜违规条目
-- 一条违规：规则、对象、实测值、阈值、严重度、可读原因与建议。
-- 依据需求：MRC-001, MRC-008, MRC-009, MRC-010, T10, T11, T12
-- 建模说明：建议只写文本+动作类型，不落执行状态；执行由 Agent 计划/对比卡片承载（MRC-009）。
-- ==========================================================================
CREATE TABLE mrc_issue (
    -- 问题 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属运行
    run_id text NOT NULL REFERENCES mrc_run(id) ON DELETE CASCADE,
    -- 被评版本（冗余）（冗余用于问题列表热查询，避免每次都 join mrc_run）
    version_id text NOT NULL REFERENCES version(id) ON DELETE CASCADE,
    -- 规则版本（冗余）
    rule_version_id text NOT NULL REFERENCES mrc_rule_version(id) ON DELETE RESTRICT,
    -- 规则码
    rule_code text NOT NULL CHECK (rule_code IN ('R1','R2','R3','R4','R5','R6','R7')),
    -- 严重度
    severity text NOT NULL CHECK (severity IN ('blocker','major','minor','info')),
    -- 违规对象类型
    target_kind text NOT NULL CHECK (target_kind IN ('route','pad','net','component','structure','face','board_outline','slice_path','deposit_pass','other')),
    -- 违规对象 ID
    target_id text DEFAULT '',
    -- 实测值
    measured_num double precision,
    -- 阈值
    threshold_num double precision,
    -- 单位
    unit text DEFAULT 'mm' CHECK (unit IN ('mm','deg','c','count','ratio')),
    -- 为什么违规（中文）
    message text NOT NULL,
    -- 建议动作（不自动执行）
    suggestion_json jsonb,
    -- 层号
    layer_no integer,
    -- 位置 X
    x_mm double precision,
    -- 位置 Y
    y_mm double precision,
    -- 产生时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 material｜材料
-- 材料型号与配方：可焊银浆（A）与热固化介电环氧（B）；停用后不再出现在切片选择列表。
-- 依据需求：SLC-013, SLC-011, 1.7 首版工艺与硬件基线
-- 建模说明：首版不用 UV 材料（约束 3）；配方缺失时拒绝切片（SLC-011/013）。
-- ==========================================================================
CREATE TABLE material (
    -- 材料 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 材料代号（A/B 或自定义）
    code text NOT NULL CHECK (length(trim(code)) BETWEEN 1 AND 32),
    -- 材料名称
    name text NOT NULL,
    -- 类别
    kind text NOT NULL CHECK (kind IN ('silver_paste','dielectric','other')),
    -- 配方（组分、比例、黏度）
    formula_json jsonb,
    -- 启用状态
    is_active boolean NOT NULL DEFAULT true,
    -- 说明
    note text DEFAULT '',
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 material_batch｜材料批次
-- 材料型号 + 批次号 + 到货日期 + 启用状态；批次号进 G-code 头，缺陷可回溯到批次。
-- 依据需求：SLC-008, SLC-013, PRT-009, T16
-- 建模说明：批次在切片提交时冻结进 deposit_pass，导出只读冻结值；换批次必须显式重切（冲突 C12）。
-- ==========================================================================
CREATE TABLE material_batch (
    -- 批次 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属材料
    material_id text NOT NULL REFERENCES material(id) ON DELETE CASCADE,
    -- 批次号
    batch_no text NOT NULL CHECK (length(trim(batch_no))>0),
    -- 到货日期
    arrived_at timestamptz,
    -- 启用状态
    is_active boolean NOT NULL DEFAULT true,
    -- 说明
    note text DEFAULT '',
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 physical_layer｜物理层
-- 下保护层、电路层、上保护层三层：层序、厚度、材料。
-- 依据需求：SLC-001, T13, 1.7 物理层
-- 建模说明：层与道次必须拆两张表：道次 0–2 同属“下保护”，只建一张表无法表达“同一高度两个道次”。
-- ==========================================================================
CREATE TABLE physical_layer (
    -- 物理层 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属切片
    slice_set_id text NOT NULL REFERENCES slice_set(id) ON DELETE CASCADE,
    -- 层序（0 起）
    layer_index integer NOT NULL CHECK (layer_index>=0),
    -- 层类别（下保护 / 电路 / 上保护）
    kind text NOT NULL CHECK (kind IN ('bottom_mask','circuit','top_mask')),
    -- 层厚（mm）
    thickness_mm double precision NOT NULL CHECK (thickness_mm>0),
    -- 主材料
    material_id text REFERENCES material(id) ON DELETE SET NULL,
    -- 说明
    note text DEFAULT ''
);

-- ==========================================================================
-- 表 deposit_pass｜道次
-- 一次材料沉积：同一高度可有介电道次与银浆道次，典型 7 个、银浆分两次填则 8 个。
-- 依据需求：SLC-002, SLC-004, SLC-008, T13, T14
-- 建模说明：层:道次 = 1:N，必须两张表；ER v0.1 只建 Layer 是硬伤（同一高度两个道次无法表达）。
-- ==========================================================================
CREATE TABLE deposit_pass (
    -- 道次 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属切片
    slice_set_id text NOT NULL REFERENCES slice_set(id) ON DELETE CASCADE,
    -- 所属物理层
    physical_layer_id text NOT NULL REFERENCES physical_layer(id) ON DELETE CASCADE,
    -- 道次序（0 起，全局）
    pass_index integer NOT NULL CHECK (pass_index>=0),
    -- 材料
    material_id text NOT NULL REFERENCES material(id) ON DELETE RESTRICT,
    -- 材料批次（切片时冻结）
    material_batch_id text REFERENCES material_batch(id) ON DELETE RESTRICT,
    -- 图案类型
    pattern text NOT NULL CHECK (pattern IN ('solid','dam_and_trench','trench_fill','pad_pit','pad_fill','support','other')),
    -- 道次厚度（mm）
    thickness_mm double precision NOT NULL CHECK (thickness_mm>0),
    -- 道次顶面 Z（mm）
    z_top_mm double precision,
    -- 说明
    note text DEFAULT ''
);

-- ==========================================================================
-- 表 slice_path｜路径段
-- 一段可执行轨迹：挤出或空移，几何放 blob，行上保留长度/包围盒/层高供过滤。
-- 依据需求：SLC-004, SLC-006, SLC-007, SLC-008, NFR-001
-- 建模说明：唯一使用整型主键的表：单切片可达数万段，36 字节 UUID 会显著放大索引；对外仍以 (deposit_pass, seq) 定位。控制点绝不拆行。
-- ==========================================================================
CREATE TABLE slice_path (
    -- 行号主键（整型，性能例外）
    id bigint PRIMARY KEY NOT NULL,
    -- 所属道次
    deposit_pass_id text NOT NULL REFERENCES deposit_pass(id) ON DELETE CASCADE,
    -- 道次内序号（0 起）
    seq integer NOT NULL CHECK (seq>=0),
    -- 路径类型（空移与挤出必须可区分（SLC-004））
    path_kind text NOT NULL CHECK (path_kind IN ('extrude','travel')),
    -- 点序（CAS，float32 x,y,z）
    geom_blob_hash char(64) NOT NULL CHECK (length(geom_blob_hash)=64) REFERENCES blob(hash) ON DELETE RESTRICT,
    -- 点数
    point_count integer NOT NULL CHECK (point_count>=2),
    -- 长度（mm）
    length_mm double precision NOT NULL CHECK (length_mm>=0),
    -- 包围盒 X 最小
    bbox_min_x_mm double precision,
    -- 包围盒 Y 最小
    bbox_min_y_mm double precision,
    -- 包围盒 X 最大
    bbox_max_x_mm double precision,
    -- 包围盒 Y 最大
    bbox_max_y_mm double precision,
    -- 所在高度（mm）
    z_mm double precision CHECK (z_mm IS NULL OR z_mm>=0),
    -- 速度（mm/s）
    speed_mm_s double precision CHECK (speed_mm_s IS NULL OR speed_mm_s>0),
    -- 出料系数
    flow_factor double precision CHECK (flow_factor IS NULL OR flow_factor>=0)
);

-- ==========================================================================
-- 表 thermal_step｜热历程阶段
-- 排程时间轴：预凝胶、银浆定型、层固化、整板终固化、冷却；按道次或按物理层。
-- 依据需求：SLC-005, PRT-004, SLC-009, T14
-- 建模说明：固化中断恢复要按当前温度重算剩余保温时间，因此 hold_s 必须可查、不可只存文本（PRT-004）。
-- ==========================================================================
CREATE TABLE thermal_step (
    -- 阶段 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属切片
    slice_set_id text NOT NULL REFERENCES slice_set(id) ON DELETE CASCADE,
    -- 阶段序（0 起）
    step_index integer NOT NULL CHECK (step_index>=0),
    -- 阶段类型
    stage text NOT NULL CHECK (stage IN ('pre_gel','silver_set','cure','final_cure','cool')),
    -- 作用范围（固化按道次还是按物理层可配，默认按物理层（待确认 9-2））
    target_kind text NOT NULL DEFAULT 'layer' CHECK (target_kind IN ('layer','pass','board')),
    -- 目标层/道次 ID
    target_id text DEFAULT '',
    -- 目标温度（℃）
    temp_c double precision NOT NULL,
    -- 升温斜率（℃/min）
    ramp_c_per_min double precision CHECK (ramp_c_per_min IS NULL OR ramp_c_per_min>0),
    -- 保温时长（秒）
    hold_s integer NOT NULL DEFAULT 0 CHECK (hold_s>=0),
    -- 说明
    note text DEFAULT '',
    -- 温度在工艺范围内
    CHECK (temp_c BETWEEN 0 AND 300),
    -- 层固化升温不超过 3 ℃/min（1.7）
    CHECK (stage <> 'cure' OR ramp_c_per_min IS NULL OR ramp_c_per_min <= 3)
);

-- ==========================================================================
-- 表 gcode_export｜G-code 导出
-- 一次导出：内容哈希（规范化）与文件哈希（落盘字节）双记录；批次清单摘要供反查。
-- 依据需求：SLC-008, SLC-007, T16, AGT-008, T20
-- 建模说明：只存 file_hash 会让“两次导出哈希一致”恒假（文件内常有时间戳）；验收一律看 content_hash。
-- ==========================================================================
CREATE TABLE gcode_export (
    -- 导出 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属切片
    slice_set_id text NOT NULL REFERENCES slice_set(id) ON DELETE CASCADE,
    -- 规范化内容哈希（验收口径）（剔除时间戳/会话 ID/绝对路径，浮点统一 %.3f）
    content_hash char(64) NOT NULL CHECK (length(content_hash)=64),
    -- 落盘文件字节哈希
    file_hash char(64) NOT NULL CHECK (length(file_hash)=64),
    -- G-code 文件（CAS）
    blob_hash char(64) CHECK (length(blob_hash)=64) REFERENCES blob(hash) ON DELETE SET NULL,
    -- 字节数
    byte_size integer CHECK (byte_size IS NULL OR byte_size>=0),
    -- G-code 头（版本、材料、批次、层/道次、烘箱阶段）
    header_json jsonb,
    -- 材料批次清单摘要（一次查询反查批次）
    material_manifest_hash char(64) CHECK (length(material_manifest_hash)=64),
    -- 导出器版本
    engine_version text NOT NULL,
    -- 导出时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 device｜设备
-- 真机或虚拟打印机：连接方式、地址、状态与能力档案。
-- 依据需求：PRT-001, PRT-002, 5.3 设备接口
-- 建模说明：虚拟打印机是 device 的一种（conn_kind='virtual'），不是另一套模型。
-- ==========================================================================
CREATE TABLE device (
    -- 设备 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程（空=全局设备）
    project_id text REFERENCES project(id) ON DELETE CASCADE,
    -- 设备名
    name text NOT NULL,
    -- 连接方式
    conn_kind text NOT NULL CHECK (conn_kind IN ('serial','lan','virtual')),
    -- 地址（串口/主机:端口）
    address text DEFAULT '',
    -- 连接与运行状态（与 5.3 的状态枚举对齐）
    status text NOT NULL DEFAULT 'offline' CHECK (status IN ('offline','idle','precheck','homing','calibrating','printing','paused','fault','complete')),
    -- 最近连接时间
    last_connected_at timestamptz,
    -- 能力档案（幅面、轴数、喷头数、温度上限）
    profile_json jsonb,
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 device_calibration｜喷头标定记录
-- A/B 喷头偏移与各自 Z 高、热态标定时间与结果；超差禁止开始打印。
-- 依据需求：PRT-013, 1.7 双喷头对位, PRT-002
-- 建模说明：标定是事件而非几何属性：每次标定追加一行，打印前取最近一次 is_within_tolerance=1 的记录。
-- ==========================================================================
CREATE TABLE device_calibration (
    -- 标定 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属设备
    device_id text NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    -- A 喷头 X 偏移
    nozzle_a_dx_mm double precision NOT NULL DEFAULT 0,
    -- A 喷头 Y 偏移
    nozzle_a_dy_mm double precision NOT NULL DEFAULT 0,
    -- A 喷头 Z 偏移
    nozzle_a_dz_mm double precision NOT NULL DEFAULT 0,
    -- B 喷头 X 偏移
    nozzle_b_dx_mm double precision NOT NULL DEFAULT 0,
    -- B 喷头 Y 偏移
    nozzle_b_dy_mm double precision NOT NULL DEFAULT 0,
    -- B 喷头 Z 偏移
    nozzle_b_dz_mm double precision NOT NULL DEFAULT 0,
    -- 热态标定温度（℃）
    hot_state_temp_c double precision NOT NULL DEFAULT 80 CHECK (hot_state_temp_c BETWEEN 0 AND 300),
    -- 允许偏差（默认 0.1）
    tolerance_mm double precision NOT NULL DEFAULT 0.1 CHECK (tolerance_mm>0),
    -- 是否在允差内
    is_within_tolerance boolean NOT NULL DEFAULT true,
    -- 标定结果（实测点、残差）
    result_json jsonb,
    -- 标定时间
    measured_at timestamptz NOT NULL,
    -- 操作者
    operator text DEFAULT 'local'
);

-- ==========================================================================
-- 表 print_job｜打印作业
-- 一次打印：绑定切片与 G-code、设备、状态机与阶段；作业历史由此派生。
-- 依据需求：PRT-002, PRT-003, PRT-010, PRT-014, T17, T18
-- 建模说明：status 与 phase 双字段：固化中暂停的语义与打印中暂停完全不同，不能塞进一个字段（PRT-004）。
-- ==========================================================================
CREATE TABLE print_job (
    -- 作业 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 所用切片
    slice_set_id text NOT NULL REFERENCES slice_set(id) ON DELETE RESTRICT,
    -- 所用 G-code 导出
    gcode_export_id text REFERENCES gcode_export(id) ON DELETE RESTRICT,
    -- 执行设备
    device_id text NOT NULL REFERENCES device(id) ON DELETE RESTRICT,
    -- 作业状态
    status text NOT NULL DEFAULT 'created' CHECK (status IN ('created','precheck_failed','ready','running','paused','interrupted','curing','cooling','completed','failed','aborted')),
    -- 当前阶段（与 status 正交：暂停要看是在打印中还是固化中（PRT-003/004））
    phase text NOT NULL DEFAULT 'idle' CHECK (phase IN ('idle','printing','curing','cooling')),
    -- 开工校验用（规范化哈希）
    gcode_content_hash char(64) NOT NULL CHECK (length(gcode_content_hash)=64),
    -- 总层数
    layer_total integer CHECK (layer_total IS NULL OR layer_total>0),
    -- 当前层号
    layer_current integer DEFAULT 0 CHECK (layer_current>=0),
    -- 打印前检查逐项结果
    precheck_json jsonb,
    -- 结果摘要（耗时、告警、结论）
    result_json jsonb,
    -- 开始时间
    started_at timestamptz,
    -- 结束时间
    ended_at timestamptz,
    -- 耗时（秒）
    duration_s integer CHECK (duration_s IS NULL OR duration_s>=0),
    -- 创建时间
    created_at timestamptz NOT NULL,
    -- 结束不早于开始
    CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at),
    -- 完成的作业必须有结束时间
    CHECK (status <> 'completed' OR ended_at IS NOT NULL)
);

-- ==========================================================================
-- 表 print_event｜打印事件
-- 统一事件时间线：状态变更、前置检查逐项、故障、动作、参数调整。
-- 依据需求：PRT-002, PRT-005, PRT-006, PRT-011, PRT-012, T17, T18
-- 建模说明：故障与缺陷分开：事件记录“发生了什么”，缺陷记录“打坏了什么”（PRT-005 vs PRT-008）。
-- ==========================================================================
CREATE TABLE print_event (
    -- 事件 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属作业
    print_job_id text NOT NULL REFERENCES print_job(id) ON DELETE CASCADE,
    -- 作业内序号（从 1 递增）
    seq integer NOT NULL CHECK (seq>=1),
    -- 事件类型（skip = 跳层/跳段（PRT-012））
    event_kind text NOT NULL CHECK (event_kind IN ('status','precheck','fault','action','param','skip','note')),
    -- 事件码/故障码（故障：overheat/clog/broken_line/material_out/oven_underheat/alignment_drift）
    code text DEFAULT '',
    -- 中文说明
    message text NOT NULL,
    -- 详情（温度、位置、处置动作）
    detail_json jsonb,
    -- 发生层号
    layer_no integer,
    -- 发生道次
    pass_index integer CHECK (pass_index IS NULL OR pass_index>=0),
    -- 发生路径段序号
    path_seq integer CHECK (path_seq IS NULL OR path_seq>=0),
    -- 发生时间
    at timestamptz NOT NULL,
    -- 跳层/跳段必须记录层号与原因码（PRT-012）
    CHECK (event_kind <> 'skip' OR (layer_no IS NOT NULL AND code <> ''))
);

-- ==========================================================================
-- 表 print_interruption｜暂停与续打
-- 两类暂停（打印中/固化中）与断电续打：恢复点、温度、剩余保温时间、坐标可信度校验。
-- 依据需求：PRT-003, PRT-004, PRT-014, T17
-- 建模说明：两种暂停的差别要落到列上：打印中恢复需重校坐标（coord_check_json），固化中恢复需重算剩余保温（remaining_hold_s）。
-- ==========================================================================
CREATE TABLE print_interruption (
    -- 中断 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属作业
    print_job_id text NOT NULL REFERENCES print_job(id) ON DELETE CASCADE,
    -- 中断类型（motion_pause 打印中暂停；curing_pause 固化中暂停；power_loss 断电）
    kind text NOT NULL CHECK (kind IN ('motion_pause','curing_pause','power_loss','fault_stop')),
    -- 发生时间
    at timestamptz NOT NULL,
    -- 停在哪一层
    layer_no integer,
    -- 停在哪一道次
    pass_index integer,
    -- 停在哪一段路径
    path_seq integer,
    -- 暂停时温度（℃）
    temp_at_c double precision,
    -- 本阶段已进行时长（秒）
    elapsed_in_phase_s integer CHECK (elapsed_in_phase_s IS NULL OR elapsed_in_phase_s>=0),
    -- 剩余保温时长（秒）
    remaining_hold_s integer CHECK (remaining_hold_s IS NULL OR remaining_hold_s>=0),
    -- 恢复策略（固化中断必须 recompute_hold，禁止直接续时（PRT-004））
    resume_decision text DEFAULT '' CHECK (resume_decision IN ('','resume','recalibrate_resume','recompute_hold','abort')),
    -- 坐标可信度校验结果（断电续打用）
    coord_check_json jsonb,
    -- 说明
    note text DEFAULT '',
    -- 固化中断必须记录温度
    CHECK (kind <> 'curing_pause' OR temp_at_c IS NOT NULL),
    -- 断电续打必须记录坐标校验结果
    CHECK (kind <> 'power_loss' OR coord_check_json IS NOT NULL)
);

-- ==========================================================================
-- 表 fault_injection｜故障注入
-- 六类可复现故障：类型、层、位置、随机种子；同参数重复运行缺陷位置一致。
-- 依据需求：PRT-007, PRT-008, T18
-- 建模说明：六类故障：断线/堵头/超温/烘箱失温/对位漂移/断料；随机种子固定才能“同参数重复运行缺陷位置一致”。
-- ==========================================================================
CREATE TABLE fault_injection (
    -- 注入 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属作业
    print_job_id text NOT NULL REFERENCES print_job(id) ON DELETE CASCADE,
    -- 故障类型
    fault_kind text NOT NULL CHECK (fault_kind IN ('broken_line','clog','overheat','oven_underheat','alignment_drift','material_out')),
    -- 发生层
    layer_no integer NOT NULL CHECK (layer_no>=0),
    -- 位置 X
    x_mm double precision,
    -- 位置 Y
    y_mm double precision,
    -- 随机种子（可复现的关键（PRT-007））
    random_seed integer NOT NULL,
    -- 附加参数（程度、持续时间）
    params_json jsonb,
    -- 生效时间
    applied_at timestamptz
);

-- ==========================================================================
-- 表 defect｜缺陷
-- 打印中发现的问题：类型、层、位置、严重度、证据与处理状态；三元绑定作业/切片/设计版本。
-- 依据需求：PRT-008, PRT-009, T18
-- 建模说明：状态可变但每次变更写 print_event（kind='action'），保留轨迹；证据按内容寻址。
-- ==========================================================================
CREATE TABLE defect (
    -- 缺陷 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属作业
    print_job_id text NOT NULL REFERENCES print_job(id) ON DELETE CASCADE,
    -- 所属切片（冗余，便于定位）
    slice_set_id text NOT NULL REFERENCES slice_set(id) ON DELETE RESTRICT,
    -- 归因设计版本（冗余，固化）（避免事后经三跳 join 推导导致归因漂移（冲突 C5））
    design_version_id text NOT NULL REFERENCES version(id) ON DELETE RESTRICT,
    -- 所属道次
    deposit_pass_id text REFERENCES deposit_pass(id) ON DELETE SET NULL,
    -- 层号
    layer_no integer CHECK (layer_no IS NULL OR layer_no>=0),
    -- 位置 X
    x_mm double precision,
    -- 位置 Y
    y_mm double precision,
    -- 缺陷类型（断路/短路/空洞/鼓包/漏焊盘/分层）
    defect_kind text NOT NULL CHECK (defect_kind IN ('open','short','void','bulge','missed_pad','delamination','other')),
    -- 严重度
    severity text NOT NULL DEFAULT 'major' CHECK (severity IN ('blocker','major','minor','info')),
    -- 处理状态
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','fixed','ignored')),
    -- 证据（图片/切片快照）
    evidence_blob_hash char(64) CHECK (length(evidence_blob_hash)=64) REFERENCES blob(hash) ON DELETE SET NULL,
    -- 发现时间
    detected_at timestamptz NOT NULL,
    -- 说明
    note text DEFAULT ''
);

-- ==========================================================================
-- 表 attribution｜缺陷归因
-- 归因结论与建议：原因分类、置信度、回滚/重切/调参建议；一个缺陷可多次复议。
-- 依据需求：PRT-009, SLC-010, PRT-008, T18
-- 建模说明：独立成表：归因会复议、会被推翻，塞进 defect 行会覆盖历史（PRT-009）。
-- ==========================================================================
CREATE TABLE attribution (
    -- 归因 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属缺陷
    defect_id text NOT NULL REFERENCES defect(id) ON DELETE CASCADE,
    -- 原因分类
    cause_kind text NOT NULL CHECK (cause_kind IN ('design','process','material','machine')),
    -- 置信度（0–1）
    confidence double precision NOT NULL DEFAULT 0.5 CHECK (confidence>=0 AND confidence<=1),
    -- 归因到的设计版本
    design_version_id text REFERENCES version(id) ON DELETE SET NULL,
    -- 归因方法（规则/统计/人工）
    method text DEFAULT 'rule' CHECK (method IN ('rule','model','manual','hybrid')),
    -- 建议动作
    suggestion_kind text NOT NULL DEFAULT 'none' CHECK (suggestion_kind IN ('rollback','reslice','param_tune','none')),
    -- 建议详情（改哪个参数、回滚到哪版）
    suggestion_json jsonb,
    -- 决策者
    decided_by text NOT NULL DEFAULT 'local',
    -- 决策时间
    decided_at timestamptz NOT NULL,
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 plan｜执行计划
-- 一次 Agent 运行的计划头：目标、约束、基线版本、授权级别、预算与终止原因。
-- 依据需求：AGT-001, AGT-002, AGT-003, AGT-004, AGT-006, AGT-007, AGT-011, T19
-- 建模说明：重放不重新推理，只回放 tool_call 序列；model_id/prompt_digest 仅作审计留痕。
-- ==========================================================================
CREATE TABLE plan (
    -- 计划 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 起始版本（重放基准）
    base_version_id text NOT NULL REFERENCES version(id) ON DELETE RESTRICT,
    -- 起始版本内容摘要（同版本号不同内容 = 不可比；重放必须校这个）
    base_digest char(64) NOT NULL CHECK (length(base_digest)=64),
    -- Agent 角色（设计/审查/裁决/计划）
    agent_role text NOT NULL CHECK (agent_role IN ('design','review','arbiter','planner')),
    -- 目标（用户可读）
    goal text NOT NULL,
    -- 约束（禁改网络拓扑、最小线宽等）
    constraints_json jsonb,
    -- 状态
    status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','running','awaiting_confirm','converged','stopped_no_improvement','stopped_budget','rejected','failed','replayed')),
    -- 授权级别（默认每步确认（AGT-006））
    auth_level text NOT NULL DEFAULT 'step_confirm' CHECK (auth_level IN ('suggest_only','step_confirm','auto')),
    -- 模型标识（审计用，不参与重放）
    model_id text DEFAULT '',
    -- 是否降级运行（无云端模型）（AGT-010：断网时用本地规则集跑通检查—修改—复检）
    degraded boolean NOT NULL DEFAULT false,
    -- 提示摘要（审计用）
    prompt_digest char(64) CHECK (length(prompt_digest)=64),
    -- 工具链版本（引擎+切片器）
    toolchain_version text NOT NULL,
    -- 随机种子（自动布局/布线/故障注入的随机性来源）
    random_seed integer NOT NULL DEFAULT 0,
    -- 已用轮数
    rounds_used integer NOT NULL DEFAULT 0 CHECK (rounds_used>=0),
    -- 轮数上限
    round_limit integer NOT NULL DEFAULT 8 CHECK (round_limit>0),
    -- 预算（耗时、调用次数上限）
    budget_json jsonb,
    -- 终止原因
    stop_reason text DEFAULT '',
    -- 起始分数
    score_before double precision CHECK (score_before IS NULL OR (score_before>=0 AND score_before<=100)),
    -- 收敛分数
    score_after double precision CHECK (score_after IS NULL OR (score_after>=0 AND score_after<=100)),
    -- 创建时间
    created_at timestamptz NOT NULL,
    -- 结束时间
    ended_at timestamptz,
    -- 轮数不得超过上限（AGT-007）
    CHECK (rounds_used <= round_limit)
);

-- ==========================================================================
-- 表 plan_step｜计划步骤
-- 计划里的一步：意图、工具与版本、解析后的参数、前置条件、期望结果、确认与执行状态。
-- 依据需求：AGT-004, AGT-005, AGT-006, AGT-008, T19
-- 建模说明：“只应用一步”落在 diff_card.decision='apply_one' + 该步 status='applied'，不再单建审批表。
-- ==========================================================================
CREATE TABLE plan_step (
    -- 步骤 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属计划
    plan_id text NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
    -- 步骤序号（从 1 递增）
    step_no integer NOT NULL CHECK (step_no>=1),
    -- 这一步想干什么（人类可读）
    intent text NOT NULL,
    -- 工具名
    tool_name text NOT NULL,
    -- 工具版本（重放可比性依赖它（AGT-008））
    tool_version text NOT NULL,
    -- 解析后的具体命令（含对象 ID）（必须存具体命令，不能只存自然语言意图）
    params_json jsonb NOT NULL,
    -- 前置条件
    precondition_json jsonb,
    -- 期望结果
    expected_json jsonb,
    -- 状态
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','awaiting_confirm','applied','skipped','rejected','failed')),
    -- 风险级别（dangerous 需人工确认，黑名单来自 tool_registry）
    risk_level text NOT NULL DEFAULT 'low' CHECK (risk_level IN ('low','medium','high','dangerous')),
    -- 是否需要确认
    requires_confirm boolean NOT NULL DEFAULT true,
    -- 确认结论
    confirm_decision text DEFAULT '' CHECK (confirm_decision IN ('','approved','rejected')),
    -- 确认人
    confirmed_by text DEFAULT '',
    -- 确认时间
    confirmed_at timestamptz,
    -- 执行后产生的操作
    applied_op_id text REFERENCES operation(id) ON DELETE SET NULL,
    -- 创建时间
    created_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 tool_call｜工具调用
-- 每次实际调用工具的明细：参数、结果、耗时、错误码；重放按它逐条回放。
-- 依据需求：AGT-008, AGT-009, AGT-011, AGT-010
-- 建模说明：重放按 (step_id, seq) 逐条回放；必须存实际调用参数与工具版本，不能只留意图。
-- ==========================================================================
CREATE TABLE tool_call (
    -- 调用 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属步骤
    step_id text NOT NULL REFERENCES plan_step(id) ON DELETE CASCADE,
    -- 步骤内序号（从 1 递增）
    seq integer NOT NULL CHECK (seq>=1),
    -- 工具名
    tool_name text NOT NULL,
    -- 工具版本
    tool_version text NOT NULL,
    -- 调用参数
    args_json jsonb NOT NULL,
    -- 结果
    result_status text NOT NULL CHECK (result_status IN ('ok','error','rejected')),
    -- 结果摘要（一致性比对）
    result_digest char(64) CHECK (length(result_digest)=64),
    -- 耗时（毫秒）
    duration_ms integer CHECK (duration_ms IS NULL OR duration_ms>=0),
    -- 错误码
    error_code text DEFAULT '',
    -- 调用时间
    at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 tool_registry｜能力注册表
-- 工具 → 界面动作的映射：Agent 能做的事人也能做，没有隐藏能力。
-- 依据需求：AGT-009, AGT-007, AGT-006
-- 建模说明：没有这张表，AGT-009“随机抽 5 条 Agent 操作人工可复现”只能靠文档声明。
-- ==========================================================================
CREATE TABLE tool_registry (
    -- 工具名
    tool_name text NOT NULL,
    -- 工具版本
    tool_version text NOT NULL,
    -- 对应的界面操作路径（如“走线域 > 选择网络 > 手动布线”，人工等效抽检的判据）
    human_ui_action text NOT NULL,
    -- 是否危险操作（危险操作禁止在 suggest_only/auto 下执行（AGT-007））
    is_dangerous boolean NOT NULL DEFAULT false,
    -- 最低授权级别
    min_auth_level text NOT NULL DEFAULT 'step_confirm' CHECK (min_auth_level IN ('suggest_only','step_confirm','auto')),
    -- 说明
    note text DEFAULT '',
    PRIMARY KEY (tool_name, tool_version)
);

-- ==========================================================================
-- 表 diff_card｜对比卡片
-- 修改前后的分数、改动对象、命令清单与理由；处置为应用/拒绝/只应用一步。
-- 依据需求：AGT-005, AGT-006, PRJ-004, AGT-002, T19
-- 建模说明：拒绝后模型与分数不变：decision='reject' 时不写 version_item、不产生新版本（AGT-005）。
-- ==========================================================================
CREATE TABLE diff_card (
    -- 卡片 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属计划
    plan_id text NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
    -- 修改前版本
    before_version_id text REFERENCES version(id) ON DELETE SET NULL,
    -- 修改后版本
    after_version_id text REFERENCES version(id) ON DELETE SET NULL,
    -- 修改前分数
    score_before double precision CHECK (score_before IS NULL OR (score_before>=0 AND score_before<=100)),
    -- 修改后分数
    score_after double precision CHECK (score_after IS NULL OR (score_after>=0 AND score_after<=100)),
    -- 改动对象清单
    changed_objects_json jsonb,
    -- 命令列表（可逐条应用）
    command_list_json jsonb,
    -- 理由（人类可读）
    reason text NOT NULL DEFAULT '',
    -- 处置
    decision text NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','apply','reject','apply_one')),
    -- 只应用一步时的那一步
    applied_step_id text REFERENCES plan_step(id) ON DELETE SET NULL,
    -- 处置时的授权级别（授权级别中途可改，必须留快照（AGT-006））
    auth_level_at_time text NOT NULL DEFAULT 'step_confirm' CHECK (auth_level_at_time IN ('suggest_only','step_confirm','auto')),
    -- 决策者
    decided_by text DEFAULT '',
    -- 决策时间
    decided_at timestamptz,
    -- 创建时间
    created_at timestamptz NOT NULL,
    -- 只应用一步必须记录是哪一步
    CHECK (decision <> 'apply_one' OR applied_step_id IS NOT NULL)
);

-- ==========================================================================
-- 表 replay_run｜重放记录
-- 同一计划、同一起始版本、同一工具版本的重放证据：输出哈希与一致性结论。
-- 依据需求：AGT-008, T20, AGT-004
-- 建模说明：七要素缺一不可：base_version(+digest)、tool_version、rule_version、tool_geometry、process_profile(+hash)、material_batch、random_seed。
-- ==========================================================================
CREATE TABLE replay_run (
    -- 重放 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 被重放计划
    plan_id text NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
    -- 第几次重放
    attempt_no integer NOT NULL CHECK (attempt_no>=1),
    -- 起始版本
    start_version_id text NOT NULL REFERENCES version(id) ON DELETE RESTRICT,
    -- 工具链版本
    toolchain_version text NOT NULL,
    -- 规则版本
    rule_version_id text REFERENCES mrc_rule_version(id) ON DELETE SET NULL,
    -- 工具几何
    tool_geometry_id text REFERENCES tool_geometry(id) ON DELETE SET NULL,
    -- 工艺配置
    process_profile_id text REFERENCES process_profile(id) ON DELETE SET NULL,
    -- 材料批次清单摘要（批次进 G-code 头，换批次即换哈希）
    material_manifest_hash char(64) CHECK (length(material_manifest_hash)=64),
    -- 重放产出 G-code 内容哈希
    gcode_content_hash char(64) CHECK (length(gcode_content_hash)=64),
    -- 是否与基准一致
    matched boolean NOT NULL DEFAULT false,
    -- 不一致时的差异摘要
    diff_summary_json jsonb,
    -- 重放时间
    ran_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 dictionary_entry｜受控字典
-- 枚举中文标签、诊断码解释、术语表三合一；离线可查（UI-008/UI-010），也是复合外键的枚举域。
-- 依据需求：UI-005, UI-008, UI-010, STR-002, SLC-012
-- 建模说明：枚举只存英文 code，中文标签放这里；UI-008 要求“错误信息不以内部代号作为唯一解释”，靠 description 兜底。
-- ==========================================================================
CREATE TABLE dictionary_entry (
    -- 字典域
    domain text NOT NULL CHECK (domain IN ('enum','diagnostic','glossary','mrc_rule','fault','defect')),
    -- 编码
    code text NOT NULL CHECK (length(trim(code))>0),
    -- 中文标签
    label_zh text NOT NULL,
    -- 英文标签
    label_en text DEFAULT '',
    -- 解释（错误码的“为什么”与“怎么办”）
    description text DEFAULT '',
    -- 父级编码（树形术语表）
    parent_code text DEFAULT '',
    -- 排序号
    sort_order integer NOT NULL DEFAULT 0,
    -- 启用状态
    is_active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (domain, code)
);

-- ==========================================================================
-- 表 cloud_call｜云端调用记录
-- 云端智能体的调用证据：用途、开关、请求/响应摘要、耗时与降级去向。
-- 依据需求：AGT-012, 5.4 云端接口, 9-5 待确认
-- 建模说明：只存摘要不存原文：符合 DAT-007 隐私要求；关闭云功能后 status 只会是 skipped。
-- ==========================================================================
CREATE TABLE cloud_call (
    -- 调用 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程
    project_id text REFERENCES project(id) ON DELETE CASCADE,
    -- 用途
    purpose text NOT NULL CHECK (purpose IN ('explain','rank','defect_detect','train','inference','other')),
    -- 调用时云功能是否开启
    enabled boolean NOT NULL DEFAULT true,
    -- 请求摘要（不落原文）
    request_digest char(64) CHECK (length(request_digest)=64),
    -- 响应摘要
    response_digest char(64) CHECK (length(response_digest)=64),
    -- 模型标识
    model_id text DEFAULT '',
    -- 耗时（毫秒）
    latency_ms integer CHECK (latency_ms IS NULL OR latency_ms>=0),
    -- 结果
    status text NOT NULL CHECK (status IN ('ok','failed','degraded','skipped')),
    -- 降级去向（本地规则集标识）
    degraded_to text DEFAULT '',
    -- 详情（错误、重试次数）
    detail_json jsonb,
    -- 调用时间
    at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 accept_result｜验收任务结果
-- 7.1 的 20 条验收任务逐条留证：输入哈希、成功与否、轮数、耗时、初始分/收敛分、G-code 哈希。
-- 依据需求：7.1 验收任务, 7.2 验收口径, T19, T20
-- 建模说明：这张表让“P0 全过、P1 允许失败但记录原因”的验收口径可被机器核对，而不是靠人写报告。
-- ==========================================================================
CREATE TABLE accept_result (
    -- 结果 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 任务号
    task_no text NOT NULL CHECK (task_no ~ '^T[0-9]{2}$'),
    -- 所用工程
    project_id text REFERENCES project(id) ON DELETE SET NULL,
    -- 输入文件哈希（T19 要求固定验收板哈希随文档入库，本列即载体）
    input_blob_hash char(64) NOT NULL CHECK (length(input_blob_hash)=64),
    -- 是否通过
    passed boolean NOT NULL DEFAULT false,
    -- Agent 收敛轮数
    rounds integer CHECK (rounds IS NULL OR rounds>=0),
    -- 耗时（秒）
    duration_s double precision CHECK (duration_s IS NULL OR duration_s>=0),
    -- 初始分数
    score_initial double precision CHECK (score_initial IS NULL OR (score_initial>=0 AND score_initial<=100)),
    -- 收敛分数
    score_final double precision CHECK (score_final IS NULL OR (score_final>=0 AND score_final<=100)),
    -- G-code 内容哈希
    gcode_hash char(64) CHECK (length(gcode_hash)=64),
    -- 失败诊断码
    diagnostic_code text DEFAULT '',
    -- 备注
    notes text DEFAULT '',
    -- 执行时间
    ran_at timestamptz NOT NULL
);

-- ==========================================================================
-- 表 package_manifest｜工程包清单
-- 导出/导入的工程包：内容清单、整体哈希、是否内嵌 blob、导入冲突记录。
-- 依据需求：PRJ-006, DAT-004, NFR-008
-- 建模说明：工程包一律内嵌被引用 blob（冲突 C6）：跨机导入不能假设对方机器已有同样的 CAS 文件。
-- ==========================================================================
CREATE TABLE package_manifest (
    -- 清单 ID
    id text PRIMARY KEY NOT NULL CHECK (length(id)=36),
    -- 所属工程（空=整库备份包）（导出单个工程时指向该工程；整库备份时为空）
    project_id text REFERENCES project(id) ON DELETE CASCADE,
    -- 导出时间
    exported_at timestamptz NOT NULL,
    -- 整包哈希
    package_hash char(64) NOT NULL CHECK (length(package_hash)=64),
    -- 数据模式版本
    schema_version integer NOT NULL CHECK (schema_version>=1),
    -- 是否内嵌 blob（跨机必需）
    include_blobs boolean NOT NULL DEFAULT true,
    -- 清单（表名→行数、blob 哈希列表）
    entries_json jsonb,
    -- 导入结果
    import_result text DEFAULT '' CHECK (import_result IN ('','ok','conflict','failed')),
    -- 冲突明细（同源工程、哈希不一致）
    conflict_json jsonb,
    -- 创建时间
    created_at timestamptz NOT NULL
);

ALTER TABLE version ADD CONSTRAINT fk_version_project_id FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE;

-- ---------------------------- 索引 ----------------------------
-- 最近工程列表只取未删除
CREATE INDEX idx_project_alive ON project (deleted_at) WHERE deleted_at IS NULL;
-- 按最近使用排序（PRJ-002）
CREATE INDEX idx_project_updated_at ON project (updated_at);

-- 版本号在工程内唯一（PRJ-004）
CREATE UNIQUE INDEX idx_version_project_id_version_no ON version (project_id, version_no);
-- 版本树回溯
CREATE INDEX idx_version_parent_version_id ON version (parent_version_id);
-- 按时间列历史
CREATE INDEX idx_version_project_id_created_at ON version (project_id, created_at);

-- 查看某版本改了哪些对象（PRJ-004）
CREATE INDEX idx_version_item_version_id_target_kind ON version_item (version_id, target_kind);
-- 反查某对象被哪些版本改过
CREATE INDEX idx_version_item_target_kind_target_id ON version_item (target_kind, target_id);

-- 按工程查会话
CREATE INDEX idx_edit_session_project_id_started_at ON edit_session (project_id, started_at);

-- 撤销栈按序号回放（PRJ-005）
CREATE UNIQUE INDEX idx_operation_session_id_seq ON operation (session_id, seq);
-- 查找某操作的撤销记录
CREATE INDEX idx_operation_undo_of_op_id ON operation (undo_of_op_id);
-- 对象历史
CREATE INDEX idx_operation_target_kind_target_id ON operation (target_kind, target_id);

-- 恢复时取最近快照再重放后续操作
CREATE INDEX idx_snapshot_session_id_op_seq ON snapshot (session_id, op_seq);
-- 按工程列快照
CREATE INDEX idx_snapshot_project_id_scope ON snapshot (project_id, scope);

-- 完整性巡检只扫异常项（DAT-005）
CREATE INDEX idx_blob_bad ON blob (verify_status) WHERE verify_status<>'ok';
-- 按类别清理/备份
CREATE INDEX idx_blob_kind_created_at ON blob (kind, created_at);

-- 按工程看迁移历史
CREATE INDEX idx_migration_log_project_id_applied_at ON migration_log (project_id, applied_at);

-- 按角色取源文件
CREATE INDEX idx_external_file_project_id_role ON external_file (project_id, role);
-- 打开工程时只校验异常项
CREATE INDEX idx_extfile_bad ON external_file (project_id, verify_status) WHERE verify_status<>'ok';

-- 一个工程的结构模型
CREATE INDEX idx_structure_project_id ON structure (project_id);
-- 按网格哈希找结构（缓存复用）
CREATE INDEX idx_structure_mesh_blob_hash ON structure (mesh_blob_hash);

-- 一个结构只有一份当前板框
CREATE UNIQUE INDEX idx_board_outline_structure_id ON board_outline (structure_id);

-- 特征列表按序展示（STR-006）
CREATE INDEX idx_feature_structure_id_sort_order ON feature (structure_id, sort_order);
-- 切片前查 pending 特征
CREATE INDEX idx_feature_structure_id_status ON feature (structure_id, status);

-- 待处理候选列表
CREATE INDEX idx_bool_candidate_structure_id_status ON bool_candidate (structure_id, status);

-- 问题列表按环节/严重度筛选
CREATE INDEX idx_diagnostic_project_id_scope_severity ON diagnostic (project_id, scope, severity);
-- 按诊断码统计（坏样本回归）
CREATE INDEX idx_diagnostic_code ON diagnostic (code);
-- 对象定位
CREATE INDEX idx_diagnostic_target_kind_target_id ON diagnostic (target_kind, target_id);

-- 一个工程的电路
CREATE INDEX idx_circuit_project_id ON circuit (project_id);

-- 同一电路内网络类名唯一
CREATE UNIQUE INDEX idx_net_class_circuit_id_name ON net_class (circuit_id, name);

-- 位号在电路内唯一（T06 数量核对）
CREATE UNIQUE INDEX idx_component_circuit_id_ref_des ON component (circuit_id, ref_des);
-- 锁定器件过滤
CREATE INDEX idx_component_circuit_id_locked ON component (circuit_id, locked);

-- 网络名唯一（CIR-003）
CREATE UNIQUE INDEX idx_net_circuit_id_name ON net (circuit_id, name);
-- 按网络类批量改约束
CREATE INDEX idx_net_net_class_id ON net (net_class_id);

-- 器件内焊盘号唯一
CREATE UNIQUE INDEX idx_pad_component_id_pad_no ON pad (component_id, pad_no);
-- 按网络取焊盘（连通性检查）
CREATE INDEX idx_pad_net_id ON pad (net_id);

-- 高亮单网络、分层连通性（CIR-003/009）
CREATE INDEX idx_route_net_id_layer_no ON route (net_id, layer_no);
-- 在库走线列表
CREATE INDEX idx_route_circuit_id_deleted_at ON route (circuit_id, deleted_at);

-- 按网络列跳线
CREATE INDEX idx_jumper_net_id_status ON jumper (net_id, status);

-- 按电路取禁布区
CREATE INDEX idx_keepout_circuit_id ON keepout (circuit_id);

-- 按层取铺铜
CREATE INDEX idx_copper_zone_circuit_id_layer_no ON copper_zone (circuit_id, layer_no);

-- 按电路/网络取过孔
CREATE INDEX idx_via_circuit_id_net_id ON via (circuit_id, net_id);
-- 连通性计算时按网络取
CREATE INDEX idx_via_net_id ON via (net_id);

-- 同一电路的候选排序
CREATE UNIQUE INDEX idx_layout_candidate_circuit_id_rank_no ON layout_candidate (circuit_id, rank_no);
-- 待选候选列表
CREATE INDEX idx_layout_candidate_circuit_id_status ON layout_candidate (circuit_id, status);

-- 最近一次检查
CREATE INDEX idx_connectivity_check_project_id_scope_ran_at ON connectivity_check (project_id, scope, ran_at);
-- 按版本追溯
CREATE INDEX idx_connectivity_check_version_id_scope ON connectivity_check (version_id, scope);
-- 切片导出前查结果（SLC-007）
CREATE INDEX idx_connectivity_check_slice_set_id ON connectivity_check (slice_set_id);

-- 取当前生效规则版本
CREATE INDEX idx_rulever_active ON mrc_rule_version (is_active) WHERE is_active;
-- 版本链回溯
CREATE INDEX idx_mrc_rule_version_supersedes_id ON mrc_rule_version (supersedes_id);

-- 按规则取全部参数
CREATE INDEX idx_mrc_rule_threshold_rule_code ON mrc_rule_threshold (rule_code);

-- 当前在用喷嘴
CREATE INDEX idx_toolgeom_active ON tool_geometry (is_active) WHERE is_active;

-- 某版本的评分历史（MRC-011）
CREATE INDEX idx_mrc_run_version_id_created_at ON mrc_run (version_id, created_at);
-- 同一指纹只允许一次（重跑即新行）
CREATE UNIQUE INDEX uq_mrc_run_fingerprint ON mrc_run (version_id, rule_version_id, tool_geometry_id, engine_version, input_digest);
-- 按工程看评分趋势
CREATE INDEX idx_mrc_run_project_id_created_at ON mrc_run (project_id, created_at);

-- 问题列表按版本+规则+严重度筛选排序（MRC-010）
CREATE INDEX idx_issue_hot ON mrc_issue (version_id, rule_version_id, rule_code, severity);
-- 单次检查详情
CREATE INDEX idx_mrc_issue_run_id ON mrc_issue (run_id);
-- 点列表项定位到对象
CREATE INDEX idx_mrc_issue_target_kind_target_id ON mrc_issue (target_kind, target_id);

-- 材料代号唯一
CREATE UNIQUE INDEX idx_material_code ON material (code);
-- 可选材料列表
CREATE INDEX idx_material_active ON material (is_active) WHERE is_active;

-- 同材料批次号唯一
CREATE UNIQUE INDEX idx_material_batch_material_id_batch_no ON material_batch (material_id, batch_no);
-- 索引
CREATE INDEX idx_batch_active ON material_batch (is_active) WHERE is_active;

-- 工程内配置名唯一
CREATE UNIQUE INDEX uq_profile_project_name ON process_profile (project_id, name) WHERE project_id IS NOT NULL;
-- 全局模板名唯一
CREATE UNIQUE INDEX uq_profile_global_name ON process_profile (name) WHERE project_id IS NULL;

-- 工程内可用/过期切片
CREATE INDEX idx_slice_set_project_id_status ON slice_set (project_id, status);
-- 按版本查切片
CREATE INDEX idx_slice_set_design_version_id ON slice_set (design_version_id);
-- 按配置查切片
CREATE INDEX idx_slice_set_process_profile_id ON slice_set (process_profile_id);

-- 层序唯一（T13 三层）
CREATE UNIQUE INDEX idx_physical_layer_slice_set_id_layer_index ON physical_layer (slice_set_id, layer_index);

-- 道次序在切片内唯一
CREATE UNIQUE INDEX idx_deposit_pass_slice_set_id_pass_index ON deposit_pass (slice_set_id, pass_index);
-- 按层取道次
CREATE INDEX idx_deposit_pass_physical_layer_id ON deposit_pass (physical_layer_id);
-- 批次反查（SLC-008）
CREATE INDEX idx_deposit_pass_material_batch_id ON deposit_pass (material_batch_id);

-- 回放顺序（SLC-006）
CREATE UNIQUE INDEX idx_slice_path_deposit_pass_id_seq ON slice_path (deposit_pass_id, seq);
-- 几何去重
CREATE INDEX idx_slice_path_geom_blob_hash ON slice_path (geom_blob_hash);

-- 时间轴顺序（T14）
CREATE UNIQUE INDEX idx_thermal_step_slice_set_id_step_index ON thermal_step (slice_set_id, step_index);

-- 同一内容哈希只记一次（T16 两次导出一致）
CREATE UNIQUE INDEX idx_gcode_export_slice_set_id_content_hash ON gcode_export (slice_set_id, content_hash);
-- 跨工程比对相同 G-code
CREATE INDEX idx_gcode_export_content_hash ON gcode_export (content_hash);

-- 按工程列设备
CREATE INDEX idx_device_project_id_conn_kind ON device (project_id, conn_kind);
-- 在线设备过滤
CREATE INDEX idx_device_status ON device (status);

-- 取最近一次有效标定（PRT-002 前置检查）
CREATE INDEX idx_device_calibration_device_id_measured_at ON device_calibration (device_id, measured_at);

-- 作业历史按工程/时间筛选（PRT-010）
CREATE INDEX idx_print_job_project_id_started_at ON print_job (project_id, started_at);
-- 按切片查作业
CREATE INDEX idx_print_job_slice_set_id ON print_job (slice_set_id);
-- 在跑作业
CREATE INDEX idx_print_job_status ON print_job (status);
-- 哈希反查作业（SLC-008 批次反查）
CREATE INDEX idx_print_job_gcode_content_hash ON print_job (gcode_content_hash);

-- 时间线回放（PRT-006）
CREATE UNIQUE INDEX idx_print_event_print_job_id_seq ON print_event (print_job_id, seq);
-- 只看故障/只看前置检查
CREATE INDEX idx_print_event_print_job_id_event_kind ON print_event (print_job_id, event_kind);

-- 按作业列中断记录
CREATE INDEX idx_print_interruption_print_job_id_at ON print_interruption (print_job_id, at);

-- 按作业看注入
CREATE INDEX idx_fault_injection_print_job_id_fault_kind ON fault_injection (print_job_id, fault_kind);
-- 复现同一注入
CREATE INDEX idx_fault_injection_fault_kind_layer_no_random_seed ON fault_injection (fault_kind, layer_no, random_seed);

-- 缺陷列表按作业+状态（PRT-008）
CREATE INDEX idx_defect_print_job_id_status ON defect (print_job_id, status);
-- 层定位
CREATE INDEX idx_defect_print_job_id_layer_no ON defect (print_job_id, layer_no);
-- 按版本归因（PRT-009）
CREATE INDEX idx_defect_design_version_id ON defect (design_version_id);
-- 按严重度筛选
CREATE INDEX idx_defect_severity ON defect (severity);

-- 一个缺陷的归因历史
CREATE INDEX idx_attribution_defect_id ON attribution (defect_id);
-- 按版本汇总归因
CREATE INDEX idx_attribution_design_version_id ON attribution (design_version_id);

-- 计划列表
CREATE INDEX idx_plan_project_id_status ON plan (project_id, status);
-- 按基线版本查计划
CREATE INDEX idx_plan_base_version_id ON plan (base_version_id);

-- 步骤顺序（AGT-004）
CREATE UNIQUE INDEX idx_plan_step_plan_id_step_no ON plan_step (plan_id, step_no);
-- 按工具统计
CREATE INDEX idx_plan_step_tool_name_tool_version ON plan_step (tool_name, tool_version);

-- 回放顺序
CREATE UNIQUE INDEX idx_tool_call_step_id_seq ON tool_call (step_id, seq);
-- 工具使用统计
CREATE INDEX idx_tool_call_tool_name_tool_version ON tool_call (tool_name, tool_version);

-- 危险操作黑名单
CREATE INDEX idx_tool_danger ON tool_registry (is_dangerous) WHERE is_dangerous;

-- 一个计划的对比卡片
CREATE INDEX idx_diff_card_plan_id ON diff_card (plan_id);
-- 待处置卡片
CREATE INDEX idx_diff_card_decision ON diff_card (decision);

-- 同一计划的重放序列
CREATE UNIQUE INDEX idx_replay_run_plan_id_attempt_no ON replay_run (plan_id, attempt_no);
-- 跨计划比对相同产出
CREATE INDEX idx_replay_run_gcode_content_hash ON replay_run (gcode_content_hash);

-- 按域取树
CREATE INDEX idx_dictionary_entry_domain_parent_code ON dictionary_entry (domain, parent_code);
-- 术语检索（FTS5 外部内容表的来源）
CREATE INDEX idx_dictionary_entry_label_zh ON dictionary_entry (label_zh);

-- 按工程查云端调用
CREATE INDEX idx_cloud_call_project_id_at ON cloud_call (project_id, at);
-- 比赛材料的“真实调用”证据
CREATE INDEX idx_cloud_call_purpose_status ON cloud_call (purpose, status);

-- 按任务看历次结果
CREATE INDEX idx_accept_result_task_no_ran_at ON accept_result (task_no, ran_at);
-- 按输入比对可复现性
CREATE INDEX idx_accept_result_input_blob_hash ON accept_result (input_blob_hash);

-- 按工程列工程包
CREATE INDEX idx_package_manifest_project_id_exported_at ON package_manifest (project_id, exported_at);

-- --------------------------- 触发器 ---------------------------
CREATE TRIGGER trg_project_touch BEFORE UPDATE ON project FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TRIGGER trg_version_no_update BEFORE UPDATE ON version FOR EACH ROW EXECUTE FUNCTION forbid_mutation('DAT-002: version 表不可篡改（禁止 UPDATE）');

CREATE TRIGGER trg_version_no_delete BEFORE DELETE ON version FOR EACH ROW EXECUTE FUNCTION forbid_mutation('DAT-002: version 表不可删除（历史只读）');

CREATE TRIGGER trg_version_item_no_update BEFORE UPDATE ON version_item FOR EACH ROW EXECUTE FUNCTION forbid_mutation('DAT-002: 版本变更项不可修改，只能随版本追加');

CREATE TRIGGER trg_version_item_no_delete BEFORE DELETE ON version_item FOR EACH ROW EXECUTE FUNCTION forbid_mutation('DAT-002: 版本变更项不可删除（历史只读）');

CREATE TRIGGER trg_operation_append_only BEFORE UPDATE ON operation FOR EACH ROW EXECUTE FUNCTION forbid_mutation('PRJ-005: 操作日志只追加，不允许改写');

CREATE TRIGGER trg_operation_no_delete BEFORE DELETE ON operation FOR EACH ROW EXECUTE FUNCTION forbid_mutation('PRJ-005: 操作日志不可删除（撤销靠追加，不靠删行）');

CREATE TRIGGER trg_mrc_run_append_only BEFORE UPDATE ON mrc_run FOR EACH ROW EXECUTE FUNCTION forbid_mutation('MRC-011: 评分结果只追加，旧分数不得被覆盖');

CREATE TRIGGER trg_mrc_run_no_delete BEFORE DELETE ON mrc_run FOR EACH ROW EXECUTE FUNCTION forbid_mutation('MRC-011: 评分运行不可删除（会级联删掉违规记录）');


CREATE OR REPLACE FUNCTION issue_run_consistency() RETURNS trigger AS $$
DECLARE v text; r text;
BEGIN
  SELECT version_id, rule_version_id INTO v, r FROM mrc_run WHERE id = NEW.run_id;
  IF NEW.version_id <> v OR NEW.rule_version_id <> r THEN
    RAISE EXCEPTION 'MRC-008: 违规条目的 version/rule_version 必须与所属运行一致';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_issue_run_consistency BEFORE INSERT ON mrc_issue
FOR EACH ROW EXECUTE FUNCTION issue_run_consistency();

CREATE TRIGGER trg_issue_no_update BEFORE UPDATE ON mrc_issue FOR EACH ROW EXECUTE FUNCTION forbid_mutation('MRC-011: 违规条目不可改写，重新评分应追加新运行');

CREATE TRIGGER trg_issue_no_delete BEFORE DELETE ON mrc_issue FOR EACH ROW EXECUTE FUNCTION forbid_mutation('MRC-011: 违规条目不可删除（旧结果必须可解释）');


CREATE OR REPLACE FUNCTION slice_stale_on_version() RETURNS trigger AS $$
BEGIN
  UPDATE slice_set SET status = 'stale', stale_since = now()
   WHERE project_id = NEW.project_id AND status = 'ready'
     AND design_version_id <> NEW.id;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_slice_stale_on_version AFTER INSERT ON version
FOR EACH ROW EXECUTE FUNCTION slice_stale_on_version();


CREATE OR REPLACE FUNCTION slice_ready_guard() RETURNS trigger AS $$
DECLARE cur text;
BEGIN
  SELECT current_version_id INTO cur FROM project WHERE id = NEW.project_id;
  IF NEW.status = 'ready' AND NEW.design_version_id <> cur THEN
    RAISE EXCEPTION 'SLC-010: 绑定版本已不是当前版本，不得置为 ready';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_slice_ready_guard BEFORE UPDATE OF status ON slice_set
FOR EACH ROW EXECUTE FUNCTION slice_ready_guard();

CREATE TRIGGER trg_gcode_append_only BEFORE UPDATE ON gcode_export FOR EACH ROW EXECUTE FUNCTION forbid_mutation('SLC-008: 导出记录只追加，哈希不得被改写');

CREATE TRIGGER trg_gcode_no_delete BEFORE DELETE ON gcode_export FOR EACH ROW EXECUTE FUNCTION forbid_mutation('SLC-008: 导出记录不可删除（哈希是追溯证据）');


CREATE OR REPLACE FUNCTION defect_binding() RETURNS trigger AS $$
DECLARE v text;
BEGIN
  SELECT design_version_id INTO v FROM slice_set WHERE id = NEW.slice_set_id;
  IF NEW.design_version_id <> v THEN
    RAISE EXCEPTION 'PRT-009: 缺陷的设计版本必须与所用切片绑定的版本一致';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_defect_binding BEFORE INSERT ON defect
FOR EACH ROW EXECUTE FUNCTION defect_binding();


CREATE OR REPLACE FUNCTION review_step_readonly() RETURNS trigger AS $$
DECLARE role text;
BEGIN
  SELECT agent_role INTO role FROM plan WHERE id = NEW.plan_id;
  IF NEW.status = 'applied' AND role = 'review' THEN
    RAISE EXCEPTION 'AGT-002: 审查 Agent 只读，不得执行步骤';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_review_step_readonly_ins BEFORE INSERT ON plan_step
FOR EACH ROW EXECUTE FUNCTION review_step_readonly();


CREATE OR REPLACE FUNCTION review_step_readonly() RETURNS trigger AS $$
DECLARE role text;
BEGIN
  SELECT agent_role INTO role FROM plan WHERE id = NEW.plan_id;
  IF NEW.status = 'applied' AND role = 'review' THEN
    RAISE EXCEPTION 'AGT-002: 审查 Agent 只读，不得执行步骤';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_review_step_readonly_upd BEFORE UPDATE ON plan_step
FOR EACH ROW EXECUTE FUNCTION review_step_readonly();


CREATE OR REPLACE FUNCTION review_card_readonly() RETURNS trigger AS $$
DECLARE role text;
BEGIN
  SELECT agent_role INTO role FROM plan WHERE id = NEW.plan_id;
  IF NEW.decision IN ('apply','apply_one') AND role = 'review' THEN
    RAISE EXCEPTION 'AGT-002: 审查 Agent 只读，不得应用修改';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_review_card_readonly_ins BEFORE INSERT ON diff_card
FOR EACH ROW EXECUTE FUNCTION review_card_readonly();


CREATE OR REPLACE FUNCTION review_card_readonly() RETURNS trigger AS $$
DECLARE role text;
BEGIN
  SELECT agent_role INTO role FROM plan WHERE id = NEW.plan_id;
  IF NEW.decision IN ('apply','apply_one') AND role = 'review' THEN
    RAISE EXCEPTION 'AGT-002: 审查 Agent 只读，不得应用修改';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_review_card_readonly_upd BEFORE UPDATE ON diff_card
FOR EACH ROW EXECUTE FUNCTION review_card_readonly();
