"""答案数据源抽象."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from ..unipus.api_client import ParsedAnswer


@dataclass
class AnswerQuery:
    """一次答案获取的输入参数."""
    course_instance_id: str
    task_id: str
    open_id: str
    # 题目原始文本(供 AI 源使用,API 源用不到)
    questions: list[str] = field(default_factory=list)
    # 页面可见标题(供 ManualAnswerSource 模糊匹配),例如:
    # "1-2 Text A: Language focus | Word building: Practice"
    title: str = ""


@dataclass
class AnswerResult:
    """获取结果."""
    answers: List[ParsedAnswer]
    source: str          # 数据源名,日志用
    cache_hit: bool = False
    # 是否允许写入答案缓存。AI 答主观题等"无机器评分"场景应设为 False,
    # 否则一次错误回答会被缓存永久污染下一轮。
    cacheable: bool = True

    @property
    def ok(self) -> bool:
        return bool(self.answers)


class AnswerSource(ABC):
    """所有数据源的统一接口."""

    name: str = "base"

    @abstractmethod
    def fetch(self, query: AnswerQuery) -> Optional[AnswerResult]:
        """获取答案.失败返回 None,让上层 fallback 到下一个源."""
        raise NotImplementedError

    @property
    def available(self) -> bool:
        """是否可用(配置齐全等)."""
        return True
