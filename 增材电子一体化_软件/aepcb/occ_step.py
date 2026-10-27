"""OpenCASCADE 增强的 STEP 解析（可选依赖）。

设计原则：**增强不替换**。
    装不上 cadquery-ocp 时 `available()` 返回 False，调用方退回 `step.py` 的
    子集解析 + `STEP-UNSUPPORTED-GEOMETRY` 告警，整条链路照常跑。

为什么值得做：
    子集解析只认平面/圆柱面，遇到 NURBS 曲面只能告警说“可能不准”；
    用 OCC 读进来三角化后，法向是算出来的，R3 悬垂判定才真正覆盖曲面件。
"""
from __future__ import annotations

import math
from pathlib import Path


def available() -> bool:
    try:
        import OCP  # noqa: F401
        return True
    except Exception:
        return False


def version() -> str | None:
    if not available():
        return None
    import OCP
    for attr in ("__version__", "VERSION", "__VERSION__"):
        v = getattr(OCP, attr, None)
        if v:
            return str(v)
    return "unknown"


def triangulate(path: str | Path, deflection: float = 0.2,
                angular: float = 0.5) -> dict | None:
    """读 STEP → 三角化 → 返回面法向、包围盒与三角形数。

    `deflection` 是弦高容差（mm）：越大越粗、越快；
    0.2 mm 对“板级结构件的悬垂判定”够用，再细对 R3 没有意义。

    返回 None 表示 OCC 不可用或文件读不进来——调用方必须能处理这种情况。
    """
    if not available():
        return None
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.Bnd import Bnd_Box
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        return None
    reader.TransferRoots()
    shape = reader.OneShape()

    BRepMesh_IncrementalMesh(shape, deflection, False, angular, True)

    buckets: dict[int, dict] = {}
    triangles = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            n = tri.NbTriangles()
            triangles += n
            for i in range(1, n + 1):
                a, b, c = tri.Triangle(i).Get()
                p1, p2, p3 = tri.Node(a), tri.Node(b), tri.Node(c)
                ux, uy, uz = (p2.X() - p1.X(), p2.Y() - p1.Y(), p2.Z() - p1.Z())
                vx, vy, vz = (p3.X() - p1.X(), p3.Y() - p1.Y(), p3.Z() - p1.Z())
                nx = uy * vz - uz * vy
                ny = uz * vx - ux * vz
                nz = ux * vy - uy * vx
                norm = math.sqrt(nx * nx + ny * ny + nz * nz)
                if norm < 1e-12:
                    continue
                nz_n = abs(nz / norm)
                slope = math.degrees(math.acos(max(-1.0, min(1.0, nz_n))))
                area = math.sqrt((ux * ux + uy * uy + uz * uz) *
                                 (vx * vx + vy * vy + vz * vz)) / 2.0
                # 按 10° 一个桶聚合：几十万三角面不能原样塞进模型
                k = min(9, int(slope // 10))
                rec = buckets.get(k)
                if rec is None:
                    buckets[k] = {"slope_deg": slope,
                                  "normal": (nx / norm, ny / norm, nz / norm),
                                  "area_mm2": area, "triangles": 1}
                else:
                    rec["area_mm2"] += area
                    rec["triangles"] += 1
        exp.Next()

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, True)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()

    return {
        "buckets": list(buckets.values()),
        "triangles": triangles,
        "bbox": (xmin, ymin, zmin, xmax, ymax, zmax),
        "size": (xmax - xmin, ymax - ymin, zmax - zmin),
        "max_slope_deg": max((b["slope_deg"] for b in buckets.values()), default=0.0),
        "deflection_mm": deflection,
    }
