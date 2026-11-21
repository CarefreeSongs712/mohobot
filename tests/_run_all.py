"""跑全部回归测试(除 concurrency, 单独跑)。"""
import asyncio
import glob
import importlib.util
import inspect
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

failed = []
passed = 0
for f in sorted(glob.glob("tests/test_*.py")):
    if "concurrency" in f:
        continue
    name = f.replace("/", ".").replace("\\", ".").replace(".py", "")
    spec = importlib.util.spec_from_file_location(name, f)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)

    try:
        for fn in sorted(dir(mod)):
            if not fn.startswith("test_"):
                continue
            target = getattr(mod, fn)
            params = inspect.signature(target).parameters
            kwargs = {}
            temp_dir = None
            if params:
                if set(params) == {"tmp_path"}:
                    temp_dir = tempfile.mkdtemp(prefix="mohobot-test-")
                    kwargs["tmp_path"] = Path(temp_dir)
                else:
                    raise TypeError(f"{fn} 使用了 runner 不支持的 fixture: {sorted(params)}")
            try:
                if asyncio.iscoroutinefunction(target):
                    asyncio.run(target(**kwargs))
                else:
                    target(**kwargs)
            finally:
                if temp_dir:
                    shutil.rmtree(temp_dir, ignore_errors=True)
        passed += 1
        print(f"PASS {f}")
    except Exception as e:
        failed.append((f, e))
        print(f"FAIL {f}: {e}")
        traceback.print_exc()

print("---")
print(f"{passed} passed, {len(failed)} failed")
if failed:
    sys.exit(1)
