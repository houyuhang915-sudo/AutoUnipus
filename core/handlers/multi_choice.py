"""多选题 handler.

答案格式有几种常见情况,我们都尝试:
- "A,B,C" / "ABC" / "A、B、C" / ["A","B","C"]
"""
from __future__ import annotations

import re
from typing import List

from playwright.sync_api import ElementHandle, Page

from .. import logger
from ..unipus.api_client import ParsedAnswer
from .base import QuestionHandler


_SELECTORS = [
    "div.question:has(input[type=checkbox])",
    "[class*=questionMulti]",
    "[class*=multipleChoice]",
]


def _split_choices(raw) -> list[str]:
    if isinstance(raw, list):
        items = raw
    else:
        s = str(raw).strip().upper()
        items = re.split(r"[,，、\s/]+", s) if "," in s or "，" in s or "、" in s or " " in s else list(s)
    out: list[str] = []
    for item in items:
        item = (item or "").strip().upper()
        for ch in item:
            if "A" <= ch <= "G":
                out.append(ch)
                break
    # 去重保序
    seen, result = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


class MultiChoiceHandler(QuestionHandler):
    name = "multi-choice"

    def detect(self, page: Page) -> List[ElementHandle]:
        seen: list[ElementHandle] = []
        for sel in _SELECTORS:
            try:
                for el in page.query_selector_all(sel):
                    # 必须真有 checkbox 才算多选题
                    try:
                        if not el.query_selector("input[type=checkbox]"):
                            continue
                    except Exception:
                        continue
                    if el not in seen:
                        seen.append(el)
            except Exception:
                continue
        return [el for el in seen if _safe_is_visible(el)]

    def fill(self, page: Page, element: ElementHandle, answer: ParsedAnswer) -> bool:
        if not answer.answers:
            return False
        choices = _split_choices(answer.answers[0] if len(answer.answers) == 1 else answer.answers)
        if not choices:
            logger.warn(f"[{self.name}] 无法解析多选答案: {answer.answers!r}")
            return False
        ok = True
        for c in choices:
            try:
                box = element.wait_for_selector(
                    f'input[type=checkbox][value="{c}"]', timeout=1500
                )
                if box is None:
                    ok = False
                    continue
                if not box.is_checked():
                    box.click(timeout=1500)
            except Exception as e:
                logger.warn(f"[{self.name}] 勾选 {c} 失败: {e}")
                ok = False
        return ok


def _safe_is_visible(el: ElementHandle) -> bool:
    try:
        return el.is_visible()
    except Exception:
        return False
