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

# 尖括号语法分两层, 兼顾"不误伤 ASCII 比较式"与"不放过内部块":
#
# 1. 通用层(_ANGLE_TAG_RE): 属性必须是 name=value 形态(value 可带引号),
#    且属性名以字母/下划线/冒号开头。于是 `a < b && c > d`、`a < foo bar > c`
#    这类比较式不会被当成标签剥掉(旧实现允许任意 ASCII 属性段, 会误删)。
# 2. DROP 层(_ANGLE_DROP_ATTR_RE): 内部块标签必须尽量认全, 否则"开标签认不出、
#    闭标签被当游离闭标签丢"会让内部内容直接泄漏给用户。所以对 DROP 家族额外
#    接受裸 token 属性(`<eot 1>`、`<tool_call foo=bar>`)与紧跟其后的
#    `name=value`(`<function=search_meme>`), 且属性段引号感知 —— 属性值里的
#    `>`(如 `<think data="a>b">`)不会被误当作标签结尾。
_ANGLE_ATTR_NAME = r"[a-zA-Z_:][a-zA-Z0-9_.:-]*"
_ANGLE_ATTR_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^\s\"'<>]+)"
_ANGLE_TAG_RE = re.compile(
    r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9_-]{0,18})"
    r"(?:\s+" + _ANGLE_ATTR_NAME + r"\s*=\s*" + _ANGLE_ATTR_VALUE + r")*\s*/?>"
)
_ANGLE_DROP_TOKEN = (
    r"(?:" + _ANGLE_ATTR_NAME + r"\s*=\s*" + _ANGLE_ATTR_VALUE + r"|[^\s\"'<>]+)"
)
_ANGLE_DROP_ATTR_RE = re.compile(
    r"<\s*(/?)\s*(" + "|".join(sorted(DROP_TAGS, key=len, reverse=True)) + r")"
    r"(?=[\s/>=])(?:\s*=\s*" + _ANGLE_ATTR_VALUE + r")?"
    r"(?:\s+" + _ANGLE_DROP_TOKEN + r")*\s*/?>",
    re.IGNORECASE,
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
            if _has_unquoted_angle_close(tail):
                return None  # 引号外已有闭定界符却未成完整标签: 不是标签, 放行
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


def _has_unquoted_angle_close(tail: str) -> bool:
    """tail 中是否存在「引号外」的 ``>``。

    属性值里允许出现 ``>``(如 ``<think data="a>b">``): 若把引号内的 ``>``
    当作标签已闭合, 流式分片一旦落在该位置之后就会把未完成的标签当正文吐出,
    造成"整段正常、分片泄漏"的不一致。
    """
    quote = ""
    for ch in tail:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return True
    return False


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
        self._drop_stack: list[str] = []
        self._drop_return_mode: str | None = None
        self._drop_scan_pos = 0
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
                if final and self._mode == "drop":
                    self._leave_drop()
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

    def _next_tag(self, start: int = 0):
        """buf 中最早的完整标签; 返回 (家族, match) 或 None。

        同起点时尖括号优先(形态更明确)。
        """
        best = None  # (start, kind, m)
        for regex, kind in ((_ANGLE_TAG_RE, "angle"), (_ANGLE_DROP_ATTR_RE, "angle"),
                            (_BR_VOICE_RE, "br"), (_PAR_VOICE_RE, "par")):
            m = regex.search(self._buf, start)
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
            # DROP 的不显示/不朗读契约优先于 voice; FORMAT 仍剥壳保正文。
            self._emit_content(out, self._buf[:m.start()])
            self._buf = self._buf[m.end():]
            if family == "drop" and not closing:
                self._enter_drop(name, return_mode="voice")
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
            self._enter_drop(name, return_mode=None)
            return True

        # format(含未知): 剥标签保内容
        self._emit_content(out, self._buf[:m.start()])
        self._buf = self._buf[m.end():]
        return True

    def _enter_drop(self, name: str, *, return_mode: str | None) -> None:
        self._drop_name = name.lower()
        self._drop_stack = [self._drop_name]
        self._drop_return_mode = return_mode
        self._drop_scan_pos = 0
        self._mode = "drop"

    def _leave_drop(self) -> None:
        self._mode = self._drop_return_mode
        self._drop_return_mode = None
        self._drop_stack = []
        self._drop_name = ""
        self._drop_scan_pos = 0

    def _consume_drop(self, final: bool, out: list[str]) -> bool:
        """DROP 嵌套压栈, 直到外层闭合后恢复此前正文/VOICE 状态。"""
        while True:
            match = self._next_tag(self._drop_scan_pos)
            if match is None:
                break
            kind, m = match
            self._drop_scan_pos = m.end()
            if kind != "angle" or _classify(m.group(2)) != "drop":
                continue
            closing, name = bool(m.group(1)), m.group(2).lower()
            if not closing:
                self._drop_stack.append(name)
            elif name in self._drop_stack:
                # 模型工具产物常内层闭标签错配; 祖先闭合时一并吞掉
                # 未闭合子块。匹配最近同名层, 保持同名嵌套的泄漏保护。
                matching = len(self._drop_stack) - 1 - self._drop_stack[::-1].index(name)
                del self._drop_stack[matching:]
                if not self._drop_stack:
                    self._buf = self._buf[m.end():]
                    self._leave_drop()
                    return True
        if final:
            self._buf = ""
            self._leave_drop()
            return True
        if len(self._buf) > _DROP_SALVAGE_CHARS and self._drop_name in {
            "tool_call", "function", "parameter", "arg_key", "arg_value",
            "description", "search_meme", "steal_meme", "send_meme",
        }:
            # 保留既有超长未闭合工具块救援策略: 内容按正文放行, 不吞掉整条回复。
            # 但畸形工具块的内容不得进入朗读 —— 否则流式分片大小会改变朗读文本
            # (实测 chunk=1 时约 500 字工具内容会被读出来)。因此这里结束当前
            # voice 片段, 并让后续正文留在正文态, 不再累积到朗读。
            salvage, self._buf = self._buf, ""
            if self._drop_return_mode == "voice":
                self._spans.append("".join(self._cur_span))
                self._cur_span = []
            self._drop_return_mode = None
            self._leave_drop()
            out.append(salvage)
            return True
        return False


# ── 无标签推理/自查块 (Issue #57) ────────────────────────────
#
# 生产事故: 模型把"检查系统约束 / 编写回复 / 字数检查 / 符合要求"这类
# 内部自查过程当成正文输出, 且没有包在 <think> 等标签里, 标签过滤器完全
# 抓不到, 分段逻辑还会把它拆成十几个气泡直接发给用户。
#
# 这类块只出现在回复的**开头**, 且有稳定的中文表头。清洗策略:
#   1. 若末尾出现"符合要求。"类自检结论, 取它之后的正文(模型常会把最终
#      回复在自查后再输出一遍);
#   2. 否则取"编写回复：/生成回复："之后的草稿;
#   3. 再否则把开头的自查块整段剔除。
# 非"开头 + 表头 + 冒号"三重命中不动原文, 避免误伤正常聊天。

# 可能出现在推理块开头的表头(必须紧跟冒号才判定)
_REASONING_HEADERS = (
    "检查系统约束", "系统约束检查", "检查约束", "约束检查", "规则检查",
    "检查规则", "思考过程", "分析思路", "自我检查", "自查内容",
    "字数检查", "字数统计", "格式检查", "输出检查", "编写回复", "生成回复",
    "拟定回复", "实际回复",
)
# 草稿分隔标记: 其后为模型拟定的回复正文
_DRAFT_MARKERS = ("编写回复", "生成回复", "拟定回复", "实际回复")
# 自检通过标记: 其后常跟着真正要发出的回复
_PASS_MARKERS = ("符合要求", "检查通过", "满足要求", "符合规范")
# 尾部检查块表头(出现在草稿之后)
_TAIL_CHECK_HEADERS = ("字数检查", "字数统计", "格式检查", "输出检查")


def looks_like_unwrapped_reasoning(text: str) -> bool:
    """回复是否以无标签的自查/推理块开头(流式门控用)。

    要求"开头 + 已知表头 + 紧跟冒号"三重命中。冒号是必要条件: 像
    "检查系统约束很严格" 这种正常口语不会被误判。
    """
    stripped = (text or "").lstrip()
    for header in _REASONING_HEADERS:
        if stripped.startswith(header):
            rest = stripped[len(header):].lstrip()
            if rest.startswith(("：", ":")):
                return True
            # 表头后面可能先跟换行再写冒号(如"检查系统约束\n：")
            if rest[:1] in ("\n", "\r") and rest.lstrip().startswith(("：", ":")):
                return True
    return False


def _strip_tail_check_block(text: str) -> str:
    """剔除草稿之后的自检块(字数检查…符合要求), 保留其余正文。"""
    out = text
    for header in _TAIL_CHECK_HEADERS:
        idx = out.find(header + "：")
        if idx < 0:
            idx = out.find(header + ":")
        if idx < 0:
            continue
        tail = out[idx:]
        cut = -1
        for marker in _PASS_MARKERS:
            pos = tail.find(marker)
            if pos >= 0:
                cut = pos + len(marker)
                break
        if cut >= 0:
            # 自检结论之后可能还有正文(最终回复): 有实质内容才保留, 否则只留结论前正文
            remainder = tail[cut:].strip(" \t\r\n。.！!，,：:")
            head = out[:idx].rstrip()
            if _has_substance(remainder):
                out = f"{head}\n{remainder}" if head else remainder
            else:
                out = head
        else:
            # 没有结论标记: 认为该块一直到结尾都是自检, 丢弃
            out = out[:idx].rstrip()
    return out


def _has_substance(text: str) -> bool:
    """正文是否有实质内容(至少一个字母/数字/汉字)；纯标点/空白不算。"""
    return any(ch.isalnum() for ch in (text or ""))


def strip_unwrapped_reasoning(text: str) -> str:
    """剔除未包裹在标签里的模型自查/推理过程, 只保留真正要发出的回复。

    仅在"回复以已知自查表头 + 冒号开头"时生效; 否则原样返回, 不影响正常聊天。
    无法确定正文时返回空串(宁可这条不发, 也不把内心戏发给用户)。
    """
    if not text:
        return ""
    if not looks_like_unwrapped_reasoning(text):
        return text

    body = text.lstrip()

    # 策略 1: 末尾自检通过后模型又输出了一遍最终回复 —— 取最后一个结论标记之后
    best_tail = ""
    for marker in _PASS_MARKERS:
        pos = body.rfind(marker)
        if pos < 0:
            continue
        tail = body[pos + len(marker):].lstrip(" \t\r\n。.！!，,：:")
        if _has_substance(tail) and len(tail) > len(best_tail):
            best_tail = tail
    if _has_substance(best_tail):
        return best_tail.strip()

    # 策略 2: "编写回复：/生成回复：" 之后的草稿(可能有多个, 取最后一个)
    draft_pos = -1
    for marker in _DRAFT_MARKERS:
        for sep in ("：", ":"):
            pos = body.rfind(marker + sep)
            if pos > draft_pos:
                draft_pos = pos + len(marker) + 1
    if draft_pos >= 0:
        draft = body[draft_pos:].strip()
        draft = _strip_tail_check_block(draft).strip()
        if _has_substance(draft):
            return draft

    # 策略 3: 没有草稿标记 —— 剔除开头的自查块(到首个空行为止), 保留后续正文
    lines = body.split("\n")
    idx = 0
    while idx < len(lines) and lines[idx].strip():
        idx += 1
    rest = "\n".join(lines[idx:]).strip()
    if not rest:
        return ""
    # 剩余部分若仍是自查(如 字数检查…), 继续剥
    rest = _strip_tail_check_block(rest).strip()
    # 剥完只剩标点(如"符合要求。"的残留句号)视为无正文
    return rest if _has_substance(rest) else ""


# ── 中英混杂清理 (REQ-7) ─────────────────────────────────────
#
# 现象: 中文对话里突兀出现英文话语标记与 AI 套话, 如
#   "Well, 我觉得…" / "Okay so 那个…" / "As an AI language model, …"
# 这些是底座模型的英文偏置与"AI 自我认知"残留, 属 OOC。
#
# 策略: **只清理白名单内的英文话语标记与 AI 套话**, 不动正常英文内容
# (歌名/人名/术语如 "Vocaloid"、"BPM" 一律保留)。清理范围:
#   1. 句首的英文填充词 + 逗号(Well/Okay/So/Actually/Anyway/Alright/Hmm/Oh);
#   2. 句首的 "As an AI ..." / "I'm sorry, but ..." 类 AI 套话前缀;
#   3. 中英之间的多余空格(如 "我觉得 well 这个" 不动, 只处理删除后的残留空格)。
# 判定保守: 只匹配**句首**(行首或中文标点后), 且要求后接中文或直接接正文。

_ENGLISH_FILLERS = (
    "Well", "Okay", "OK", "So", "Actually", "Anyway", "Alright", "Hmm", "Oh",
    "Umm", "Uh", "Right", "Sure", "Indeed", "Basically", "Honestly", "Look",
)
_FILLER_RE = re.compile(
    r"(?:^|(?<=[。！？!?…\n，,；;：:]))\s*(?:"
    + "|".join(_ENGLISH_FILLERS)
    # 允许 "Okay so," / "Well then," 这类连接链; 逗号可选(中文回复里常直接接正文)
    + r")(?:\s+(?:so|then|well))?\s*[,，]?\s+",
    re.IGNORECASE,
)
# AI 自我认知套话: 整句前缀
_AI_BOILERPLATE_RE = re.compile(
    r"^(?:As an AI(?: language model)?|I'?m (?:just )?an AI|"
    r"I am (?:just )?an AI)[^。！？!?\n]{0,80}[。！？!?.,，]\s*",
    re.IGNORECASE,
)
# 英文道歉式套话前缀("I'm sorry, but ...")在中文回复里属 OOC
_APOLOGY_BOILERPLATE_RE = re.compile(
    r"^(?:I'?m sorry|Sorry)\s*[,，]\s*(?:but\s*)?", re.IGNORECASE,
)


def strip_english_fillers(text: str) -> str:
    """清理中文回复里突兀的英文话语标记与 AI 套话（保守白名单）。

    只处理句首的少量英文填充词与 AI 自我认知前缀；正常英文单词、歌名、
    专有名词一律保留。返回清理后的文本。
    """
    if not text:
        return ""
    out = text
    # 1) AI 套话(整句前缀) —— 仅在回复开头
    out = _AI_BOILERPLATE_RE.sub("", out.lstrip())
    # 2) 英文道歉套话
    out = _APOLOGY_BOILERPLATE_RE.sub("", out.lstrip())
    # 3) 句首英文填充词(可多次命中, 如 "Well, okay, 那…")
    for _ in range(4):
        new = _FILLER_RE.sub("", out)
        if new == out:
            break
        out = new
    return out.lstrip()


def strip_reply_artifacts(text: str) -> tuple[str, str]:
    """非流式全文处理: 剥除所有可识别产物。

    返回 (显示文本, 朗读文本) — 显示文本 = voice/format 标签已剥、drop 块已弃的
    全文; 朗读文本按顺序合并全部 voice 标注(含变体), 无标注时为空串。

    标签剥除后再做一次无标签自查块清洗(Issue #57): 模型会绕过标签把
    "检查约束/编写回复/字数检查" 直接写成正文, 必须在这一层兜住。
    """
    f = ReplyArtifactFilter()
    display = f.feed(text or "")
    rest, tts_text = f.finish()
    return strip_unwrapped_reasoning(display + rest), tts_text


# ── OOC 三防: 乱码符号 / 内心独白 / 整段外语漂移(2026-10 生产样本驱动) ──
# 设计约束: 全部保守判定 + 每次命中记日志(可统计), 宁可漏过不可误伤。
# 希腊/西里尔字母不在删除范围(俄语歌名等是合法内容)。

# 正常中文聊天用不到的文字系统: 阿拉伯/希伯来/叙利亚/孟加拉/古木基/泰米尔/
# 泰文/老挝/格鲁吉亚/埃塞俄比亚/藏文/缅甸/高棉。bot 采样漂移会产生这类 token
# (生产样本: "同款CT5别رخ被借"、"بالمأنح有话直说"、单发的"ค")。
_EXOTIC_GLYPH_RE = re.compile(
    "[\u0530-\u058F\u0590-\u05FF\u0600-\u06FF\u0700-\u074F\u0750-\u077F"
    "\u0980-\u09FF\u0A00-\u0A7F\u0B80-\u0BFF\u0D80-\u0DFF\u0E00-\u0E7F"
    "\u0E80-\u0EFF\u0F00-\u0FFF\u1000-\u109F\u10A0-\u10FF\u1200-\u137F"
    "\u1780-\u17FF\u2D00-\u2D2F]"
)

# 内心独白开头(模型把推理过程写成正文)。生产样本均为英文("Wait, no — the
# user sent…"), 中文样本暂缺 — 只列"逗号/破折号紧跟"的 wait 与 the user 系,
# 避免 "Wait a moment" 这类正常短语误伤。
_MONOLOGUE_RE = re.compile(
    r"^(?:wait\b[,，—–-]|wait, no\b|the (?:user|sender)\b|user (?:sent|wants|is)\b)",
    re.IGNORECASE,
)

# 歌曲卡片/命令帮助等模板特征: 命中任一则不按外语漂移处理。
# 编号行(N. / N、)与《》书名号是歌曲列表分段后的典型形态 —— 分段发送时
# 歌名行会脱离"为您找到了…"卡片头独立判定, 不豁免会误拦纯外文歌名行。
_FOREIGN_TEMPLATE_MARK = re.compile(
    r"为您找到|为您播放|播放第|♪|🎤|💿|未知指令|/help|用法[:：]|今日老婆|剩余抽取"
    r"|^\d+\s*[.、)．]|《[^》]{1,60}》|\d+:\d{2}"
)

# 代码/报错/URL/命令等"引用型内容"豁免: 中文会话里贴英文报错、代码、链接是正常需求,
# 不能被当成 OOC 整段丢弃。
_FOREIGN_REFERENCE_MARK = re.compile(
    r"```|`[^`]{2,}`|https?://|\bwww\.|"
    r"\b(?:Traceback|Error|Exception|TypeError|ValueError|KeyError|"
    r"ModuleNotFoundError|ImportError|AttributeError|SyntaxError)\b|"
    r"\b(?:pip|npm|npx|git|docker|sudo|apt|systemctl)\s+\S|"
    r"\b(?:import|def|class|return)\s+\w+|\{\s*\""
)

# 用户明确要求英文/翻译时, 中文触发也不拦英文回复(约束见 #88 的全局语言指令)。
_TRIGGER_ALLOWS_FOREIGN = re.compile(
    r"翻译|译成|译为|英文|英语|外语|translate|english|日文|日语", re.IGNORECASE
)


def wants_foreign_language(text: str) -> bool:
    """用户是否明确要求其它语言/翻译(用于放宽外语判定与语言约束)。"""
    return bool(_TRIGGER_ALLOWS_FOREIGN.search(text or ""))


def strip_exotic_glyphs(text: str) -> str:
    """删除正常聊天用不到的文字系统字符(阿拉伯/泰/藏等), 其余原样保留。

    豁免: 外文字符 ≥8 的长句视为合法翻译/引用内容(可带中文引导语)原样保留;
    生产 badcase 都是中文句子里混入的短碎片(؋/رخ/ค/بالمأنح, 最多 7 字), 仍会清理。
    """
    if not text:
        return ""
    exotic = _EXOTIC_GLYPH_RE.findall(text)
    if not exotic:
        return text
    if len(exotic) >= 8:
        from loguru import logger
        logger.info(f"[ooc-guard] 疑似合法外文, 保留: {text[:60]!r}")
        return text
    cleaned = _EXOTIC_GLYPH_RE.sub("", text)
    if cleaned != text:
        from loguru import logger
        logger.info(f"[ooc-guard] 删除乱码符号({len(exotic)}): {text[:60]!r}")
    return cleaned


def is_chinese_trigger(text: str) -> bool:
    """触发消息是否为中文会话(≥2 个汉字)。"""
    return bool(re.search(r"[\u4e00-\u9fff]{2,}", text or ""))


def looks_like_monologue(text: str) -> bool:
    """回复是否以内心独白句式开头(模型推理外泄的变体, 无 #60 的自查表头)。

    允许前置引号/加粗/句读/列表符号: 生产出现过 "�Wait, …"、"**Wait, …**" 变体。
    """
    cleaned = re.sub(r'^[\s"\'“”‘’*_>「『（(\-—–、，。！？:：]+', "", text or "")
    return bool(_MONOLOGUE_RE.match(cleaned))


def is_foreign_language_reply(segment: str, trigger_text: str) -> str:
    """中文触发消息下, 段落是否整体漂成了英文。

    返回非空字符串 = 命中原因(调用方丢弃该段); 空串 = 放行。
    判定(全部满足): 触发消息含≥2个汉字(中文会话); 段落无模板标记;
    英文字母≥15且是汉字数的4倍以上(车名/型号混排不受影响)。
    """
    seg = (segment or "").strip()
    if len(seg) < 12:
        return ""
    if not is_chinese_trigger(trigger_text):
        return ""  # 触发不是中文会话(用户在用英文聊), 不拦
    if _TRIGGER_ALLOWS_FOREIGN.search(trigger_text or ""):
        return ""  # 用户明确要英文/翻译, 放行
    if _FOREIGN_TEMPLATE_MARK.search(seg):
        return ""
    if _FOREIGN_REFERENCE_MARK.search(seg):
        return ""  # 代码/报错/URL 等引用型内容, 不按漂移丢弃
    n_latin = len(re.findall(r"[A-Za-z]", seg))
    n_han = len(re.findall(r"[\u4e00-\u9fff]", seg))
    if n_latin >= 15 and n_latin > n_han * 4:
        return f"latin={n_latin}/han={n_han}"
    return ""
