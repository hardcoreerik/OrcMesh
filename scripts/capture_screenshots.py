"""
Capture README screenshots straight from the running Qt widgets.

Produces clean, reproducible images with no desktop chrome. Connects to a
radio (if one is given) so telemetry and live NodeDB data are populated,
otherwise falls back to whatever is in the local persisted history.

    python scripts/capture_screenshots.py [--port COM11]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

OUT_DIR = Path(__file__).parent.parent / "docs" / "screenshots"


def _grab(widget, name: str, via_window: bool = False) -> None:
    """Save a PNG of `widget`.

    Normal Qt widgets are rendered directly with widget.grab() — exact, and
    immune to display-scaling math. Anything containing a QWebEngineView (the
    map) renders in a separate compositor and comes out blank that way, so
    those are captured from the composited window and cropped, deriving the
    device-pixel ratio from the pixmap itself rather than trusting the
    reported DPR.
    """
    if not via_window:
        pixmap = widget.grab()
    else:
        from PySide6.QtWidgets import QApplication

        window = widget.window()
        pixmap = QApplication.primaryScreen().grabWindow(window.winId())

        ratio_x = pixmap.width() / max(window.width(), 1)
        ratio_y = pixmap.height() / max(window.height(), 1)
        top_left = widget.mapTo(window, widget.rect().topLeft())
        pixmap = pixmap.copy(
            round(top_left.x() * ratio_x), round(top_left.y() * ratio_y),
            round(widget.width() * ratio_x), round(widget.height() * ratio_y),
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.png"
    pixmap.save(str(path), "PNG")
    print(f"  saved {path.name}  ({pixmap.width()}x{pixmap.height()})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", help="Serial port to connect to (e.g. COM11)")
    parser.add_argument("--settle", type=int, default=25,
                        help="Seconds to wait for connect/sync/tiles before capturing")
    args = parser.parse_args()

    sys.argv = sys.argv[:1]  # keep Qt from parsing our flags

    from meshchat.services.app_logging import configure_logging
    configure_logging(debug=False)

    import os
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu-sandbox")

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setOrganizationName("OrcMesh")
    app.setApplicationName("OrcMesh")

    from meshchat.ui.theme import global_stylesheet
    app.setStyleSheet(global_stylesheet())

    from meshchat.ui.main_window import MainWindow
    window = MainWindow()
    window.resize(1600, 950)
    window.show()

    def pump(seconds: float) -> None:
        loop = QEventLoop()
        QTimer.singleShot(int(seconds * 1000), loop.quit)
        loop.exec()

    if args.port:
        print(f"Connecting to {args.port} ...")
        window._controller.connect_serial(args.port)

    print(f"Settling for {args.settle}s (sync + map tiles) ...")
    pump(args.settle)

    print("Capturing:")

    # ── Monitor page (map + full dashboard) ────────────────────────────
    window._stack.setCurrentIndex(1)
    window._nav_monitor.setChecked(True)
    pump(6)
    # Both of these contain the map's QWebEngineView, so must come from the
    # composited window rather than an offscreen widget render.
    _grab(window._monitor_page, "monitor-dashboard", via_window=True)
    _grab(window._monitor_page._map_widget, "map", via_window=True)

    # KPI card strip — the metric cards live in the row above the filter bar
    kpi_row = window._monitor_page._card_direct.parentWidget()
    _grab(kpi_row, "kpi-cards")

    # Distribution panels (packet breakdown / channels / roles / hardware)
    _grab(window._monitor_page._dist_panel, "distribution-panels")

    # ── Nodes page (table + inspector) ─────────────────────────────────
    window._stack.setCurrentIndex(2)
    window._nav_nodes.setChecked(True)
    pump(2)

    # Show a node with the richest data available, preferring a *remote* node
    # (the locally-attached radio has no last-hop RF signal or hop counts of
    # its own, so it makes a poor showcase for the inspector).
    nodes = window._ingestor.get_nodes()
    local = window._local_node_num

    def richness(n) -> tuple:
        return (
            n.node_num != local,
            n.last_snr is not None,
            n.battery_level is not None or n.voltage is not None,
            n.last_hops_used is not None,
            n.packet_count,
        )

    if nodes:
        best = max(nodes, key=richness)
        window._nodes_page._inspector.show_node(best)
        print(f"  inspector showing: {best.display_name} "
              f"(snr={best.last_snr}, battery={best.battery_level}, hops={best.last_hops_used})")
    pump(2)

    _grab(window._nodes_page, "nodes-page")
    _grab(window._nodes_page._inspector, "node-inspector-signal")

    # A second inspector shot on whichever node actually reports telemetry —
    # signal-rich and telemetry-rich nodes are usually not the same node.
    with_telemetry = [n for n in nodes if n.battery_level is not None or n.voltage is not None]
    if with_telemetry:
        tel_node = max(with_telemetry, key=lambda n: n.packet_count)
        window._nodes_page._inspector.show_node(tel_node)
        pump(1)
        print(f"  telemetry inspector: {tel_node.display_name} "
              f"(battery={tel_node.battery_level}, voltage={tel_node.voltage})")
        _grab(window._nodes_page._inspector, "node-inspector-telemetry")

    # ── Chat page ──────────────────────────────────────────────────────
    window._stack.setCurrentIndex(0)
    window._nav_chat.setChecked(True)
    pump(2)
    _grab(window._stack.currentWidget(), "chat")

    # ── Spectrum page ──────────────────────────────────────────────────
    window._stack.setCurrentIndex(3)
    window._nav_spectrum.setChecked(True)
    pump(2)
    _grab(window._spectrum_page, "spectrum")

    print("Done.")
    window._spectrum_page.shutdown()
    window._controller.shutdown()
    window._store.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
