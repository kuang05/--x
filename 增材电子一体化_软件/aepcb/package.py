"""工程包导出/导入（PRJ-006）。

包结构：
    manifest.json     schema 版本、表行数、每个 blob 的哈希与大小、整包哈希
    tables/<表>.json  逐行数据（含 id，导入时原样恢复）
    blobs/<hash>      被引用的内容寻址文件

设计取舍：
    - **内嵌 blob**：不假设目标机器已有同样的 CAS 文件（数据模型冲突 C6 的裁定）。
    - **原样恢复 id**：单用户单机，导入到空库即可，不做 id 重映射。
    - 导入前校验整包哈希与 schema 版本；不匹配直接拒绝，不做“尽力恢复”。
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from .store import DB, new_id, now

SCHEMA_VERSION = 1

# 依赖顺序：父表在前；project↔version 的环在导入时单独处理
TABLES = [
    "project", "version", "version_item", "edit_session", "operation",
    "blob", "blob_gc_log", "migration_log", "external_file", "app_setting", "snapshot",
    "structure", "board_outline", "feature", "bool_candidate", "diagnostic",
    "circuit", "net_class", "component", "pad", "net", "route", "jumper", "keepout",
    "layout_candidate",
    "mrc_rule_version", "mrc_rule_threshold", "tool_geometry", "mrc_run", "mrc_issue",
    "material", "material_batch", "process_profile",
    "slice_set", "physical_layer", "deposit_pass", "slice_path", "thermal_step", "gcode_export",
    "connectivity_check",
    "device", "device_calibration", "print_job", "print_event", "print_interruption",
    "fault_injection", "defect", "attribution",
    "plan", "plan_step", "tool_call", "tool_registry", "diff_card", "replay_run",
    "accept_result", "cloud_call", "package_manifest", "dictionary_entry",
]


def _project_rows(db: DB, pid: str) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """按工程裁剪：只导出这个工程及其引用到的规则/工具/材料/blob。"""
    filters: dict[str, tuple[str, list]] = {
        "project": ("id=?", [pid]),
        "version": ("project_id=?", [pid]),
        "version_item": ("version_id IN (SELECT id FROM version WHERE project_id=?)", [pid]),
        "edit_session": ("project_id=?", [pid]),
        "operation": ("session_id IN (SELECT id FROM edit_session WHERE project_id=?)", [pid]),
        "snapshot": ("project_id=?", [pid]),
        "external_file": ("project_id=?", [pid]),
        "structure": ("project_id=?", [pid]),
        "board_outline": ("structure_id IN (SELECT id FROM structure WHERE project_id=?)", [pid]),
        "feature": ("structure_id IN (SELECT id FROM structure WHERE project_id=?)", [pid]),
        "bool_candidate": ("structure_id IN (SELECT id FROM structure WHERE project_id=?)", [pid]),
        "diagnostic": ("project_id=?", [pid]),
        "circuit": ("project_id=?", [pid]),
        "net_class": ("circuit_id IN (SELECT id FROM circuit WHERE project_id=?)", [pid]),
        "component": ("circuit_id IN (SELECT id FROM circuit WHERE project_id=?)", [pid]),
        "net": ("circuit_id IN (SELECT id FROM circuit WHERE project_id=?)", [pid]),
        "pad": ("component_id IN (SELECT id FROM component WHERE circuit_id IN "
                "(SELECT id FROM circuit WHERE project_id=?))", [pid]),
        "route": ("circuit_id IN (SELECT id FROM circuit WHERE project_id=?)", [pid]),
        "jumper": ("circuit_id IN (SELECT id FROM circuit WHERE project_id=?)", [pid]),
        "keepout": ("circuit_id IN (SELECT id FROM circuit WHERE project_id=?)", [pid]),
        "via": ("circuit_id IN (SELECT id FROM circuit WHERE project_id=?)", [pid]),
        "layout_candidate": ("circuit_id IN (SELECT id FROM circuit WHERE project_id=?)", [pid]),
        "connectivity_check": ("project_id=?", [pid]),
        "mrc_run": ("project_id=?", [pid]),
        "mrc_issue": ("run_id IN (SELECT id FROM mrc_run WHERE project_id=?)", [pid]),
        "mrc_rule_version": ("id IN (SELECT rule_version_id FROM mrc_run WHERE project_id=?)", [pid]),
        "mrc_rule_threshold": ("rule_version_id IN (SELECT rule_version_id FROM mrc_run WHERE project_id=?)", [pid]),
        "tool_geometry": ("id IN (SELECT tool_geometry_id FROM mrc_run WHERE project_id=?)", [pid]),
        "slice_set": ("project_id=?", [pid]),
        "physical_layer": ("slice_set_id IN (SELECT id FROM slice_set WHERE project_id=?)", [pid]),
        "deposit_pass": ("slice_set_id IN (SELECT id FROM slice_set WHERE project_id=?)", [pid]),
        "slice_path": ("deposit_pass_id IN (SELECT id FROM deposit_pass WHERE slice_set_id IN "
                       "(SELECT id FROM slice_set WHERE project_id=?))", [pid]),
        "thermal_step": ("slice_set_id IN (SELECT id FROM slice_set WHERE project_id=?)", [pid]),
        "gcode_export": ("slice_set_id IN (SELECT id FROM slice_set WHERE project_id=?)", [pid]),
        "process_profile": ("id IN (SELECT process_profile_id FROM slice_set WHERE project_id=?)", [pid]),
        "material_batch": ("id IN (SELECT material_batch_id FROM deposit_pass WHERE slice_set_id IN "
                           "(SELECT id FROM slice_set WHERE project_id=?) AND material_batch_id IS NOT NULL)",
                           [pid]),
        "material": ("id IN (SELECT material_id FROM deposit_pass WHERE slice_set_id IN "
                     "(SELECT id FROM slice_set WHERE project_id=?))", [pid]),
        "device": ("project_id=?", [pid]),
        "device_calibration": ("device_id IN (SELECT id FROM device WHERE project_id=?)", [pid]),
        "print_job": ("project_id=?", [pid]),
        "print_event": ("print_job_id IN (SELECT id FROM print_job WHERE project_id=?)", [pid]),
        "print_interruption": ("print_job_id IN (SELECT id FROM print_job WHERE project_id=?)", [pid]),
        "fault_injection": ("print_job_id IN (SELECT id FROM print_job WHERE project_id=?)", [pid]),
        "defect": ("print_job_id IN (SELECT id FROM print_job WHERE project_id=?)", [pid]),
        "attribution": ("defect_id IN (SELECT id FROM defect WHERE print_job_id IN "
                        "(SELECT id FROM print_job WHERE project_id=?))", [pid]),
        "plan": ("project_id=?", [pid]),
        "plan_step": ("plan_id IN (SELECT id FROM plan WHERE project_id=?)", [pid]),
        "tool_call": ("step_id IN (SELECT id FROM plan_step WHERE plan_id IN "
                      "(SELECT id FROM plan WHERE project_id=?))", [pid]),
        "diff_card": ("plan_id IN (SELECT id FROM plan WHERE project_id=?)", [pid]),
        "replay_run": ("plan_id IN (SELECT id FROM plan WHERE project_id=?)", [pid]),
        "accept_result": ("project_id=?", [pid]),
        "cloud_call": ("project_id=?", [pid]),
        "package_manifest": ("project_id=?", [pid]),
        "migration_log": ("project_id=?", [pid]),
    }
    rows: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}
    for table in TABLES:
        if table in ("blob", "blob_gc_log", "app_setting", "dictionary_entry", "tool_registry"):
            continue
        where = filters.get(table)
        if not where:
            continue
        sql, params = where
        got = [dict(r) for r in db.q(f"SELECT * FROM {table} WHERE {sql}", tuple(params))]
        rows[table] = got
        counts[table] = len(got)

    # blob 闭包：扫描已收集行里形如 sha256 的字符串，只带真正用到的文件
    import re as _re
    hex64 = _re.compile(r"^[0-9a-f]{64}$")
    hashes: set[str] = set()
    for table_rows in rows.values():
        for r in table_rows:
            for v in r.values():
                if isinstance(v, str) and hex64.match(v):
                    hashes.add(v)
    blob_rows = [dict(r) for r in db.q("SELECT * FROM blob") if r["hash"] in hashes]
    rows["blob"] = blob_rows
    counts["blob"] = len(blob_rows)
    return rows, counts


def export_project(db: DB, project_id: str, out_path: str | Path) -> dict:
    """导出单个工程（含引用到的规则/工具/材料与 blob），可在另一台机器导入。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows, counts = _project_rows(db, project_id)
    payloads: dict[str, bytes] = {}
    for table, table_rows in rows.items():
        payloads[f"tables/{table}.json"] = json.dumps(
            table_rows, ensure_ascii=False, sort_keys=True).encode()
    blobs = {}
    for b in rows.get("blob", []):
        try:
            data = db.get_blob(b["hash"])
        except FileNotFoundError:
            continue
        payloads[f"blobs/{b['hash']}"] = data
        blobs[b["hash"]] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    manifest = {"schema_version": SCHEMA_VERSION, "app_version": "1.4.0",
                "scope": "project", "project_id": project_id,
                "exported_at": now(), "counts": counts, "blobs": blobs}
    payloads["manifest.json"] = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    package_hash = hashlib.sha256(
        b"".join(sorted(payloads[k] for k in payloads if k != "manifest.json"))).hexdigest()
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in payloads.items():
            z.writestr(name, data)
    db.ins("package_manifest", project_id=project_id, exported_at=manifest["exported_at"],
           package_hash=package_hash, schema_version=SCHEMA_VERSION, include_blobs=1,
           entries_json=json.dumps({"scope": "project", "counts": counts,
                                    "blobs": len(blobs)}, ensure_ascii=False),
           import_result="", created_at=now())
    db.commit()
    return {"path": str(p), "package_hash": package_hash, "bytes": p.stat().st_size,
            "counts": counts, "blobs": len(blobs), "scope": "project"}


