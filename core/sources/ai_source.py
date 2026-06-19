"""AI 兜底答案源.

设计:
- 抓取当前题目页的可见题面文本
- 喂给 OpenAI Chat Completions 兼容 API(DeepSeek / 通义 / Kimi / 本地 Ollama 都行)
- 解析返回的 JSON 数组,每条对应一个空位/题目

适用题型:
- ✅ 单选 / 多选(返回字母)
- ✅ 选词填空 / 填空(返回单词或短语)
- ✅ 翻译 / 简答(返回完整文本)
- ❌ 听力(没音频文本无法回答,自动跳过)
"""
from __future__ import annotations

import json
import re
from typing import Any, List, Optional

import requests
from playwright.sync_api import Page

from .. import logger
from ..config import AIConfig
from ..unipus.api_client import ParsedAnswer
from .base import AnswerQuery, AnswerResult, AnswerSource


_LISTENING_JS = """
() => {
    // 只有当 audio 是主体内容时(题目区有专门的播放器)才算
    // 单词卡片/题面里的小喇叭不算
    const big = document.querySelector(
        '[class*=audio-player], [class*=audioPlayer], [class*=listening i]:not([class*=icon])'
    );
    return !!big;
}
"""


# 标题里出现这些关键词就当成听力题(优先用标题判断)
_LISTENING_TITLE_KEYWORDS = (
    "listening",
    "listen and",
    "listen to",
    "watch and listen",
    "audio passage",
    "听力",
    "听写",
)


_QUESTION_EXTRACTION_JS = r"""
() => {
    // 尝试只取"题目主体区"的文本,缩短 prompt
    const sels = [
        '[class*=questionContainer]',
        '[class*=question-area]',
        '[class*=questionPanel]',
        '[class*=task-content]',
        '[class*=mainPanel]',
        '#root',
        'main',
        'body',
    ];
    let root = null;
    for (const s of sels) {
        const el = document.querySelector(s);
        if (el) { root = el; break; }
    }
    if (!root) root = document.body;

    const inner = root.innerText || '';
    // 单词板/词库:某些题型把候选词放在 grid/table 里,要保留
    let extra = '';
    const wordBank = root.querySelector('[class*=wordBank], [class*=word-bank], [class*=optionsList]');
    if (wordBank) extra = '\n\n候选词:\n' + (wordBank.innerText || '');

    return (inner + extra).slice(0, 4000);
}
"""


# 探测页面上有哪些题型 / 数量,告诉 AI 返回什么形式的答案
_QUESTION_STRUCTURE_JS = r"""
() => {
    const inputs = document.querySelectorAll('input, textarea, select');
    const radioGroups = new Set();
    const checkboxGroups = new Set();
    let textInputs = 0;
    let textareas = 0;
    let selects = 0;
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
        textInputs: textInputs,
        textareas: textareas,
        selects: selects,
    };
}
"""


_SYSTEM_PROMPT = (
    "You are an expert tutor for the Chinese college English textbook "
    "'新视野大学英语读写教程2(第三版)' / 'New Vision College English Reading and Writing Course 2, Third Edition'. "
    "Solve the given exercise. Return ONLY a JSON array of answers, in question order, with no commentary or markdown. "
    "Rules: "
    "- For single/multi choice questions, return the option letter(s) like \"A\" or \"AC\". "
    "- For fill-in-blank or word-building questions, return the exact word/phrase to fill, with correct grammatical form (singular/plural, tense, derivation). "
    "- For banked cloze, pick from the provided word bank exactly. "
    "- For translation, return the full translated sentence. "
    "- If a question is unclear or cannot be answered, return an empty string \"\" for that slot. "
    "Return shape example: [\"successful\", \"cooperative\", \"A\", \"speculate\"]."
)


