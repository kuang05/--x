"""二维几何与栅格：距离、相交、轮廓填充、蛇形路径。全部纯 numpy + PIL。"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw

Point = tuple[float, float]


def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def polyline_len(pts: list[Point]) -> float:
    return sum(dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def point_seg_dist(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    if L == 0:
        return dist(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def seg_intersect(a, b, c, d) -> bool:
    """真交叉（不含共线/端点接触），足以判断短路。"""
    o1, o2 = _orient(a, b, c), _orient(a, b, d)
    o3, o4 = _orient(c, d, a), _orient(c, d, b)
    return (o1 * o2 < 0) and (o3 * o4 < 0)


def bbox(pts: list[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


class Grid:
    """板面栅格：默认 0.25 mm/格，60×40 板约 240×160 格。"""

    def __init__(self, w_mm: float, h_mm: float, res: float = 0.25):
        self.w, self.h, self.res = float(w_mm), float(h_mm), float(res)
        self.nx = max(1, int(round(self.w / self.res)))
        self.ny = max(1, int(round(self.h / self.res)))

    # ---- 坐标 ----
    def cell(self, x: float, y: float) -> tuple[int, int]:
        return min(self.nx - 1, max(0, int(x / self.res))), min(self.ny - 1, max(0, int(y / self.res)))

    def center(self, i: int, j: int) -> Point:
        return ((i + 0.5) * self.res, (j + 0.5) * self.res)

    def empty(self) -> np.ndarray:
        return np.zeros((self.ny, self.nx), dtype=bool)

    # ---- 栅格化 ----
    def polygon(self, pts: list[Point]) -> np.ndarray:
        img = Image.new("L", (self.nx, self.ny), 0)
        d = ImageDraw.Draw(img)
        d.polygon([(x / self.res, y / self.res) for x, y in pts], fill=255)
        return np.array(img) > 0

    def lines(self, paths: list[list[Point]], width_mm: float) -> np.ndarray:
        img = Image.new("L", (self.nx, self.ny), 0)
        d = ImageDraw.Draw(img)
        w = max(1, int(round(width_mm / self.res)))
        for pts in paths:
            d.line([(x / self.res, y / self.res) for x, y in pts], fill=255, width=w)
        return np.array(img) > 0

    def circles(self, items: list[tuple[Point, float]]) -> np.ndarray:
        img = Image.new("L", (self.nx, self.ny), 0)
        d = ImageDraw.Draw(img)
        for (x, y), r in items:
            cx, cy, rr = x / self.res, y / self.res, r / self.res
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=255)
        return np.array(img) > 0

    # ---- 形态学 ----
    def dilate(self, mask: np.ndarray, r_mm: float) -> np.ndarray:
        it = int(round(r_mm / self.res))
        if it <= 0:
            return mask.copy()
        out = mask.copy()
        for _ in range(it):
            m = out.copy()
            m[1:, :] |= out[:-1, :]
            m[:-1, :] |= out[1:, :]
            m[:, 1:] |= out[:, :-1]
            m[:, :-1] |= out[:, 1:]
            out = m
        return out

    def erode(self, mask: np.ndarray, r_mm: float) -> np.ndarray:
        it = int(round(r_mm / self.res))
        out = mask.copy()
        for _ in range(it):
            m = out.copy()
            m[1:, :] &= out[:-1, :]
            m[:-1, :] &= out[1:, :]
            m[:, 1:] &= out[:, :-1]
            m[:, :-1] &= out[:, 1:]
            m[0, :] = m[-1, :] = False
            m[:, 0] = m[:, -1] = False
            out = m
        return out

    def opening(self, mask: np.ndarray, r_mm: float) -> np.ndarray:
        return self.dilate(self.erode(mask, r_mm), r_mm)

    # ---- 路径 ----
    def serpentine(self, mask: np.ndarray, step_mm: float, y_offset_mm: float = 0.0,
                  vertical: bool = False) -> list[list[Point]]:
        """按行（或按列）扫描生成蛇形填充；vertical=True 用于层间正交填充。"""
        work = mask.T if vertical else mask
        rows, cols = work.shape
        step = max(1, int(round(step_mm / self.res)))
        paths: list[list[Point]] = []
        flip = False
        for j in range(step // 2, rows, step):
            idx = np.flatnonzero(work[j])
            if idx.size == 0:
                continue
            runs = []
            start = prev = int(idx[0])
            for k in idx[1:]:
                k = int(k)
                if k != prev + 1:
                    runs.append((start, prev))
                    start = k
                prev = k
            runs.append((start, prev))
            if flip:
                runs.reverse()
            for a, b in runs:
                if flip:
                    a, b = b, a
                p0 = (a + 0.5) * self.res
                p1 = (b + 0.5) * self.res
                q = (j + 0.5) * self.res + y_offset_mm
                if vertical:
                    paths.append([(q, p0), (q, p1)])
                else:
                    paths.append([(p0, q), (p1, q)])
            flip = not flip
        return paths

    def mask_to_paths(self, mask: np.ndarray, step_mm: float = 0.4) -> list[list[Point]]:
        return self.serpentine(mask, step_mm)


def offset_path(pts: list[Point], dz: float) -> list[tuple[float, float, float]]:
    return [(x, y, dz) for x, y in pts]


def bounding_region(mask: np.ndarray, res: float) -> tuple[float, float, float, float]:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return (0.0, 0.0, 0.0, 0.0)
    return (xs.min() * res, ys.min() * res, (xs.max() + 1) * res, (ys.max() + 1) * res)
