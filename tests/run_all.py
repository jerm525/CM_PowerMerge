"""Minimal runner so tests work without pytest: python tests/run_all.py (pytest also works)."""
import importlib
import pathlib
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
failed = 0
for path in sorted(pathlib.Path(__file__).parent.glob("test_*.py")):
    mod = importlib.import_module(path.stem)
    for name in dir(mod):
        if name.startswith("test_") and callable(getattr(mod, name)):
            try:
                getattr(mod, name)()
                print(f"PASS {path.stem}.{name}")
            except Exception:
                failed += 1
                print(f"FAIL {path.stem}.{name}")
                traceback.print_exc()
print(f"\n{'ALL PASSED' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
