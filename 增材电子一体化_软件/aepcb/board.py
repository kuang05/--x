"""板文件导入：把板规格（器件/焊盘/网络/走线/禁布区）写入 58 表 schema。

格式刻意保持简单（JSON），因为真实 EDA 解析器不是本期目标；
字段命名与数据模型一致，后续接 KiCad 解析器时只换这一层。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .store import DB, now


@dataclass
class ImportResult:
    circuit_id: str
    structure_id: str
    outline_id: str
    component_ids: dict[str, str]
    pad_ids: dict[str, str]
    net_ids: dict[str, str]
    route_ids: dict[str, str]
    file_hash: str
    counts: dict[str, int]


def import_step(db: DB, project_id: str, text: str, step_module) -> tuple[str, object]:
    # 优先走增强路径（子集 + 可选 OCC）；老模块只有 parse 时也能跑
    parser = getattr(step_module, "parse_enhanced", None) or step_module.parse
    model = parser(text)
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    h = db.put_blob(data, "step")
    ext = db.ins("external_file", project_id=project_id, role="step", path="inputs/board.step",
                 size_bytes=len(data), hash_at_import=h, hash_last_seen=h,
                 format=model.schema, verify_status="ok", imported_at=now())
    x0, y0, z0, x1, y1, z1 = model.bbox
    mesh = json.dumps({"points": model.points[:2000],
                       "faces": [[f.normal[0], f.normal[1], f.normal[2], f.slope_deg]
                                 for f in model.faces]},
                      separators=(",", ":")).encode()
    mesh_hash = db.put_blob(mesh, "mesh")
    sid = db.ins("structure", project_id=project_id, source_file_id=ext,
                 step_schema=model.schema, entity_kind=model.entity_kind,
                 flattened=1 if model.entity_kind == "assembly" else 0,
                 unit_in=model.units if model.units in ("mm", "inch") else "unknown",
                 unit_scale=1.0, mesh_blob_hash=mesh_hash, mesh_tolerance_mm=0.02,
                 triangle_count=len(model.points) * 2, bbox_min_x_mm=x0, bbox_min_y_mm=y0,
                 bbox_min_z_mm=z0, bbox_max_x_mm=x1, bbox_max_y_mm=y1, bbox_max_z_mm=z1,
                 import_status="ok", import_ms=1, imported_at=now(), created_at=now())
    for code, sev, msg in model.diagnostics:
        db.add_diagnostic(project_id, "structure", code, msg, severity=sev,
                          target_kind="structure", target_id=sid)
    return sid, model


def import_board(db: DB, project_id: str, structure_id: str, spec: dict) -> ImportResult:
    from .geometry import bbox, polyline_len

    board = spec["board"]
    w, h = float(board["w"]), float(board["h"])
    comps = spec.get("components", [])
    pads = spec.get("pads", [])
    nets = spec.get("nets", [])
    routes = spec.get("routes", [])

    cid = db.ins("circuit", project_id=project_id, source_format="netlist+coord",
                 unit_in="mm", board_w_mm=w, board_h_mm=h,
                 component_count=len(comps), pad_count=len(pads), net_count=len(nets),
                 import_hash=db.put_blob(json.dumps(spec, sort_keys=True).encode(), "other"),
                 imported_at=now(), created_at=now())
    nc = db.ins("net_class", circuit_id=cid, name="default",
                min_width_mm=float(board.get("min_width", 0.6)),
                min_spacing_mm=float(board.get("min_spacing", 0.6)), created_at=now())

    comp_ids, pad_ids, net_ids = {}, {}, {}
    for n in nets:
        net_ids[n["name"]] = db.ins("net", circuit_id=cid, name=n["name"],
                                    net_class_id=nc, is_power=1 if n.get("power") else 0,
                                    created_at=now())
    for c in comps:
        comp_ids[c["ref"]] = db.ins("component", circuit_id=cid, ref_des=c["ref"],
                                    footprint=c.get("footprint", "QFN"),
                                    x_mm=float(c["x"]), y_mm=float(c["y"]),
                                    rot_deg=int(c.get("rot", 0)), side=c.get("side", "top"),
                                    height_mm=float(c.get("h", 1.2)),
                                    body_w_mm=float(c.get("body_w", 4.0)),
                                    body_h_mm=float(c.get("body_h", 4.0)),
                                    locked=1 if c.get("locked") else 0,
                                    net_class_id=nc, created_at=now())
    for p in pads:
        pad_ids[f"{p['comp']}.{p['no']}"] = db.ins(
            "pad", component_id=comp_ids[p["comp"]], net_id=net_ids.get(p["net"]),
            pad_no=str(p["no"]), x_mm=float(p["x"]), y_mm=float(p["y"]),
            w_mm=float(p.get("w", 1.5)), h_mm=float(p.get("h", 1.0)),
            shape=p.get("shape", "rect"), side=p.get("side", "top"), plated=0)

    route_ids = {}
    for r in routes:
        pts = [(float(x), float(y)) for x, y in r["points"]]
        geom = json.dumps([[round(x, 4), round(y, 4)] for x, y in pts],
                          separators=(",", ":")).encode()
        gh = db.put_blob(geom, "route")
        x0, y0, x1, y1 = bbox(pts)
        route_ids[r["net"]] = db.ins(
            "route", circuit_id=cid, net_id=net_ids[r["net"]],
            layer_no=int(r.get("layer", 0)), side=r.get("side", "top"),
            width_mm=float(r["width"]), geom_blob_hash=gh,
            length_mm=round(polyline_len(pts), 3), point_count=len(pts),
            start_x_mm=pts[0][0], start_y_mm=pts[0][1],
            end_x_mm=pts[-1][0], end_y_mm=pts[-1][1], created_at=now())

    for k in spec.get("keepouts", []):
        geom = json.dumps(k["points"]).encode()
        db.ins("keepout", circuit_id=cid, kind=k.get("kind", "rect"),
               geometry_blob_hash=db.put_blob(geom, "other"),
               applies_layer=str(k.get("layer", "all")), reason=k.get("reason", "设计禁布"),
               created_at=now())

    # 铺铜：KiCad zone → copper_zone（异网重叠即短路，见 R2）
    for z in spec.get("zones", []):
        net_name = z.get("net")
        if not net_name or net_name not in net_ids:
            continue
        db.ins("copper_zone", circuit_id=cid, net_id=net_ids[net_name],
               layer_no=int(z.get("layer", 0)),
               geometry_blob_hash=db.put_blob(json.dumps(z["points"]).encode(), "other"),
               priority=int(z.get("priority", 0)), filled=int(z.get("filled", 1)),
               created_at=now())

    for v in spec.get("vias", []):
        if not v.get("net"):
            continue
        db.ins("via", circuit_id=cid, net_id=net_ids[v["net"]],
               x_mm=float(v["x"]), y_mm=float(v["y"]),
               diameter_mm=float(v.get("diameter", 0.6)),
               drill_mm=float(v.get("drill", 0.3)),
               from_layer=int(v.get("from_layer", 0)),
               to_layer=int(v.get("to_layer", 1)), created_at=now())

    outline = spec.get("outline") or []
    poly = outline if len(outline) >= 3 else [[0, 0], [w, 0], [w, h], [0, h]]
    if len(poly) >= 3:
        area = abs(sum(poly[i][0] * poly[(i + 1) % len(poly)][1]
                       - poly[(i + 1) % len(poly)][0] * poly[i][1]
                       for i in range(len(poly)))) / 2
    else:
        area = w * h
    oid = db.ins("board_outline", structure_id=structure_id, kind="auto",
                 geometry_blob_hash=db.put_blob(json.dumps(poly).encode(), "other"),
                 area_mm2=round(area or w * h, 3), is_valid=1, created_at=now())

    return ImportResult(cid, structure_id, oid, comp_ids, pad_ids, net_ids, route_ids,
                        spec.get("hash", ""), {"components": len(comps), "pads": len(pads),
                                               "nets": len(nets), "routes": len(routes)})


def load_route_points(db: DB, route_row) -> list[tuple[float, float]]:
    raw = db.get_blob(route_row["geom_blob_hash"])
    return [(float(a), float(b)) for a, b in json.loads(raw)]
