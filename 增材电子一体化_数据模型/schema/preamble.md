# 增材电子一体化软件 · 数据模型设计说明书

| 项 | 内容 |
|---|---|
| 文档版本 | **1.0（字段级，完整版）** |
| 日期 | 2026-10-26 |
| 状态 | 设计基线，可直接建库 |
| 上游依据 | 《增材电子一体化软件·需求规格说明书》v1.1（105 条需求、25 个实体） |
| 覆盖范围 | {{STATS}} |
| 交付形态 | 本说明书 + 两份可执行 DDL（SQLite / PostgreSQL）+ ER 图 + 需求追溯矩阵 + 校验脚本 |
| 读者 | 开发、测试、验收 |
| 唯一事实源 | `schema/*.py`；本文件、DDL、ER 图、追溯矩阵均由它生成，禁止手改生成物 |

---

## 1. 这份文档要解决什么

需求规格说明书说清了"软件要做成什么样"，但没有规定数据怎么落。本文补上这一层：**每个实体有哪些字段、什么类型、什么约束、什么状态、靠什么索引查、对应哪条需求**。

三条硬要求决定了本文的写法：

1. **可执行**：DDL 必须能直接建库跑通，而不是示意图。`03_ddl/schema_sqlite.sql` 在本机 SQLite 3.46.1 上实测通过（含触发器与外键检查）。
2. **可追溯**：每个表、每个关键字段都标注依据的需求编号；反查见 `04_追溯/需求追溯矩阵.md`。
3. **可验收**：关键不变量（版本不可篡改、评分只追加、缺陷三元绑定、切片过期）不是靠口头约定，而是落到 CHECK 与触发器上，见 `06_校验/校验报告.md`。

---

## 2. 六条设计原则

**P1 单一事实源。** {{TABLES}} 张表定义在 `schema/` 的 8 个 Python 模块里，DDL、字典、ER 图、追溯矩阵四份产物由 `tools/` 生成。改字段只改一处，四个产物同步变化。手改生成物会在下次生成时被覆盖，这是刻意的。

**P2 当前工作集 + 版本差异，不做多版本行。** 业务表（结构、走线、切片参数）只保留"当前状态"一份，历史靠 `version_item` 的对象级差分 + `blob` 的几何快照 + `snapshot` 周期快照重建。理由：一块 20 万面的模型不可能每个版本存一份全量；而"每行带 version_id"会让 {{TABLES}} 张表全部翻倍，查询与一致性都无法维护。

**P3 几何与长文本进 CAS，行上只留索引列。** 点序、网格、证据、G-code 存文件系统（`blobs/ab/cd/<sha256>`），`blob` 表登记哈希、大小、校验状态与引用计数。行上保留 `length_mm`、`bbox_*`、`point_count` 这类可过滤的标量，避免为了画一条线的包围盒去读大文件。

**P4 只追加的真相，可缓存的派生。** `version`、`mrc_run`、`mrc_issue`、`operation`、`gcode_export` 一律只追加，触发器禁止 UPDATE。分数、引用计数、飞线这类派生值允许冗余，但必须能重算。

**P5 追溯链闭合到"哪一版、哪一批、哪一刀"。** 缺陷 → 作业 → 切片 → 设计版本 → 材料批次，这条链上每一跳都是外键，不允许"事后推断"。

**P6 人工等效优先于智能体。** Agent 的每个动作都要能在 `tool_registry` 里映射到一条界面操作路径；做不到就不该存在这个工具（AGT-009）。

---

## 3. 五个必须讲清的机制

### 3.1 撤销不产生版本，提交才产生版本

SRS 自身有一处硬冲突：PRJ-005 要求"撤销不产生版本、版本号在撤销过程中不变"，DAT-002 的验收要点却写"撤销通过新版本实现"。1.1 版变更记录第 3 条已经裁定前者为准，但正文没改干净。

模型按 PRJ-005 落地，三个载体各司其职：

| 载体 | 语义 | 何时写 |
|---|---|---|
| `operation` | 每一次成功修改（含撤销、重做） | 立即写，只追加，带 `inverse_json` |
| `snapshot` | 会话恢复点 | 每 N 次操作或按自动保存间隔 |
| `version` | 用户可见的提交点 | 保存、提交、应用计划、回滚、导入时 |

