"""Legacy 答案源 — 老版 U校园 暴力反推方案 + qid-based 新接口尝试.

适用部署:
- URL 形如 ``/_pc_default/pc.html#/course-v1:.../courseware/u2/u2g60/u2g62/u2g62/p_1``
- 答案接口 ``/v3/answer/{course}/{u2g62}/default`` 返回 ``code=1 参数错误``
  (因为 task_id 应该传 qid 而不是章节段)

策略(按优先级):
1. 用 ``/api/pc/summary/{course}/{u2g62}/default/`` 拿到 qid 列表
2. **优先用 qid** 调 ``/v3/answer/{course}/{qid}/default`` —— 老版 answer 端点的真身
3. 否则回落到 ``POST /api/v3/submit/...`` 暴力反推 isRight(只对单选)

原暴力反推原理:
    1. 全部题填 'A' 提交 → 服务器返回每题 isRight
    2. 答错的题改成下一个字母(B / C / D ...)再提交
    3. 直到所有 isRight=True,此时记录的字母就是正确答案

局限:
- 暴力反推**只支持单选**。多选 / 选词填空 / 填空 / 翻译 全部失败。
- qid-based 答案接口若服务端实现了,可能覆盖所有题型。
"""
from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

import requests
from playwright.sync_api import Page

from .. import logger
from ..unipus.api_client import ParsedAnswer, UnipusAPIClient, UnipusAPIError
from .base import AnswerQuery, AnswerResult, AnswerSource


SUMMARY_BASE = "https://ucontent.unipus.cn/course/api/pc/summary"
SUBMIT_BASE = "https://ucontent.unipus.cn/course/api/v3/submit"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_LETTERS = "ABCDEFG"

# 老版 URL 里的"章节段",形如 /u2g62/
_CHAPTER_RE = re.compile(r"/u\d+g\d+/", re.IGNORECASE)
# 老版 URL 里的"课程"段:#后面 到 /courseware 之前
_COURSE_RE = re.compile(r"(?<=#).+?(?=/courseware)")


def _resolve_legacy_url(url: str) -> Optional[Tuple[str, str]]:
    """从老版 URL 抽出 (course, chapter) 两个原始字符串.

    course   形如 ``/course-v1:Unipus+nhce_3_rw_2_sz+2020_09``(带前导斜杠)
    chapter  形如 ``/u2g62/``(带前后斜杠)
    """
    course_m = _COURSE_RE.search(url)
    if not course_m:
        return None
    chapters = _CHAPTER_RE.findall(url)
    if not chapters:
        return None
    course = course_m.group(0)
    if not course.startswith("/"):
        course = "/" + course
    chapter = chapters[-1]
    return course, chapter


