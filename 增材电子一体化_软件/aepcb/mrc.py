"""可打性规则 R1–R7 与评分（MRC-001—MRC-011）。

打分口径写死在代码里并写进说明：
    score = max(0, 100 - Σ 严重度权重)，blocker 12 / major 8 / minor 3 / info 1
同一输入重复检查必须得到同一分数（MRC-008），因此 input_digest 覆盖几何、参数与阈值。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math

from .board import load_route_points
from .geometry import point_seg_dist, polyline_len
from .routing import crosses, min_gap
from .store import DB, now

DEFAULTS = {
    "R1": {"min_width_mm": 0.5},
    "R2": {"min_spacing_mm": 0.5},
    "R3": {"max_slope_deg": 45.0},
    "R4": {"min_clearance_mm": 0.5},
    "R5": {"max_occlusion": 0.0},
    "R6": {"max_delta_t_c": 5.0},
    "R7": {"min_channel_mm": 0.5},
}
SEVERITY = {"R1": "major", "R2": "major", "R3": "major", "R4": "major",
            "R5": "major", "R6": "minor", "R7": "minor"}
WEIGHT = {"blocker": 12, "major": 8, "minor": 3, "info": 1}
COMPONENT_BODY_MM = 4.0     # 器件本体近似尺寸（QFN-16），用于 R4/R5


def ensure_baseline(db: DB) -> dict:
    rv = db.one("SELECT id FROM mrc_rule_version WHERE is_active=1")
    if rv:
        rule_id = rv["id"]
    else:
        rule_id = db.ins("mrc_rule_version", label="v1 首版基线", note="按 SRS 3.4 默认阈值",
                         created_by="system", is_active=1, created_at=now())
        for code, params in DEFAULTS.items():
            for key, val in params.items():
                unit = "deg" if "deg" in key else ("c" if "_t_" in key else
                                                   ("count" if "occlusion" in key else "mm"))
                db.ins("mrc_rule_threshold", rule_version_id=rule_id, rule_code=code,
                       param_key=key, value_num=float(val), unit=unit, enabled=1)
    tg = db.one("SELECT id FROM tool_geometry WHERE is_active=1")
    if tg:
        tool_id = tg["id"]
    else:
        tool_id = db.ins("tool_geometry", name="0.25 针头", nozzle_inner_d_mm=0.25,
                         nozzle_outer_d_mm=0.41, safety_gap_mm=0.5, is_active=1, created_at=now())

    mat_a = db.one("SELECT id FROM material WHERE code='A'")
    if not mat_a:
        mat_a_id = db.ins("material", code="A", name="可焊烧结银浆", kind="silver_paste",
                          is_active=1, created_at=now())
        db.ins("material", code="B", name="热固化介电环氧", kind="dielectric",
               is_active=1, created_at=now())
        db.ins("material_batch", material_id=mat_a_id, batch_no="AG-2026-09",
               arrived_at=now(), is_active=1, created_at=now())
    b = db.one("SELECT id FROM material_batch WHERE is_active=1")
    prof = db.one("SELECT id FROM process_profile WHERE is_valid=1 AND project_id IS NULL")
    if not prof:
        params = {"layer_h_mm": 0.08, "width_comp_mm": 0.06, "z_comp_mm": 0.02,
                  "speed_mm_s": 12.0, "flow": 1.0, "pre_gel_c": 80, "pre_gel_s": 90,
                  "silver_set_c": 100, "silver_set_s": 90, "cure_c": 170, "cure_s": 420,
                  "ramp_c_per_min": 3.0, "cool_c": 60}
        prof_id = db.ins("process_profile", project_id=None, name="三层默认",
                         params_json=json.dumps(params), params_hash=sha256_text(json.dumps(params)),
                         is_default=1, is_valid=1, created_at=now())
    else:
        prof_id = prof["id"]
    return {"rule_version_id": rule_id, "tool_geometry_id": tool_id,
            "process_profile_id": prof_id, "material_batch_id": b["id"] if b else None}


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _thresholds(db: DB, rule_id: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for row in db.q("SELECT * FROM mrc_rule_threshold WHERE rule_version_id=? AND enabled=1", (rule_id,)):
        out.setdefault(row["rule_code"], {})[row["param_key"]] = float(row["value_num"])
    for code, params in DEFAULTS.items():
        for k, v in params.items():
            out.setdefault(code, {}).setdefault(k, float(v))
    return out


def _faces(db: DB, project_id: str) -> list[list[float]]:
    row = db.one("SELECT mesh_blob_hash FROM structure WHERE project_id=? "
                 "ORDER BY created_at DESC, rowid DESC LIMIT 1", (project_id,))
    if not row:
        return []
    try:
        return json.loads(db.get_blob(row["mesh_blob_hash"])).get("faces", [])
    except Exception:
        return []


def create_rule_version(db: DB, label: str, changes: dict[str, float],
                         note: str = "") -> dict:
    """MRC-011：新建规则版本（复制当前阈值并覆盖），旧版本保留不可变。"""
    base = ensure_baseline(db)["rule_version_id"]
    rows = db.q("SELECT * FROM mrc_rule_threshold WHERE rule_version_id=?", (base,))
    params: dict[tuple[str, str], tuple[float, str, int]] = {
        (r["rule_code"], r["param_key"]): (float(r["value_num"]), r["unit"], int(r["enabled"]))
        for r in rows}
    for key, val in changes.items():
        if "." not in key:
            raise ValueError(f"阈值键应为 R<n>.<param>，得到 {key}")
        code, param = key.split(".", 1)
        if code not in SEVERITY:
            raise ValueError(f"未知规则码 {code}")
        unit = params.get((code, param), (0.0, "mm", 1))[1]
        params[(code, param)] = (float(val), unit, 1)
    snapshot = sha256_text(json.dumps(sorted((f"{c}.{p}", v[0]) for (c, p), v in params.items())))
    db.c.execute("UPDATE mrc_rule_version SET is_active=0 WHERE is_active=1")
    rid = db.ins("mrc_rule_version", label=label, note=note or f"覆盖 {len(changes)} 个阈值",
                 created_by="local", supersedes_id=base, snapshot_hash=snapshot,
                 is_active=1, created_at=now())
    for (code, param), (val, unit, enabled) in params.items():
        db.ins("mrc_rule_threshold", rule_version_id=rid, rule_code=code,
               param_key=param, value_num=val, unit=unit, enabled=enabled)
    db.commit()
    return {"rule_version_id": rid, "label": label, "supersedes": base,
            "changes": changes, "thresholds": len(params)}


def list_rule_versions(db: DB) -> list[dict]:
    return [dict(r) for r in db.q(
        "SELECT id,label,is_active,supersedes_id,created_at FROM mrc_rule_version "
        "ORDER BY created_at DESC, rowid DESC")]


def run(db: DB, project_id: str, version_id: str, rule_version_id: str | None = None,
        tool_geometry_id: str | None = None, engine_version: str = "1.0.0") -> dict:
    base = ensure_baseline(db)
    rv = rule_version_id or base["rule_version_id"]
    tg = tool_geometry_id or base["tool_geometry_id"]
    th = _thresholds(db, rv)

    circuit = db.one("SELECT * FROM circuit WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (project_id,))
    routes = db.q("SELECT * FROM route WHERE circuit_id=? AND deleted_at IS NULL", (circuit["id"],))
    comps = db.q("SELECT * FROM component WHERE circuit_id=?", (circuit["id"],))
    pts = {r["id"]: load_route_points(db, r) for r in routes}
    net_name = {row["id"]: row["name"] for row in db.q("SELECT id,name FROM net WHERE circuit_id=?", (circuit["id"],))}
    issues: list[dict] = []

    def issue(code, target_kind, target_id, measured, threshold, unit, msg, suggestion,
              layer=None, x=None, y=None):
        issues.append({"rule_code": code, "severity": SEVERITY[code], "target_kind": target_kind,
                       "target_id": target_id, "measured_num": measured, "threshold_num": threshold,
                       "unit": unit, "message": msg,
                       "suggestion_json": json.dumps(suggestion, ensure_ascii=False),
                       "layer_no": layer, "x_mm": x, "y_mm": y})

    # R1 线宽
    for r in routes:
        floor = th["R1"]["min_width_mm"]
        if r["width_mm"] < floor - 1e-9:
            p = pts[r["id"]]
            issue("R1", "route", r["id"], r["width_mm"], floor, "mm",
                  f"{net_name.get(r['net_id'], '?')} 线宽 {r['width_mm']:.2f} mm 低于下限 {floor:.2f} mm",
                  {"action": "set_width", "to": max(floor, 0.6), "route_id": r["id"]},
                  layer=r["layer_no"], x=p[0][0], y=p[0][1])

    # R2 间距 / R7 介电流道（同层异网两两比较）
    spacing = th["R2"]["min_spacing_mm"]
    channel = th["R7"]["min_channel_mm"]
    for a, b in itertools.combinations(routes, 2):
        if a["layer_no"] != b["layer_no"] or a["net_id"] == b["net_id"]:
            continue
        d = min_gap(pts[a["id"]], pts[b["id"]])
        edge = d - (a["width_mm"] + b["width_mm"]) / 2
        if edge < 1e-9:
            continue
        names = f"{net_name.get(a['net_id'], '?')}/{net_name.get(b['net_id'], '?')}"
        if edge < spacing - 1e-9 and not crosses(pts[a["id"]], pts[b["id"]]):
            issue("R2", "route", a["id"], round(edge, 3), spacing, "mm",
                  f"{names} 间距 {edge:.2f} mm 小于 {spacing:.2f} mm",
                  {"action": "shift_route", "route_id": b["id"],
                   "delta_mm": round(spacing - edge + 0.2, 3), "axis": "y"},
                  layer=a["layer_no"])
        if edge < channel - 1e-9:
            issue("R7", "route", a["id"], round(edge, 3), channel, "mm",
                  f"{names} 之间介电流道净宽 {edge:.2f} mm 小于 {channel:.2f} mm，填充会断流",
                  {"action": "shift_route", "route_id": b["id"],
                   "delta_mm": round(channel - edge + 0.2, 3), "axis": "y"},
                  layer=a["layer_no"])

    # R3 悬垂
    max_slope = th["R3"]["max_slope_deg"]
    for i, f in enumerate(_faces(db, project_id)):
        slope = float(f[3])
        if slope > max_slope + 1e-9:
            issue("R3", "structure", "", round(slope, 2), max_slope, "deg",
                  f"第 {i + 1} 个面与水平面夹角 {slope:.1f}° 超过 {max_slope:.0f}°，会悬垂",
                  {"action": "add_support_or_reorient", "face_index": i})

    # R4 可达性 / R5 遮挡
    clearance = th["R4"]["min_clearance_mm"]
    comp_pads: dict[str, list[tuple[float, float]]] = {}
    for p in db.q("SELECT * FROM pad WHERE component_id IN "
                  "(SELECT id FROM component WHERE circuit_id=?)", (circuit["id"],)):
        comp_pads.setdefault(p["component_id"], []).append((p["x_mm"], p["y_mm"]))

    for c in comps:
        hw = float(c["body_w_mm"]) / 2
        hh = float(c["body_h_mm"]) / 2
        x0, x1 = c["x_mm"] - hw, c["x_mm"] + hw
        y0, y1 = c["y_mm"] - hh, c["y_mm"] + hh

        def crosses_body(pts, x0=x0, x1=x1, y0=y0, y1=y1):
            """沿线段采样：只查拐点会把“横穿本体”的线漏掉。"""
            import math as _m
            for i in range(len(pts) - 1):
                (ax, ay), (bx, by) = pts[i], pts[i + 1]
                n = max(1, int(_m.hypot(bx - ax, by - ay) / 0.5))
                for k in range(1, n):
                    x = ax + (bx - ax) * k / n
                    y = ay + (by - ay) * k / n
                    if x0 < x < x1 and y0 < y < y1:
                        return True
            return False
        own = comp_pads.get(c["id"], [])
        for r in routes:
            p = pts[r["id"]]
            # 接到本器件焊盘的走线必然贴边，不算碰撞（否则每条线都误报）
            touches = any(abs(p[0][0] - px) < 0.05 and abs(p[0][1] - py) < 0.05 or
                          abs(p[-1][0] - px) < 0.05 and abs(p[-1][1] - py) < 0.05
                          for px, py in own)
            if touches:
                continue
            # 只算内部拐点：端点就是焊盘，落在本体边界上不算遮挡
            inside = any(x0 < x < x1 and y0 < y < y1 for x, y in p[1:-1]) \
                or crosses_body(p)
            if inside:
                issue("R5", "route", r["id"], 1.0, 0.0, "count",
                      f"{c['ref_des']} 下方有走线，先打的层会挡住后打路径",
                      {"action": "shift_route", "route_id": r["id"],
                       "delta_mm": 2.0, "axis": "y", "component": c["ref_des"]},
                      layer=r["layer_no"])
                continue
            near = min(min(point_seg_dist((c["x_mm"], c["y_mm"]), p[i], p[i + 1])
                           for i in range(len(p) - 1)), 99) if len(p) > 1 else 99
            if c["height_mm"] >= 1.0 and near < max(hw, hh) + clearance:
                issue("R4", "route", r["id"], round(near - hw, 3), clearance, "mm",
                      f"走线距 {c['ref_des']} 本体 {(near - hw):.2f} mm，小于机头安全间隙 {clearance:.2f} mm",
                      {"action": "shift_route", "route_id": r["id"], "delta_mm": 0.8, "axis": "y"},
                      layer=r["layer_no"])

    # R6 温度场（估算）：只报最不利的一点，避免同一次检查重复计分
    max_dt = th["R6"]["max_delta_t_c"]
    worst = None
    for r in routes:
        for x, y in pts[r["id"]]:
            dt = 0.35 * abs(x - 30.0) + 0.10 * abs(y - 20.0)
            if worst is None or dt > worst[0]:
                worst = (dt, r, x, y)
    if worst and worst[0] > max_dt:
        dt, r, x, y = worst
        issue("R6", "route", r["id"], round(dt, 2), max_dt, "c",
              f"该位置估算温差 {dt:.1f} ℃ 超过 {max_dt:.0f} ℃，固化可能不足（估算值，仅报最不利点）",
              {"action": "shift_route", "route_id": r["id"], "delta_mm": 1.5, "axis": "x"},
              layer=r["layer_no"], x=x, y=y)

    penalty = sum(WEIGHT[i["severity"]] for i in issues)
    score = max(0.0, 100.0 - penalty)
    digest = sha256_text(json.dumps({
        "routes": sorted([(r["id"], round(r["width_mm"], 4), r["layer_no"],
                           [[round(x, 3), round(y, 3)] for x, y in pts[r["id"]]]) for r in routes]),
        "thresholds": th, "faces": _faces(db, project_id),
    }, sort_keys=True, default=str))

    # MRC-008：同一指纹重复检查必须得到同一结果。指纹相同则幂等复用，不再追加行。
    existing = db.one(
        "SELECT * FROM mrc_run WHERE version_id=? AND rule_version_id=? AND tool_geometry_id=? "
        "AND engine_version=? AND input_digest=?",
        (version_id, rv, tg, engine_version, digest))
    if existing:
        rows = db.q("SELECT * FROM mrc_issue WHERE run_id=? ORDER BY rule_code", (existing["id"],))
        return {"run_id": existing["id"], "score": existing["total_score"],
                "issues": [dict(r) for r in rows], "digest": digest, "reused": True}

    run_id = db.ins("mrc_run", project_id=project_id, version_id=version_id,
                    rule_version_id=rv, tool_geometry_id=tg, engine_version=engine_version,
                    input_digest=digest, total_score=round(score, 2),
                    issue_count=len(issues), duration_ms=1, created_at=now())
    for i in issues:
        db.ins("mrc_issue", run_id=run_id, version_id=version_id, rule_version_id=rv,
               rule_code=i["rule_code"], severity=i["severity"], target_kind=i["target_kind"],
               target_id=i["target_id"], measured_num=i["measured_num"],
               threshold_num=i["threshold_num"], unit=i["unit"], message=i["message"],
               suggestion_json=i["suggestion_json"], layer_no=i["layer_no"],
               x_mm=i["x_mm"], y_mm=i["y_mm"], created_at=now())
    db.commit()
    return {"run_id": run_id, "score": round(score, 2), "issues": issues, "digest": digest}
