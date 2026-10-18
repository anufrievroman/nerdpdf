"""Install nerdpdf as a macOS .app bundle that launches this Python environment."""

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

from . import __version__

BUNDLE_ID = "com.nerdpdf.nerdpdf"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")
ICNS = Path(__file__).resolve().parent / "icons" / "nerdpdf.icns"


def _info_plist():
    return {
        "CFBundleName": "nerdpdf",
        "CFBundleDisplayName": "nerdpdf",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "nerdpdf",
        "CFBundleIconFile": "nerdpdf",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "PDF document",
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "Alternate",
            "LSItemContentTypes": ["com.adobe.pdf"],
        }],
    }


def _framework_interpreter():
    """The real interpreter inside a framework build's Python.app (Homebrew, python.org), if any."""
    if not sysconfig.get_config_var("PYTHONFRAMEWORK"):
        return None
    exe = Path(sys.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    return exe if exe.exists() else None


def _launcher(macos_dir):
    """Write Contents/MacOS/nerdpdf.

    Framework Pythons re-exec into their own Python.app, so macOS would show the app as "Python".
    Running a copy of that interpreter from inside our bundle makes nerdpdf.app the main bundle
    (menu name, Dock, Finder "Open With"); __PYVENV_LAUNCHER__ points it at this environment.
    """
    interpreter = _framework_interpreter()
    if interpreter:
        shutil.copy2(interpreter, macos_dir / "python")
        script = (f"export __PYVENV_LAUNCHER__={shlex.quote(sys.executable)}\n"
                  'exec "$(dirname "$0")/python" -m nerdpdf "$@"\n')
    else:
        script = f'exec {shlex.quote(sys.executable)} -m nerdpdf "$@"\n'
    launcher = macos_dir / "nerdpdf"
    launcher.write_text("#!/bin/sh\n" + script)
    launcher.chmod(0o755)


def install_app(dest_dir):
    if sys.platform != "darwin":
        sys.exit("--install-app is only supported on macOS")
    dest = Path(dest_dir).expanduser().resolve() / "nerdpdf.app"
    if dest.exists():
        try:
            ident = plistlib.loads((dest / "Contents" / "Info.plist").read_bytes()).get("CFBundleIdentifier")
        except (OSError, plistlib.InvalidFileException):
            ident = None
        if ident != BUNDLE_ID:
            sys.exit(f"{dest} exists and is not a nerdpdf bundle; leaving it alone")
        shutil.rmtree(dest)

    contents = dest / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    shutil.copy(ICNS, contents / "Resources" / "nerdpdf.icns")
    with open(contents / "Info.plist", "wb") as f:
        plistlib.dump(_info_plist(), f)
    _launcher(contents / "MacOS")

    if os.path.exists(LSREGISTER):  # make Finder / Spotlight / "Open With" pick it up now
        subprocess.run([LSREGISTER, "-f", str(dest)], check=False)
    print(f"Installed {dest}")
    print(f"It runs {sys.executable}; reinstall if you move or recreate that environment.")
