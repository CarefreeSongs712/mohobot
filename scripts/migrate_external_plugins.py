from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

EXTERNAL_PLUGINS = {
    "anysearch": ("搜索", "AnySearch 网页搜索工具", "MIT", ["search"]),
    "beszel_servers": ("Beszel 监控", "Beszel 服务器状态卡片", "MIT", ["monitor"]),
    "bilibiliparse": ("B 站解析", "Bilibili 视频/动态链接解析", "MIT", ["bilibili"]),
    "divination": ("占卜", "每日占卜与运势", "MIT", ["fun"]),
    "meme_stealer": ("表情包小偷", "表情包收集、检索与发送", "AGPL-3.0", ["meme"]),
    "neteasemusic": ("网易云点歌", "网易云音乐搜索与点歌", "MIT", ["music"]),
    "proactive_chat": ("主动聊天", "私聊主动发起对话", "AGPL-3.0", ["chat"]),
    "qzone": ("QQ 空间", "QQ 空间发布与浏览", "MIT", ["qzone"]),
    "six66": ("六六六", "群聊随机复读 6/66/666", "MIT", ["fun"]),
    "wifepicker": ("抽老婆", "群聊抽老婆互动", "MIT", ["fun"]),
}

IMPORT_REWRITES = {
    "from mohobot.file_store import": "from mohobot.api import",
    "from mohobot.models.onebot import": "from mohobot.api import",
    "from mohobot.history_dedup import": "from mohobot.api import",
    "from mohobot.utils.time_utils import": "from mohobot.api import",
    "from mohobot.utils.cq_code import": "from mohobot.api import",
    "from mohobot.utils.llm_diagnostic import": "from mohobot.api import",
    "from mohobot.utils.status_card import": "from mohobot.api import",
    "from mohobot.utils.image_card import": "from mohobot.api import",
    "from mohobot.services.llm_tools import": "from mohobot.api import",
    "from mohobot.ban import": "from mohobot.api import",
    "from mohobot.ban.store import": "from mohobot.api import",
}

CI = """name: ci
on:
  push:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install ruff pytest pyyaml
      - run: ruff check --select E9,F63,F7 .
      - run: python -m compileall -q .
      - run: python -m pytest -q tests
"""


def _copy_source(source: Path, target: Path) -> None:
    target.mkdir(parents=True)
    if source.is_file():
        shutil.copy2(source, target / "main.py")
        return
    for item in source.iterdir():
        if item.name in {"__pycache__", "tests", ".git", "dist"}:
            continue
        dest = target / item.name
        if item.is_dir():
            shutil.copytree(item, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, dest)


def _rewrite_imports(root: Path) -> None:
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for old, new in IMPORT_REWRITES.items():
            text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")


def _write_support_files(root: Path, name: str, owner: str) -> None:
    display_name, description, license_name, tags = EXTERNAL_PLUGINS[name]
    repo = f"https://github.com/{owner}/mohobot-plugin-{name}"
    (root / "metadata.yaml").write_text(
        f"name: {name}\nauthor: {owner}\ndisplay_name: {display_name}\n"
        f"version: 1.0.0\ndescription: {description}\nrepo: {repo}\n"
        f"license: {license_name}\napi_version: 1\nmohobot_version: \">=0.1.0\"\n"
        f"tags: [{', '.join(tags)}]\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        f"# mohobot-plugin-{name}\n\n{description}\n\n"
        f"## 安装\n\n在 Mohobot 面板「插件市场」安装，或把仓库内容放到 `data/plugins/{name}/`。\n\n"
        "## 开发\n\n```bash\npython -m compileall -q .\npython -m pytest -q tests\n```\n",
        encoding="utf-8",
    )
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(CI, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_manifest.py").write_text(
        "from pathlib import Path\n\nimport yaml\n\n\n"
        "def test_manifest() -> None:\n"
        "    root = Path(__file__).parents[1]\n"
        "    metadata = yaml.safe_load((root / 'metadata.yaml').read_text(encoding='utf-8'))\n"
        f"    assert metadata['name'] == '{name}'\n"
        "    assert metadata['api_version'] == 1\n"
        "    assert (root / 'main.py').is_file()\n",
        encoding="utf-8",
    )
    license_path = root / "LICENSE"
    if not license_path.exists():
        if license_name == "AGPL-3.0":
            shutil.copy2(Path("plugins/meme_stealer/LICENSE"), license_path)
        else:
            shutil.copy2(Path("LICENSE"), license_path)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def build(main_repo: Path, output: Path, name: str, owner: str) -> Path:
    source = main_repo / "plugins" / f"{name}.py"
    if not source.exists():
        source = main_repo / "plugins" / name
    if not source.exists():
        raise SystemExit(f"插件不存在: {name}")
    repo = output / f"mohobot-plugin-{name}"
    if repo.exists():
        raise SystemExit(f"目标已存在: {repo}")
    _copy_source(source, repo)
    _rewrite_imports(repo)
    _write_support_files(repo, name, owner)
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=CarefreeSongs712", "-c", "user.email=carefreesongs@163.com", "add", ".")
    _git(repo, "-c", "user.name=CarefreeSongs712", "-c", "user.email=carefreesongs@163.com", "commit", "-qm", f"init {name} plugin")
    return repo


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("plugins", nargs="*", choices=sorted(EXTERNAL_PLUGINS), default=None)
    parser.add_argument("--main-repo", default=".")
    parser.add_argument("--output", default="research/plugin-repos")
    parser.add_argument("--owner", default="CarefreeSongs712")
    args = parser.parse_args()
    names = args.plugins or sorted(EXTERNAL_PLUGINS)
    main_repo = Path(args.main_repo).resolve()
    output = (main_repo / args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in names:
        repo = build(main_repo, output, name, args.owner)
        print(repo)


if __name__ == "__main__":
    main()
