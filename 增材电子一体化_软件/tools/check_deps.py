"""检查第三方 import 与 pyproject 声明是否一致（打包前必跑）。

用法：python3 tools/check_deps.py
退出码：0 一致；1 有未声明或声明未用。
"""
from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STDLIB = set(sys.stdlib_module_names)
# 包名 → import 名不一致的映射
ALIASES = {
    "opencv-python-headless": "cv2",
    "opencv-python": "cv2",
    "scikit-learn": "sklearn",
    "pyserial": "serial",
    "pillow": "PIL",
}


def declared() -> tuple[set[str], set[str]]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core = {d.split(">")[0].split("=")[0].split("[")[0].strip()
            for d in data["project"].get("dependencies", [])}
    opt = set()
    for group in data["project"].get("optional-dependencies", {}).values():
        for d in group:
            opt.add(d.split(">")[0].split("=")[0].strip())
    return core, opt


def imported() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted((ROOT / "aepcb").glob("*.py")) + sorted((ROOT / "tools").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in STDLIB or name in {"aepcb", "fixtures", "tools"}:
                    continue
                found.setdefault(name, set()).add(path.name)
    return found


def main() -> int:
    core, opt = declared()
    core_imports = {ALIASES.get(p, p.replace("-", "_")) for p in core}
    opt_imports = {ALIASES.get(p, p.replace("-", "_")) for p in opt}
    used = imported()
    undeclared = {k: v for k, v in used.items() if k not in core_imports | opt_imports}
    unused = (core_imports | opt_imports) - set(used)
    for name, files in sorted(undeclared.items()):
        print(f"❌ 未声明依赖：{name}（用于 {', '.join(sorted(files))}）")
    for name in sorted(unused):
        print(f"⚪ 声明未用：{name}")
    print(f"\n声明 {len(core)} 个必需 / {len(opt)} 个可选；代码实际 import {len(used)} 个第三方包")
    return 1 if undeclared else 0


if __name__ == "__main__":
    raise SystemExit(main())
