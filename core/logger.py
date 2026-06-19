"""统一日志输出.

保留原项目 [Info]/[Tip]/[Error]/[System] 的前缀风格,方便用户看日志,
同时提供模块化调用接口,避免 `print` 散落各处。

在 v2 中额外提供:
- 内存环形缓冲(供 webui 拉取历史日志)
- pub/sub 队列(供 webui SSE 推送实时日志)
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any


_LEVEL_TAGS = {
    "info": "[Info]",
    "tip": "[Tip]",
    "warn": "[Warn]",
    "error": "[Error]",
    "system": "[System]",
    "debug": "[Debug]",
}

# 内存日志缓冲(最近 N 条),webui 启动时一次性拉历史
_BUFFER: deque[dict[str, Any]] = deque(maxlen=2000)
_BUFFER_LOCK = threading.Lock()

# 订阅者队列列表,供 SSE 实时推送
_SUBSCRIBERS: list[queue.Queue] = []
_SUB_LOCK = threading.Lock()


def _broadcast(entry: dict[str, Any]) -> None:
    with _BUFFER_LOCK:
        _BUFFER.append(entry)
    with _SUB_LOCK:
        dead: list[queue.Queue] = []
        for q in _SUBSCRIBERS:
            try:
                q.put_nowait(entry)
            except queue.Full:
                dead.append(q)
        for q in dead:
            try:
                _SUBSCRIBERS.remove(q)
            except ValueError:
                pass


def _emit(level: str, msg: str, *, with_time: bool = False) -> None:
    tag = _LEVEL_TAGS.get(level, f"[{level.title()}]")
    prefix = f"{datetime.now().strftime('%H:%M:%S')} " if with_time else ""
    line = f"{prefix}{tag}{msg}"
    stream = sys.stderr if level in ("error", "warn") else sys.stdout
    print(line, file=stream)
    _broadcast({"level": level, "msg": line, "ts": time.time()})


# ---------- 给 webui 用的工具 ----------

def get_recent(limit: int = 500) -> list[dict[str, Any]]:
    with _BUFFER_LOCK:
        items = list(_BUFFER)
    return items[-limit:]


def subscribe(maxsize: int = 1024) -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=maxsize)
    with _SUB_LOCK:
        _SUBSCRIBERS.append(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _SUB_LOCK:
        try:
            _SUBSCRIBERS.remove(q)
        except ValueError:
            pass


def clear_buffer() -> None:
    with _BUFFER_LOCK:
        _BUFFER.clear()


def info(msg: str) -> None:
    _emit("info", msg)


def tip(msg: str) -> None:
    _emit("tip", msg)


def warn(msg: str) -> None:
    _emit("warn", msg)


def error(msg: str) -> None:
    _emit("error", msg)


def system(msg: str) -> None:
    _emit("system", msg)


def debug(msg: str, enabled: bool = False) -> None:
    if enabled:
        _emit("debug", msg, with_time=True)