def _read_localstorage_jwt(page: Page) -> Optional[str]:
    js = """
    () => {
        const candidates = ['jwtToke', 'jwtToken', 'jwt'];
        for (const k of candidates) {
            const v = (typeof localStorage !== 'undefined' && localStorage.getItem(k))
                   || (typeof sessionStorage !== 'undefined' && sessionStorage.getItem(k));
            if (v) return v;
        }
        return null;
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return None


# ---------------- 答案反推核心(从 res/fetcher.py 移植) ---------------- #


def _flatten_qids(index_map: dict) -> List[str]:
    out: list[str] = []
    for outer in (index_map or {}).values():
        if not isinstance(outer, dict):
            continue
        for inner in outer.values():
            qid = (inner or {}).get("qid")
            if qid:
                out.append(qid)
    return out


def _sort_ans(resp_json: dict, num: int) -> List[dict]:
    content = ((resp_json.get("data") or {}).get("user_answers")) or {}
    answer = []
    for i in range(num):
        item = content.get(str(i)) or {}
        answer.append(
            {
                "choice": item.get("student_answer"),
                "isRight": bool(item.get("isRight")),
            }
        )
    return answer


def _change_ans(answer: List[dict]) -> Tuple[List[dict], bool]:
    """把答错的题答案换成下一个字母.返回 (answer, all_right)."""
    all_right = True
    for ans in answer:
        if ans["isRight"]:
            continue
        all_right = False
        cur = (ans.get("choice") or "A").upper()
        idx = _LETTERS.find(cur)
        if idx < 0 or idx + 1 >= len(_LETTERS):
            # 已经试到 G 还不对,放弃这道
            continue
        ans["choice"] = _LETTERS[idx + 1]
    return answer, all_right


def _change_data(answer: List[dict], data: dict) -> None:
    for i, ans in enumerate(answer):
        slot = data["answers"].get(str(i))
        if slot is None:
            continue
        slot["user_answer"]["answer"] = {"index": i, "answer": ans.get("choice") or "A"}


# ---------------- AnswerSource 实现 ---------------- #


class LegacySubmitSource(AnswerSource):
    name = "unipus-legacy"

    # 单题最多重试到第几个字母(超过就放弃)
    MAX_LETTER_ROUNDS = len(_LETTERS)

    def __init__(self, page: Page):
        self.page = page

    @property
    def available(self) -> bool:
        # 只有 URL 像老版才启用,避免对新版重复请求
        url = ""
        try:
            url = self.page.url
        except Exception:
            pass
        return _resolve_legacy_url(url) is not None

    # ------------- 核心 -------------

    def fetch(self, query: AnswerQuery) -> Optional[AnswerResult]:
        url = ""
        try:
            url = self.page.url
        except Exception:
            pass
        parts = _resolve_legacy_url(url)
        if not parts:
            logger.warn(f"[{self.name}] URL 不像老版,跳过: {url}")
            return None
        course, chapter = parts

        token = _read_localstorage_jwt(self.page)
        if not token:
            logger.warn(f"[{self.name}] 拿不到 localStorage.jwtToke,跳过")
            return None

        headers = {
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "X-Annotator-Auth-Token": token,
        }
        # 关键:走 Playwright 的 request 上下文(继承浏览器 cookie),
        # 否则老版 U校园 后端会因为缺会话 cookie 直接给 200 + 空 body
        api_request = self.page.context.request

        # 1) 取 qids
        qids = self._fetch_qids(course, chapter, headers, api_request)
        if not qids:
            logger.warn(f"[{self.name}] summary 接口没返回 qid")
            return None
        logger.info(f"[{self.name}] 取到 {len(qids)} 个 qid")

        # 2) 优先用 qid 调新版 /v3/answer/{course}/{qid}/default
        qid_answers = self._try_qid_answer_endpoint(query, qids)
        if qid_answers:
            logger.info(
                f"[{self.name}] qid-based answer 接口拿到 {len(qid_answers)} 条答案"
            )
            return AnswerResult(
                answers=qid_answers, source=f"{self.name}:qid-answer"
            )

        # 3) 回落到 submit 暴力反推(只对单选有用)
        logger.info(f"[{self.name}] 走暴力反推(仅单选)")
        all_answers: list[ParsedAnswer] = []
        submit_url = f"{SUBMIT_BASE}{course}{chapter}"
        for qid in qids:
            sub = self._reverse_one_qid(submit_url, qid, headers, api_request, total=1)
            if sub is None:
                continue
            for ans in sub:
                if ans.get("choice"):
                    all_answers.append(
                        ParsedAnswer(answers=[ans["choice"]], id=len(all_answers))
                    )

        if not all_answers:
            logger.warn(f"[{self.name}] 没反推出任何单选答案(可能本练习没有单选)")
            return None
        return AnswerResult(answers=all_answers, source=f"{self.name}:brute-force")

    # ------------- qid-based 新接口 -------------

    def _try_qid_answer_endpoint(
        self, query: AnswerQuery, qids: list[str]
    ) -> Optional[list[ParsedAnswer]]:
        """逐个 qid 尝试 /v3/answer/{course}/{qid}/default,聚合所有答案."""
        client = UnipusAPIClient(self.page)
        # openId 缺失时,先 fallback 一次
        open_id = query.open_id
        if not open_id:
            open_id = client.fetch_open_id() or ""

        merged: list[ParsedAnswer] = []
        any_success = False
        for qid in qids:
            try:
                ans = client.get_answers(
                    query.course_instance_id, qid, open_id
                )
            except UnipusAPIError as e:
                logger.debug(
                    f"[{self.name}] qid={qid[:8]}.. answer 接口失败: {e}",
                    enabled=True,
                )
                continue
            except Exception as e:
                logger.debug(
                    f"[{self.name}] qid={qid[:8]}.. 异常: {e}",
                    enabled=True,
                )
                continue
            if ans:
                any_success = True
                for a in ans:
                    merged.append(
                        ParsedAnswer(answers=a.answers, id=len(merged))
                    )
        return merged if (any_success and merged) else None

    # ------------- 工具 -------------

    def _fetch_qids(
        self, course: str, chapter: str, headers: dict, api_request
    ) -> List[str]:
        url = f"{SUMMARY_BASE}{course}{chapter}default/"
        try:
            r = api_request.get(url, headers=headers, timeout=15000)
        except Exception as e:
            logger.warn(f"[{self.name}] summary 请求异常: {e}")
            return []
        try:
            text = r.text() or ""
        except Exception:
            text = ""
        logger.info(
            f"[{self.name}] summary -> HTTP {r.status} len={len(text)} {url}"
        )
        if r.status != 200:
            return []
        try:
            payload = json.loads(text) if text else {}
        except Exception:
            return []
        summary = payload.get("summary") or payload.get("data") or {}
        index_map = summary.get("indexMap") if isinstance(summary, dict) else None
        return _flatten_qids(index_map or {})

    def _reverse_one_qid(
        self, submit_url: str, qid: str, headers: dict, api_request, *, total: int
    ) -> Optional[List[dict]]:
        """暴力反推单个 qid 的所有小题答案."""
        data = {
            "answers": {
                str(i): {
                    "user_answer": {
                        "qid": qid,
                        "answer": {"index": i, "answer": "A"},
                    }
                }
                for i in range(total)
            }
        }
        rounds = 0
        while rounds < self.MAX_LETTER_ROUNDS:
            try:
                r = api_request.post(
                    submit_url,
                    data=json.dumps(data),
                    headers=headers,
                    timeout=15000,
                )
            except Exception as e:
                logger.warn(f"[{self.name}] submit 请求异常: {e}")
                return None
            if r.status != 200:
                logger.warn(
                    f"[{self.name}] submit qid={qid[:8]}... HTTP {r.status}"
                )
                return None
            try:
                resp_json = json.loads(r.text() or "")
            except Exception:
                logger.warn(f"[{self.name}] submit 返回非 JSON")
                return None

            answer = _sort_ans(resp_json, total)
            answer, all_right = _change_ans(answer)
            if all_right:
                return answer
            _change_data(answer, data)
            rounds += 1
        logger.warn(
            f"[{self.name}] qid={qid[:8]}... 试到 G 还没全对,放弃"
        )
        return None
