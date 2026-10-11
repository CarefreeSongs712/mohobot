"""回复产物过滤器 — 剥除 LLM 回复中不应发送的标记产物。

行为设计基于 2026-10 生产 7 天 41,565 条 LLM 发送消息的产物普查(约 6.4% 受影响),
经用户确认的三类策略:

- VOICE(tts 家族含拼写变体与（tts）圆括号形态): 剥标签, 内容照常显示并计入朗读文本;
- FORMAT(模型自创格式/装饰标签 text/response/caps/b/call/p/q 等): 剥标签, 内容照常显示;
- DROP(工具调用语法块 tool_call 系、思考块 think 系、emoji/bbox 等代码类标签):
  标签与内部内容整块丢弃, 不显示不计入语音。

识别容忍:
- 大小写不敏感; 尖/方/【】/圆括号家族; 可选 / 闭前缀; 已知标签名允许携带属性;
- 尖括号下的未知纯标签按 FORMAT 处理(剥标签保内容), 对未来新产物自愈;
  方括号/【】/圆括号家族只对 VOICE 别名生效, 避免误伤 [assistant] 等引用文本;
- 词边界安全: <ttsx>、<tts2> 不会被 <tts> 规则误判。

流式安全: 疑似标签起始处扣留待判定, 判定失败按原文放行, 扣留有界;
未闭合: voice 到结尾(容错忘写闭标签), drop 到结尾(泄漏不显示),
工具语法块超过 salvage 上限时按正文放行(防止畸形块吞掉整条回复)。
"""

from __future__ import annotations

import re

# ── 标签词表(源自生产普查, 可随新样本扩充) ──────────────────

# 朗读标注: 标签剥除, 内容显示并计入语音
VOICE_TAGS = frozenset({
    "tts", "ts", "voice", "speech", "say",
    "tt", "ttt", "tss", "tgs", "mts", "is_tts", "welcome_tts",
})
# 格式标签: 标签剥除, 内容照常显示
FORMAT_TAGS = frozenset({
    "text", "txt", "response", "caps", "b", "i", "s", "e", "em", "u",
    "strong", "bold", "call", "p", "q", "span", "div", "td", "tr", "th",
    "table", "button", "br", "hr", "font", "snoring", "cough", "sigh",
    "envelope", "message", "tags",
})
# 丢弃块: 标签与内容整体丢弃
DROP_TAGS = frozenset({
    "tool_call", "function", "parameter", "arg_key", "arg_value",
    "description", "search_meme", "steal_meme", "send_meme",
    "think", "thinking", "reasoning", "consumed", "eot",
    "emoji", "bbox", "ratio",
})
# 工具语法块的救援上限: 超长未闭合视为畸形, 已吞内容按正文放行
_DROP_SALVAGE_CHARS = 2000

# 方括号/【】/圆括号家族只认 VOICE 别名(含既有 ts/voice/speech/say)
_VOICE_ALIAS_ALT = "|".join(sorted(VOICE_TAGS, key=len, reverse=True))

# 尖括号标签: 开/闭 + 名字 + 可选属性(属性不参与名判定)。
# 属性段限定 ASCII 可打印字符: 产物的属性(如 style="...")都是 ASCII,
# 而正文里 "a < b 且 c > d" 这类比较文本含中文, 借此天然排除。
_ANGLE_TAG_RE = re.compile(
    r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9_-]{0,18})\s*[\x20-\x7e]*?>"
)
_BR_VOICE_RE = re.compile(
    r"[\[【]\s*(/?)\s*(" + _VOICE_ALIAS_ALT + r")\s*[\]】]",
    re.IGNORECASE,
)
_PAR_VOICE_RE = re.compile(
    r"[(（]\s*(/?)\s*(" + _VOICE_ALIAS_ALT + r")\s*[)）]",
    re.IGNORECASE,
)

# 流式扣留: 各家族起始定界符与其纯标签形态的长度上限
_HOLD_CAP_ANGLE = 80   # 允许携带属性
_HOLD_CAP_SHORT = 14   # [tts] / 【/tts】 / （tts） 等短形态


def _classify(name: str) -> str:
    """标签名 → 'voice' | 'format' | 'drop'(未知标签按 format 自愈)。"""
    low = name.lower()
    if low in VOICE_TAGS:
        return "voice"
    if low in DROP_TAGS:
        return "drop"
    return "format"


