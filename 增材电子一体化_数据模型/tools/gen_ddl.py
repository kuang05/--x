#!/usr/bin/env python3
"""由 schema/ 生成两份可执行 DDL：SQLite（目标运行时）与 PostgreSQL（服务端/团队协作）。

用法：python3 tools/gen_ddl.py
输出：03_ddl/schema_sqlite.sql、03_ddl/schema_postgres.sql
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from schema import model                                    # noqa: E402
from schema.base import DOMAINS, Col, Table, column_checks  # noqa: E402

OUT = ROOT / "03_ddl"
STAMP = "2026-10-26"

SQLITE_TYPE = {"text": "TEXT", "int": "INTEGER", "int64": "INTEGER", "real": "REAL",
               "bool": "INTEGER", "json": "TEXT", "time": "TEXT", "hash": "TEXT", "blob": "BLOB"}
PG_TYPE = {"text": "text", "int": "integer", "int64": "bigint", "real": "double precision",
           "bool": "boolean", "json": "jsonb", "time": "timestamptz", "hash": "char(64)",
           "blob": "bytea"}

# 追加式/不可变表的 UPDATE 触发器说明
FORBID_MSG = {
    "trg_version_no_update": "DAT-002: version 表不可篡改（禁止 UPDATE）",
    "trg_version_no_delete": "DAT-002: version 表不可删除（历史只读）",
    "trg_version_item_no_update": "DAT-002: 版本变更项不可修改，只能随版本追加",
    "trg_version_item_no_delete": "DAT-002: 版本变更项不可删除（历史只读）",
    "trg_operation_append_only": "PRJ-005: 操作日志只追加，不允许改写",
    "trg_operation_no_delete": "PRJ-005: 操作日志不可删除（撤销靠追加，不靠删行）",
    "trg_mrc_run_append_only": "MRC-011: 评分结果只追加，旧分数不得被覆盖",
    "trg_mrc_run_no_delete": "MRC-011: 评分运行不可删除（会级联删掉违规记录）",
    "trg_issue_no_update": "MRC-011: 违规条目不可改写，重新评分应追加新运行",
    "trg_issue_no_delete": "MRC-011: 违规条目不可删除（旧结果必须可解释）",
    "trg_gcode_append_only": "SLC-008: 导出记录只追加，哈希不得被改写",
    "trg_gcode_no_delete": "SLC-008: 导出记录不可删除（哈希是追溯证据）",
}


# --------------------------------------------------------------------------
# 循环依赖：找出必须延后到 ALTER TABLE 的外键（PG 不支持前向引用）
# --------------------------------------------------------------------------

def deferred_fks(tables: list[Table]) -> set[tuple[str, str]]:
    tm = {t.name: t for t in tables}
    state: dict[str, int] = {}
    deferred: set[tuple[str, str]] = set()

    def dfs(name: str) -> None:
        state[name] = 1
        for c in tm[name].fks():
            tgt = c.fk_table
            if tgt == name or tgt not in tm:
                continue
            if state.get(tgt, 0) == 1:
                deferred.add((name, c.name))
            elif state.get(tgt, 0) == 0:
                dfs(tgt)
        state[name] = 2

    for t in tables:
        if state.get(t.name, 0) == 0:
            dfs(t.name)
    return deferred


def topo_order(tables: list[Table], deferred: set[tuple[str, str]]) -> list[Table]:
    tm = {t.name: t for t in tables}
    seen: dict[str, int] = {}
    order: list[Table] = []

    def visit(name: str) -> None:
        if seen.get(name) == 2:
            return
        if seen.get(name) == 1:      # 兜底：仍有环则按声明顺序切断
            return
        seen[name] = 1
        for c in tm[name].fks():
            if (name, c.name) in deferred:
                continue
            if c.fk_table in tm and c.fk_table != name:
                visit(c.fk_table)
        seen[name] = 2
        order.append(tm[name])

    for t in tables:
        visit(t.name)
    return order


# --------------------------------------------------------------------------
# 列定义
# --------------------------------------------------------------------------

def _glob_to_pg(expr: str) -> str:
    """SQLite GLOB 转 PG 正则（只覆盖本模型用到的形态）。"""
    if "GLOB 'T[0-9][0-9]'" in expr:
        return expr.replace("task_no GLOB 'T[0-9][0-9]'", "task_no ~ '^T[0-9]{2}$'")
    return expr


def _pg_check(expr: str) -> tuple[str, bool]:
    """PG 的 LIKE 默认把反斜杠当转义符，含反斜杠的路径校验不能直接移植。

    返回 (可移植部分, 是否有子句被剔除)。被剔除的语义由应用层实现，
    并在生成物里显式注释，避免“看起来一样、其实更弱”。
    """
    if "\\" not in expr:
        return expr, False
    parts = [p.strip() for p in expr.split(" AND ")]
    keep = [p for p in parts if "\\" not in p]
    dropped = len(keep) != len(parts)
    return (" AND ".join(keep) if keep else "1=1"), dropped


def col_sql(c: Col, dialect: str, *, inline_pk: bool, defer: bool = False) -> list[str]:
    tmap = SQLITE_TYPE if dialect == "sqlite" else PG_TYPE
    parts = [c.name, tmap[c.type]]
    if inline_pk and c.pk:
        parts.append("PRIMARY KEY")
    if c.uq:
        parts.append("UNIQUE")
    if c.nn:
        parts.append("NOT NULL")
    if c.default is not None:
        d = c.default
        if dialect == "pg" and c.type == "bool" and d in ("0", "1"):
            d = "false" if d == "0" else "true"
        parts.append(f"DEFAULT {d}")
    for expr, _ in column_checks(c):
        if dialect == "pg":
            if c.type in ("bool", "json", "time"):
                continue
            expr = _glob_to_pg(expr)
        parts.append(f"CHECK ({expr})")
    if c.fk and not defer:
        parts.append(f"REFERENCES {c.fk_table}({c.fk_col}) ON DELETE {c.on_delete}")
    return [" ".join(parts)]


def table_sql(t: Table, dialect: str, deferred: set[tuple[str, str]]) -> str:
    lines: list[str] = []
    lines.append("-- " + "=" * 74)
    lines.append(f"-- 表 {t.name}｜{t.cn}")
    lines.append(f"-- {t.desc}")
    lines.append(f"-- 依据需求：{', '.join(t.req)}")
    if t.note:
        lines.append(f"-- 建模说明：{t.note}")
    lines.append("-- " + "=" * 74)
    lines.append(f"CREATE TABLE {t.name} (")
    body: list[str] = []
    inline_pk = len(t.pk_cols) == 1
    for c in t.cols:
        body.append(f"    -- {c.cn}" + (f"（{c.note}）" if c.note else ""))
        for frag in col_sql(c, dialect, inline_pk=inline_pk,
                            defer=((t.name, c.name) in deferred and dialect == "pg")):
            body.append(f"    {frag},")
    if not inline_pk:
        body.append(f"    PRIMARY KEY ({', '.join(t.pk_cols)}),")
    for expr, cn in t.chk:
        if dialect == "pg":
            e, dropped = _pg_check(_glob_to_pg(expr))
            if dropped:
                body.append("    -- PG 注意：含反斜杠的路径校验在应用层执行（LIKE 默认转义差异）")
        else:
            e = expr
        body.append(f"    -- {cn}")
        body.append(f"    CHECK ({e}),")
    body[-1] = body[-1].rstrip(",")
    lines.extend(body)
    lines.append(");")
    return "\n".join(lines)


def index_sql(t: Table, dialect: str) -> list[str]:
    out = []
    for i in t.idx:
        name = i.name or f"idx_{t.name}_{'_'.join(i.cols)}"
        uniq = "UNIQUE " if i.unique else ""
        where = f" WHERE {i.where}" if i.where else ""
        if dialect == "pg" and i.where:
            # PG 的布尔列不能与 1 比较；部分索引条件直接写成裸列
            cond = _glob_to_pg(i.where)
            for col in ("is_active", "is_dangerous", "is_pinned", "degraded", "dry_run",
                        "passed", "matched", "flattened", "locked", "plated", "recovered",
                        "enabled", "is_default", "is_valid", "is_within_tolerance", "is_power"):
                cond = cond.replace(f"{col}=1", col).replace(f"{col} = 1", col)
            where = f" WHERE {cond}"
        out.append(f"-- {i.note or '索引'}")
        out.append(f"CREATE {uniq}INDEX {name} ON {t.name} ({', '.join(i.cols)}){where};")
    return out


def pg_trigger_sql(t: Table) -> list[str]:
    out: list[str] = []
    for name, _raw in t.trg:
        if name in FORBID_MSG:
            msg = FORBID_MSG[name].replace("'", "''")
            event = "UPDATE OR DELETE" if name.endswith("_delete") else "UPDATE"
            if name.endswith("_delete"):
                event = "DELETE"
            out.append(
                f"CREATE TRIGGER {name} BEFORE {event} ON {t.name} FOR EACH ROW "
                f"EXECUTE FUNCTION forbid_mutation('{msg}');")
        elif name == "trg_project_touch":
            out.append(
                "CREATE TRIGGER trg_project_touch BEFORE UPDATE ON project FOR EACH ROW "
                "EXECUTE FUNCTION touch_updated_at();")
        elif name == "trg_issue_run_consistency":
            out.append("""
