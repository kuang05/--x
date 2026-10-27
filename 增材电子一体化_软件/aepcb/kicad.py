"""KiCad `.kicad_pcb` 子集解析：footprint / pad / segment / net → 板规格 dict。

只处理单层板（F.Cu）与顶层贴片焊盘，图层映射与旋转暂按 0 处理——
这些限制写在这里，而不是藏在代码里。产出结构与 `fixtures.t19a` 完全一致，
所以导入、检查、切片、导出全链路不需要任何改动。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')


def _tokenize(text: str) -> list[str]:
    return TOKEN.findall(text)


def _parse(tokens: list[str]) -> list:
    stack: list[list] = [[]]
    for tok in tokens:
        if tok == "(":
            new: list = []
            stack[-1].append(new)
            stack.append(new)
        elif tok == ")":
            if len(stack) > 1:
                stack.pop()
        else:
            if tok.startswith('"') and tok.endswith('"'):
                tok = tok[1:-1]
            stack[-1].append(tok)
    return stack[0]


def _items(node: list, name: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def _first(node: list, name: str):
    for c in node:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def _floats(node, count=2, offset=1) -> list[float]:
    out = []
    for v in node[offset:offset + count]:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(0.0)
    while len(out) < count:
        out.append(0.0)
    return out


def _chain(segments: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """把零散线段拼成折线（端点距离 < 0.01 视为相连）。"""
    segs = [list(s) for s in segments]
    merged: list[list[tuple[float, float]]] = []
    while segs:
        cur = segs.pop(0)
        changed = True
        while changed:
            changed = False
            for i, s in enumerate(segs):
                if abs(cur[-1][0] - s[0][0]) < 0.01 and abs(cur[-1][1] - s[0][1]) < 0.01:
                    cur += s[1:]
                elif abs(cur[-1][0] - s[-1][0]) < 0.01 and abs(cur[-1][1] - s[-1][1]) < 0.01:
                    cur += s[::-1][1:]
                elif abs(cur[0][0] - s[-1][0]) < 0.01 and abs(cur[0][1] - s[-1][1]) < 0.01:
                    cur = s[:-1] + cur
                elif abs(cur[0][0] - s[0][0]) < 0.01 and abs(cur[0][1] - s[0][1]) < 0.01:
                    cur = s[::-1][:-1] + cur
                else:
                    continue
                segs.pop(i)
                changed = True
                break
        merged.append(cur)
    return merged


def _layer_no(name: str) -> int:
    """KiCad 铜层名 → 层号：F.Cu=0，In{n}.Cu=n，B.Cu=15（本模型限 0–15）。"""
    name = str(name)
    if name.startswith("F."):
        return 0
    if name.startswith("B."):
        return 15
    if name.startswith("In") and "." in name:
        try:
            return max(1, min(14, int(name[2:].split(".")[0])))
        except ValueError:
            return 0
    return 0


def _layer_name(no: int) -> str:
    if int(no) == 0:
        return "F.Cu"
    if int(no) >= 15:
        return "B.Cu"
    return f"In{int(no)}.Cu"


def _arc_points(start, mid, end, steps: int = 24) -> list[tuple[float, float]]:
    """三点圆弧 → 折线。KiCad 用 start/mid/end 描述弧，不存圆心。

    弧线离散成折线后再算间距：把弧当直线会把实际弯曲长度和间距都算错。
    """
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:                      # 三点共线：退化成直线
        return [start, end]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1)
          + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3)
          + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a0, am, a2 = (math.atan2(y1 - uy, x1 - ux), math.atan2(y2 - uy, x2 - ux),
                  math.atan2(y3 - uy, x3 - ux))
    tau = 2 * math.pi
    ccw_span = (a2 - a0) % tau
    ccw_mid = (am - a0) % tau
    span = ccw_span if ccw_mid <= ccw_span else -(tau - ccw_span)
    return [(ux + r * math.cos(a0 + span * i / steps),
             uy + r * math.sin(a0 + span * i / steps)) for i in range(steps + 1)]


def parse(text: str, board_w: float | None = None, board_h: float | None = None) -> dict:
    tree = _parse(_tokenize(text))
    root = next((n for n in tree if isinstance(n, list) and n and n[0] == "kicad_pcb"), tree)
    root = root if isinstance(root, list) and root and root[0] == "kicad_pcb" else (root[0] if root else [])

    nets: dict[int, str] = {}
    for n in _items(root, "net"):
        if len(n) >= 3:
            try:
                nets[int(n[1])] = n[2]
            except (ValueError, TypeError):
                pass

    components, pads = [], []
    warnings: list[dict] = []
    pad_warnings: list[dict] = []
    for fp in _items(root, "footprint"):
        ref = None
        for prop in _items(fp, "property"):
            if len(prop) >= 3 and prop[1] == "Reference":
                ref = prop[2]
        if not ref:
            for t in _items(fp, "fp_text"):
                if len(t) >= 3 and t[1] == "reference":
                    ref = t[2]
        at = _first(fp, "at")
        fx, fy = _floats(at) if at else (0.0, 0.0)
        frot = 0.0
        if at and len(at) > 3:
            try:
                frot = float(at[3])
            except (TypeError, ValueError):
                frot = 0.0
        ref = ref or f"FP{len(components) + 1}"
        layer = _first(fp, "layer")
        side = "bottom" if layer and len(layer) > 1 and layer[1].startswith("B.") else "top"
        # 旋转需归一化到 0/90/180/270（模型 CHECK 要求）；非 90° 倍数记诊断，不静默猜
        rot_norm = int(round(frot / 90.0) * 90) % 360
        components.append({"ref": ref, "x": fx, "y": fy, "rot": rot_norm,
                           "side": side, "footprint": fp[1] if len(fp) > 1 else "FP", "h": 1.2})
        if abs(frot - rot_norm) > 1e-6:
            warnings.append({"ref": ref, "raw_rot": frot, "snapped": rot_norm})
        for pad in _items(fp, "pad"):
            if len(pad) < 2:
                continue
            pat = _first(pad, "at")
            px, py = _floats(pat) if pat else (0.0, 0.0)
            # 封装旋转必须作用到焊盘偏移上，否则贴片位置整体偏
            rad = math.radians(frot)
            cos_a, sin_a = math.cos(rad), math.sin(rad)
            ax = fx + px * cos_a - py * sin_a
            ay = fy + px * sin_a + py * cos_a
            size = _first(pad, "size")
            w, h = _floats(size) if size else (1.0, 1.0)
            # 焊盘形状：模型只收 rect/circle/roundrect/other，其余归 other 并告警
            raw_shape = pad[3] if len(pad) > 3 else "rect"
            shape_map = {"rect": "rect", "circle": "circle", "oval": "roundrect",
                         "roundrect": "roundrect", "trapezoid": "other", "custom": "other"}
            model_shape = shape_map.get(raw_shape, "other")
            polygon: list[list[float]] = []
            if raw_shape == "custom":
                prim = _first(pad, "primitives")
                for gp in (_items(prim, "gr_poly") if prim else []):
                    pts_node = _first(gp, "pts")
                    for xy in (_items(pts_node, "xy") if pts_node else []):
                        qx, qy = _floats(xy)
                        polygon.append([round(ax + qx * cos_a - qy * sin_a, 4),
                                        round(ay + qx * sin_a + qy * cos_a, 4)])
                if polygon:
                    xs = [q[0] for q in polygon]
                    ys = [q[1] for q in polygon]
                    w, h = max(xs) - min(xs), max(ys) - min(ys)
                pad_warnings.append({"pad": f"{ref}.{pad[1]}", "shape": "custom",
                                     "approx": "bounding_box",
                                     "polygon_points": len(polygon)})
            net = _first(pad, "net")
            net_name = None
            if net and len(net) >= 3:
                net_name = net[2]
            elif net and len(net) == 2:
                try:
                    net_name = nets.get(int(net[1]))
                except ValueError:
                    net_name = None
            pads.append({"comp": ref, "no": str(pad[1]),
                         "x": round(ax, 4), "y": round(ay, 4),
                         "w": w, "h": h, "shape": model_shape,
                         "polygon": polygon,
                         "net": net_name or f"N_{ref}_{pad[1]}"})

    by_net: dict[str, list[list[tuple[float, float]]]] = {}
    for seg in _items(root, "segment"):
        s, e, w = _first(seg, "start"), _first(seg, "end"), _first(seg, "width")
        if not (s and e):
            continue
        x0, y0 = _floats(s)
        x1, y1 = _floats(e)
        width = _floats(w, 1)[0] if w else 0.25
        lay = _first(seg, "layer")
        layer_no = _layer_no(lay[1]) if lay and len(lay) > 1 else 0
        net = _first(seg, "net")
        name = nets.get(int(net[1])) if net and len(net) > 1 else None
        by_net.setdefault(name or "N_UNKNOWN", []).append([(x0, y0), (x1, y1)])
        seg_widths = by_net.setdefault("__width__" + (name or "N_UNKNOWN"), [])
        seg_widths.append([width, layer_no])

    routes = []
    for key, segs in by_net.items():
        if key.startswith("__width__"):
            continue
        widths = by_net.get("__width__" + key, [[0.25, 0]])
        width = float(widths[0][0]) if widths else 0.25
        layer_no = int(widths[0][1]) if widths else 0
        for poly in _chain(segs):
            if len(poly) >= 2:
                routes.append({"net": key, "layer": layer_no, "width": width,
                               "points": [[round(x, 3), round(y, 3)] for x, y in poly]})

    # 弧形走线：离散成折线后并入同一网络的路径
    for arc in _items(root, "arc"):
        s, m, e = _first(arc, "start"), _first(arc, "mid"), _first(arc, "end")
        if not (s and m and e):
            continue
        w_node = _first(arc, "width")
        width = _floats(w_node, 1)[0] if w_node else 0.25
        lay = _first(arc, "layer")
        layer_no = _layer_no(lay[1]) if lay and len(lay) > 1 else 0
        net = _first(arc, "net")
        name = nets.get(int(net[1])) if net and len(net) > 1 else None
        pts = _arc_points(_floats(s), _floats(m), _floats(e))
        routes.append({"net": name or "N_UNKNOWN", "layer": layer_no, "width": width,
                       "points": [[round(x, 3), round(y, 3)] for x, y in pts],
                       "arc": True})

    # 铺铜：整片多边形铜箔，异网重叠即短路
    zones: list[dict] = []
    zone_keepouts: list[dict] = []
    for z in _items(root, "zone"):
        net = _first(z, "net")
        net_name = None
        if net and len(net) > 1:
            try:
                net_name = nets.get(int(net[1]))
            except (ValueError, TypeError):
                net_name = None
        lay = _first(z, "layer")
        layer_no = _layer_no(lay[1]) if lay and len(lay) > 1 else 0
        prio = _first(z, "priority")
        priority = int(_floats(prio, 1)[0]) if prio else 0
        pts_node = _first(_first(z, "polygon") or [], "pts")
        pts: list[list[float]] = []
        if pts_node:
            for xy in _items(pts_node, "xy"):
                px, py = _floats(xy)
                pts.append([round(px, 3), round(py, 3)])
        if len(pts) >= 3 and _first(z, "keepout") is not None:
            # KiCad 的 zone 可带 keepout 属性：那是禁止铺铜区，不是铜皮
            zone_keepouts.append({"kind": "polygon", "points": pts, "layer": layer_no,
                                  "reason": "KiCad zone keepout"})
        elif len(pts) >= 3 and net_name:
            zones.append({"net": net_name, "layer": layer_no, "priority": priority,
                          "filled": 1 if _first(z, "filled_polygon") else 0,
                          "points": pts})
        elif pts and not net_name:
            warnings.append({"zone": "无网络", "points": len(pts),
                            "reason": "没有 net，无法判定短路"})

    vias = []
    for v in _items(root, "via"):
        at = _first(v, "at")
        if not at:
            continue
        x, y = _floats(at)
        size = _first(v, "size")
        drill = _first(v, "drill")
        net = _first(v, "net")
        name = nets.get(int(net[1])) if net and len(net) > 1 else None
        lay = _first(v, "layers")
        from_layer, to_layer = 0, 15
        if lay and len(lay) >= 3:
            nums = sorted((_layer_no(t) for t in lay[1:]))
            from_layer, to_layer = nums[0], nums[-1]
        vias.append({"net": name, "x": round(x, 3), "y": round(y, 3),
                     "diameter": _floats(size, 1)[0] if size else 0.6,
                     "drill": _floats(drill, 1)[0] if drill else 0.3,
                     "from_layer": from_layer, "to_layer": to_layer})

    # 板框：Edge.Cuts 上的直线/弧/矩形/圆。异形板框不能靠焊盘外接矩形推。
    outline: list[list[float]] = []
    outline_kinds: set[str] = set()
    for gk in ("gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly", "gr_curve"):
        for g in _items(root, gk):
            lay = _first(g, "layer")
            if not (lay and len(lay) > 1 and lay[1] == "Edge.Cuts"):
                continue
            outline_kinds.add(gk)
            if gk == "gr_line":
                s2, e2 = _first(g, "start"), _first(g, "end")
                if s2 and e2:
                    outline.append([round(v, 3) for v in _floats(s2)])
                    outline.append([round(v, 3) for v in _floats(e2)])
            elif gk == "gr_rect":
                s2, e2 = _first(g, "start"), _first(g, "end")
                if s2 and e2:
                    x0, y0 = _floats(s2)
                    x1, y1 = _floats(e2)
                    outline += [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
            elif gk == "gr_circle":
                c2, e2 = _first(g, "center"), _first(g, "end")
                if c2 and e2:
                    cx, cy = _floats(c2)
                    ex, ey = _floats(e2)
                    r = math.hypot(ex - cx, ey - cy)
                    outline += [[cx - r, cy - r], [cx + r, cy - r],
                                [cx + r, cy + r], [cx - r, cy + r]]
            elif gk == "gr_arc":
                s2, m2, e2 = _first(g, "start"), _first(g, "mid"), _first(g, "end")
                if s2 and m2 and e2:
                    for px2, py2 in _arc_points(_floats(s2), _floats(m2), _floats(e2), steps=8):
                        outline.append([round(px2, 3), round(py2, 3)])
            elif gk == "gr_poly":
                pts_node = _first(g, "pts")
                for xy in (_items(pts_node, "xy") if pts_node else []):
                    outline.append([round(v, 3) for v in _floats(xy)])
            elif gk == "gr_curve":
                # 三次贝塞尔（KiCad 的 gr_curve 通常 4 个控制点）：离散成折线
                pts_node = _first(g, "pts")
                ctrl = [[round(v, 3) for v in _floats(xy)]
                        for xy in (_items(pts_node, "xy") if pts_node else [])]
                if len(ctrl) >= 4:
                    p0, p1, p2, p3 = ctrl[:4]
                    for i in range(9):
                        t = i / 8.0
                        mt = 1.0 - t
                        x = (mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0]
                             + 3 * mt * t * t * p2[0] + t ** 3 * p3[0])
                        y = (mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1]
                             + 3 * mt * t * t * p2[1] + t ** 3 * p3[1])
                        outline.append([round(x, 3), round(y, 3)])
                elif ctrl:
                    outline.extend(ctrl)

    all_x = [p["x"] for p in pads] or [0.0]
    all_y = [p["y"] for p in pads] or [0.0]
    w = board_w or (max(all_x) + 5)
    h = board_h or (max(all_y) + 5)
    if outline and not board_w:
        ox = [p[0] for p in outline]
        oy = [p[1] for p in outline]
        ow, oh = max(ox) - min(ox), max(oy) - min(oy)
        if ow > 1e-6 and oh > 1e-6:                      # 轮廓比焊盘外接更可信
            w, h = ow, oh
    if outline and len(outline) < 3:
        warnings.append({"outline": "too_few_points", "points": len(outline)})
    net_names = sorted({p["net"] for p in pads} | {r["net"] for r in routes})
    return {
        "board": {"name": "kicad-import", "w": round(w, 3), "h": round(h, 3),
                  "min_width": 0.5, "min_spacing": 0.5},
        "components": components,
        "pads": pads,
        "nets": [{"name": n, "class": "default"} for n in net_names],
        "routes": routes,
        "vias": [v for v in vias if v["net"]],
        "zones": zones,
        "outline": outline,
        "outline_kinds": sorted(outline_kinds),
        "keepouts": zone_keepouts,
        "rotation_warnings": warnings,
        "pad_warnings": pad_warnings,
    }


def parse_file(path: str | Path, **kw) -> dict:
    return parse(Path(path).read_text(encoding="utf-8", errors="ignore"), **kw)


def write_fixture_kicad(path: str | Path, spec: dict) -> Path:
    """把板规格反向写成 .kicad_pcb 子集，用于自测与演示。"""
    lines = ["(kicad_pcb (version 20221018) (generator aepcb)"]
    for i, n in enumerate(spec["nets"], start=1):
        lines.append(f'  (net {i} "{n["name"]}")')
    net_idx = {n["name"]: i for i, n in enumerate(spec["nets"], start=1)}
    for c in spec["components"]:
        lines.append(f'  (footprint "{c.get("footprint", "FP")}" (layer "F.Cu") '
                     f'(at {c["x"]} {c["y"]}) (property "Reference" "{c["ref"]}")')
        for p in spec["pads"]:
            if p["comp"] != c["ref"]:
                continue
            lines.append(f'    (pad "{p["no"]}" smd rect (at {p["x"] - c["x"]} {p["y"] - c["y"]}) '
                         f'(size {p.get("w", 1.5)} {p.get("h", 1.0)}) '
                         f'(net {net_idx.get(p["net"], 0)} "{p["net"]}"))')
        lines.append('  )')
    for r in spec["routes"]:
        pts = r["points"]
        layer_name = _layer_name(int(r.get("layer", 0)))
        for i in range(len(pts) - 1):
            lines.append(f'  (segment (start {pts[i][0]} {pts[i][1]}) '
                         f'(end {pts[i + 1][0]} {pts[i + 1][1]}) (width {r["width"]}) '
                         f'(layer "{layer_name}") (net {net_idx.get(r["net"], 0)}))')
    for v in spec.get("vias", []):
        layers = f'"{_layer_name(v.get("from_layer", 0))}" "{_layer_name(v.get("to_layer", 15))}"'
        lines.append(f'  (via (at {v["x"]} {v["y"]}) (size {v.get("diameter", 0.6)}) '
                     f'(drill {v.get("drill", 0.3)}) (layers {layers}) '
                     f'(net {net_idx.get(v["net"], 0)}))')
    lines.append(")")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p
