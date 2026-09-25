"""Mohobot 聊天记录审核面板 — 半独立 WebUI。

- 独立进程/入口(review/main.py), 额外端口(默认 9091), mohobot 停止不影响它。
- 对 mohobot 的 data/ 目录严格只读; 审核状态存自己的 SQLite(review/data/review.db)。
- 数据源: data/history 消息事件流(唯一来源)。
  群聊合并归档 history/group/{群号}.jsonl(跨 bot 去重, 行内 bot_id 标注归属),
  私聊 history/{bot_id}/private/{chat_id}.jsonl 只增不删, 含收到的消息
  (post_type="message")与 bot 自己发送的消息(post_type="message_sent",
  由 WSServer 出站层归档)。
- 群聊审核范围(面板侧过滤): bot 的发言 + 用户 @ 某只 bot 或引用某只 bot
  发言的消息; 私聊全部审。
- 消息身份: message_id("mid:<id>", 只增不删 → 审核结论永不失联);
  无 message_id 时退回内容指纹。
- 登录防爆破: 处理全局串行化 + 每次尝试固定 0.5s 硬延迟。
"""
