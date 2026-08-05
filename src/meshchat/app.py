"""MeshChat for Windows — application entry point."""
from __future__ import annotations

import argparse
import logging
import sys
import traceback

log = logging.getLogger(__name__)


def _install_excepthook() -> None:
    """Route otherwise-unhandled exceptions to the log and a dialog.

    Release builds are windowed (console=False), so an unhandled exception
    normally kills the app with no output anywhere the user can see. Without
    this, "it just closed" is the only bug report you ever get.
    """
    def handler(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return

        log.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                box = QMessageBox()
                box.setIcon(QMessageBox.Icon.Critical)
                box.setWindowTitle("MeshChat — Unexpected Error")
                box.setText(
                    "MeshChat hit an unexpected error.\n\n"
                    "It has been written to the log file "
                    "(Help > Open Log Folder). The app may still be usable."
                )
                box.setDetailedText(detail)
                box.exec()
        except Exception:
            # A dialog failure must never mask the original exception, which
            # has already been logged above.
            log.exception("Could not display the crash dialog")

    sys.excepthook = handler


def main() -> int:
    parser = argparse.ArgumentParser(prog="meshchat", description="MeshChat for Windows")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    args, _rest = parser.parse_known_args()

    # Logging must be configured before any other import that touches logging
    from meshchat.services.app_logging import configure_logging
    configure_logging(debug=args.debug)
    _install_excepthook()

    # Qt must be set up before creating QApplication
    # High-DPI is automatic in Qt6; we just need the env hint on Windows
    import os
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

    # PySide6-WebEngine requires this env var on some Windows configs
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu-sandbox")

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
