"""固定夹具 T19-A：8 器件 / 12 网络 / 24 焊盘 / 12 条走线。

网络由走线端点决定（每条走线连两个焊盘，二者同网），避免"网络名与几何不一致"。
设计意图（对应 7.1 T19 与 MRC 规则）：
    3 条 0.4 mm 细线        → R1 ×3（major，各 8 分）
    1 对平行线间距 0.3 mm   → R2 ×1（major 8 分）+ R7 ×1（minor 3 分）
    1 处靠门侧走线          → R6 ×1（minor 3 分，只报最不利点）
合计 24 + 8 + 3 + 3 = 38 → 初始分 62。
"""
from __future__ import annotations

BOARD_W, BOARD_H = 60.0, 40.0

COMPS = [
    ("U1", 8, 20), ("U2", 20, 20), ("U3", 32, 20), ("U4", 44, 20),
    ("R1", 8, 34), ("R2", 20, 34), ("R3", 32, 34), ("R4", 44, 34),
]

# 走线：net → (from 焊盘, 中间折点, to 焊盘, 宽度, 层)
ROUTES = [
    ("N01", ("U1", "1"), [(6, 12), (18, 12)], ("U2", "1"), 0.4, 0),
    ("N02", ("U1", "2"), [(10, 14), (22, 14)], ("U2", "2"), 0.6, 1),
    ("N03", ("U3", "1"), [(30, 12), (42, 12)], ("U4", "1"), 0.6, 0),
    ("N04", ("U3", "2"), [(34, 16), (46, 16)], ("U4", "2"), 0.6, 1),
    ("N05", ("R1", "1"), [(6, 28), (42, 28)], ("R4", "1"), 0.6, 0),
    ("N06", ("R2", "1"), [(18, 28.9), (30, 28.9)], ("R3", "1"), 0.6, 0),
    ("N07", ("R1", "2"), [(10, 38), (22, 38)], ("R2", "2"), 0.6, 0),
    ("N08", ("R3", "2"), [(34, 38), (46, 38)], ("R4", "2"), 0.6, 0),
    ("N09", ("U1", "3"), [(8, 32)], ("R1", "3"), 0.4, 1),
    ("N10", ("U2", "3"), [(20, 32)], ("R2", "3"), 0.4, 1),
    ("N11", ("U3", "3"), [(32, 32)], ("R3", "3"), 0.6, 1),
    ("N12", ("U4", "3"), [(44, 32)], ("R4", "3"), 0.6, 1),
]


def _pad_xy(ref: str, no: str) -> tuple[float, float]:
    for r, cx, cy in COMPS:
        if r != ref:
            continue
        dy = 2 if cy < 27 else -2
        return {"1": (cx - 2, cy), "2": (cx + 2, cy), "3": (cx, cy + dy)}[no]
    raise KeyError(f"{ref}.{no}")


def t19a() -> dict:
    components = [{"ref": r, "x": cx, "y": cy, "rot": 0, "side": "top",
                   "footprint": "QFN-16", "h": 1.2} for r, cx, cy in COMPS]

    pads, net_of_pad = [], {}
    for r, cx, cy in COMPS:
        dy = 2 if cy < 27 else -2
        for no, (px, py) in {"1": (cx - 2, cy), "2": (cx + 2, cy), "3": (cx, cy + dy)}.items():
            pads.append({"comp": r, "no": no, "x": px, "y": py, "net": None})

    routes = []
    for net, a, mid, b, width, layer in ROUTES:
        pa, pb = _pad_xy(*a), _pad_xy(*b)
        pts = [pa] + [(float(x), float(y)) for x, y in mid] + [pb]
        routes.append({"net": net, "layer": layer, "width": width, "points": pts})
        net_of_pad[f"{a[0]}.{a[1]}"] = net
        net_of_pad[f"{b[0]}.{b[1]}"] = net

    for p in pads:
        p["net"] = net_of_pad[f"{p['comp']}.{p['no']}"]

    nets = [{"name": net, "class": "default", "power": net in ("N01", "N05")}
            for net, *_ in ROUTES]
    return {
        "board": {"name": "T19-A", "w": BOARD_W, "h": BOARD_H,
                  "min_width": 0.5, "min_spacing": 0.5},
        "components": components,
        "pads": pads,
        "nets": nets,
        "routes": routes,
        "keepouts": [{"points": [[0, 0], [60, 0], [60, 2], [0, 2]], "layer": "all",
                      "reason": "板边禁布"}],
    }