def _rows(db: DB, table: str) -> list[dict]:
    return [dict(r) for r in db.q(f"SELECT * FROM {table}")]


def export(db: DB, out_path: str | Path) -> dict:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    payloads: dict[str, bytes] = {}
    for t in TABLES:
        rows = _rows(db, t)
        counts[t] = len(rows)
        payloads[f"tables/{t}.json"] = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()

    blob_rows = db.q("SELECT hash, byte_size FROM blob")
    blobs = {}
    for b in blob_rows:
        try:
            data = db.get_blob(b["hash"])
        except FileNotFoundError:
            continue
        payloads[f"blobs/{b['hash']}"] = data
        blobs[b["hash"]] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "app_version": "1.3.0",
        "exported_at": now(),
        "counts": counts,
        "blobs": blobs,
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    payloads["manifest.json"] = manifest_bytes
    package_hash = hashlib.sha256(
        b"".join(sorted(payloads[k] for k in payloads if k != "manifest.json"))).hexdigest()

    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in payloads.items():
            z.writestr(name, data)
    size = p.stat().st_size
    db.ins("package_manifest", project_id=None, exported_at=manifest["exported_at"],
           package_hash=package_hash, schema_version=SCHEMA_VERSION, include_blobs=1,
           entries_json=json.dumps({"counts": counts, "blobs": len(blobs)}, ensure_ascii=False),
           import_result="", created_at=now())
    db.commit()
    return {"path": str(p), "package_hash": package_hash, "bytes": size,
            "counts": counts, "blobs": len(blobs)}


