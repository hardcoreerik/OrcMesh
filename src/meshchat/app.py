"""OrcMesh — application entry point."""
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
                box.setWindowTitle("OrcMesh — Unexpected Error")
                box.setText(
                    "OrcMesh hit an unexpected error.\n\n"
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
    parser = argparse.ArgumentParser(
        prog="orcmesh", description="OrcMesh — LoRa Mesh Operations Console"
    )
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    parser.add_argument(
        "--version", action="store_true",
        help="Print the OrcMesh and dependency versions and exit",
    )
    args, _rest = parser.parse_known_args()

    if args.version:
        # Answered before Qt loads: useful in bug reports, and it must work
        # even when the GUI stack cannot start at all.
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as pkg_version

        from meshchat.version import __version__

        print(f"OrcMesh {__version__}")
        # Package metadata rather than a __version__ attribute: meshtastic
        # does not expose one, so getattr() reported "unknown" for it.
        for dist in ("meshtastic", "PySide6", "pyqtgraph"):
            try:
                print(f"{dist} {pkg_version(dist)}")
            except PackageNotFoundError:
                print(f"{dist} (not installed)")
        print(f"Python {sys.version.split()[0]}")
        return 0

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
    # Keep the legacy Qt identity so upgrades retain existing settings.
    app.setOrganizationName("MeshChat")
    app.setOrganizationDomain("meshtastic.org")
    app.setApplicationName("MeshChat")
    app.setApplicationDisplayName("OrcMesh")

    from meshchat.version import __version__
    app.setApplicationVersion(__version__)

    # Apply global stylesheet and palette
    from meshchat.ui.theme import global_stylesheet
    app.setStyleSheet(global_stylesheet())

    from meshchat.ui.main_window import MainWindow
    window = MainWindow()
    window.show()

    return app.exec()
