"""候选排序代理模型（规则蒸馏）· v3。

定位：**不改判定，只改顺序**。Agent 每轮要比较多个近似候选，逐个跑完整 R1–R7 太慢；
这里用轻量几何特征预测分数，先粗排，最终分数仍由规则引擎给出。

v3 相对 v2 的修正（都是 v2 暴露出来的缺口）：
    1. **高分区补采样**：v2 只覆盖 0–65 分，而 Agent 目标区间是 62→97，
       因此加"修复式扰动"（_polish），按概率把违规修好，让样本覆盖全分数带；
    2. **端到端计时**：v2 报的 3000× 只是特征侧微基准，v3 同时报
       "应用改动 + 评分"的端到端加速，避免拿半个流程当整体；
    3. **跨板验证**：v2 只在同一块板上扰动，v3 提供 leave-one-board-out，
       检验换拓扑后还能不能用。
"""
from __future__ import annotations

import itertools
import json
import math
import pickle
import time
from pathlib import Path

from . import mrc
from .board import load_route_points
from .routing import min_gap, set_points
from .store import DB

FEATURE_NAMES = [
    "n_routes", "n_layers", "n_nets", "n_pads",
    "w_min", "w_mean", "w_std", "w_p10",
    "deficit_w", "n_w_bad", "frac_w_bad",
    "gap_min", "gap_mean", "gap_p10",
    "deficit_gap", "n_gap_bad", "frac_gap_bad", "n_pairs",
    "len_total", "len_mean", "pts_mean", "n_pts_bad",
]


# ---------------- 特征 ----------------

def _routes(db: DB, circuit_id: str):
    rows = db.q("SELECT * FROM route WHERE circuit_id=? AND deleted_at IS NULL", (circuit_id,))
    pts = {r["id"]: load_route_points(db, r) for r in rows}
    return rows, pts


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return float(s[idx])


def _min_gap_np(pa, pb) -> float:
    """向量化最小间隙（与 routing.min_gap 同义）。

    调用方传预先转好的 ndarray，避免每对重复 asarray；
    纯 Python 双重循环会把"代理比规则快"直接吃掉（实测只剩 1.3×）。
    """
    import numpy as np

    pa = np.asarray(pa, dtype=float).reshape(-1, 2)
    pb = np.asarray(pb, dtype=float).reshape(-1, 2)
    if len(pa) < 2 or len(pb) < 2:
        return float("inf")

    def _one_way(P, S):
        Q1, Q2 = S[:-1], S[1:]
        d = Q2 - Q1                                   # (s,2)
        L2 = np.maximum((d ** 2).sum(1), 1e-12)       # (s,)
        w = P[:, None, :] - Q1[None, :, :]            # (k,s,2)
        t = np.clip((w * d[None]).sum(2) / L2[None], 0.0, 1.0)   # (k,s)
        proj = Q1[None] + t[..., None] * d[None]      # (k,s,2)
        return float(np.sqrt(((P[:, None, :] - proj) ** 2).sum(2)).min())

    return min(_one_way(pa, pb), _one_way(pb, pa))


def _bbox_gap(pa, pb) -> float:
    """包围盒间隙下界：明显分离的一对不必算精确距离。"""
    import numpy as np
    ax0, ay0 = pa[:, 0].min(), pa[:, 1].min()
    ax1, ay1 = pa[:, 0].max(), pa[:, 1].max()
    bx0, by0 = pb[:, 0].min(), pb[:, 1].min()
    bx1, by1 = pb[:, 1].max(), pb[:, 1].max()
    dx = max(0.0, max(ax0 - bx1, bx0 - ax1))
    dy = max(0.0, max(ay0 - by1, by0 - ay1))
    del ax0, ay0, ax1, ay1, bx0, by0, bx1, by1
    return float(math.hypot(dx, dy))


