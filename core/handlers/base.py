"""题型 handler 抽象.

设计原则:
- 每个 handler 负责一种题型的"识别"与"作答"
- 识别:在当前页面找出本类型的所有题目元素,按 DOM 顺序返回
- 作答:给定题目元素 + 一条 ParsedAnswer,把答案填进去

dispatcher.fill_all(page, answers) 会:
1. 让所有 handler 各自识别,按 DOM 顺序合并出统一的"题目列表"
2. 把答案数组按顺序对应进去,逐题调 handler.fill
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from playwright.sync_api import ElementHandle, Page

from ..unipus.api_client import ParsedAnswer


@dataclass
class DetectedQuestion:
    """一个被识别出来的题目."""
    handler_name: str
    element: ElementHandle
    # 由 Playwright 给出的 DOM 位置评分,用于跨 handler 的稳定排序
    position: int = 0


class QuestionHandler(ABC):
    """一种题型的处理器."""

    name: str = "base"

    @abstractmethod
    def detect(self, page: Page) -> List[ElementHandle]:
        """返回当前页面中本题型的所有题目元素(DOM 顺序)."""
        raise NotImplementedError

    @abstractmethod
    def fill(self, page: Page, element: ElementHandle, answer: ParsedAnswer) -> bool:
        """把答案填入题目.成功返回 True,不支持/失败返回 False."""
        raise NotImplementedError


class FillResult:
    """整个页面填答的结果摘要."""

    def __init__(self) -> None:
        self.filled = 0
        self.skipped = 0
        self.failed = 0
        self.unsupported_types: list[str] = []

    @property
    def total(self) -> int:
        return self.filled + self.skipped + self.failed

    def __repr__(self) -> str:
        return (
            f"FillResult(filled={self.filled}, skipped={self.skipped}, "
            f"failed={self.failed}, unsupported={self.unsupported_types})"
        )
