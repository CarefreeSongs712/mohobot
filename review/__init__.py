"""Mohobot 聊天记录审核面板 — 半独立 WebUI。

- 独立进程/入口(review/main.py), 额外端口(默认 9091), mohobot 停止不影响它。
- 对 mohobot 的 data/ 目录严格只读(contexts/history/图片缓存),
  审核状态存自己的 SQLite(review/data/review.db)。
- 消息身份: 指纹 = sha256(session_key|role|timestamp|content) (方案 B,
  不修改 mohobot); 用户消息的 message_id 通过 timestamp+user_id 与
  history JSONL join 得出。
"""
