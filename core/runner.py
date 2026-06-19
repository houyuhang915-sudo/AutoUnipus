"""主流程编排.

两个公开入口:
    run_auto_mode(config)    自动模式:遍历 class_url,逐题作答并提交
    run_assist_mode(config)  辅助模式:用户手动进入题目页,按 Enter 自动选中(不提交)

支持取消:外部把 cancel_event 传进来,在关键点轮询。
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from playwright._impl._errors import TargetClosedError, TimeoutError
from playwright.sync_api import Page, sync_playwright

from . import logger
from .cache.store import AnswerCache
from .config import AppConfig
from .handlers.registry import DEFAULT_HANDLERS, fill_page
from .handlers.base import QuestionHandler
from .sources import (
    AIAnswerSource,
    APIAnswerSource,
    AnswerQuery,
    AnswerResult,
    AnswerSource,
    ContentAPIAnswerSource,
    LegacySubmitSource,
    ManualAnswerSource,
)
from .sources.sniff_source import SniffAnswerSource
from .unipus.api_client import ParsedAnswer, UnipusAPIClient
from .unipus.login import auto_login
from .unipus.page_info import PageInfo, extract_page_info
from .unipus.sniffer import NetworkSniffer


_TITLE_PATTERN = re.compile(r"[0-9]+?\.[0-9]+?.+")


# 听力题特征:页面里有 audio 元素 / class 包含 listening / 文案含"Listening"
_LISTENING_JS = """
() => {
    if (document.querySelector('audio')) return true;
    if (document.querySelector('[class*=listening i], [class*=audio i]')) return true;
    const txt = (document.body.innerText || '').toLowerCase();
    if (/\\b(listen(ing)?\\s+to|audio)\\b/.test(txt) && document.querySelector('button[class*=play], [class*=audio-player]')) return true;
    return false;
}
"""


def _is_listening(page: Page) -> bool:
    try:
        return bool(page.evaluate(_LISTENING_JS))
    except Exception:
        return False


def _get_page_title(page: Page) -> str:
    """汇总当前题目页可见的标题/Tab 文本,用作 ManualSource 模糊匹配的依据."""
    js = """
    () => {
        const out = [];
        const sels = [
            '[class*=layoutHeaderStyle][class*=menuList]',
            '[class*=layoutHeaderStyle]',
            '[class*=questionsTabs] [class*=active]',
            '[class*=questionTabs] [class*=active]',
            '[role=tab][aria-selected=true]',
            '[class*=tab][class*=active]',
        ];
        const seen = new Set();
        for (const s of sels) {
            for (const el of document.querySelectorAll(s)) {
                const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                if (t && t.length < 200 && !seen.has(t)) {
                    seen.add(t);
                    out.push(t);
                }
            }
        }
        return out.slice(0, 6).join(' | ');
    }
    """
    try:
        return (page.evaluate(js) or "").strip()
    except Exception:
        return ""


class CancelledError(RuntimeError):
    """流程被外部取消."""


# ---------- 单题运行结果(供 _run_class 收集做最终汇总) ----------

# 页面类型分类
PAGE_ANSWERING = "answering"   # 有 input,需要做题
PAGE_READING = "reading"        # 无 input,纯阅读
PAGE_SUBJECTIVE = "subjective"  # 主观题(其他主观题... 暂无评分)
PAGE_UNKNOWN = "unknown"        # 没识别出来,提交也失败


@dataclass
class ExerciseStats:
    """单题作答结果.

    字段语义:
        idx          第几题(1-based)
        total        本课程必修练习入口总数
        task_id      U校园 task id,如 u2g68
        row_title    课程目录里这一行的标题(用作日志)
        icon_kind    点的图标 class 名,如 icon-lianxi / icon-yulan / icon-wenben
        page_type    上面 4 个枚举之一
        accuracy     完成弹窗里的"正确率"。100/83/None(主观题或没拿到)
        filled       fill_page 实际填入题数
        skipped      跳过题数
        failed       填答失败题数
        cache_hit    本题答案是从缓存命中的
        ai_used      调过 AI(没缓存且不是阅读型)
        manual_hit   命中人工答案库
        submitted    实际提交了(没被风控拦)
        error        异常信息(若有)
    """
    idx: int = 0
    total: int = 0
    task_id: str = ""
    row_title: str = ""
    icon_kind: str = ""
    page_type: str = PAGE_UNKNOWN
    accuracy: Optional[int] = None
    filled: int = 0
    skipped: int = 0
    failed: int = 0
    cache_hit: bool = False
    ai_used: bool = False
    manual_hit: bool = False
    submitted: bool = False
    error: Optional[str] = None


def _check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError("流程已被用户取消")


def _select_handlers(config: AppConfig) -> list[QuestionHandler]:
    """按配置筛选启用的 handler,空配置 = 全部启用."""
    if not config.handlers:
        return list(DEFAULT_HANDLERS)
    enabled = set(config.handlers)
    return [h for h in DEFAULT_HANDLERS if h.name in enabled]


# ============================================================
# 答案获取(链式 fallback + 缓存)
# ============================================================

class AnswerResolver:
    """缓存优先,然后按顺序走数据源."""

    def __init__(
        self,
        sources: list[AnswerSource],
        cache: Optional[AnswerCache] = None,
    ):
        # 不在构造时过滤 available:某些 source 的 available 依赖运行时状态
        # (例如 LegacySubmitSource 看 page.url),需要 resolve 时再判断
        self.sources = list(sources)
        self.cache = cache

    def resolve(self, info: PageInfo, questions: list[str] | None = None,
                title: str = "", expected_count: int = 0) -> Optional[AnswerResult]:
        """获取答案.

        Args:
            expected_count: 当前页面真正有多少道题(从 DOM 数 input/textarea/radio/checkbox 来)。
                            > 0 时,会拿来校验每个数据源返回的答案条数 — 答案数 < 题数
                            视为"残缺,信不过",跳过这个源继续 fallback。这样能挡住:
                              · 缓存里只有 2 条(被 review-harvest 部分覆盖坏)但页面有 12 道
                              · 用户手填的人工答案库只录了一半
                            两种典型坑。
                            = 0 时跳过校验(用于 dry_run 或 assist 模式)。
        """
        if not info.course_instance_id or not info.task_id:
            logger.warn(f"[resolver] 缺少 {info.missing()},无法获取答案")
            return None

        # 1. 缓存
        if self.cache:
            cached = self.cache.get(info.course_instance_id, info.task_id)
            if cached:
                if expected_count and len(cached) != expected_count:
                    kind = "少" if len(cached) < expected_count else "多"
                    logger.warn(
                        f"[resolver] 缓存 {len(cached)} 条 {kind}于页面 {expected_count} 道题"
                        f"({'残缺' if kind == '少' else '可能字母+文本重复抓'}),"
                        f"跳过缓存改走数据源"
                    )
                else:
                    logger.info(f"[resolver] 命中缓存({len(cached)} 条答案)")
                    return AnswerResult(answers=cached, source="cache", cache_hit=True)

        # 2. 走数据源
        query = AnswerQuery(
            course_instance_id=info.course_instance_id,
            task_id=info.task_id,
            open_id=info.open_id or "",
            questions=questions or [],
            title=title,
        )
        for src in self.sources:
            if not src.available:
                logger.debug(f"[resolver] 跳过不可用源: {src.name}", enabled=False)
                continue
            res = src.fetch(query)
            if res and res.ok:
                # 同样校验数据源返回数 vs 页面题数
                if expected_count and len(res.answers) != expected_count:
                    kind = "少" if len(res.answers) < expected_count else "多"
                    logger.warn(
                        f"[resolver] 来源 {src.name} 返回 {len(res.answers)} 条 {kind}于页面 {expected_count} 道,"
                        f"判为不可靠,继续 fallback 下一个源"
                    )
                    continue
                logger.info(f"[resolver] 来源={res.source} 共 {len(res.answers)} 条答案")
                if self.cache and res.cacheable:
                    self.cache.put(
                        info.course_instance_id, info.task_id, res.answers, res.source
                    )
                elif self.cache and not res.cacheable:
                    logger.tip(
                        f"[resolver] 来源 {res.source} 标记 cacheable=False,跳过写缓存"
                    )
                return res
        logger.error("[resolver] 所有数据源都未拿到答案")
        return None


# ============================================================
# 浏览器初始化(登录 + 视口设置)
# ============================================================

def _launch_browser(p, driver: str):
    if driver == "Chrome":
        logger.info("正在启动 Chrome 浏览器...")
        return p.chromium.launch(channel="chrome", headless=False)
    logger.info("正在启动 Edge 浏览器...")
    return p.chromium.launch(channel="msedge", headless=False)


def _init_page(p, config: AppConfig) -> tuple[Page, NetworkSniffer]:
    browser = _launch_browser(p, config.driver)
    context = browser.new_context()
    context.grant_permissions(["microphone", "camera"])
    page = context.new_page()
    page.set_default_timeout(300_000)

    # 网络抓包:页面层全程订阅,popup 也会在打开时 attach
    sniffer = NetworkSniffer()
    sniffer.attach(page)
    # 挂在 page 上方便其它组件按需取(如 ContentAPIAnswerSource 借此挖 hex task_id)
    setattr(page, "_autounipus_sniffer", sniffer)

    logger.info("等待登录完成...")
    auto_login(page, config.username, config.password)

    page.wait_for_selector(".my_course_box")

    # 绕过环境检测弹窗(原项目流程)
    try:
        page.locator(".layui-layer-btn0").click(timeout=5000)
    except Exception:
        pass
    try:
        popup = page.wait_for_event("popup", timeout=5000)
        sniffer.attach(popup)
        popup.close()
    except Exception:
        pass

    # 设置视口大小
    try:
        viewsize = page.evaluate(
            "() => ({ width: window.screen.availWidth, height: window.screen.availHeight })"
        )
        viewsize["height"] = max(viewsize.get("height", 800) - 50, 500)
        page.set_viewport_size(viewsize)
    except Exception:
        pass

    return page, sniffer


# ============================================================
# 答题主流程
# ============================================================

def _close_dialog_if_any(page: Page) -> None:
    try:
        page.wait_for_selector(".dialog-header-pc--close-yD7oN", timeout=2500).click()
    except Exception:
        pass


# ---------- 自动关掉"录音机/麦克风权限"等无关噪音弹窗 ----------

_DISMISS_NOISE_POPUP_JS = r"""
() => {
    const closed = [];
    const NOISE = /(录音机|Permission\s*denied|麦克风|microphone|不支持|网络错误|加载失败|系统错误|提示)/i;
    const IMPORTANT = /(本次共完成|正确率|查看答案|得分|平均分|频繁|操作过快|提交失败)/i;
    const CONFIRM = /^(确定|确认|关闭|OK|知道了|I\s*know|好的|取消|Cancel|×)$/i;

    // ---- 策略 A:常规 dialog/modal class 扫一遍 ----
    const dialogSel = (
        '[class*=dialog], [class*=Dialog], [class*=modal], [class*=Modal], ' +
        '[class*=popup], [class*=Popup], [class*=layer], [class*=Layer], ' +
        '[class*=alert], [class*=Alert], [class*=notice], [class*=Notice], ' +
        '[class*=tip], [class*=Tip], [class*=mask], [class*=Mask], ' +
        '[role=dialog], [role=alertdialog]'
    );
    const seen = new Set();
    const visit = (el) => {
        if (!el || seen.has(el)) return;
        seen.add(el);
        if (!(el.offsetParent || el.getClientRects().length)) return;
        const text = (el.innerText || '').trim();
        if (!text || text.length > 600) return;
        if (IMPORTANT.test(text)) return;       // 完成弹窗等不能关
        if (!NOISE.test(text)) return;
        for (const b of el.querySelectorAll('button, [role=button], a, [class*=close]')) {
            if (!(b.offsetParent || b.getClientRects().length)) continue;
            const t = (b.innerText || b.getAttribute('aria-label') || '').trim();
            if (CONFIRM.test(t)) {
                try { b.click(); } catch (_) {}
                closed.push(text.slice(0, 60));
                return;
            }
        }
    };
    for (const el of document.querySelectorAll(dialogSel)) visit(el);

    // ---- 策略 B:策略 A 没关掉的话,扫全文 ----
    // 只要页面里出现噪音关键词,就遍历所有可见的 确定/OK 按钮挨个试
    const bodyText = (document.body.innerText || '');
    if (NOISE.test(bodyText) && !IMPORTANT.test(bodyText)) {
        for (const b of document.querySelectorAll('button, [role=button]')) {
            if (!(b.offsetParent || b.getClientRects().length)) continue;
            const t = (b.innerText || '').trim();
            if (!CONFIRM.test(t)) continue;
            // 沿着祖先找最近的 dialog 容器,确认它真的属于噪音弹窗
            let ancestor = b.parentElement;
            let foundNoise = false;
            for (let i = 0; i < 6 && ancestor; i++) {
                const at = (ancestor.innerText || '').trim();
                if (IMPORTANT.test(at)) { foundNoise = false; break; }
                if (NOISE.test(at) && at.length < 600) { foundNoise = true; break; }
                ancestor = ancestor.parentElement;
            }
            if (foundNoise) {
                try { b.click(); } catch (_) {}
                closed.push((ancestor.innerText || '').trim().slice(0, 60));
            }
        }
    }
    return closed;
}
"""


def _dismiss_noise_popups(page: Page) -> int:
    """关掉麦克风权限错误等无关弹窗.返回关掉的个数."""
    try:
        closed = page.evaluate(_DISMISS_NOISE_POPUP_JS) or []
    except Exception:
        return 0
    if closed:
        for t in closed:
            logger.info(f"[runner] 已自动关闭噪音弹窗: {t!r}")
    return len(closed)


# ---------- 持续运行的噪音弹窗杀手:MutationObserver 守护 ----------

_INSTALL_POPUP_KILLER_JS = r"""
() => {
    if (window.__autoUnipusPopupKiller) return 'already-installed';
    window.__autoUnipusPopupKiller = true;

    const NOISE = /(录音机|Permission\s*denied|麦克风|microphone|不支持|网络错误|加载失败|系统错误)/i;
    const IMPORTANT = /(本次共完成|正确率\s*[为:：]?\s*\d+\s*%|查看答案|得分|平均分|频繁|操作过快|提交失败)/i;
    const CONFIRM = /^(确定|确认|关闭|OK|知道了|I\s*know|好的)$/i;

    function tryKill(node) {
        try {
            if (!node || node.nodeType !== 1) return;
            if (!(node.offsetParent || node.getClientRects().length)) return;
            const text = (node.innerText || '').trim();
            if (!text || text.length > 600) return;
            if (IMPORTANT.test(text)) return;
            if (!NOISE.test(text)) return;
            // 找内部的"确定/OK"按钮,点之
            let clicked = false;
            for (const b of node.querySelectorAll('button, [role=button], a')) {
                if (!(b.offsetParent || b.getClientRects().length)) continue;
                const t = (b.innerText || '').trim();
                if (CONFIRM.test(t)) {
                    try { b.click(); clicked = true; } catch (_) {}
                    break;
                }
            }
            // 没找到按钮就直接 hide
            if (!clicked) {
                node.style.cssText += '; display: none !important; visibility: hidden !important; pointer-events: none !important;';
            }
        } catch (_) {}
    }

    function scan(root) {
        try {
            root.querySelectorAll('div, section, article, aside').forEach(tryKill);
        } catch (_) {}
    }

    // 0. 立即扫一遍现有节点
    scan(document.body);

    // 1. MutationObserver:长期看着 body 子树新增的节点
    const obs = new MutationObserver((mutations) => {
        for (const m of mutations) {
            if (m.type === 'childList') {
                for (const node of m.addedNodes) {
                    if (node.nodeType !== 1) continue;
                    tryKill(node);
                    if (node.querySelectorAll) {
                        scan(node);
                    }
                }
            } else if (m.type === 'attributes' || m.type === 'characterData') {
                if (m.target && m.target.nodeType === 1) tryKill(m.target);
            }
        }
    });
    obs.observe(document.body, {
        childList: true, subtree: true,
        attributes: true, attributeFilter: ['style', 'class'],
        characterData: true,
    });

    // 2. 兜底:每秒主动扫一遍
    setInterval(() => scan(document.body), 1000);

    return 'installed';
}
"""


def _install_popup_killer(page: Page) -> None:
    """注入持续运行的噪音弹窗杀手(MutationObserver + 每秒兜底扫).

    一旦页面里出现"录音机/麦克风/Permission denied"等弹窗,会立刻被点掉或 hide。
    重复注入到同一页面会自动 no-op(检查 window 标志位)。
    """
    try:
        result = page.evaluate(_INSTALL_POPUP_KILLER_JS)
        if result == "installed":
            logger.info("[runner] 噪音弹窗杀手已注入(MutationObserver 持续监控)")
    except Exception as e:
        logger.warn(f"[runner] 注入弹窗杀手失败: {e}")


def _click_exercise_open_target(
    page: Page, exercise_locator, sniffer: Optional[NetworkSniffer] = None
) -> Page:
    """点击练习入口,返回真正承载题目的 page.

    U校园 不同部署:
    - 部分版本是 SPA 同标签跳转(URL hash 变化)
    - 部分版本会 window.open 弹一个新标签到 ucontent.unipus.cn
    - 部分版本会 window.open 一个跨域 iframe 容器
    本函数同时监听新 page 事件,若有新 page 则切到它。
    """
    context = page.context
    new_pages: list[Page] = []

    def on_page(p: Page) -> None:
        new_pages.append(p)
        if sniffer is not None:
            try:
                sniffer.attach(p)
            except Exception:
                pass

    context.on("page", on_page)
    try:
        try:
            exercise_locator.click()
        except Exception as e:
            logger.warn(f"[runner] 点击练习失败: {e}")
        # 给浏览器一点时间打开新页或完成跳转
        page.wait_for_timeout(1200)
    finally:
        try:
            context.remove_listener("page", on_page)
        except Exception:
            pass

    target = new_pages[-1] if new_pages else page
    # 把 sniffer 也挂到 target page 上,方便 ContentAPI 等取
    if sniffer is not None:
        try:
            setattr(target, "_autounipus_sniffer", sniffer)
        except Exception:
            pass
    try:
        target.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass
    return target


def _wait_for_task_page(page: Page, timeout_ms: int = 12_000) -> bool:
    """等到 URL/页面看起来像 U校园题目页.

    判定标准(任一满足即可):
    - 域名是 ucontent.unipus.cn
    - URL 含 course-v2:
    - URL 含 36+ 字符的连续 hex
    - 页面里出现典型题目容器 / .iKnow / 提交栏
    """
    js = """
    () => {
        const u = location.href;
        if (/ucontent\\.unipus\\.cn/i.test(u)) return true;
        if (/course-v2:/.test(u)) return true;
        if (/[a-f0-9]{16,}/i.test(u)) return true;
        if (document.querySelector('.iKnow, .submit-bar-pc--btn-1_Xvo, [class*=questionDefault]')) return true;
        return false;
    }
    """
    deadline = timeout_ms
    step = 400
    elapsed = 0
    while elapsed < deadline:
        try:
            if page.evaluate(js):
                return True
        except Exception:
            pass
        page.wait_for_timeout(step)
        elapsed += step
    return False


# ---------- 检测 review 模式 + 找"重新练习"按钮 ----------

_DETECT_REVIEW_MODE_JS = r"""
() => {
    // U校园 review/已完成 模式的特征:页面上出现"上次提交时间"/"最高成绩"/"最高平均分"
    const txt = (document.body.innerText || '').slice(0, 8000);
    return /(上次提交时间|最高成绩|最高平均分|本次成绩|本题已答|已答完|查看解析)/i.test(txt);
}
"""

_CLICK_RESTART_JS = r"""
() => {
    const btns = document.querySelectorAll('button, [role=button], a, span, div');
    for (const b of btns) {
        if (!(b.offsetParent || b.getClientRects().length)) continue;
        const t = (b.innerText || b.textContent || '').trim();
        if (!t || t.length > 12) continue;
        if (/^(重新练习|重新作答|重新答题|再练一次|再答一次|重做|重新开始|Restart)$/i.test(t)) {
            try { b.click(); return t; } catch (_) {}
        }
    }
    return null;
}
"""


def _save_to_manual_library(
    task_id: str,
    answers: list[ParsedAnswer],
    path: str = "data/manual_answers.json",
) -> int:
    """把 harvest 扒到的答案存进 manual_answers.json 的 exact_by_task 里.

    这样 webui 答案库面板能看到、能编辑,下次跑同一题(任何账号)
    都会直接从 manual 库命中,免 AI、免提交、免风控。
    返回写入的条目数。
    """
    items = []
    for a in answers:
        if a.answers and str(a.answers[0]).strip():
            items.append(str(a.answers[0]).strip())
    if not items:
        return 0

    p = Path(path)
    data: dict = {}
    if p.exists():
        try:
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}

    bucket = data.setdefault("exact_by_task", {})
    if not isinstance(bucket, dict):
        bucket = {}
        data["exact_by_task"] = bucket

    bucket[task_id] = items

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return len(items)
    except Exception as e:
        logger.warn(f"[harvest] 写 manual_answers 失败: {e}")
        return 0


def _click_next_or_submit(page: Page) -> bool:
    """点击 提交栏 最后一个按钮(下一页 / 提交).返回是否点到了."""
    try:
        buttons = page.locator(".submit-bar-pc--btn-1_Xvo").all()
        if not buttons:
            return False
        buttons[-1].click()
        return True
    except Exception as e:
        logger.warn(f"点击下一页/提交失败: {e}")
        return False


# ---------- U校园 风控弹窗检测 / 反风控 ----------

_RATELIMIT_DETECT_JS = r"""
() => {
    const dialogs = document.querySelectorAll(
        '[class*=dialog], [class*=modal], [class*=popup], [class*=layui-layer], [class*=el-message-box]'
    );
    for (const d of dialogs) {
        // 仅看可见的
        if (!(d.offsetParent || d.getClientRects().length)) continue;
        const text = (d.innerText || '').trim();
        if (!text) continue;
        if (/(频繁|操作过快|提交失败|稍后再试|too\s+frequent|rate\s*limit)/i.test(text)) {
            return text.slice(0, 200);
        }
    }
    return null;
}
"""

_DISMISS_DIALOG_JS = r"""
() => {
    // 优先点"确定/确认/OK/关闭"按钮
    const btns = document.querySelectorAll('button, [role=button]');
    for (const b of btns) {
        if (!(b.offsetParent || b.getClientRects().length)) continue;
        const t = (b.innerText || '').trim();
        if (/^(确定|确认|好的|知道了|OK|Close|关闭)$/i.test(t)) {
            b.click();
            return t;
        }
    }
    return null;
}
"""


def _detect_rate_limit(page: Page) -> Optional[str]:
    try:
        return page.evaluate(_RATELIMIT_DETECT_JS)
    except Exception:
        return None


def _dismiss_dialog(page: Page) -> Optional[str]:
    try:
        return page.evaluate(_DISMISS_DIALOG_JS)
    except Exception:
        return None


def _safe_submit(page: Page, *, cool_down: int = 60) -> bool:
    """点击下一页/提交,带:
    - 提交前 2-4 秒人类节奏停顿
    - 提交后检测"操作过于频繁"弹窗
    - 触发风控时关弹窗 + 暂停 cool_down 秒,再让上层重试
    """
    page.wait_for_timeout(random.randint(2000, 4000))
    if not _click_next_or_submit(page):
        return False
    # 给服务器一点时间响应
    page.wait_for_timeout(900)

    warning = _detect_rate_limit(page)
    if warning:
        logger.warn(
            f"[runner] U校园 风控触发: {warning!r} — 关闭弹窗后冷却 {cool_down}s"
        )
        _dismiss_dialog(page)
        # 慢慢等
        for _ in range(cool_down):
            page.wait_for_timeout(1000)
        # 提示用户后续动作
        logger.tip(
            "[runner] 冷却完成,继续。如果反复触发可以把 cool_down 调更大,"
            "或减小答题速度(增大 BlankHandler / TranslationHandler 里的字符 delay)"
        )
        return False  # 让上层知道这次没真的提交
    return True


# ---------- 完成弹窗 → 查看答案 → 扒答案 → 入库 ----------

# 题目提交完成后的弹窗特征文字
_COMPLETION_POPUP_JS = r"""
() => {
    // 强匹配:含"本次共完成 N 道题"或"正确率为 NN%"或"查看答案"按钮的弹窗
    const STRONG = /(本次共完成|正确率\s*[为:：]?\s*\d+\s*%|查看答案)/i;
    // 弱匹配:含"得分""完成""平均分"等
    const WEAK = /(得分|平均分|本次答题|完成本次)/i;

    // 1) 先按常见 dialog/modal class 找(覆盖 layui / antd / element-ui 等)
    const dialogSel = (
        '[class*=dialog], [class*=Dialog], [class*=modal], [class*=Modal], ' +
        '[class*=popup], [class*=Popup], [class*=layui-layer], ' +
        '[class*=el-message-box], [class*=ant-modal], [class*=result], ' +
        '[class*=score], [class*=summary], [class*=submitResult], ' +
        '[class*=submitTip], [class*=examResult], [class*=finishTip]'
    );
    for (const d of document.querySelectorAll(dialogSel)) {
        if (!(d.offsetParent || d.getClientRects().length)) continue;
        const text = (d.innerText || '').trim();
        if (!text) continue;
        if (STRONG.test(text)) return text.slice(0, 300);
    }
    // 2) 兜底:扫所有可见的 div / section,看是否有强匹配文本
    const all = document.querySelectorAll('div, section, aside');
    for (const el of all) {
        if (!(el.offsetParent || el.getClientRects().length)) continue;
        const text = (el.innerText || '').trim();
        if (text.length < 8 || text.length > 600) continue;
        if (STRONG.test(text)) return text.slice(0, 300);
    }
    // 3) 弱匹配兜底
    for (const el of all) {
        if (!(el.offsetParent || el.getClientRects().length)) continue;
        const text = (el.innerText || '').trim();
        if (text.length < 8 || text.length > 600) continue;
        if (WEAK.test(text)) return text.slice(0, 300);
    }
    return null;
}
"""


_CLICK_VIEW_ANSWER_JS = r"""
() => {
    const btns = document.querySelectorAll('button, [role=button], a');
    for (const b of btns) {
        if (!(b.offsetParent || b.getClientRects().length)) continue;
        const t = (b.innerText || b.textContent || '').trim();
        if (/^(查看答案|查看解析|看答案|查看正确答案)$/i.test(t)) {
            b.click();
            return t;
        }
    }
    return null;
}
"""


# 在 review 模式下尽力扒出正确答案。 U校园 review DOM 几种常见形态都试一遍。
# 严格模式:只看带"正确答案/right/correct"语义的元素,不会误抓我们自己刚提交的错答案。
_EXTRACT_REVIEW_STRICT_JS = r"""
() => {
    const out = [];
    const seen = new Set();
    const push = (s) => {
        if (!s) return;
        s = String(s).trim();
        if (!s || seen.has(s)) return;
        if (s.length > 500) return;
        seen.add(s);
        out.push(s);
    };

    // 形态 1: 显式"正确答案/参考答案"节点(覆盖目前已知的所有 U校园 类名变体)
    for (const sel of [
        '[class*=rightAnswer]',
        '[class*=right-answer]',
        '[class*=correctAnswer]',
        '[class*=correct-answer]',
        '[class*=referenceAnswer]',
        '[class*=reference-answer]',
        '[class*=standardAnswer]',
        '[class*=standard-answer]',
        '[class*=answer-key]',
        '[class*=answerKey]',
        '[class*=answer-correct]',
        '[class*=answerCorrect]',
        '[class*=answerRight]',
        '[class*=answer-right]',
        // U校园 新版 / element-ui 风格
        '[class*=keyAnswer]',
        '[class*=key-answer]',
        '[class*=trueAnswer]',
        '[class*=true-answer]',
        '[class*=answerShow]',
        '[class*=answer-show]',
        '[class*=showAnswer]',
        '[class*=show-answer]',
        '[class*=answerKeyText]',
        '[class*=correctOption]',
        '[class*=correct-option]',
        '[class*=optionRight]',
        '[class*=option-right]',
        '[class*=is-right]',
        '[class*=isRight]',
        '[class*=refAnswer]',
        '[class*=ref-answer]',
    ]) {
        for (const el of document.querySelectorAll(sel)) {
            push(el.innerText);
        }
    }

    // 形态 2: 文本中含 "正确答案: xxx" / "Correct answer: xxx" / "参考答案: xxx"
    if (out.length === 0) {
        const all = document.querySelectorAll('div, span, p, td, li, dd, em, strong');
        for (const el of all) {
            const t = (el.innerText || '').trim();
            // 注意:这种文本在每道题前都会重复一份,push 时 Set 去重
            const m = t.match(/(?:正确答案|参考答案|标准答案|Correct\s+answer|Reference\s+answer|Key)\s*[:：]\s*(.+?)(?:$|\n|。)/i);
            if (m) push(m[1]);
        }
    }

    // 形态 5: review 模式下绿色文本兜底
    // U校园 各版本几乎都把"标答"用绿色显示(用户错答用红色)。
    // 这里用 computed style 找深绿到亮绿区间的文字,排除题面长文本。
    if (out.length === 0) {
        const candidates = document.querySelectorAll(
            'span, em, strong, b, i, td, dd, li, p'
        );
        for (const el of candidates) {
            if (!(el.offsetParent || el.getClientRects().length)) continue;
            const text = (el.innerText || '').trim();
            if (!text || text.length < 1 || text.length > 200) continue;
            // 跳过有子元素的容器(避免重复抓父子文本)
            if (el.children.length > 0) continue;
            const style = window.getComputedStyle(el);
            const m = (style.color || '').match(/rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)/);
            if (!m) continue;
            const r = +m[1], g = +m[2], b = +m[3];
            // "绿色"判定:绿值显著高于红和蓝(避开灰色 / 黑色 / 蓝色 / 红色)
            if (g > 100 && g > r + 40 && g > b + 40) {
                push(text);
            }
        }
    }

    return out;
}
"""

# 宽松模式:除上面 1+2,再加 3+4 — 这两种可能拿到的是用户自己的提交。
# 仅在 正确率=100% 时使用(那时候提交=正确)。
_EXTRACT_REVIEW_LENIENT_JS = r"""
() => {
    const out = [];
    const seen = new Set();
    const push = (s) => {
        if (!s) return;
        s = String(s).trim();
        if (!s || seen.has(s)) return;
        if (s.length > 500) return;
        seen.add(s);
        out.push(s);
    };

    // 形态 1
    for (const sel of [
        '[class*=rightAnswer]',
        '[class*=right-answer]',
        '[class*=correctAnswer]',
        '[class*=correct-answer]',
        '[class*=referenceAnswer]',
        '[class*=reference-answer]',
        '[class*=standardAnswer]',
        '[class*=standard-answer]',
        '[class*=answer-key]',
        '[class*=answerKey]',
        '[class*=answer-correct]',
        '[class*=answerCorrect]',
        '[class*=answerRight]',
        '[class*=answer-right]',
    ]) {
        for (const el of document.querySelectorAll(sel)) {
            push(el.innerText);
        }
    }

    // 形态 2
    if (out.length === 0) {
        const all = document.querySelectorAll('div, span, p, td, li');
        for (const el of all) {
            const t = (el.innerText || '').trim();
            const m = t.match(/(?:正确答案|参考答案|标准答案|Correct\s+answer|Reference\s+answer)\s*[:：]\s*(.+)/i);
            if (m) push(m[1]);
        }
    }

    // 形态 3: review 模式下的填空 input(只读,带 value)
    if (out.length === 0) {
        const inputs = document.querySelectorAll(
            'input[readonly][value], input[disabled][value], input.right, input[class*=right][value], input[class*=correct][value]'
        );
        for (const inp of inputs) {
            push(inp.value);
        }
    }

    // 形态 4: 单选题 review,选中状态的 radio
    if (out.length === 0) {
        const radios = document.querySelectorAll(
            'input[type=radio][checked], input[type=radio]:checked'
        );
        for (const r of radios) {
            const v = r.getAttribute('value') || '';
            if (v) push(v);
        }
    }

    return out;
}
"""

# 兼容老调用点保留别名(指向 lenient,行为不变)
_EXTRACT_REVIEW_ANSWERS_JS = _EXTRACT_REVIEW_LENIENT_JS


# 主观题完成弹窗特征:U校园 显示"其他主观题... 暂无评分"
_SUBJECTIVE_RE = re.compile(r"(其他主观题|主观题.*?暂无评分|暂无评分)")


def _is_subjective_popup(popup_text: str) -> bool:
    """完成弹窗是否表示这是主观题(无机器评分,review 也没法扒答案)."""
    if not popup_text:
        return False
    return bool(_SUBJECTIVE_RE.search(popup_text))


_CLICK_REDO_JS = r"""
() => {
    const btns = document.querySelectorAll('button, [role=button], a');
    for (const b of btns) {
        if (!(b.offsetParent || b.getClientRects().length)) continue;
        const t = (b.innerText || b.textContent || '').trim();
        if (/^(再做一次|重做|重新作答|再答一次|再来一次|Retry|Try\s+again)$/i.test(t)) {
            b.click();
            return t;
        }
    }
    return null;
}
"""


_ACCURACY_RE = re.compile(r"正确率\s*[为:：]?\s*([\d.]+)\s*%")
_TOTAL_QUESTIONS_RE = re.compile(r"本次共完成\s*(\d+)\s*道题")


def _parse_total_questions(text: str) -> Optional[int]:
    """从弹窗文本中抽出"本次共完成 N 道题"里的 N."""
    m = _TOTAL_QUESTIONS_RE.search(text or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _parse_accuracy(text: str) -> Optional[int]:
    """从弹窗文本里抽出正确率百分比(0-100). 无法识别返回 None.支持小数."""
    m = _ACCURACY_RE.search(text or "")
    if m:
        try:
            return max(0, min(100, int(round(float(m.group(1))))))
        except ValueError:
            return None
    # 兜底:任意 NN.N% / NN%
    m = re.search(r"([\d]+(?:\.[\d]+)?)\s*%", text or "")
    if m:
        try:
            return max(0, min(100, int(round(float(m.group(1))))))
        except ValueError:
            return None
    return None


def _try_close_dialog(page: Page) -> None:
    """尽力关闭当前 modal,继续学习/确定/关闭都点."""
    js = r"""
    () => {
        const btns = document.querySelectorAll('button, [role=button]');
        for (const b of btns) {
            if (!(b.offsetParent || b.getClientRects().length)) continue;
            const t = (b.innerText || '').trim();
            if (/^(继续学习|继续|确定|确认|关闭|Close)$/i.test(t)) {
                b.click();
                return t;
            }
        }
        return null;
    }
    """
    try:
        page.evaluate(js)
    except Exception:
        pass


_PAREN_WRAP_RE = re.compile(
    r"^\s*[\(（]\s*(.+?)\s*[\)）]\s*$",
    re.DOTALL,
)


def _normalize_review_text(s: str) -> str:
    """清洗 review 模式扒到的标答文本:剥首尾空白 + 去掉外层成对括号.

    U校园 review 经常把标答用 "(answer)" 形式渲染(中文/英文括号都见过),
    直接填到 input 框里会全错。这里只剥**最外层**一对括号 — 像
    "(in the form of)" → "in the form of",
    "(stand up for)" → "stand up for",
    但 "in the form of (someone)" 这种内嵌括号不动。
    """
    s = (s or "").strip()
    if not s:
        return ""
    m = _PAREN_WRAP_RE.match(s)
    if m:
        s = m.group(1).strip()
    return s


def _dump_review_html(page: Page, task_id: str) -> None:
    """把当前 review 页面的题目区 HTML 存一份,方便后续加 selector 排查."""
    js = r"""
    () => {
        // 优先抓题目主体区,失败就退到 body
        const sels = [
            '[class*=questionContainer]',
            '[class*=question-area]',
            '[class*=questionPanel]',
            '[class*=task-content]',
            '[class*=mainPanel]',
            '#root',
            'main',
        ];
        for (const s of sels) {
            const el = document.querySelector(s);
            if (el) return el.outerHTML;
        }
        return document.body ? document.body.outerHTML : '';
    }
    """
    try:
        html = page.evaluate(js) or ""
    except Exception:
        return
    if not html:
        return
    # 截断超大 HTML(题目区一般 < 1MB)
    if len(html) > 5_000_000:
        html = html[:5_000_000] + "\n<!-- truncated -->"
    out_dir = Path("data/debug")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe_tid = re.sub(r"[^A-Za-z0-9_-]", "_", task_id or "unknown")
    target = out_dir / f"review_{safe_tid}_{ts}.html"
    try:
        target.write_text(html, encoding="utf-8")
        logger.tip(
            f"[harvest] review 页面 HTML 已保存到 {target}"
            f"(把这个文件发给我,我加上对应的 review selector)"
        )
    except Exception as e:
        logger.debug(f"[harvest] 写 dump 失败: {e}", enabled=False)


def _harvest_correct_answers(
    page: Page,
    info: PageInfo,
    cache: Optional[AnswerCache],
    handlers: Optional[list[QuestionHandler]] = None,
) -> Optional[int]:
    """提交后看到完成弹窗时:点'查看答案' → 扒正确答案 → 写缓存.

    返回:解析到的正确率(0-100)。没扒到弹窗或解析不出 % 时返回 None。
    重做不在此处做 — 由上层 runner 通过"退出 + 重新进入题目"实现。
    """
    # 给页面 1.5s 让完成弹窗渲染出来
    page.wait_for_timeout(1500)

    popup_text = None
    try:
        popup_text = page.evaluate(_COMPLETION_POPUP_JS)
    except Exception as e:
        logger.warn(f"[harvest] 检测弹窗时报错: {e}")
    if not popup_text:
        # 多给一会儿等弹窗动画
        page.wait_for_timeout(1500)
        try:
            popup_text = page.evaluate(_COMPLETION_POPUP_JS)
        except Exception:
            popup_text = None
    if not popup_text:
        logger.tip(
            "[harvest] 提交后没看到完成弹窗 — 可能 U校园 这个版本不弹,"
            "或弹窗 DOM 跟我们假设的不同。"
        )
        return None

    accuracy = _parse_accuracy(popup_text)
    subjective = _is_subjective_popup(popup_text)
    logger.info(
        f"[harvest] 检测到完成弹窗 (正确率={accuracy if accuracy is not None else '?'}%"
        f"{', 主观题' if subjective else ''}): "
        f"{popup_text[:120]!r}"
    )

    # 主观题:U校园 review 不会展示标答(只显示用户提交+评分占位),
    # 也没机器评分可参考。这种情况绝对不能写缓存,会污染下一轮。
    if subjective:
        logger.tip(
            "[harvest] 此题为主观题(暂无评分),不扒 review 也不写缓存。"
            "如已确认 AI 答案靠谱,可在 webui 手动答案库面板录入对应 task_id"
        )
        _try_close_dialog(page)
        return accuracy

    # 点"查看答案"
    clicked = None
    try:
        clicked = page.evaluate(_CLICK_VIEW_ANSWER_JS)
    except Exception as e:
        logger.warn(f"[harvest] 点击查看答案时报错: {e}")
    if not clicked:
        logger.warn(
            "[harvest] 弹窗里没找到'查看答案'按钮(也许此题不允许查看)"
        )
        _try_close_dialog(page)
        return accuracy
    logger.info(f"[harvest] 已点击 {clicked!r},等页面进入 review 模式")
    page.wait_for_timeout(2500)

    # 扒答案 — 严格模式只信带 right/correct 类名的元素。
    # 当且仅当 正确率=100%(我们提交的就是对的) 时,才允许从 readonly input/checked radio
    # 兜底,否则会把"我们填错的答案"当成"正确答案"存回缓存,反向污染。
    use_lenient = accuracy == 100
    extractor = _EXTRACT_REVIEW_LENIENT_JS if use_lenient else _EXTRACT_REVIEW_STRICT_JS
    items: list = []
    try:
        items = page.evaluate(extractor) or []
    except Exception as e:
        logger.warn(f"[harvest] 扒 review 答案异常: {e}")

    # 清洗扒出来的字符串:
    # 1) 剥首尾空白
    # 2) 去掉外层括号 — U校园 review 模式经常把标答渲染成 "(answer)" 格式,
    #    填到 input 时必须不带括号
    items = [_normalize_review_text(s) for s in items if str(s).strip()]
    items = [s for s in items if s]
    if not items:
        if not use_lenient:
            logger.warn(
                f"[harvest] 严格模式未扒到带'正确答案'类名/文本的元素"
                f"(正确率={accuracy}%,放弃 harvest 防止存错答案污染缓存)"
            )
            # 把 review HTML 存一份,方便诊断 — 你把这个文件发给我我加 selector
            try:
                _dump_review_html(page, info.task_id)
            except Exception as e:
                logger.debug(f"[harvest] dump review html 失败: {e}", enabled=False)
        else:
            logger.warn(
                "[harvest] review 模式下没扒到正确答案 — DOM 跟我们假设的形态都不一致"
            )
        _try_close_dialog(page)
        return accuracy

    parsed = [ParsedAnswer(answers=[s], id=i) for i, s in enumerate(items)]
    logger.info(
        f"[harvest] 从 review 扒到 {len(parsed)} 条正确答案: "
        f"{[a.answers[0][:30] for a in parsed[:5]]}{'...' if len(parsed) > 5 else ''}"
    )

    # 数目对不上检测:正确率 < 100% 时,严格模式(form 5 颜色识别)只能扒到"被你答错"的
    # 那几道的标答 — 而不是全部题的标答。如果直接 cache.put,会把原来 N 条的缓存
    # 覆盖成 K 条(K << N),反向变坏。下次跑只能填 K 道,其他 N-K 道留空。
    # 反过来 K > N 也是错(form 4 + form 5 把单选题的字母 + 选项全文都抓回来,
    # 一题变两条),会让 dispatch 错位填答。
    # 综上:只接受 K == N 的 harvest 结果。
    total_questions = _parse_total_questions(popup_text)
    extracted_count = len(parsed)
    if total_questions and extracted_count != total_questions:
        kind = "残缺(用于纠错)" if extracted_count < total_questions else "多了(可能字母+文本重复抓)"
        logger.warn(
            f"[harvest] 扒到 {extracted_count} 条 ≠ 完成的 {total_questions} 道 {kind},"
            f"为防止污染缓存 → **不写缓存,不改 manual lib**"
        )
        logger.tip(
            f"[harvest] 这 {extracted_count} 条参考答案可能对人工修题有用,"
            f"建议你 F12 看下 review 页对照后手动改进 "
            f"manual_answers.json:exact_by_task.{info.task_id}"
        )
        for i, a in enumerate(parsed):
            logger.info(f"[harvest] 参考标答 [{i + 1}] {a.answers[0]!r}")
        _try_close_dialog(page)
        return accuracy

    if cache is not None:
        cache.put(info.course_instance_id, info.task_id, parsed, "review-harvest")
        logger.info(f"[harvest] 答案已写入缓存,下次进同一题直接 100% 命中")

    # 同时存进 manual_answers.json 的 exact_by_task,
    # webui 答案库面板能看到/编辑,跨账号也能复用
    n_saved = _save_to_manual_library(info.task_id, parsed)
    if n_saved:
        logger.info(
            f"[harvest] 已写入 manual_answers.json: exact_by_task.{info.task_id} ({n_saved} 条)"
        )

    # 关掉 review 弹窗,继续后续流程
    _try_close_dialog(page)
    return accuracy


def _try_redo_with_correct_answers(
    page: Page,
    correct_answers: list[ParsedAnswer],
    handlers: list[QuestionHandler],
    info: PageInfo,
    cache: Optional[AnswerCache],
) -> bool:
    """点击 '再做一次' / '重做' 按钮,用扒到的正确答案重新填一遍 + 提交.

    返回 True 表示成功执行了 redo 流程(无论最终是否 100%)。
    """
    redo_clicked = None
    try:
        redo_clicked = page.evaluate(_CLICK_REDO_JS)
    except Exception:
        pass
    if not redo_clicked:
        logger.tip(
            "[runner] 没找到'再做一次'按钮 — U校园 可能不让重做。"
            "答案已存进缓存,下次跑同一题会 100% 命中"
        )
        return False

    logger.info(f"[runner] 已点击 {redo_clicked!r},准备用正确答案重答")
    page.wait_for_timeout(2500)

    # 关掉可能残留的弹窗(redo 后偶尔会再弹一次确认框)
    _try_close_dialog(page)

    # 重新填
    try:
        fr = fill_page(page, correct_answers, handlers=handlers)
        logger.info(
            f"[runner] 重做填入 {fr.filled} 题 / 跳过 {fr.skipped} / 失败 {fr.failed}"
        )
    except Exception as e:
        logger.warn(f"[runner] 重做填答异常: {e}")
        return False

    # 再提交一次
    if not _safe_submit(page):
        logger.warn("[runner] 重做后提交未成功")
        return True

    # 看新弹窗,期望 100%
    page.wait_for_timeout(800)
    try:
        text2 = page.evaluate(_COMPLETION_POPUP_JS)
    except Exception:
        text2 = None
    if text2:
        acc2 = _parse_accuracy(text2)
        if acc2 == 100:
            logger.info(f"[runner] 重做后正确率 100%! ✓")
        else:
            logger.warn(
                f"[runner] 重做后正确率 = {acc2}%(扒到的答案可能不全或与提交格式有差)"
            )
    _try_close_dialog(page)
    return True


def _print_title(page: Page) -> None:
    try:
        head = page.wait_for_selector(
            ".layoutHeaderStyle--menuList-Ef90e", timeout=1000
        ).text_content()
        if head:
            m = _TITLE_PATTERN.findall(head)
            if m:
                logger.info(f"获取 <<{m[0]}>> 答案成功!")
    except Exception:
        pass


def _answer_current_page(
    page: Page,
    resolver: AnswerResolver,
    handlers: list[QuestionHandler],
    *,
    submit: bool,
    dry_run: bool,
    harvest_first_pass: bool = False,
    stats: Optional[ExerciseStats] = None,
) -> tuple[bool, bool]:
    """对当前题目页执行:取答案 → 填答 → (可选)提交.

    返回 (success, redo_recommended):
        success            流程是否顺利走完(即便部分跳过也算)
        redo_recommended   harvest 检测到正确率<100%, 建议外层退出本题再次进入
    顺便把详细状态写到 stats(若给).
    """
    info = extract_page_info(page)
    if not info.course_instance_id or not info.task_id:
        logger.warn(
            f"[runner] 无法识别页面信息,缺少 {info.missing()};"
            f"跳过。当前 URL: {page.url}"
        )
        if stats is not None:
            stats.error = f"缺少页面信息: {info.missing()}"
        return False, False

    if stats is not None:
        stats.task_id = info.task_id
        stats.page_type = PAGE_ANSWERING

    title = _get_page_title(page)
    logger.info(
        f"[runner] task={info.task_id} title={title or '(空)'}"
    )

    # 仅做提示:很多页面含音频(单词发音也是 audio),不再据此跳过
    if _is_listening(page):
        logger.tip("[runner] 当前页含音频元素(单词发音/听力均会触发,继续尝试取答案)")

    # 数一下页面真正有多少道作答型题目,用于校验缓存/答案源条数对不对得上
    expected_count = _count_page_questions(page)
    if expected_count:
        logger.info(f"[runner] 页面共 {expected_count} 道题")

    result = resolver.resolve(info, title=title, expected_count=expected_count)

    if stats is not None and result is not None:
        stats.cache_hit = result.cache_hit
        stats.ai_used = result.source == "ai"
        stats.manual_hit = result.source == "manual"

    has_answers = bool(result and result.ok)
    if not has_answers:
        if harvest_first_pass and submit and not dry_run:
            logger.tip(
                "[runner] 没拿到答案,但 harvest_first_pass=True;"
                "空着提交以触发 U校园 review,扒正确答案进库"
            )
            fr = type("FR", (), {"filled": 0, "skipped": 0, "failed": 0})()
        else:
            logger.warn("[runner] 没拿到答案,跳过本题")
            if stats is not None:
                stats.error = "未拿到答案"
            return False, False
    else:
        # 填答案前再扫一遍噪音弹窗,避免 keyboard.type 时被焦点抢走
        _dismiss_noise_popups(page)
        fr = fill_page(page, result.answers, handlers=handlers)
        # 填完再扫一遍(打字过程中可能新弹出来)
        _dismiss_noise_popups(page)
        # 如果有失败,可能是被弹窗挡的;再清一遍噪音弹窗 + 重填一次
        if fr.failed > 0:
            n_closed = _dismiss_noise_popups(page)
            page.wait_for_timeout(400)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            n_closed += _dismiss_noise_popups(page)
            if n_closed > 0:
                logger.info(
                    f"[runner] 填答失败 {fr.failed} 题,已关 {n_closed} 个噪音弹窗,重填一次"
                )
                fr = fill_page(page, result.answers, handlers=handlers)
        logger.info(
            f"[runner] 已填 {fr.filled} 题 / 跳过 {fr.skipped} / 失败 {fr.failed}"
        )

    if stats is not None:
        stats.filled = fr.filled
        stats.skipped = fr.skipped
        stats.failed = fr.failed

    if dry_run:
        logger.tip("[runner] dry_run=True,不点击提交/下一页")
        return True, False

    redo_recommended = False
    if submit:
        ok = _safe_submit(page)
        if not ok:
            logger.warn("[runner] 本题提交未真正完成(可能被风控拦),将继续下一题")
            if stats is not None:
                stats.submitted = False
        else:
            if stats is not None:
                stats.submitted = True
            # 提交后检查完成弹窗,扒答案进缓存,顺便看正确率
            try:
                accuracy = _harvest_correct_answers(
                    page, info, resolver.cache, handlers,
                )
                if stats is not None:
                    stats.accuracy = accuracy
                    # accuracy 是 None 通常意味着主观题
                    if accuracy is None:
                        stats.page_type = PAGE_SUBJECTIVE
                if accuracy is not None and accuracy < 100:
                    redo_recommended = True
            except Exception as e:
                logger.warn(f"[runner] harvest 答案异常: {e}")
                if stats is not None and stats.error is None:
                    stats.error = f"harvest 异常: {e}"
    return True, redo_recommended


# ---------- 阅读型页面识别 + 模拟阅读 ----------

# 判断当前页是不是"无任何作答 input 的纯阅读页"。U校园 把课文/视频文本/学习指南
# 也当成必修项,但页面没有 radio/checkbox/textarea/textInput/select,这种页面
# 走 AI + 提交完全没意义,只需停留几秒触发学习记录,然后退出。
_PURE_READING_DETECT_JS = r"""
() => {
    const inputs = document.querySelectorAll('input, textarea, select');
    const radioGroups = new Set();
    const checkboxGroups = new Set();
    let textInputs = 0, textareas = 0, selects = 0;
    for (const el of inputs) {
        if (!(el.offsetParent || el.getClientRects().length)) continue;
        const tag = (el.tagName || '').toUpperCase();
        if (tag === 'TEXTAREA') {
            textareas++;
        } else if (tag === 'SELECT') {
            selects++;
        } else if (tag === 'INPUT') {
            const t = (el.type || 'text').toLowerCase();
            if (t === 'radio') {
                radioGroups.add(el.name || '');
            } else if (t === 'checkbox') {
                checkboxGroups.add(el.name || '');
            } else if (t === 'text' || t === '' || t === 'search') {
                textInputs++;
            }
        }
    }
    return {
        radioGroups: radioGroups.size,
        checkboxGroups: checkboxGroups.size,
        textInputs, textareas, selects,
        // 用作"作答类问题数"汇总:radio/checkbox 按组(name)算 1 道,
        // 其它每个 input 算 1 道
        questionCount: radioGroups.size + checkboxGroups.size + textInputs + textareas + selects,
    };
}
"""


def _is_pure_reading_page(page: Page) -> bool:
    """页面是否完全没有可作答元素 → 走阅读流程而不是答题."""
    try:
        info = page.evaluate(_PURE_READING_DETECT_JS) or {}
    except Exception:
        return False
    return int(info.get("questionCount") or 0) == 0


def _count_page_questions(page: Page) -> int:
    """统计当前页面共有多少道作答型题目(用于校验缓存/答案源条数)."""
    try:
        info = page.evaluate(_PURE_READING_DETECT_JS) or {}
    except Exception:
        return 0
    return int(info.get("questionCount") or 0)


_FINISH_READING_JS = r"""
() => {
    const btns = document.querySelectorAll('button, [role=button], a');
    for (const b of btns) {
        if (!(b.offsetParent || b.getClientRects().length)) continue;
        const t = (b.innerText || b.textContent || '').trim();
        if (/^(完成|我已学习|已读|已学|继续学习|完成学习|完成阅读|下一页|下一步)$/i.test(t)) {
            try { b.click(); return t; } catch (_) {}
        }
    }
    return null;
}
"""


def _stay_on_reading_page(page: Page, duration: int = 8) -> None:
    """阅读型页面:停留 + 模拟滚动,可能的话再点一下"完成/继续学习"按钮.

    动作分三段:开头停一会儿 → 缓慢滚到底 → 末尾再停一会儿。
    最后扫一眼是否有"完成/我已学习"按钮,有就点掉。
    """
    duration = max(3, int(duration))
    logger.info(
        f"[runner] 识别为阅读型页面(无任何作答 input),停留 {duration} 秒模拟阅读"
    )
    half = max(1, duration // 2)
    # 先在顶部停一会儿
    try:
        page.evaluate("() => window.scrollTo({top: 0})")
    except Exception:
        pass
    page.wait_for_timeout(max(1500, half * 600))

    # 缓慢滚动到底,分多次推进
    try:
        page.evaluate(
            "(steps) => {"
            "  const total = document.body.scrollHeight;"
            "  for (let i = 1; i <= steps; i++) {"
            "    setTimeout(() => window.scrollTo({"
            "      top: (total * i) / steps, behavior: 'smooth'"
            "    }), i * 600);"
            "  }"
            "}",
            max(3, half),
        )
    except Exception:
        pass
    page.wait_for_timeout(half * 1000)

    # 再在底部留一会儿
    page.wait_for_timeout(max(1500, (duration - half) * 600))

    # 看看有没有"完成/继续学习"按钮可以点
    try:
        clicked = page.evaluate(_FINISH_READING_JS)
        if clicked:
            logger.info(f"[runner] 阅读页点击了 {clicked!r}")
    except Exception:
        pass
    page.wait_for_timeout(1200)


def _do_must_exercise(
    target_page: Page,
    resolver: AnswerResolver,
    handlers: list[QuestionHandler],
    *,
    is_first: bool,
    submit: bool,
    dry_run: bool,
    harvest_first_pass: bool = False,
    reading_duration_sec: int = 8,
    stats: Optional[ExerciseStats] = None,
) -> bool:
    """返回是否建议外层重做本题(harvest 检测到正确率 < 100%).

    如给了 stats,会把详细分类/正确率/填答数据写入。
    """
    # 等到真正的题目页加载完成
    if not _wait_for_task_page(target_page, timeout_ms=12_000):
        logger.warn(
            f"[runner] 等不到题目页特征,继续尝试。当前 URL: {target_page.url}"
        )

    if is_first:
        try:
            target_page.wait_for_selector(".iKnow", timeout=3000).click()
        except Exception:
            pass
    _close_dialog_if_any(target_page)

    # 注入持续运行的噪音弹窗杀手(MutationObserver),录音机错误冒一次杀一次
    _install_popup_killer(target_page)
    # 再立刻同步关一遍现有的(jic 注入前已经弹了)
    _dismiss_noise_popups(target_page)

    # 区分页面类型:阅读型(无任何作答 input)直接停留 + 模拟阅读 + 退出,
    # 不调 AI、不空提交,避免触发风控也节省 API 配额
    if _is_pure_reading_page(target_page):
        if stats is not None:
            stats.page_type = PAGE_READING
        _stay_on_reading_page(target_page, duration=reading_duration_sec)
        _print_title(target_page)
        return False

    _success, redo = _answer_current_page(
        target_page, resolver, handlers,
        submit=submit, dry_run=dry_run,
        harvest_first_pass=harvest_first_pass,
        stats=stats,
    )
    _print_title(target_page)
    return redo


# ---------- "必修"行的练习入口枚举 ----------

# 把每个"必修"行内的所有练习入口图标打上 data-autounipus-idx 序号,
# 这样后续可以按序号点击,而且重新加载页面后只要再调一次就能恢复。
# 覆盖范围:不只是 .icon-lianxi(练习),还包括 Preview/写作/阅读 等所有
# class 形如 icon-XXX 的可见 iconfont,从而把 2-1 Preview / 2-4 Structure
# analysis & writing 这类被旧版漏掉的必修项一并扫进来。
_TAG_REQUIRED_EXERCISES_JS = r"""
() => {
    // 排除明显不是练习入口的常用图标(arrow/箭头/搜索/设置等装饰)
    const SKIP_RE = /^icon-(arrow|chevron|down|up|left|right|plus|minus|close|search|home|user|setting|menu|info|question$|warn|warning|error|success|loading|spinner|refresh|reload)/i;
    // 必修标记本身不能点
    const isRequiredMark = (el) =>
        el.classList.contains('icon-bixiu') ||
        el.classList.contains('iconCustumStyle');

    // 清空旧标签
    document.querySelectorAll('[data-autounipus-idx]').forEach(e => {
        e.removeAttribute('data-autounipus-idx');
        e.removeAttribute('data-autounipus-row-title');
        e.removeAttribute('data-autounipus-icon');
    });

    let n = 0;
    const seenIcon = new Set();
    const rowsSummary = [];
    const seenRows = new Set();

    for (const mark of document.querySelectorAll('.icon-bixiu')) {
        // 上溯找到这一行容器:行内必须包含至少 2 个 .iconfont
        // (1 个必修标记 + 1 个练习入口),否则继续往上找。
        let row = mark.parentElement;
        for (let i = 0; i < 8 && row; i++) {
            if (row.querySelectorAll('.iconfont').length >= 2) break;
            row = row.parentElement;
        }
        if (!row || seenRows.has(row)) continue;
        seenRows.add(row);

        // 行标题:抓行内可见文本,截 100 字
        const title = (row.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 100);

        // 行内所有 iconfont,过滤出像练习入口的
        let rowIcons = 0;
        for (const ic of row.querySelectorAll('.iconfont')) {
            if (isRequiredMark(ic)) continue;
            if (!(ic.offsetParent || ic.getClientRects().length)) continue;
            const iconName = Array.from(ic.classList).find(
                c => c.startsWith('icon-') && c !== 'iconfont'
            );
            if (!iconName) continue;
            if (SKIP_RE.test(iconName)) continue;
            if (seenIcon.has(ic)) continue;
            seenIcon.add(ic);

            ic.setAttribute('data-autounipus-idx', String(n));
            ic.setAttribute('data-autounipus-row-title', title);
            ic.setAttribute('data-autounipus-icon', iconName);
            n++;
            rowIcons++;
        }
        if (rowIcons > 0) {
            rowsSummary.push({ title, icons: rowIcons });
        }
    }

    return { total: n, rows: rowsSummary };
}
"""


def _tag_required_exercises(page: Page) -> int:
    """给所有"必修"行的练习入口打 data-autounipus-idx 序号,返回总数."""
    try:
        result = page.evaluate(_TAG_REQUIRED_EXERCISES_JS) or {}
        total = int(result.get("total") or 0)
        # 把 summary 缓存到 page 上,供 _list_required_exercises_summary 直接用
        setattr(page, "_autounipus_required_summary", result.get("rows") or [])
        return total
    except Exception as e:
        logger.warn(f"[runner] 打必修练习标记失败: {e}")
        setattr(page, "_autounipus_required_summary", [])
        return 0


def _list_required_exercises_summary(page: Page) -> list[str]:
    """返回每行 必修 的可读摘要,供日志输出."""
    rows = getattr(page, "_autounipus_required_summary", []) or []
    out = []
    for r in rows:
        title = (r.get("title") or "").strip() or "(无标题)"
        icons = r.get("icons") or 0
        out.append(f"{icons} 个入口 · {title[:80]}")
    return out


# ---------- 跑完一个课程后的汇总 ----------

def _render_run_summary(
    course_name: str,
    stats_list: list[ExerciseStats],
    *,
    started_at: float,
) -> None:
    """跑完一个课程后,把每题的状态聚合打印一份表格 + 平均正确率."""
    elapsed = max(0, int(time.time() - started_at))
    mins, secs = divmod(elapsed, 60)
    elapsed_str = f"{mins}分{secs}秒" if mins else f"{secs}秒"

    total = len(stats_list)
    by_type = {
        PAGE_ANSWERING: 0,
        PAGE_READING: 0,
        PAGE_SUBJECTIVE: 0,
        PAGE_UNKNOWN: 0,
    }
    accuracies: list[int] = []
    perfect = 0           # 100% 题数
    partial = 0           # 0 < 正确率 < 100
    failed_pages = 0      # 没拿到答案 / 异常 / 没提交成功
    cache_hits = 0
    ai_calls = 0
    manual_hits = 0
    submitted = 0

    less_than_100: list[ExerciseStats] = []   # 需要关注的题(<100% 或 异常)

    for s in stats_list:
        by_type[s.page_type] = by_type.get(s.page_type, 0) + 1
        if s.cache_hit:
            cache_hits += 1
        if s.ai_used:
            ai_calls += 1
        if s.manual_hit:
            manual_hits += 1
        if s.submitted:
            submitted += 1
        if s.error:
            failed_pages += 1
            less_than_100.append(s)
            continue
        if s.accuracy is not None:
            accuracies.append(s.accuracy)
            if s.accuracy >= 100:
                perfect += 1
            else:
                partial += 1
                less_than_100.append(s)

    avg_acc = (
        round(sum(accuracies) / len(accuracies), 1) if accuracies else None
    )

    # 输出
    logger.info("=" * 60)
    logger.info(f"📋 课程汇总: {course_name}")
    logger.info("=" * 60)
    logger.info(f"  耗时             {elapsed_str}")
    logger.info(f"  必修练习入口数   {total}")
    logger.info(
        f"  · 答题型         {by_type[PAGE_ANSWERING]} (其中已提交 {submitted})"
    )
    logger.info(f"  · 阅读型         {by_type[PAGE_READING]}")
    logger.info(f"  · 主观题         {by_type[PAGE_SUBJECTIVE]}")
    if by_type[PAGE_UNKNOWN]:
        logger.info(f"  · 未识别         {by_type[PAGE_UNKNOWN]}")
    logger.info("")
    if accuracies:
        logger.info(
            f"  本次平均正确率   {avg_acc}%   "
            f"(满分 {perfect} 题 / 部分对 {partial} 题 / 失败 {failed_pages} 题)"
        )
    elif failed_pages:
        logger.warn(f"  本次没有任何题拿到正确率(失败 {failed_pages} 题)")
    else:
        logger.info(
            f"  本次没有客观题评分(全部为阅读 / 主观题 / 已提交但未触发完成弹窗)"
        )

    logger.info("")
    logger.info(
        f"  答案来源:        cache 命中 {cache_hits} 题 · AI 调用 {ai_calls} 题 · "
        f"人工答案库命中 {manual_hits} 题"
    )

    # 列出需要人工关注的题(< 100% / 异常)
    if less_than_100:
        logger.info("")
        logger.warn("  ⚠ 以下题目正确率 < 100% 或有异常,可能需要人工核对:")
        for s in less_than_100[:30]:
            tag = (
                f"{s.accuracy}%" if s.accuracy is not None
                else (s.error or "未拿到正确率")
            )
            row = (s.row_title or "(无标题)")[:60]
            logger.warn(
                f"    · 练习 {s.idx}/{s.total}  task={s.task_id or '?':<8}  "
                f"{tag:<10}  行: {row}"
            )
        if len(less_than_100) > 30:
            logger.warn(f"    · …(还有 {len(less_than_100) - 30} 项未列出)")
    logger.info("=" * 60)


def _run_class(
    page: Page,
    class_url: str,
    resolver: AnswerResolver,
    handlers: list[QuestionHandler],
    *,
    submit: bool,
    dry_run: bool,
    cancel: Optional[threading.Event],
    sniffer: Optional[NetworkSniffer] = None,
    harvest_first_pass: bool = False,
    reading_duration_sec: int = 8,
) -> None:
    page.goto(class_url)

    # 课程标题
    try:
        course_text = page.wait_for_selector(".cc_course_intro_text").text_content() or ""
        course_name = course_text.strip().splitlines()[0] if course_text.strip() else "(未知课程)"
    except Exception:
        course_name = "(未知课程)"
    logger.info(f"当前课程: {course_name}")

    page.wait_for_selector(".icon-bixiu.iconCustumStyle.iconfont")

    # 收集"必修"练习入口
    total = _tag_required_exercises(page)
    if total == 0:
        logger.warn(f"课程 {course_name} 未找到必修练习")
        return

    summary = _list_required_exercises_summary(page)
    logger.info(f"识别到 {total} 个必修练习入口(覆盖 {len(summary)} 行 必修):")
    for line in summary[:30]:
        logger.info(f"  · {line}")
    if len(summary) > 30:
        logger.info(f"  · …(还有 {len(summary) - 30} 行未列出)")
    logger.info("开始逐一作答")

    all_stats: list[ExerciseStats] = []
    class_started_at = time.time()

    for idx in range(total):
        _check_cancel(cancel)
        target_page: Optional[Page] = None
        ex_stats = ExerciseStats(idx=idx + 1, total=total)
        all_stats.append(ex_stats)
        try:
            # 回目录,重新打标签(避免 element handle 失效)
            page.goto(class_url)
            page.wait_for_selector(".icon-bixiu.iconCustumStyle.iconfont")
            fresh_total = _tag_required_exercises(page)
            if idx >= fresh_total:
                logger.warn(
                    f"重新打标后练习数量从 {total} 变为 {fresh_total},"
                    f"提前结束"
                )
                break

            ex_locator = page.locator(f"[data-autounipus-idx=\"{idx}\"]")
            if ex_locator.count() == 0:
                logger.warn(f"找不到第 {idx + 1} 个练习入口(标记丢失),跳过")
                ex_stats.error = "练习入口丢失"
                continue

            # 给当前要点的图标一个直观日志
            try:
                row_title = ex_locator.evaluate(
                    "el => el.getAttribute('data-autounipus-row-title') || ''"
                )
                icon_kind = ex_locator.evaluate(
                    "el => el.getAttribute('data-autounipus-icon') || ''"
                )
                ex_stats.row_title = row_title
                ex_stats.icon_kind = icon_kind
                logger.info(
                    f"[runner] ⇒ 练习 {idx + 1}/{total}: 行={row_title!r} 图标={icon_kind!r}"
                )
            except Exception:
                pass

            # 抓包队列每题清一次,避免上一题的响应混淆判断
            if sniffer is not None:
                sniffer.reset()
            target_page = _click_exercise_open_target(
                page, ex_locator.first, sniffer,
            )
            if target_page is not page:
                logger.debug(
                    f"练习在新标签页打开: {target_page.url}",
                    enabled=True,
                )
            _do_must_exercise(
                target_page, resolver, handlers,
                is_first=(idx == 0),
                submit=submit,
                dry_run=dry_run,
                harvest_first_pass=harvest_first_pass,
                reading_duration_sec=reading_duration_sec,
                stats=ex_stats,
            )
        except CancelledError:
            raise
        except Exception as e:
            logger.error(f"第 {idx + 1} 个练习作答异常: {e}")
            ex_stats.error = str(e)
        finally:
            # 关闭题目页,回到目录
            if target_page is not None and target_page is not page:
                try:
                    target_page.close()
                except Exception:
                    pass

    logger.info(f"课程: {course_name} 已完成!")
    _render_run_summary(course_name, all_stats, started_at=class_started_at)


# ============================================================
# 对外入口
# ============================================================

def _build_resolver(
    page: Page,
    config: AppConfig,
    sniffer: NetworkSniffer,
) -> AnswerResolver:
    cache = AnswerCache(path=config.cache.path, enabled=config.cache.enabled)
    # 经过实测,你这老版部署上 ContentAPI / Sniff / 新版 API / Legacy 暴力反推
    # 都拿不到答案(只会刷一堆 code=1 参数错误日志),裁掉。
    # 只保留:
    #   1) 用户预录的答案库(听力题用,以及任何用户已经验证过的题)
    #   2) AI 答题(覆盖其它所有题型)
    sources: list[AnswerSource] = [
        ManualAnswerSource("data/manual_answers.json"),
    ]
    if config.ai.enabled:
        sources.append(AIAnswerSource(config.ai, page=page))
    return AnswerResolver(sources=sources, cache=cache)


def run_auto_mode(
    config: AppConfig,
    cancel: Optional[threading.Event] = None,
) -> None:
    handlers = _select_handlers(config)
    logger.info(f"启用 handler: {[h.name for h in handlers]}")
    if config.dry_run:
        logger.tip("当前为 dry_run 模式:填答案但不会提交")
    if config.harvest_first_pass:
        logger.tip(
            "harvest_first_pass=True:答案没拿到也会强行提交,触发 review 模式扒答案进库"
        )

    with sync_playwright() as p:
        try:
            page, sniffer = _init_page(p, config)
            resolver = _build_resolver(page, config, sniffer)
            for url in config.class_urls:
                _check_cancel(cancel)
                if "unipus" not in url:
                    logger.warn(f"忽略非 unipus 链接: {url}")
                    continue
                _run_class(
                    page, url, resolver, handlers,
                    submit=True, dry_run=config.dry_run, cancel=cancel,
                    sniffer=sniffer,
                    harvest_first_pass=config.harvest_first_pass,
                    reading_duration_sec=config.reading_duration_sec,
                )
            logger.info("所有课程已完成!!")
        except CancelledError as e:
            logger.system(str(e))
        except TargetClosedError:
            logger.error("糟糕,网页关闭了!")
        except TimeoutError:
            logger.error("程序长时间无响应,自动退出...")


def run_assist_mode(
    config: AppConfig,
    cancel: Optional[threading.Event] = None,
    trigger: Optional[threading.Event] = None,
) -> None:
    """辅助模式.

    Args:
        cancel:  外部取消信号(任意时刻)
        trigger: webui 用的"现在扫描"信号;为 None 时走 stdin input() (CLI)
    """
    handlers = _select_handlers(config)
    logger.info(f"启用 handler: {[h.name for h in handlers]}")

    with sync_playwright() as p:
        try:
            page, sniffer = _init_page(p, config)
            resolver = _build_resolver(page, config, sniffer)
            logger.system("请先进入题目界面,然后回到本程序触发扫描")
            while True:
                _check_cancel(cancel)
                if trigger is None:
                    # CLI 模式:阻塞等用户敲回车
                    input("[System]按 Enter 获取答案:")
                else:
                    # WebUI 模式:轮询 trigger,允许中途取消
                    while True:
                        _check_cancel(cancel)
                        if trigger.wait(timeout=0.5):
                            trigger.clear()
                            break
                _check_cancel(cancel)
                # 抓包记录每次手动触发都重置一次,聚焦当前页
                sniffer.reset()
                page.reload()
                _close_dialog_if_any(page)
                _install_popup_killer(page)
                _dismiss_noise_popups(page)
                ok, _redo = _answer_current_page(
                    page, resolver, handlers,
                    submit=False, dry_run=True,
                )
                if ok:
                    _print_title(page)
                else:
                    logger.error("当前页面没拿到答案,请确认是否在题目页")
        except CancelledError as e:
            logger.system(str(e))
        except TargetClosedError:
            logger.error("糟糕,网页关闭了!")
        except TimeoutError:
            logger.error("程序长时间无响应,自动退出...")
