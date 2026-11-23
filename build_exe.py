#!/usr/bin/env python3
"""
Builds VastRenderHub.exe (100% pure native Windows desktop app via CustomTkinter + Pillow)
using PyInstaller with a generated multi-size app icon.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
ASSETS_DIR = ROOT / "assets"
ICON_PATH = ASSETS_DIR / "app_icon.ico"
DIST_EXE = ROOT / "dist" / "VastRenderHub.exe"
OUT_EXE = ROOT / "VastRenderHub.exe"


def generate_icon(icon_path: Path):
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([8, 8, size - 8, size - 8], radius=54, fill=(99, 102, 241, 255))
    draw.rounded_rectangle([18, 18, size - 18, size - 18], radius=46, fill=(24, 24, 48, 255))

    bolt = [
        (146, 40),
        (78, 138),
        (124, 138),
        (106, 216),
        (182, 114),
        (134, 114),
    ]
    draw.polygon(bolt, fill=(168, 85, 247, 255))
    draw.polygon(
        [(x + 2, y + 2) if i % 2 == 0 else (x, y) for i, (x, y) in enumerate(bolt)],
        fill=(244, 244, 255, 255),
    )

    img.save(icon_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"[✓] Generated icon: {icon_path}")


def main():
    generate_icon(ICON_PATH)

    sep = ";" if sys.platform.startswith("win") else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",
        "--name", "VastRenderHub",
        "--icon", str(ICON_PATH),
        "--add-data", f"{ICON_PATH}{sep}assets",
        "--collect-data", "customtkinter",
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "desktop_app",
        str(ROOT / "receiver.py"),
    ]
    print("[*] Running PyInstaller:", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(ROOT))
    if res.returncode != 0:
        print(f"[!] PyInstaller failed with exit code {res.returncode}")
        sys.exit(res.returncode)

    if not DIST_EXE.is_file():
        print(f"[!] Expected output missing: {DIST_EXE}")
        sys.exit(1)

    shutil.copy2(DIST_EXE, OUT_EXE)
    size_mb = OUT_EXE.stat().st_size / (1024 * 1024)
    print(f"[✓] Built {OUT_EXE} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
