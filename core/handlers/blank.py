"""填空题 handler(普通 input/textarea 文本填空)."""
from __future__ import annotations

import random
from typing import List

from playwright.sync_api import ElementHandle, Page

from .. import logger
from ..unipus.api_client import ParsedAnswer
from .base import QuestionHandler


_INPUT_SELECTORS = [
    "input[type=text]",
    "input:not([type])",
    "textarea",
    "[contenteditable=true]",
]


def _human_type(page: Page, element: ElementHandle, text: str) -> None:
    """模拟人手敲键盘:聚焦 → 强力清空 → 逐字符输入(每字 25-60ms).

    强力清空步骤(应对 U校园 review 模式的预填充):
    1. 临时移除 readonly / disabled 属性
    2. 直接 e.value = '' + 派发 input/change 事件(让 React/Vue 同步)
    3. 三连点全选 + Backspace 兜底
    """
    try:
        element.click()
    except Exception:
        pass
    page.wait_for_timeout(random.randint(80, 180))

    # ① 移除 readonly/disabled,清 value,触发框架同步
    try:
        element.evaluate(
            """
            e => {
                e.removeAttribute('readonly');
                e.removeAttribute('disabled');
                if ('value' in e) e.value = '';
                if (e.isContentEditable) e.innerText = '';
                e.dispatchEvent(new Event('input', { bubbles: true }));
                e.dispatchEvent(new Event('change', { bubbles: true }));
            }
            """
        )
    except Exception:
        pass

    # ② 三连点全选已有内容,再 Backspace 删干净
    try:
        element.click(click_count=3)
        page.keyboard.press("Backspace")
    except Exception:
        pass

    # ③ 兜底:Playwright 的 fill("") 也试一下(对真 input 友好)
    try:
        element.fill("")
    except Exception:
        pass

    # 逐字符敲
    page.keyboard.type(text, delay=random.randint(25, 60))
    page.wait_for_timeout(random.randint(100, 220))


class BlankHandler(QuestionHandler):
    """填空题(直接输入文本).

    注:页面通常是"一个题目容器内多个 input"。
    我们把每个 input/textarea 视为一个独立的"空",
    顺序与 ParsedAnswer 列表对齐。
    """

    name = "blank"

    def detect(self, page: Page) -> List[ElementHandle]:
        # 找题目容器(含 input/textarea 但不含 radio/checkbox)
        try:
            blocks = page.query_selector_all(
                "div.question:has(input[type=text]):not(:has(input[type=radio])):not(:has(input[type=checkbox]))"
            )
        except Exception:
            blocks = []
        # 兜底:如果没匹配到容器,直接用裸 input
        if not blocks:
            try:
                blocks = page.query_selector_all("input[type=text], textarea")
            except Exception:
                blocks = []
        return [b for b in blocks if _safe_is_visible(b)]

    def fill(self, page: Page, element: ElementHandle, answer: ParsedAnswer) -> bool:
        if not answer.answers:
            return False
        text = str(answer.answers[0]).strip()
        if not text:
            return False
        try:
            tag = (element.evaluate("e => e.tagName") or "").upper()
            if tag in ("INPUT", "TEXTAREA"):
                _human_type(page, element, text)
                return True
            # 容器 -> 内部第一个 input
            inner = element.query_selector(", ".join(_INPUT_SELECTORS))
            if inner:
                _human_type(page, inner, text)
                return True
            # contenteditable
            element.click()
            page.wait_for_timeout(random.randint(80, 180))
            page.keyboard.type(text, delay=random.randint(25, 60))
            page.wait_for_timeout(random.randint(100, 220))
            return True
        except Exception as e:
            logger.warn(f"[{self.name}] 填入失败: {e}")
            return False


def _safe_is_visible(el: ElementHandle) -> bool:
    try:
        return el.is_visible()
    except Exception:
        return False