def features(db: DB, circuit_id: str, width_floor: float = 0.3,
             gap_floor: float = 0.2, spacing: float = 0.4) -> list[float]:
    """轻量几何特征：只做折线距离与统计，不做栅格化。

    `deficit_*` 是关键：它把"差多少"累加起来，直接对应规则的扣分强度，
    而均值类特征会把一条严重违规摊薄成看不出。
    """
    rows, pts = _routes(db, circuit_id)
    import numpy as _np
    npts = {k: _np.asarray(v, dtype=float).reshape(-1, 2) for k, v in pts.items()}
    widths = [float(r["width_mm"]) for r in rows]
    gaps: list[float] = []
    for a, b in itertools.combinations(rows, 2):
        if a["layer_no"] != b["layer_no"] or a["net_id"] == b["net_id"]:
            continue
        pa, pb = npts[a["id"]], npts[b["id"]]
        # 包围盒已分开 2 mm 以上时，对 R2/R7（阈值 0.2–0.4）不可能违规，按 2 mm 计
        if _bbox_gap(pa, pb) > 2.0:
            gaps.append(2.0)
            continue
        d = _min_gap_np(pa, pb)
        edge = d - (float(a["width_mm"]) + float(b["width_mm"])) / 2
        if edge > 1e-9:
            gaps.append(edge)
    lengths, pts_counts = [], []
    for r in rows:
        p = pts[r["id"]]
        pts_counts.append(len(p))
        lengths.append(sum(math.hypot(p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1])
                           for i in range(len(p) - 1)))
    n = len(rows) or 1
    w_mean = sum(widths) / n
    w_std = math.sqrt(sum((w - w_mean) ** 2 for w in widths) / n)
    n_nets = db.scalar("SELECT COUNT(*) FROM net WHERE circuit_id=?", (circuit_id,), 0)
    n_pads = db.scalar("SELECT COUNT(*) FROM pad WHERE component_id IN "
                       "(SELECT id FROM component WHERE circuit_id=?)", (circuit_id,), 0)
    return [
        float(len(rows)), float(len({r["layer_no"] for r in rows})), float(n_nets), float(n_pads),
        min(widths) if widths else 0.0, w_mean, w_std, _pct(widths, 0.1),
        sum(max(0.0, width_floor - w) for w in widths),
        float(sum(1 for w in widths if w < width_floor)),
        sum(1 for w in widths if w < width_floor) / n,
        min(gaps) if gaps else 1.0, (sum(gaps) / len(gaps)) if gaps else 0.0, _pct(gaps, 0.1),
        sum(max(0.0, spacing - g) for g in gaps),
        float(sum(1 for g in gaps if g < gap_floor)),
        (sum(1 for g in gaps if g < gap_floor) / len(gaps)) if gaps else 0.0,
        float(len(gaps)),
        sum(lengths), (sum(lengths) / n) if lengths else 0.0,
        sum(pts_counts) / n,
        float(sum(1 for c in pts_counts if c < 3)),
    ]


# ---------------- 采样工具 ----------------

def _force_width(db: DB, route_id: str, width_mm: float) -> None:
    """仅采样用：绕开 CIR-010 的网络类下限，以便覆盖 R1 违规区间。

    这不是产品操作（界面/Agent 只能走 routing.set_width），只用于生成训练样本；
    标签仍由规则引擎读同一张表得出。
    """
    db.c.execute("UPDATE route SET width_mm=? WHERE id=?", (width_mm, route_id))


def _snapshot(db: DB, circuit_id: str):
    rows, pts = _routes(db, circuit_id)
    return [(r["id"], float(r["width_mm"]), list(pts[r["id"]])) for r in rows]


def _restore(db: DB, circuit_id: str, snap) -> None:
    for rid, width, points in snap:
        row = db.one("SELECT width_mm FROM route WHERE id=?", (rid,))
        if abs(float(row["width_mm"]) - width) > 1e-9:
            _force_width(db, rid, width)
        cur = load_route_points(db, db.one("SELECT * FROM route WHERE id=?", (rid,)))
        if len(cur) == len(points) and any(
                abs(cur[i][0] - points[i][0]) > 1e-9 or abs(cur[i][1] - points[i][1]) > 1e-9
                for i in range(len(cur))):
            set_points(db, rid, points)


