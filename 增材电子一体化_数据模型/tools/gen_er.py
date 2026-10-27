#!/usr/bin/env python3
"""生成 ER 图交付物。

- 05_图表/er_full.mmd          Mermaid 全量（含字段），可交互渲染
- 05_图表/er_overview.mmd      Mermaid 关系总览（不含字段）
- 05_图表/ER_字段级_v1.0.png   分域字段级图（PIL 绘制，离线可看）
- 05_图表/ER_域总览_v1.0.png   8 域卡片 + 域间关系矩阵

用法：python3 tools/gen_er.py
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont          # noqa: E402

from schema import model                              # noqa: E402
from schema.base import DOMAINS, Table               # noqa: E402

OUT = ROOT / "05_图表"
FONT_CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_CJK_B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

COLORS = {
    "core":    ("#E8EAF6", "#3949AB", "#C5CAE9"),
    "struct":  ("#E3F2FD", "#1565C0", "#BBDEFB"),
    "circuit": ("#E8F5E9", "#2E7D32", "#C8E6C9"),
    "mrc":     ("#FFF3E0", "#EF6C00", "#FFE0B2"),
    "slice":   ("#F3E5F5", "#6A1B9A", "#E1BEE7"),
    "print":   ("#FFEBEE", "#C62828", "#FFCDD2"),
    "agent":   ("#E0F7FA", "#00838F", "#B2EBF2"),
    "misc":    ("#F5F5F5", "#616161", "#E0E0E0"),
}

BOX_W, GAP_X, GAP_Y = 372, 26, 22
HEAD_H, ROW_H, BODY_PAD = 32, 15, 6
PAD, PER_ROW = 44, 7


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_CJK_B if bold else FONT_CJK, size)


F_TITLE = font(30, True)
F_SUB = font(15)
F_BAND = font(17, True)
F_HEAD = font(13, True)
F_NAME = font(10)
F_NAME_B = font(10, True)
F_TYPE = font(9)
F_CN = font(9)
F_LEGEND = font(12)
F_SMALL = font(10)


def box_height(t: Table) -> int:
    return HEAD_H + len(t.cols) * ROW_H + BODY_PAD


def ellipsize(draw: ImageDraw.ImageDraw, text: str, f, max_w: int) -> str:
    if draw.textlength(text, font=f) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return text + "…"


# --------------------------------------------------------------------------
# Mermaid
# --------------------------------------------------------------------------

def mermaid_full() -> str:
    lines = ["erDiagram", "  %% 增材电子一体化软件 · 字段级 ER（由 schema/ 生成，勿手改）", ""]
    for t in model.TABLES:
        lines.append(f"  {t.name} {{")
        for c in t.cols:
            mark = ""
            if c.pk:
                mark = " PK"
            elif c.fk:
                mark = " FK"
            label = c.cn.replace('"', "'")
            lines.append(f"    {c.type} {c.name}{mark} \"{label}\"")
        lines.append("  }")
    lines.append("")
    for t in model.TABLES:
        for c in t.fks():
            card = "||--o|" if t.name == "board_outline" else ("||--o{" if c.uq else "||--o{")
            lines.append(f'  {c.fk_table} {card} {t.name} : "{c.name}"')
    return "\n".join(lines) + "\n"


def mermaid_overview() -> str:
    lines = ["erDiagram", "  %% 关系总览（不含字段）", ""]
    for t in model.TABLES:
        lines.append(f"  {t.name} {{\n    text id PK\n  }}")
    lines.append("")
    for t in model.TABLES:
        for c in t.fks():
            lines.append(f'  {c.fk_table} ||--o{{ {t.name} : "{c.name}"')
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 字段级 PNG
# --------------------------------------------------------------------------

def layout() -> tuple[dict, list, int, int]:
    pos: dict[str, tuple[int, int, int, int]] = {}
    bands = []
    y = 150
    for key, cn, _desc in DOMAINS:
        tables = [t for t in model.TABLES if t.domain == key]
        rows = [tables[i:i + PER_ROW] for i in range(0, len(tables), PER_ROW)]
        band_top = y
        y += 34
        for row in rows:
            h = max(box_height(t) for t in row)
            for i, t in enumerate(row):
                x = PAD + i * (BOX_W + GAP_X)
                pos[t.name] = (x, y, BOX_W, h)
            y += h + GAP_Y
        y += 14
        bands.append((key, cn, band_top, y - band_top))
    width = PAD * 2 + PER_ROW * BOX_W + (PER_ROW - 1) * GAP_X
    return pos, bands, width, y + 30


def draw_edges(d: ImageDraw.ImageDraw, pos: dict) -> None:
    for t in model.TABLES:
        for c in t.fks():
            if c.fk_table not in pos:
                continue
            sx, sy, sw, sh = pos[t.name]
            tx, ty, tw, th = pos[c.fk_table]
            s_cx, s_cy = sx + sw // 2, sy + sh // 2
            t_cx, t_cy = tx + tw // 2, ty + th // 2
            # 从源框最近的水平边出发，竖直到目标中心高度，再水平进入目标框
            if s_cx <= t_cx:
                p0 = (sx + sw, s_cy)
                p2 = (tx, t_cy)
            else:
                p0 = (sx, s_cy)
                p2 = (tx + tw, t_cy)
            p1 = (p0[0], p2[1])
            d.line([p0, p1, p2], fill=(150, 150, 150), width=1)
            d.ellipse([p0[0] - 2, p0[1] - 2, p0[0] + 2, p0[1] + 2], fill=(120, 120, 120))
            d.line([p2[0] - 4, p2[1] - 3, p2[0], p2[1]], fill=(120, 120, 120), width=1)
            d.line([p2[0] - 4, p2[1] + 3, p2[0], p2[1]], fill=(120, 120, 120), width=1)


def draw_box(d: ImageDraw.ImageDraw, t: Table, x: int, y: int, w: int, h: int) -> None:
    bg, fg, soft = COLORS[t.domain]
    d.rounded_rectangle([x, y, x + w, y + h], radius=6, fill="white", outline=soft, width=2)
    d.rounded_rectangle([x, y, x + w, y + HEAD_H], radius=6, fill=fg)
    d.rectangle([x, y + HEAD_H - 6, x + w, y + HEAD_H], fill=fg)
    title = f"{t.name}"
    d.text((x + 8, y + 6), title, font=F_HEAD, fill="white")
    sub = ellipsize(d, t.cn, F_TYPE, w - 12 - d.textlength(title, font=F_HEAD) - 14)
    d.text((x + 12 + d.textlength(title, font=F_HEAD) + 8, y + 9), sub, font=F_TYPE, fill="#FFFFFF")
    ry = y + HEAD_H + 3
    for c in t.cols:
        if c.pk:
            mark, mcol = "PK", "#C62828"
        elif c.fk:
            mark, mcol = "FK", "#1565C0"
        else:
            mark, mcol = "", None
        if mark:
            d.text((x + 6, ry), mark, font=F_TYPE, fill=mcol)
        name_x = x + 28
        d.text((name_x, ry), ellipsize(d, c.name, F_NAME, 118), font=F_NAME_B, fill="#212121")
        d.text((x + 152, ry), ellipsize(d, c.type, F_TYPE, 56), font=F_TYPE, fill="#9E9E9E")
        d.text((x + 212, ry), ellipsize(d, c.cn, F_CN, w - 218), font=F_CN, fill="#5F6368")
        ry += ROW_H


def render_fields() -> Path:
    pos, bands, width, height = layout()
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)

    # 标题
    d.text((PAD, 26), "增材电子一体化软件 · 字段级数据模型 ER 图", font=F_TITLE, fill="#1A237E")
    d.text((PAD, 66), f"54 张表 / 605 个字段 · 由 schema/ 生成于 2026-10-26 · "
                      f"连线为外键（多端指向一端）· 完整约束见《数据模型设计说明书》",
           font=F_SUB, fill="#5F6368")

    # 图例
    lx = PAD
    for key, cn, _ in DOMAINS:
        bg, fg, _soft = COLORS[key]
        d.rounded_rectangle([lx, 100, lx + 16, 116], radius=3, fill=fg)
        d.text((lx + 22, 100), cn, font=F_LEGEND, fill="#3C4043")
        lx += 30 + int(d.textlength(cn, font=F_LEGEND)) + 26
    d.text((lx + 10, 100), "PK 主键 / FK 外键 / 灰色连线 = 外键关系",
           font=F_SMALL, fill="#9E9E9E")

    # 域带背景
    for key, cn, top, h in bands:
        bg, fg, _soft = COLORS[key]
        d.rectangle([20, top, width - 20, top + h - 6], fill=bg)
        d.rectangle([20, top, width - 20, top + 3], fill=fg)
        tables = [t for t in model.TABLES if t.domain == key]
        cols = sum(len(t.cols) for t in tables)
        d.text((30, top + 8), f"{cn}　{len(tables)} 表 / {cols} 字段",
               font=F_BAND, fill=fg)

    # 先画连线，再画框（框会盖住穿过的线）
    draw_edges(d, pos)
    for t in model.TABLES:
        x, y, w, h = pos[t.name]
        draw_box(d, t, x, y, w, h)

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "ER_字段级_v1.0.png"
    img.save(path, optimize=True)
    print(f"PNG -> {path}  {img.size[0]}x{img.size[1]}")
    return path


# --------------------------------------------------------------------------
# 域总览 PNG
# --------------------------------------------------------------------------

def render_overview() -> Path:
    counts: Counter = Counter()
    for t in model.TABLES:
        for c in t.fks():
            if c.fk_table != t.name:
                counts[(t.domain, model.table_map()[c.fk_table].domain)] += 1

    W, H = 1800, 1180
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 34), "数据模型 · 域总览", font=font(32, True), fill="#1A237E")
    d.text((60, 78), "8 个域、54 张表；箭头上的数字表示跨域外键条数",
           font=F_SUB, fill="#5F6368")

    card_w, card_h = 800, 200
    for i, (key, cn, desc) in enumerate(DOMAINS):
        col, row = i % 2, i // 2
        x = 60 + col * (card_w + 60)
        y = 120 + row * (card_h + 30)
        bg, fg, soft = COLORS[key]
        d.rounded_rectangle([x, y, x + card_w, y + card_h], radius=10, fill=bg, outline=fg, width=2)
        d.rectangle([x, y, x + 8, y + card_h], fill=fg)
        tables = [t for t in model.TABLES if t.domain == key]
        d.text((x + 24, y + 14), f"{cn}", font=font(19, True), fill=fg)
        d.text((x + 24 + d.textlength(cn, font=font(19, True)) + 14, y + 20),
               f"{len(tables)} 张表 / {sum(len(t.cols) for t in tables)} 字段",
               font=F_SMALL, fill="#616161")
        names = "、".join(t.name for t in tables)
        # 手工换行
        line, lines = "", []
        for name in tables:
            test = (line + "、" + name.name) if line else name.name
            if d.textlength(test, font=F_SMALL) > card_w - 48:
                lines.append(line)
                line = name.name
            else:
                line = test
        if line:
            lines.append(line)
        for j, ln in enumerate(lines[:6]):
            d.text((x + 24, y + 52 + j * 22), ln, font=F_SMALL, fill="#37474F")

    # 域间关系矩阵
    mx, my = 60, 1010
    d.text((mx, my - 40), "域间外键条数（行 → 列）", font=font(16, True), fill="#37474F")
    cw = 110
    keys = [k for k, _c, _d in DOMAINS]
    for j, k in enumerate(keys):
        d.text((mx + 190 + j * cw, my), k, font=F_SMALL, fill=COLORS[k][1])
    for i, k in enumerate(keys):
        d.text((mx, my + 24 + i * 22), k, font=F_SMALL, fill=COLORS[k][1])
        for j, k2 in enumerate(keys):
            n = counts.get((k, k2), 0)
            if k == k2:
                continue
            if n:
                d.rounded_rectangle([mx + 186 + j * cw, my + 20 + i * 22,
                                     mx + 186 + j * cw + 46, my + 40 + i * 22],
                                    radius=4, fill=COLORS[k][0], outline=COLORS[k][2])
                d.text((mx + 194 + j * cw, my + 22 + i * 22), str(n), font=F_SMALL, fill="#37474F")

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "ER_域总览_v1.0.png"
    img.save(path, optimize=True)
    print(f"PNG -> {path}  {img.size[0]}x{img.size[1]}")
    return path


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "er_full.mmd").write_text(mermaid_full(), encoding="utf-8")
    (OUT / "er_overview.mmd").write_text(mermaid_overview(), encoding="utf-8")
    print(f"MMD -> {OUT / 'er_full.mmd'} / er_overview.mmd")
    render_fields()
    render_overview()


if __name__ == "__main__":
    main()
