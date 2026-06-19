"""U校园 答案接口客户端.

两个关键端点:
1. GET /course/api/v3/answer/{courseInstanceId}/{taskId}/default
   - 返回 {code, data, k},data 是 "unipus." + AES-128-ECB hex 密文
   - 解密密钥 = "1a2b3c4d" + k (k 由响应给出)

2. https://uai.unipus.cn/api/account/user/info
   - 用作 openId 的回退获取(跨子域,需要走 page.request)

认证策略:
- 老版本 U校园 服务端只认登录后写到 localStorage 的 jwtToke
- 新版本 / yuanarcsin 项目用客户端硬编码 secret 自签 JWT
- 我们两套都试,优先 localStorage,失败再自签
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, List, Optional

from playwright.sync_api import APIRequestContext, Page

from .. import logger
from ..crypto.aes_ecb import decrypt_aes128_ecb_zero_pad
from ..crypto.jwt_hs256 import generate_auth_token


ANSWER_API_BASE = "https://ucontent.unipus.cn/course/api/v3/answer"
USER_INFO_API = "https://uai.unipus.cn/api/account/user/info"
_AES_KEY_PREFIX = "1a2b3c4d"  # 与 secret 一样硬编码


class UnipusAPIError(RuntimeError):
    """答案接口调用失败."""


@dataclass
class ParsedAnswer:
    """单题答案的扁平化结构.

    answers 在多 children 的题型(如 4 个填空)里会被展开成多条,
    每条对应页面上一个独立的输入位/选项。
    """
    answers: List[str]
    id: int = 0


def _request_ctx(page_or_ctx: Page | APIRequestContext) -> APIRequestContext:
    """从 Page 或直接的 APIRequestContext 取出请求上下文."""
    if isinstance(page_or_ctx, APIRequestContext):
        return page_or_ctx
    return page_or_ctx.context.request


def _read_localstorage_jwt(page: Page) -> Optional[str]:
    """读 localStorage.jwtToke(老版 U校园 登录后写入的会话 JWT).

    早期 U校园 客户端用的就是这个 token 作为 X-Annotator-Auth-Token,
    比自签 JWT 更稳。
    """
    js = """
    () => {
        const candidates = ['jwtToke', 'jwtToken', 'jwt', 'unipus_jwt'];
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


class UnipusAPIClient:
    """轻量答案 API 客户端.

    复用 Playwright BrowserContext 的请求上下文以继承登录后的 cookie。
    """

    def __init__(self, page: Page):
        self.page = page
        self._req: APIRequestContext = page.context.request

    # ------------------ 原始接口 ------------------

    def fetch_answer_raw(
        self,
        course_instance_id: str,
        task_id: str,
        open_id: str,
        *,
        prefer_localstorage_jwt: bool = True,
    ) -> dict:
        """调用答案接口,返回原始 JSON(尚未解密).

        如果 ``prefer_localstorage_jwt=True``(默认):
            先用 localStorage.jwtToke,失败/参数错时再用自签 JWT 重试一次
        否则只用自签 JWT。
        """
        url = f"{ANSWER_API_BASE}/{course_instance_id}/{task_id}/default"
        attempts: list[tuple[str, str]] = []

        if prefer_localstorage_jwt:
            local_jwt = _read_localstorage_jwt(self.page)
            if local_jwt:
                attempts.append(("localStorage.jwtToke", local_jwt))
        attempts.append(("self-signed", generate_auth_token(open_id)))

        last_err: Optional[Exception] = None
        last_payload: Optional[dict] = None
        for label, token in attempts:
            try:
                resp = self._req.get(
                    url, headers={"x-annotator-auth-token": token}
                )
            except Exception as e:
                last_err = e
                logger.warn(f"[unipus-api] 请求异常({label}): {e}")
                continue

            logger.info(f"[unipus-api] {label} -> HTTP {resp.status} {url}")
            if not resp.ok:
                last_err = UnipusAPIError(
                    f"HTTP {resp.status}: {url} ({label})"
                )
                continue

            try:
                payload = resp.json()
            except Exception as e:
                last_err = UnipusAPIError(f"返回非 JSON({label}): {e}")
                continue

            last_payload = payload
            code = payload.get("code")
            if code == 0:
                return payload  # 成功
            logger.warn(
                f"[unipus-api] {label} code={code} "
                f"msg={payload.get('msg') or payload.get('message')}"
            )

        if last_payload is not None:
            # 至少有一次拿到完整 JSON,让上层去看 code/msg
            return last_payload
        raise UnipusAPIError(str(last_err) if last_err else "所有认证方式都失败")

    def fetch_open_id(self) -> Optional[str]:
        """从 uai 子域获取 openId(回退方案)."""
        try:
            resp = self._req.get(USER_INFO_API)
            if not resp.ok:
                return None
            data = resp.json()
            user = (data.get("value") or {}).get("userInfo") or {}
            return user.get("appUserId")
        except Exception:
            return None

    # ------------------ 解密 + 解析 ------------------

    @staticmethod
    def decrypt_data(data: str, k: str) -> str:
        if not data:
            return ""
        if not data.startswith("unipus."):
            # 未加密,直接返回
            return data
        hex_cipher = data[len("unipus."):]
        return decrypt_aes128_ecb_zero_pad(hex_cipher, _AES_KEY_PREFIX + k)

    def get_answers(
        self, course_instance_id: str, task_id: str, open_id: str
    ) -> List[ParsedAnswer]:
        """一站式:获取 + 解密 + 解析,返回扁平化的题目答案列表."""
        raw = self.fetch_answer_raw(course_instance_id, task_id, open_id)
        if raw.get("code") != 0:
            raise UnipusAPIError(
                f"答案 API code={raw.get('code')} "
                f"msg={raw.get('msg') or raw.get('message')}"
            )
        decrypted = self.decrypt_data(raw.get("data", ""), raw.get("k", ""))
        return parse_answers(decrypted)


def parse_answers(decrypted_json: str) -> List[ParsedAnswer]:
    """解析解密后的答案 JSON.

    服务端返回的每个外层 item 形如:
        {
          "id": ..., "qid": ...,
          "answer":   "{...嵌套 JSON,含 children[*].answers...}",
          "analysis": "{...嵌套 JSON,含 children[*].analysis...}"
        }

    我们把所有 children 的 answers/analysis 展平成一条条原子答案,
    顺序与页面渲染顺序一致。
    """
    if not decrypted_json:
        return []
    try:
        arr = json.loads(decrypted_json)
    except Exception:
        return []
    if not isinstance(arr, list):
        return []

    raw_items: list[dict] = []
    for item in arr:
        bucket: dict[str, Any] = {"answers": [], "id": item.get("id", 0)}

        # answer 字段:用户答案模板/参考答案
        ans_field = item.get("answer")
        if ans_field:
            try:
                content = json.loads(ans_field) if isinstance(ans_field, str) else ans_field
                for child in content.get("children", []) or []:
                    children_ans = child.get("answers") or []
                    if children_ans:
                        bucket["answers"].append(children_ans[0])
            except Exception:
                pass

        # analysis 字段:官方解析(部分题型直接是正确答案)
        ana_field = item.get("analysis")
        if ana_field:
            try:
                analysis = json.loads(ana_field) if isinstance(ana_field, str) else ana_field
                children = analysis.get("children") or []
                for child in children:
                    if child.get("analysis"):
                        bucket["answers"].append(child["analysis"])
                if not children and analysis.get("analysis"):
                    bucket["answers"].append(analysis["analysis"])
            except Exception:
                pass

        raw_items.append(bucket)

    # 展平
    flat: list[ParsedAnswer] = []
    for it in raw_items:
        for ans in it["answers"]:
            flat.append(ParsedAnswer(answers=[ans], id=len(flat)))

    if flat:
        return flat
    # 没展平出东西时,回落到原始(可能是 WRITING 这种顶层 analysis)
    return [ParsedAnswer(answers=it["answers"], id=it["id"]) for it in raw_items]
