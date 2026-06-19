"""ContentAPIAnswerSource — 老版 U校园 ``/api/content/`` 端点直取答案.

借鉴自 DMCSWCG/UnipusGetAnswer,但端点换成 v3 路径。

核心发现:
- U校园 SPA 用 ``/course/api/v3/content/{course}/{chapter}/default/`` 拿题目内容
- 响应是 ``{"code":0, "content":"unipus.<hex>"}``,**hex 是 AES-128-ECB 加密**
- 解密密钥 = ``"1a2b3c4d" + k``,k 通常在响应同层 JSON 里
- 解密出来的 JSON 里嵌套了完整题目内容,包括正确答案

提取策略:
1. 优先 AES 解密(看 ``content`` / ``data`` 字段是不是 ``unipus.`` 开头)
2. 走 JSON 树递归找 answer/answers/right_answer 等字段
3. 再不行回落到正则
"""
from __future__ import annotations

import json
import random
import re
from typing import Any, List, Optional

import requests
from playwright.sync_api import Page

from .. import logger
from ..crypto.jwt_hs256 import generate_auth_token
from ..unipus.api_client import ParsedAnswer, UnipusAPIClient
from .base import AnswerQuery, AnswerResult, AnswerSource
from .legacy_source import _read_localstorage_jwt, _resolve_legacy_url


CONTENT_API_BASE = "https://ucontent.unipus.cn/course/api/v3/content"
# 后备:DMCSWCG/UnipusGetAnswer 用的老路径,部分老部署仍然返回数据
CONTENT_API_BASE_LEGACY = "https://ucontent.unipus.cn/course/api/content"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_SEED = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


# ----------- JWT 三种生成方式 -----------


def _empty_signed_jwt() -> str:
    """空 secret 自签 JWT(DMCSWCG/UnipusGetAnswer 风格).

    服务端不验证签名,只要 JWT 格式合法即可,所以 secret 用空串也能过。
    """
    fake_id = "".join(random.choices(_SEED, k=32))
    return generate_auth_token(fake_id, secret="")


# ----------- 答案提取 -----------


_ANSWER_KEYS = (
    "answer",
    "answers",
    "right_answer",
    "rightAnswer",
    "correct_answer",
    "correctAnswer",
    "standard_answer",
    "standardAnswer",
    "std_answer",
    "stdAnswer",
    "ref_answer",
    "refAnswer",
    "reference_answer",
    "referenceAnswer",
    "model_answer",
    "modelAnswer",
    "key_answer",
    "keyAnswer",
)


