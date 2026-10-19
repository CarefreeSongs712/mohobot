"""新闻/动态知识库(REQ-6): 采集入库 → 管理员审核 → 检索与提示词注入。

默认关闭; 开启后只有**已审核通过**的条目会进入 LLM 上下文或工具结果。
"""

from mohobot.news.service import NewsService
from mohobot.news.store import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    NewsItem,
    NewsStore,
)

__all__ = [
    "NewsService", "NewsStore", "NewsItem",
    "STATUS_PENDING", "STATUS_APPROVED", "STATUS_REJECTED",
]
