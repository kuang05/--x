"""G-code 导出：内容哈希（规范化）+ 文件哈希（落盘字节）双记录。

关键设计（SLC-008 / T16 / AGT-008）：
    content_hash 只看规范化后的正文 —— 剔除时间戳、会话 ID、绝对路径，浮点统一 %.3f；
    file_hash    看真实落盘字节 —— 含一行导出时间，因此两次导出必然不同。
验收一律比对 content_hash，否则"两次导出哈希一致"永远不可能成立。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import slicecheck
from .store import DB, now

FEED_TRAVEL = 3000
FEED_PRINT = 1200


def _fmt(v: float) -> str:
    return f"{v:.3f}"


def build(db: DB, slice_id: str) -> dict:
    sl = db.one("SELECT * FROM slice_set WHERE id=?", (slice_id,))
    if not sl:
        raise ValueError("切片不存在")
    # SLC-007：切片后连通性未通过时阻断导出
    conn = slicecheck.latest(db, slice_id)
    if conn is not None and conn["result"] == "fail":
        raise ValueError(f"切片后连通性检查未通过（断路 {conn['open_count']} / 短路 {conn['short_count']}），已阻断导出")
    project = db.one("SELECT * FROM project WHERE id=?", (sl["project_id"],))
    version = db.one("SELECT * FROM version WHERE id=?", (sl["design_version_id"],))
    prof = db.one("SELECT * FROM process_profile WHERE id=?", (sl["process_profile_id"],))
    params = json.loads(prof["params_json"])
    speed_mm_s = float(params.get("speed_mm_s", 12.0))
    feed_print = max(60, int(round(speed_mm_s * 60)))
    feed_travel = max(feed_print, int(round(speed_mm_s * 60 * 2.5)))
    passes = db.q("SELECT * FROM deposit_pass WHERE slice_set_id=? ORDER BY pass_index", (slice_id,))
    layers = {r["id"]: r for r in db.q("SELECT * FROM physical_layer WHERE slice_set_id=?", (slice_id,))}
    mat = {r["id"]: r for r in db.q("SELECT * FROM material")}
    batch = {r["id"]: r for r in db.q("SELECT * FROM material_batch")}

    lines = [
        ";AEPCB-1.0",
        f";PROJECT {project['name']}",
        f";VERSION {version['version_no']}",
        f";SLICE-DIGEST {sl['input_digest']}",
        f";LAYERS {sl['layer_count']} PASSES {sl['pass_count']} THICK {_fmt(sl['total_thickness_mm'])}",
        "G21 ;mm", "G90 ;absolute",
    ]
    material_manifest = []
    e_total = 0.0
    for p in passes:
        layer = layers[p["physical_layer_id"]]
        material = mat.get(p["material_id"], {"code": "?", "kind": "?"})
        b = batch.get(p["material_batch_id"]) if p["material_batch_id"] else None
        lines.append(f";LAYER {layer['layer_index']} {layer['kind']}")
        lines.append(f";PASS {p['pass_index']} {p['pattern']} {material['code']}")
        lines.append(f";MATERIAL {material['code']} {material['kind']}"
                     + (f" BATCH {b['batch_no']}" if b else ""))
        if b:
            material_manifest.append([p["pass_index"], material["code"], b["batch_no"]])
        lines.append(f"G1 Z{_fmt(p['z_top_mm'])} F{feed_travel}")
        paths = db.q("SELECT * FROM slice_path WHERE deposit_pass_id=? ORDER BY seq", (p["id"],))
        for path in paths:
            geom = json.loads(db.get_blob(path["geom_blob_hash"]))
            if path["path_kind"] == "travel":
                (x0, y0), (x1, y1) = geom[0][:2], geom[1][:2]
                lines.append(f"G0 X{_fmt(x1)} Y{_fmt(y1)} F{feed_travel}")
                continue
            x0, y0 = geom[0][0], geom[0][1]
            e_total += path["length_mm"] * 0.02
            seg_feed = int(round((path["speed_mm_s"] or speed_mm_s) * 60))
            lines.append(f"G0 X{_fmt(x0)} Y{_fmt(y0)} F{feed_travel}")
            for pt in geom[1:]:
                lines.append(f"G1 X{_fmt(pt[0])} Y{_fmt(pt[1])} E{_fmt(e_total)} F{seg_feed}")
        # 热历程并入该层结束后
        steps = db.q("SELECT * FROM thermal_step WHERE slice_set_id=? AND stage IN ('cure','final_cure') "
                     "ORDER BY step_index", (slice_id,))
    # 热历程块（放在所有道次之后，符合"整板终固化"）
    for st in db.q("SELECT * FROM thermal_step WHERE slice_set_id=? ORDER BY step_index", (slice_id,)):
        lines.append(f";OVEN {st['stage']} {_fmt(st['temp_c'])}C "
                     f"RAMP {_fmt(st['ramp_c_per_min'] or 0)}C/MIN HOLD {st['hold_s']}S")
    lines += ["M104 S0", "M140 S0", "G28 X Y", ";END"]
    content = "\n".join(lines) + "\n"

    manifest = json.dumps(sorted(material_manifest)).encode()
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    file_text = content + f";EXPORTED_AT {now()}\n"
    file_hash = hashlib.sha256(file_text.encode("utf-8")).hexdigest()

    out_dir = db.root / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{content_hash[:16]}.gcode"
    path.write_text(file_text, encoding="utf-8")

    # 内容相同的重复导出是幂等的：不新增记录，但磁盘文件会因时间戳而字节不同。
    # 这正是“双哈希”存在的理由：验收只认 content_hash。
    existing = db.one("SELECT * FROM gcode_export WHERE slice_set_id=? AND content_hash=?",
                      (slice_id, content_hash))
    if existing:
        return {"export_id": existing["id"], "content_hash": content_hash,
                "file_hash": file_hash, "path": str(path), "bytes": len(file_text.encode()),
                "material_manifest_hash": existing["material_manifest_hash"],
                "duplicate": True}

    blob_hash = db.put_blob(file_text.encode("utf-8"), "gcode")

    export_id = db.ins("gcode_export", slice_set_id=slice_id, content_hash=content_hash,
                       file_hash=file_hash, blob_hash=blob_hash, byte_size=len(file_text.encode()),
                       header_json=json.dumps({"version_no": version["version_no"],
                                                "layers": sl["layer_count"],
                                                "passes": sl["pass_count"]}),
                       material_manifest_hash=hashlib.sha256(manifest).hexdigest(),
                       engine_version="1.0.0", created_at=now())
    db.commit()
    return {"export_id": export_id, "content_hash": content_hash, "file_hash": file_hash,
            "path": str(path), "bytes": len(file_text.encode()),
            "material_manifest_hash": hashlib.sha256(manifest).hexdigest(),
            "duplicate": False}
