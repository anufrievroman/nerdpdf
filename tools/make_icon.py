"""Generate the app icon: a periodic-table tile for the element "Pdf" (Nerd), version as atomic number.

Writes nerdpdf/icons/nerdpdf.svg, nerdpdf.png (1024 px) and, on macOS, nerdpdf.icns.
Run with: .venv/bin/python tools/make_icon.py
"""

import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "nerdpdf" / "icons"
FONT = "Helvetica Neue, Helvetica, Arial, sans-serif"


def version():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def svg():
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0A84FF"/><stop offset="1" stop-color="#5E5CE6"/>
    </linearGradient>
  </defs>
  <rect x="100" y="110" width="824" height="824" rx="185" fill="#000" opacity="0.2"/>
  <rect x="100" y="100" width="824" height="824" rx="185" fill="url(#tile)"/>
  <rect x="134" y="134" width="756" height="756" rx="155" fill="none"
        stroke="#FFFFFF" stroke-opacity="0.35" stroke-width="6"/>
  <g fill="#FFFFFF" font-family="{FONT}">
    <text x="200" y="290" font-size="110" font-weight="500">v{version()}</text>
    <text x="512" y="650" font-size="360" font-weight="700" text-anchor="middle"
          letter-spacing="-8">Pdf</text>
    <text x="512" y="790" font-size="104" font-weight="400" text-anchor="middle"
          fill-opacity="0.9">Nerd</text>
  </g>
</svg>
"""


def render(svg_text, size):
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    QSvgRenderer(QByteArray(svg_text.encode())).render(p)
    p.end()
    return img


def main():
    app = QGuiApplication(sys.argv)  # noqa: F841  Qt needs an application object to paint
    OUT.mkdir(parents=True, exist_ok=True)
    text = svg()
    (OUT / "nerdpdf.svg").write_text(text)
    render(text, 1024).save(str(OUT / "nerdpdf.png"))
    if shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "nerdpdf.iconset"
            iconset.mkdir()
            for s in (16, 32, 128, 256, 512):
                render(text, s).save(str(iconset / f"icon_{s}x{s}.png"))
                render(text, s * 2).save(str(iconset / f"icon_{s}x{s}@2x.png"))
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT / "nerdpdf.icns")],
                           check=True)
    print("wrote", *sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