def _walk_json(obj: Any, out: list[ParsedAnswer]) -> None:
    """递归遍历 JSON,把含答案字段的节点抓出来.

    支持多种字段命名:answer/answers/right_answer/correctAnswer/...
    """
    if isinstance(obj, dict):
        # 优先 list 形式的 answers
        for k in ("answers",):
            v = obj.get(k)
            if isinstance(v, list) and v:
                got = False
                for a in v:
                    a = _flatten_value(a)
                    if a:
                        out.append(ParsedAnswer(answers=[str(a)], id=len(out)))
                        got = True
                if got:
                    return
        # 单值答案字段
        for k in _ANSWER_KEYS:
            if k == "answers":
                continue  # 已经处理过
            if k in obj and obj.get(k) not in (None, ""):
                a = _flatten_value(obj[k])
                if a:
                    out.append(ParsedAnswer(answers=[str(a)], id=len(out)))
                    return
        for v in obj.values():
            _walk_json(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json(item, out)


def _flatten_value(v: Any) -> Any:
    if isinstance(v, dict):
        for k in ("text", "value", "content", "answer"):
            if v.get(k):
                return v[k]
    if isinstance(v, list):
        # 列表里的字典也试一下
        items = [_flatten_value(x) for x in v]
        items = [str(x) for x in items if x]
        return ", ".join(items) if items else None
    return v


def _regex_extract(text: str) -> list[ParsedAnswer]:
    """从原文本里用正则抓答案,兼容多种转义层次."""
    out: list[ParsedAnswer] = []

    # 1) 双转义 "answers":["..."]
    for m in re.findall(r'\\"answers\\":\[\\"(.*?)\\"\]', text):
        for piece in re.split(r'\\",\\"', m):
            piece = piece.replace('\\"', '"').strip()
            if piece:
                out.append(ParsedAnswer(answers=[piece], id=len(out)))

    if not out:
        # 2) 双转义 "answer":"..."
        for m in re.findall(r'\\"answer\\":\\"((?:[^"\\]|\\.)*?)\\"', text):
            piece = m.replace('\\"', '"').strip()
            if piece:
                out.append(ParsedAnswer(answers=[piece], id=len(out)))

    if not out:
        # 3) 普通 JSON 不转义
        for m in re.findall(r'"answers"\s*:\s*\[(.*?)\]', text, re.DOTALL):
            for s in re.findall(r'"((?:[^"\\]|\\.)*)"', m):
                if s:
                    out.append(ParsedAnswer(answers=[s], id=len(out)))

    if not out:
        for m in re.findall(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"', text):
            if m:
                out.append(ParsedAnswer(answers=[m], id=len(out)))
    return out


# ----------- AES 解密辅助 -----------


def try_aes_decrypt(
    payload: dict,
    *,
    extra_k_candidates: Optional[list[str]] = None,
) -> Optional[List[ParsedAnswer]]:
    """从一个 JSON dict 里找 ``unipus.<hex>`` 加密字段并解密.

    支持的字段名:data / content / answer / answers
    k 候选(都试一遍):
      - 同层 JSON 的 k / key / secret / iv / salt / nonce / kk 字段
      - 调用方传入的额外候选(URL / chapter / 课程 ID 切片等)
      - 一些常见的 8 字节固定值
      - decrypt 函数会自动 pad/truncate 到 16 字节,所以非 8 字节也会试
    """
    encrypted: Optional[str] = None
    enc_field = ""
    for field in ("data", "content", "answer", "answers", "result", "body"):
        v = payload.get(field)
        if isinstance(v, str) and v.startswith("unipus."):
            encrypted = v
            enc_field = field
            break
    if not encrypted:
        return None

    k_candidates: list[str] = []
    seen: set[str] = set()

    def add(k: str) -> None:
        if k not in seen:
            seen.add(k)
            k_candidates.append(k)

    # 同层 JSON 字段
    for kf in ("k", "key", "secret", "iv", "salt", "nonce", "kk", "sign"):
        v = payload.get(kf)
        if isinstance(v, str) and v:
            add(v)

    # 调用方注入的额外候选(URL/chapter 等)
    for k in extra_k_candidates or []:
        if k:
            add(k)

    # 一批常见的 8 字节(配合 "1a2b3c4d" 前缀凑 16 字节)
    for k in [
        "",
        "default",
        "default0",
        "00000000",
        "11111111",
        "12345678",
        "abcdefgh",
        "abcd1234",
        "unipus00",
        "unipusxx",
        "edx00000",
    ]:
        add(k)

    for k in k_candidates:
        try:
            decrypted = UnipusAPIClient.decrypt_data(encrypted, k)
        except Exception:
            continue
        if not decrypted:
            continue

        # 试 JSON 解析
        try:
            inner = json.loads(decrypted)
        except Exception:
            inner = None
        out: list[ParsedAnswer] = []
        if isinstance(inner, (dict, list)):
            _walk_json(inner, out)
        if out:
            logger.info(
                f"[aes-decrypt] 字段={enc_field} k={k!r} -> {len(out)} 条答案"
            )
            return out
        # 文本兜底
        text_ans = _regex_extract(decrypted) if len(decrypted) > 50 else []
        if text_ans:
            logger.info(
                f"[aes-decrypt] 字段={enc_field} k={k!r} -> 正则 {len(text_ans)} 条"
            )
            return text_ans

    # 全部失败,打印详细诊断
    keys_brief = []
    for k, v in payload.items():
        kind = type(v).__name__
        preview = ""
        if isinstance(v, str):
            preview = v[:60].replace("\n", " ")
            if len(v) > 60:
                preview += f"... (len={len(v)})"
        elif isinstance(v, (list, dict)):
            preview = f"{kind}({len(v)} items)"
        else:
            preview = repr(v)
        keys_brief.append(f"{k}={preview}")
    logger.warn(
        f"[aes-decrypt] 字段={enc_field!r} 是 unipus.* 加密格式,"
        f"但所有 k 候选({len(k_candidates)} 个)都解密失败。\n"
        f"  同层 JSON 顶层字段:\n  - " + "\n  - ".join(keys_brief)
    )
    return None


# ----------- AnswerSource 实现 -----------


class ContentAPIAnswerSource(AnswerSource):
    name = "content-api"

    def __init__(self, page: Page):
        self.page = page

    @property
    def available(self) -> bool:
        try:
            url = self.page.url
        except Exception:
            return False
        # 只对老版 U校园(course-v1: + ucontent.unipus.cn)启用
        return "ucontent.unipus.cn" in url and "course-v1:" in url

    def fetch(self, query: AnswerQuery) -> Optional[AnswerResult]:
        try:
            page_url = self.page.url
        except Exception:
            return None
        parts = _resolve_legacy_url(page_url)
        if not parts:
            logger.warn(f"[{self.name}] URL 不像老版,跳过: {page_url}")
            return None
        course, chapter = parts

        # 端点可能要的是真正的 hex task_id 而不是 u2g62 这种 URL 段。
        # 我们先用 chapter 试,再尝试从 sniffer 已抓到的请求里挖 hex task_id 试。
        candidate_tasks: list[tuple[str, str]] = [("chapter", chapter)]
        for hex_id in self._collect_hex_task_ids():
            candidate_tasks.append(("hex-from-sniff", f"/{hex_id}/"))

        # 三种 token 都试
        local_jwt = _read_localstorage_jwt(self.page)
        token_attempts: list[tuple[str, str]] = []
        if local_jwt:
            token_attempts.append(("localStorage.jwtToke", local_jwt))
        token_attempts.append(("empty-signed", _empty_signed_jwt()))
        token_attempts.append(("self-signed", generate_auth_token("")))

        # 关键:用 Playwright 的 request 上下文(继承浏览器 cookie/Origin/Referer)
        # 而不是 Python 的 requests 库 — 老版 U校园 后端会基于会话 cookie 二次鉴权,
        # 没 cookie 时给 200 + 空 body 当反爬。
        api_request = self.page.context.request

        # 端点优先 v3(当前 SPA 在用),失败回落到老路径(DMCSWCG 风格)
        api_bases = [("v3", CONTENT_API_BASE), ("legacy", CONTENT_API_BASE_LEGACY)]

        first_dump: Optional[tuple[str, str]] = None  # (url, body_preview)

        for base_label, base in api_bases:
            for task_label, task_seg in candidate_tasks:
                api_url = f"{base}{course}{task_seg}default/"
                for tok_label, token in token_attempts:
                    headers = {
                        "User-Agent": _USER_AGENT,
                        "Content-Type": "application/json",
                        "X-Annotator-Auth-Token": token,
                    }
                    try:
                        r = api_request.get(api_url, headers=headers, timeout=15000)
                    except Exception as e:
                        logger.warn(
                            f"[{self.name}] [{base_label}/{task_label}/{tok_label}] "
                            f"请求异常: {e}"
                        )
                        continue
                    try:
                        body_text = r.text() or ""
                    except Exception:
                        body_text = ""
                    logger.info(
                        f"[{self.name}] [{base_label}/{task_label}/{tok_label}] "
                        f"HTTP {r.status} len={len(body_text)} {api_url}"
                    )
                    if r.status != 200:
                        continue

                    if first_dump is None or (not first_dump[1] and body_text):
                        first_dump = (api_url, body_text[:1500])

                    if not body_text:
                        continue

                    # 1) 优先 AES 解密(content/data 字段是 unipus.<hex> 这种)
                    try:
                        payload = json.loads(body_text)
                        if isinstance(payload, dict):
                            # 诊断:打印响应顶层 key,看 k 字段叫什么
                            logger.info(
                                f"[{self.name}] [{base_label}/{task_label}] "
                                f"response keys={list(payload.keys())[:12]}"
                            )
                            extra_k = self._k_candidates_from_context(
                                course, chapter, page_url
                            )
                            ans = try_aes_decrypt(
                                payload, extra_k_candidates=extra_k
                            )
                            if ans:
                                return AnswerResult(
                                    answers=ans, source=f"{self.name}:aes"
                                )
                            # 普通 JSON 树
                            out: list[ParsedAnswer] = []
                            _walk_json(payload, out)
                            if out:
                                logger.info(
                                    f"[{self.name}] [{base_label}/{task_label}/{tok_label}] "
                                    f"JSON 树提取 {len(out)} 条答案"
                                )
                                return AnswerResult(
                                    answers=out, source=f"{self.name}:json"
                                )
                    except Exception:
                        pass

                    # 2) 正则兜底
                    ans = _regex_extract(body_text)
                    if ans:
                        logger.info(
                            f"[{self.name}] [{base_label}/{task_label}/{tok_label}] "
                            f"正则提取 {len(ans)} 条答案"
                        )
                        return AnswerResult(
                            answers=ans, source=f"{self.name}:regex"
                        )

        # 全部失败 — 强制 dump 第一份非空 200 响应
        if first_dump:
            url, preview = first_dump
            logger.warn(
                f"[{self.name}] 所有组合都没能提取答案。第一份 200 响应:\n"
                f"  url={url}\n"
                f"  body[:1500]={preview or '(空)'}"
            )
        else:
            logger.warn(f"[{self.name}] 所有组合都没拿到 200 响应")
        return None

    # --------- 辅助 ---------

    def _collect_hex_task_ids(self) -> list[str]:
        """从抓包队列里挖出可能的 hex task_id(32 位 hex).

        通常来自 ``/course/api/v2/course_progress/.../{HEX}/default`` 这种端点。
        """
        sniffer = self._lookup_sniffer()
        if sniffer is None:
            return []
        seen: list[str] = []
        seen_set: set[str] = set()
        for cap in sniffer.captures:
            url = cap.get("url", "")
            for m in re.findall(r"/([a-f0-9]{16,40})/", url, flags=re.IGNORECASE):
                if m not in seen_set:
                    seen_set.add(m)
                    seen.append(m)
        # 也从 JSON 响应体里挖(某些响应里的字段值是 hex)
        for cap in sniffer.captures[-30:]:  # 只看最近的,避免太慢
            text = cap.get("text") or ""
            for m in re.findall(r'"([a-f0-9]{24,40})"', text):
                if m not in seen_set:
                    seen_set.add(m)
                    seen.append(m)
        return seen[:8]  # 限制 8 个,避免试一上午

    def _lookup_sniffer(self):
        """从 page 拿挂载的 NetworkSniffer.

        这里走个间接桥梁:_build_resolver 把 sniffer 注入到 SniffAnswerSource。
        我们从 page 上偷一下,实在拿不到就返回 None。
        """
        # 先看是不是有人塞过到 page 上
        s = getattr(self.page, "_autounipus_sniffer", None)
        return s

    def _k_candidates_from_context(
        self, course: str, chapter: str, page_url: str
    ) -> list[str]:
        """从 URL / chapter / 抓包到的 JS 里挖可能的 AES k.

        重点:
        - chapter 各种变形(原值 / 8 字节填充 / 截短)
        - 课程 ID 后缀片段
        - URL query 参数 cid / appid / schId / eccId
        - 抓包里出现过的 24~40 位 hex(切前 8 / 后 8)
        - 常见的 page-level JS 中可能出现的 16 字节十六进制串
        """
        out: list[str] = []
        seen: set[str] = set()

        def push(s: str) -> None:
            if s and s not in seen:
                seen.add(s)
                out.append(s)

        # 1. chapter 直接 + 各种 padding
        ch = chapter.strip("/")
        push(ch)
        push(ch.ljust(8, "0")[:8])
        push(ch.rjust(8, "0")[:8])

        # 2. course id 后 8 / 前 8 字符
        course_clean = course.strip("/")
        if len(course_clean) >= 8:
            push(course_clean[-8:])
            push(course_clean[:8])

        # 3. URL query 参数
        try:
            from urllib.parse import urlparse, parse_qs
            p = urlparse(page_url)
            qs = parse_qs(p.query)
            for k in ("cid", "appid", "schId", "eccId", "classId"):
                v = qs.get(k, [""])[0]
                if v:
                    push(v)
                    push(v.ljust(8, "0")[:8])
        except Exception:
            pass

        # 4. 从抓包响应体里找出现频率高的 16/32 hex(可能就是密钥)
        sniffer = self._lookup_sniffer()
        if sniffer is not None:
            for cap in sniffer.captures[-30:]:
                text = cap.get("text") or ""
                for m in re.findall(r'\b([0-9a-f]{8,32})\b', text):
                    if 8 <= len(m) <= 16:
                        push(m)

        # 5. 试着挖 SPA 的 JS 文件里的硬编码 16-char hex 串
        try:
            js_keys = self._extract_keys_from_page_js()
            for k in js_keys:
                push(k)
        except Exception as e:
            logger.debug(f"[{self.name}] 挖 JS key 异常: {e}", enabled=True)

        return out[:24]  # 上限 24 个,避免试一上午

    def _extract_keys_from_page_js(self) -> list[str]:
        """从当前页面加载的 script 文件里 grep 出疑似 AES key 的硬编码值.

        U校园 SPA 的 bundle 可能 inline 这种字符串,例如:
            const KEY = "1a2b3c4ddefault0";
            decrypt(data, "abcdefgh");
        """
        js = """
        async () => {
            const out = new Set();
            const scripts = Array.from(document.scripts)
                .map(s => s.src).filter(s => s && s.includes('unipus.cn'));
            for (const url of scripts.slice(0, 3)) {
                try {
                    const r = await fetch(url);
                    const t = await r.text();
                    // 16 字节 hex / 字母数字串
                    const matches = t.match(/[\"\\\']([0-9a-f]{16}|[A-Za-z0-9_]{8,16})[\"\\\']/g) || [];
                    for (const m of matches.slice(0, 200)) {
                        out.add(m.replace(/[\"\\\']/g, ''));
                    }
                    if (out.size > 200) break;
                } catch (_) {}
            }
            return Array.from(out);
        }
        """
        try:
            arr = self.page.evaluate(js) or []
        except Exception:
            return []
        # 只取看着像 key 的(8-16 字符,不太长)
        return [s for s in arr if 8 <= len(s) <= 16][:60]