撤销 = 追加一条 `op_type='undo'`、`undo_of_op_id` 指向原操作的记录，并应用其反向操作；`version_no` 全程不动。这样"连续撤销 10 步再重做 10 步、版本号不变"可以直接用 SQL 核对（`06_校验/validate.py` 的 V-01/V-02 就是干这个的）。

### 3.2 两个哈希：内容哈希与文件哈希

同一份 G-code 连续导出两次，文件里通常带时间戳，"字节哈希相同"永远不成立。所以导出记录双哈希：

- `content_hash`：规范化内容（剔除时间戳/会话 ID/绝对路径，浮点统一 `%.3f`，字段顺序固定）。**验收只看这个**（SLC-008、T16、AGT-008、T20）。
- `file_hash`：落盘字节哈希，用于排查"文件被外部改动"。

只存后者会让"两次导出哈希一致"这条验收项恒假——这是最容易在实现阶段翻车、又最难事后补救的一处。

### 3.3 分数属于运行，不属于版本

`version` 上只有 `score_before/score_after` 两个提交瞬间的快照，用于列表展示。真正的分数在 `mrc_run`：

- 一次评分 = 一行，五元唯一 `(version_id, rule_version_id, tool_geometry_id, engine_version, input_digest)`；
- 改阈值重评分 → 新 `rule_version` + 新 `mrc_run`，旧行一动不动（MRC-011）；
- `mrc_issue` 冗余 `version_id/rule_version_id`，换来 `(version_id, rule_version_id, rule_code, severity)` 这个热索引，问题列表页不必三表 join。

如果把 `score` 做成 `version` 的可变列，"旧分数不被覆盖"和"历史不可篡改"会同时失效。

### 3.4 切片过期必须显式记录

`slice_set.status='stale'` 不能靠"是否等于当前版本"实时推导：工程一旦回滚，旧切片会"变回最新"，而它绑定的是回滚前的那一版工艺。所以：

- 绑定 `design_version_id` + 显式 `stale_since`；
- `version` 插入后由触发器把同工程其他 `ready` 切片打成 `stale`；
- 重算产生**新**切片行，不原地把 `stale` 改回 `ready`。

### 3.5 重放一致需要七个固定量

`replay_run` 记录的七要素，缺一个 100% 重放就做不到：

1. `start_version_id` + `base_digest`（同版本号不同内容 = 不可比）
2. 工具版本（`plan_step.tool_version` / `plan.toolchain_version`）
3. 规则版本（`rule_version_id`）
4. 工具几何（`tool_geometry_id`，喷嘴一换就是另一套可达性判定）
5. 工艺配置 + `params_hash`
6. 材料批次（`material_manifest_hash`，批次号进 G-code 头）
7. 随机种子（`plan.random_seed`，自动布局/布线/故障注入的随机性来源）

重放是**回放已记录的 `tool_call` 序列**，不重新推理；因此 `plan_step.params_json` 必须存解析后的具体命令（含对象 ID），不能只存自然语言意图。这也是断网降级（AGT-010）仍能跑通的前提。

---

## 4. 命名与类型约定

| 约定 | 规则 | 原因 |
|---|---|---|
| 表名 | 单数、`snake_case`（`project`、`mrc_issue`） | 与 ER 图、代码实体一致 |
| 主键 | 业务表统一 `id TEXT`，UUIDv7（36 字符小写） | 跨机器导入不冲突；时间有序，索引局部性好 |
| 例外 | `slice_path.id` 用整型行号；`dictionary_entry` 用 `(domain, code)` 复合键；`mrc_rule_threshold` 用三列复合键 | 路径表行数可达数十万，36 字节主键会显著放大索引 |
| 外键 | `<表>_id`；`ON DELETE` 显式声明（CASCADE 只用于子几何，历史表一律 RESTRICT） | 历史不可物理删除 |
| 时间 | 一律 UTC ISO-8601 带毫秒，`TEXT` + `strftime` CHECK（PG: `timestamptz`） | 跨时区可读、可字典序排序 |
| 排序 | 用 `version_no` / `seq`，**不用时间戳** | 同秒多次操作、系统时钟回拨都会乱序 |
| 长度 | `REAL`，单位 mm；角度用**度**；温度 ℃；时长秒 | SRS 阈值表就是 45°/0.5 mm，换单位会让字典与验收条款对不上 |
| 布尔 | SQLite `INTEGER` + `CHECK IN (0,1)`；PG `boolean` | 避免 `'false'` 字符串被当真 |
| 枚举 | 存英文 code，中文标签在 `dictionary_entry` | UI-008 要求界面中文，但数据库不该存中文枚举 |
| 哈希 | `TEXT` + `length=64` CHECK（小写十六进制） | 大小写混用会让去重失效 |
| JSON | SQLite `TEXT` + `json_valid` CHECK；PG `jsonb` | 只用于"整体读写、无跨行引用"的字段 |

