"""审核面板 FastAPI 应用 — 登录鉴权 + 审核/异常/导出/统计 API。

鉴权: config.yaml 手工维护用户(PBKDF2 哈希), 登录发内存 token
(Authorization: Bearer), 与主面板同构; 所有用户权限相同。
登录防爆破: 处理全局串行化 + 每次尝试固定 0.5s 硬延迟
(登录耗时恒定防计时侧信道, 串行使并发爆破失效)。
数据源: mohobot data/history 消息事件流(唯一来源, 身份=message_id)。
"""

from __future__ import annotations

import asyncio
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
from review.hash_password import hash_password
from review.loader import MERGED_BOT_ID, format_ts, parse_session_key
from review.store import ReviewStore

TAG_OPTIONS = ["色情", "政治", "辱骂", "其他"]


def create_app(cfg: ReviewConfig, data: _loader.MohobotData, store: ReviewStore,
               config_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Mohobot Review Panel", docs_url=None, redoc_url=None)
    static_dir = Path(__file__).resolve().parent / "static"

    # 内存 token 表: {token: {user, expiry}}
    tokens: dict[str, dict[str, Any]] = {}
    # 登录串行锁(防爆破: 同一时刻只处理一个登录请求)
    login_lock: asyncio.Lock = asyncio.Lock()

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

    async def _session_counts() -> list[dict[str, Any]]:
        """把 loader 会话概要与审核状态合并 → 带 unreviewed/normal/abnormal 计数。

        force=True 重扫目录(文件级 mtime 缓存使其开销仅为 stat 调用),
        保证 mohobot 侧新写入的消息立即可见。
        计数来自 store 的进程内缓存(与 statuses_by_session 同一次构建),
        loader/DB 的重调用都走线程, 不冻结事件循环。
        """
        counts = await asyncio.to_thread(store.counts_by_session)
        items = []
        for s in await asyncio.to_thread(data.list_sessions, True):
            normal, abnormal = counts.get(s["session_key"], (0, 0))
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

    def _remaining_after(entries: list[dict[str, Any]], judged: list[str]) -> int:
        """在已取到的 entries 上算剩余待审(判定后调用, 免去重新加载+重新 enrich)。"""
        done = set(judged)
        return sum(1 for e in entries
                   if e["status"] == "unreviewed" and e["fingerprint"] not in done)

    def _enrich_sync(sk: str) -> list[dict[str, Any]]:
        """会话明细组装(含状态/异常读取) — 整体在线程里跑, 不阻塞事件循环。"""
        return data.enrich_entries(
            sk,
            store.statuses_by_session().get(sk, {}),
            store.abnormal_by_fingerprint(),
        )

    # ── Auth ─────────────────────────────────────────────────

    @app.post("/api/login")
    async def login(request: Request):
        # 串行化 + 无条件 0.5s 硬延迟: 防并发爆破, 且无论成败登录耗时恒定
        # (不给攻击者"密码对不对"的计时侧信道)
        async with login_lock:
            await asyncio.sleep(0.5)
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

    # ── 修改密码(仅自己) ─────────────────────────────────────

    def _write_user_password(username: str, new_hash: str) -> bool:
        """把新哈希写回 config.yaml(优先按行替换, 保留注释与格式)。

        返回 False 表示无配置路径/写入失败(调用方拒绝修改)。
        """
        import yaml as _yaml

        if config_path is None:
            return False
        p = Path(config_path)
        if not p.exists():
            return False
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        in_target = False
        replaced = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("- username:"):
                in_target = stripped.split(":", 1)[1].strip().strip("\"'") == username
            elif in_target and stripped.startswith("password_hash:"):
                indent = line[:len(line) - len(line.lstrip())]
                lines[i] = f'{indent}password_hash: "{new_hash}"\n'
                replaced = True
                in_target = False
        if replaced:
            p.write_text("".join(lines), encoding="utf-8")
            return True
        # 兜底: 结构与预期不符 → 整体重写(注释会丢失)
        try:
            raw = _yaml.safe_load(text) or {}
            for item in raw.get("users") or []:
                if isinstance(item, dict) and item.get("username") == username:
                    item["password_hash"] = new_hash
            p.write_text(_yaml.dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
            return True
        except Exception as e:
            logger.error(f"审核面板密码写入失败({username}): {e}")
            return False

    @app.post("/api/password")
    async def change_password(request: Request):
        user = _auth(request)
        body = await request.json()
        old_pw = str(body.get("old_password", ""))
        new_pw = str(body.get("new_password", ""))
        account = next((x for x in cfg.users if x.username == user), None)
        if account is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if not verify_password(old_pw, account.password_hash):
            raise HTTPException(status_code=400, detail="旧密码错误")
        if len(new_pw) < 6:
            raise HTTPException(status_code=400, detail="新密码至少 6 位")
        if new_pw == old_pw:
            raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")
        new_hash = hash_password(new_pw)
        if not _write_user_password(user, new_hash):
            raise HTTPException(status_code=500, detail="配置写入失败, 密码未修改")
        account.password_hash = new_hash  # 内存热生效
        # 吊销该用户其它会话(保留当前)
        current_token = request.headers.get("authorization", "")[7:].strip()
        for tk in [tk for tk, v in tokens.items() if v["user"] == user and tk != current_token]:
            tokens.pop(tk, None)
        logger.info(f"审核面板密码已修改: {user}")
        return {"ok": True}

    # ── 会话列表 / 明细 ──────────────────────────────────────

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request):
        _auth(request)
        sessions = await asyncio.to_thread(data.list_sessions)
        nicknames = await asyncio.to_thread(data.bot_nicknames)
        bots = [
            {"bot_id": b, "nickname": nicknames.get(b, b)}
            for b in sorted({s["bot_id"] for s in sessions
                             if s["chat_type"] == "private"})
        ]
        return {"bots": bots, "tags": TAG_OPTIONS}

    @app.get("/api/sessions")
    async def sessions(request: Request, bot: str = "", chat_type: str = "",
                       status: str = "all", sort: str = "oldest"):
        _auth(request)
        items = await _session_counts()
        if bot:
            # 群聊合并会话: bot 过滤按成员 bot 判断(该 bot 在此群的归档存在)
            items = [x for x in items
                     if x["bot_id"] == bot or bot in (x.get("bots") or [])]
        if chat_type in ("private", "group"):
            items = [x for x in items if x["chat_type"] == chat_type]
        if status == "unreviewed":
            items = [x for x in items if x["unreviewed"] > 0]
        elif status == "abnormal":
            items = [x for x in items if x["abnormal"] > 0]
        items = _sort_sessions(items, sort)
        return {"sessions": items}

    @app.get("/api/session/{bot_id}/{chat_type}/{chat_id}")
    async def session_detail(request: Request, bot_id: str, chat_type: str,
                             chat_id: str, page: int = 0, page_size: int = 100):
        """会话明细(分页)。

        page<=0 时自动锚定第一条待审消息所在页(无待审则最后一页),
        前端判定完当前页后重新打开会话即自动跳到下一批待审。
        """
        _auth(request)
        sk = _loader.session_key(bot_id, chat_type, chat_id)
        entries = await asyncio.to_thread(_enrich_sync, sk)
        if not entries:
            raise HTTPException(status_code=404, detail="会话不存在或为空")
        sessions = await asyncio.to_thread(data.list_sessions)
        info = next(
            (s for s in sessions if s["session_key"] == sk), None
        )
        nicknames = await asyncio.to_thread(data.bot_nicknames)
        total = len(entries)
        unreviewed = sum(1 for e in entries if e["status"] == "unreviewed")
        page_size = max(1, min(int(page_size or 100), 500))
        pages = max(1, (total + page_size - 1) // page_size)
        if page <= 0:
            first_pend = next(
                (i for i, e in enumerate(entries) if e["status"] == "unreviewed"),
                None,
            )
            page = (first_pend // page_size + 1) if first_pend is not None else pages
        page = min(max(page, 1), pages)
        window = entries[(page - 1) * page_size: page * page_size]
        # 异常条目所在页(前端可一键跳转, 避免异常条目落在别的页看不到)
        abnormal_pages = sorted({
            i // page_size + 1
            for i, e in enumerate(entries) if e["status"] == "abnormal"
        })
        return {
            "session_key": sk,
            "bot_id": bot_id,
            "bot_nickname": (nicknames.get(bot_id, bot_id)
                             if bot_id != MERGED_BOT_ID else ""),
            "chat_type": chat_type,
            "chat_id": chat_id,
            "display_name": info["display_name"] if info else chat_id,
            "mtime": info["mtime"] if info else 0,
            "unreviewed": unreviewed,
            "abnormal_count": sum(1 for e in entries if e["status"] == "abnormal"),
            "abnormal_pages": abnormal_pages,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "entries": window,
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

        entries = await asyncio.to_thread(_enrich_sync, sk)
        if entries is None or not entries:
            raise HTTPException(status_code=404, detail="会话不存在或为空")

        if action == "skip":
            store.skip(sk, user, detail=str(body.get("detail", "")))
            return {"ok": True, "action": "skip",
                    "remaining_unreviewed": _remaining_after(entries, [])}

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
                    "remaining_unreviewed": _remaining_after(entries, fps)}

        if action == "abnormal":
            tags = [str(t) for t in (body.get("tags") or []) if t in TAG_OPTIONS]
            note = str(body.get("note", "")).strip()
            # 判定批量一次写入(避免逐条 judge 各自扫一遍本会话结论)
            marked = [fp for fp in fps if fp in by_fp]
            store.judge(sk, marked, "abnormal", user)
            for fp in marked:
                e = by_fp[fp]
                store.add_abnormal(
                    sk, fp, e["role"], e["speaker"] or e["role"],
                    e["content"], e["message_id"], tags, note, user,
                )
            logger.info(f"[review] {user}: {sk} 异常 {len(marked)} 条 (tags={tags})")
            return {"ok": True, "action": "abnormal", "changed": len(marked),
                    "remaining_unreviewed": _remaining_after(entries, marked)}

        raise HTTPException(status_code=400, detail=f"未知操作: {action}")

    # ── 异常记录 ─────────────────────────────────────────────

    @app.get("/api/abnormal")
    async def abnormal_list(request: Request, bot: str = "", tag: str = ""):
        _auth(request)
        records = store.list_abnormal(bot=bot, tag=tag)
        nicknames = await asyncio.to_thread(data.bot_nicknames)
        # 附会话显示名
        name_map = {s["session_key"]: s["display_name"] for s in await asyncio.to_thread(data.list_sessions)}
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

    @app.delete("/api/abnormal/{record_id}")
    async def abnormal_delete(request: Request, record_id: int):
        """删除异常记录 —— 该消息的审核结论同时撤销(回到未审核)。"""
        user = _auth(request)
        rec = await asyncio.to_thread(store.delete_abnormal, record_id, user)
        if rec is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        logger.info(
            f"[review] {user}: 删除异常记录 #{record_id} "
            f"({rec['session_key']}) → 该消息回到未审核"
        )
        return {"ok": True, "id": record_id}

    @app.delete("/api/abnormal")
    async def abnormal_delete_all(request: Request, bot: str = "", tag: str = ""):
        """批量删除异常记录(可按 bot 前缀/标签过滤)。

        对应消息当前结论为「异常」的一并撤销(回到未审核);
        已是 normal 的陈旧记录只删记录、不动结论。
        """
        user = _auth(request)
        deleted = await asyncio.to_thread(
            store.delete_all_abnormal, bot, tag, user,
        )
        logger.info(
            f"[review] {user}: 批量删除异常记录 {deleted} 条 "
            f"(bot={bot or '全部'}, tag={tag or '全部'})"
        )
        return {"ok": True, "deleted": deleted}

    @app.get("/api/export")
    async def export(request: Request, bot: str = "", tag: str = ""):
        _auth(request)
        records = store.list_abnormal(bot=bot, tag=tag)
        nicknames = await asyncio.to_thread(data.bot_nicknames)
        name_map = {s["session_key"]: s["display_name"] for s in await asyncio.to_thread(data.list_sessions)}

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
        counts = await _session_counts()
        per_bot: dict[str, dict[str, int]] = {}
        overall = {"total": 0, "normal": 0, "abnormal": 0, "unreviewed": 0}
        for c in counts:
            b = per_bot.setdefault(c["bot_id"], {"total": 0, "normal": 0, "abnormal": 0, "unreviewed": 0})
            for k in overall:
                b[k] += c[k]
                overall[k] += c[k]
        nicknames = await asyncio.to_thread(data.bot_nicknames)
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
