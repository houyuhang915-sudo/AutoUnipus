"""从 Playwright Page 提取 courseInstanceId / taskId / openId.

参考 yuanarcsin/unipus_auto 的 extractPageInfo,但适配 Playwright Python:
- URL/path/hash 解析在 Python 端做
- localStorage / sessionStorage / window.* 全局读取通过 page.evaluate
- cookie 直接走 page.context.cookies()
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page


_HEX_ID = re.compile(r"^[a-f0-9]{12,40}$", re.IGNORECASE)
_UG_ID = re.compile(r"^u\d+g\d+$", re.IGNORECASE)  # U校园老版本章节 ID,如 u2g62
_COURSE_PREFIX = re.compile(r"^course-v[12]:")     # 同时支持 v1 / v2
_OPENID_PATTERNS = [
    re.compile(r'"open_?id"\s*:\s*"([^"]+)"', re.IGNORECASE),
]


@dataclass
class PageInfo:
    course_instance_id: Optional[str] = None
    task_id: Optional[str] = None
    open_id: Optional[str] = None

    def is_complete(self) -> bool:
        return bool(self.course_instance_id and self.task_id and self.open_id)

    def missing(self) -> list[str]:
        miss = []
        if not self.course_instance_id:
            miss.append("courseInstanceId")
        if not self.task_id:
            miss.append("taskId")
        if not self.open_id:
            miss.append("openId")
        return miss


def _parse_url_parts(url: str) -> tuple[list[str], dict[str, list[str]]]:
    """拆出 (所有路径段[含 hash], 合并的 query 字典)."""
    parsed = urlparse(url)
    path_parts = parsed.path.split("/") if parsed.path else []

    hash_str = parsed.fragment.lstrip("/")
    hash_query_idx = hash_str.find("?")
    if hash_query_idx >= 0:
        hash_path = hash_str[:hash_query_idx]
        hash_query = hash_str[hash_query_idx + 1:]
    else:
        hash_path = hash_str
        hash_query = ""

    hash_path_parts = hash_path.split("/") if hash_path else []
    all_parts = [p for p in (path_parts + hash_path_parts) if p]

    query = parse_qs(parsed.query)
    if hash_query:
        for k, v in parse_qs(hash_query).items():
            query.setdefault(k, []).extend(v)

    return all_parts, query


def _from_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """从 URL 提取 (courseInstanceId, taskId).

    同时支持两套 U校园:
    - 新版: hash 含 `course-v2:...` + 12+ hex 节点 ID
    - 老版: hash 含 `course-v1:...+xxx` + `u\\d+g\\d+` 章节 ID(如 u2g62)
    """
    all_parts, query = _parse_url_parts(url)

    course_id = None
    for part in all_parts:
        if _COURSE_PREFIX.match(part):
            course_id = part
            break

    # taskId 优先 hex(新版),否则取 u\d+g\d+ 的最后一个(老版)
    task_id: Optional[str] = None
    hex_ids = [p for p in all_parts if _HEX_ID.match(p) and p != course_id]
    if hex_ids:
        task_id = hex_ids[-1]
    else:
        ug_ids = [p for p in all_parts if _UG_ID.match(p)]
        if ug_ids:
            task_id = ug_ids[-1]

    if not task_id:
        for k in ("taskId", "nodeId", "task_id"):
            if k in query and query[k]:
                task_id = query[k][0]
                break
    if not course_id:
        for k in ("courseInstanceId", "courseId", "instanceId", "cid"):
            if k in query and query[k]:
                course_id = query[k][0]
                break

    return course_id, task_id


def _b64url_decode(s: str) -> bytes:
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _open_id_from_jwt_cookie(cookies: list[dict]) -> Optional[str]:
    for ck in cookies:
        if ck.get("name", "").lower() != "jwt":
            continue
        try:
            parts = ck["value"].split(".")
            if len(parts) != 3:
                continue
            payload = json.loads(_b64url_decode(parts[1]))
            if payload.get("openId"):
                return payload["openId"]
            if payload.get("open_id"):
                return payload["open_id"]
        except Exception:
            continue
    return None


def _open_id_from_named_cookies(cookies: list[dict]) -> Optional[str]:
    for ck in cookies:
        name = ck.get("name", "")
        if re.search(r"openid|open_id", name, re.IGNORECASE):
            val = ck.get("value", "")
            if val:
                return val
    return None


_STORAGE_KEYS = ["openId", "open_id", "u_openId", "uai_openId", "unipus_openId"]


def _open_id_from_storage(page: Page) -> Optional[str]:
    js = """
    (keys) => {
        for (const k of keys) {
            const v = (typeof localStorage !== 'undefined' && localStorage.getItem(k))
                   || (typeof sessionStorage !== 'undefined' && sessionStorage.getItem(k));
            if (v) return v;
        }
        return null;
    }
    """
    try:
        return page.evaluate(js, _STORAGE_KEYS)
    except Exception:
        return None


def _open_id_from_globals(page: Page) -> Optional[str]:
    js = """
    () => {
        const buckets = [
            window.__INITIAL_STATE__, window.__NUXT__, window.__NEXT_DATA__,
            window.__APP_STATE__, window.store
        ];
        for (const g of buckets) {
            if (!g) continue;
            try {
                const s = typeof g === 'string' ? g : JSON.stringify(g);
                const m = s.match(/"open_?id"\\s*:\\s*"([^"]+)"/i);
                if (m) return m[1];
            } catch (_) {}
        }
        return null;
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return None


