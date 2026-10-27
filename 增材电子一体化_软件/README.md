# 增材电子一体化软件 · 可运行实现（v1.26.0）

把"需求 → 数据模型 → 可跑软件"打通的版本：**Python 标准库 + numpy/scipy/OpenCV**，无服务依赖。
含单页界面（2D/3D、图层开关、画线/改道、整网批量改宽、撤销与回退到指定步）、
KiCad 解析（F.Cu/In{n}.Cu/B.Cu 分层与盲埋孔）、A* 自动布线（换层自动打过孔）、跳线修复、
三层七道次切片（**轮廓 + 内填 + 层间正交交替**）、切片后连通性、双哈希 G-code、
Marlin 真机驱动、虚拟打印与缺陷归因、可重放 Agent 回路、整库/单工程两种工程包。

```bash
python3 -m aepcb.cli demo            # 全链演示（终端）
python3 -m aepcb.cli serve           # 浏览器界面 http://127.0.0.1:8765
python3 -m aepcb.cli accept          # T01–T25 验收，P0 失败时退出码 1
python3 -m aepcb.cli kicad board.kicad_pcb   # 导入真实 KiCad 板文件
python3 -m unittest discover -s tests -v     # 64 个端到端用例
python3 -m aepcb.cli doctor          # 环境自检（依赖/数据库/可选组件）
python3 tools/bench.py               # 切片性能基准
python3 tools/pg_verify.py           # PostgreSQL 实跑验证（需 pgserver）

make demo / test / accept / bench / pg / ci   # 等价的一键入口

# 分步命令（真实使用场景）
python3 -m aepcb.cli new --name 我的板 --w 60 --h 40
python3 -m aepcb.cli import --step board.step --board board.json
python3 -m aepcb.cli check / slice / export / print / agent
python3 -m aepcb.cli package --out project.aepkg
python3 -m aepcb.cli restore project.aepkg
python3 -m aepcb.cli rules --set R1.min_width_mm=0.4 --label v2   # 阈值版本化
python3 -m aepcb.cli evidence --out workspace/evidence.md   # 验收证据包（MD + JSON）
python3 -m aepcb.cli pitch            # 比赛一键：清库 → 全链验收 → 报告 + 证据
python3 -m aepcb.cli surrogate        # 训练候选排序代理模型（规则蒸馏）
python3 -m aepcb.cli sweep            # 宽度扫描：代理粗排 + 规则确认（5 候选 2.77×）
```

## 实测结果