class AIAnswerSource(AnswerSource):
    name = "ai"

    def __init__(self, config: AIConfig, page: Optional[Page] = None):
        self.config = config
        self.page = page  # 由 runner 在构造 resolver 时注入

    @property
    def available(self) -> bool:
        return self.config.enabled and bool(self.config.api_key) and self.page is not None

    # ------------------ 主入口 ------------------

    def fetch(self, query: AnswerQuery) -> Optional[AnswerResult]:
        if not self.available:
            return None
        if self.page is None:
            return None

        # 听力题跳过(只看标题。DOM 检查太容易被单词发音播放器误伤)
        if self._is_listening_by_title(query.title):
            logger.tip(
                f"[{self.name}] 标题里有听力关键词 ({query.title[:80]!r}),"
                f"AI 无法处理音频,跳过(应由人工答案库覆盖)"
            )
            return None

        content = self._extract_question_content()
        if not content or len(content) < 30:
            logger.warn(f"[{self.name}] 题面文本过短,跳过 (len={len(content)})")
            return None

        # 探测页面上的题型结构,告诉 AI 该返回什么形式的答案
        structure = self._detect_structure()
        expected_count = self._expected_answer_count(structure)
        if expected_count == 0:
            logger.tip(
                f"[{self.name}] 页面未探测到任何 input/textarea/radio/checkbox,"
                f"无需 AI 答题(应由上层走阅读流程),跳过"
            )
            return None
        prompt = self._build_prompt(content, query.title, structure, expected_count)

        # 模型尝试顺序:主模型 + 配置的 fallback 模型,任一拿到非空响应即停。
        # 中转/上游对特定 prompt 偶尔会"软拒答"(返回 200 + 空内容),
        # 换一个模型大概率能跑通。
        models_to_try: list[str] = []
        seen = set()
        for m in [self.config.model, *self.config.fallback_models]:
            m = (m or "").strip()
            if m and m not in seen:
                seen.add(m)
                models_to_try.append(m)
        if not models_to_try:
            logger.error(f"[{self.name}] 没有可用的模型(model 和 fallback_models 都为空)")
            return None

        reply: Optional[str] = None
        used_model = ""
        for mi, mname in enumerate(models_to_try):
            tag = "主模型" if mi == 0 else f"fallback#{mi}"
            logger.info(
                f"[{self.name}] {tag} 调用 {mname} (题面 {len(content)} 字, 期望 {expected_count} 条答案)"
                f" 结构 {structure}"
            )
            reply = self._chat_completion(prompt, model=mname)

            # 空响应:先把题面砍到一半再试一次同模型
            if reply == "" and len(content) > 1500:
                shrunk = content[:1500]
                logger.tip(
                    f"[{self.name}] {mname} 第一次返回空,把题面从 {len(content)} 字砍到 {len(shrunk)} 字重试"
                )
                shrunk_prompt = self._build_prompt(
                    shrunk, query.title, structure, expected_count
                )
                reply = self._chat_completion(shrunk_prompt, model=mname)

            if reply:
                used_model = mname
                if mi > 0:
                    logger.info(f"[{self.name}] fallback 模型 {mname} 拿到响应")
                break
            elif reply == "":
                logger.warn(
                    f"[{self.name}] {mname} 返回空,准备试下一个模型(剩余 {len(models_to_try) - mi - 1} 个)"
                )
            else:
                # reply is None,说明 HTTP/网络出错,换模型也大概率不行 — 但还是试一下
                logger.warn(
                    f"[{self.name}] {mname} 调用异常,试下一个模型"
                )

        if not reply:
            logger.error(
                f"[{self.name}] 全部 {len(models_to_try)} 个模型都没拿到响应,放弃此题"
            )
            return None

        answers = self._parse_reply(reply)
        if not answers:
            logger.warn(f"[{self.name}] 解析 AI 回复失败,前 200 字:\n  {reply[:200]}")
            return None

        # 过滤:全空当成失败,让上层 fallback 继续走
        real = [
            a for a in answers
            if a.answers and any(str(s).strip() for s in a.answers)
        ]
        if not real:
            logger.warn(
                f"[{self.name}] AI 回复解析出 {len(answers)} 条但全为空,视为失败"
                f"。原始回复前 200 字:\n  {reply[:200]}"
            )
            return None
        if len(real) < len(answers):
            logger.warn(
                f"[{self.name}] 过滤掉 {len(answers) - len(real)} 条空答案,"
                f"实际可用 {len(real)} 条"
            )
        # 重新编号
        for i, a in enumerate(real):
            a.id = i

        logger.info(f"[{self.name}] 解析成功,共 {len(real)} 条答案")
        # 主观题(只有 textarea,没有客观题输入)无机器评分,review 也不展示标答。
        # AI 这种页面有可能写错(比如把简答题答成单词),不该写入缓存防止污染下一轮。
        cacheable = True
        if structure:
            objective_inputs = (
                structure.get("radioGroups", 0)
                + structure.get("checkboxGroups", 0)
                + structure.get("textInputs", 0)
                + structure.get("selects", 0)
            )
            textareas = structure.get("textareas", 0)
            if textareas > 0 and objective_inputs == 0:
                cacheable = False
                logger.tip(
                    f"[{self.name}] 当前页只含 textarea(主观题),"
                    f"AI 答案不写入缓存(避免一次写错永远填错)"
                )
        return AnswerResult(answers=real, source=self.name, cacheable=cacheable)

    # ------------------ 工具 ------------------

    def _is_listening_by_title(self, title: str) -> bool:
        """先看标题(最准):练习名里有 Listening / 听力 / Watch and listen 才是真听力."""
        t = (title or "").lower()
        if not t:
            return False
        return any(kw in t for kw in _LISTENING_TITLE_KEYWORDS)

    def _is_listening_by_dom(self) -> bool:
        """兜底:页面有专门的 audio-player 容器(不是单词小喇叭)."""
        try:
            return bool(self.page.evaluate(_LISTENING_JS))
        except Exception:
            return False

    def _extract_question_content(self) -> str:
        try:
            return (self.page.evaluate(_QUESTION_EXTRACTION_JS) or "").strip()
        except Exception as e:
            logger.warn(f"[{self.name}] 提取题面失败: {e}")
            return ""

    def _detect_structure(self) -> dict:
        try:
            return self.page.evaluate(_QUESTION_STRUCTURE_JS) or {}
        except Exception:
            return {}

    @staticmethod
    def _expected_answer_count(structure: dict) -> int:
        """根据探测到的题型结构估算应返回多少个答案."""
        return (
            structure.get("radioGroups", 0)
            + structure.get("checkboxGroups", 0)
            + structure.get("textInputs", 0)
            + structure.get("textareas", 0)
            + structure.get("selects", 0)
        )

    def _build_prompt(
        self,
        content: str,
        title: str = "",
        structure: Optional[dict] = None,
        expected_count: int = 0,
    ) -> str:
        head = f"练习标题: {title}\n\n" if title else ""

        s = structure or {}
        struct_lines: list[str] = []
        if s.get("radioGroups"):
            struct_lines.append(
                f"- {s['radioGroups']} 道单选题:返回大写字母,如 \"A\" / \"B\" / \"C\" / \"D\""
            )
        if s.get("checkboxGroups"):
            struct_lines.append(
                f"- {s['checkboxGroups']} 道多选题:返回字母组合,如 \"AC\" 或 \"ABD\""
            )
        if s.get("selects"):
            struct_lines.append(
                f"- {s['selects']} 个下拉选词题:返回正确选项的完整文字(如 \"intensify\")"
            )
        if s.get("textInputs"):
            struct_lines.append(
                f"- {s['textInputs']} 个文本填空:返回单个单词或短语(注意时态、单复数、词性)"
            )
        if s.get("textareas"):
            struct_lines.append(
                f"- {s['textareas']} 道简答/翻译题:**每题必须返回完整的英文/中文句子**,"
                f"不能只给一个单词。读通题面再作答。"
            )

        struct_section = ""
        if struct_lines:
            struct_section = (
                "【页面题型结构(自动探测)】\n"
                + "\n".join(struct_lines)
                + (
                    f"\n→ 总共需要返回 **{expected_count}** 个答案,顺序与页面从上到下一致。\n\n"
                    if expected_count
                    else "\n\n"
                )
            )

        return (
            f"{head}{struct_section}"
            f"请解答以下练习,**必须**只返回 JSON 数组(英文双引号),"
            f"不要解释、不要 markdown 代码块、不要 ```json 等围栏。\n\n"
            f"【题目内容】\n{content}"
        )

    def _chat_completion(self, user_prompt: str, *, model: Optional[str] = None) -> Optional[str]:
        """发一次 chat/completions,返回 message.content.

        失败原因(全部记日志):
            - 网络异常 / HTTP 非 200       → return None
            - 响应不是 JSON 也不是 SSE     → return None
            - 解析成功但 content 为空      → return ""(让上层决定要不要重试)
        """
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or self.config.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "stream": False,
        }

        try:
            resp = requests.post(
                url,
                headers=headers,
                data=json.dumps(payload),
                timeout=self.config.timeout,
            )
        except Exception as e:
            logger.error(f"[{self.name}] 请求异常: {e}")
            return None

        if resp.status_code != 200:
            logger.error(
                f"[{self.name}] HTTP {resp.status_code}: {resp.text[:300]}"
            )
            return None

        text = resp.text
        # 1) 先按普通 JSON 解
        data: Optional[dict] = None
        try:
            data = json.loads(text)
        except Exception:
            # 2) 中转站强制 SSE 时,把 chunks 拼起来
            data = _parse_sse_chunks(text)
            if data is None:
                logger.error(
                    f"[{self.name}] 响应既不是 JSON 也不是 SSE:\n  {text[:300]}"
                )
                return None
            logger.debug(f"[{self.name}] SSE 流已拼装成完整响应", enabled=True)

        # 取 content。注意:某些中转站会返回 choices=[] 或 content=""(模型 0 token 输出),
        # 此时 data 已是合法 dict 但 content 为空 — 返回空字符串让上层走重试路径。
        try:
            choices = data.get("choices") or []
            if not choices:
                usage = data.get("usage") or {}
                logger.warn(
                    f"[{self.name}] 中转站返回 choices=[](模型 0 token 输出),"
                    f"usage={usage}。常见原因:内容审核 / 模型超时 / 限流"
                )
                return ""
            content = (choices[0].get("message") or {}).get("content")
            if content is None:
                content = ""
            if not content.strip():
                fr = choices[0].get("finish_reason")
                logger.warn(
                    f"[{self.name}] AI content 为空 (finish_reason={fr!r}, "
                    f"usage={data.get('usage')})"
                )
                return ""
            return content
        except Exception as e:
            logger.error(
                f"[{self.name}] 响应里取不到 content: {e}\n  {str(data)[:300]}"
            )
            return None

    def _parse_reply(self, reply: str) -> List[ParsedAnswer]:
        """从 AI 回复里抽出 JSON 数组."""
        text = reply.strip()
        # 去掉 markdown 代码块
        if text.startswith("```"):
            text = re.sub(r"^```\w*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)
            text = text.strip()

        # 尝试两种结构:
        #   ["a","b","c"]   纯数组
        #   {"answers":[...]}  / {"...": [...]}  对象包数组
        candidates: list[Any] = []
        try:
            candidates.append(json.loads(text))
        except Exception:
            # 找文本里第一个 [...]
            m = re.search(r"\[[\s\S]*\]", text)
            if m:
                try:
                    candidates.append(json.loads(m.group(0)))
                except Exception:
                    pass

        for c in candidates:
            if isinstance(c, list):
                return self._items_to_parsed(c)
            if isinstance(c, dict):
                # 找第一个 list value
                for v in c.values():
                    if isinstance(v, list):
                        return self._items_to_parsed(v)
        return []

    @staticmethod
    def _items_to_parsed(items: list) -> List[ParsedAnswer]:
        out: list[ParsedAnswer] = []
        for x in items:
            if isinstance(x, list):
                # 多选答案展平为 "A,C"
                x = ",".join(str(i) for i in x if i)
            elif isinstance(x, dict):
                x = x.get("answer") or x.get("text") or ""
            out.append(ParsedAnswer(answers=[str(x)], id=len(out)))
        return out


# ============== SSE 流式响应拼装 ==============


def _parse_sse_chunks(text: str) -> Optional[dict]:
    """把 OpenAI Chat Completions 的 SSE 流拼成一份完整响应.

    部分中转站即便我们传 ``stream:false`` 也强制返回 SSE,
    长成这样::

        data: {"choices":[{"delta":{"role":"assistant"}}]}
        data: {"choices":[{"delta":{"content":"O"}}]}
        data: {"choices":[{"delta":{"content":"K"}}]}
        data: [DONE]

    返回值:
        - None        没有任何能解析的 data: 块(完全不是 SSE)
        - dict        合法 SSE。注意 content 可能为空字符串(模型 0 token 输出),
                      上层应判断 message.content 长度并按"AI 空响应"处理。
    """
    if "data:" not in text:
        return None
    role = ""
    content_parts: list[str] = []
    final_id = None
    final_model = None
    finish_reason = None
    usage: Optional[dict] = None
    had_chunks = False  # 至少成功 parse 了一个 data: JSON 块

    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except Exception:
            continue
        had_chunks = True
        if chunk.get("id"):
            final_id = chunk["id"]
        if chunk.get("model"):
            final_model = chunk["model"]
        if chunk.get("usage"):
            usage = chunk["usage"]
        for ch in chunk.get("choices", []) or []:
            delta = ch.get("delta") or {}
            if delta.get("role"):
                role = delta["role"]
            if delta.get("content"):
                content_parts.append(delta["content"])
            # 部分实现把内容塞在 message.content 而不是 delta.content
            msg = ch.get("message") or {}
            if msg.get("content"):
                content_parts.append(msg["content"])
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]

    if not had_chunks:
        return None

    return {
        "id": final_id or "",
        "model": final_model or "",
        "usage": usage,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": role or "assistant",
                    "content": "".join(content_parts),
                },
                "finish_reason": finish_reason,
            }
        ],
    }
