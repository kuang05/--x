"""模型聚合入口：把各域的 Table 汇总成一份有序、自检过的模型。"""
from __future__ import annotations

from typing import Dict, List

from . import agent, circuit, core, misc, mrc
from . import print as print_dom
from . import slice as slice_dom
from . import struct
from .base import DOMAINS, Table, validate

MODULES = [core, struct, circuit, mrc, slice_dom, print_dom, agent, misc]

TABLES: List[Table] = [t for m in MODULES for t in m.TABLES]

PROBLEMS = validate(TABLES)

DOMAIN_ORDER = [d[0] for d in DOMAINS]
DOMAIN_CN = {d[0]: d[1] for d in DOMAINS}
DOMAIN_DESC = {d[0]: d[2] for d in DOMAINS}


def table_map() -> Dict[str, Table]:
    return {t.name: t for t in TABLES}


def by_domain() -> Dict[str, List[Table]]:
    out: Dict[str, List[Table]] = {d: [] for d in DOMAIN_ORDER}
    for t in TABLES:
        out[t.domain].append(t)
    return out


def stats() -> Dict[str, int]:
    cols = sum(len(t.cols) for t in TABLES)
    idx = sum(len(t.idx) for t in TABLES)
    fks = sum(len(t.fks()) for t in TABLES)
    chks = sum(len(t.all_checks()) for t in TABLES)
    trg = sum(len(t.trg) for t in TABLES)
    return {"tables": len(TABLES), "columns": cols, "indexes": idx,
            "fks": fks, "checks": chks, "triggers": trg}


def requirement_index() -> Dict[str, List[str]]:
    """需求编号 -> 涉及的表名（按模型声明顺序）。"""
    out: Dict[str, List[str]] = {}
    for t in TABLES:
        for r in t.req:
            out.setdefault(r, []).append(t.name)
    return out


if PROBLEMS:  # 模型自检不通过就不允许生成任何交付物
    raise SystemExit("模型自检失败：\n" + "\n".join(PROBLEMS))
