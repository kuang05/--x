"""虚拟打印：前置检查、G-code 回放、暂停/续打、故障注入、缺陷与归因。

不连接真实硬件（首版边界），但事件、状态机、缺陷记录走的是同一套表，
换成串口驱动时只需要替换 `_send` 这一层。
"""
from __future__ import annotations

import json
import random

from .store import DB, now

FAULTS = ["broken_line", "clog", "overheat", "oven_underheat", "alignment_drift", "material_out"]


def ensure_device(db: DB, project_id: str) -> str:
    dev = db.one("SELECT id FROM device WHERE conn_kind='virtual' AND project_id=?", (project_id,))
    if dev:
        return dev["id"]
    dev_id = db.ins("device", project_id=project_id, name="虚拟打印机", conn_kind="virtual",
                    address="virtual://0", status="idle", last_connected_at=now(),
                    profile_json=json.dumps({"sheet": [60, 40], "axes": 3, "nozzles": 2}),
                    created_at=now())
    db.ins("device_calibration", device_id=dev_id, nozzle_a_dx_mm=0, nozzle_a_dy_mm=0,
           nozzle_a_dz_mm=0, nozzle_b_dx_mm=0.05, nozzle_b_dy_mm=-0.02, nozzle_b_dz_mm=0.01,
           hot_state_temp_c=80, tolerance_mm=0.1, is_within_tolerance=1,
           result_json=json.dumps({"residual_mm": 0.02}), measured_at=now(), operator="local")
    db.commit()
    return dev_id


def preflight(db: DB, slice_id: str, export_id: str, device_id: str) -> dict:
    checks = []
    sl = db.one("SELECT * FROM slice_set WHERE id=?", (slice_id,))
    calib = db.one("SELECT * FROM device_calibration WHERE device_id=? ORDER BY measured_at DESC LIMIT 1",
                   (device_id,))
    exp = db.one("SELECT * FROM gcode_export WHERE id=?", (export_id,))
    checks.append(("归零", True, ""))
    checks.append(("喷头对位", bool(calib and calib["is_within_tolerance"]),
                   "" if calib else "缺少标定记录"))
    checks.append(("切片就绪", sl["status"] == "ready", f"状态 {sl['status']}"))
    checks.append(("G-code 完整", bool(exp), ""))
    mat = db.one("SELECT mb.batch_no FROM deposit_pass dp JOIN material_batch mb "
                 "ON mb.id=dp.material_batch_id WHERE dp.slice_set_id=? LIMIT 1", (slice_id,))
    checks.append(("材料批次", bool(mat), "未指定银浆批次"))
    ok = all(c[1] for c in checks)
    return {"passed": ok, "checks": [{"item": c[0], "ok": c[1], "reason": c[2]} for c in checks]}


def start(db: DB, project_id: str, slice_id: str, export_id: str) -> str:
    device_id = ensure_device(db, project_id)
    pre = preflight(db, slice_id, export_id, device_id)
    sl = db.one("SELECT * FROM slice_set WHERE id=?", (slice_id,))
    exp = db.one("SELECT * FROM gcode_export WHERE id=?", (export_id,))
    if not pre["passed"]:
        job = db.ins("print_job", project_id=project_id, slice_set_id=slice_id,
                     gcode_export_id=export_id, device_id=device_id,
                     status="precheck_failed", phase="idle",
                     gcode_content_hash=exp["content_hash"], precheck_json=json.dumps(pre, ensure_ascii=False),
                     created_at=now())
        db.add_diagnostic(project_id, "print", "PRT-PRECHECK", "打印前检查未通过",
                          severity="error", target_kind="print_job", target_id=job,
                          detail=pre)
        db.commit()
        raise ValueError(f"打印前检查未通过：{pre['checks']}")
    job = db.ins("print_job", project_id=project_id, slice_set_id=slice_id,
                 gcode_export_id=export_id, device_id=device_id, status="running", phase="printing",
                 gcode_content_hash=exp["content_hash"], layer_total=sl["layer_count"],
                 layer_current=0, precheck_json=json.dumps(pre, ensure_ascii=False),
                 started_at=now(), created_at=now())
    db.c.execute("UPDATE device SET status='printing' WHERE id=?", (device_id,))
    db.commit()
    return job


def _event(db, job_id: str, seq: int, kind: str, code: str, message: str,
           detail: dict | None = None, layer: int | None = None) -> None:
    db.ins("print_event", print_job_id=job_id, seq=seq, event_kind=kind, code=code,
           message=message, detail_json=json.dumps(detail or {}, ensure_ascii=False),
           layer_no=layer, at=now())


def replay(db: DB, job_id: str, per_layer_s: float = 1.0, inject_at: dict | None = None) -> dict:
    """按 G-code 逐层回放；inject_at={'layer':2,'kind':'broken_line'} 时在该层停住。"""
    job = db.one("SELECT * FROM print_job WHERE id=?", (job_id,))
    layers = db.scalar("SELECT layer_count FROM slice_set WHERE id=?", (job["slice_set_id"],), 3)
    seq = 1
    _event(db, job_id, seq, "status", "running", "开始回放")
    fault_at = (inject_at or {}).get("layer")
    for layer in range(1, layers + 1):
        seq += 1
        _event(db, job_id, seq, "status", "layer", f"进入第 {layer} 层", {"layer": layer}, layer)
        if fault_at == layer:
            seq += 1
            kind = inject_at["kind"]
            _event(db, job_id, seq, "fault", kind, f"第 {layer} 层发生 {kind}", {"injected": True}, layer)
            db.c.execute("UPDATE print_job SET status='failed', phase='idle', layer_current=?, "
                         "ended_at=?, result_json=? WHERE id=?",
                         (layer, now(), json.dumps({"stopped_at_layer": layer}), job_id))
            db.c.execute("UPDATE device SET status='fault' WHERE id=?", (job["device_id"],))
            db.commit()
            return {"job_id": job_id, "result": "failed", "layer": layer, "kind": kind}
    seq += 1
    _event(db, job_id, seq, "status", "curing", "进入固化阶段")
    _event(db, job_id, seq + 1, "status", "cooling", "冷却至 60 ℃ 以下")
    db.c.execute("UPDATE print_job SET status='completed', phase='cooling', layer_current=?, "
                 "ended_at=?, duration_s=?, result_json=? WHERE id=?",
                 (layers, now(), int(layers * per_layer_s), json.dumps({"layers": layers}), job_id))
    db.c.execute("UPDATE device SET status='idle' WHERE id=?", (job["device_id"],))
    db.commit()
    return {"job_id": job_id, "result": "completed", "layers": layers}