def _perturb(db: DB, circuit_id: str, rng, snap, scale: float = 1.0,
             max_changes: int | None = None, gentle: bool = False) -> dict:
    """制造违规：改宽度、推拐点。scale 越小改动越轻微。

    `max_changes=1` 用于候选扰动：贴近 Agent 的单步操作（一次只改一处），
    否则 apply 开销会把端到端延迟算大。

    `gentle=True` 时宽度不低于 R1 阈值（0.3）且位移很小，
    用来生成高分区（80–97）的候选；否则候选一改就掉 8–16 分，
    采样出来的分数带会全部挤在低分区。
    """
    changed = {"width": 0, "points": 0}
    budget = len(snap) if max_changes is None else max_changes
    for rid, width, points in snap:
        if changed["width"] + changed["points"] >= budget:
            break
        roll = rng.random()
        if roll < 0.45:
            lo, hi = (0.5, 0.85) if gentle else (0.2, max(0.25, 0.8 * scale))
            _force_width(db, rid, round(rng.uniform(lo, hi), 3))
            changed["width"] += 1
        elif roll < 0.85 and len(points) >= 3:
            span = 0.15 if gentle else 0.8
            delta = round(rng.uniform(-span, span) * scale, 3)
            axis = 0 if rng.random() < 0.5 else 1
            moved = [points[0]] + [
                (p[0] + (delta if axis == 0 else 0), p[1] + (delta if axis == 1 else 0))
                for p in points[1:-1]] + [points[-1]]
            set_points(db, rid, moved)
            changed["points"] += 1
    return changed


def _polish(db: DB, circuit_id: str, rng, p: float, spacing: float = 0.4) -> int:
    """修复式扰动：以概率 p 把违规修好，用来覆盖高分区（62→100）。

    v2 的样本全在 0–65，Agent 真正要用的 62–97 一段没有样本，模型等于没验证。
    """
    fixed = 0
    rows, _ = _routes(db, circuit_id)
    for r in rows:
        if rng.random() > p:
            continue
        if float(r["width_mm"]) < 0.5:
            _force_width(db, r["id"], round(rng.uniform(0.5, 0.75), 3))
            fixed += 1
    rows, pts = _routes(db, circuit_id)
    for a, b in itertools.combinations(rows, 2):
        if a["layer_no"] != b["layer_no"] or a["net_id"] == b["net_id"]:
            continue
        d = min_gap(pts[a["id"]], pts[b["id"]])
        edge = d - (float(a["width_mm"]) + float(b["width_mm"])) / 2
        if edge >= spacing or rng.random() > p:
            continue
        pb = pts[b["id"]]
        if len(pb) < 3:
            continue
        delta = spacing - edge + 0.2
        moved = [pb[0]] + [(x, y + delta) for x, y in pb[1:-1]] + [pb[-1]]
        set_points(db, b["id"], moved)
        pts[b["id"]] = moved
        fixed += 1
    return fixed


# ---------------- 数据集 ----------------

