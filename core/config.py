"""配置加载.

向后兼容旧 account.json,缺失新字段时给默认值。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class AIConfig:
    enabled: bool = False
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    # 主模型空响应/失败时,按顺序自动尝试下面这些备用模型,直到拿到答案。
    # 留空 = 不做模型 fallback,只对主模型做"题面砍半"重试。
    fallback_models: List[str] = field(default_factory=list)
    timeout: int = 30


@dataclass
class CacheConfig:
    enabled: bool = True
    path: str = "data/answers.db"


# 所有可用的 handler 名(与 core.handlers.* 中的 .name 对齐)
ALL_HANDLERS = [
    "single-choice",
    "multi-choice",
    "word-blank",
    "blank",
    "translation",
]


@dataclass
class AppConfig:
    username: str
    password: str
    automode: bool = True
    driver: str = "Edge"
    class_urls: List[str] = field(default_factory=list)
    ai: AIConfig = field(default_factory=AIConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    # 启用的 handler 列表,空 = 全部启用(向后兼容)
    handlers: List[str] = field(default_factory=lambda: list(ALL_HANDLERS))
    # 试运行:填答案但不点 提交/下一页,方便先肉眼校对
    dry_run: bool = False
    # 第一遍跑题时,即便 resolver 拿不到答案也强行进入提交流程,
    # 触发 U校园 完成弹窗 → 自动点"查看答案" → 扒正确答案进缓存。
    # 第一遍正确率会差,但第二遍跑同一批题时缓存命中 = 100%。
    harvest_first_pass: bool = False
    # 阅读型(纯文本/无任何作答 input)入口的停留秒数。U校园 会按访问时间记录学习进度,
    # 太短可能不计入完成度;太长又拖慢整体节奏。默认 8 秒。
    reading_duration_sec: int = 8
    debug: bool = False

    @classmethod
    def load(cls, path: str | Path = "account.json") -> "AppConfig":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"配置文件不存在: {p}. 请参考 README.md 创建 account.json"
            )
        with p.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        # class_url 可能是 list 或 str,做兼容
        urls = raw.get("class_url", [])
        if isinstance(urls, str):
            urls = [urls]
        urls = [u.strip() for u in urls if u and u.strip()]

        ai_raw = raw.get("ai", {}) or {}
        cache_raw = raw.get("cache", {}) or {}

        # 启用的 handler:支持缺省(老配置)→ 全开
        handlers_raw = raw.get("handlers")
        if handlers_raw is None:
            handlers = list(ALL_HANDLERS)
        else:
            handlers = [str(h).strip() for h in handlers_raw if str(h).strip()]

        return cls(
            username=str(raw.get("username", "")).strip(),
            password=str(raw.get("password", "")).strip(),
            automode=bool(raw.get("Automode", True)),
            driver=str(raw.get("Driver", "Edge")).strip() or "Edge",
            class_urls=urls,
            ai=AIConfig(
                enabled=bool(ai_raw.get("enabled", False)),
                base_url=str(ai_raw.get("base_url", "https://api.deepseek.com/v1")).strip(),
                api_key=str(ai_raw.get("api_key", "")).strip(),
                model=str(ai_raw.get("model", "deepseek-chat")).strip(),
                fallback_models=[
                    str(m).strip() for m in (ai_raw.get("fallback_models") or [])
                    if str(m).strip()
                ],
                timeout=int(ai_raw.get("timeout", 30)),
            ),
            cache=CacheConfig(
                enabled=bool(cache_raw.get("enabled", True)),
                path=str(cache_raw.get("path", "data/answers.db")).strip(),
            ),
            handlers=handlers,
            dry_run=bool(raw.get("dry_run", False)),
            harvest_first_pass=bool(raw.get("harvest_first_pass", False)),
            reading_duration_sec=int(raw.get("reading_duration_sec", 8)),
            debug=bool(raw.get("debug", False)),
        )

    def to_dict(self) -> dict:
        """序列化回 account.json 兼容格式."""
        return {
            "username": self.username,
            "password": self.password,
            "Automode": self.automode,
            "Driver": self.driver,
            "class_url": list(self.class_urls),
            "ai": {
                "enabled": self.ai.enabled,
                "base_url": self.ai.base_url,
                "api_key": self.ai.api_key,
                "model": self.ai.model,
                "fallback_models": list(self.ai.fallback_models),
                "timeout": self.ai.timeout,
            },
            "cache": {
                "enabled": self.cache.enabled,
                "path": self.cache.path,
            },
            "handlers": list(self.handlers),
            "dry_run": self.dry_run,
            "harvest_first_pass": self.harvest_first_pass,
            "reading_duration_sec": self.reading_duration_sec,
            "debug": self.debug,
        }

    def save(self, path: str | Path = "account.json") -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def validate(self) -> Optional[str]:
        """返回错误描述字符串;None 表示通过。"""
        if not self.username:
            return "username 不能为空"
        if not self.password:
            return "password 不能为空"
        if self.driver not in {"Edge", "Chrome"}:
            return f"Driver 必须是 Edge 或 Chrome,当前: {self.driver!r}"
        if self.automode and not self.class_urls:
            return "自动模式必须填写至少一个 class_url"
        if self.ai.enabled and not self.ai.api_key:
            return "ai.enabled=true 但 ai.api_key 为空"
        return None
