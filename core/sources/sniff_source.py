"""SniffAnswerSource — 从浏览器自身网络请求里翻答案.

与其猜 U校园 后端的端点/参数,不如**让 U校园 自己的前端去发请求**,
我们用 Playwright 的 response 钩子把所有疑似答案的响应缓存下来,
再按几种已知模式提取。

支持的提取模式:
- ``data`` / ``content`` 字段以 ``unipus.`` 开头 + AES-128-ECB 解密
  (k 优先取同层 JSON 中的 k 字段,兜底用空串 / default 等)
- ``data.user_answers[*].right_answer`` / ``correct_answer`` / ``standard_answer``
- ``summary.indexMap[*][*]`` 内含 answer / right_answer 字段
- 顶层 ``answers`` 数组
"""
from __future__ import annotations

import json
from typing import Any, List, Optional

from playwright.sync_api import Page

from .. import logger
from ..unipus.api_client import ParsedAnswer, UnipusAPIClient, parse_answers
from ..unipus.sniffer import NetworkSniffer
from .base import AnswerQuery, AnswerResult, AnswerSource
from .content_source import _walk_json, _regex_extract, try_aes_decrypt


class SniffAnswerSource(AnswerSource):
    name = "network-sniff"

    def __init__(self, page: Page, sniffer: NetworkSniffer):
        self.page = page
        self.sniffer = sniffer

    # --------------------------------------------------

    def fetch(self, query: AnswerQuery) -> Optional[AnswerResult]:
        # 给页面一点时间发起初始请求
        try:
            self.page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass

        captures = self.sniffer.captures
        if not captures:
            logger.warn(f"[{self.name}] 抓包队列为空,跳过")
            return None

        # 只看本道题相关的响应(URL 中包含 task_id 或 course_instance_id)
        related = [
            c for c in captures
            if (
                query.task_id and query.task_id in c["url"]
            ) or (
                query.course_instance_id
                and query.course_instance_id.replace(":", "%3A") in c["url"]
            ) or (
                query.course_instance_id and query.course_instance_id in c["url"]
            )
        ]
        if not related:
            related = captures  # 兜底:全部都看

        # 倒序遍历:更晚的响应通常更接近"真相"
        for cap in reversed(related):
            j = cap.get("json")
            if not isinstance(j, dict):
                continue

            for extractor, label in (
                (_extract_aes, "aes-encrypted"),
                (_extract_user_answers, "user_answers"),
                (_extract_summary_index, "summary.indexMap"),
                (_extract_top_answers, "top.answers"),
            ):
                ans = extractor(j)
                if ans:
                    logger.info(
                        f"[{self.name}] 模式={label} 来源={cap['url'][:100]} "
                        f"提取 {len(ans)} 条答案"
                    )
                    return AnswerResult(answers=ans, source=f"{self.name}:{label}")

        # 都没命中,把抓到的 URL + 响应体 dump 出来方便诊断
        logger.warn(
            f"[{self.name}] 抓到 {len(related)} 个相关响应,但都无法提取答案。"
            f"先列出全部 URL 供诊断:"
        )
        for cap in related:
            logger.warn(
                f"  {cap['status']} {cap['method']} {cap['url'][:160]}"
            )
        # 再 dump 后 6 条的响应体
        logger.warn(f"[{self.name}] 最后 6 条的响应体片段:")
        for cap in list(related)[-6:]:
            body_text = cap.get("text") or ""
            preview = body_text[:1000]
            if preview:
                logger.warn(f"  ---\n  {cap['url'][:120]}\n  body: {preview}")
        return None


# ============== 提取模式实现 ==============


def _extract_aes(j: dict) -> Optional[List[ParsedAnswer]]:
    """委托 content_source.try_aes_decrypt — 找 unipus.<hex> 加密字段并解密."""
    return try_aes_decrypt(j)


def _extract_user_answers(j: dict) -> Optional[List[ParsedAnswer]]:
    """老版 submit 响应:data.user_answers[index].right_answer/correct_answer."""
    data = j.get("data")
    if not isinstance(data, dict):
        return None
    ua = data.get("user_answers")
    if not isinstance(ua, dict):
        return None

    out: list[ParsedAnswer] = []
    keys = sorted(
        ua.keys(),
        key=lambda x: int(x) if isinstance(x, str) and x.isdigit() else 0,
    )
    for k in keys:
        v = ua.get(k)
        if not isinstance(v, dict):
            continue
        ra = (
            v.get("right_answer")
            or v.get("correct_answer")
            or v.get("standard_answer")
            or v.get("ref_answer")
        )
        ra = _flatten(ra)
        if ra:
            out.append(ParsedAnswer(answers=[str(ra)], id=len(out)))
    return out or None


def _extract_summary_index(j: dict) -> Optional[List[ParsedAnswer]]:
    """summary.indexMap.<sec>.<q>.{answer | right_answer | analysis}."""
    summary = j.get("summary") or (
        j.get("data", {}).get("summary") if isinstance(j.get("data"), dict) else None
    )
    if not isinstance(summary, dict):
        return None
    idx = summary.get("indexMap")
    if not isinstance(idx, dict):
        return None

    out: list[ParsedAnswer] = []
    for outer in idx.values():
        if not isinstance(outer, dict):
            continue
        for inner in outer.values():
            if not isinstance(inner, dict):
                continue
            ans = (
                inner.get("right_answer")
                or inner.get("correct_answer")
                or inner.get("answer")
                or inner.get("analysis")
            )
            ans = _flatten(ans)
            if ans:
                out.append(ParsedAnswer(answers=[str(ans)], id=len(out)))
    return out or None


def _extract_top_answers(j: dict) -> Optional[List[ParsedAnswer]]:
    """顶层 answers 数组,例如某些 review 接口."""
    arr = j.get("answers") or (
        j.get("data", {}).get("answers") if isinstance(j.get("data"), dict) else None
    )
    if not isinstance(arr, list) or not arr:
        return None

    out: list[ParsedAnswer] = []
    for item in arr:
        if isinstance(item, dict):
            ans = (
                item.get("right_answer")
                or item.get("correct_answer")
                or item.get("answer")
                or item.get("analysis")
            )
        else:
            ans = item
        ans = _flatten(ans)
        if ans:
            out.append(ParsedAnswer(answers=[str(ans)], id=len(out)))
    return out or None


def _flatten(v: Any) -> Any:
    """把 {"answer": "..."} / {"text": "..."} 这种嵌套层剥掉一层."""
    if isinstance(v, dict):
        for k in ("answer", "text", "value", "content"):
            if k in v and v[k]:
                return v[k]
    return v
