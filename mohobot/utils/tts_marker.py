"""TTS 标注标记解析(模糊识别) — <tts>...</tts> 及其常见变体。

LLM 回复中的朗读标注标签在实际输出里常有变体, 解析端统一容错:
- 标签名别名: tts / voice / speech / say(大小写不敏感)
- 空格容忍: < tts > 、</ tts > 、< / tts >
- 括号形态: 尖括号 <> 、ASCII 方括号 [] 、中文方括号 【】
- 闭标签统一用 / 前缀: </tts> 、【/tts】 、[ / speech ]
- 只接受纯标签: 带属性(如 <tts speed="2">)不算标签, 原文保留
- 词边界安全: <ttsx> 、<tts2> 不会被误判; 正文里的 a < b 原样透传

解析语义(与旧版一致):
- 标签本身剥除, 标注内容仍显示;
- 收集第一个非空标注内容供 TTS 合成(多标注取第一个非空);
- 未闭合时内容到结尾(容错忘写闭标签);
- 游离闭标签丢弃。

系统提示词仍只引导 <tts></tts> 规范形态, 模糊识别是兜底。
"""

from __future__ import annotations

import re

# 句末边界(超长标注截断用): 读到第一个句末标点为止
_SENTENCE_BOUNDARY = "。！？!?…\n"

# 标签匹配: 三种括号家族 × 可选 / 闭前缀 × 别名(大小写不敏感), 纯标签无属性
_TAG_RE = re.compile(
    r"<\s*/?\s*(?:tts|voice|speech|say)\s*>"
    r"|\[\s*/?\s*(?:tts|voice|speech|say)\s*\]"
    r"|【\s*/?\s*(?:tts|voice|speech|say)\s*】",
    re.IGNORECASE,
)
_OPEN_DELIMS = ("<", "[", "【")
_CLOSE_DELIM = {"<": ">", "[": "]", "【": "】"}
# 纯标签最长 </ speech > = 11 字符; 扣留超过该长度仍未成标签 → 按正文放行
_MAX_TAG_HOLD = 12


def _is_close_tag(tag_text: str) -> bool:
    """/ 前缀 = 闭标签(纯标签内不会有其它 /)。"""
    return "/" in tag_text


def _first_delim(text: str) -> int | None:
    """第一个疑似标签起始定界符的位置; 无则 None。"""
    idxs = [i for i in (text.find(d) for d in _OPEN_DELIMS) if i != -1]
    return min(idxs) if idxs else None


def _held_is_decided(held: str) -> bool:
    """以定界符开头的扣留段是否已可判定"不是标签"。

    - 出现了本族闭定界符(如 < ... > 中间没有合法标签名) → 已失败
    - 长度超过纯标签上限仍不成标签 → 已失败
    """
    closer = _CLOSE_DELIM[held[0]]
    if closer in held[1:]:
        return True
    return len(held) > _MAX_TAG_HOLD


def normalize_tts_content(content: str, max_chars: int = 20) -> str:
    """规范化朗读文本: 去首尾空白; 超过 max_chars 时读到第一个句末标点,
    无句末标点则硬截到 max_chars(防止无标点长文本无限朗读)。
    """
    text = content.strip()
    if len(text) <= max_chars:
        return text
    for i, ch in enumerate(text):
        if ch in _SENTENCE_BOUNDARY:
            cut = i + 1
            return text[:cut].strip() if cut > 0 else text
    return text[:max_chars]


def _pick_tts_text(spans: list[str], max_chars: int) -> str:
    """第一个非空标注(空标注如 < tts >  </ tts > 跳过, 取后面真正的标注)。"""
    for s in spans:
        norm = normalize_tts_content(s, max_chars)
        if norm:
            return norm
    return ""


def strip_and_extract(text: str, max_chars: int = 20) -> tuple[str, str]:
    """非流式全文处理: 剥除所有可识别标签(含变体)。

    返回 (显示文本, 朗读文本) — 显示文本=剥掉标签后的全文(标注内容仍显示),
    朗读文本取第一个非空标注并 normalize, 无标注时为空串。
    """
    display: list[str] = []
    spans: list[str] = []
    pos = 0
    span_start = -1
    in_tts = False
    for m in _TAG_RE.finditer(text):
        tag = m.group(0)
        if in_tts:
            if _is_close_tag(tag):
                spans.append(text[span_start:m.start()])
                in_tts = False
            else:
                # 嵌套开标签: 当作普通内容原样显示
                display.append(text[pos:m.end()])
                pos = m.end()
                continue
            display.append(text[pos:m.start()])
            pos = m.end()
        else:
            display.append(text[pos:m.start()])
            pos = m.end()
            if not _is_close_tag(tag):
                in_tts = True
                span_start = m.end()
            # 游离闭标签: 丢弃(不显示)
    display.append(text[pos:])
    if in_tts:
        spans.append(text[span_start:])
    return "".join(display), _pick_tts_text(spans, max_chars)


