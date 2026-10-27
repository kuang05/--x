"""后台任务：进度与取消（UI-005）。

为什么不用线程强杀：
    Python 线程无法安全中断，强杀会留下半写状态。
    所以取消是**协作式**的——只在道次边界检查标志；
    若任务已写过库，由 rollback 回调把这次产生的 slice_set 删干净。

对外只暴露三件事：提交（submit）、查进度（get）、取消（cancel）。
"""
from __future__ import annotations

import threading
import traceback
import uuid
from typing import Callable


class Job:
    def __init__(self, job_id: str, kind: str):
        self.id = job_id
        self.kind = kind
        self.status = "running"          # running / done / cancelled / error
        self.progress = 0
        self.total = 1
        self.error: dict | None = None
        self.result: dict | None = None
        self.rollback: Callable[[], None] | None = None
        self._cancel = threading.Event()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def snapshot(self) -> dict:
        pct = 0 if not self.total else int(round(100 * self.progress / self.total))
        return {"id": self.id, "kind": self.kind, "status": self.status,
                "progress": pct, "done": self.progress, "total": self.total,
                "error": self.error,
                "result": {k: v for k, v in (self.result or {}).items()
                           if k in ("slice_id", "paths", "length_mm", "passes")}}


class Registry:
    """进程内任务表。单机单进程够用，不引入外部队列。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, fn: Callable[[Job], dict],
               rollback: Callable[[], None] | None = None) -> str:
        job = Job(uuid.uuid4().hex[:12], kind)
        job.rollback = rollback
        with self._lock:
            self._jobs[job.id] = job

        def _run() -> None:
            try:
                job.result = fn(job) or {}
                job.status = "done"
                job.progress = job.total
            except Exception as exc:                      # noqa: BLE001
                if type(exc).__name__ == "SliceCancelled" or job.cancelled():
                    job.status = "cancelled"
                    if job.rollback:
                        try:
                            job.rollback()
                        except Exception:
                            traceback.print_exc()
                else:
                    job.status = "error"
                    job.error = {"code": type(exc).__name__,
                                 "reason": str(exc),
                                 "hint": "检查工程数据与依赖；服务端日志有堆栈"}
            finally:
                # 取消后清空回调，避免二次调用
                job.rollback = None

        threading.Thread(target=_run, daemon=True).start()
        return job.id

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status != "running":
            return False
        job._cancel.set()
        return True

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
