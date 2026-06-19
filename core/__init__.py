"""AutoUnipus 核心模块.

子包:
    crypto    AES-128-ECB / HS256 JWT 等加解密原语
    unipus    U校园相关:登录、答案接口、页面信息提取
    sources   答案数据源:API 直取(主) / AI 兜底(预留)
    cache     SQLite 答案缓存
    handlers  题型适配器
"""

__version__ = "2.0.0"
