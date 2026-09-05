"""审核面板 FastAPI 应用 — 登录鉴权 + 审核/异常/导出/统计 API。

鉴权: config.yaml 手工维护用户(PBKDF2 哈希), 登录发内存 token
(Authorization: Bearer), 与主面板同构; 所有用户权限相同。
"""

from __future__ import annotations

import csv
import io
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from loguru import logger

from review import loader as _loader
from review.config import ReviewConfig, verify_password
from review.loader import format_ts, parse_session_key
from review.store import ReviewStore

TAG_OPTIONS = ["色情", "政治", "辱骂", "其他"]


def create_app(cfg: ReviewConfig, data: _loader.MohobotData, store: ReviewStore) -> FastAPI:
    app = FastAPI(title="Mohobot Review Panel", docs_url=None, redoc_url=None)
    static_dir = Path(__file__).resolve().parent / "static"

    # 内存 token 表: {token: {user, expiry}}
    tokens: dict[str, dict[str, Any]] = {}

    def _auth(request: Request) -> str:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="未登录")
        token = header[7:].strip()
        info = tokens.get(token)
        if not info or info["expiry"] < time.time():
            tokens.pop(token, None)
            raise HTTPException(status_code=401, detail="登录已过期")
        info["expiry"] = time.time() + cfg.token_expiry  # 活跃续期
        return info["user"]

    def _cleanup_tokens() -> None:
        now = time.time()
        for k in [k for k, v in tokens.items() if v["expiry"] < now]:
            tokens.pop(k, None)

    # ── 会话计数辅助 ─────────────────────────────────────────

    def _session_counts() -> list[dict[str, Any]]:
        """把 loader 会话概要与审核状态合并 → 带 unreviewed/normal/abnormal 计数。

        force=True 重扫目录(文件级 mtime 缓存使其开销仅为 stat 调用),
        保证 mohobot 侧新写入的消息立即可见。
        """
        all_statuses = store.statuses_by_session()
        items = []
        for s in data.list_sessions(force=True):
            sk = s["session_key"]
            sts = all_statuses.get(sk, {})
            normal = sum(1 for v in sts.values() if v["status"] == "normal")
            abnormal = sum(1 for v in sts.values() if v["status"] == "abnormal")
            unreviewed = max(0, s["total"] - normal - abnormal)
            items.append({**s, "normal": normal, "abnormal": abnormal, "unreviewed": unreviewed})
        return items

    def _sort_sessions(items: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
        if sort == "recent":
            return sorted(items, key=lambda x: x["mtime"], reverse=True)
        # oldest(默认): 未审核在前(按最旧优先), 已审完的排后
        pending = sorted((x for x in items if x["unreviewed"] > 0), key=lambda x: x["mtime"])
        done = sorted((x for x in items if x["unreviewed"] == 0), key=lambda x: x["mtime"])
        return pending + done

    def _unreviewed_fps(sk: str, entries: list[dict[str, Any]]) -> list[str]:
        return [e["fingerprint"] for e in entries if e["status"] == "unreviewed"]

    def _remaining_unreviewed(sk: str) -> int:
        """判定后重算剩余待审(重新取最新审核状态, 不用判定前的快照)。"""
        entries = data.enrich_entries(sk, store.statuses_by_session().get(sk, {}),
                                      store.abnormal_by_fingerprint())
        return len(_unreviewed_fps(sk, entries or []))

    # ── Auth ─────────────────────────────────────────────────

    @app.post("/api/login")
    async def login(request: Request):
        body = await request.json()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        user = next((u for u in cfg.users if u.username == username), None)
        if user is None or not verify_password(password, user.password_hash):
            logger.warning(f"审核面板登录失败: {username}")
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        _cleanup_tokens()
        token = secrets.token_hex(32)
        tokens[token] = {"user": username, "expiry": time.time() + cfg.token_expiry}
        logger.info(f"审核面板登录成功: {username}")
        return {"token": token, "username": username}

    @app.post("/api/logout")
    async def logout(request: Request):
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            tokens.pop(header[7:].strip(), None)
        return {"ok": True}

    @app.get("/api/me")
    async def me(request: Request):
        return {"user": _auth(request)}

    # ── 会话列表 / 明细 ──────────────────────────────────────

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request):
        _auth(request)
        nicknames = data.bot_nicknames()
        bots = [
            {"bot_id": b, "nickname": nicknames.get(b, b)}
            for b in sorted({s["bot_id"] for s in data.list_sessions()})
        ]
        return {"bots": bots, "tags": TAG_OPTIONS}

    @app.get("/api/sessions")
    async def sessions(request: Request, bot: str = "", chat_type: str = "",
                       status: str = "all", sort: str = "oldest"):
        _auth(request)
        items = _session_counts()
        if bot:
            items = [x for x in items if x["bot_id"] == bot]
        if chat_type in ("private", "group"):
            items = [x for x in items if x["chat_type"] == chat_type]
        if status == "unreviewed":
            items = [x for x in items if x["unreviewed"] > 0]
        elif status == "abnormal":
            items = [x for x in items if x["abnormal"] > 0]
        items = _sort_sessions(items, sort)
        return {"sessions": items}

    @app.get("/api/session/{bot_id}/{chat_type}/{chat_id}/{session_id}")
    async def session_detail(request: Request, bot_id: str, chat_type: str,
                             chat_id: str, session_id: str):
        _auth(request)
        sk = _loader.session_key(bot_id, chat_type, chat_id, session_id)
        entries = data.enrich_entries(sk, store.statuses_by_session().get(sk, {}),
                                      store.abnormal_by_fingerprint())
        if not entries:
            raise HTTPException(status_code=404, detail="会话不存在或为空")
        info = next(
            (s for s in data.list_sessions() if s["session_key"] == sk), None
        )
        nicknames = data.bot_nicknames()
        return {
            "session_key": sk,
            "bot_id": bot_id,
            "bot_nickname": nicknames.get(bot_id, bot_id),
            "chat_type": chat_type,
            "chat_id": chat_id,
            "session_id": session_id,
            "display_name": info["display_name"] if info else chat_id,
            "mtime": info["mtime"] if info else 0,
            "unreviewed": sum(1 for e in entries if e["status"] == "unreviewed"),
            "total": sum(1 for e in entries if e["kind"] != "summary"),
            "entries": entries,
        }

    # ── 审核操作 ─────────────────────────────────────────────

    @app.post("/api/review")
    async def review(request: Request):
        user = _auth(request)
        body = await request.json()
        sk = str(body.get("session_key", ""))
        action = str(body.get("action", ""))
        fps = [str(f) for f in (body.get("fingerprints") or [])]
        try:
            parse_session_key(sk)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad session_key")

        entries = data.enrich_entries(sk, store.statuses_by_session().get(sk, {}),
                                      store.abnormal_by_fingerprint())
        if entries is None or not entries:
            raise HTTPException(status_code=404, detail="会话不存在或为空")

        if action == "skip":
            store.skip(sk, user, detail=str(body.get("detail", "")))
            return {"ok": True, "action": "skip",
                    "remaining_unreviewed": _remaining_unreviewed(sk)}

        # 未指定指纹 → 默认当前全部待审
        if not fps:
            fps = _unreviewed_fps(sk, entries)
        if not fps:
            return {"ok": True, "action": action, "remaining_unreviewed": 0}

        by_fp = {e["fingerprint"]: e for e in entries}

        if action == "normal":
            changed = store.judge(sk, fps, "normal", user)
            logger.info(f"[review] {user}: {sk} 正常 {changed} 条")
            return {"ok": True, "action": "normal", "changed": changed,
                    "remaining_unreviewed": _remaining_unreviewed(sk)}

        if action == "abnormal":
            tags = [str(t) for t in (body.get("tags") or []) if t in TAG_OPTIONS]
            note = str(body.get("note", "")).strip()
            for fp in fps:
                e = by_fp.get(fp)
                if e is None:
                    continue
                store.judge(sk, [fp], "abnormal", user)
                store.add_abnormal(
                    sk, fp, e["role"], e["speaker"] or e["role"],
                    e["content"], e["message_id"], tags, note, user,
                )
            logger.info(f"[review] {user}: {sk} 异常 {len(fps)} 条 (tags={tags})")
            return {"ok": True, "action": "abnormal", "changed": len(fps),
                    "remaining_unreviewed": _remaining_unreviewed(sk)}

        raise HTTPException(status_code=400, detail=f"未知操作: {action}")

    # ── 异常记录 ─────────────────────────────────────────────

    @app.get("/api/abnormal")
    async def abnormal_list(request: Request, bot: str = "", tag: str = ""):
        _auth(request)
        records = store.list_abnormal(bot=bot, tag=tag)
        nicknames = data.bot_nicknames()
        # 附会话显示名
        name_map = {s["session_key"]: s["display_name"] for s in data.list_sessions()}
        for r in records:
            r["bot_id"] = r["session_key"].split("/", 1)[0]
            r["bot_nickname"] = nicknames.get(r["bot_id"], r["bot_id"])
            r["display_name"] = name_map.get(r["session_key"], "")
            r["time_str"] = format_ts(r.get("created_at"))
        return {"records": records, "tags": TAG_OPTIONS}

    @app.put("/api/abnormal/{record_id}")
    async def abnormal_update(request: Request, record_id: int):
        user = _auth(request)
        body = await request.json()
        tags = [str(t) for t in (body.get("tags") or []) if t in TAG_OPTIONS]
        note = str(body.get("note", "")).strip()
        ok = store.update_abnormal(record_id, tags, note, user)
        if not ok:
            raise HTTPException(status_code=404, detail="记录不存在")
        return {"ok": True}

    @app.get("/api/export")
    async def export(request: Request, bot: str = "", tag: str = ""):
        _auth(request)
        records = store.list_abnormal(bot=bot, tag=tag)
        nicknames = data.bot_nicknames()
        name_map = {s["session_key"]: s["display_name"] for s in data.list_sessions()}

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["记录ID", "审核时间", "Bot", "会话", "发言人", "消息类型",
                         "message_id", "内容", "标签", "备注", "审核人"])
        for r in records:
            bot_id = r["session_key"].split("/", 1)[0]
            writer.writerow([
                r["id"],
                format_ts(r.get("created_at")),
                f"{bot_id}({nicknames.get(bot_id, bot_id)})",
                name_map.get(r["session_key"], ""),
                r.get("speaker", ""),
                r.get("role", ""),
                r.get("message_id", ""),
                r.get("content", ""),
                ",".join(r.get("tags", [])),
                r.get("note", ""),
                r.get("reviewer", ""),
            ])
        content = buf.getvalue().encode("utf-8-sig")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="abnormal_{stamp}.csv"'},
        )

    # ── 统计 ─────────────────────────────────────────────────

    @app.get("/api/stats")
    async def stats(request: Request):
        _auth(request)
        counts = _session_counts()
        per_bot: dict[str, dict[str, int]] = {}
        overall = {"total": 0, "normal": 0, "abnormal": 0, "unreviewed": 0}
        for c in counts:
            b = per_bot.setdefault(c["bot_id"], {"total": 0, "normal": 0, "abnormal": 0, "unreviewed": 0})
            for k in overall:
                b[k] += c[k]
                overall[k] += c[k]
        nicknames = data.bot_nicknames()
        return {
            "overall": overall,
            "per_bot": [
                {"bot_id": b, "nickname": nicknames.get(b, b), **v}
                for b, v in sorted(per_bot.items())
            ],
            "per_reviewer": store.reviewer_stats(),
            "recent_log": store.recent_log(50),
            "log_count": store.log_count(),
            "session_count": len(counts),
        }

    # ── 前端 ─────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static_dir / "index.html")

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        return FileResponse(static_dir / "index.html")

    return app
