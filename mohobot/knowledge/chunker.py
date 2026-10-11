"""知识库切块 — markdown 按 H2 标题切, index.json 逐条成卡。

切块规则:
- 虚拟歌手/*.md 等条目文件: 文档标题取首个一级标题(兜底文件名),
  按 `## ` 二级标题切段, 段前导言单独成块; 单块正文超过
  _MAX_SECTION_CHARS 时按空行再切, 仍超长则按行硬切。
- index.json: singers/songs/producers 每条渲染为一张文本卡
  (含别名/公司/代表曲等结构化字段), 既是检索语料也是别名命中来源。
- README.md 为导航说明, 跳过不切块。

chunk_id 由 (source, title, 序号) 决定, 同一文件内容不变则 id 稳定;
文件级增量(mtime/size 变化即整文件重切块重嵌入)由 service 层处理。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

# 单块正文字符上限: 超过则按空行二次切分(仍超长按行硬切)。
# 检索结果注入 LLM 时每块再按 max_chars_per_result 截断, 这里控制粒度。
_MAX_SECTION_CHARS = 1000
# README.md 是目录导航, 对问答无增益
_SKIP_FILES = {"README.md"}


@dataclass(frozen=True)
class Chunk:
    """一个可嵌入的知识块。source 为相对知识源目录的正斜杠路径。"""

    chunk_id: str
    source: str
    title: str
    text: str


def _make_chunk_id(source: str, title: str, seq: int) -> str:
    raw = f"{source}|{title}|{seq}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _split_long_body(body: str) -> list[str]:
    """超长正文切分: 先按空行, 仍超长按行聚合到上限。"""
    if len(body) <= _MAX_SECTION_CHARS:
        return [body]
    paragraphs = [p.strip() for p in re_split_blank(body) if p.strip()]
    if len(paragraphs) > 1 and all(len(p) <= _MAX_SECTION_CHARS for p in paragraphs):
        return paragraphs
    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in body.splitlines():
        if current and current_len + len(line) + 1 > _MAX_SECTION_CHARS:
            pieces.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        pieces.append("\n".join(current))
    return [p for p in pieces if p.strip()]


def re_split_blank(text: str) -> list[str]:
    """按空行切分段落(保留其余空白原样)。"""
    return text.split("\n\n")


def chunk_markdown(path: Path, rel_path: str) -> list[Chunk]:
    """单个 markdown 文件按 H2 切块。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    doc_title = ""
    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            doc_title = stripped[2:].strip()
            break
    if not doc_title:
        doc_title = path.stem

    # 以 `## ` 为界切段; 首个 H2 之前的导言(去掉文档标题行)单独成块
    sections: list[tuple[str, list[str]]] = []  # (h2标题或"", 行列表)
    current_title = ""
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            sections.append((current_title, current))
            current_title = stripped[3:].strip()
            current = []
            continue
        current.append(line)
    sections.append((current_title, current))

    chunks: list[Chunk] = []
    seq = 0
    for h2, body_lines in sections:
        body = "\n".join(body_lines).strip()
        # 导言块里通常只有文档大标题, 无正文则跳过
        if not h2:
            body = "\n".join(
                l for l in body_lines if not l.strip().startswith("# ")
            ).strip()
        if not body:
            continue
        title = f"{doc_title} · {h2}" if h2 else doc_title
        for piece in _split_long_body(body):
            chunks.append(Chunk(
                chunk_id=_make_chunk_id(rel_path, title, seq),
                source=rel_path, title=title, text=piece,
            ))
            seq += 1
    return chunks


def _fmt_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            if v not in (None, "", [], {}):
                parts.append(f"{k} {v}")
        return " | ".join(parts)
    if isinstance(value, (list, tuple)):
        return "、".join(str(v) for v in value if v not in (None, ""))
    return str(value)


