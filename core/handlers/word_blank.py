"""选词填空 handler.

U校园选词填空形态多样,常见两种:
1. 题目下方一行候选词,空位是按钮 / dropzone
2. 每个空位是 select 下拉

为了首版稳健,这里只做:
- 检测 select 形式 -> 选 option(value 或 text 匹配答案)
- 检测 input[readonly] + 候选词列表 -> 点击候选词

更复杂的拖拽形式(react-dnd)留到后续根据真实页面 DOM 适配。
"""
from __future__ import annotations

from typing import List

from playwright.sync_api import ElementHandle, Page

from .. import logger
from ..unipus.api_client import ParsedAnswer
from .base import QuestionHandler


class WordBlankHandler(QuestionHandler):
    name = "word-blank"

    def detect(self, page: Page) -> List[ElementHandle]:
        elements: list[ElementHandle] = []
        try:
            elements.extend(page.query_selector_all("select"))
        except Exception:
            pass
        return [el for el in elements if _safe_is_visible(el)]

    def fill(self, page: Page, element: ElementHandle, answer: ParsedAnswer) -> bool:
        if not answer.answers:
            return False
        target = str(answer.answers[0]).strip()
        if not target:
            return False
        try:
            tag = (element.evaluate("e => e.tagName") or "").upper()
            if tag == "SELECT":
                # 先按 value,再按 label
                try:
                    element.select_option(value=target)
                    return True
                except Exception:
                    pass
                try:
                    element.select_option(label=target)
                    return True
                except Exception:
                    pass
            return False
        except Exception as e:
            logger.warn(f"[{self.name}] 选词失败: {e}")
            return False


def _safe_is_visible(el: ElementHandle) -> bool:
    try:
        return el.is_visible()
    except Exception:
        return False