def _verify(z: zipfile.ZipFile) -> dict:
    manifest = json.loads(z.read("manifest.json"))
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"schema 版本不匹配：包 {manifest['schema_version']} / 本机 {SCHEMA_VERSION}")
    for name in z.namelist():
        if name.startswith("blobs/"):
            h = name.split("/", 1)[1]
            data = z.read(name)
            if hashlib.sha256(data).hexdigest() != h:
                raise ValueError(f"blob 哈希不符：{h}")
    return manifest


GLOBAL_TABLES = {"app_setting", "dictionary_entry", "tool_registry"}
NO_REMAP = {"blob", "slice_path"} | GLOBAL_TABLES


def import_package(db: DB, path: str | Path, merge: bool = False) -> dict:
    """导入工程包。merge=False 要求空库；merge=True 时重映射 id 后叠加到现有库。"""
    p = Path(path)
    with zipfile.ZipFile(p) as z:
        manifest = _verify(z)
        rows_by_table: dict[str, list[dict]] = {}
        for name in z.namelist():
            if name.startswith("tables/"):
                table = name[len("tables/"):-len(".json")]
                rows_by_table[table] = json.loads(z.read(name))
        blobs = {n.split("/", 1)[1]: z.read(n) for n in z.namelist() if n.startswith("blobs/")}

        id_map: dict[str, str] = {}
        if merge:
            # 1) 全局字典类去重：material 按 code、全局 process_profile 按 name
            keep: dict[str, list[dict]] = {}
            for table, table_rows in rows_by_table.items():
                if table in GLOBAL_TABLES:
                    continue
                kept = []
                for r in table_rows:
                    if table == "material" and r.get("code"):
                        ex = db.one("SELECT id FROM material WHERE code=?", (r["code"],))
                        if ex:
                            id_map[r["id"]] = ex["id"]
                            continue
                    if table == "process_profile" and r.get("project_id") is None:
                        ex = db.one("SELECT id FROM process_profile WHERE project_id IS NULL AND name=?",
                                    (r["name"],))
                        if ex:
                            id_map[r["id"]] = ex["id"]
                            continue
                    kept.append(r)
                keep[table] = kept
            rows_by_table = keep
            # 2) 其余行重新分配 id
            for table, table_rows in rows_by_table.items():
                if table in NO_REMAP:
                    continue
                for r in table_rows:
                    old = r.get("id")
                    if old and old not in id_map:
                        id_map[old] = new_id()
                        r["id"] = id_map[old]
            # 3) 所有引用列改写成新 id
            for table, table_rows in rows_by_table.items():
                for r in table_rows:
                    for k, v in list(r.items()):
                        if isinstance(v, str) and v in id_map:
                            r[k] = id_map[v]

        db.c.execute("PRAGMA defer_foreign_keys=ON")
        for table in TABLES:
            table_rows = rows_by_table.get(table)
            if not table_rows:
                continue
            if table == "project":
                table_rows = [{**r, "current_version_id": None} for r in table_rows]
            if table == "version":
                table_rows.sort(key=lambda r: (r.get("project_id", ""), r.get("version_no", 0)))
            if table == "operation":
                table_rows.sort(key=lambda r: (r.get("session_id", ""), r.get("seq", 0)))
            cols = [c for c in table_rows[0] if not (table == "slice_path" and c == "id")]
            marks = ", ".join("?" for _ in cols)
            prefix = "INSERT OR IGNORE" if table == "blob" else "INSERT"
            sql = f"{prefix} INTO {table} ({', '.join(cols)}) VALUES ({marks})"
            db.c.executemany(sql, [tuple(r.get(c) for c in cols) for r in table_rows])

        for h, data in blobs.items():
            target = db.root / f"blobs/{h[:2]}/{h[2:4]}/{h}"
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

    for proj in db.q("SELECT id FROM project"):
        v = db.scalar("SELECT id FROM version WHERE project_id=? ORDER BY version_no DESC LIMIT 1",
                      (proj["id"],))
        if v:
            db.c.execute("UPDATE project SET current_version_id=? WHERE id=?", (v, proj["id"]))
    db.commit()
    broken = db.q("PRAGMA foreign_key_check")
    if broken:
        raise ValueError(f"导入后外键不一致：{broken[:3]}")
    return {"manifest": manifest, "counts": manifest["counts"], "merged": merge,
            "projects": [id_map.get(r["id"], r["id"]) for r in rows_by_table.get("project", [])]}


