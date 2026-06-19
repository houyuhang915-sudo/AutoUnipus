"""SQLite 答案缓存.

按 (course_instance_id, task_id) 缓存解密后的答案数组,
避免重复调用 API 与潜在风控。
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import List, Optional

from ..unipus.api_client import ParsedAnswer


_SCHEMA = """
CREATE TABLE IF NOT EXISTS answer_cache (
    course_instance_id TEXT NOT NULL,
    task_id            TEXT NOT NULL,
    payload            TEXT NOT NULL,    -- json.dumps(List[{answers, id}])
    source             TEXT NOT NULL,
    created_at         INTEGER NOT NULL,
    PRIMARY KEY (course_instance_id, task_id)
);
"""


class AnswerCache:
    """key=(course_instance_id, task_id) 的答案缓存."""

    def __init__(self, path: str | Path = "data/answers.db", enabled: bool = True):
        self.enabled = enabled
        self.path = Path(path)
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as conn:
                conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, course_instance_id: str, task_id: str) -> Optional[List[ParsedAnswer]]:
        if not self.enabled:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload FROM answer_cache WHERE course_instance_id=? AND task_id=?",
                (course_instance_id, task_id),
            ).fetchone()
        if not row:
            return None
        try:
            arr = json.loads(row["payload"])
        except Exception:
            return None
        answers = [
            ParsedAnswer(answers=item["answers"], id=item.get("id", 0))
            for item in arr
        ]
        # 过滤掉历史脏数据:全是空字符串的 entry 视为未命中
        if not _has_real_content(answers):
            return None
        return answers

    def put(
        self,
        course_instance_id: str,
        task_id: str,
        answers: List[ParsedAnswer],
        source: str,
    ) -> None:
        if not self.enabled or not answers:
            return
        # 不要把空答案写进缓存,免得后续命中拿到垃圾
        if not _has_real_content(answers):
            return
        payload = json.dumps(
            [{"answers": a.answers, "id": a.id} for a in answers],
            ensure_ascii=False,
        )
        with closing(self._connect()) as conn:
            conn.execute(
                "REPLACE INTO answer_cache "
                "(course_instance_id, task_id, payload, source, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (course_instance_id, task_id, payload, source, int(time.time())),
            )
            conn.commit()


def _has_real_content(answers: List[ParsedAnswer]) -> bool:
    """至少有一个 ParsedAnswer 有非空字符串答案."""
    for a in answers:
        if not a.answers:
            continue
        for s in a.answers:
            if str(s).strip():
                return True
    return False
