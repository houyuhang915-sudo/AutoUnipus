"""手动答案库 — 用户预先录入的标准答案,作为最优先源.

数据文件 ``data/manual_answers.json`` 结构:

{
  "exact_by_qid":   { "<qid>": ["ans1", "ans2", ...] },
  "exact_by_task":  { "u2g68": [...] },
  "fuzzy_by_title": { "1-2 Text A Word building": [...] }
}

匹配优先级:
1. exact_by_qid  最稳(qid 在 U校园 是唯一标识,从 summary 抓出)
2. exact_by_task 次稳(URL 里的 task 段,老/新版都能拿)
3. fuzzy_by_title 模糊(把 key 和 query.title 都做规范化后子串匹配)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

from .. import logger
from ..unipus.api_client import ParsedAnswer
from .base import AnswerQuery, AnswerResult, AnswerSource


def _normalize(s: str) -> str:
    """全小写 + 去标点空白,留下字母数字."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


class ManualAnswerSource(AnswerSource):
    name = "manual-db"

    def __init__(self, path: str | Path = "data/manual_answers.json"):
        self.path = Path(path)
        self._data: dict = {}
        self._mtime: float = 0.0
        self._load_if_changed()

    @property
    def available(self) -> bool:
        return True

    # ------------- 内部 -------------

    def _load_if_changed(self) -> None:
        """文件 mtime 变了就重读,不重启 webui 也能生效."""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._data = {}
            self._mtime = 0.0
            return
        if stat.st_mtime == self._mtime:
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception as e:
            logger.warn(f"[{self.name}] 答案库加载失败: {e}")
            self._data = {}
            self._mtime = 0.0
            return
        self._mtime = stat.st_mtime
        n = sum(
            len(self._data.get(k, {}) or {})
            for k in ("exact_by_qid", "exact_by_task", "fuzzy_by_title")
        )
        logger.info(f"[{self.name}] 答案库已加载: {self.path} 共 {n} 条")

    def _to_parsed(self, items: list) -> List[ParsedAnswer]:
        out: list[ParsedAnswer] = []
        for v in items:
            out.append(ParsedAnswer(answers=[str(v)], id=len(out)))
        return out

    # ------------- 主入口 -------------

    def fetch(self, query: AnswerQuery) -> Optional[AnswerResult]:
        self._load_if_changed()
        if not self._data:
            return None

        # 1) by_qid 精确(query.task_id 在老版有时 = qid hex,新版 = 节点 hex)
        by_qid = self._data.get("exact_by_qid") or {}
        if query.task_id and query.task_id in by_qid:
            items = by_qid[query.task_id]
            if items:
                logger.info(
                    f"[{self.name}] qid 命中: {query.task_id} ({len(items)} 条)"
                )
                return AnswerResult(
                    answers=self._to_parsed(items),
                    source=f"{self.name}:qid",
                )

        # 2) by_task 精确(URL 里的 task 段,老版 u2g68 / 新版 hex)
        by_task = self._data.get("exact_by_task") or {}
        if query.task_id and query.task_id in by_task:
            items = by_task[query.task_id]
            if items:
                logger.info(
                    f"[{self.name}] task 命中: {query.task_id} ({len(items)} 条)"
                )
                return AnswerResult(
                    answers=self._to_parsed(items),
                    source=f"{self.name}:task",
                )

        # 3) fuzzy_by_title 模糊
        title_norm = _normalize(query.title)
        if title_norm:
            by_title = self._data.get("fuzzy_by_title") or {}
            for key, items in by_title.items():
                key_norm = _normalize(key)
                if key_norm and key_norm in title_norm:
                    if items:
                        logger.info(
                            f"[{self.name}] 标题模糊命中 key={key!r} "
                            f"({len(items)} 条)"
                        )
                        return AnswerResult(
                            answers=self._to_parsed(items),
                            source=f"{self.name}:title",
                        )

        # 都没命中 — 给用户一个清晰的提示
        logger.tip(
            f"[{self.name}] 未命中,可在 {self.path.name} 添加:\n"
            f"    \"exact_by_task\": {{ {query.task_id!r}: [...] }}  "
            f"或\n    \"fuzzy_by_title\": {{ {query.title or '<title>'!r}: [...] }}"
        )
        return None