CREATE OR REPLACE FUNCTION issue_run_consistency() RETURNS trigger AS $$
DECLARE v text; r text;
BEGIN
  SELECT version_id, rule_version_id INTO v, r FROM mrc_run WHERE id = NEW.run_id;
  IF NEW.version_id <> v OR NEW.rule_version_id <> r THEN
    RAISE EXCEPTION 'MRC-008: 违规条目的 version/rule_version 必须与所属运行一致';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_issue_run_consistency BEFORE INSERT ON mrc_issue
FOR EACH ROW EXECUTE FUNCTION issue_run_consistency();""")
        elif name == "trg_slice_stale_on_version":
            out.append("""
CREATE OR REPLACE FUNCTION slice_stale_on_version() RETURNS trigger AS $$
BEGIN
  UPDATE slice_set SET status = 'stale', stale_since = now()
   WHERE project_id = NEW.project_id AND status = 'ready'
     AND design_version_id <> NEW.id;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_slice_stale_on_version AFTER INSERT ON version
FOR EACH ROW EXECUTE FUNCTION slice_stale_on_version();""")
        elif name == "trg_defect_binding":
            out.append("""
CREATE OR REPLACE FUNCTION defect_binding() RETURNS trigger AS $$
DECLARE v text;
BEGIN
  SELECT design_version_id INTO v FROM slice_set WHERE id = NEW.slice_set_id;
  IF NEW.design_version_id <> v THEN
    RAISE EXCEPTION 'PRT-009: 缺陷的设计版本必须与所用切片绑定的版本一致';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_defect_binding BEFORE INSERT ON defect