def _ids_from_globals(page: Page) -> dict:
    """从 window.__INITIAL_STATE__ 等全局状态里抓 courseInstanceId / taskId."""
    js = """
    () => {
        const buckets = [
            window.__INITIAL_STATE__, window.__NUXT__, window.__NEXT_DATA__,
            window.__APP_STATE__, window.store
        ];
        const out = { courseInstanceId: null, taskId: null };
        for (const g of buckets) {
            if (!g) continue;
            try {
                const s = typeof g === 'string' ? g : JSON.stringify(g);
                if (!out.courseInstanceId) {
                    const m = s.match(/(?:courseInstanceId|course_instance_id|instanceId)\\s*[:=]\\s*"([^"]+)"/i);
                    if (m) out.courseInstanceId = m[1];
                }
                if (!out.taskId) {
                    const m = s.match(/(?:taskId|task_id|nodeId|node_id)\\s*[:=]\\s*"([^"]+)"/i);
                    if (m) out.taskId = m[1];
                }
                if (out.courseInstanceId && out.taskId) break;
            } catch (_) {}
        }
        return out;
    }
    """
    try:
        return page.evaluate(js) or {}
    except Exception:
        return {}


def _ids_from_dom(page: Page) -> dict:
    """从 [data-course-id] / [data-task-id] 等 DOM data 属性里抓."""
    js = """
    () => {
        const el = document.querySelector(
            '[data-course-id], [data-instance-id], [data-course-instance-id], [data-task-id], [data-node-id]'
        );
        if (!el) return { courseInstanceId: null, taskId: null };
        const ds = el.dataset || {};
        return {
            courseInstanceId: ds.courseId || ds.instanceId || ds.courseInstanceId || null,
            taskId: ds.taskId || ds.nodeId || null
        };
    }
    """
    try:
        return page.evaluate(js) or {}
    except Exception:
        return {}


def extract_page_info(page: Page) -> PageInfo:
    """从当前页面尽力提取出 courseInstanceId / taskId / openId.

    回退顺序与 yuanarcsin/unipus_auto 的 JS 实现保持一致:
        URL → window.* 全局状态 → DOM data-* 属性
    openId 走另一条链:JWT cookie → 通用 cookie → localStorage → 全局状态
    """
    info = PageInfo()
    info.course_instance_id, info.task_id = _from_url(page.url)

    # 从 window 全局状态补
    if not (info.course_instance_id and info.task_id):
        gids = _ids_from_globals(page)
        info.course_instance_id = info.course_instance_id or gids.get("courseInstanceId")
        info.task_id = info.task_id or gids.get("taskId")

    # 从 DOM data-* 属性补
    if not (info.course_instance_id and info.task_id):
        dids = _ids_from_dom(page)
        info.course_instance_id = info.course_instance_id or dids.get("courseInstanceId")
        info.task_id = info.task_id or dids.get("taskId")

    cookies = []
    try:
        cookies = page.context.cookies()
    except Exception:
        pass

    info.open_id = (
        _open_id_from_jwt_cookie(cookies)
        or _open_id_from_named_cookies(cookies)
        or _open_id_from_storage(page)
        or _open_id_from_globals(page)
    )
    return info
