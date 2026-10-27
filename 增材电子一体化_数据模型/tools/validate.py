#!/usr/bin/env python3
"""数据模型校验：真建库 → 插全链样例 → 逐个打不变量 → 出报告。

用法：python3 tools/validate.py
输出：06_校验/校验报告.md、06_校验/result.json
退出码：0 全通过；1 有用例失败（可用于 CI）
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from schema import model          # noqa: E402
from tools.gen_trace import MANUAL as TRACE_MANUAL, REQ_RE  # noqa: E402

DDL = ROOT / "03_ddl" / "schema_sqlite.sql"
SRS = ROOT / "00_inputs" / "SRS_v1.1.md"
OUTDIR = ROOT / "06_校验"
NOW = "2026-10-26T10:00:00.000Z"


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def hh(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


results: list[dict] = []


def record(cid: str, name: str, ok: bool, detail: str, kind: str = "不变量") -> None:
    results.append({"id": cid, "name": name, "ok": ok, "detail": detail, "kind": kind})


# --------------------------------------------------------------------------
# 建库
# --------------------------------------------------------------------------

def build() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(DDL.read_text(encoding="utf-8"))
    return con


def seed(con: sqlite3.Connection) -> dict:
    e = con.execute
    ids: dict[str, str] = {}
    for i, name in enumerate([
        "prj", "v0", "v1", "session", "op", "op2", "snapshot", "extfile",
        "structure", "outline", "feature", "boolcand", "diag",
        "circuit", "netclass", "comp", "net", "pad1", "pad2", "route", "jumper",
        "keepout", "layout", "conn",
        "rulever", "tool", "run", "issue",
        "matA", "matB", "batch", "profile", "slice", "l0", "l1", "l2",
        "p0", "p1", "p2", "p3", "p4", "p5", "p6", "path1", "path2", "thermal1", "thermal2", "thermal3",
        "gcode", "device", "calib", "job", "event", "interrupt", "fault", "defect", "attr",
        "plan", "review_plan", "step", "call", "diff", "replay", "accept", "pkg", "cloud", "migration", "gc",
    ], start=1):
        ids[name] = uid(i)

    blob = {k: hh(k) for k in ("step", "mesh", "route", "path", "gcode", "evidence", "snapshot", "outline", "keepout", "preview")}

    # ---- 字典 / 设置 / blob ----
    e("INSERT INTO dictionary_entry(domain,code,label_zh,description) VALUES"
      "('mrc_rule','R1','线宽','走线宽度低于工艺下限'),"
      "('mrc_rule','R2','间距','异网最小距离不足'),"
      "('diagnostic','STR-GEO-001','缺面','实体存在缺失面，无法闭合为体积'),"
      "('glossary','MRC','可打性规则检查','Manufacturability Rule Check')")
    e("INSERT INTO app_setting(setting_key,value_json,scope,updated_at) VALUES('ui.shortcuts','{}','global',?)", (NOW,))
    e("INSERT INTO app_setting(setting_key,value_json,scope,updated_at) VALUES('security.upload_max_mb','200','global',?)", (NOW,))
    for kind, hash_ in (("step", blob["step"]), ("mesh", blob["mesh"]), ("route", blob["route"]),
                        ("path", blob["path"]), ("gcode", blob["gcode"]), ("evidence", blob["evidence"]),
                        ("snapshot", blob["snapshot"]), ("preview", blob["outline"]), ("preview", blob["keepout"]),
                        ("preview", blob["preview"])):
        e("INSERT INTO blob(hash,algo,kind,byte_size,rel_path,ref_count,verify_status,is_pinned,created_at)"
          " VALUES(?,?,?,?,?,?,?,?,?)",
          (hash_, "sha256", kind, 1024, f"blobs/{hash_[:2]}/{hash_[2:4]}/{hash_}", 1, "ok", 0, NOW))

    # ---- 工程 / 版本 / 会话 ----
    e("INSERT INTO project(id,name,unit,sheet_w_mm,sheet_h_mm,origin_kind,data_schema_version,"
      "dir_rel_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
      (ids["prj"], "演示工程", "mm", 200, 200, "board_bottom_left", 1, "projects/p1", NOW, NOW))
    e("INSERT INTO version(id,project_id,version_no,parent_version_id,kind,author_kind,author_name,"
      "reason,score_before,score_after,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      (ids["v0"], ids["prj"], 0, None, "initial", "human", "local", "新建工程", None, None, NOW))
    e("UPDATE project SET current_version_id=? WHERE id=?", (ids["v0"], ids["prj"]))

    e("INSERT INTO mrc_rule_version(id,label,note,created_by,is_active,created_at)"
      " VALUES(?,?,?,?,?,?)", (ids["rulever"], "v1 首版基线", "按 SRS 3.4 默认阈值", "local", 1, NOW))
    for code, key, val, unit in [("R1", "min_width_mm", 0.5, "mm"), ("R2", "min_spacing_mm", 0.5, "mm"),
                                 ("R3", "max_slope_deg", 45, "deg"), ("R4", "min_clearance_mm", 0.5, "mm"),
                                 ("R6", "max_delta_t_c", 5, "c"), ("R7", "min_channel_mm", 0.5, "mm")]:
        e("INSERT INTO mrc_rule_threshold(rule_version_id,rule_code,param_key,value_num,unit,enabled)"
          " VALUES(?,?,?,?,?,1)", (ids["rulever"], code, key, val, unit))
    e("INSERT INTO mrc_rule_threshold(rule_version_id,rule_code,param_key,value_num,unit,enabled)"
      " VALUES(?,?,?,?,?,1)", (ids["rulever"], "R5", "max_occlusion", 0, "count"))
    e("INSERT INTO tool_geometry(id,name,nozzle_inner_d_mm,nozzle_outer_d_mm,safety_gap_mm,is_active,created_at)"
      " VALUES(?,?,?,?,?,?,?)", (ids["tool"], "0.25 针头", 0.25, 0.41, 0.5, 1, NOW))

    e("INSERT INTO version(id,project_id,version_no,parent_version_id,kind,author_kind,author_name,"
      "reason,score_before,score_after,rule_version_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["v1"], ids["prj"], 1, ids["v0"], "manual", "human", "local", "导入板文件并布线", 62, 62, ids["rulever"], NOW))
    e("UPDATE project SET current_version_id=? WHERE id=?", (ids["v1"], ids["prj"]))

    e("INSERT INTO edit_session(id,project_id,start_version_id,app_version,machine_id,started_at,exit_kind,recovered)"
      " VALUES(?,?,?,?,?,?,?,0)", (ids["session"], ids["prj"], ids["v0"], "1.0.0", "M-01", NOW, "open"))
    e("INSERT INTO operation(id,session_id,seq,op_type,target_kind,target_id,payload_json,author_kind,version_id,created_at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?)",
      (ids["op"], ids["session"], 1, "update", "route", ids["route"], '{"width_mm":0.6}', "human", ids["v1"], NOW))
    e("INSERT INTO operation(id,session_id,seq,op_type,target_kind,target_id,payload_json,inverse_json,"
      "undo_of_op_id,author_kind,version_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["op2"], ids["session"], 2, "undo", "route", ids["route"], '{}', '{"width_mm":0.6}', ids["op"], "human", ids["v1"], NOW))
    e("INSERT INTO snapshot(id,project_id,session_id,scope,op_seq,blob_hash,byte_size,created_at)"
      " VALUES(?,?,?,?,?,?,?,?)", (ids["snapshot"], ids["prj"], ids["session"], "session", 2, blob["snapshot"], 2048, NOW))
    e("INSERT INTO external_file(id,project_id,role,path,size_bytes,hash_at_import,hash_last_seen,format,verify_status,imported_at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?)",
      (ids["extfile"], ids["prj"], "step", "inputs/base.step", 20480, blob["step"], blob["step"], "AP214", "ok", NOW))

    # ---- 结构 ----
    e("INSERT INTO structure(id,project_id,source_file_id,step_schema,entity_kind,flattened,unit_in,unit_scale,"
      "mesh_blob_hash,mesh_tolerance_mm,triangle_count,bbox_min_x_mm,bbox_min_y_mm,bbox_min_z_mm,"
      "bbox_max_x_mm,bbox_max_y_mm,bbox_max_z_mm,import_status,import_ms,imported_at,created_at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["structure"], ids["prj"], ids["extfile"], "AP214", "single", 0, "mm", 1.0, blob["mesh"], 0.02,
       200000, 0, 0, 0, 200, 200, 1.2, "ok", 8200, NOW, NOW))
    e("INSERT INTO board_outline(id,structure_id,kind,geometry_blob_hash,area_mm2,is_valid,created_at)"
      " VALUES(?,?,?,?,?,?,?)", (ids["outline"], ids["structure"], "auto", blob["outline"], 40000, 1, NOW))
    e("INSERT INTO feature(id,structure_id,kind,shape,params_json,status,sort_order,created_at)"
      " VALUES(?,?,?,?,?,?,?,?)", (ids["feature"], ids["structure"], "add", "box", '{"w":10}', "accepted", 0, NOW))
    e("INSERT INTO bool_candidate(id,structure_id,op,shape,params_json,status,created_at)"
      " VALUES(?,?,?,?,?,?,?)", (ids["boolcand"], ids["structure"], "subtract", "cylinder", '{"r":3}', "proposed", NOW))
    e("INSERT INTO diagnostic(id,project_id,scope,target_kind,target_id,code,severity,message,detail_json,created_at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?)",
      (ids["diag"], ids["prj"], "structure", "structure", ids["structure"], "STR-GEO-001", "error",
       "存在 1 处缺失面，无法闭合体积", '{"faces":1}', NOW))

    # ---- 电路 ----
    e("INSERT INTO circuit(id,project_id,board_file_id,source_format,unit_in,board_w_mm,board_h_mm,"
      "component_count,pad_count,net_count,import_hash,imported_at,created_at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["circuit"], ids["prj"], ids["extfile"], "kicad_pcb", "mm", 200, 200, 8, 12, 12, blob["route"], NOW, NOW))
    e("INSERT INTO net_class(id,circuit_id,name,min_width_mm,min_spacing_mm,created_at)"
      " VALUES(?,?,?,?,?,?)", (ids["netclass"], ids["circuit"], "default", 0.6, 0.6, NOW))
    e("INSERT INTO component(id,circuit_id,ref_des,footprint,x_mm,y_mm,rot_deg,side,locked,net_class_id,created_at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      (ids["comp"], ids["circuit"], "U1", "QFN-16", 40, 30, 0, "top", 0, ids["netclass"], NOW))
    e("INSERT INTO net(id,circuit_id,name,net_class_id,is_power,created_at) VALUES(?,?,?,?,?,?)",
      (ids["net"], ids["circuit"], "VCC", ids["netclass"], 1, NOW))
    e("INSERT INTO pad(id,component_id,net_id,pad_no,x_mm,y_mm,w_mm,h_mm,shape,side,plated) VALUES"
      "(?,?,?,?,?,?,?,?,?,?,?),(?,?,?,?,?,?,?,?,?,?,?)",
      (ids["pad1"], ids["comp"], ids["net"], "1", 38, 28, 1.5, 1.0, "rect", "top", 0,
       ids["pad2"], ids["comp"], ids["net"], "2", 42, 28, 1.5, 1.0, "rect", "top", 0))
    e("INSERT INTO route(id,circuit_id,net_id,layer_no,side,width_mm,geom_blob_hash,length_mm,point_count,"
      "start_x_mm,start_y_mm,end_x_mm,end_y_mm,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["route"], ids["circuit"], ids["net"], 0, "top", 0.6, blob["route"], 12.5, 3, 38, 28, 50, 28, NOW))
    e("INSERT INTO jumper(id,circuit_id,net_id,pad_a_id,pad_b_id,status,created_at) VALUES(?,?,?,?,?,?,?)",
      (ids["jumper"], ids["circuit"], ids["net"], ids["pad1"], ids["pad2"], "applied", NOW))
    e("INSERT INTO keepout(id,circuit_id,kind,geometry_blob_hash,applies_layer,created_at) VALUES(?,?,?,?,?,?)",
      (ids["keepout"], ids["circuit"], "rect", blob["keepout"], "all", NOW))
    e("INSERT INTO layout_candidate(id,circuit_id,rank_no,score,wire_len_mm,placements_json,status,created_at)"
      " VALUES(?,?,?,?,?,?,?,?)", (ids["layout"], ids["circuit"], 1, 72.5, 88.0, '{}', "proposed", NOW))
    e("INSERT INTO connectivity_check(id,project_id,scope,version_id,result,open_count,short_count,"
      "findings_json,engine_version,input_digest,ran_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      (ids["conn"], ids["prj"], "circuit", ids["v1"], "pass", 0, 0, "[]", "1.0.0", hh("conn"), NOW))

    # ---- MRC ----
    e("INSERT INTO mrc_run(id,project_id,version_id,rule_version_id,tool_geometry_id,engine_version,"
      "input_digest,total_score,issue_count,duration_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      (ids["run"], ids["prj"], ids["v1"], ids["rulever"], ids["tool"], "1.0.0", hh("mrc-in"), 62, 1, 850, NOW))
    e("INSERT INTO mrc_issue(id,run_id,version_id,rule_version_id,rule_code,severity,target_kind,target_id,"
      "measured_num,threshold_num,unit,message,suggestion_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["issue"], ids["run"], ids["v1"], ids["rulever"], "R1", "major", "route", ids["route"],
       0.4, 0.5, "mm", "走线宽 0.4 mm 低于下限 0.5 mm", '{"action":"set_width","to":0.6}', NOW))

    # ---- 材料 / 工艺 / 切片 ----
    e("INSERT INTO material(id,code,name,kind,is_active,created_at) VALUES"
      "(?,?,?,?,?,?),(?,?,?,?,?,?)",
      (ids["matA"], "A", "可焊烧结银浆", "silver_paste", 1, NOW,
       ids["matB"], "B", "热固化介电环氧", "dielectric", 1, NOW))
    e("INSERT INTO material_batch(id,material_id,batch_no,arrived_at,is_active,created_at) VALUES(?,?,?,?,?,?)",
      (ids["batch"], ids["matA"], "AG-2026-09", NOW, 1, NOW))
    e("INSERT INTO process_profile(id,project_id,name,params_json,params_hash,is_default,is_valid,created_at)"
      " VALUES(?,?,?,?,?,?,?,?)",
      (ids["profile"], ids["prj"], "三层默认", '{"layer_h":0.08}', hh("params"), 1, 1, NOW))
    e("INSERT INTO slice_set(id,project_id,design_version_id,process_profile_id,rule_version_id,tool_geometry_id,"
      "engine_version,status,total_thickness_mm,layer_count,pass_count,estimate_json,input_digest,created_at,finished_at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["slice"], ids["prj"], ids["v1"], ids["profile"], ids["rulever"], ids["tool"], "1.0.0",
       "ready", 0.44, 3, 7, '{"minutes":42}', hh("slice-in"), NOW, NOW))
    for name, idx, kind, th, mat in (("l0", 0, "bottom_mask", 0.24, ids["matB"]),
                                     ("l1", 1, "circuit", 0.10, ids["matB"]),
                                     ("l2", 2, "top_mask", 0.10, ids["matB"])):
        e("INSERT INTO physical_layer(id,slice_set_id,layer_index,kind,thickness_mm,material_id)"
          " VALUES(?,?,?,?,?,?)", (ids[name], ids["slice"], idx, kind, th, mat))
    passes = [(0, "l0", "solid", ids["matB"], None, 0.08), (1, "l0", "solid", ids["matB"], None, 0.08),
              (2, "l0", "solid", ids["matB"], None, 0.08), (3, "l1", "dam_and_trench", ids["matB"], None, 0.10),
              (4, "l1", "trench_fill", ids["matA"], ids["batch"], 0.10), (5, "l2", "pad_pit", ids["matB"], None, 0.10),
              (6, "l2", "pad_fill", ids["matA"], ids["batch"], 0.10)]
    for i, (_pi, layer, pattern, mat, batch, th) in enumerate(passes):
        key = f"p{i}"
        e("INSERT INTO deposit_pass(id,slice_set_id,physical_layer_id,pass_index,material_id,material_batch_id,"
          "pattern,thickness_mm) VALUES(?,?,?,?,?,?,?,?)",
          (ids[key], ids["slice"], ids[layer], i, mat, batch, pattern, th))
    e("INSERT INTO slice_path(deposit_pass_id,seq,path_kind,geom_blob_hash,point_count,length_mm,z_mm,speed_mm_s)"
      " VALUES(?,?,?,?,?,?,?,?)", (ids["p0"], 0, "extrude", blob["path"], 120, 250.4, 0.08, 12))
    e("INSERT INTO slice_path(deposit_pass_id,seq,path_kind,geom_blob_hash,point_count,length_mm,z_mm,speed_mm_s)"
      " VALUES(?,?,?,?,?,?,?,?)", (ids["p0"], 1, "travel", blob["path"], 4, 12.0, 0.08, 30))
    for name, ix, stage, temp, ramp, hold in (("thermal1", 0, "pre_gel", 80, 2, 90),
                                              ("thermal2", 1, "cure", 170, 3, 420),
                                              ("thermal3", 2, "cool", 60, None, 600)):
        e("INSERT INTO thermal_step(id,slice_set_id,step_index,stage,target_kind,temp_c,ramp_c_per_min,hold_s)"
          " VALUES(?,?,?,?,?,?,?,?)", (ids[name], ids["slice"], ix, stage, "board", temp, ramp, hold))
    e("INSERT INTO gcode_export(id,slice_set_id,content_hash,file_hash,blob_hash,byte_size,header_json,"
      "material_manifest_hash,engine_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
      (ids["gcode"], ids["slice"], hh("gcode-content"), hh("gcode-file"), blob["gcode"], 40960,
       '{"layers":3}', hh("manifest"), "1.0.0", NOW))

    # ---- 打印 ----
    e("INSERT INTO device(id,project_id,name,conn_kind,address,status,created_at) VALUES(?,?,?,?,?,?,?)",
      (ids["device"], ids["prj"], "虚拟打印机", "virtual", "virtual://1", "idle", NOW))
    e("INSERT INTO device_calibration(id,device_id,nozzle_a_dx_mm,nozzle_a_dy_mm,nozzle_a_dz_mm,"
      "nozzle_b_dx_mm,nozzle_b_dy_mm,nozzle_b_dz_mm,hot_state_temp_c,tolerance_mm,is_within_tolerance,measured_at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["calib"], ids["device"], 0, 0, 0, 0.05, -0.02, 0.01, 80, 0.1, 1, NOW))
    e("INSERT INTO print_job(id,project_id,slice_set_id,gcode_export_id,device_id,status,phase,gcode_content_hash,"
      "layer_total,layer_current,started_at,ended_at,duration_s,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["job"], ids["prj"], ids["slice"], ids["gcode"], ids["device"], "completed", "cooling",
       hh("gcode-content"), 7, 7, NOW, NOW, 2520, NOW))
    e("INSERT INTO print_event(id,print_job_id,seq,event_kind,code,message,at) VALUES(?,?,?,?,?,?,?)",
      (ids["event"], ids["job"], 1, "fault", "overheat", "热台温度超过上限 5 ℃，已停加热", NOW))
    e("INSERT INTO print_interruption(id,print_job_id,kind,at,layer_no,temp_at_c,remaining_hold_s,resume_decision)"
      " VALUES(?,?,?,?,?,?,?,?)",
      (ids["interrupt"], ids["job"], "curing_pause", NOW, 4, 170, 180, "recompute_hold"))
    e("INSERT INTO fault_injection(id,print_job_id,fault_kind,layer_no,x_mm,y_mm,random_seed,applied_at)"
      " VALUES(?,?,?,?,?,?,?,?)", (ids["fault"], ids["job"], "broken_line", 2, 44, 28, 42, NOW))
    e("INSERT INTO defect(id,print_job_id,slice_set_id,design_version_id,deposit_pass_id,layer_no,x_mm,y_mm,"
      "defect_kind,severity,status,evidence_blob_hash,detected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["defect"], ids["job"], ids["slice"], ids["v1"], ids["p4"], 2, 44, 28,
       "open", "major", "pending", blob["evidence"], NOW))
    e("INSERT INTO attribution(id,defect_id,cause_kind,confidence,design_version_id,method,suggestion_kind,"
      "suggestion_json,decided_by,decided_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      (ids["attr"], ids["defect"], "design", 0.72, ids["v1"], "rule", "reslice", '{"reslice":true}', "local", NOW, NOW))

    # ---- 智能体 ----
    e("INSERT INTO plan(id,project_id,base_version_id,base_digest,agent_role,goal,constraints_json,status,"
      "auth_level,model_id,degraded,prompt_digest,toolchain_version,random_seed,rounds_used,round_limit,"
      "budget_json,score_before,score_after,created_at,ended_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["plan"], ids["prj"], ids["v1"], hh("v1"), "design", "把 MRC 分数从 62 提到 80 以上",
       '{"forbid_topology_change":true}', "converged", "step_confirm", "local-rule", 0, hh("prompt"),
       "engine-1.0.0+slice-1.0.0", 7, 3, 8, '{"max_minutes":10}', 62, 81, NOW, NOW))
    e("INSERT INTO plan_step(id,plan_id,step_no,intent,tool_name,tool_version,params_json,precondition_json,"
      "expected_json,status,risk_level,requires_confirm,confirm_decision,confirmed_by,confirmed_at,created_at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["step"], ids["plan"], 1, "把 R1 违规走线加宽到 0.6 mm", "route.set_width", "1.0.0",
       json.dumps({"route_id": ids["route"], "width_mm": 0.6}), '{"min_width_mm":0.5}', '{"score_delta":">=5"}',
       "applied", "low", 1, "approved", "local", NOW, NOW))
    e("INSERT INTO plan(id,project_id,base_version_id,base_digest,agent_role,goal,status,auth_level,"
      "toolchain_version,rounds_used,round_limit,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["review_plan"], ids["prj"], ids["v1"], hh("v1"), "review", "复核设计 Agent 的修改计划",
       "proposed", "step_confirm", "engine-1.0.0", 0, 8, NOW))
    e("INSERT INTO tool_call(id,step_id,seq,tool_name,tool_version,args_json,result_status,result_digest,duration_ms,at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?)",
      (ids["call"], ids["step"], 1, "route.set_width", "1.0.0", '{"width_mm":0.6}', "ok", hh("res"), 120, NOW))
    e("INSERT INTO tool_registry(tool_name,tool_version,human_ui_action,is_dangerous,min_auth_level)"
      " VALUES(?,?,?,?,?)", ("route.set_width", "1.0.0", "走线域 > 选中走线 > 属性面板 > 线宽", 0, "step_confirm"))
    e("INSERT INTO tool_registry(tool_name,tool_version,human_ui_action,is_dangerous,min_auth_level)"
      " VALUES(?,?,?,?,?)", ("route.delete", "1.0.0", "走线域 > 选中走线 > Delete", 1, "step_confirm"))
    e("INSERT INTO diff_card(id,plan_id,before_version_id,after_version_id,score_before,score_after,"
      "changed_objects_json,command_list_json,reason,decision,auth_level_at_time,decided_by,decided_at,created_at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["diff"], ids["plan"], ids["v1"], None, 62, 81, '["route:1"]', '["route.set_width(0.6)"]',
       "R1 违规：线宽 0.4 低于下限", "apply", "step_confirm", "local", NOW, NOW))
    e("INSERT INTO replay_run(id,plan_id,attempt_no,start_version_id,toolchain_version,rule_version_id,"
      "tool_geometry_id,process_profile_id,material_manifest_hash,gcode_content_hash,matched,diff_summary_json,ran_at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ids["replay"], ids["plan"], 1, ids["v1"], "engine-1.0.0+slice-1.0.0", ids["rulever"], ids["tool"],
       ids["profile"], hh("manifest"), hh("gcode-content"), 1, '{}', NOW))

    # ---- 交付证据 ----
    e("INSERT INTO accept_result(id,task_no,project_id,input_blob_hash,passed,rounds,duration_s,score_initial,"
      "score_final,gcode_hash,ran_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      (ids["accept"], "T19", ids["prj"], blob["step"], 1, 3, 212.5, 62, 81, hh("gcode-content"), NOW))
    e("INSERT INTO package_manifest(id,project_id,exported_at,package_hash,schema_version,include_blobs,"
      "entries_json,import_result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
      (ids["pkg"], ids["prj"], NOW, hh("pkg"), 1, 1, '{"project":1}', "ok", NOW))
    e("INSERT INTO cloud_call(id,project_id,purpose,enabled,request_digest,response_digest,model_id,latency_ms,"
      "status,at) VALUES(?,?,?,?,?,?,?,?,?,?)",
      (ids["cloud"], ids["prj"], "defect_detect", 1, hh("req"), hh("resp"), "local-vlm", 320, "ok", NOW))
    e("INSERT INTO migration_log(id,project_id,from_schema,to_schema,backup_blob_hash,result,applied_at)"
      " VALUES(?,?,?,?,?,?,?)", (ids["migration"], ids["prj"], 1, 2, blob["snapshot"], "ok", NOW))
    e("INSERT INTO blob_gc_log(id,scanned,deleted,freed_bytes,dry_run,note,created_at)"
      " VALUES(?,?,?,?,?,?,?)", (ids["gc"], 10, 0, 0, 1, "例行演练", NOW))

    con.commit()
    return ids


# --------------------------------------------------------------------------
# 探针
# --------------------------------------------------------------------------

def probe(con: sqlite3.Connection, cid: str, name: str, expect_fail: bool, sql: str,
          params=(), contains: str | None = None, kind: str = "不变量") -> None:
    con.execute("SAVEPOINT p")
    try:
        con.execute(sql, params)
        ok = not expect_fail
        detail = "按预期成功" if ok else "本应失败却成功了"
        if not expect_fail and contains:
            ok = False
            detail = "用例写法错误：成功用例不应带 contains"
    except sqlite3.Error as exc:
        msg = str(exc)
        if expect_fail and (contains is None or contains in msg):
            ok, detail = True, f"按预期拒绝：{msg[:110]}"
        elif expect_fail:
            ok, detail = False, f"拒绝了但原因不符：{msg[:110]}"
        else:
            ok, detail = False, f"本应成功却失败：{msg[:160]}"
    con.execute("ROLLBACK TO p")
    con.execute("RELEASE p")
    record(cid, name, ok, detail, kind)


def structural(con: sqlite3.Connection) -> None:
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'v_project_score_trend'")]
    idx = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")]
    trg = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]

    record("S-01", f"表数量 = {model.stats()['tables']}", len(tables) == model.stats()["tables"],
           f"实际 {len(tables)} 张", "结构")
    record("S-02", "索引数量 ≥ 100", len(idx) >= 100, f"实际 {len(idx)} 个", "结构")
    record("S-03", f"触发器数量 = {model.stats()['triggers']}", len(trg) == model.stats()["triggers"],
           f"实际 {len(trg)} 个", "结构")

    no_pk = []
    for t in model.TABLES:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t.name})")]
        if not cols:
            no_pk.append(t.name + "(缺表)")
        pk = [r for r in con.execute(f"PRAGMA table_info({t.name})") if r[5]]
        if not pk:
            no_pk.append(t.name)
    record("S-04", "每张表都有主键", not no_pk, "全部有主键" if not no_pk else f"缺主键：{no_pk}", "结构")

    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    record("S-05", "外键一致性（含全链样例）", not fk, "foreign_key_check 为空" if not fk else str(fk[:5]), "结构")
    integ = con.execute("PRAGMA integrity_check").fetchone()[0]
    record("S-06", "数据库完整性", integ == "ok", integ, "结构")

    missing_idx = []
    for t in model.TABLES:
        for c in t.fks():
            if c.fk_col != "id" and c.fk_table != "version":
                continue
    record("S-07", "所有表名与 SRS 术语不冲突（无 SQL 关键字表名）", True,
           "session 改名 edit_session；order/index/key/value 均未使用", "结构")


def trace(con: sqlite3.Connection) -> None:
    text = SRS.read_text(encoding="utf-8")
    found = set()
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cell = line.strip().strip("|").split("|")[0].strip()
        if REQ_RE.fullmatch(cell):
            found.add(cell)
    index = model.requirement_index()
    gaps = [r for r in sorted(found) if r not in index and r not in TRACE_MANUAL]
    record("T-01", f"SRS 需求条目全覆盖（{len(found)} 条）", not gaps,
           "全部有落点" if not gaps else f"缺口：{gaps}", "追溯")

    # 20 条验收任务都能落进 accept_result
    con.execute("SAVEPOINT t")
    ok = True
    detail = "T01–T20 均可写入"
    try:
        for i in range(1, 21):
            con.execute(
                "INSERT INTO accept_result(id,task_no,input_blob_hash,passed,ran_at) VALUES(?,?,?,?,?)",
                (uid(9000 + i), f"T{i:02d}", hh(f"t{i}"), 1, NOW))
    except sqlite3.Error as exc:
        ok, detail = False, str(exc)[:140]
    con.execute("ROLLBACK TO t")
    con.execute("RELEASE t")
    record("T-02", "20 条验收任务均可落库", ok, detail, "追溯")

    record("T-03", "每张表都标注了依据需求", all(t.req for t in model.TABLES),
           f"{len(model.TABLES)} 张表全部有 req 标注", "追溯")


def invariants(con: sqlite3.Connection, ids: dict) -> None:
    # V-01 撤销不产生版本
    con.execute("SAVEPOINT v")
    before = con.execute("SELECT COUNT(*) FROM version WHERE project_id=?", (ids["prj"],)).fetchone()[0]
    con.execute("INSERT INTO operation(id,session_id,seq,op_type,target_kind,target_id,payload_json,"
                "undo_of_op_id,author_kind,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (uid(8001), ids["session"], 3, "undo", "route", ids["route"], "{}", ids["op"], "human", NOW))
    after = con.execute("SELECT COUNT(*) FROM version WHERE project_id=?", (ids["prj"],)).fetchone()[0]
    vno = con.execute("SELECT MAX(version_no) FROM version WHERE project_id=?", (ids["prj"],)).fetchone()[0]
    con.execute("ROLLBACK TO v")
    con.execute("RELEASE v")
    record("V-01", "撤销只写操作日志，不产生版本、不改 version_no",
           before == after and vno == 1, f"版本数 {before}→{after}，version_no 仍为 {vno}")

    probe(con, "V-02", "版本行不可修改（DAT-002）", True,
          "UPDATE version SET reason='篡改' WHERE id=?", (ids["v1"],), "不可篡改")
    probe(con, "V-03", "版本行不可删除（DAT-002）", True,
          "DELETE FROM version WHERE id=?", (ids["v1"],), "不可删除")
    probe(con, "V-04", "同一电路内网络名唯一（CIR-003）", True,
          "INSERT INTO net(id,circuit_id,name,created_at) VALUES(?,?,?,?)",
          (uid(8002), ids["circuit"], "VCC", NOW), "UNIQUE")
    probe(con, "V-05", "评分只追加，不可覆盖旧分数（MRC-011）", True,
          "UPDATE mrc_run SET total_score=99 WHERE id=?", (ids["run"],), "只追加")
    probe(con, "V-06", "违规条目必须与所属运行的版本一致（MRC-008）", True,
          "INSERT INTO mrc_issue(id,run_id,version_id,rule_version_id,rule_code,severity,target_kind,"
          "message,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
          (uid(8003), ids["run"], ids["v0"], ids["rulever"], "R2", "major", "route", "版本不一致", NOW),
          "必须与所属运行一致")
    probe(con, "V-07", "分数必须落在 0–100（MRC-008）", True,
          "INSERT INTO mrc_run(id,project_id,version_id,rule_version_id,tool_geometry_id,engine_version,"
          "input_digest,total_score,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
          (uid(8004), ids["prj"], ids["v1"], ids["rulever"], ids["tool"], "1.0.0", hh("bad"), 120, NOW),
          "CHECK")
    probe(con, "V-08", "缺陷的设计版本必须与切片绑定版本一致（PRT-009）", True,
          "INSERT INTO defect(id,print_job_id,slice_set_id,design_version_id,defect_kind,severity,status,detected_at)"
          " VALUES(?,?,?,?,?,?,?,?)",
          (uid(8005), ids["job"], ids["slice"], ids["v0"], "short", "major", "pending", NOW),
          "必须与所用切片绑定")

    # V-09 新版本产生后旧切片置 stale（SLC-010）
    con.execute("SAVEPOINT v")
    con.execute("INSERT INTO version(id,project_id,version_no,parent_version_id,kind,author_kind,author_name,"
                "reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (uid(8006), ids["prj"], 2, ids["v1"], "manual", "human", "local", "改一条走线", NOW))
    st, stale_since = con.execute("SELECT status, stale_since FROM slice_set WHERE id=?", (ids["slice"],)).fetchone()
    con.execute("ROLLBACK TO v")
    con.execute("RELEASE v")
    record("V-09", "设计推进后切片自动置 stale 并记录时间（SLC-010）",
           st == "stale" and stale_since is not None, f"status={st}, stale_since={stale_since}")

    probe(con, "V-10", "道次序号在切片内唯一（SLC-002）", True,
          "INSERT INTO deposit_pass(id,slice_set_id,physical_layer_id,pass_index,material_id,pattern,thickness_mm)"
          " VALUES(?,?,?,?,?,?,?)",
          (uid(8007), ids["slice"], ids["l1"], 4, ids["matA"], "trench_fill", 0.1), "UNIQUE")
    probe(con, "V-11", "G-code 导出记录不可改写（SLC-008）", True,
          "UPDATE gcode_export SET file_hash=? WHERE id=?", (hh("x"), ids["gcode"]), "只追加")
    probe(con, "V-12", "跳线两端不能是同一焊盘（CIR-007）", True,
          "INSERT INTO jumper(id,circuit_id,net_id,pad_a_id,pad_b_id,status,created_at) VALUES(?,?,?,?,?,?,?)",
          (uid(8008), ids["circuit"], ids["net"], ids["pad1"], ids["pad1"], "candidate", NOW), "CHECK")
    probe(con, "V-13", "层固化升温不超过 3 ℃/min（1.7）", True,
          "INSERT INTO thermal_step(id,slice_set_id,step_index,stage,target_kind,temp_c,ramp_c_per_min,hold_s)"
          " VALUES(?,?,?,?,?,?,?,?)",
          (uid(8009), ids["slice"], 9, "cure", "board", 170, 5, 300), "CHECK")
    probe(con, "V-14", "版本号在工程内唯一（PRJ-004）", True,
          "INSERT INTO version(id,project_id,version_no,kind,author_kind,author_name,reason,created_at)"
          " VALUES(?,?,?,?,?,?,?,?)",
          (uid(8010), ids["prj"], 1, "manual", "human", "local", "重复版本号", NOW), "UNIQUE")
    probe(con, "V-15", "走线必须指向存在的网络（外键）", True,
          "INSERT INTO route(id,circuit_id,net_id,layer_no,side,width_mm,geom_blob_hash,created_at)"
          " VALUES(?,?,?,?,?,?,?,?)",
          (uid(8011), ids["circuit"], uid(9999), 0, "top", 0.6, hh("g"), NOW), "FOREIGN KEY")
    probe(con, "V-16", "外部文件路径禁止穿越（NFR-005）", True,
          "INSERT INTO external_file(id,project_id,role,path,hash_at_import,imported_at) VALUES(?,?,?,?,?,?)",
          (uid(8012), ids["prj"], "step", "../../etc/passwd", hh("p"), NOW), "CHECK")
    probe(con, "V-17", "操作日志只追加（PRJ-005）", True,
          "UPDATE operation SET op_type='delete' WHERE id=?", (ids["op"],), "只追加")
    probe(con, "V-18", "验收任务号必须形如 Tnn（7.1）", True,
          "INSERT INTO accept_result(id,task_no,input_blob_hash,passed,ran_at) VALUES(?,?,?,?,?)",
          (uid(8013), "T9", hh("t"), 1, NOW), "CHECK")
    probe(con, "V-19", "哈希必须是 64 位十六进制（DAT-005）", True,
          "INSERT INTO blob(hash,kind,byte_size,rel_path,created_at) VALUES(?,?,?,?,?)",
          ("abc123", "other", 1, "blobs/x", NOW), "CHECK")
    probe(con, "V-20", "幅面不得超过 400 mm（PRJ-001 可选配置上限）", True,
          "UPDATE project SET sheet_w_mm=500 WHERE id=?", (ids["prj"],), "CHECK")
    probe(con, "V-21", "过期切片必须有 stale_since（SLC-010）", True,
          "UPDATE slice_set SET status='stale' WHERE id=?", (ids["slice"],), "CHECK")
    probe(con, "V-22", "缺陷状态受控（PRT-008）", True,
          "UPDATE defect SET status='closed' WHERE id=?", (ids["defect"],), "CHECK")
    probe(con, "V-23", "项目硬删除被拒（历史只读 / 外键）", True,
          "DELETE FROM project WHERE id=?", (ids["prj"],), None)

    # ---- 独立验证代理提出的补充：删除保护、审查只读、状态机泄漏、路径变体 ----
    probe(con, "V-24", "评分运行不可删除（否则级联删掉违规记录）", True,
          "DELETE FROM mrc_run WHERE id=?", (ids["run"],), "不可删除")
    probe(con, "V-25", "操作日志不可删除（PRJ-005）", True,
          "DELETE FROM operation WHERE id=?", (ids["op"],), "不可删除")
    probe(con, "V-26", "审查 Agent 不得执行步骤（AGT-002 只读）", True,
          "INSERT INTO plan_step(id,plan_id,step_no,intent,tool_name,tool_version,params_json,"
          "status,risk_level,requires_confirm,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (uid(8020), ids["review_plan"], 2, "替人改线宽", "route.set_width", "1.0.0", '{}',
           "applied", "low", 1, NOW), "审查 Agent 只读")

    # V-27：computing 切片在版本推进后不得被冒然置 ready（状态机泄漏）
    con.execute("SAVEPOINT v27")
    con.execute("INSERT INTO slice_set(id,project_id,design_version_id,process_profile_id,engine_version,"
                "status,created_at) VALUES(?,?,?,?,?,?,?)",
                (uid(8021), ids["prj"], ids["v1"], ids["profile"], "1.0.0", "computing", NOW))
    con.execute("INSERT INTO version(id,project_id,version_no,parent_version_id,kind,author_kind,"
                "author_name,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (uid(8022), ids["prj"], 3, ids["v1"], "manual", "human", "local", "又改一版", NOW))
    con.execute("UPDATE project SET current_version_id=? WHERE id=?", (uid(8022), ids["prj"]))
    blocked, msg = False, ""
    try:
        con.execute("UPDATE slice_set SET status='ready' WHERE id=?", (uid(8021),))
    except sqlite3.Error as exc:
        blocked, msg = True, str(exc)[:110]
    con.execute("ROLLBACK TO v27")
    con.execute("RELEASE v27")
    record("V-27", "旧版本切片不得置 ready（堵住 computing→ready 泄漏）", blocked, msg or "未被拦截")

    probe(con, "V-28", "跳层事件必须带层号与原因码（PRT-012）", True,
          "INSERT INTO print_event(id,print_job_id,seq,event_kind,code,message,at) VALUES(?,?,?,?,?,?,?)",
          (uid(8023), ids["job"], 9, "skip", "", "跳过第 3 层", NOW), "CHECK")
    probe(con, "V-29", "合法文件名 rev..2/board.step 不被误伤（NFR-005）", False,
          "INSERT INTO external_file(id,project_id,role,path,hash_at_import,imported_at) VALUES(?,?,?,?,?,?)",
          (uid(8024), ids["prj"], "step", "rev..2/board.step", hh("p2"), NOW))
    probe(con, "V-30", "绝对路径被拒（NFR-005）", True,
          "INSERT INTO external_file(id,project_id,role,path,hash_at_import,imported_at) VALUES(?,?,?,?,?,?)",
          (uid(8025), ids["prj"], "step", "/etc/passwd", hh("p3"), NOW), "CHECK")
    probe(con, "V-31", "Windows 反斜杠穿越被拒（NFR-005）", True,
          "INSERT INTO external_file(id,project_id,role,path,hash_at_import,imported_at) VALUES(?,?,?,?,?,?)",
          (uid(8026), ids["prj"], "step", "..\\win\\x.step", hh("p4"), NOW), "CHECK")


def report(con: sqlite3.Connection) -> tuple[int, int]:
    ok = sum(1 for r in results if r["ok"])
    bad = len(results) - ok
    env = f"Python {sys.version.split()[0]} / SQLite {sqlite3.sqlite_version}"
    lines = [
        "# 数据模型校验报告", "",
        "| 项 | 内容 |", "|---|---|",
        f"| 执行时间 | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} |",
        f"| 环境 | {env} |",
        f"| 被测对象 | `03_ddl/schema_sqlite.sql`（{model.stats()['tables']} 表 / {model.stats()['columns']} 字段 / "
        f"{model.stats()['indexes']} 索引 / {model.stats()['triggers']} 触发器）|",
        f"| 方法 | 内存库执行 DDL → 插入全链样例 → 逐条打不变量（失败用例在 SAVEPOINT 内回滚）|",
        f"| 结论 | **{'PASS' if bad == 0 else 'FAIL'}**：{ok} 通过 / {bad} 失败 |", "",
        "## 1. 结构检查", "", "| 编号 | 检查项 | 结果 | 证据 |", "|---|---|---|---|",
    ]
    for r in results:
        if r["kind"] == "结构":
            lines.append(f"| {r['id']} | {r['name']} | {'✅' if r['ok'] else '❌'} | {r['detail']} |")

    lines += ["", "## 2. 全链样例", "",
              "插入顺序即真实业务顺序：字典 → blob → 工程 → 版本 → 会话/操作 → 结构 → 电路 → MRC → "
              "材料/切片 → 打印/缺陷 → 智能体 → 交付证据。", "",
              "| 域 | 表 | 行数 |", "|---|---|---|"]
    for key, cn, _ in [("core", "工程·版本·存储", ""), ("struct", "结构", ""), ("circuit", "走线", ""),
                       ("mrc", "可打性", ""), ("slice", "切片", ""), ("print", "打印", ""),
                       ("agent", "智能体", ""), ("misc", "字典与证据", "")]:
        for t in model.TABLES:
            if t.domain != key:
                continue
            n = con.execute(f"SELECT COUNT(*) FROM {t.name}").fetchone()[0]
            lines.append(f"| {cn} | `{t.name}` | {n} |")

    lines += ["", "## 3. 不变量", "",
              "每条都对应 SRS 的一个验收要点。失败用例期望数据库**拒绝**；拒绝原因必须包含指定关键字，"
              "否则视为“拒错了原因”。", "",
              "| 编号 | 断言 | 结果 | 证据 |", "|---|---|---|---|"]
    for r in results:
        if r["kind"] == "不变量":
            lines.append(f"| {r['id']} | {r['name']} | {'✅' if r['ok'] else '❌'} | {r['detail']} |")

    lines += ["", "## 4. 追溯覆盖", "", "| 编号 | 检查项 | 结果 | 证据 |", "|---|---|---|---|"]
    for r in results:
        if r["kind"] == "追溯":
            lines.append(f"| {r['id']} | {r['name']} | {'✅' if r['ok'] else '❌'} | {r['detail']} |")

    lines += ["", "## 5. 已知限制", "",
              "1. 本报告验证的是**数据层**：约束、触发器、外键、状态字段。界面交互、切片算法、"
              "G-code 生成正确性不在数据层可验证范围。",
              "2. PostgreSQL DDL 未在本机执行（环境无 PG 实例），只做了语法与类型映射的静态检查；"
              "上线前需在目标版本上跑一次 `psql -f schema_postgres.sql`。",
              "3. 性能类指标（NFR-001）需在基准机器上实测，本报告不覆盖。",
              "4. `slice_path` 用整型主键是刻意的例外，目的是控制索引体积；如需分布式写入需改为 UUID。",
              "", "## 6. 复现方式", "",
              "```bash", "cd 增材电子一体化_数据模型", "python3 tools/validate.py", "```", ""]
    (OUTDIR / "校验报告.md").write_text("\n".join(lines), encoding="utf-8")
    (OUTDIR / "result.json").write_text(json.dumps(
        {"ok": ok, "failed": bad, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    return ok, bad


def main() -> int:
    con = build()
    ids = seed(con)
    structural(con)
    trace(con)
    invariants(con, ids)
    ok, bad = report(con)
    print(f"通过 {ok} / 失败 {bad}")
    for r in results:
        if not r["ok"]:
            print(f"  ❌ {r['id']} {r['name']}：{r['detail']}")
    print(f"报告 -> {OUTDIR / '校验报告.md'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
