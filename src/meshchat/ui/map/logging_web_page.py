"""MeshChat – LoggingWebEnginePage: routes the map's in-page JS console
(errors, warnings, failed tile loads, etc.) into the app's own log file so
map issues are diagnosable from meshchat.log without opening devtools."""
from __future__ import annotations

import logging

from PySide6.QtWebEngineCore import QWebEnginePage

log = logging.getLogger(__name__)

_LEVEL_NAMES = {
    QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel: "INFO",
    QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel: "WARNING",
    QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel: "ERROR",
}


class LoggingWebEnginePage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line_number, source_id) -> None:  # noqa: N802
        level_name = _LEVEL_NAMES.get(level, "INFO")
        log.info("[map JS %s] %s:%d — %s", level_name, source_id, line_number, message)
