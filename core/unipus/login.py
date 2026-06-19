"""U校园 登录流程.

从原 AutoUnipus.py 中抽出的登录步骤,保持原行为:
- 自动填用户名/密码,勾选条款,点提交
- 出现图形验证码时让用户手动输入
"""
from __future__ import annotations

from playwright.sync_api import Page

from .. import logger


LOGIN_URL = "https://u.unipus.cn/user/student"


def auto_login(page: Page, username: str, password: str) -> None:
    """登录到 U校园 学生中心.

    与原版本行为一致:
    - 出现图形验证码时不自动 fill,把 placeholder 改成提示语让用户手动输入。
    - 不等待跳转完成(由调用方 wait_for_selector 决定)。
    """
    logger.tip("图形验证码需手动输入.")
    page.goto(LOGIN_URL)
    page.locator('[name="username"]').fill(username)
    page.locator('[name="password"]').fill(password)
    # 第二个 checkbox = 同意协议
    checkboxes = page.locator('[type="checkbox"]').all()
    if len(checkboxes) >= 2:
        checkboxes[1].click()
    page.locator(".btn.btn-login.btn-fill").click()

    logger.tip("出现安全验证不必担心,手动认证就好了.")
    page.wait_for_timeout(1000)

    try:
        page.wait_for_selector("#pw-captchaCode", timeout=800)
        page.eval_on_selector(
            "#pw-captchaCode",
            'el => el.placeholder = "PS:请手动输入图形验证码"',
        )
    except Exception:
        return
