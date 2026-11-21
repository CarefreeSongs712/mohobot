import importlib.util
from pathlib import Path


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "migrate_external_plugins.py"
    spec = importlib.util.spec_from_file_location("migrate_external_plugins", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_rewrite_imports_only_rewrites_real_import_lines(tmp_path: Path) -> None:
    module = _load_script()
    source = tmp_path / "plugin.py"
    source.write_text(
        "from mohobot.file_store import json_read\n"
        "# from mohobot.file_store import keep_me\n"
        "TEXT = 'from mohobot.file_store import not_an_import'\n"
        "def f():\n"
        "    from mohobot.utils.time_utils import now\n",
        encoding="utf-8",
    )
    module._rewrite_imports(tmp_path)
    text = source.read_text(encoding="utf-8")
    assert text.count("from mohobot.api import json_read") == 1
    assert "from mohobot.api import now" in text
    assert "# from mohobot.file_store import keep_me" in text
    assert "TEXT = 'from mohobot.file_store import not_an_import'" in text