| 环节 | 结果 |
|---|---|
| STEP 导入 | AP214，60×40×1.2 mm，单位 mm；平面法向 + **圆柱面（孔壁/立柱）**均参与 R3 |
| 板文件导入 | 8 器件 / 24 焊盘 / 12 网络 / 12 走线（JSON 与 KiCad 两种入口） |
| 连通性 | 断路 0 / 短路 0；支持分段拼接与跳线直连 |
| 可打性 | **62.0 分**，问题 6 条（R1×3、R2、R6、R7） |
| 自动布线 | 清空 12 条网络后 **布通 12/12**，层 0 被挡自动换层 1，并**在换层处自动打过孔** |
| 跳线 | 断开一个网络 → 候选 → 应用 → 连通性恢复 |
| 切片 | 3 层 / 7 道次 / 1304 段路径 / 24.98 m / 总厚 0.44 mm；轮廓→内填→空移排序，层间正交交替 |
| 切片后连通性 | 覆盖式判据；删除路径即报断路并**阻断 G-code 导出** |
| G-code | 双哈希，重导出幂等；**分道次进给**（介电 12 mm/s → F720，银浆 6 mm/s → F360） |
| 虚拟打印 | 7 层回放，注入断线 → 缺陷 → 归因；固化暂停→续打会**重算剩余保温**（PRT-004） |
| 真机驱动 | Marlin 子集（行号+校验和+ok/resend），回环假固件实测 7 命令 6 ok |
| Agent | **R1/R2/R7/R4/R5 五类可修**；62 → 97 分，4 轮收敛，重放分数完全一致 |
| 撤销/重做 | 改宽/改道/**删除**都可撤销，version 行数全程不变（PRJ-005） |
| 规则版本 | CLI `rules --set` 与界面「新建并重评」都可建版本；旧分数不被覆盖（MRC-011） |
| 过孔 | `via` 入模型（60 表/666 字段）；跨层无过孔判断路，加孔后 pass；KiCad 往返保留 |
| KiCad 分层 | `F.Cu=0 / In{n}.Cu=n / B.Cu=15`，盲埋孔层对原样保留（往返测 layer 0/1/15） |
| KiCad 旋转 | 封装旋转补偿到焊盘绝对坐标；非 90° 倍数归一化并记 `rotation_warnings` |
| 工程包 | 整库导出 zip（行数据 + blob + 清单哈希）→ 空库导入后行数与 MRC 分数一致 |
| 单工程工程包 | 按 `project_id` 串联裁剪（规则/工具/材料/blob 闭包），导入后库内只剩该工程 |
| 包合并导入 | `merge=True` 时全量 id 重映射；material/全局工艺配置去重，blob 按哈希忽略重复；合并后分数一致 |
| 自检 | `doctor` 11/11 通过（依赖含 PIL/cv2/sklearn、60 表建库、工作目录、可选 pgserver/pyserial） |
| 验收 | **25/25 通过（T01–T25），P0 零失败 → PASS** |
| 单测 | **64/64 通过** |
| 界面 | **五区布局**（顶栏/四域/画布/侧栏/底栏）、2D/3D、**3D 旋转缩放平移适配**、**剖切与两点测距**、图层开关、画线/拖拽改道、**器件移动/旋转/翻面/锁定**、整网与批量改宽、**建议宽度**、操作栈回退、规则版本重评、**快捷键**、**切片进度与取消** |
| UI 性能 | 选择响应 **17.7 ms**（≤50）、改宽 **3.2 ms**（≤120）、三维投影 **1.12 ms/帧**（20 万顶点，**仅计算不含渲染**） |
| 证据包 | `evidence` 导出 Markdown+JSON：规则与阈值、评分历史、切片/G-code 双哈希、连通性、计划步骤、T01–T24；**`accept` 跑完自动产出** |
| 性能 | 切片 60×40 **0.4–0.7 s**、200×200 **2.4–4.8 s**（同机多次波动；冷缓存时为 1.03 / 2.91 s） |
| MRC 评分 | 缓存后 **3.0 ms**（原 27.8 ms）；候选改动后热读 **7 ms** |
| PostgreSQL | **实跑 PASS**：60 表 / 21 触发器 / 171 索引 + 5 条行为探针 |
| 演示模式 | `pitch` 一键：清库 → T01–T24 全链验收 → `report.html` + `evidence.md`（约 50 s） |
| 参赛材料 | `../参赛材料/`：赛事研究、申报书、演示与答辩要点 |
| 代理模型 | 360 样本、分组留出 ρ **0.952**；**宽度扫描场景**：5 个候选穷举 35.4 ms → 粗排+确认 **12.8 ms（2.77×）**，选择与规则最优一致；Agent 路径为负收益（89 vs 174 ms）故默认关闭 |

## 参赛定位（华为 ICT 大赛 · 创新赛）

- 赛题方向建议：**Agent 智能体设计与应用**；五维评审（创新性、系统复杂性/技术复合性、商业/社会价值、功能完备性、答辩）逐条有证据。
- 一键跑通：`python3 -m aepcb.cli pitch`，或见 `../参赛材料/README.md`。

```
增材电子一体化_软件/
├── schema.sql            60 张表（由数据模型交付物生成）
├── aepcb/
│   ├── store.py          连接/PRAGMA/UUIDv7/仓储/CAS（多线程安全）
│   ├── geometry.py       折线距离、栅格化、形态学、蛇形填充
│   ├── step.py           STEP 子集解析
│   ├── kicad.py          .kicad_pcb 子集解析（footprint/pad/segment/net，F.Cu/B.Cu）
│   ├── board.py          两种入口统一落库
│   ├── routing.py        连通性、界面等价操作、跳线、新建走线
│   ├── autoroute.py      0.5 mm 网格 A*，支持换层重试
│   ├── mrc.py            R1–R7、幂等评分
│   ├── slicing.py        三层七道次、路径生成、热历程、估算
│   ├── slicecheck.py     切片后覆盖式连通性（SLC-007）
│   ├── gcode.py          确定性导出、双哈希、连通性阻断
│   ├── printer.py        虚拟打印、暂停、故障注入、缺陷与归因
│   ├── driver.py         Marlin 主机协议 + 回环假固件
│   ├── agent.py          规则化设计循环、绝对状态步骤、重放
│   ├── history.py        撤销/重做（operation 日志，不产生版本）
│   ├── doctor.py         环境自检（依赖/建库/可选组件）
│   ├── package.py        工程包导出/导入（内嵌 blob + 整包哈希）
│   ├── accept.py         T01–T24 验收运行器
│   ├── report.py         HTML/SVG 报告（2D + 等轴 3D + 问题 + 验收）
│   ├── web.py            单页界面（stdlib HTTP + JSON API）
│   └── cli.py            命令行入口
├── Makefile              make demo/test/accept/bench/pg/ci
├── pyproject.toml        可安装（console script: aepcb）
├── fixtures/t19a.py      T19-A 固定验收板
├── tests/test_chain.py   64 个端到端用例
├── tools/bench.py        切片性能基准
├── tools/pg_verify.py    PostgreSQL 实跑验证
├── tools/ci.sh           测试 + 验收 + 基准一条命令
└── workspace/            运行时（app.db、blobs、exports、pgdata、report.html）
```

## 界面

`python3 -m aepcb.cli serve` 后打开 `http://127.0.0.1:8765`：

- **2D 板面 / 3D 等轴**双视图；层 0 红、层 1 绿、焊盘黄、器件灰块；层 0/层 1 可单独开关；
- **点击画线 / 改道**：可选目标层与线宽，在画布上点若干点 → 保存落 `route` 表；画在层 1 会自动在两端**补过孔**；
- **多工程切换**：顶栏下拉切换当前工程（选择存在 `app_setting`，刷新不丢），旁边可「新建工程」；
- **属性面板**：点选走线可改宽度（低于工艺下限会被拒）、**整网批量改宽**、**Shift+点击多选后批量改宽**（均可逐段撤销）、软删除；
- **操作栈**：行内显示每一步的序号/类型/对象/状态，点击任意行回退到该步之后；
- **规则版本**：输入 `R1.min_width_mm=0.4` 新建规则版本并立即重评，旧分数保留；
- 六个按钮驱动真实能力：检查、切片、导出、虚拟打印、Agent 优化、生成报告。

前端只做渲染与输入，所有写操作都走 `aepcb` 模块，不存在第二套存储。

## 五个关键设计（不变）

1. **数据模型就是存储层**：`schema.sql` 由 `增材电子一体化_数据模型/` 生成，缺字段回单一事实源改、重新生成。累计据此补过 `component.height_mm`、`mrc_issue.target_kind`。
2. **撤销与提交分离**：版本只在提交时产生，版本行不可 UPDATE/DELETE（SQLite 与 PG 都有触发器）。
3. **双哈希**：`content_hash` 规范化、`file_hash` 落盘字节；重复导出幂等，验收只认前者。
4. **分数只追加**：同指纹重复检查幂等，改阈值或几何才产生新 `mrc_run`。
5. **Agent 只用界面等价操作**：`route.set_width` / `route.set_points`，步骤存绝对状态，重放不重新推理。
6. **规则用真实几何**：R4/R5 读器件 `body_w_mm/body_h_mm`（不是全局常量），夹具默认 4×4 mm，改本体尺寸即刻反映到评分。

## 验收口径

`python3 -m aepcb.cli accept` 写 25 条 `accept_result`，P0 有失败则退出码 1，可直接接 CI。

| 任务 | 级别 | 断言（一句话） |
|---|---|---|
| T01 | P0 | STEP 导入包围盒 60×40 |
| T02 | P0 | 坏 STEP 只出诊断、不崩溃 |
| T03 | P1 | 拒绝布尔候选后分数不变 |
| T04 | P1 | 挖槽候选拒绝后模型与分数不变 |
| T05 | P0 | 导入坏文件后工程仍可用（STEP-EMPTY 诊断） |
| T06 | P0 | 器件/焊盘/网络/走线 8/24/12/12 |
| T07 | P0 | 连通性 pass（断路 0 / 短路 0） |
| T08 | P0 | 自动布线布通率 ≥80% 且连通 pass |
| T09 | P0 | 断路 1 → 加跳线后 pass |
| T10 | P0 | R1/R2 命中 |
| T11 | P1 | 悬垂 60° 触发 R3 |
| T12 | P0 | R6/R7 命中 |
| T13 | P0 | 3 层 7 道次 |
| T14 | P0 | 热历程 pre_gel→silver_set→cure |
| T15 | P1 | 断开网络后连通 fail（断路 ≥1） |
| T16 | P0 | 两次导出内容哈希一致、字节哈希不同 |
| T17 | P0 | 虚拟打印回放 completed |
| T18 | P0 | 故障注入产生缺陷并归因 |
| T19 | P0 | Agent 62→97，≤8 轮 |
| T20 | P0 | 重放一致 + 全链 G-code |
| T21 | P0 | 器件吸附 1.7→1.5、焊盘同步、锁定后拒绝移动 |
| T22 | P0 | 进度到 100%、取消后回滚执行 |
| T23 | P0 | 五区布局 + `device` + `chat` 字段 |
| T24 | P1 | 剖切/测量控件 + 72.11 mm 测距常数 |
| T25 | P0 | 宽度扫描 3 候选并还原几何 |

P0 失败即门禁不通过；T03/T04/T11/T15/T24 为 P1（不阻断，但计入总数）。

## 已知缺口（不掩盖）

1. **没有真机**：Marlin 协议已实现并用假固件验证，但没有物理打印机可联调；`pyserial` 未安装时 `SerialTransport` 会明确报错而不是静默降级。
2. **工程包默认整库、可单工程导出**：`export_project` 可按 `project_id` 串联裁剪；导入只能导到空库（不做 id 重映射与合并），合并导入尚未实现。
3. **界面是单页看板，不是完整 IDE**：已有画线/改道/图层开关/操作栈回退，但仍缺属性批量编辑、三维拾取与框选。
4. **STEP 只解析子集**：点、平面法向、单位、包围盒；不做 B-rep 布尔与曲面。
5. **KiCad 只支持单层贴片 + F.Cu/B.Cu 线段 + 简单过孔**：多层堆叠、异形焊盘、盲埋孔未做。
6. **过孔只建模电学连接**：PG/金属化工艺参数未入模；跨层判定依赖过孔，但孔壁可靠性、填孔工艺不在范围内。
7. **性能口径为本机**：Intel Xeon Platinum 8255C @2.5 GHz / 3 核；60×40 切片中位数 **0.46 s**、200×200 中位数 **5.81 s**（区间 2.45–7.58）；选择响应 17.7 ms、改宽 3.2 ms。SRS 指定基准机 i5-12400 未测，**本文所有性能数字均按本机记录**。

## 扩展方式

| 目标 | 改哪里 |
|---|---|
| 真机联调 | `driver.SerialTransport` + `printer` 的发送层；事件表复用 |
| 拖拽改道 | `web.py` 前端 + `routing.set_points`；数据层不用动 |
| KiCad 多层/异形焊盘 | `kicad.py` 的 pad/segment 分支，产出同一 spec dict |
| 曲面切片 | 换掉 `slicing.py` 的栅格来源，保留 `deposit_pass` / `slice_path` 结构 |

扩展时守住一条：**新能力必须落到既有表上**，不要再造一套存储，否则追溯链会断。