class TTSMarkerFilter:
    """流式标签过滤器(模糊识别)。

    用法:
        f = TTSMarkerFilter(max_chars=20)
        for chunk in stream:
            display = f.feed(chunk)   # 可安全发出的显示文本(可能为空)
        rest, tts_text = f.finish()   # 流结束: 剩余显示文本 + 朗读文本

    状态机: text(正文) / tts(标注内容内)。
    遇到疑似起始定界符(<、[、【)时扣留待判定; 判定失败按原样放行
    (不丢字符), 判定为标签则剥除。扣留有界(纯标签 ≤ 12 字符)。
    """

    def __init__(self, max_chars: int = 20):
        self._max_chars = max_chars
        self._buf = ""
        self._spans: list[str] = []      # 已闭合的完整标注
        self._cur_span: list[str] = []   # 当前(未闭合)标注的内容分片累积
        self._in_tts = False

    def feed(self, chunk: str) -> str:
        """喂入一个流式 chunk, 返回当前可安全发出的显示文本。"""
        if chunk:
            self._buf += chunk
        return self._drain(final=False)

    def finish(self) -> tuple[str, str]:
        """流结束: 冲刷缓冲, 返回 (剩余显示文本, 朗读文本)。

        标签未闭合时视为"内容到结尾"(LLM 忘写闭标签的容错),
        内容同样计入朗读文本且仍显示; 扣留中的疑似标签按正文放行。
        """
        out = self._drain(final=True)
        return out, _pick_tts_text(self._spans, self._max_chars)

    # ── 内部 ─────────────────────────────────────────────────

    def _emit_content(self, out: list[str], piece: str) -> None:
        """标注内容同时进显示流和朗读累积。"""
        if piece:
            out.append(piece)
            self._cur_span.append(piece)

    def _close_span(self) -> None:
        if self._in_tts:
            self._spans.append("".join(self._cur_span))
            self._cur_span = []
            self._in_tts = False

    def _drain(self, final: bool) -> str:
        out: list[str] = []
        while True:
            if not self._buf:
                if final and self._in_tts:
                    self._close_span()
                break
            m = _TAG_RE.search(self._buf)
            if self._in_tts:
                if m and _is_close_tag(m.group(0)):
                    # 闭标签: 内容收口, 回到正文态
                    self._emit_content(out, self._buf[:m.start()])
                    self._close_span()
                    self._buf = self._buf[m.end():]
                    continue
                if m:
                    # 嵌套开标签: 当作普通内容
                    self._emit_content(out, self._buf[:m.end()])
                    self._buf = self._buf[m.end():]
                    continue
                k = _first_delim(self._buf)
                if k is None:
                    # 无定界符 → 全部是内容, 直接发
                    self._emit_content(out, self._buf)
                    self._buf = ""
                    continue
                if k > 0:
                    self._emit_content(out, self._buf[:k])
                    self._buf = self._buf[k:]
                if final:
                    self._emit_content(out, self._buf)
                    self._close_span()
                    self._buf = ""
                    break
                if _held_is_decided(self._buf):
                    # 判定不是标签 → 整段是内容; 但段内可能有别的定界符, 从那里重试
                    k2 = _first_delim(self._buf[1:])
                    if k2 is not None:
                        self._emit_content(out, self._buf[:1 + k2])
                        self._buf = self._buf[1 + k2:]
                        continue
                    self._emit_content(out, self._buf)
                    self._buf = ""
                    continue
                break  # 扣留等待更多数据
            # ── 正文态 ──
            if m:
                out.append(self._buf[:m.start()])
                self._buf = self._buf[m.end():]
                if _is_close_tag(m.group(0)):
                    continue  # 游离闭标签: 丢弃
                self._in_tts = True
                continue
            k = _first_delim(self._buf)
            if k is None:
                out.append(self._buf)
                self._buf = ""
                break
            out.append(self._buf[:k])
            self._buf = self._buf[k:]
            if final:
                out.append(self._buf)
                self._buf = ""
                break
            if _held_is_decided(self._buf):
                # 判定不是标签 → 按正文放行; 段内可能有别的定界符, 从那里重试
                k2 = _first_delim(self._buf[1:])
                if k2 is not None:
                    out.append(self._buf[:1 + k2])
                    self._buf = self._buf[1 + k2:]
                    continue
                out.append(self._buf)
                self._buf = ""
                continue
            break  # 扣留等待更多数据
        return "".join(out)