FOR EACH ROW EXECUTE FUNCTION defect_binding();""")
        elif name == "trg_slice_ready_guard":
            out.append("""
CREATE OR REPLACE FUNCTION slice_ready_guard() RETURNS trigger AS $$
DECLARE cur text;
BEGIN
  SELECT current_version_id INTO cur FROM project WHERE id = NEW.project_id;
  IF NEW.status = 'ready' AND NEW.design_version_id <> cur THEN
    RAISE EXCEPTION 'SLC-010: 绑定版本已不是当前版本，不得置为 ready';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_slice_ready_guard BEFORE UPDATE OF status ON slice_set
FOR EACH ROW EXECUTE FUNCTION slice_ready_guard();""")
        elif name in ("trg_review_step_readonly_ins", "trg_review_step_readonly_upd"):
            event = "INSERT" if name.endswith("ins") else "UPDATE"
            out.append(f"""
CREATE OR REPLACE FUNCTION review_step_readonly() RETURNS trigger AS $$
DECLARE role text;
BEGIN
  SELECT agent_role INTO role FROM plan WHERE id = NEW.plan_id;
  IF NEW.status = 'applied' AND role = 'review' THEN
    RAISE EXCEPTION 'AGT-002: 审查 Agent 只读，不得执行步骤';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER {name} BEFORE {event} ON plan_step
FOR EACH ROW EXECUTE FUNCTION review_step_readonly();""")
        elif name in ("trg_review_card_readonly_ins", "trg_review_card_readonly_upd"):
            event = "INSERT" if name.endswith("ins") else "UPDATE"
            out.append(f"""
CREATE OR REPLACE FUNCTION review_card_readonly() RETURNS trigger AS $$
DECLARE role text;
BEGIN
  SELECT agent_role INTO role FROM plan WHERE id = NEW.plan_id;
  IF NEW.decision IN ('apply','apply_one') AND role = 'review' THEN
    RAISE EXCEPTION 'AGT-002: 审查 Agent 只读，不得应用修改';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER {name} BEFORE {event} ON diff_card
