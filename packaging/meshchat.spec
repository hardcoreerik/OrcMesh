# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for MeshChat for Windows.

Build with:
    .\.venv\Scripts\pyinstaller packaging\meshchat.spec

Or use the build script:
    .\scripts\build.ps1
"""
import sys
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# SPECPATH is the directory containing this .spec file (packaging/).
# ROOT is the project root one level up.
ROOT = Path(SPECPATH).parent
SRC = str(ROOT / "src")

# ── Data files ─────────────────────────────────────────────────────────────
datas = []

# 1. Leaflet web assets (map HTML/CSS/JS and optional vendor/)
datas += [(str(ROOT / "src/meshchat/ui/map/web"), "meshchat/ui/map/web")]

# 2. Meshtastic protobuf definitions (*.proto files shipped with the package)
datas += collect_data_files("meshtastic", includes=["*.proto", "*.pyi"])

# 3. PySide6 WebEngine resources (icudtl.dat, qtwebengine_resources.pak, etc.)
#    PyInstaller's PySide6 hook usually handles these, but be explicit for safety.
try:
    import PySide6
    pyside6_dir = Path(PySide6.__file__).parent
    webengine_resources = pyside6_dir / "resources"
    if webengine_resources.exists():
        datas += [(str(webengine_resources), "PySide6/resources")]

    # QtWebEngineProcess.exe — needed by QtWebEngine for the renderer process
    renderer = pyside6_dir / "QtWebEngineProcess.exe"
    if renderer.exists():
        datas += [(str(renderer), "PySide6")]

    # WebEngine translations
    translations = pyside6_dir / "translations"
    if translations.exists():
        datas += [(str(translations), "PySide6/translations")]
except Exception:
    pass

# ── Hidden imports ─────────────────────────────────────────────────────────
hiddenimports = []

# meshtastic / protobuf
hiddenimports += collect_submodules("meshtastic")
hiddenimports += collect_submodules("google.protobuf")
hiddenimports += [
    "google.protobuf.descriptor",
    "google.protobuf.descriptor_pool",
    "google.protobuf.reflection",
    "google.protobuf.symbol_database",
    "google.protobuf.internal.decoder",
]

# BLE backend — bleak uses Windows Runtime on Win10+
hiddenimports += [
    "bleak.backends.winrt",
    "bleak.backends.winrt.client",
    "bleak.backends.winrt.scanner",
]
hiddenimports += collect_submodules("bleak")

# PyPubSub
hiddenimports += collect_submodules("pubsub")
hiddenimports += ["pubsub.core", "pubsub.utils"]

# pyqtgraph — has lazy imports
hiddenimports += collect_submodules("pyqtgraph")
hiddenimports += ["pyqtgraph.graphicsItems.PlotItem.PlotItem"]

# platformdirs
hiddenimports += ["platformdirs", "platformdirs.windows"]

# PySide6 WebEngine (sometimes not auto-discovered)
hiddenimports += [
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineQuick",
]

# Misc
hiddenimports += [
    "numpy",
    "numpy.core._multiarray_umath",
    "meshchat.app",
    "meshchat.version",
    "meshchat.controllers.meshtastic_controller",
    "meshchat.services.app_logging",
    "meshchat.services.packet_ingestor",
    "meshchat.ui.main_window",
    "meshchat.ui.map.map_widget",
    "meshchat.ui.theme",
]

# ── Analysis ───────────────────────────────────────────────────────────────
a = Analysis(
    [str(ROOT / "src/meshchat/__main__.py")],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT / "packaging/hooks")],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging/runtime_hooks/set_paths.py")],
    excludes=["tkinter"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MeshChat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # no console window in production
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging/icon.ico") if (ROOT / "packaging/icon.ico").exists() else None,
    version=str(ROOT / "packaging/version_info.txt") if (ROOT / "packaging/version_info.txt").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=["vcruntime140.dll", "python3*.dll", "Qt6*.dll"],
    name="MeshChat",
)