def _singer_card(entry: dict) -> str:
    lines = [f"【歌手档案】{entry.get('name', '')}"]
    if entry.get("en_name"):
        lines[-1] += f" ({entry['en_name']})"
    if entry.get("aliases"):
        lines.append(f"别名: {_fmt_value(entry['aliases'])}")
    lines.append(
        f"类别: {entry.get('category', '')} | 运营: {entry.get('company', '')}"
    )
    lines.append(
        f"引擎沿革: {_fmt_value(entry.get('engines'))}"
        f" | 首次发售: {entry.get('debut_date', '')} ({entry.get('debut_engine', '')})"
    )
    if entry.get("voice_provider"):
        lines.append(f"声源: {entry['voice_provider']}")
    if entry.get("illustrator"):
        lines.append(f"画师: {entry['illustrator']}")
    character = _fmt_value(entry.get("character"))
    if character:
        lines.append(f"角色设定: {character}")
    if entry.get("representative_songs"):
        lines.append(f"代表曲: {_fmt_value(entry['representative_songs'])}")
    if entry.get("summary"):
        lines.append(f"简介: {entry['summary']}")
    lines.append(f"详见条目: {entry.get('file', '')}")
    return "\n".join(lines)


def _song_card(entry: dict) -> str:
    lines = [f"【歌曲档案】{entry.get('title', '')}"]
    lines.append(
        f"演唱: {_fmt_value(entry.get('singers'))} | P主: {entry.get('producer', '')}"
    )
    lines.append(
        f"发布: {entry.get('date', '')} | 引擎: {entry.get('engine', '')}"
    )
    if entry.get("achievement"):
        lines.append(f"成绩: {entry['achievement']}")
    if entry.get("significance"):
        lines.append(f"意义: {entry['significance']}")
    lines.append(f"详见: {entry.get('file', '')}")
    return "\n".join(lines)


def _producer_card(entry: dict) -> str:
    lines = [f"【创作者档案】{entry.get('name', '')}"]
    lines.append(
        f"活跃期: {entry.get('active', '')} | 身份: {entry.get('role', '')}"
    )
    if entry.get("works"):
        lines.append(f"代表作: {_fmt_value(entry['works'])}")
    if entry.get("highlight"):
        lines.append(f"亮点: {entry['highlight']}")
    lines.append(f"详见: {entry.get('file', '')}")
    return "\n".join(lines)


def chunk_index_json(path: Path, rel_path: str) -> list[Chunk]:
    """index.json 逐条成卡(检索语料 + 别名来源)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []

    builders = (
        ("singers", "歌手", _singer_card),
        ("songs", "歌曲", _song_card),
        ("producers", "创作者", _producer_card),
    )
    chunks: list[Chunk] = []
    seq = 0
    for key, label, builder in builders:
        entries = data.get(key) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("title") or ""
            if not name:
                continue
            text = builder(entry)
            title = f"{label}档案 · {name}"
            chunks.append(Chunk(
                chunk_id=_make_chunk_id(rel_path, title, seq),
                source=rel_path, title=title, text=text,
            ))
            seq += 1
    return chunks


def scan_source_dir(
    source_dir: str | Path,
) -> list[tuple[str, Path, float, int]]:
    """扫描知识源文件: [(rel_path, abs_path, mtime, size)], 顺序稳定。

    只收 .md 与 index.json(与切块规则一致, README 跳过);
    供 service 做文件级增量比对。
    """
    root = Path(source_dir)
    if not root.is_dir():
        return []
    out: list[tuple[str, Path, float, int]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in _SKIP_FILES:
            continue
        if not (path.name.lower().endswith(".md") or path.name == "index.json"):
            continue
        stat = path.stat()
        out.append((path.relative_to(root).as_posix(), path, stat.st_mtime, stat.st_size))
    return out


def chunk_file(path: Path, rel_path: str) -> list[Chunk]:
    """单文件切块入口(md / index.json, 其余类型返回空)。"""
    name = path.name
    if name.lower().endswith(".md"):
        return chunk_markdown(path, rel_path)
    if name == "index.json":
        return chunk_index_json(path, rel_path)
    return []


def chunk_source_dir(source_dir: str | Path) -> list[Chunk]:
    """扫描知识源目录切块。目录不存在/为空时返回空列表。"""
    chunks: list[Chunk] = []
    for rel, path, _, _ in scan_source_dir(source_dir):
        chunks.extend(chunk_file(path, rel))
    return chunks
