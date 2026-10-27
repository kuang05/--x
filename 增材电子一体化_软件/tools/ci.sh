#!/usr/bin/env bash
# 一条命令跑完可自动化的验收：单测 → 验收 → 切片基准 → UI 性能 → 依赖一致性。
# 失败即中断（set -e），可直接接 CI。
#
# 注：若网关/沙箱限制单次命令时长（如 60 s），请分段执行 2/5、3/5 步骤。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/5 端到端用例 =="
python3 -m unittest discover -s tests

echo "== 2/5 T01–T25 验收 =="
rm -f workspace/app.db workspace/app.db-wal workspace/app.db-shm
python3 -m aepcb.cli accept

echo "== 3/5 切片性能基准（取中位数） =="
python3 tools/bench.py

echo "== 4/5 UI 性能实测（UI-009） =="
python3 tools/ui_bench.py --repeat 3

echo "== 5/5 依赖声明一致性 =="
python3 tools/check_deps.py

echo "== 全部通过 =="
