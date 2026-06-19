"""翻译题 / 简答题 handler.

页面形态通常是大段 textarea 或 contenteditable 区域,
我们直接把答案塞进去。与 BlankHandler 的差别在于选择器策略 + 多空位顺序。

填法是模拟键盘逐字符敲入,避免 U校园 风控"操作过于频繁"。
"""
from __future__ import annotations

import random
from typing import List

from playwright.sync_api import ElementHandle, Page

from .. import logger
from ..unipus.api_client import ParsedAnswer
from .base import QuestionHandler


class TranslationHandler(QuestionHandler):
    name = "translation"

    def detect(self, page: Page) -> List[ElementHandle]:
        try:
            els = page.query_selector_all(
                "textarea, [contenteditable=true]"
            )
        except Exception:
            els = []
        return [el for el in els if _safe_is_visible(el)]

    def fill(self, page: Page, element: ElementHandle, answer: ParsedAnswer) -> bool:
        if not answer.answers:
            return False
        text = str(answer.answers[0]).strip()
        if not text:
            return False
        try:
            tag = (element.evaluate("e => e.tagName") or "").upper()
            try:
                element.click()
            except Exception:
                pass
            page.wait_for_timeout(random.randint(120, 240))
            # 强力清空(应对 review 模式预填充)
            try:
                element.evaluate(
                    """
                    e => {
                        e.removeAttribute('readonly');
                        e.removeAttribute('disabled');
                        if ('value' in e) e.value = '';
                        if (e.isContentEditable) e.innerHTML = '';
                        e.dispatchEvent(new Event('input', { bubbles: true }));
                        e.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    """
                )
            except Exception:
                pass
            try:
                element.click(click_count=3)
                page.keyboard.press("Backspace")
            except Exception:
                pass
            if tag == "TEXTAREA":
                try:
                    element.fill("")
                except Exception:
                    pass
            # 翻译题文字多,按字符 8-25ms 节奏(打字员速度)
            page.keyboard.type(text, delay=random.randint(8, 25))
            page.wait_for_timeout(random.randint(150, 300))
            return True
        except Exception as e:
            logger.warn(f"[{self.name}] 填入失败: {e}")
            return False


def _safe_is_visible(el: ElementHandle) -> bool:
    try:
        return el.is_visible()
    except Exception:
        return False
