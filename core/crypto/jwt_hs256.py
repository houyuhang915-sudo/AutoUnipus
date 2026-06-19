"""JWT HS256 自签.

U校园前端硬编码 JWT secret 用于自签 X-Annotator-Auth-Token。
这里用最小依赖实现(stdlib hmac + hashlib),不引入 PyJWT。

注意:payload 中 exp 是 **毫秒** 时间戳(JS 实现照搬 Date.now() + 31536000000)。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


# 与 yuanarcsin/unipus_auto 一致;若 U校园 后端轮换则需更新
JWT_SECRET = "a824b379f126b8b7aa5e33dee83fb0a05aa7462c"
JWT_ISS = "c4f772063dcfa98e9c50"
JWT_AUD = "edx.unipus.cn"
_ONE_YEAR_MS = 31_536_000_000


def _b64url(data: bytes) -> str:
    """URL-safe base64 不带末尾 '='."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_auth_token(open_id: str, secret: str = JWT_SECRET) -> str:
    """生成 X-Annotator-Auth-Token.

    Args:
        open_id: 从 cookie / localStorage / API 获取到的用户 openId
        secret:  HMAC 密钥(默认 JWT_SECRET)

    Returns:
        完整 JWT 字符串 "header.payload.signature"
    """
    header = {"typ": "JWT", "alg": "HS256"}
    payload = {
        "open_id": open_id or "",
        "name": "",
        "email": "",
        "administrator": False,
        # JS: Date.now() 是毫秒,这里照样输出毫秒级时间戳以保持一致
        "exp": int(time.time() * 1000) + _ONE_YEAR_MS,
        "iss": JWT_ISS,
        "aud": JWT_AUD,
    }

    h_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h_b64}.{p_b64}.{_b64url(sig)}"