def _partial_tail_hold(buf: str, mode: str | None) -> int | None:
    """返回流式扣留起点(相对 buf); None = 不扣留。

    找最后一个"可能是未完成标签起始"的位置:
    - 尖括号: < 后无 >, 且形态仍像标签(可选 / + 名字前缀 + 无 <>), 长度有界;
    - 方/【/圆括号: 起始符后是某 VOICE 别名(可含 / 与空格)的真前缀, 长度有界。
    drop 模式下没有需要保序显示的内容, 不扣留。
    """
    if mode == "drop":
        return None
    for i in range(len(buf) - 1, -1, -1):
        ch = buf[i]
        if ch == "<":
            tail = buf[i:]
            if ">" in tail[1:]:
                return None  # 已有闭定界符却未成完整标签: 不是标签, 放行
            if len(tail) > _HOLD_CAP_ANGLE:
                return None
            # 名字可选: 裸 "<" 与 "</" 都可能补全成标签, 也要扣留
            if re.fullmatch(r"<\s*/?\s*[a-zA-Z]?[a-zA-Z0-9_-]{0,18}[\x20-\x7e]{0,80}", tail):
                return i
            return None  # 形态已不可能成为标签(如属性含中文), 放行
        if ch in "[【(" or ch == "（":
            closer = {"[": "]", "【": "】", "(": ")", "（": "）"}[ch]
            tail = buf[i:]
            if closer in tail[1:]:
                return None
            if len(tail) > _HOLD_CAP_SHORT:
                return None
            inner = re.sub(r"\s+", "", tail[1:])
            if inner.startswith("/"):
                inner = inner[1:]
            if inner and any(a.startswith(inner) for a in VOICE_TAGS):
                return i
            if not inner:
                return i  # 仅起始符: 仍可能成为 [tts / （tts
            return None
    return None


def _pick_tts_text(spans: list[str]) -> str:
    """合并全部非空标注为一条朗读文本(多段按出现顺序, 空行分隔)。"""
    parts = [s.strip() for s in spans if s.strip()]
    return "\n".join(parts)


