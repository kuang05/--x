"""STL 解析（ASCII + 二进制）：三角面片 → 法向 → 悬垂角。

为什么需要它：
    STEP 的 NURBS 要 OpenCASCADE，本机磁盘装不下（见 v1.6.4/v1.6.5 记录）；
    而 STL 是三角网格，纯 numpy 就能算出**精确**法向。
    所以在 CAD 里把 STEP 导成 STL，再进本软件，曲面部件的 R3 悬垂判定同样成立。

口径说明：
    STL 没有单位，按 mm 处理；没有拓扑，只有三角面；法向由顶点叉积算，
    与文件里写的 facet normal 无关——坏文件常有错误法向，不能信。
"""
from __future__ import annotations

import struct

import numpy as np

from .step import Face, StepModel


def _is_ascii(data: bytes) -> bool:
    head = data[:512].lstrip()
    return head.startswith(b"solid") and b"facet" in data[:4096]


def _parse_ascii(data: bytes) -> np.ndarray:
    """返回 (n,3,3) 顶点数组。"""
    tris: list[list[tuple[float, float, float]]] = []
    cur: list[tuple[float, float, float]] = []
    for raw in data.decode("utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if line.startswith("vertex"):
            parts = line.split()
            if len(parts) >= 4:
                cur.append((float(parts[1]), float(parts[2]), float(parts[3])))
                if len(cur) == 3:
                    tris.append(cur)
                    cur = []
    return np.asarray(tris, dtype=float) if tris else np.zeros((0, 3, 3))


def _parse_binary(data: bytes) -> np.ndarray:
    if len(data) < 84:
        return np.zeros((0, 3, 3))
    count = struct.unpack_from("<I", data, 80)[0]
    expect = 84 + count * 50
    if count == 0 or expect > len(data) + 1:
        return np.zeros((0, 3, 3))
    tris = np.empty((count, 3, 3), dtype=float)
    for i in range(count):
        off = 84 + i * 50 + 12            # 跳过文件里的 normal
        v = struct.unpack_from("<9f", data, off)
        tris[i] = [(v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8])]
    return tris


def parse(data: bytes) -> StepModel:
    """解析 STL，产出与 step.StepModel 同构的模型，供上层无差别使用。"""
    m = StepModel()
    m.schema = "STL"
    tris = _parse_ascii(data) if _is_ascii(data) else _parse_binary(data)
    if tris.size == 0:
        m.diagnostics.append(("STL-EMPTY", "fatal", "未解析到任何三角面"))
        return m

    pts = tris.reshape(-1, 3)
    uniq = np.unique(np.round(pts, 6), axis=0)
    m.points = [tuple(p) for p in uniq[:20000]]
    m.entity_kind = "mesh"

    # 法向用顶点叉积算：文件里的 facet normal 不可信
    u = tris[:, 1] - tris[:, 0]
    v = tris[:, 2] - tris[:, 0]
    n = np.cross(u, v)
    norm = np.linalg.norm(n, axis=1)
    keep = norm > 1e-12
    n = n[keep] / norm[keep, None]
    if len(n) == 0:
        m.diagnostics.append(("STL-DEGENERATE", "warn", "全部三角面退化，无法算法向"))
        return m

    nz = np.abs(n[:, 2])
    slopes = np.degrees(np.arccos(np.clip(nz, -1.0, 1.0)))
    # 按 10° 分桶，取每桶面积最大的面作代表；几十万面不能逐个塞进模型
    areas = (np.linalg.norm(u[keep], axis=1) * np.linalg.norm(v[keep], axis=1)) / 2.0
    buckets: dict[int, int] = {}
    for i, s in enumerate(slopes):
        k = min(9, int(s // 10))
        if k not in buckets or areas[i] > areas[buckets[k]]:
            buckets[k] = i
    m.faces = [Face(tuple(float(x) for x in n[i]), float(slopes[i]))
               for i in buckets.values()]

    m.diagnostics.append((
        "STL-MESH", "info",
        f"三角面 {len(tris)} 个，有效法向 {int(keep.sum())} 个，"
        f"法向桶 {len(buckets)} 个（按面积取代表）"))
    if len(tris) < 12:
        m.diagnostics.append(("STL-FEW-TRIANGLES", "warn",
                              f"仅 {len(tris)} 个三角面，包围盒可能不准"))
    x0, y0, z0 = pts.min(axis=0)
    x1, y1, z1 = pts.max(axis=0)
    if (x1 - x0) <= 1e-9 or (y1 - y0) <= 1e-9:
        m.diagnostics.append(("STL-DEGENERATE", "error", "包围盒存在零尺寸"))
    return m
