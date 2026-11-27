import subprocess
import sys
from pathlib import Path


def test_snitch_registration_is_idempotent():
    subprocess.run(
        [sys.executable, "-c", """import importlib
from mohobot.services.llm_tools import registry
import plugins.snitch as snitch_mod

assert registry.contains("snitch")
snitch_mod._register()
assert registry.contains("snitch")
importlib.reload(snitch_mod)
assert registry.contains("snitch")
"""],
        cwd=Path(__file__).resolve().parent.parent,
        check=True,
    )
