"""单选题 handler.

兼容多种 U校园页面:
- React build 的 .questions--questionDefault-XXX(原项目用)
- 通用结构:含 input[type=radio][value=A/B/...]
"""
from __future__ import annotations

from typing import List

from playwright.sync_api import ElementHandle, Page

from .. import logger
from ..unipus.api_client import ParsedAnswer
from .base import QuestionHandler


_SELECTORS = [
    # 旧版本 minified 选择器(原项目)
    ".questions--questionDefault-2XLzl.undefined",
    # 通用兜底:含 radio 输入的题目容器
    "div.question:has(input[type=radio])",
    "[class*=questionDefault]",
]


def _normalize_choice(raw: str) -> str:
    """把答案标准化为 'A' / 'B' / ... 单字符."""
    if not raw:
        return ""
    s = raw.strip().upper()
    # 服务器有时返回 "A. xxx" 或 "正确答案:A",抓首个字母
    for ch in s:
        if "A" <= ch <= "G":
            return ch
    return s


class SingleChoiceHandler(QuestionHandler):
    name = "single-choice"

    def detect(self, page: Page) -> List[ElementHandle]:
        seen: list[ElementHandle] = []
        seen_ids: set[str] = set()
        for sel in _SELECTORS:
            try:
                for el in page.query_selector_all(sel):
                    # 关键:**必须真的含有 radio 输入**,不然不算单选题
                    # 否则像 [class*=questionDefault] 这种宽 selector
                    # 会把所有题目容器(包括简答 textarea / 填空)抢走
                    try:
                        if not el.query_selector("input[type=radio]"):
                            continue
                    except Exception:
                        continue
                    # 去重:用 outerHTML 长度+前 80 字 hash 简单识别
                    key = (el.evaluate("e => e.getBoundingClientRect().top + ',' + e.tagName") or "")
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                    seen.append(el)
            except Exception:
                continue
        # 过滤不可见
        return [el for el in seen if _safe_is_visible(el)]

    def fill(self, page: Page, element: ElementHandle, answer: ParsedAnswer) -> bool:
        if not answer.answers:
            return False
        choice = _normalize_choice(str(answer.answers[0]))
        if not choice:
            logger.warn(f"[{self.name}] 答案无法解析: {answer.answers!r}")
            return False
        try:
            radio = element.wait_for_selector(
                f'input[type=radio][value="{choice}"]', timeout=2000
            )
            if radio is None:
                return False
            radio.click(timeout=1500)
            return True
        except Exception as e:
            logger.warn(f"[{self.name}] 点击 {choice} 失败: {e}")
            return False


def _safe_is_visible(el: ElementHandle) -> bool:
    try:
        return el.is_visible()
    except Exception:
        return False