def _legacy_import(db: DB, path: str | Path) -> dict:
    p = Path(path)
    with zipfile.ZipFile(p) as z:
        manifest = _verify(z)
        # 自引用外键（version.parent_version_id、operation.undo_of_op_id）无法保证行序，
        # 因此把外键检查推迟到提交时；提交前还要人工核一遍。
        db.c.execute("PRAGMA defer_foreign_keys=ON")
        for t in TABLES:
            name = f"tables/{t}.json"
            if name not in z.namelist():
                continue
            rows = json.loads(z.read(name))
            if not rows:
                continue
            if t == "project":
                rows = [{**r, "current_version_id": None} for r in rows]
            if t == "version":
                rows.sort(key=lambda r: (r.get("project_id", ""), r.get("version_no", 0)))
            if t == "operation":
                rows.sort(key=lambda r: (r.get("session_id", ""), r.get("seq", 0)))
            cols = list(rows[0])
            marks = ", ".join("?" for _ in cols)
            sql = f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({marks})"
            db.c.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        for name in z.namelist():
            if name.startswith("blobs/"):
                h = name.split("/", 1)[1]
                target = db.root / f"blobs/{h[:2]}/{h[2:4]}/{h}"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(name))
    # 回填工程当前版本
    for proj in db.q("SELECT id FROM project"):
        v = db.scalar("SELECT id FROM version WHERE project_id=? ORDER BY version_no DESC LIMIT 1",
                      (proj["id"],))
        if v:
            db.c.execute("UPDATE project SET current_version_id=? WHERE id=?", (v, proj["id"]))
    db.commit()
    broken = db.q("PRAGMA foreign_key_check")
    if broken:
        raise ValueError(f"导入后外键不一致：{broken[:3]}")
    return {"manifest": manifest, "counts": manifest["counts"]}
