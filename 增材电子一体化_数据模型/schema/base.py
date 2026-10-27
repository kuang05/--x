"""数据模型单一事实源（Single Source of Truth）。

所有交付物（SQLite DDL / PostgreSQL DDL / 数据字典 / Mermaid ER / 追溯矩阵）
都由本目录下的 Python 定义生成，禁止手工改生成物，否则四份文档必然漂移。

类型词汇表（逻辑类型 -> 物理类型）
    text   TEXT          PG: text
    int    INTEGER       PG: integer
    int64  INTEGER       PG: bigint
    real   REAL          PG: double precision
    bool   INTEGER(0/1)  PG: boolean
    json   TEXT + json_valid CHECK   PG: jsonb
    time   TEXT(ISO8601 Z, 毫秒)     PG: timestamptz
    hash   TEXT(64 hex)  PG: char(64)
    blob   BLOB          PG: bytea
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

# 域顺序即 ER 图的分带顺序（自顶向下）
DOMAINS: "list[tuple[str, str, str]]" = [
    # (key, 中文名, 说明)
    ("core", "工程·版本·存储", "工程根、提交版本、会话操作日志、内容寻址存储"),
    ("struct", "结构域", "STEP 导入、网格、板框、特征与布尔建议、诊断"),
    ("circuit", "走线域", "板文件导入、器件、焊盘、网络、走线、跳线、禁布区、连通性"),
    ("mrc", "可打性域", "规则版本、阈值、工具几何、评分运行、违规条目"),
    ("slice", "切片域", "材料与批次、工艺配置、物理层与道次、路径、热历程、G-code"),
    ("print", "打印域", "设备与标定、作业与事件、暂停/续打、故障注入、缺陷与归因"),
    ("agent", "智能体域", "计划、步骤、工具调用、能力注册表、对比卡片、重放"),
    ("misc", "字典与交付证据", "受控字典、验收任务结果、工程包清单"),
]

UUID_LEN = 36
HASH_LEN = 64
ISO_CHECK = "IS strftime('%Y-%m-%dT%H:%M:%fZ', {col})"


@dataclass
class Col:
    """一个字段。"""

    name: str
    type: str
    cn: str
    nn: bool = False                 # NOT NULL
    pk: bool = False                 # 单列主键
    uq: bool = False                 # 单列唯一
    default: Optional[str] = None    # 字面量默认值（已带引号或数字）
    check: Optional[str] = None      # 额外 CHECK 表达式
    fk: Optional[str] = None         # "table.column"
    on_delete: str = "RESTRICT"      # CASCADE / SET NULL / RESTRICT
    note: str = ""

    @property
    def fk_table(self) -> Optional[str]:
        return self.fk.split(".")[0] if self.fk else None

    @property
    def fk_col(self) -> Optional[str]:
        return self.fk.split(".")[1] if self.fk else None


@dataclass
class Index:
    cols: Sequence[str]
    name: str = ""
    where: str = ""          # 部分索引条件
    note: str = ""
    unique: bool = False


@dataclass
class Table:
    name: str
    cn: str
    domain: str
    desc: str
    req: List[str]                    # 依据的需求编号
    cols: List[Col]
    pk: Optional[List[str]] = None    # 复合主键
    idx: List[Index] = field(default_factory=list)
    chk: List[Tuple[str, str]] = field(default_factory=list)   # (表达式, 中文说明)
    trg: List[Tuple[str, str]] = field(default_factory=list)   # (名称, 完整 SQL)
    note: str = ""                    # 建模说明/取舍

    # ---- 便捷访问 ----
    def col(self, name: str) -> Col:
        for c in self.cols:
            if c.name == name:
                return c
        raise KeyError(f"{self.name}.{name}")

    @property
    def pk_cols(self) -> List[str]:
        if self.pk:
            return list(self.pk)
        return [c.name for c in self.cols if c.pk]

    def fks(self) -> List[Col]:
        return [c for c in self.cols if c.fk]

    def all_checks(self) -> List[Tuple[str, str]]:
        """单列约束 + 表级约束，统一成 (表达式, 说明) 便于文档化。"""
        out: List[Tuple[str, str]] = []
        for c in self.cols:
            out.extend(column_checks(c))
        out.extend(self.chk)
        return out


# --------------------------------------------------------------------------
# 构造辅助
# --------------------------------------------------------------------------

def column_checks(c: Col) -> List[Tuple[str, str]]:
    """单个字段自带的约束（DDL 与字典共用同一份逻辑，避免两处漂移）。"""
    out: List[Tuple[str, str]] = []
    if c.type == "bool":
        out.append((f"{c.name} IN (0,1)", f"{c.name} 为布尔值"))
    if c.type == "json":
        out.append((f"json_valid({c.name})", f"{c.name} 必须是合法 JSON"))
    if c.type == "time":
        out.append((f"{c.name} {ISO_CHECK.format(col=c.name)}",
                    f"{c.name} 必须是 UTC ISO-8601（毫秒）"))
    if c.type == "hash":
        out.append((f"length({c.name})={HASH_LEN}", f"{c.name} 为 64 位小写十六进制哈希"))
    if c.name == "id" and c.pk and c.type == "text":
        out.append((f"length(id)={UUID_LEN}", "id 为 36 字符 UUIDv7"))
    if c.check:
        out.append((c.check, c.note or f"{c.name} 取值约束"))
    return out


def C(name: str, type: str, cn: str, *, nn: bool = False, pk: bool = False,
      uq: bool = False, default: Optional[str] = None, check: Optional[str] = None,
      fk: Optional[str] = None, on_delete: str = "RESTRICT", note: str = "") -> Col:
    return Col(name, type, cn, nn=nn, pk=pk, uq=uq, default=default,
               check=check, fk=fk, on_delete=on_delete, note=note)


def ID(cn: str = "主键") -> Col:
    """统一主键：UUIDv7 文本。"""
    return C("id", "text", cn, nn=True, pk=True)


def CREATED(cn: str = "创建时间") -> Col:
    return C("created_at", "time", cn, nn=True)


def UPDATED(cn: str = "最后修改时间") -> Col:
    return C("updated_at", "time", cn, nn=True)


def T(name: str, cn: str, domain: str, desc: str, req: Sequence[str],
      cols: Sequence[Col], *, pk: Optional[Sequence[str]] = None,
      idx: Sequence[Index] = (), chk: Sequence[Tuple[str, str]] = (),
      trg: Sequence[Tuple[str, str]] = (), note: str = "") -> Table:
    return Table(name=name, cn=cn, domain=domain, desc=desc, req=list(req),
                 cols=list(cols), pk=list(pk) if pk else None,
                 idx=list(idx), chk=list(chk), trg=list(trg), note=note)


# --------------------------------------------------------------------------
# 模型自检：任何不一致都在生成前炸掉，不允许产出半对的 DDL
# --------------------------------------------------------------------------

def validate(tables: Sequence[Table]) -> List[str]:
    problems: List[str] = []
    names = [t.name for t in tables]
    if len(names) != len(set(names)):
        dup = {n for n in names if names.count(n) > 1}
        problems.append(f"表名重复: {sorted(dup)}")
    index_names: List[str] = []
    for t in tables:
        if t.domain not in {d[0] for d in DOMAINS}:
            problems.append(f"{t.name}: 未知域 {t.domain}")
        if not t.req:
            problems.append(f"{t.name}: 未关联任何需求编号")
        colnames = [c.name for c in t.cols]
        if len(colnames) != len(set(colnames)):
            problems.append(f"{t.name}: 字段名重复")
        if not t.pk_cols:
            problems.append(f"{t.name}: 没有主键")
        for p in t.pk_cols:
            if p not in colnames:
                problems.append(f"{t.name}: 主键列 {p} 不存在")
        for c in t.cols:
            if c.fk:
                tgt, tcol = c.fk.split(".")
                tt = next((x for x in tables if x.name == tgt), None)
                if tt is None:
                    problems.append(f"{t.name}.{c.name}: 外键目标表 {tgt} 不存在")
                elif tcol not in [x.name for x in tt.cols]:
                    problems.append(f"{t.name}.{c.name}: 外键目标列 {c.fk} 不存在")
        for i in t.idx:
            for c in i.cols:
                if c not in colnames:
                    problems.append(f"{t.name}: 索引 {i.cols} 引用了不存在的列 {c}")
            index_names.append(i.name or f"idx_{t.name}_{'_'.join(i.cols)}")
    dup_idx = {n for n in index_names if index_names.count(n) > 1}
    if dup_idx:
        problems.append(f"索引重名: {sorted(dup_idx)}")
    return problems
