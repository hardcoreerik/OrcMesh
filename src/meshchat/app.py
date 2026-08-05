"""MeshChat for Windows — application entry point."""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="meshchat", description="MeshChat for Windows")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    args, _rest = parser.parse_known_args()

    # Logging must be configured before any other import that touches logging
    from meshchat.services.app_logging import configure_logging
    configure_logging(debug=args.debug)

    # Qt must be set up before creating QApplication
    # High-DPI is automatic in Qt6; we just need the env hint on Windows
    import os
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

    # PySide6-WebEngine requires this env var on some Windows configs
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu-sandbox")

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setOrganizationName("MeshChat")
    app.setOrganizationDomain("meshtastic.org")
    app.setApplicationName("MeshChat")

    from meshchat.version import __version__
    app.setApplicationVersion(__version__)

    # Apply global stylesheet and palette
    from meshchat.ui.theme import global_stylesheet
    app.setStyleSheet(global_stylesheet())

    from meshchat.ui.main_window import MainWindow
    window = MainWindow()
    window.show()

    return app.exec()
