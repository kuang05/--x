#!/usr/bin/env python3
"""验证 OCC 增强解析：生成夹具 STEP → 三角化 → 打印法向与悬垂角。

用法：
    python3 tools/check_occ.py            # 用当前解释器
    /opt/venv/bin/python tools/check_occ.py   # 装到独立 venv 时

退出码：0 可用且跑通；1 不可用或失败。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aepcb import occ_step, step  # noqa: E402


def main() -> int:
    print(f"OCC 可用：{occ_step.available()}")
    if not occ_step.available():
        print("提示：pip install 'aepcb[occ]'（本机磁盘不足时需另建 venv 到大分区）")
        return 1
    print(f"OCC 版本：{occ_step.version()}")

    src = step.make_fixture_step(60, 40, 1.2, overhang_deg=60, with_cylinder=True)
    with tempfile.NamedTemporaryFile("w", suffix=".step", delete=False,
                                     encoding="utf-8") as f:
        f.write(src)
        path = f.name
    try:
        out = occ_step.triangulate(path, deflection=0.2)
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    if not out:
        print("三角化失败：OCC 读不进这个文件")
        return 1

    print(f"三角面：{out['triangles']}，法向桶：{len(out['buckets'])}")
    print(f"包围盒：{[round(v, 2) for v in out['bbox']]}")
    print(f"尺寸：{[round(v, 2) for v in out['size']]} mm")
    print(f"最大悬垂角：{out['max_slope_deg']:.1f}°")
    for b in sorted(out["buckets"], key=lambda x: -x["slope_deg"])[:4]:
        print(f"  桶：{b['slope_deg']:5.1f}°  法向 {tuple(round(v, 2) for v in b['normal'])}"
              f"  面积 {b['area_mm2']:.1f} mm²  面片 {b['triangles']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
