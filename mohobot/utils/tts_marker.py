"""TTS 标注标记的流式解析(<tts>...</tts>).

LLM 回复中用 <tts></tts> 标注一句需要朗读的话。过滤器做三件事:
1. 显示流剥除标签本身(标注内容仍显示);
2. 收集标注内容供 TTS 合成(多标注取第一个);
3. 流式场景下防止标签跨 chunk 撕裂 — 缓冲区尾部可能是半截标签时扣留不发。

非流式路径用模块级 strip_and_extract() 一次性处理。
"""

from __future__ import annotations

# 句末边界(超长标注截断用): 读到第一个句末标点为止
_SENTENCE_BOUNDARY = "。！？!?…\n"


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


def strip_and_extract(text: str, max_chars: int = 20) -> tuple[str, str]:
    """非流式全文处理: 剥除所有 <tts>/</tts> 标签。

    返回 (显示文本, 朗读文本) — 显示文本=剥掉标签后的全文(标注内容仍显示),
    朗读文本取第一个标注并 normalize, 无标注时为空串。
    """
    display_parts: list[str] = []
    spans: list[str] = []
    rest = text
    while True:
        idx = rest.find("<tts>")
        if idx == -1:
            display_parts.append(rest.replace("</tts>", ""))
            break
        display_parts.append(rest[:idx])
        after_open = rest[idx + len("<tts>"):]
        close_idx = after_open.find("</tts>")
        if close_idx == -1:
            # 未闭合: 内容到结尾
            spans.append(after_open)
            display_parts.append(after_open)
            rest = ""
            break
        content = after_open[:close_idx]
        spans.append(content)
        display_parts.append(content)  # 标注内容仍显示, 只剥标签
        rest = after_open[close_idx + len("</tts>"):]
    tts_text = normalize_tts_content(spans[0], max_chars) if spans else ""
    return "".join(display_parts), tts_text


class TTSMarkerFilter:
    """流式 <tts> 标记过滤器。

    用法:
        f = TTSMarkerFilter(max_chars=20)
        for chunk in stream:
            display = f.feed(chunk)   # 可安全发出的显示文本(可能为空)
        rest, tts_text = f.finish()   # 流结束: 剩余显示文本 + 朗读文本
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
        内容同样计入朗读文本且仍显示。
        """
        out = self._drain(final=True)
        tts_text = normalize_tts_content(self._spans[0], self._max_chars) if self._spans else ""
        return out, tts_text

    # ── 内部 ─────────────────────────────────────────────────

    def _drain(self, final: bool) -> str:
        out: list[str] = []
        while True:
            if self._in_tts:
                close_idx = self._buf.find("</tts>")
                if close_idx != -1:
                    content = self._buf[:close_idx]
                    self._cur_span.append(content)
                    self._spans.append("".join(self._cur_span))
                    self._cur_span = []
                    out.append(content)  # 标注内容仍显示, 只剥标签
                    self._buf = self._buf[close_idx + len("</tts>"):]
                    self._in_tts = False
                    continue
                if final:
                    self._cur_span.append(self._buf)
                    self._spans.append("".join(self._cur_span))
                    self._cur_span = []
                    out.append(self._buf)
                    self._buf = ""
                    break
                # 标注内容照常进显示流, 同时累积给 TTS; 尾部可能是半截闭标签 → 扣留
                keep = self._partial_tag_len(self._buf, "</tts>")
                emit_len = len(self._buf) - keep
                if emit_len > 0:
                    piece = self._buf[:emit_len]
                    out.append(piece)
                    self._cur_span.append(piece)
                    self._buf = self._buf[emit_len:]
                break
            # 普通文本态
            open_idx = self._buf.find("<tts>")
            if open_idx != -1:
                out.append(self._buf[:open_idx])
                self._buf = self._buf[open_idx + len("<tts>"):]
                self._in_tts = True
                continue
            if final:
                out.append(self._buf)
                self._buf = ""
                break
            # 尾部可能是半截 <tts> 开标签 → 扣留等下一个 chunk
            keep = self._partial_tag_len(self._buf, "<tts>")
            emit_len = len(self._buf) - keep
            if emit_len > 0:
                out.append(self._buf[:emit_len])
                self._buf = self._buf[emit_len:]
            break
        return "".join(out)

    @staticmethod
    def _partial_tag_len(buf: str, tag: str) -> int:
        """buf 尾部与 tag 前缀匹配的最长长度(半截标签扣留量, 不含完整 tag)。"""
        max_k = min(len(tag) - 1, len(buf))
        for k in range(max_k, 0, -1):
            if buf[-k:] == tag[:k]:
                return k
        return 0
