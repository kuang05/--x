"""环境自检：交付前一条命令确认依赖、数据库、可选组件是否就绪。

用法：python3 -m aepcb.cli doctor
返回：检查项列表，任一「必需项」失败则退出码 1。
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
import tempfile
from pathlib import Path

REQUIRED = {"python", "numpy", "scipy", "PIL", "cv2", "sklearn", "sqlite", "schema", "workspace"}
EXPECTED_TABLES = 59


def _version(mod) -> str:
    return getattr(mod, "__version__", "?")


def run(workspace: Path | None = None) -> list[dict]:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, required: bool = True, hint: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail,
                       "required": required, "hint": hint})

    py = sys.version_info
    add("python", py >= (3, 10), f"{py.major}.{py.minor}.{py.micro}", hint="需要 Python ≥ 3.10")

    for mod_name, pkg in (("numpy", "numpy"), ("scipy", "scipy"), ("PIL", "pillow"),
                          ("cv2", "opencv-python-headless"), ("sklearn", "scikit-learn")):
        try:
            mod = importlib.import_module(mod_name)
            add(mod_name, True, _version(mod))
        except Exception:
            add(mod_name, False, "未安装", hint=f"pip install {pkg}")

    add("sqlite", sqlite3.sqlite_version_info >= (3, 35, 0), sqlite3.sqlite_version,
        hint="需要 SQLite ≥ 3.35（部分索引/JSON）")

    schema = Path(__file__).resolve().parents[1] / "schema.sql"
    tables = 0
    if schema.exists():
        try:
            con = sqlite3.connect(":memory:")
            con.executescript(schema.read_text(encoding="utf-8"))
            tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                                 "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
            con.close()
        except Exception as exc:
            add("schema", False, f"{type(exc).__name__}: {exc}", hint="schema.sql 无法建库")
            tables = -1
        if tables >= 0:
            add("schema", tables == EXPECTED_TABLES, f"{tables} 张表", hint=f"表数应为 {EXPECTED_TABLES}")
    else:
        add("schema", False, "schema.sql 缺失", hint="从数据模型交付物复制")

    ws = workspace or Path(__file__).resolve().parents[1] / "workspace"
    try:
        ws.mkdir(parents=True, exist_ok=True)
        probe = ws / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("workspace", True, str(ws))
    except Exception as exc:
        add("workspace", False, f"{type(exc).__name__}: {exc}", hint="工作目录不可写")

    try:
        import pgserver  # noqa: F401
        add("pgserver", True, "已安装（可选，用于 PG 验证）", required=False)
    except Exception:
        add("pgserver", False, "未安装（可选）", required=False, hint="pip install pgserver")

    try:
        import serial  # noqa: F401
        add("pyserial", True, "已安装（可选，用于真机）", required=False)
    except Exception:
        add("pyserial", False, "未安装（可选）", required=False, hint="pip install pyserial")

    return checks


def summary(checks: list[dict]) -> tuple[int, int, int]:
    ok = sum(1 for c in checks if c["ok"])
    failed_required = sum(1 for c in checks if not c["ok"] and c["name"] in REQUIRED)
    return ok, len(checks), failed_required
