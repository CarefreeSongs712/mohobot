"""跨账号稳定的消息身份, 用于 history 群合并归档去重。

部分 OneBot 实现(NapCat 实测)的 ``message_id`` 是**按账号分配**的: 同一条群
消息经不同 bot 连接推送时 id 各不相同。合并归档若只按 ``message_id`` 去重,
多 bot 群里每条消息会被写入 N 次。

这里提供跨账号一致的指纹: ``(发送者, 消息段类型, 文本内容)``。同一条消息在
每个账号上的 ``message`` 段结构一致, 因此指纹一致; 发送方自己的
``message_sent`` 也在发送前用同一套指纹登记, 其他 bot 随后收到的副本据此
跳过, 避免 bot 发言被存成两行。

登记在发送前完成(而不是等 echo 后归档时), 这样无论"sent 先写"还是"inbound
先写"都能正确去重。发送失败/被拦截时释放登记, 不吞掉真实的入站记录。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


def message_signature(message: Any, *, strict: bool = False) -> tuple[Any, ...]:
    """消息段列表(或纯文本)的跨账号稳定指纹。

    - ``strict=False``(发送侧匹配用): 文本段合并成一段文本, 非文本段只记
      类型。发送侧内容(如 ``base64://…``)与入站副本(文件名/URL)必然不同,
      只能按"类型 + 文本"配对。
    - ``strict=True``(入站副本之间判重用): 非文本段额外带上 ``file``/
      ``url``/``id``, 避免同一秒内两张不同图片被误判为同一条消息。

    文本段个数不参与比较(不同账号可能拆段方式不同); ``reply`` 段只影响
    引用展示且各账号 id 不同, 一律不参与。
    """
    if isinstance(message, str):
        return ((), message.strip())
    types: list[str] = []
    texts: list[str] = []
    for seg in message or []:
        if not isinstance(seg, dict):
            continue
        stype = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if stype == "text":
            texts.append(str(data.get("text") or ""))
        elif stype == "reply":
            continue
        elif strict:
            # 只取 file/url: 部分实现(如合并转发的 id)按账号分配, 各账号不同,
            # 用它会把同一条消息判成两条。
            ident = str(data.get("file") or data.get("url") or "")
            types.append((stype, ident))
        else:
            types.append(stype)
    return (tuple(types), "".join(texts).strip())


def forward_signature() -> tuple[Any, ...]:
    """合并转发发送的指纹(入站副本只有一个 forward 段)。"""
    return (("forward",), "")


def signature_for_send(action: str, params: dict[str, Any]) -> tuple[Any, ...]:
    """发送侧消息指纹(宽松档): 合并转发按 forward 段登记, 其余按消息内容。"""
    if action == "send_group_forward_msg":
        return forward_signature()
    return message_signature(params.get("message"))


class SentMessageClaims:
    """发送侧的消息指纹登记(带 TTL), 供入站归档判定"这是我方发言的副本"。

    - ``claim`` 在消息真正派发前调用; 同一条消息只登记一次。
    - ``release`` 在确认未发出时调用。
    - ``matches`` 供 ``MessageHandler`` 判断入站副本是否已有 ``message_sent``。

    TTL 只需覆盖 echo 往返与归档任务延迟; 过期后同一内容再次发送不会互相
    干扰。窗口按群隔离并限制容量, 避免长期运行无限增长。
    """

    def __init__(self, ttl: float = 30.0, max_entries: int = 4096) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        # {group_id: OrderedDict[(sender_id, signature), timestamp]}
        self._claims: dict[str, OrderedDict[tuple, float]] = {}

    @staticmethod
    def _key(sender_id: Any, signature: tuple[Any, ...]) -> tuple:
        return (str(sender_id), signature)

    def _bucket(self, group_id: Any) -> OrderedDict:
        return self._claims.setdefault(str(group_id), OrderedDict())

    def _prune(self, bucket: OrderedDict, now: float) -> None:
        while bucket:
            _, ts = next(iter(bucket.items()))
            if now - ts < self._ttl and len(bucket) <= self._max_entries:
                break
            bucket.popitem(last=False)

    def claim(self, group_id: Any, sender_id: Any, signature: tuple[Any, ...]) -> None:
        import time

        now = time.monotonic()
        bucket = self._bucket(group_id)
        bucket[self._key(sender_id, signature)] = now
        bucket.move_to_end(self._key(sender_id, signature))
        self._prune(bucket, now)

    def release(self, group_id: Any, sender_id: Any, signature: tuple[Any, ...]) -> None:
        bucket = self._claims.get(str(group_id))
        if bucket is not None:
            bucket.pop(self._key(sender_id, signature), None)

    def matches(self, group_id: Any, sender_id: Any, signature: tuple[Any, ...]) -> bool:
        import time

        bucket = self._claims.get(str(group_id))
        if not bucket:
            return False
        key = self._key(sender_id, signature)
        ts = bucket.get(key)
        if ts is None:
            return False
        if time.monotonic() - ts >= self._ttl:
            bucket.pop(key, None)
            return False
        return True
