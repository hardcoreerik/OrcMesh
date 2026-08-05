"""
PyInstaller runtime hook — set environment variables required by QtWebEngine
before any Qt code is imported.
"""
import os
import sys

# Tell QtWebEngine where to find its helper process and resources.
# When frozen, sys._MEIPASS is the directory containing all bundled files.
_base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
_pyside6 = os.path.join(_base, "PySide6")

if os.path.isdir(_pyside6):
    os.environ.setdefault("QTWEBENGINEPROCESS_PATH",
                          os.path.join(_pyside6, "QtWebEngineProcess.exe"))

# Suppress GPU sandbox issues in production builds
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu-sandbox --no-sandbox")

# High-DPI — Qt6 auto-handles this but the env hint prevents some edge-case blurriness
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
