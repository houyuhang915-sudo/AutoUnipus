"""答案数据源.

按优先级链式 fallback:
    ManualAnswerSource(用户预录答案库,最优先)
    ContentAPIAnswerSource(老版 U校园 /api/content/ 直取,通杀题型)
    SniffAnswerSource(浏览器抓包)
    APIAnswerSource(新版 U校园 /api/v3/answer/)
    LegacySubmitSource(老版 qid 答案 / 暴力反推 单选)
    AIAnswerSource(AI 兜底)
"""
from .ai_source import AIAnswerSource
from .api_source import APIAnswerSource
from .base import AnswerQuery, AnswerResult, AnswerSource
from .content_source import ContentAPIAnswerSource
from .legacy_source import LegacySubmitSource
from .manual_source import ManualAnswerSource

__all__ = [
    "AnswerQuery",
    "AnswerResult",
    "AnswerSource",
    "ManualAnswerSource",
    "ContentAPIAnswerSource",
    "APIAnswerSource",
    "LegacySubmitSource",
    "AIAnswerSource",
]
