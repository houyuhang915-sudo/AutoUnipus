"""Flask 后端.

职责:
- 读/写 account.json
- 启动 / 停止 runner 线程(auto / assist)
- assist 模式下中转"扫描当前页"触发
- 历史日志拉取 + SSE 实时推送
- 缓存查询/清空
"""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
import traceback
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify, request, send_from_directory
import requests as _requests

from core import logger
from core.config import ALL_HANDLERS, AppConfig
from core.runner import run_assist_mode, run_auto_mode


# -------------------- 全局运行状态 --------------------

class RunnerState:
    """单例式的 runner 状态.持有当前线程、cancel/trigger event."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.mode: Optional[str] = None  # "auto" / "assist"
        self.started_at: Optional[float] = None
        self.cancel = threading.Event()
        self.trigger = threading.Event()
        self.last_error: Optional[str] = None

    def is_running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def to_dict(self) -> dict:
        return {
            "running": self.is_running(),
            "mode": self.mode,
            "started_at": self.started_at,
            "elapsed": (time.time() - self.started_at) if self.started_at else None,
            "last_error": self.last_error,
        }


_state = RunnerState()


def _runner_thread_target(mode: str, config: AppConfig) -> None:
    try:
        if mode == "auto":
            run_auto_mode(config, cancel=_state.cancel)
        elif mode == "assist":
            run_assist_mode(config, cancel=_state.cancel, trigger=_state.trigger)
        else:
            logger.error(f"未知模式: {mode}")
    except Exception as e:
        _state.last_error = f"{type(e).__name__}: {e}"
        logger.error(f"runner 异常: {_state.last_error}")
        logger.debug(traceback.format_exc(), enabled=True)
    finally:
        with _state.lock:
            _state.mode = None
            _state.started_at = None
            # cancel/trigger 留着,下次启动前 reset


# -------------------- Flask App --------------------

def create_app(config_path: str = "account.json") -> Flask:
    static_dir = Path(__file__).parent / "static"
    app = Flask(__name__, static_folder=str(static_dir), static_url_path="/static")
    app.config["JSON_AS_ASCII"] = False
    app.config["CONFIG_PATH"] = config_path

    # --------- 静态首页 ---------

    @app.route("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    # --------- 配置 ---------

    @app.route("/api/config", methods=["GET"])
    def get_config():
        path = app.config["CONFIG_PATH"]
        try:
            cfg = AppConfig.load(path)
        except FileNotFoundError:
            return jsonify({"error": f"未找到 {path}"}), 404
        data = cfg.to_dict()
        # 不暴露明文密码到前端(可选;此处保留,因为是本机自用 webui)
        return jsonify({"config": data, "all_handlers": list(ALL_HANDLERS)})

    @app.route("/api/config", methods=["PUT"])
    def put_config():
        path = app.config["CONFIG_PATH"]
        body = request.get_json(silent=True) or {}
        try:
            # 直接把前端传来的 JSON 落盘,再用 AppConfig.load 校验
            Path(path).write_text(
                json.dumps(body, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            cfg = AppConfig.load(path)
            err = cfg.validate()
            if err:
                return jsonify({"error": err}), 400
            return jsonify({"ok": True, "config": cfg.to_dict()})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    # --------- 运行控制 ---------

    @app.route("/api/run", methods=["POST"])
    def run_start():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode", "auto")
        if mode not in ("auto", "assist"):
            return jsonify({"error": "mode 必须是 auto 或 assist"}), 400

        with _state.lock:
            if _state.is_running():
                return jsonify({"error": f"已在运行: mode={_state.mode}"}), 409

            try:
                cfg = AppConfig.load(app.config["CONFIG_PATH"])
            except Exception as e:
                return jsonify({"error": f"加载配置失败: {e}"}), 500
            err = cfg.validate()
            if err:
                return jsonify({"error": f"配置不合法: {err}"}), 400

            _state.cancel.clear()
            _state.trigger.clear()
            _state.last_error = None
            _state.mode = mode
            _state.started_at = time.time()
            t = threading.Thread(
                target=_runner_thread_target,
                args=(mode, cfg),
                daemon=True,
                name=f"AutoUnipus-{mode}",
            )
            _state.thread = t
            t.start()
        return jsonify({"ok": True, "state": _state.to_dict()})

    @app.route("/api/stop", methods=["POST"])
    def run_stop():
        if not _state.is_running():
            return jsonify({"ok": True, "state": _state.to_dict()})
        _state.cancel.set()
        # trigger 也设一下,让 assist 的 wait 立刻返回去检查 cancel
        _state.trigger.set()
        return jsonify({"ok": True, "state": _state.to_dict()})

    @app.route("/api/trigger", methods=["POST"])
    def assist_trigger():
        if not _state.is_running() or _state.mode != "assist":
            return jsonify({"error": "辅助模式未在运行"}), 400
        _state.trigger.set()
        return jsonify({"ok": True})

    @app.route("/api/state", methods=["GET"])
    def get_state():
        return jsonify(_state.to_dict())

    # --------- 日志 ---------

    @app.route("/api/logs", methods=["GET"])
    def get_logs():
        limit = int(request.args.get("limit", "500"))
        return jsonify({"logs": logger.get_recent(limit)})

    @app.route("/api/logs/clear", methods=["POST"])
    def clear_logs():
        logger.clear_buffer()
        return jsonify({"ok": True})

    @app.route("/api/logs/stream")
    def stream_logs():
        def gen():
            q = logger.subscribe()
            try:
                # 先把当前缓冲推一遍,前端断线重连后能看到完整历史
                for entry in logger.get_recent(200):
                    yield _sse(entry)
                while True:
                    try:
                        entry = q.get(timeout=15)
                        yield _sse(entry)
                    except queue.Empty:
                        # 心跳,防代理超时关连接
                        yield ": keepalive\n\n"
            finally:
                logger.unsubscribe(q)

        return Response(gen(), mimetype="text/event-stream")

    # --------- 缓存 ---------

    @app.route("/api/cache/stats", methods=["GET"])
    def cache_stats():
        try:
            cfg = AppConfig.load(app.config["CONFIG_PATH"])
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        path = Path(cfg.cache.path)
        if not path.exists():
            return jsonify({"path": str(path), "rows": 0, "size_bytes": 0})
        try:
            with closing(sqlite3.connect(path)) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM answer_cache"
                ).fetchone()
                count = row[0] if row else 0
            return jsonify({
                "path": str(path),
                "rows": count,
                "size_bytes": path.stat().st_size,
            })
        except Exception as e:
            return jsonify({"path": str(path), "error": str(e)}), 500

    @app.route("/api/cache", methods=["DELETE"])
    def cache_clear():
        try:
            cfg = AppConfig.load(app.config["CONFIG_PATH"])
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        path = Path(cfg.cache.path)
        if path.exists():
            try:
                path.unlink()
                logger.info(f"已清空答案缓存: {path}")
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})

    # --------- 手动答案库 ---------

    MANUAL_PATH = Path("data/manual_answers.json")

    @app.route("/api/manual-answers", methods=["GET"])
    def get_manual_answers():
        if not MANUAL_PATH.exists():
            return jsonify({"raw": "{}"})
        try:
            return jsonify({"raw": MANUAL_PATH.read_text(encoding="utf-8")})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/manual-answers", methods=["PUT"])
    def put_manual_answers():
        body = request.get_json(silent=True) or {}
        raw = body.get("raw", "")
        # 校验是合法 JSON
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return jsonify({"error": "顶层必须是 JSON 对象"}), 400
        except Exception as e:
            return jsonify({"error": f"JSON 不合法: {e}"}), 400
        try:
            MANUAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            MANUAL_PATH.write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"已保存手动答案库: {MANUAL_PATH}")
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})

    @app.route("/api/manual-answers/add", methods=["POST"])
    def append_manual_answers():
        """快速录入一条答案 — 表单友好.

        body: { kind: "task" | "qid" | "title", key: "u2g68", answers: ["A", "B", ...] }
        会合并到 manual_answers.json 中对应的字典。
        """
        body = request.get_json(silent=True) or {}
        kind = body.get("kind", "task")
        key = (body.get("key") or "").strip()
        answers = body.get("answers") or []
        if not key:
            return jsonify({"error": "key 不能为空"}), 400
        if not isinstance(answers, list) or not answers:
            return jsonify({"error": "answers 必须是非空数组"}), 400
        # 清洗每个答案为字符串
        answers = [str(a).strip() for a in answers if str(a).strip()]
        if not answers:
            return jsonify({"error": "answers 全部为空"}), 400

        section_map = {
            "task":  "exact_by_task",
            "qid":   "exact_by_qid",
            "title": "fuzzy_by_title",
        }
        section = section_map.get(kind)
        if not section:
            return jsonify({"error": f"kind 必须是 task/qid/title,当前: {kind!r}"}), 400

        # 读 → 改 → 写
        try:
            if MANUAL_PATH.exists():
                data = json.loads(MANUAL_PATH.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
        except Exception as e:
            return jsonify({"error": f"现有答案库 JSON 不合法: {e}"}), 400

        bucket = data.setdefault(section, {})
        if not isinstance(bucket, dict):
            bucket = {}
            data[section] = bucket
        bucket[key] = answers

        try:
            MANUAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            MANUAL_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(
                f"已添加答案: {section}.{key} ({len(answers)} 条)"
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, "section": section, "key": key, "count": len(answers)})

    @app.route("/api/manual-answers/forget-task", methods=["POST"])
    def forget_task():
        """按 task_id 同时删除 SQLite 缓存和 manual_answers.json 中的脏数据.

        body: { "task_id": "u2g68" }
        典型场景:发现某个 task 的缓存答案是错的(比如 AI 把主观题答成单词了),
        点这个按钮一次清干净,下次跑会重新询问 AI 或走人工答案。
        """
        body = request.get_json(silent=True) or {}
        task_id = (body.get("task_id") or "").strip()
        if not task_id:
            return jsonify({"error": "task_id 不能为空"}), 400

        # --- 1. 清缓存 ---
        try:
            cfg = AppConfig.load(app.config["CONFIG_PATH"])
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        cache_path = Path(cfg.cache.path)
        cache_deleted = 0
        if cache_path.exists():
            try:
                with closing(sqlite3.connect(cache_path)) as conn:
                    cur = conn.execute(
                        "DELETE FROM answer_cache WHERE task_id=?", (task_id,)
                    )
                    cache_deleted = cur.rowcount or 0
                    conn.commit()
            except Exception as e:
                return jsonify({"error": f"删缓存失败: {e}"}), 500

        # --- 2. 清 manual_answers.json 的 exact_by_task ---
        manual_deleted = False
        if MANUAL_PATH.exists():
            try:
                data = json.loads(MANUAL_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    bucket = data.get("exact_by_task")
                    if isinstance(bucket, dict) and task_id in bucket:
                        del bucket[task_id]
                        manual_deleted = True
                        MANUAL_PATH.write_text(
                            json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
            except Exception as e:
                return jsonify({"error": f"改 manual_answers.json 失败: {e}"}), 500

        logger.info(
            f"[forget-task] task={task_id} 缓存删 {cache_deleted} 行,"
            f"manual_answers.json {'已删' if manual_deleted else '未命中'}"
        )
        return jsonify({
            "ok": True,
            "task_id": task_id,
            "cache_deleted": cache_deleted,
            "manual_deleted": manual_deleted,
        })

    @app.route("/api/manual-answers/sync-from-cache", methods=["POST"])
    def sync_cache_to_manual():
        """把 SQLite 缓存里的所有题目答案同步到 manual_answers.json 的 exact_by_task.

        场景:之前 harvest 流程因 bug 没把答案落到 JSON,但 SQLite 缓存正常。
        点这个按钮一次性补回去,后续跨账号也能从 manual 库命中。
        """
        try:
            cfg = AppConfig.load(app.config["CONFIG_PATH"])
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        cache_path = Path(cfg.cache.path)
        if not cache_path.exists():
            return jsonify({"ok": True, "added": 0, "skipped": 0, "note": "缓存为空"})

        # 读现有 manual lib
        try:
            if MANUAL_PATH.exists():
                data = json.loads(MANUAL_PATH.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
        except Exception as e:
            return jsonify({"error": f"现有答案库 JSON 不合法: {e}"}), 400

        bucket = data.setdefault("exact_by_task", {})
        if not isinstance(bucket, dict):
            bucket = {}
            data["exact_by_task"] = bucket

        added = 0
        skipped = 0
        details: list[dict] = []
        try:
            with closing(sqlite3.connect(cache_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT course_instance_id, task_id, payload, source FROM answer_cache"
                ).fetchall()
        except Exception as e:
            return jsonify({"error": f"读缓存失败: {e}"}), 500

        for r in rows:
            task_id = r["task_id"]
            try:
                arr = json.loads(r["payload"])
            except Exception:
                skipped += 1
                continue
            items: list[str] = []
            for entry in arr:
                ans = entry.get("answers") if isinstance(entry, dict) else None
                if not ans:
                    continue
                first = str(ans[0]).strip() if ans else ""
                if first:
                    items.append(first)
            if not items:
                skipped += 1
                continue
            existing = bucket.get(task_id)
            # 只在缺失或为空时覆盖,避免误踩用户手动改过的精修答案
            if existing and isinstance(existing, list) and any(
                str(x).strip() for x in existing
            ):
                skipped += 1
                details.append({"task_id": task_id, "status": "skip-exists",
                                "count": len(items)})
                continue
            bucket[task_id] = items
            added += 1
            details.append({"task_id": task_id, "status": "added",
                            "source": r["source"], "count": len(items)})

        try:
            MANUAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            MANUAL_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(
                f"[sync-cache] 从 SQLite 同步 {added} 个 task 到 manual_answers.json"
                f"(跳过 {skipped} 个)"
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, "added": added, "skipped": skipped,
                        "details": details})

    @app.route("/api/manual-answers/parse", methods=["POST"])
    def parse_manual_answers_blocks():
        """把粘贴的纯文本切成"块",每块一个 key + 答案列表."""
        body = request.get_json(silent=True) or {}
        raw = body.get("raw", "") or ""
        blocks = _parse_blocks(raw)
        return jsonify({
            "blocks": [
                {"key": k, "answers": a, "count": len(a)}
                for k, a in blocks
            ]
        })

    @app.route("/api/manual-answers/import", methods=["POST"])
    def import_manual_answers_blocks():
        """把解析好的多个块一次性合并进 manual_answers.json."""
        body = request.get_json(silent=True) or {}
        blocks = body.get("blocks") or []
        kind = body.get("kind", "title")  # title / task / qid
        section_map = {
            "task": "exact_by_task",
            "qid": "exact_by_qid",
            "title": "fuzzy_by_title",
        }
        section = section_map.get(kind, "fuzzy_by_title")

        try:
            if MANUAL_PATH.exists():
                data = json.loads(MANUAL_PATH.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
        except Exception as e:
            return jsonify({"error": f"现有答案库 JSON 不合法: {e}"}), 400

        bucket = data.setdefault(section, {})
        if not isinstance(bucket, dict):
            bucket = {}
            data[section] = bucket

        n = 0
        for b in blocks:
            key = (b.get("key") or "").strip()
            ans = b.get("answers") or []
            if not key or not isinstance(ans, list):
                continue
            ans = [str(x).strip() for x in ans if str(x).strip()]
            if ans:
                bucket[key] = ans
                n += 1

        try:
            MANUAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            MANUAL_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"批量导入 {n} 个块到 {section}")
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, "added": n, "section": section})

    # --------- AI 连通测试 ---------

    @app.route("/api/ai/test", methods=["POST"])
    def test_ai():
        """发一条最小请求验证 AI 中转站能不能用."""
        try:
            cfg = AppConfig.load(app.config["CONFIG_PATH"])
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        if not cfg.ai.api_key:
            return jsonify({"ok": False, "error": "ai.api_key 为空,请先在配置区填写并保存"}), 400

        body = request.get_json(silent=True) or {}
        # 允许覆盖 model 来快速试不同模型,默认从配置读
        model = (body.get("model") or cfg.ai.model).strip()

        url = cfg.ai.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {cfg.ai.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": "Reply with only the two characters: OK"},
            ],
            "temperature": 0.0,
            "max_tokens": 20,
            "stream": False,
        }
        t0 = time.time()
        try:
            resp = _requests.post(
                url, headers=headers, data=json.dumps(payload),
                timeout=cfg.ai.timeout,
            )
        except Exception as e:
            return jsonify({
                "ok": False,
                "error": f"请求异常: {e}",
                "url": url,
                "model": model,
            }), 502

        elapsed_ms = int((time.time() - t0) * 1000)
        info: dict = {
            "url": url,
            "status": resp.status_code,
            "model": model,
            "elapsed_ms": elapsed_ms,
        }
        if resp.status_code != 200:
            info["ok"] = False
            info["error"] = f"HTTP {resp.status_code}"
            info["body"] = resp.text[:500]
            return jsonify(info), 502

        # 正常 JSON 响应
        text = resp.text
        try:
            data = json.loads(text)
        except Exception:
            # 兜底:某些中转站强制 SSE,把 chunk 拼起来
            data = _try_parse_sse_chunks(text)
            if data is None:
                info["ok"] = False
                info["error"] = "响应不是合法 JSON 也不是 SSE 流"
                info["body"] = text[:500]
                return jsonify(info), 502
            info["sse_assembled"] = True

        try:
            reply = data["choices"][0]["message"]["content"]
        except Exception:
            info["ok"] = False
            info["error"] = "响应里取不到 choices[0].message.content"
            info["raw"] = data
            return jsonify(info), 502

        info["ok"] = True
        info["reply"] = (reply or "").strip()
        if isinstance(data.get("usage"), dict):
            info["usage"] = data["usage"]
        if data.get("model"):
            info["resolved_model"] = data["model"]
        return jsonify(info)

    return app


def _try_parse_sse_chunks(text: str) -> Optional[dict]:
    """把 OpenAI SSE 流(`data: {...}\\n\\ndata: {...}`)拼成一份完整响应.

    返回 None  → 完全不是 SSE 格式
    返回 dict  → 合法 SSE。注意 message.content 可能为空(模型 0 token 输出)
    """
    if "data:" not in text:
        return None
    role = ""
    content_parts: list[str] = []
    final_id = None
    final_model = None
    finish_reason = None
    usage = None
    had_chunks = False
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
        "choices": [{
            "index": 0,
            "message": {"role": role or "assistant", "content": "".join(content_parts)},
            "finish_reason": finish_reason,
        }],
    }


# ============== 解析 OCR 粘贴文本 ==============


import re as _re


_NUMBER_PREFIX = _re.compile(
    r"^\s*[\(（]?(\d+|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|[一二三四五六七八九十])\s*[\)\).、]?\s*(.+)$"
)


def _parse_blocks(raw: str) -> list[tuple[str, list[str]]]:
    """把粘贴文本切成 [(key, [answers...]), ...].

    用空行分块。每块第一行视为 key,后续行视为答案,
    会自动剥掉 "1." / "1)" / "①" / "一、" 等常见编号前缀。
    """
    blocks: list[tuple[str, list[str]]] = []
    cur_title = ""
    cur_answers: list[str] = []

    def flush() -> None:
        nonlocal cur_title, cur_answers
        if cur_title and cur_answers:
            blocks.append((cur_title, cur_answers))
        cur_title = ""
        cur_answers = []

    for raw_line in (raw or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if not cur_title:
            cur_title = line
            continue
        m = _NUMBER_PREFIX.match(line)
        ans = m.group(2).strip() if m else line
        if ans:
            cur_answers.append(ans)
    flush()
    return blocks


def _sse(entry: dict[str, Any]) -> str:
    return f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