FOR EACH ROW EXECUTE FUNCTION review_card_readonly();""")
    return out


HEADER_SQLITE = f"""-- ============================================================================
-- 增材电子一体化软件 · 数据模型 DDL（SQLite 3.35+ / 实测 3.46.1）
-- 由 tools/gen_ddl.py 从 schema/ 自动生成，请勿手改。
-- 生成日期：{STAMP}
-- 表数：{model.stats()['tables']}｜字段：{model.stats()['columns']}｜索引：{model.stats()['indexes']}
--           外键：{model.stats()['fks']}｜CHECK：{model.stats()['checks']}｜触发器：{model.stats()['triggers']}
--
-- 使用方式（每次连接都必须执行，SQLite 的外键开关是连接级的）：
--   PRAGMA foreign_keys = ON;
--   PRAGMA journal_mode = WAL;
--   PRAGMA synchronous = FULL;      -- NFR-002 保存原子性
--   PRAGMA busy_timeout = 5000;
-- ============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA encoding = 'UTF-8';
PRAGMA user_version = 1;

"""

HEADER_PG = f"""-- ============================================================================
-- 增材电子一体化软件 · 数据模型 DDL（PostgreSQL 14+）
-- 由 tools/gen_ddl.py 从 schema/ 自动生成，请勿手改。
-- 生成日期：{STAMP}
-- 说明：时间戳用 timestamptz（写入方必须传 UTC）；JSON 用 jsonb；
--       循环外键（project↔version）用 ALTER TABLE 追加，其余内联。
-- ============================================================================

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '%', TG_ARGV[0];
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

"""


def main() -> None:
    tables = model.TABLES
    deferred = deferred_fks(tables)

    # ---------------- SQLite ----------------
    parts = [HEADER_SQLITE]
    for t in tables:
        parts.append(table_sql(t, "sqlite", deferred))
        parts.append("")
    parts.append("-- ---------------------------- 索引 ----------------------------")
    for t in tables:
        idx = index_sql(t, "sqlite")
        if idx:
            parts.append("\n".join(idx))
            parts.append("")
    parts.append("-- --------------------------- 触发器 ---------------------------")
    for t in tables:
        for name, sql in t.trg:
            parts.append(f"-- {name}")
            parts.append(sql.strip())
            parts.append("")
    parts.append("-- 视图：某工程的质量趋势（问题列表页直接用，避免应用层重复写 SQL）")
    parts.append("""CREATE VIEW v_project_score_trend AS
SELECT r.project_id,
       r.version_id,
       v.version_no,
       r.rule_version_id,
       r.total_score,
       r.issue_count,
       r.started_at
  FROM mrc_run r
  JOIN version v ON v.id = r.version_id;""")
    parts.append("")
    (OUT / "schema_sqlite.sql").write_text("\n".join(parts), encoding="utf-8")

    # ---------------- PostgreSQL ----------------
    parts = [HEADER_PG]
    for t in topo_order(tables, deferred):
        parts.append(table_sql(t, "pg", deferred))
        parts.append("")
    for t in tables:
        for c in t.fks():
            if (t.name, c.name) in deferred:
                parts.append(
                    f"ALTER TABLE {t.name} ADD CONSTRAINT fk_{t.name}_{c.name} "
                    f"FOREIGN KEY ({c.name}) REFERENCES {c.fk_table}({c.fk_col}) "
                    f"ON DELETE {c.on_delete};")
    parts.append("")
    parts.append("-- ---------------------------- 索引 ----------------------------")
    for t in tables:
        idx = index_sql(t, "pg")
        if idx:
            parts.append("\n".join(idx))
            parts.append("")
    parts.append("-- --------------------------- 触发器 ---------------------------")
    for t in tables:
        for line in pg_trigger_sql(t):
            parts.append(line)
            parts.append("")
    (OUT / "schema_postgres.sql").write_text("\n".join(parts), encoding="utf-8")

    print(f"SQLite  -> {(OUT / 'schema_sqlite.sql')}")
    print(f"Postgres-> {(OUT / 'schema_postgres.sql')}")
    print(f"表 {model.stats()['tables']} 张，字段 {model.stats()['columns']} 个")
    print(f"循环外键（PG 走 ALTER）：{sorted(deferred)}")


if __name__ == "__main__":
    main()
