"""API 直取 答案源.

调用 U校园官方 /course/api/v3/answer/.../default 接口,客户端 AES 解密。
准确率 100%,但依赖硬编码的 JWT secret 与 AES key prefix。
"""
from __future__ import annotations

from typing import Optional

from .. import logger
from ..unipus.api_client import UnipusAPIClient, UnipusAPIError
from .base import AnswerQuery, AnswerResult, AnswerSource


class APIAnswerSource(AnswerSource):
    name = "unipus-api"

    def __init__(self, client: UnipusAPIClient):
        self.client = client

    def fetch(self, query: AnswerQuery) -> Optional[AnswerResult]:
        if not (query.course_instance_id and query.task_id):
            logger.warn(
                f"[{self.name}] 跳过:缺少 "
                f"courseInstanceId={query.course_instance_id!r} "
                f"taskId={query.task_id!r}"
            )
            return None

        open_id = query.open_id
        if not open_id:
            # 回退:从 uai 子域获取
            open_id = self.client.fetch_open_id() or ""
            if open_id:
                logger.info(f"[{self.name}] 通过 /api/account/user/info 拿到 openId")
        if not open_id:
            logger.warn(f"[{self.name}] 无法获取 openId,跳过")
            return None

        try:
            answers = self.client.get_answers(
                query.course_instance_id, query.task_id, open_id
            )
        except UnipusAPIError as e:
            logger.warn(f"[{self.name}] 获取失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[{self.name}] 异常: {e}")
            return None

        if not answers:
            logger.warn(f"[{self.name}] 接口返回空答案集")
            return None

        return AnswerResult(answers=answers, source=self.name)
