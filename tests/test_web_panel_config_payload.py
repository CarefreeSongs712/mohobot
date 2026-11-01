"""面板全局配置保存前端的静态回归：payload 的每个键都必须有对应渲染点。

背景（PR #106 / 配置三方审计）：
`saveGlobalConfig()` 曾发送一个前端从未渲染的键 `usage_excluded_models`，
后端 `PUT /api/config` 白名单照单全收，导致每次点"保存全局配置"就把
`global.yaml` 里手工配置的排除名单静默清空。

本测试固化该不变量：**saveGlobalConfig 发送的每个叶子键，都必须在面板
HTML 里有对应的 `cfg-<key>` 输入控件**。若以后有人新增发送键但忘记加 UI
（或删除 UI 但保留发送），测试会失败，避免再次静默覆盖配置。

纯静态解析 index.html，不启动服务、不依赖浏览器。
"""
from __future__ import annotations

import re
from pathlib import Path

_HTML_PATH = Path(__file__).resolve().parent.parent / "mohobot" / "web_panel" / "static" / "index.html"

# formField/checkField/selectField/textareaField(name) 统一生成 id="cfg-<name>"
_FORM_HELPERS = ("formField", "checkField", "selectField", "textareaField")
# num(id, default) 直接使用传入的完整 id
_NUM_HELPER = "num"


def _extract_save_global_payload(html: str) -> str:
    """截取 saveGlobalConfig() 中 `const data = {...}` 的对象字面量文本。"""
    fn = html.find("async function saveGlobalConfig")
    assert fn != -1, "index.html 中找不到 saveGlobalConfig()"
    start = html.find("const data = {", fn)
    assert start != -1, "saveGlobalConfig() 中找不到 const data = {...}"
    start = html.find("{", start)
    depth = 0
    for i in range(start, len(html)):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
    raise AssertionError("saveGlobalConfig payload 对象未闭合")


def _payload_leaf_keys(payload: str) -> list[str]:
    """按缩进栈解析对象字面量的叶子键路径（如 reply.segment_min_len）。

    payload 为源码文本；忽略注释行。值为 `{` 的键视为嵌套容器，向下展开。
    """
    leaves: list[str] = []
    stack: list[tuple[int, str]] = []
    for line in payload.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped in ("{", "}"):
            continue
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        indent, key, rest = len(m.group(1)), m.group(2), m.group(3).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([k for _, k in stack] + [key])
        if rest.startswith("{"):
            stack.append((indent, key))
            continue
        leaves.append(path)
    return leaves


def _rendered_cfg_ids(html: str) -> set[str]:
    """收集面板全部会渲染出的 cfg 输入控件 id。"""
    ids: set[str] = set()
    for helper in _FORM_HELPERS:
        for name in re.findall(
            rf"{helper}\(\s*['\"]([^'\"]+)['\"]", html
        ):
            ids.add(f"cfg-{name}")
    for raw_id in re.findall(rf"{_NUM_HELPER}\(\s*['\"]([^'\"]+)['\"]", html):
        # num('cfg-reply.segment_min_len', 12) 直接传完整 id；
        # 也兼容 num('reply.segment_min_len', 12) 的写法。
        ids.add(raw_id if raw_id.startswith("cfg-") else f"cfg-{raw_id}")
    return ids


def test_save_global_config_payload_keys_have_render_points() -> None:
    html = _HTML_PATH.read_text(encoding="utf-8")
    payload = _extract_save_global_payload(html)
    leaves = _payload_leaf_keys(payload)
    assert len(leaves) >= 20, f"payload 解析异常（仅 {len(leaves)} 个键）"

    rendered = _rendered_cfg_ids(html)
    missing = [k for k in leaves if f"cfg-{k}" not in rendered]
    assert not missing, (
        "saveGlobalConfig 发送了没有对应 DOM 渲染点的键，会在每次保存时"
        f"静默覆盖配置文件原值（PR #106 同类问题）: {missing}"
    )
    # 明确钉住 #106：若将来恢复发送 usage_excluded_models，必须先补 UI 输入项。
    if "usage_excluded_models" in leaves:
        assert "cfg-usage_excluded_models" in rendered, (
            "usage_excluded_models 被恢复进 saveGlobalConfig payload，但面板没有"
            " cfg-usage_excluded_models 输入项——会重新引入 #106 的静默清空 bug"
        )


if __name__ == "__main__":
    test_save_global_config_payload_keys_have_render_points()
    print("[CFG1] 全局配置 payload 键均有渲染点 OK")
