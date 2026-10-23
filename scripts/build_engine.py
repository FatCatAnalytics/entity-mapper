"""Package the Python engine for the desktop app: dist-engine/engine/engine(.exe).

    python scripts/build_engine.py

Uses the project's .venv if there is one. Needs web/dist (npm run build:web) and PyInstaller.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    venv = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    python = str(venv) if venv.exists() else sys.executable
    if not (ROOT / "web" / "dist" / "index.html").exists():
        print("web/dist is missing: run `npm run build:web` first.")
        return 1
    out = ROOT / "dist-engine"
    shutil.rmtree(out / "engine", ignore_errors=True)
    code = subprocess.call([python, "-m", "PyInstaller", "engine.spec", "--noconfirm",
                            "--distpath", str(out), "--workpath", str(ROOT / "build" / "engine")], cwd=ROOT)
    if code == 0:
        exe = out / "engine" / ("engine.exe" if sys.platform == "win32" else "engine")
        size = sum(f.stat().st_size for f in (out / "engine").rglob("*") if f.is_file()) / 1e6
        print(f"Built {exe} ({size:.0f} MB in the folder)")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
