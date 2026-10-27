"""生成最小 STL 夹具（ASCII 与二进制），供测试与演示使用。"""
from __future__ import annotations

import struct


def make_ascii_stl(w: float = 20.0, h: float = 10.0, slope_deg: float = 45.0) -> bytes:
    """一个底面 + 一个指定悬垂角的斜面。"""
    import math

    rad = math.radians(slope_deg)
    # 斜面沿 +Y 方向抬起：法向与 Z 轴夹角 = slope_deg
    d = h / max(1e-6, math.tan(rad)) if slope_deg < 89.9 else 0.01
    tris = [
        ((0, 0, 0), (w, 0, 0), (w, h, 0)),
        ((0, 0, 0), (w, h, 0), (0, h, 0)),
        # 关键：第三点必须离开 y=0 平面，否则法向沿 Y（斜 90°）
        ((0, 0, 0), (w, 0, 0), (0, d, h)),
    ]
    lines = ["solid fixture"]
    for a, b, c in tris:
        lines += ["  facet normal 0 0 0", "    outer loop"]
        for p in (a, b, c):
            lines.append(f"      vertex {p[0]:.6f} {p[1]:.6f} {p[2]:.6f}")
        lines += ["    endloop", "  endfacet"]
    lines.append("endsolid fixture")
    return ("\n".join(lines) + "\n").encode()


def make_binary_stl(w: float = 20.0, h: float = 10.0) -> bytes:
    """两个三角面组成的矩形，用于验证二进制解析。"""
    tris = [
        ((0, 0, 0), (w, 0, 0), (w, h, 0)),
        ((0, 0, 0), (w, h, 0), (0, h, 0)),
    ]
    out = bytearray(b"\0" * 80)
    out += struct.pack("<I", len(tris))
    for a, b, c in tris:
        out += struct.pack("<3f", 0.0, 0.0, 1.0)          # 文件里的 normal（解析时不用）
        for p in (a, b, c):
            out += struct.pack("<3f", *p)
        out += struct.pack("<H", 0)
    return bytes(out)
