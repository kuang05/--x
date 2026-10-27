"""持久层：SQLite 连接、UUIDv7、通用仓储、内容寻址 blob。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[1] / "schema.sql"

_NO_ID = {"blob", "dictionary_entry", "mrc_rule_threshold", "tool_registry",
          "app_setting", "slice_path"}


def now() -> str:
    """UTC ISO-8601，带毫秒。秒级精度会导致同一秒内多次写入排序不确定。"""
    t = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
    return f"{base}.{int((t % 1) * 1000):03d}Z"


def new_id() -> str:
    """UUIDv7：48 位毫秒时间戳 + 版本位 + 随机，时间有序。"""
    ms = int(time.time() * 1000)
    b = bytearray(ms.to_bytes(6, "big") + os.urandom(10))
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        # HTTP 服务是多线程的；连接必须允许跳线程，并用锁串行化写操作
        self._lock = threading.RLock()
        self.c = sqlite3.connect(self.path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA foreign_keys=ON")
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=FULL")
        self.c.execute("PRAGMA busy_timeout=5000")
        if fresh or not self._has_schema():
            self.c.executescript(SCHEMA.read_text(encoding="utf-8"))
            self.c.commit()
        self.root = self.path.parent
        # 内容寻址的 blob 不会变，可以安全缓存；否则每次评分都要把几何文件全读一遍
        self._blob_cache: dict[str, bytes] = {}
        self._blob_cache_limit = 512

    # ---------- 基础 ----------
    def _has_schema(self) -> bool:
        return bool(self.c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project'").fetchone())

    def ins(self, table: str, **kw):
        if table not in _NO_ID and "id" not in kw:
            kw["id"] = new_id()
        cols = ", ".join(kw)
        marks = ", ".join("?" for _ in kw)
        with self._lock:
            try:
                self.c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(kw.values()))
            except sqlite3.Error as exc:
                raise sqlite3.Error(f"{table}: {exc} / {kw}") from None
        return kw.get("id")

    def q(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self.c.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        with self._lock:
            return self.c.execute(sql, params).fetchone()

    def scalar(self, sql: str, params=(), default=None):
        row = self.one(sql, params)
        return row[0] if row else default

    def commit(self) -> None:
        self.c.commit()

    def close(self) -> None:
        try:
            self.c.commit()
        finally:
            self.c.close()

    # ---------- 内容寻址 ----------
    def put_blob(self, data: bytes, kind: str) -> str:
        h = sha256(data)
        rel = f"blobs/{h[:2]}/{h[2:4]}/{h}"
        p = self.root / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        if self.one("SELECT hash FROM blob WHERE hash=?", (h,)):
            self.c.execute("UPDATE blob SET ref_count=ref_count+1 WHERE hash=?", (h,))
        else:
            self.ins("blob", hash=h, algo="sha256", kind=kind, byte_size=len(data),
                     rel_path=rel, ref_count=1, verify_status="ok", created_at=now())
        # 写后立刻被读是常态（候选试算、回滚、重放），顺手入缓存避免冷读
        with self._lock:
            if len(self._blob_cache) >= self._blob_cache_limit:
                self._blob_cache.clear()
            self._blob_cache[h] = data
        return h

    def get_blob(self, h: str) -> bytes:
        """读 blob（按内容哈希缓存）。

        blob 路径由 sha256 决定、写入后不再变，所以缓存无失效问题；
        mrc.run 一次要读整块板的几何，不缓存时纯读盘就能占掉 90% 的评分时间。
        """
        with self._lock:
            hit = self._blob_cache.get(h)
        if hit is not None:
            return hit
        data = (self.root / f"blobs/{h[:2]}/{h[2:4]}/{h}").read_bytes()
        with self._lock:
            if len(self._blob_cache) >= self._blob_cache_limit:
                self._blob_cache.clear()
            self._blob_cache[h] = data
        return data

    # ---------- 便捷 ----------
    def add_diagnostic(self, project_id: str, scope: str, code: str, message: str,
                       severity: str = "error", target_kind: str = "", target_id: str = "",
                       detail: dict | None = None) -> str:
        return self.ins("diagnostic", project_id=project_id, scope=scope, code=code,
                        message=message, severity=severity, target_kind=target_kind,
                        target_id=target_id, detail_json=json.dumps(detail or {}, ensure_ascii=False),
                        created_at=now())

    def setting(self, key: str, default=None):
        row = self.one("SELECT value_json FROM app_setting WHERE setting_key=?", (key,))
        return json.loads(row["value_json"]) if row else default

    def set_setting(self, key: str, value) -> None:
        self.c.execute(
            "INSERT INTO app_setting(setting_key,value_json,scope,updated_at) VALUES(?,?,'global',?) "
            "ON CONFLICT(setting_key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now()))