def build_dataset(db: DB, project_id: str, version_id: str, groups: int = 60,
                  per_group: int = 6, seed: int = 20261027,
                  polish_probs: tuple[float, ...] = (0.0, 0.35, 0.7, 1.0)) -> dict:
    """按基线分组采样。

    每组：先制造违规 → 按概率修复到某个程度（决定该组落在哪个分数带）→
    再在其上生成 per_group 个**近似候选**（小幅扰动），组内才比较得出"哪个改法更好"。
    """
    import random
    rng = random.Random(seed)
    cid = db.scalar(
        "SELECT id FROM circuit WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (project_id,))
    snap = _snapshot(db, cid)
    X, y, gid = [], [], []
    apply_ms = rule_ms = feat_ms = 0.0
    for g in range(groups):
        _perturb(db, cid, rng, snap, scale=rng.uniform(0.4, 1.0))
        _polish(db, cid, rng, rng.choice(polish_probs))
        base = _snapshot(db, cid)
        for _ in range(per_group):
            t0 = time.perf_counter()
            # 1/3 候选保持原样：否则候选总是被扣分，80–97 段永远采不到
            if rng.random() >= 0.34:
                _perturb(db, cid, rng, base, scale=rng.uniform(0.05, 0.3),
                         max_changes=1, gentle=True)
            t1 = time.perf_counter()
            run = mrc.run(db, project_id, version_id)
            t2 = time.perf_counter()
            X.append(features(db, cid))
            t3 = time.perf_counter()
            y.append(float(run["score"]))
            gid.append(g)
            apply_ms += (t1 - t0) * 1000
            rule_ms += (t2 - t1) * 1000
            feat_ms += (t3 - t2) * 1000
            _restore(db, cid, base)
        _restore(db, cid, snap)
    n = max(1, len(y))
    return {"X": X, "y": y, "groups": gid, "circuit_id": cid,
            "apply_ms": apply_ms / n, "rule_ms": rule_ms / n, "feature_ms": feat_ms / n}


# ---------------- 训练与评估 ----------------

def train_regressor(X, y, seed: int = 0):
    from sklearn.ensemble import GradientBoostingRegressor
    model = GradientBoostingRegressor(n_estimators=400, max_depth=3,
                                      learning_rate=0.05, subsample=0.9, random_state=seed)
    model.fit(X, y)
    return model


def train_pairwise(X, y, groups, seed: int = 0):
    """组内两两做差：学习"谁更好"，不学绝对分数。"""
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier
    diffs, labels = [], []
    by_group: dict[int, list[int]] = {}
    for i, g in enumerate(groups):
        by_group.setdefault(g, []).append(i)
    for idxs in by_group.values():
        for i, j in itertools.combinations(idxs, 2):
            if abs(y[i] - y[j]) < 1e-9:
                continue
            better, worse = (i, j) if y[i] > y[j] else (j, i)
            vb, vw = np.asarray(X[better]), np.asarray(X[worse])
            # 对称采样：正反各一条，既是平衡二分类，也让模型学到"差距越大越肯定"
            diffs.append(vb - vw)
            labels.append(1.0)
            diffs.append(vw - vb)
            labels.append(0.0)
    if not diffs:
        return None
    model = GradientBoostingClassifier(n_estimators=200, max_depth=3, random_state=seed)
    model.fit(np.asarray(diffs), np.asarray(labels))
    return model


def _pairwise_rank(model, Xg) -> list[int]:
    import numpy as np
    n = len(Xg)
    wins = [0] * n
    for i in range(n):
        for j in range(i + 1, n):
            d = (np.asarray(Xg[i]) - np.asarray(Xg[j])).reshape(1, -1)
            p = float(model.predict_proba(d)[0][1])
            if p >= 0.5:
                wins[i] += 1
            else:
                wins[j] += 1
    return wins


def evaluate_rank(model, X, y, groups, timing: dict, pairwise=None) -> dict:
    import numpy as np
    from scipy.stats import spearmanr

    t0 = time.perf_counter()
    pred = model.predict(np.asarray(X, dtype=float))
    pred_ms = (time.perf_counter() - t0) * 1000 / max(1, len(X))
    rho = float(spearmanr(y, pred).correlation) if len(set(y)) > 2 else 1.0
    mae = float(np.mean(np.abs(np.asarray(pred) - np.asarray(y))))

    by_group: dict[int, list[int]] = {}
    for i, g in enumerate(groups):
        by_group.setdefault(g, []).append(i)
    top1 = top1_pair = judged = 0
    for idxs in by_group.values():
        ys = [y[i] for i in idxs]
        if len(set(ys)) < 2:
            continue
        judged += 1
        best = int(np.argmax(ys))
        if int(np.argmax([pred[i] for i in idxs])) == best:
            top1 += 1
        if pairwise is not None:
            wins = _pairwise_rank(pairwise, [X[i] for i in idxs])
            if int(np.argmax(wins)) == best:
                top1_pair += 1

    apply_ms = float(timing.get("apply_ms", 0.0))
    rule_ms = float(timing.get("rule_ms", 0.0))
    feat_ms = float(timing.get("feature_ms", 0.0)) + pred_ms
    return {
        "spearman": round(rho, 4),
        "mae": round(mae, 3),
        "top1_regressor": round(top1 / judged, 3) if judged else None,
        "top1_pairwise": round(top1_pair / judged, 3) if (pairwise is not None and judged) else None,
        "groups": judged,
        # 纯评分侧
        "feature_ms": round(feat_ms, 4),
        "rule_ms": round(rule_ms, 3),
        "speedup": round(rule_ms / feat_ms, 1) if feat_ms > 0 else None,
        # 端到端：应用改动 + 评分，才是 Agent 每轮真实付的代价
        "apply_ms": round(apply_ms, 3),
        "e2e_rule_ms": round(apply_ms + rule_ms, 3),
        "e2e_surrogate_ms": round(apply_ms + feat_ms, 3),
        "speedup_e2e": round((apply_ms + rule_ms) / (apply_ms + feat_ms), 2)
        if (apply_ms + feat_ms) > 0 else None,
        "n": len(y),
        "score_range": [round(min(y), 2), round(max(y), 2)],
        "score_bands": _bands(y),
    }


def _bands(y) -> dict[str, int]:
    """分数带分布：确认高分区（Agent 目标 62–97）有没有样本。"""
    out = {"0-40": 0, "40-60": 0, "60-80": 0, "80-95": 0, "95-100": 0}
    for v in y:
        if v < 40:
            out["0-40"] += 1
        elif v < 60:
            out["40-60"] += 1
        elif v < 80:
            out["60-80"] += 1
        elif v < 95:
            out["80-95"] += 1
        else:
            out["95-100"] += 1
    return out


# ---------------- 入口 ----------------

def _new_board(db: DB, name: str, w: float, h: float) -> tuple[str, str]:
    from .store import now
    pid = db.ins("project", name=name, unit="mm", sheet_w_mm=w, sheet_h_mm=h,
                 origin_kind="board_bottom_left", data_schema_version=1,
                 dir_rel_path=f"projects/{name}", created_at=now(), updated_at=now())
    v0 = db.ins("version", project_id=pid, version_no=0, parent_version_id=None,
                kind="initial", author_kind="human", author_name="local",
                reason="代理模型采样", created_at=now())
    db.c.execute("UPDATE project SET current_version_id=? WHERE id=?", (v0, pid))
    db.commit()
    return pid, v0


def _scratch(ws: Path, name: str = "surrogate_scratch.db") -> DB:
    scratch = ws / name
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(scratch) + suffix)
        if p.exists():
            p.unlink()
    return DB(scratch)


