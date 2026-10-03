"""表情包小偷：偷群聊表情包，视觉模型审核标注入库，聊天时由会话模型按语义选图发送。"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# 热重载时 mohobot 会重新执行本文件，但子模块会命中 sys.modules 缓存；
# 由 mohobot 加载时先清掉旧模块，改动 meme_stealer_core 后在面板点「重载」即可生效。
if __name__.startswith("mohobot_plugin_"):
    for _name in [n for n in sys.modules if n == "meme_stealer_core" or n.startswith("meme_stealer_core.")]:
        del sys.modules[_name]

from meme_stealer_core.app import StealerApp  # noqa: E402
from meme_stealer_core.host import ConfigStore, StealerEvent, logger  # noqa: E402
from meme_stealer_core.web.server import WebUIServer  # noqa: E402

PLUGIN_NAME = "meme_stealer"
COMMAND = "/meme"

# 当前消息的事件视图。mohobot 每条消息在独立 task 里走完
# 感知 → 观察 → 拦截 → LLM（含工具调用），同一 task 内共享这个上下文。
_CURRENT_EVENT: contextvars.ContextVar[StealerEvent | None] = contextvars.ContextVar(
    "meme_stealer_event", default=None
)

_SEEN_MESSAGE_LIMIT = 4096

TOOL_SPECS: dict[str, dict[str, Any]] = {
    "search_meme": {
        "description": (
            "在表情库里按语义搜索表情包，返回若干候选供你挑选。"
            "候选附有图上文字、描述和适合回应的话；选中合适的就用编号调用 send_meme，"
            "都不合适就不发，也可以换个描述再搜。"
            "不要用文件、终端或通用消息工具直接发送表情库里的文件。"
        ),
        "properties": {
            "description": {
                "type": "string",
                "description": (
                    "你想发的表情包长什么样：画面主体与动作、图上可能的文字、想表达的语气。"
                    "写成一两句自然的话，例如“一只猫瞪大眼睛，写着‘啊？’，表示难以置信”。"
                ),
            }
        },
        "required": ["description"],
    },
    "send_meme": {
        "description": (
            "发送 search_meme 返回的某个候选表情包，它会在你的文字回复之后发出。"
            "不要在回复正文里提到这张表情包。"
        ),
        "properties": {
            "emoji_id": {"type": "integer", "description": "search_meme 返回的候选编号。"}
        },
        "required": ["emoji_id"],
    },
    "steal_meme": {
        "description": (
            "把当前消息（或用户引用的那条消息）里的图片收进表情库，"
            "视觉模型会自动审核并标注图上文字、描述和适用场景。"
            "用户说“偷一下”“收了这张图”时直接调用；看到适合作表情包的图片时也可以主动收录。"
            "image_ref 可以留空，留空时使用当前消息里的第一张图片。"
            "表情包偷取总开关关闭或当前会话被偷取名单禁用时会拒绝入库。"
        ),
        "properties": {
            "image_ref": {
                "type": "string",
                "description": "可选。当前消息中某张图片的 URL 或文件路径；留空则取第一张图片。",
            }
        },
        "required": [],
    },
}


def _sniff_mime(head: bytes) -> str:
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"GIF":
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


class _MohobotBridge:
    """StealerEvent 发消息 / 取图用的 WSServer 适配。"""

    @staticmethod
    def _ws() -> Any:
        return Plugin._ws_server

    async def send_segments(
        self, bot_id: str, chat_type: str, chat_id: str, segments: list[dict[str, Any]]
    ) -> bool:
        ws = self._ws()
        if ws is None or not chat_id:
            logger.warning("[Stealer] WSServer 未就绪，消息未发送")
            return False
        try:
            if chat_type == "group":
                await ws.send_group_msg(bot_id, chat_id, segments)
            else:
                await ws.send_private_msg(bot_id, chat_id, segments)
            return True
        except Exception as e:
            logger.warning(f"[Stealer] 发送消息失败: {e}")
            return False

    async def _call(self, bot_id: str, action: str, params: dict[str, Any], timeout: float) -> dict:
        ws = self._ws()
        if ws is None:
            return {}
        try:
            resp = await ws.send_to_bot(bot_id, action, params, wait_response=True, timeout=timeout)
        except Exception as e:
            logger.debug(f"[Stealer] OneBot {action} 调用失败: {e}")
            return {}
        data = (resp or {}).get("data") if isinstance(resp, dict) else None
        return data if isinstance(data, dict) else {}

    async def fetch_image_data_uri(self, bot_id: str, file_ref: str) -> str:
        if not file_ref or file_ref.startswith("base64://"):
            return ""
        data = await self._call(bot_id, "get_image", {"file": file_ref}, timeout=8.0)
        b64 = str(data.get("base64", "") or "")
        if b64:
            try:
                head = base64.b64decode(b64[:64])
            except Exception:
                head = b""
            return f"data:{_sniff_mime(head)};base64,{b64}"
        url = str(data.get("url", "") or "")
        return url if url.startswith(("http://", "https://")) else ""

    async def fetch_message_segments(self, bot_id: str, message_id: str) -> list[dict[str, Any]]:
        try:
            mid: Any = int(message_id)
        except (TypeError, ValueError):
            mid = message_id
        data = await self._call(bot_id, "get_msg", {"message_id": mid}, timeout=5.0)
        message = data.get("message")
        if isinstance(message, list):
            return [seg for seg in message if isinstance(seg, dict)]
        if isinstance(message, str):
            from meme_stealer_core.host.event import parse_message_segments

            return parse_message_segments(message)
        return []


class Plugin:
    """表情包小偷：自动偷取群聊表情包，视觉模型审核标注入库；聊天时会话模型按语义检索并配表情包。WebUI 见插件配置。"""

    global_triggers = {COMMAND}

    info = {
        "commands": [
            {"name": "meme status", "desc": "表情包小偷运行状态与统计"},
            {"name": "meme list", "desc": "列出表情包（/meme list [分类] [每页数量] [页码]）"},
            {"name": "meme on/off", "desc": "开关表情包偷取", "admin": True},
            {"name": "meme auto_on/auto_off", "desc": "开关聊天配表情", "admin": True},
            {"name": "meme 偷", "desc": "30 秒内发的下一张图直接入库", "admin": True},
            {"name": "meme help", "desc": "表情包小偷全部指令"},
        ]
    }

    # ── 注入（必须 classmethod）──
    _ws_server: Any = None
    _bot_manager: Any = None
    _llm_service: Any = None
    _task_supervisor: Any = None
    _data_dir: str = "./data"
    _admin_ids: list[str] = []

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_bot_manager(cls, bot_manager) -> None:
        cls._bot_manager = bot_manager

    @classmethod
    def inject_llm_service(cls, llm_service) -> None:
        cls._llm_service = llm_service

    @classmethod
    def inject_task_supervisor(cls, supervisor) -> None:
        cls._task_supervisor = supervisor

    @classmethod
    def inject_data_dir(cls, data_dir) -> None:
        cls._data_dir = str(data_dir)

    @classmethod
    def inject_admin_ids(cls, admin_ids) -> None:
        cls._admin_ids = [str(a) for a in (admin_ids or [])]

    def __init__(self) -> None:
        self.plugin_config: dict[str, Any] = {}
        self.app: StealerApp | None = None
        self.webui: WebUIServer | None = None
        self._bridge = _MohobotBridge()
        self._registered_tools: set[str] = set()
        self._seen_messages: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._hinted_chats: set[tuple[str, str, str]] = set()
        self._background: set[asyncio.Task] = set()

    # ── 生命周期 ───────────────────────────────────────────

    def _config_path(self) -> Path:
        return Path(self._data_dir) / "plugins_config" / f"{PLUGIN_NAME}.json"

    def _plugin_data_dir(self) -> Path:
        return Path(self._data_dir) / "plugins_data" / PLUGIN_NAME

    def _load_config_store(self) -> ConfigStore:
        """面板注入的配置只含 schema 字段；插件自己写过的键以存档为准一并读入。"""
        path = self._config_path()
        initial = dict(getattr(self, "plugin_config", None) or {})
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if isinstance(saved, dict):
            initial.update(saved)
        return ConfigStore(path, initial)

    async def on_startup(self) -> None:
        try:
            app = StealerApp(
                self._load_config_store(),
                self._plugin_data_dir(),
                plugin_dir=_PLUGIN_DIR,
                llm_service_getter=lambda: Plugin._llm_service,
                tools_registered=self._tools_registered,
            )
            await app.initialize()
        except Exception as e:
            logger.error(f"[Stealer] 插件启动失败: {e}", exc_info=True)
            return
        self.app = app
        self._register_tools()
        await self._sync_webui()

    async def on_shutdown(self) -> None:
        self._unregister_tools()
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        self._background.clear()
        if self.webui is not None:
            await self.webui.stop()
            self.webui = None
        app, self.app = self.app, None
        if app is not None:
            await app.terminate()

    def on_config_update(self, config: dict) -> None:
        """面板保存配置后同步调用（不能是 async）。"""
        self.plugin_config = config or {}
        app = self.app
        if app is None:
            return
        changed = app.apply_external_config(self.plugin_config)
        if changed & {"webui_enabled", "webui_host", "webui_port", "webui_password"}:
            if "webui_password" in changed and self.webui is not None:
                self.webui.revoke_all_sessions()
            self._spawn(self._sync_webui(restart=bool(changed - {"webui_password"})), "webui-sync")

    def _spawn(self, coro, name: str) -> None:
        supervisor = self._task_supervisor
        if supervisor is not None:
            try:
                task = supervisor.create_task(coro, name=f"meme_stealer:{name}", owner="meme_stealer")
            except Exception:
                coro.close()
                return
        else:
            task = asyncio.get_running_loop().create_task(coro, name=f"meme_stealer:{name}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _sync_webui(self, restart: bool = False) -> None:
        app = self.app
        if app is None:
            return
        cfg = app.plugin_config
        if self.webui is not None and (restart or not cfg.webui_enabled):
            await self.webui.stop()
            self.webui = None
        if not cfg.webui_enabled or self.webui is not None:
            return
        webui = WebUIServer(
            app.plugin_api,
            pages_dir=_PLUGIN_DIR / "pages" / "dashboard",
            i18n_dir=_PLUGIN_DIR / "i18n",
            password_getter=lambda: str(getattr(app.plugin_config, "webui_password", "") or ""),
        )
        if await webui.start(str(cfg.webui_host or "127.0.0.1"), int(cfg.webui_port or 9092)):
            self.webui = webui

    # ── LLM 工具 ───────────────────────────────────────────

    def _tools_registered(self) -> bool:
        try:
            from mohobot.services.llm_tools import registry
        except ImportError:
            return False
        return all(registry.contains(name) for name in ("search_meme", "send_meme"))

    def _register_tools(self) -> None:
        try:
            from mohobot.services.llm_tools import LLMTool, registry, tool_schema
        except ImportError:
            logger.warning("[Stealer] 当前 mohobot 不支持插件 LLM 工具，search_meme 等工具不可用")
            return
        handlers = {
            "search_meme": self._tool_search_meme,
            "send_meme": self._tool_send_meme,
            "steal_meme": self._tool_steal_meme,
        }
        tools = getattr(registry, "_tools", None)
        for name, spec in TOOL_SPECS.items():
            if isinstance(tools, dict):
                # 上一次加载异常退出时可能遗留同名工具；按名字替换
                tools.pop(name, None)
            try:
                registry.register(
                    LLMTool(
                        tool_schema(name, spec["description"], spec["properties"], spec["required"]),
                        handlers[name],
                    )
                )
                self._registered_tools.add(name)
            except Exception as e:
                logger.error(f"[Stealer] 注册 LLM 工具 {name} 失败: {e}")
        if self._registered_tools:
            logger.info(f"[Stealer] 已注册 LLM 工具: {', '.join(sorted(self._registered_tools))}")

    def _unregister_tools(self) -> None:
        try:
            from mohobot.services.llm_tools import registry
        except ImportError:
            return
        tools = getattr(registry, "_tools", None)
        if isinstance(tools, dict):
            for name in self._registered_tools:
                tools.pop(name, None)
        self._registered_tools.clear()

    def _tool_context(self) -> tuple[StealerApp | None, StealerEvent | None]:
        return self.app, _CURRENT_EVENT.get()

    async def _tool_search_meme(self, description: str = "", **_: Any) -> str:
        app, event = self._tool_context()
        if app is None or event is None:
            return "表情包工具当前不可用。"
        return await app.search_meme(event, description)

    async def _tool_send_meme(self, emoji_id: Any = None, **_: Any) -> str:
        app, event = self._tool_context()
        if app is None or event is None:
            return "表情包工具当前不可用。"
        result = await app.send_meme(event, emoji_id)
        if app.has_queued_meme(event):
            self._dispatch_after_reply(app, event)
        return result

    async def _tool_steal_meme(self, image_ref: str = "", **_: Any) -> str:
        app, event = self._tool_context()
        if app is None or event is None:
            return "表情包工具当前不可用。"
        return await app.steal_meme(event, str(image_ref or ""))

    @staticmethod
    def _dispatch_after_reply(app: StealerApp, event: StealerEvent) -> None:
        """本条消息的处理 task 结束（回复已全部发出）后再发排队的表情。"""
        if event.get_extra("stealer_dispatch_hooked"):
            return
        task = asyncio.current_task()
        if task is None:
            app.after_reply(event)
            return
        event.set_extra("stealer_dispatch_hooked", True)

        def _on_done(done: asyncio.Task) -> None:
            if done.cancelled():
                return
            try:
                app.after_reply(event)
            except Exception as e:
                logger.warning(f"[Stealer] 发送排队表情失败: {e}")

        task.add_done_callback(_on_done)

    # ── 消息钩子 ───────────────────────────────────────────

    def _bind_event(self, bot_id: str, event: Any, raw: dict) -> StealerEvent:
        current = _CURRENT_EVENT.get()
        if current is not None and current.event is event and current.bot_id == str(bot_id):
            return current
        stealer_event = StealerEvent(bot_id, event, raw, bridge=self._bridge)
        _CURRENT_EVENT.set(stealer_event)
        return stealer_event

    def _claim_message(self, event: StealerEvent) -> bool:
        """同一条群消息会经每个 bot 的连接各推送一次；偷图与聊天记录只处理一次。"""
        if not event.message_id:
            return True
        key = (event.unified_msg_origin, event.message_id)
        if key in self._seen_messages:
            return False
        self._seen_messages[key] = None
        while len(self._seen_messages) > _SEEN_MESSAGE_LIMIT:
            self._seen_messages.popitem(last=False)
        return True

    def _bot_qq(self, bot_id: str, event: StealerEvent) -> str:
        bm = self._bot_manager
        instance = bm.get(bot_id) if bm is not None else None
        qq = str(getattr(instance, "qq", "") or "") if instance is not None else ""
        return qq if qq and qq != "0" else event.self_id

    def _is_known_bot(self, user_id: str) -> bool:
        bm = self._bot_manager
        if bm is None or not user_id:
            return False
        try:
            return any(str(getattr(inst, "qq", "")) == user_id for inst in bm.all_bots)
        except Exception:
            return False

    def _will_reply(self, bot_id: str, event: StealerEvent) -> bool:
        """mohobot 会不会让 LLM 回复这条消息（与其群聊门控规则一致）。"""
        text = event.get_message_str()
        if text.startswith("/") or text.strip().lower() == "ping":
            return False
        if not event.group_id:
            return True
        bot_qq = self._bot_qq(bot_id, event)
        for seg in event.message_obj.raw_message.get("message", []):
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if seg.get("type") == "at" and bot_qq and str(data.get("qq", "")) == bot_qq:
                return True
        reply_id = event.get_reply_id()
        if reply_id and self._bot_manager is not None:
            instance = self._bot_manager.get(bot_id)
            if instance is not None and instance.is_my_message("group", event.group_id, reply_id):
                return True
        return False

    def _clear_stale_hint(self, bot_id: str, event: StealerEvent) -> None:
        """mohobot 只在感知文本非空时更新缓存：上一轮带过选图提示、这一轮没有时，
        若其他插件也没给感知文本，旧提示会残留到下一次回复，这里主动清掉。"""
        key = (str(bot_id), event.chat_type, event.chat_id)
        if key not in self._hinted_chats:
            return
        self._hinted_chats.discard(key)
        handler = getattr(getattr(self._ws_server, "_on_event", None), "__self__", None)
        cache = getattr(handler, "_perception_text", None)
        if isinstance(cache, dict):
            cache.pop(key, None)

    async def on_perception(self, bot_id: str, event: Any, raw: dict) -> str:
        app = self.app
        if app is None:
            return ""
        stealer_event = self._bind_event(bot_id, event, raw)
        hint = ""
        if self._will_reply(bot_id, stealer_event):
            hint = await app.build_meme_hint(stealer_event)
        if hint:
            self._hinted_chats.add((str(bot_id), stealer_event.chat_type, stealer_event.chat_id))
        else:
            self._clear_stale_hint(bot_id, stealer_event)
        return hint

    async def on_message_observed(self, bot_id: str, event: Any, raw: dict):
        app = self.app
        if app is None:
            return (False, None)
        stealer_event = self._bind_event(bot_id, event, raw)
        app.cancel_pending_meme(stealer_event)
        if self._is_known_bot(stealer_event.user_id):
            return (False, None)
        if self._claim_message(stealer_event):
            await app.on_message(stealer_event)
        return (False, None)

    async def on_message(self, bot_id: str, event: Any, raw: dict):
        stealer_event = self._bind_event(bot_id, event, raw)
        text = stealer_event.get_message_str()
        if not (text == COMMAND or text.startswith(COMMAND + " ")):
            return (False, None)
        app = self.app
        if app is None:
            return (True, "表情包小偷未能启动，请查看日志。")
        is_admin = stealer_event.user_id in set(self._admin_ids)
        args = text[len(COMMAND):].split()
        texts: list[str] = []
        try:
            async for result in app.run_command(stealer_event, args, is_admin=is_admin):
                if result is None:
                    continue
                if isinstance(result, str):
                    texts.append(result)
                elif result.has_images():
                    if texts:
                        await stealer_event.send("\n".join(texts))
                        texts.clear()
                    await stealer_event.send(result)
                else:
                    plain = result.get_plain_text()
                    if plain:
                        texts.append(plain)
        except Exception as e:
            logger.error(f"[Stealer] 指令执行失败: {e}", exc_info=True)
            texts.append(f"❌ 指令执行失败: {e}")
        return (True, "\n".join(texts) or None)
