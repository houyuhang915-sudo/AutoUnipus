"""题型注册表 + 跨题型分发器.

dispatch 流程:
1. 按优先级让每个 handler 各自 detect,得到 (handler, element, bbox) 三元组
2. 用 bounding-box "祖先包含" 规则去重(后注册的若被前面已认领的容器包住就丢弃)
3. 按 Y 坐标(再用 X 兜底)排序得到题目顺序
4. 顺序对齐 answers,逐一调 handler.fill
"""
from __future__ import annotations

from typing import List, Optional

from playwright.sync_api import ElementHandle, Page

from .. import logger
from ..unipus.api_client import ParsedAnswer
from .base import FillResult, QuestionHandler
from .blank import BlankHandler
from .multi_choice import MultiChoiceHandler
from .single_choice import SingleChoiceHandler
from .translation import TranslationHandler
from .word_blank import WordBlankHandler


# 顺序就是优先级:更具体的题型放前面
DEFAULT_HANDLERS: list[QuestionHandler] = [
    SingleChoiceHandler(),
    MultiChoiceHandler(),
    WordBlankHandler(),
    BlankHandler(),
    TranslationHandler(),
]


def _bbox(el: ElementHandle) -> Optional[dict]:
    try:
        return el.bounding_box()
    except Exception:
        return None


def _is_inside(inner: dict, outer: dict, tol: float = 1.0) -> bool:
    return (
        outer["x"] - tol <= inner["x"]
        and outer["y"] - tol <= inner["y"]
        and outer["x"] + outer["width"] + tol >= inner["x"] + inner["width"]
        and outer["y"] + outer["height"] + tol >= inner["y"] + inner["height"]
    )


def detect_all(page: Page, handlers: list[QuestionHandler]) -> list[tuple[QuestionHandler, ElementHandle, dict]]:
    """跨 handler 检测 + 去重,返回按 DOM 顺序排好的 [(handler, element, bbox)]."""
    claimed: list[tuple[QuestionHandler, ElementHandle, dict]] = []
    for h in handlers:
        try:
            elements = h.detect(page)
        except Exception as e:
            logger.warn(f"[{h.name}] detect 异常: {e}")
            continue
        for el in elements:
            bbox = _bbox(el)
            if not bbox or bbox.get("width", 0) <= 0 or bbox.get("height", 0) <= 0:
                continue
            # 是否落在已认领容器内?是则跳过(避免重复填)
            if any(_is_inside(bbox, c[2]) for c in claimed):
                continue
            claimed.append((h, el, bbox))

    claimed.sort(key=lambda t: (round(t[2]["y"]), round(t[2]["x"])))
    return claimed


def fill_page(
    page: Page,
    answers: list[ParsedAnswer],
    handlers: Optional[list[QuestionHandler]] = None,
) -> FillResult:
    """按答案数组顺序填充当前页面."""
    handlers = handlers or DEFAULT_HANDLERS
    detected = detect_all(page, handlers)

    if not detected:
        logger.warn(
            f"[dispatch] 页面没识别到任何题目元素,handler={[h.name for h in handlers]} "
            f"全都没匹配到 selector — 这页 DOM 可能跟我们 handler 假设不一样"
        )

    result = FillResult()
    for i, (handler, element, _) in enumerate(detected):
        if i >= len(answers):
            logger.warn(
                f"[dispatch] 页面题目 {i + 1} 无对应答案(总答案 {len(answers)} 条),已跳过"
            )
            result.skipped += 1
            continue
        ans = answers[i]
        ans_preview = (ans.answers[0] if ans.answers else "")[:40]
        try:
            ok = handler.fill(page, element, ans)
        except Exception as e:
            logger.warn(f"[{handler.name}] fill 异常: {e}")
            ok = False
        if ok:
            logger.debug(
                f"[dispatch] #{i+1} {handler.name} 填入 {ans_preview!r} ✓",
                enabled=True,
            )
            result.filled += 1
        else:
            logger.warn(
                f"[dispatch] #{i+1} {handler.name} 填入 {ans_preview!r} 失败"
            )
            result.failed += 1
            if handler.name not in result.unsupported_types:
                result.unsupported_types.append(handler.name)

    if len(answers) > len(detected):
        extra = len(answers) - len(detected)
        logger.warn(
            f"[dispatch] 答案多于页面题目 {extra} 条(可能存在不支持题型,或 handler selector 不对),已跳过"
        )
        result.skipped += extra

    return result
