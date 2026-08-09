"""跑全部回归测试(除 concurrency, 单独跑)。"""
import asyncio
import glob
import importlib.util
import sys
import traceback

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

    async def run(mod=mod):
        for fn in sorted(dir(mod)):
            if not fn.startswith("test_"):
                continue
            target = getattr(mod, fn)
            if asyncio.iscoroutinefunction(target):
                await target()
            else:
                target()

    try:
        asyncio.run(run())
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