---

## 5. 状态机速查

| 对象 | 字段 | 合法取值 | 关键迁移 |
|---|---|---|---|
| 版本 | `kind` | initial / manual / agent / rollback / import | 无状态机，行级不可变 |
| 切片 | `status` | computing / ready / failed / stale | ready → stale（版本推进触发）；重算产生新行 |
| 打印作业 | `status` | created / precheck_failed / ready / running / paused / interrupted / curing / cooling / completed / failed / aborted | running → paused → running；任意 → failed/aborted |
| 打印作业 | `phase` | idle / printing / curing / cooling | 与 status 正交，用来区分"打印中暂停"与"固化中暂停" |
| 中断 | `kind` / `resume_decision` | motion_pause / curing_pause / power_loss / fault_stop | curing_pause 必须 recompute_hold，禁止直接续时 |
| 缺陷 | `status` | pending / fixed / ignored | 可回 pending；归因不改变状态（在 attribution） |
| 计划 | `status` | proposed / running / awaiting_confirm / converged / stopped_no_improvement / stopped_budget / rejected / failed / replayed | 三种终止各有专名，便于统计 |
| 步骤 | `status` | pending / awaiting_confirm / applied / skipped / rejected / failed | 终态不可回退 |
| 授权 | `auth_level` | suggest_only / step_confirm / auto | 默认 step_confirm；每次处置留 `auth_level_at_time` 快照 |
| 文件校验 | `verify_status` | unverified / ok / missing / mismatch | 打开工程时批量校验外部文件 |

---

## 6. 需求冲突的裁定（建模层面）

SRS v1.1 内部有 16 处需要取舍的地方，逐条裁定见 `01_分析/需求冲突与取舍.md`。落在数据结构上的关键四条：

| # | 冲突 | 本文裁定 |
|---|---|---|
| C1 | PRJ-005"撤销不产生版本" vs DAT-002"撤销通过新版本实现" | 以 PRJ-005 为准；`operation` 承载撤销，`version` 只在提交时产生 |
| C2 | 层厚相加 0.44 mm vs 目标总厚 0.35–0.40 mm | 层厚为唯一真值，`total_thickness_mm` 只记录与告警，不判为错误 |
| C5 | 缺陷归因"到设计版本"，作业却绑定切片 | `defect` 同时固化 `print_job_id` / `slice_set_id` / `design_version_id`，并用触发器校验三者一致 |
| C6 | DAT-004 要求内容寻址，工时说明又写"不做内容寻址" | 比赛版做"哈希去重 + 引用计数"最小实现；工程包一律内嵌 blob |

---

## 7. 表清单总览

<!--TABLES_OVERVIEW-->

---

## 8. 怎么用这套交付物

```bash
# 1) 建库（SQLite，目标运行时）
sqlite3 app.db < 03_ddl/schema_sqlite.sql

# 2) 每次连接都要开外键（SQLite 的外键是连接级开关）
#    PRAGMA foreign_keys = ON;  journal_mode=WAL;  synchronous=FULL;

# 3) 跑校验：建库 + 插全链样例 + 打不变量
python3 tools/validate.py

# 4) 改了模型之后重新生成四份产物
python3 tools/gen_ddl.py && python3 tools/gen_dict.py && python3 tools/gen_er.py && python3 tools/gen_trace.py
```

迁移约定：`PRAGMA user_version` 与 `project.data_schema_version` 双记录；迁移前自动备份（DAT-006）；`blob` 的物理删除必须显式触发并留 `blob_gc_log`。
