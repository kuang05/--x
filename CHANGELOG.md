# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本号从 v1.5.0 起按「可运行基线」推进，每个 tag 对应一次全量回归通过。

## [v1.7.7] — 后台任务提交覆盖

### 新增
- `test_73_submit_slice_job`：覆盖 `web._submit_slice()` 的完整链路——
  返回 `job_id`、后台任务跑到 `done`、进度到 100%。

### 指标
- 单测 73/73。
- 行覆盖率 70.7% → **70.9%**（3811/5373）；`web.py` 53.8% → **56.7%**。

## [v1.7.6] — 动作链覆盖

### 新增
- `test_72_web_action_chain`：覆盖界面动作链——切片 → 导出 G-code → 虚拟打印（缺陷归因）
  → 智能体 → 报告生成，以及未知操作抛错。

### 指标
- 单测 72/72。
- 行覆盖率 70.3% → **70.7%**（3800/5373）；`web.py` 48.6% → **53.8%**。

## [v1.7.5] — 接口层覆盖与视频分镜

### 新增
- `test_71_web_api_mutations`：直接覆盖 `web.py` 的接口分支——画线、改宽、改道、
  整网改宽、软删除、动作命令、未切片时导出报错。
- `参赛材料/07_演示视频分镜.md`：5 分钟 8 镜 + 3 分钟精简版，含画面/操作/字幕/旁白、
  录制准备与「不要做的事」。

### 指标
- 单测 71/71。
- 行覆盖率 69.2% → **70.3%**（3777/5373）；`web.py` 38.3% → **48.6%**。

## [v1.7.4] — 板框曲线与覆盖率工具

### 新增
- `kicad.py`：解析 `Edge.Cuts` 上的 `gr_curve`（三次贝塞尔），离散成 9 点折线；
  此前只有直线/矩形/圆/弧/多边形。
- `tools/coverage_lite.py`：用标准库 `sys.settrace` 统计 `aepcb/*.py` 行覆盖率，
  不依赖第三方包（本机 pip 装 `coverage` 多次失败）；报告写 `workspace/coverage.md`。
- `参赛材料/06_答辩PPT逐页要点.md`：15 页逐页要点（标题/要点/配图/口播）+ 排版建议。

### 验证
- 单测 70/70（新增 `test_70_gr_curve_outline`）。

## [v1.7.1] — 旧库自动补表

### 修复
- **升级后打开旧库会崩**：`store.py` 只在「库不存在或完全没有表」时执行 DDL，
  59 表时代建的库被误判为已就绪，访问新表报 `no such table: copper_zone`。
- DDL 全部改为幂等：`CREATE TABLE / INDEX / UNIQUE INDEX / TRIGGER / VIEW`
  一律带 `IF NOT EXISTS`；每次打开数据库都执行一遍，旧库自动补建新增的表。

### 验证
- 单测 69/69、验收 25/25、PostgreSQL PASS。

## [v1.7.0] — STL 导入

### 新增
- `aepcb/stl.py`：ASCII 与二进制 STL 解析。法向由顶点叉积计算，
  **不采信文件里的 `facet normal`**（坏文件常写错）；几十万面按 10° 分桶，
  每桶取面积最大的面作代表，不把面片塞进模型。
- `board.import_step` 兼容 `bytes` 输入，STL 与 STEP 走同一落库路径。
- 数据模型：`step_schema` 枚举加 `STL`，`entity_kind` 枚举加 `mesh`。
- `fixtures/stl_fixture.py`：可生成指定悬垂角的 ASCII STL 与二进制 STL。

### 说明
- 这是 **STEP 完整 B-rep 的替代路径**：OCC 需 457 MB + vtk 300 MB，
  本机磁盘与带宽都不足；CAD 导出 STL 后，曲面件的 R3 判定同样成立。

## [v1.6.5] — OCC 自检工具

### 新增
- `tools/check_occ.py`：生成夹具 STEP → 三角化 → 打印法向桶与最大悬垂角，
  可在任意装了 `cadquery-ocp` 的机器上自证 OCC 路径是否可用。

## [v1.6.4] — OCC 增强路径（可选）

### 新增
- `aepcb/occ_step.py`：`STEPControl_Reader` 读文件 → `BRepMesh` 三角化 →
  法向按 10° 分桶。
- `step.parse_enhanced()`：只在**遇到不支持几何**且 OCC 可用时才三角化；
  不可用时给 `STEP-OCC-UNAVAILABLE`，链路不中断。
- `pyproject` 增加 `occ` 可选依赖；`check_deps` 识别 `cadquery-ocp → OCP`。

## [v1.6.3] — keepout 区分与焊盘旋转

### 修复
- 带 `keepout` 属性的 KiCad zone 归入**禁布区**，不再误当铜皮参与短路判定。
- custom 焊盘的多边形顶点按封装旋转矩阵变换（此前只做平移）。

## [v1.6.2] — 异形板框

### 新增
- 解析 `Edge.Cuts` 的 `gr_line / gr_rect / gr_circle / gr_arc / gr_poly`。
- 板尺寸优先取真实轮廓；面积用鞋带公式计算，异形板不再按外接矩形估。

## [v1.6.1] — STEP 告警与变形焊盘

### 新增
- `step.py`：识别 10 类不支持的几何（NURBS 曲面×3、ADVANCED_FACE、
  环面/锥面/球面、旋转面、拉伸面、圆柱延伸面），导入即给
  `STEP-UNSUPPORTED-GEOMETRY` 告警，**不再静默忽略**。
- `kicad.py`：焊盘 shape 映射 circle/oval/roundrect/trapezoid/custom，
  custom 保留多边形顶点并按外接矩形近似，同时写 `pad_warnings`。

## [v1.6.0] — KiCad 铺铜与弧形走线

### 新增
- 数据模型新增 `copper_zone` 表（59 → 60 表 / 666 字段）。
- `kicad.py`：解析 `zone`（多边形、priority、filled）与 `arc`
  （三点弧离散成折线）。
- `mrc` R2：铺铜与异网走线重叠判**短路**，距离不足判间距违规。

## [v1.5.0] — 可运行基线

### 内容
- 四域链路：结构（STEP 子集）/ 走线（KiCad 子集）/ 切片 / 打印。
- 数据模型 59 表 / 658 字段，SQLite 与 PostgreSQL 双份 DDL 均实跑。
- 验收 T01–T25（P0 20 项），`accept --list` 输出清单。
- 界面：五区布局、2D/3D、3D 旋转缩放平移适配、剖切与两点测距、
  拖拽改道、器件移动/旋转/翻面/锁定/吸附、快捷键、切片进度与取消。
- 证据包 8 节：运行环境 / 可打性 / 切片与导出 / 连通性 / Agent 计划 /
  验收结果 / UI 性能 / 代理模型。
- 性能：blob 内容哈希缓存，MRC 评分 27.8 → 3.0 ms，切片 1.03 → 0.46 s。
- 代理模型：宽度扫描 2.77×；Agent 路径为负收益，默认关闭。
