"""状态卡与帮助卡共用的联系方式解析。

数据来源: status 插件配置(data/plugins_config/status.json)。
- admin_qqs: 管理员 QQ 列表; 留空时回退全局 admins
- community_group: 交流群号
- help_url: 使用须知链接(/help 与 /status 共用)
"""

from __future__ import annotations

from typing import Any, Iterable

DEFAULT_COMMUNITY_GROUP = "398870315"
DEFAULT_HELP_URL = "https://7121099.xyz/"


def resolve_contacts(
    config: dict[str, Any] | None = None,
    fallback_admins: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """合并插件配置与默认值, 返回 {admins, group, url}。

    config 缺失/字段为空时使用默认值; admins 为空时回退 fallback_admins。
    所有值都转成字符串并去掉首尾空白, 空项丢弃。
    """
    cfg = config if isinstance(config, dict) else {}

    # 同时接受插件配置键(admin_qqs/community_group/help_url)与已解析键
    # (admins/group/url), 便于渲染层直接传入 resolve_contacts 的结果。
    raw_admins = cfg.get("admin_qqs", cfg.get("admins"))
    if raw_admins in (None, "", []):
        raw_admins = list(fallback_admins or [])
    if isinstance(raw_admins, str):
        raw_admins = [part.strip() for part in raw_admins.split(",")]
    admins = [str(a).strip() for a in (raw_admins or []) if str(a).strip()]

    group = str(
        cfg.get("community_group") or cfg.get("group") or DEFAULT_COMMUNITY_GROUP
    ).strip()
    url = str(cfg.get("help_url") or cfg.get("url") or DEFAULT_HELP_URL).strip()
    return {"admins": admins, "group": group, "url": url}
