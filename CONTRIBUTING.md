# 参与与开发说明

## 环境准备

```bash
cd 增材电子一体化_软件
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r ../requirements.lock
python3 -m aepcb.cli doctor        # 11 项自检，必需项应全绿
```

依赖装不上时先看 `doctor` 的提示；`pyproject.toml` 声明的 `opencv-python-headless`
与锁文件里的 `opencv-python` 都提供 `import cv2`，二者择一即可。

## 提交前必须通过的四道门

```bash
python3 -m unittest discover -s tests     # 1) 单测 69 项
python3 -m aepcb.cli accept               # 2) 验收 T01–T25（P0 失败即退出码 1）
python3 tools/check_deps.py               # 3) import 与 pyproject 声明一致
node --check <(提取的页面脚本)             # 4) 前端改动必跑（Python 查不出 JS 语法错误）
```

第 4 项的提取方式：

```bash
python3 -c "
import re, pathlib
from aepcb import web
js = re.search(r'<script>(.*?)</script>', web.INDEX, re.S).group(1)
pathlib.Path('/tmp/ui.js').write_text(js, encoding='utf-8')
" && node --check /tmp/ui.js
```

`make ci` 会把前两项和切片基准串起来跑；网关限制单次命令时长时，分开执行。

## 代码约定

| 约定 | 说明 |
|---|---|
| 数据模型单一事实源 | 只改 `增材电子一体化_数据模型/schema/*.py`，DDL/字典/ER/追溯都是生成物，手改会被覆盖 |
| 生成后必跑校验 | `tools/gen_ddl.py` → `gen_dict` → `gen_er` → `gen_trace` → `validate.py`（41 项） |
| DDL 必须幂等 | 所有 `CREATE TABLE/INDEX/TRIGGER/VIEW` 带 `IF NOT EXISTS`，否则旧库升级会崩 |
| 新增验收项 | 在 `accept.py` 的 `TASKS` 里登记级别与断言，并保证 `accept --list` 能看到 |
| 性能断言 | 阈值设在噪声之外；精确数字进报告，不写死在测试里 |
| 演示物不能造假 | 做不到就写“未验证”，例如 OCC 路径标注为未在本机验证 |

## 分支与提交

- 主分支 `main`，直接提交需先过四道门；功能分支建议 `feat/<简述>`。
- 提交信息写「改了什么 + 为什么」，性能类改动附前后数字。
- 版本推进时同步更新 `CHANGELOG.md` 与 `pyproject.toml` 的 `version`，并打附注标签。

## 目录职责

| 目录 | 职责 |
|---|---|
| `aepcb/` | 业务代码：结构/走线/切片/打印/界面/CLI |
| `tests/` | 端到端用例，按 `test_NN_*` 编号，编号与功能一一对应 |
| `tools/` | 生成与验证脚本（bench / pg_verify / check_deps / ui_bench / check_occ） |
| `fixtures/` | 固定验收板与 STL 夹具 |
| `../增材电子一体化_数据模型/` | 数据模型单一事实源与生成产物 |
| `../参赛材料/` | 赛事研究、申报书、演示脚本、提交物清单 |

## 遇到问题

先跑 `python3 -m aepcb.cli doctor`，它会指出缺哪个依赖、哪张表没建、工作目录是否可写。
数据库异常时可删 `workspace/app.db` 重建（会丢数据，仅限本地调试）。
