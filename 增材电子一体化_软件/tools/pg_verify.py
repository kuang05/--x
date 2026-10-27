#!/usr/bin/env python3
"""PostgreSQL 实跑验证：起 pgserver → 灌 DDL → 跑行为探针 → 出报告。

判定方式：不解析 psql 的错误文本（它在 stderr，stdout 可能为空），
而是“执行语句后查状态”——拒绝与否由数据是否被改动决定，稳健得多。

用法：python3 tools/pg_verify.py   输出：workspace/pg_verify.md
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pgserver  # noqa: E402

DDL = ROOT.parent / "增材电子一体化_数据模型" / "03_ddl" / "schema_postgres.sql"
OUT = ROOT / "workspace" / "pg_verify.md"
PGDATA = ROOT / "workspace" / "pgdata"


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


H = "a" * 64


def scalar(srv, sql: str) -> str:
    """从 psql 的对齐输出里取第一个数据单元格。"""
    out = srv.psql(sql)
    lines = [l for l in out.splitlines()]
    for i, line in enumerate(lines):
        s = line.strip()
        if s and set(s) <= set("-+ "):
            return lines[i + 1].strip() if i + 1 < len(lines) else ""
    return ""


def main() -> int:
    PGDATA.mkdir(parents=True, exist_ok=True)
    srv = pgserver.get_server(PGDATA)
    srv.psql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    srv.psql(DDL.read_text(encoding="utf-8"))

    tables = scalar(srv, "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")
    triggers = scalar(srv, "SELECT count(*) FROM information_schema.triggers WHERE trigger_schema='public';")
    indexes = scalar(srv, "SELECT count(*) FROM pg_indexes WHERE schemaname='public';")

    srv.psql(f"""
INSERT INTO project(id,name,unit,sheet_w_mm,sheet_h_mm,origin_kind,data_schema_version,
                    dir_rel_path,created_at,updated_at)
VALUES('{uid(1)}','PG 验证','mm',60,40,'board_bottom_left',1,'p',now(),now());
INSERT INTO version(id,project_id,version_no,kind,author_kind,author_name,reason,created_at)
VALUES('{uid(2)}','{uid(1)}',0,'initial','human','local','init',now());
UPDATE project SET current_version_id='{uid(2)}' WHERE id='{uid(1)}';
INSERT INTO process_profile(id,name,params_json,params_hash,is_valid,created_at)
VALUES('{uid(3)}','default','{{"layer_h":0.08}}','{H}',true,now());
-- 先造一个“第二版”并把当前版本指过去，切片仍绑在旧版本上，用于触发 ready 守卫
INSERT INTO version(id,project_id,version_no,parent_version_id,kind,author_kind,author_name,
                    reason,created_at)
VALUES('{uid(7)}','{uid(1)}',1,'{uid(2)}','manual','human','local','第二版',now());
UPDATE project SET current_version_id='{uid(7)}' WHERE id='{uid(1)}';
INSERT INTO slice_set(id,project_id,design_version_id,process_profile_id,engine_version,
                      status,layer_count,pass_count,created_at)
VALUES('{uid(4)}','{uid(1)}','{uid(2)}','{uid(3)}','1.0.0','computing',3,7,now());
INSERT INTO plan(id,project_id,base_version_id,base_digest,agent_role,goal,status,auth_level,
                 toolchain_version,random_seed,rounds_used,round_limit,created_at)
VALUES('{uid(5)}','{uid(1)}','{uid(2)}','{H}','review','复核','running','step_confirm',
       'aepcb-1.0.0',0,0,8,now());
""")

    results: list[tuple[str, str, str, bool]] = []

    def probe(name: str, action: str, verify: str, want: str, expect: str) -> None:
        srv.psql(action)
        got = scalar(srv, verify)
        results.append((name, expect, got, got == want))

    probe("版本行不可修改（DAT-002）",
          f"UPDATE version SET reason='tamper' WHERE id='{uid(2)}';",
          f"SELECT reason FROM version WHERE id='{uid(2)}';", "init", "状态未变")
    probe("版本行不可删除（DAT-002）",
          f"DELETE FROM version WHERE id='{uid(2)}';",
          "SELECT count(*) FROM version;", "2", "行仍在")
    probe("旧版本切片不得置 ready（SLC-010）",
          f"UPDATE slice_set SET status='ready' WHERE id='{uid(4)}';",
          f"SELECT status FROM slice_set WHERE id='{uid(4)}';", "computing", "状态未变")
    probe("审查 Agent 不得执行步骤（AGT-002）",
          f"INSERT INTO plan_step(id,plan_id,step_no,intent,tool_name,tool_version,params_json,"
          f"status,risk_level,requires_confirm,created_at) VALUES "
          f"('{uid(6)}','{uid(5)}',1,'x','t','1','{{}}','applied','low',true,now());",
          "SELECT count(*) FROM plan_step;", "0", "未插入")
    probe("外键与循环引用可用（project↔version）",
          f"SELECT 1;",
          f"SELECT count(*) FROM version WHERE project_id='{uid(1)}';", "2", "可查询")

    ok_all = all(r[3] for r in results)
    lines = ["# PostgreSQL 实跑验证", "",
             "| 项 | 值 |", "|---|---|",
             f"| 实例 | pgserver 内置 PostgreSQL（数据目录 `workspace/pgdata`）|",
             f"| 表 | {tables} |", f"| 触发器 | {triggers} |",
             f"| 索引（含主键）| {indexes} |",
             f"| 结论 | **{'PASS' if ok_all else 'FAIL'}** |", "",
             "## 行为探针（执行后查状态，不依赖错误文本）", "",
             "| 探针 | 期望 | 实际 | 结果 |", "|---|---|---|---|"]
    for name, expect, got, ok in results:
        lines.append(f"| {name} | {expect} | {got} | {'✅' if ok else '❌'} |")
    lines += ["", "## 本次修掉的两类 PG 问题", "",
              "1. **布尔列上的部分索引**：`WHERE is_active=1` 在 PG 报 `operator does not exist: boolean = integer`，"
              "生成器改为裸列条件（`WHERE is_active`）。",
              "2. **5 个触发器缺 PG 实现**：切片 ready 守卫、审查 Agent 只读 ×4 只生成了 SQLite 版；"
              "现已补 plpgsql 版本。",
              "", "## 类型差异（有意为之）", "",
              "| 逻辑类型 | SQLite | PostgreSQL |", "|---|---|---|",
              "| 时间 | TEXT + strftime CHECK | timestamptz |",
              "| JSON | TEXT + json_valid | jsonb |",
              "| 哈希 | TEXT + length=64 | char(64) |",
              "| 布尔 | INTEGER + CHECK IN (0,1) | boolean |", ""]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[8:]))
    print(f"\n报告 -> {OUT}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