def pause(db: DB, job_id: str, kind: str = "curing_pause", remaining_hold_s: int = 180) -> str:
    job = db.one("SELECT * FROM print_job WHERE id=?", (job_id,))
    db.ins("print_interruption", print_job_id=job_id, kind=kind, at=now(),
           layer_no=job["layer_current"], temp_at_c=170 if kind == "curing_pause" else None,
           remaining_hold_s=remaining_hold_s if kind == "curing_pause" else None,
           resume_decision="recompute_hold" if kind == "curing_pause" else "recalibrate_resume")
    db.c.execute("UPDATE print_job SET status='paused' WHERE id=?", (job_id,))
    db.commit()
    return "paused"


def resume(db: DB, job_id: str) -> dict:
    """续打：固化中断按当前温度重算剩余保温时间，而不是直接接着倒计时（PRT-004）。"""
    it = db.one("SELECT * FROM print_interruption WHERE print_job_id=? "
                "ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
    if not it:
        raise ValueError("该作业没有中断记录，无需续打")
    job = db.one("SELECT * FROM print_job WHERE id=?", (job_id,))
    if job["status"] != "paused":
        raise ValueError(f"作业状态为 {job['status']}，不是暂停中")
    remaining = int(it["remaining_hold_s"] or 0)
    temp_at = float(it["temp_at_c"] or 0.0)
    if it["kind"] == "curing_pause" and temp_at > 0:
        target, ambient = 170.0, 25.0
        loss = max(0.0, (target - temp_at) / max(1.0, target - ambient))
        remaining = int(round(remaining * (1.0 + loss)))
    db.c.execute("UPDATE print_job SET status='running', phase='curing' WHERE id=?", (job_id,))
    _event(db, job_id, 999, "action", "resume",
           f"续打：重算剩余保温 {remaining} s（中断时 {temp_at:.0f} ℃）",
           {"remaining_hold_s": remaining, "temp_at_c": temp_at}, job["layer_current"])
    db.commit()
    return {"resumed": True, "remaining_hold_s": remaining, "kind": it["kind"]}


def inject(db: DB, job_id: str, kind: str, layer_no: int, x: float, y: float, seed: int = 42) -> str:
    if kind not in FAULTS:
        raise ValueError(f"未知故障类型 {kind}")
    rng = random.Random(seed)
    jitter_x = rng.uniform(-0.5, 0.5)
    fid = db.ins("fault_injection", print_job_id=job_id, fault_kind=kind, layer_no=layer_no,
                 x_mm=round(x + jitter_x, 3), y_mm=y, random_seed=seed,
                 params_json=json.dumps({"severity": "full"}), applied_at=now())
    db.commit()
    return fid


def add_defect(db: DB, job_id: str, kind: str, layer_no: int, x: float, y: float,
               severity: str = "major", evidence: bytes | None = None) -> str:
    job = db.one("SELECT * FROM print_job WHERE id=?", (job_id,))
    sl = db.one("SELECT * FROM slice_set WHERE id=?", (job["slice_set_id"],))
    ev = db.put_blob(evidence or b"evidence:injected", "evidence")
    did = db.ins("defect", print_job_id=job_id, slice_set_id=job["slice_set_id"],
                 design_version_id=sl["design_version_id"], layer_no=layer_no,
                 x_mm=x, y_mm=y, defect_kind=kind, severity=severity, status="pending",
                 evidence_blob_hash=ev, detected_at=now())
    db.commit()
    return did


def attribute(db: DB, defect_id: str, cause_kind: str = "design", confidence: float = 0.72,
              suggestion_kind: str = "reslice", design_version_id: str | None = None) -> str:
    d = db.one("SELECT * FROM defect WHERE id=?", (defect_id,))
    aid = db.ins("attribution", defect_id=defect_id, cause_kind=cause_kind,
                 confidence=confidence, design_version_id=design_version_id or d["design_version_id"],
                 method="rule", suggestion_kind=suggestion_kind,
                 suggestion_json=json.dumps({"reslice": True, "note": "按当前版本重新切片"},
                                            ensure_ascii=False),
                 decided_by="local", decided_at=now(), created_at=now())
    db.commit()
    return aid


def run_demo(db: DB, project_id: str, slice_id: str, export_id: str,
             fault_layer: int = 2) -> dict:
    job = start(db, project_id, slice_id, export_id)
    fault_id = inject(db, job, "broken_line", fault_layer, 30.0, 20.0, seed=42)
    out = replay(db, job, inject_at={"layer": fault_layer, "kind": "broken_line"})
    defect = add_defect(db, job, "open", fault_layer, 30.0, 20.0, severity="major")
    attr = attribute(db, defect, cause_kind="design", confidence=0.72)
    return {"job_id": job, "fault_id": fault_id, "replay": out,
            "defect_id": defect, "attribution_id": attr}
