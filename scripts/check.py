#!/usr/bin/env python3
"""Pre-deploy check: every template compiles, every module imports. Exit 1 on any failure."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jinja2 import Environment, FileSystemLoader
tdir = Path(__file__).resolve().parent.parent / "app" / "templates"
env = Environment(loader=FileSystemLoader(str(tdir)))
bad = 0
for t in sorted(tdir.glob("*.html")):
    try:
        env.get_template(t.name)
    except Exception as e:  # noqa: BLE001
        print(f"TEMPLATE {t.name}: {e}"); bad += 1
try:
    import app.main  # noqa: F401
except Exception as e:  # noqa: BLE001
    print(f"IMPORT app.main: {e}"); bad += 1
print("check: ok" if not bad else f"check: {bad} problem(s)")
sys.exit(1 if bad else 0)