class ReplyArtifactFilter:
    """流式回复产物过滤器。

    用法:
        f = ReplyArtifactFilter()
        for chunk in stream:
            display = f.feed(chunk)          # 可安全发出的显示文本(可能为空)
        rest, tts_text = f.finish()          # 流结束: 剩余显示文本 + 朗读文本

    非流式可直接用 strip_reply_artifacts()。
    """

    def __init__(self):
        self._buf = ""
        self._mode: str | None = None      # None=正文 / 'voice' / 'drop'
        self._drop_name = ""
        self._spans: list[str] = []        # 已闭合的完整朗读标注
        self._cur_span: list[str] = []     # 当前未闭合标注的内容分片

    # ── 公开接口 ─────────────────────────────────────────────

    def feed(self, chunk: str) -> str:
        """喂入一个流式 chunk, 返回当前可安全发出的显示文本。"""
        if chunk:
            self._buf += chunk
        return self._drain(final=False)

    def finish(self) -> tuple[str, str]:
        """流结束: 冲刷缓冲, 返回 (剩余显示文本, 朗读文本)。"""
        out = self._drain(final=True)
        return out, _pick_tts_text(self._spans)

    # ── 内部 ─────────────────────────────────────────────────

    def _emit_content(self, out: list[str], piece: str) -> None:
        """正文同时进显示流; voice 态下同时累积朗读内容。"""
        if not piece:
            return
        out.append(piece)
        if self._mode == "voice":
            self._cur_span.append(piece)

    def _drain(self, final: bool) -> str:
        out: list[str] = []
        while True:
            if not self._buf:
                if final and self._mode == "voice":
                    self._close_span()
                break

            if self._mode == "drop":
                if self._consume_drop(final, out):
                    continue
                break

            match = self._next_tag()
            if match is not None:
                kind, m = match
                if not self._handle_tag(kind, m, out):
                    continue
                continue

            hold = _partial_tail_hold(self._buf, self._mode)
            if hold is not None:
                self._emit_content(out, self._buf[:hold])
                self._buf = self._buf[hold:]
                if final:
                    if self._mode == "voice":
                        self._close_span()
                    out.append(self._buf)
                    self._buf = ""
                    break
                break  # 扣留等待更多数据
            self._emit_content(out, self._buf)
            self._buf = ""
            if final and self._mode == "voice":
                self._close_span()
            break
        return "".join(out)

    def _close_span(self) -> None:
        if self._mode == "voice":
            self._spans.append("".join(self._cur_span))
            self._cur_span = []
        self._mode = None
        self._drop_name = ""

    def _next_tag(self):
        """buf 中最早的完整标签; 返回 (家族, match) 或 None。

        同起点时尖括号优先(形态更明确)。
        """
        best = None  # (start, kind, m)
        for regex, kind in ((_ANGLE_TAG_RE, "angle"), (_BR_VOICE_RE, "br"), (_PAR_VOICE_RE, "par")):
            m = regex.search(self._buf)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), kind, m)
        if best is None:
            return None
        return best[1], best[2]

    def _handle_tag(self, kind: str, m, out: list[str]) -> bool:
        """处理一个完整标签; 返回 True 表示已消费, False 表示转 drop 态后重扫。"""
        if kind == "angle":
            closing, name = bool(m.group(1)), m.group(2)
            family = _classify(name)
        else:
            closing, name = bool(m.group(1)), m.group(2).lower()
            family = "voice"

        if self._mode == "voice":
            if family == "voice" and closing:
                self._emit_content(out, self._buf[:m.start()])
                self._buf = self._buf[m.end():]
                self._close_span()
                return True
            if family == "voice":
                # 连续标注(如 "(tts)…(tts)…"): 剥标签并重开一段,
                # 不按正文显示 —— 生产中模型会重复使用同类前缀标注
                self._emit_content(out, self._buf[:m.start()])
                self._buf = self._buf[m.end():]
                self._close_span()
                self._mode = "voice"
                self._cur_span = []
                return True
            # voice 态内其它家族标签一律按正文(与既有 TTS 过滤的嵌套语义一致)
            self._emit_content(out, self._buf[:m.end()])
            self._buf = self._buf[m.end():]
            return True

        # 正文态
        if family == "voice":
            self._emit_content(out, self._buf[:m.start()])
            self._buf = self._buf[m.end():]
            if closing:
                return True  # 游离闭标签: 丢弃
            self._mode = "voice"
            self._cur_span = []
            return True

        if family == "drop":
            self._emit_content(out, self._buf[:m.start()])
            self._buf = self._buf[m.end():]
            if closing:
                return True  # 游离闭标签: 丢弃
            self._mode = "drop"
            self._drop_name = name.lower()
            return True

        # format(含未知): 剥标签保内容
        self._emit_content(out, self._buf[:m.start()])
        self._buf = self._buf[m.end():]
        return True

    def _consume_drop(self, final: bool, out: list[str]) -> bool:
        """drop 态: 吞掉内容直到同名闭标签; 返回 True 表示可继续主循环。"""
        close_re = re.compile(
            r"<\s*/\s*" + re.escape(self._drop_name) + r"\s*[^<>]*?>",
            re.IGNORECASE,
        )
        m = close_re.search(self._buf)
        if m:
            self._buf = self._buf[m.end():]
            self._close_span()
            return True
        if final:
            self._buf = ""
            self._close_span()
            return True
        if len(self._buf) > _DROP_SALVAGE_CHARS and self._drop_name in {
            "tool_call", "function", "parameter", "arg_key", "arg_value",
            "description", "search_meme", "steal_meme", "send_meme",
        }:
            # 畸形工具块: 不再吞正文, 已吞内容按正文放行
            salvage, self._buf = self._buf, ""
            self._close_span()
            out.append(salvage)
            return True
        return False


def strip_reply_artifacts(text: str) -> tuple[str, str]:
    """非流式全文处理: 剥除所有可识别产物。

    返回 (显示文本, 朗读文本) — 显示文本 = voice/format 标签已剥、drop 块已弃的
    全文; 朗读文本按顺序合并全部 voice 标注(含变体), 无标注时为空串。
    """
    f = ReplyArtifactFilter()
    display = f.feed(text or "")
    rest, tts_text = f.finish()
    return display + rest, tts_text
