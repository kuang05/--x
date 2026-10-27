"""STEP 子集解析：只取几何点、平面法向、单位与包围盒。

完整 B-rep 需要 OpenCASCADE；本模块只做"可打性判断"需要的部分：
  - 所有 CARTESIAN_POINT → 包围盒、尺寸、面数近似
  - PLANE + AXIS2_PLACEMENT_3D → 面法向 → 悬垂角（R3）
识别不了的结构不静默修补，一律给诊断码（STR-002）。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

# 本解析器不处理的几何：遇到就明确告警，而不是静默忽略。
# 静默忽略比“报错”更危险：用户会以为判定过了。
UNSUPPORTED = {
    "B_SPLINE_SURFACE": "NURBS 曲面",
    "B_SPLINE_SURFACE_WITH_KNOTS": "NURBS 曲面（带节点）",
    "RATIONAL_B_SPLINE_SURFACE": "有理 NURBS 曲面",
    "ADVANCED_FACE": "B-rep 高级面",
    "TOROIDAL_SURFACE": "环面",
    "CONICAL_SURFACE": "锥面",
    "SPHERICAL_SURFACE": "球面",
    "SURFACE_OF_REVOLUTION": "旋转面",
    "SURFACE_OF_LINEAR_EXTRUSION": "拉伸面",
    "SURFACE_OF_CYLINDRICAL_EXTENSION": "圆柱延伸面",
}

ENTITY = re.compile(r"#(\d+)\s*=\s*([A-Z0-9_]+)\s*\((.*?)\)\s*;", re.S)
POINT = re.compile(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)\s*\)")
DIRECTION = re.compile(r"DIRECTION\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)\s*\)")
CYLINDER = re.compile(r"CYLINDRICAL_SURFACE\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*([\d.eE+-]+)\s*\)")
REF = re.compile(r"#(\d+)")


@dataclass
class Face:
    normal: tuple[float, float, float]
    slope_deg: float          # 法向与水平面夹角（0=水平，90=竖直）


@dataclass
class StepModel:
    points: list[tuple[float, float, float]] = field(default_factory=list)
    faces: list[Face] = field(default_factory=list)
    units: str = "unknown"
    schema: str = "other"
    entity_kind: str = "single"
    diagnostics: list[tuple[str, str, str]] = field(default_factory=list)
    unsupported: dict[str, int] = field(default_factory=dict)

    @property
    def bbox(self) -> tuple[float, float, float, float, float, float]:
        if not self.points:
            return (0, 0, 0, 0, 0, 0)
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        zs = [p[2] for p in self.points]
        return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    @property
    def size(self) -> tuple[float, float, float]:
        x0, y0, z0, x1, y1, z1 = self.bbox
        return (x1 - x0, y1 - y0, z1 - z0)


def _nums(text: str) -> tuple[float, float, float]:
    vals = [float(v) for v in text.replace(" ", "").split(",") if v]
    while len(vals) < 3:
        vals.append(0.0)
    return vals[0], vals[1], vals[2]


def parse(text: str) -> StepModel:
    m = StepModel()
    ents: dict[str, tuple[str, str]] = {}
    for eid, kind, body in ENTITY.findall(text):
        ents[eid] = (kind, body)

    if "AUTOMOTIVE_DESIGN" in text or "AP214" in text:
        m.schema = "AP214"
    elif "AP242" in text:
        m.schema = "AP242"
    elif "AP203" in text:
        m.schema = "AP203"

    for kind, body in ents.values():
        if kind == "CARTESIAN_POINT":
            mt = POINT.search(f"CARTESIAN_POINT({body})")
            if mt:
                m.points.append(_nums(mt.group(1)))
        elif kind == "PLANE":
            mt = REF.search(body)
            if not mt:
                continue
            axis = ents.get(mt.group(1))
            if not axis or axis[0] not in ("AXIS2_PLACEMENT_3D", "AXIS1_PLACEMENT"):
                continue
            refs = REF.findall(axis[1])
            if len(refs) < 2:
                continue
            d = ents.get(refs[1])
            if not d or d[0] != "DIRECTION":
                continue
            md = DIRECTION.search(f"DIRECTION({d[1]})")
            if not md:
                continue
            nx, ny, nz = _nums(md.group(1))
            norm = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            nz_n = abs(nz / norm)
            slope = math.degrees(math.acos(max(-1.0, min(1.0, nz_n))))
            m.faces.append(Face((nx / norm, ny / norm, nz / norm), slope))
        elif kind == "CYLINDRICAL_SURFACE":
            # 圆柱面的法向是径向（水平），对 R3 而言就是 90° 悬垂（孔壁/立柱）
            if CYLINDER.search(f"CYLINDRICAL_SURFACE({body})"):
                m.faces.append(Face((1.0, 0.0, 0.0), 90.0))

    if "SI_UNIT(.MILLI.,.METRE.)" in text:
        m.units = "mm"
    elif "CONVERSION_BASED_UNIT" in text and "INCH" in text.upper():
        m.units = "inch"
    else:
        m.diagnostics.append(("STEP-UNIT-MISSING", "warn", "未声明单位，按 mm 处理，需人工确认"))

    if len(ents) > 50 and any(k == "NEXT_ASSEMBLY_USAGE_OCCURRENCE" for k, _ in ents.values()):
        m.entity_kind = "assembly"

    # 不支持的曲面/实体：统计后给出显式告警
    kinds = Counter(kind for kind, _ in ents.values())
    m.unsupported = {k: v for k, v in kinds.items() if k in UNSUPPORTED}
    if m.unsupported:
        detail = "、".join(f"{UNSUPPORTED[k]}×{v}" for k, v in sorted(m.unsupported.items()))
        m.diagnostics.append((
            "STEP-UNSUPPORTED-GEOMETRY", "warn",
            f"含 {detail}：本解析器不做 NURBS/B-rep，R3 悬垂与 R4 可达判定可能不准"))
    if not m.points:
        m.diagnostics.append(("STEP-EMPTY", "fatal", "未找到任何几何点，文件可能损坏或非 STEP"))
    elif len(m.points) < 8:
        m.diagnostics.append(("STEP-FEW-POINTS", "warn", f"仅 {len(m.points)} 个点，疑似残缺实体"))
    if not m.faces:
        m.diagnostics.append(("STEP-NO-PLANE", "warn", "未解析到平面，R3 悬垂检查将跳过"))
    w, h, t = m.size
    if m.points and (w <= 0 or h <= 0):
        m.diagnostics.append(("STEP-DEGENERATE", "error", "包围盒存在零尺寸，实体退化"))
    return m


def make_fixture_step(w_mm: float, h_mm: float, thickness_mm: float,
                      overhang_deg: float = 0.0, with_cylinder: bool = False) -> str:
    """生成一个最小可解析的 AP214 文本（板 + 可选 60° 侧壁），用于演示与验收。"""
    pts = [
        (0, 0, 0), (w_mm, 0, 0), (w_mm, h_mm, 0), (0, h_mm, 0),
        (0, 0, thickness_mm), (w_mm, 0, thickness_mm),
        (w_mm, h_mm, thickness_mm), (0, h_mm, thickness_mm),
    ]
    lines = [
        "ISO-10303-21;",
        "HEADER;",
        "FILE_DESCRIPTION(('additive electronics demo'),'2;1');",
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));",
        "ENDSEC;",
        "DATA;",
        "#1=SI_UNIT(.MILLI.,.METRE.);",
    ]
    n = 2
    ids = []
    for x, y, z in pts:
        lines.append(f"#{n}=CARTESIAN_POINT('',({x:.3f},{y:.3f},{z:.3f}));")
        ids.append(n)
        n += 1
    # 顶面 + 底面：法向 +Z / -Z（水平，不触发悬垂）
    lines.append(f"#{n}=DIRECTION('',(0.,0.,1.));")
    dz = n
    n += 1
    lines.append(f"#{n}=DIRECTION('',(1.,0.,0.));")
    dx = n
    n += 1
    lines.append(f"#{n}=AXIS2_PLACEMENT_3D('',#{ids[0]},#{dz},#{dx});")
    ax = n
    n += 1
    lines.append(f"#{n}=PLANE('',#{ax});")
    n += 1
    if overhang_deg > 0:
        rad = math.radians(overhang_deg)
        nx = math.sin(rad)
        nz = math.cos(rad)
        lines.append(f"#{n}=DIRECTION('',({nx:.6f},0.,{nz:.6f}));")
        dy = n
        n += 1
        lines.append(f"#{n}=AXIS2_PLACEMENT_3D('',#{ids[0]},#{dy},#{dx});")
        ax2 = n
        n += 1
        lines.append(f"#{n}=PLANE('',#{ax2});")
        n += 1
    if with_cylinder:
        # 圆柱面：法向径向水平，对 R3 而言就是 90° 悬垂（孔壁/立柱）
        lines.append(f"#{n}=AXIS2_PLACEMENT_3D('',#{ids[0]},#{dz},#{dx});")
        ax3 = n
        n += 1
        lines.append(f"#{n}=CYLINDRICAL_SURFACE('',#{ax3},2.5);")
        n += 1
    lines += ["ENDSEC;", "END-ISO-10303-21;"]
    return "\n".join(lines) + "\n"
