"""Build a single-file NetDoctor executable with PyInstaller.

  uv run --with pyinstaller python scripts/build.py

Output: dist/netdoctor (or netdoctor.exe). Signing/notarising is done separately
with your own certificates (codesign / signtool).
"""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sep = ";" if sys.platform == "win32" else ":"
static = root / "netdoctor" / "app" / "static"
subprocess.run([
    sys.executable, "-m", "PyInstaller", "--onefile", "--name", "netdoctor", "--clean", "--noconfirm",
    "--add-data", f"{static}{sep}netdoctor/app/static",
    "--collect-submodules", "dns", "--collect-submodules", "netdoctor",
    str(root / "netdoctor" / "__main__.py"),
], check=True, cwd=root)
print("Built:", *(root / "dist").iterdir())