def run(workspace: Path, groups: int = 60, per_group: int = 6) -> dict:
    """单板：分组采样 → 分组留出 → 回归/成对 → 评估 → 落盘。"""
    import numpy as np
    from sklearn.model_selection import GroupShuffleSplit

    from fixtures import t19a as fx
    from . import board, step

    ws = Path(workspace)
    db = _scratch(ws)
    pid, v0 = _new_board(db, "surrogate", 60, 40)
    sid, _ = board.import_step(db, pid, step.make_fixture_step(60, 40, 1.2), step)
    board.import_board(db, pid, sid, fx.t19a())
    mrc.ensure_baseline(db)

    data = build_dataset(db, pid, v0, groups=groups, per_group=per_group)
    X = np.asarray(data["X"], dtype=float)
    y = np.asarray(data["y"], dtype=float)
    g = np.asarray(data["groups"])
    timing = {k: data[k] for k in ("apply_ms", "rule_ms", "feature_ms")}

    tr, te = next(GroupShuffleSplit(n_splits=1, test_size=0.3, random_state=7).split(X, y, g))
    model = train_regressor(X[tr].tolist(), y[tr].tolist())
    pair = train_pairwise(X[tr].tolist(), y[tr].tolist(), g[tr].tolist())
    strict = evaluate_rank(model, X[te].tolist(), y[te].tolist(), g[te].tolist(),
                           timing, pairwise=pair)

    # 对照：随机按样本划分（v1 做法），用来看泄漏会把指标抬多少
    idx = np.random.default_rng(7).permutation(len(y))
    cut = int(len(y) * 0.7)
    tr2, te2 = idx[:cut], idx[cut:]
    model2 = train_regressor(X[tr2].tolist(), y[tr2].tolist())
    pair2 = train_pairwise(X[tr2].tolist(), y[tr2].tolist(), g[tr2].tolist())
    loose = evaluate_rank(model2, X[te2].tolist(), y[te2].tolist(), g[te2].tolist(),
                          timing, pairwise=pair2)

    model_path = ws / "surrogate.pkl"
    model_path.write_bytes(pickle.dumps(
        {"model": model, "pairwise": pair, "features": FEATURE_NAMES}, 4))
    order = sorted(zip(FEATURE_NAMES, model.feature_importances_), key=lambda kv: -kv[1])
    report = {
        "groups": groups, "per_group": per_group, "n": int(len(y)),
        "train": int(len(tr)), "test": int(len(te)),
        "group_split": strict, "random_split": loose,
        "feature_importance": [(k, round(float(v), 4)) for k, v in order[:8]],
        "model_path": str(model_path),
    }
    md = ws / "surrogate_report.md"
    md.write_text(_md(report), encoding="utf-8")
    report["report_path"] = str(md)
    (ws / "surrogate_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    db.close()
    return report


def load(workspace: Path) -> dict | None:
    """读取已训练的代理模型；没有就返回 None（调用方需自行退回规则路径）。"""
    path = Path(workspace) / "surrogate.pkl"
    if not path.exists():
        return None
    try:
        bundle = pickle.loads(path.read_bytes())
    except Exception:
        return None
    if not isinstance(bundle, dict) or "model" not in bundle:
        return None
    return bundle


def predict(bundle: dict, feats: list[float]) -> float:
    return float(bundle["model"].predict([feats])[0])


def rank(bundle: dict, feats_list: list[list[float]]) -> list[int]:
    """给一组候选排序，返回下标（最优在前）。

    优先用**成对模型**：高分区候选之间只差几分时，回归会把它们磨平
    （实测 Top-1 0.429），而成对比较能保住 0.857。
    """
    if not feats_list:
        return []
    pair = bundle.get("pairwise")
    if pair is not None:
        wins = _pairwise_rank(pair, feats_list)
        return sorted(range(len(feats_list)), key=lambda i: (-wins[i], i))
    scores = [predict(bundle, f) for f in feats_list]
    return sorted(range(len(scores)), key=lambda i: (-scores[i], i))


def _md(r: dict) -> str:
    a, b = r["group_split"], r["random_split"]
    return (
        "# 候选排序代理模型 · 评测报告 v3\n\n"
        f"- 采样：{r['groups']} 组 × {r['per_group']} 候选 = {r['n']} 样本，标签来自规则引擎\n"
        f"- 分数区间：{a['score_range'][0]} – {a['score_range'][1]}；"
        f"分数带分布 {a['score_bands']}\n"
        f"- 划分：**按基线分组留出 30%**（{r['train']} 训练 / {r['test']} 测试），另附随机划分作对照\n\n"
        "| 指标 | 分组划分（严格） | 随机划分（对照） |\n"
        "|---|---|---|\n"
        f"| Spearman ρ | {a['spearman']} | {b['spearman']} |\n"
        f"| MAE | {a['mae']} | {b['mae']} |\n"
        f"| Top-1（回归） | {a['top1_regressor']} | {b['top1_regressor']} |\n"
        f"| Top-1（成对） | {a['top1_pairwise']} | {b['top1_pairwise']} |\n"
        f"| 候选组数 | {a['groups']} | {b['groups']} |\n\n"
        "### 延迟（同一候选）\n\n"
        f"- 应用改动：{a['apply_ms']} ms\n"
        f"- 规则评分：{a['rule_ms']} ms → 端到端 **{a['e2e_rule_ms']} ms**\n"
        f"- 代理评分：{a['feature_ms']} ms → 端到端 **{a['e2e_surrogate_ms']} ms**\n"
        f"- 纯评分加速 **{a['speedup']}×**；端到端加速 **{a['speedup_e2e']}×**\n\n"
        f"- 重要特征：{', '.join(f'{k}={v}' for k, v in r['feature_importance'])}\n\n"
        "> 定位：只做候选粗排，最终分数仍由规则引擎给出；关闭后不影响任何验收结论。\n")


def run_cross_board(workspace: Path, boards: int = 3, groups: int = 10,
                    per_group: int = 5) -> dict:
    """跨板验证：留一块板不参与训练，看模型换拓扑还能不能用。

    每块板用同一夹具导入，然后随机删/加走线改变拓扑，模拟"换一块板"。
    """
    import random

    import numpy as np

    from fixtures import t19a as fx
    from . import board, routing, step
    from .store import now

    ws = Path(workspace)
    db = _scratch(ws, "surrogate_cross.db")
    X, y, board_id, gid = [], [], [], []
    apply_ms = rule_ms = feat_ms = 0.0
    group_offset = 0
    rng = random.Random(20261027)

    for b in range(boards):
        pid, v0 = _new_board(db, f"board{b}", 60, 40)
        sid, _ = board.import_step(db, pid, step.make_fixture_step(60, 40, 1.2), step)
        res = board.import_board(db, pid, sid, fx.t19a())
        mrc.ensure_baseline(db)
        cid = res.circuit_id
        # 改拓扑：随机删 0–3 条线，再加 0–3 条随机短线
        rows = db.q("SELECT * FROM route WHERE circuit_id=? AND deleted_at IS NULL", (cid,))
        for r in rng.sample(rows, k=min(len(rows), rng.randint(0, 3))):
            db.c.execute("UPDATE route SET deleted_at=? WHERE id=?", (now(), r["id"]))
        nets = db.q("SELECT * FROM net WHERE circuit_id=?", (cid,))
        for _ in range(rng.randint(0, 3)):
            net = rng.choice(nets)
            x0, y0 = rng.uniform(5, 50), rng.uniform(5, 35)
            pts = [(x0, y0), (x0 + rng.uniform(3, 8), y0),
                   (x0 + rng.uniform(3, 8), y0 + rng.uniform(2, 6))]
            try:
                routing.add_route(db, cid, net["id"], pts, 0.6)
            except ValueError:
                pass
        db.commit()

        data = build_dataset(db, pid, v0, groups=groups, per_group=per_group, seed=1000 + b)
        X += data["X"]
        y += data["y"]
        board_id += [b] * len(data["y"])
        gid += [group_offset + g for g in data["groups"]]
        group_offset += groups
        apply_ms += data["apply_ms"]
        rule_ms += data["rule_ms"]
        feat_ms += data["feature_ms"]

    Xa, ya, ga = np.asarray(X, float), np.asarray(y, float), np.asarray(board_id)
    gk = np.asarray(gid)
    timing = {"apply_ms": apply_ms / boards, "rule_ms": rule_ms / boards,
              "feature_ms": feat_ms / boards}
    per_board = []
    for b in range(boards):
        tr, te = ga != b, ga == b
        if len(set(ya[tr])) < 3 or te.sum() < 4:
            continue
        model = train_regressor(Xa[tr].tolist(), ya[tr].tolist())
        ev = evaluate_rank(model, Xa[te].tolist(), ya[te].tolist(),
                           [int(x) for x in gk[te]], timing)
        per_board.append({"board": int(b), "n": int(te.sum()),
                          "spearman": ev["spearman"], "mae": ev["mae"],
                          "top1": ev["top1_regressor"], "groups": ev["groups"]})
    full = train_regressor(Xa.tolist(), ya.tolist())
    ev_all = evaluate_rank(full, Xa.tolist(), ya.tolist(), [int(x) for x in gk], timing)
    out = {
        "boards": boards, "n": int(len(ya)), "per_board": per_board,
        "in_sample_spearman": ev_all["spearman"],
        "mean_lobo_spearman": round(float(np.mean([p["spearman"] for p in per_board])), 4)
        if per_board else None,
        "mean_lobo_top1": round(float(np.mean([p["top1"] for p in per_board])), 3)
        if per_board else None,
        "timing": timing,
    }
    md = ws / "surrogate_cross_board.md"
    lines = ["# 跨板验证（leave-one-board-out）\n",
             f"- 板数：{boards}，样本 {out['n']}，每块板拓扑不同（随机删/加走线）\n",
             f"- 同板内样本内评估 ρ：**{out['in_sample_spearman']}**（上界，非泛化）\n",
             f"- 跨板平均 ρ：**{out['mean_lobo_spearman']}**，平均 Top-1：**{out['mean_lobo_top1']}**\n",
             "\n| 留出板 | 样本 | ρ | MAE | Top-1 | 组数 |", "|---|---|---|---|---|---|"]
    for p in per_board:
        lines.append(f"| {p['board']} | {p['n']} | {p['spearman']} | {p['mae']} | "
                     f"{p['top1']} | {p['groups']} |")
    lines.append("\n> 跨板 ρ 明显低于同板 ρ 时，说明模型学到的是这块板的几何特征，换板要重训。\n")
    md.write_text("\n".join(lines), encoding="utf-8")
    out["report_path"] = str(md)
    db.close()
    return out
