"""config/global.example.yaml 的静态回归：重复键与关键字段类型。

背景（P3 配置审计）：
示例配置曾在「全局标量(节选)」块和「用量统计排除的模型」专用块各写了一次
`usage_excluded_models`。PyYAML 对重复映射键静默采用后者，前一处成为死配置，
且不同解析器行为不一致（有的报错、有的取第一个）。

本测试用自定义 SafeLoader 在构造映射时检测重复键，复用 PyYAML 的节点结构，
不依赖额外依赖；同时钉住若干关键顶层字段的类型，防止示例配置被改坏。

只读文件，不启动服务、不写任何数据。
"""
from __future__ import annotations

from pathlib import Path

import yaml

_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "global.example.yaml"
)


class _DuplicateKeyLoader(yaml.SafeLoader):
    """SafeLoader 子类：记录每个映射中重复出现的键（含行号）。"""

    def __init__(self, stream):
        super().__init__(stream)
        self.duplicates: list[tuple[str, int]] = []

    def construct_mapping(self, node, deep: bool = False):
        seen: set[object] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                self.duplicates.append((str(key), key_node.start_mark.line + 1))
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _load_with_duplicate_detection(text: str) -> tuple[object, list[tuple[str, int]]]:
    loader = _DuplicateKeyLoader(text)
    try:
        data = loader.get_single_data()
        duplicates = list(loader.duplicates)
    finally:
        loader.dispose()
    return data, duplicates


def test_no_duplicate_keys() -> None:
    text = _CONFIG_PATH.read_text(encoding="utf-8")
    _data, duplicates = _load_with_duplicate_detection(text)
    assert not duplicates, (
        "config/global.example.yaml 存在重复映射键（PyYAML 会静默取后一个，"
        f"前面的配置成为死配置）: {duplicates}"
    )
    # 正常解析路径也必须成功，避免自定义 loader 掩盖语法问题。
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict), "yaml.safe_load 未返回顶层映射"


def test_key_types() -> None:
    data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))

    assert isinstance(data["server"]["access_token"], str), "server.access_token 应为 str"
    assert isinstance(data["llm"]["stable_system_prefix"], bool), (
        "llm.stable_system_prefix 应为 bool"
    )
    assert isinstance(data["tts"], dict), "tts 应为 dict"

    news = data["news"]
    assert isinstance(news, dict), "news 应为 dict"
    for key in (
        "collector_enabled",
        "collector_uids",
        "collector_interval_seconds",
        "collector_max_items_per_uid",
        "collector_ttl_days",
    ):
        assert key in news, f"news.{key} 缺失"

    assert isinstance(data["usage_excluded_models"], list), (
        "usage_excluded_models 应为 list"
    )
    assert data["usage_excluded_models"] == ["Qwen3-8B"], (
        "usage_excluded_models 应保留专用块的默认值 ['Qwen3-8B']"
    )


if __name__ == "__main__":
    test_no_duplicate_keys()
    test_key_types()
    print("OK: config/global.example.yaml 无重复键且关键字段类型正确")
