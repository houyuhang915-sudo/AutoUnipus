"""网络抓包.

订阅 Playwright Page / BrowserContext 的 response 事件,
缓存所有看起来跟"答案"相关的响应,后续由 SniffAnswerSource 翻找。

只关心 ucontent.unipus.cn / uai.unipus.cn 域,且 URL 含
answer/analysis/submit/summary/task/exercise/question 任一关键字。
"""
from __future__ import annotations

import re
import time
from collections import deque
from typing import Any, Optional

from playwright.sync_api import Page, Response


_DOMAIN_RE = re.compile(r"(ucontent|uai|u)\.unipus\.cn", re.IGNORECASE)
# 域内只过滤掉明显的静态资源,其它一律收
_STATIC_RE = re.compile(
    r"\.(js|css|png|jpe?g|gif|svg|woff2?|ttf|ico|map|html?)(\?|$)",
    re.IGNORECASE,
)


class NetworkSniffer:
    """挂在 Page 上的响应嗅探器."""

    def __init__(self, max_captures: int = 500):
        self._captures: deque[dict[str, Any]] = deque(maxlen=max_captures)
        self._attached_pages: set[int] = set()

    # ------------- 安装 / 卸载 -------------

    def attach(self, page: Page) -> None:
        """给一个 page 装上响应监听.重复 attach 同一个 page 不会重复装."""
        key = id(page)
        if key in self._attached_pages:
            return
        self._attached_pages.add(key)
        page.on("response", self._on_response)

    # ------------- 数据 -------------

    @property
    def captures(self) -> list[dict[str, Any]]:
        return list(self._captures)

    def reset(self) -> None:
        self._captures.clear()

    def matching(self, pattern: str) -> list[dict[str, Any]]:
        rx = re.compile(pattern, re.IGNORECASE)
        return [c for c in self._captures if rx.search(c.get("url", ""))]

    # ------------- 响应处理 -------------

    def _on_response(self, response: Response) -> None:
        try:
            url = response.url
        except Exception:
            return
        if not _DOMAIN_RE.search(url):
            return
        # 过滤明显的静态资源,但保留所有 XHR/fetch
        if _STATIC_RE.search(url):
            return
        try:
            rtype = response.request.resource_type
        except Exception:
            rtype = ""
        if rtype not in ("xhr", "fetch", ""):
            return

        # 取 body(尽量解析 JSON,失败就保留原文本)
        body_json: Any = None
        body_text: Optional[str] = None
        try:
            body_text = response.text()
        except Exception:
            pass
        if body_text:
            try:
                import json as _json
                body_json = _json.loads(body_text)
            except Exception:
                body_json = None

        self._captures.append(
            {
                "url": url,
                "status": response.status,
                "method": getattr(response.request, "method", "?"),
                "text": body_text,
                "json": body_json,
                "ts": time.time(),
            }
        )
