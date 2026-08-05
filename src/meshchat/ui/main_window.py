"""MeshChat – MainWindow: application shell with nav rail and page stack."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import platformdirs
from PySide6.QtCore import Qt, QSettings, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from meshchat.controllers.meshtastic_controller import (
    ConnectionState,
    MeshtasticController,
    MessageStatus,
)
from meshchat.models.network_session import NetworkSession
from meshchat.services.monitor_store import MonitorStore
from meshchat.services.packet_ingestor import PacketIngestor
from meshchat.ui.chat_view import ChatView
from meshchat.ui.monitor.monitor_page import MonitorPage
from meshchat.ui.nodes.nodes_page import NodesPage
from meshchat.ui.theme import global_stylesheet
from meshchat.ui.widgets.channel_list import ChannelList
from meshchat.ui.widgets.connection_bar import ConnectionBar

log = logging.getLogger(__name__)

_LOG_DIR = Path(platformdirs.user_data_dir("MeshChat", appauthor=False)) / "logs"
_SETTINGS_KEY = "MeshChat/MainWindow"


# ── Nav rail button ────────────────────────────────────────────────────────

class _NavButton(QPushButton):
    def __init__(self, icon: str, label: str, parent=None):
        super().__init__(f"{icon}\n{label}", parent)
        self.setCheckable(True)
        self.setFixedSize(56, 56)


# ── Main Window ────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MeshChat for Windows")
        self.setMinimumSize(1100, 700)
        self.resize(1400, 900)
        self.setStyleSheet(global_stylesheet())

        # ── Services ──────────────────────────────────────────────────
        self._controller = MeshtasticController(self)
        self._session = NetworkSession.new()
        self._store = MonitorStore()
        self._ingestor = PacketIngestor(self._session, self._store)

        # ── Central layout ────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Nav rail ──────────────────────────────────────────────────
        nav = QWidget()
        nav.setObjectName("navRail")
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(0, 8, 0, 8)
        nav_layout.setSpacing(0)
        nav_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._nav_chat     = _NavButton("💬", "Chat")
        self._nav_monitor  = _NavButton("📡", "Monitor")
        self._nav_nodes    = _NavButton("🔵", "Nodes")
        self._nav_spectrum = _NavButton("📶", "Spectrum")
        self._nav_chat.setChecked(True)

        for btn in (self._nav_chat, self._nav_monitor, self._nav_nodes, self._nav_spectrum):
            btn.setAutoExclusive(True)
            nav_layout.addWidget(btn)

        nav_layout.addStretch()
        root.addWidget(nav)

        # ── Main content area ─────────────────────────────────────────
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # Connection bar (always visible)
        self._conn_bar = ConnectionBar()
        self._conn_bar.scan_requested.connect(self._controller.scan_ble)
        self._conn_bar.connect_ble_requested.connect(self._controller.connect_ble)
        self._conn_bar.connect_tcp_requested.connect(self._controller.connect_tcp)
        self._conn_bar.connect_serial_requested.connect(self._controller.connect_serial)
        self._conn_bar.list_serial_ports_requested.connect(self._controller.list_serial_ports)
        self._conn_bar.disconnect_requested.connect(self._controller.disconnect)
        content_layout.addWidget(self._conn_bar)

        # Chat sub-layout: channel sidebar + chat view
        chat_container = QWidget()
        chat_h = QHBoxLayout(chat_container)
        chat_h.setContentsMargins(0, 0, 0, 0)
        chat_h.setSpacing(0)

        self._channel_list = ChannelList()
        self._channel_list.channel_selected.connect(self._on_channel_selected)
        self._channel_list.dm_target_selected.connect(self._on_dm_selected)
        chat_h.addWidget(self._channel_list)

        self._chat_view = ChatView()
        self._chat_view.send_requested.connect(self._on_send)
        self._chat_view.send_direct_requested.connect(self._on_send_dm)
        chat_h.addWidget(self._chat_view, 1)

        # Restore persisted chat history (radios don't retain message history
        # themselves, so MeshChat keeps its own local copy like the Android app does)
        self._chat_view.load_history(self._load_message_history())

        # Monitor page
        self._monitor_page = MonitorPage()

        # Nodes page
        self._nodes_page = NodesPage()
        self._nodes_page.message_requested.connect(self._on_node_message_requested)
        self._nodes_page.show_on_map_requested.connect(self._on_show_node_on_map)
        self._nodes_page.position_requested.connect(self._controller.request_position)
        self._nodes_page.telemetry_requested.connect(self._controller.request_telemetry)
        self._nodes_page.traceroute_requested.connect(self._controller.send_traceroute)
        self._nodes_page.favorite_requested.connect(self._controller.set_favorite)
        self._nodes_page.remove_node_requested.connect(self._controller.remove_node)

        # Clicking a node on the Monitor page (ranking row or map pin) keeps
        # the Nodes page's own selection in sync, so switching there later
        # already shows/sorts around the same node — without yanking the
        # user away from the map they're currently looking at.
        self._monitor_page.node_selected.connect(self._nodes_page.select_node)

        # Spectrum page (RTL-SDR waterfall)
        from meshchat.ui.spectrum.spectrum_page import SpectrumPage
        self._spectrum_page = SpectrumPage()

        # Stacked widget
        self._stack = QStackedWidget()
        self._stack.addWidget(chat_container)        # index 0
        self._stack.addWidget(self._monitor_page)    # index 1
        self._stack.addWidget(self._nodes_page)      # index 2
        self._stack.addWidget(self._spectrum_page)   # index 3
        content_layout.addWidget(self._stack, 1)

        root.addWidget(content, 1)

        # Nav connections
        self._nav_chat.clicked.connect(lambda: self._stack.setCurrentIndex(0))
        self._nav_monitor.clicked.connect(lambda: self._stack.setCurrentIndex(1))
        self._nav_nodes.clicked.connect(lambda: self._stack.setCurrentIndex(2))
        self._nav_spectrum.clicked.connect(lambda: self._stack.setCurrentIndex(3))

        # ── Controller signals ────────────────────────────────────────
        ctrl = self._controller
        ctrl.connection_state_changed.connect(self._on_state_changed)
        ctrl.connected.connect(self._on_connected)
        ctrl.disconnected.connect(self._on_disconnected)
        ctrl.channels_updated.connect(self._on_channels_updated)
        ctrl.channels_updated.connect(self._monitor_page.set_channels)
        ctrl.lora_config_updated.connect(self._monitor_page.set_radio_config)
        ctrl.lora_config_updated.connect(self._spectrum_page.set_radio_config)
        ctrl.message_received.connect(self._on_message_received)
        ctrl.message_status_changed.connect(self._on_message_status)
        ctrl.ble_scan_finished.connect(self._conn_bar.set_ble_devices)
        ctrl.serial_ports_found.connect(self._conn_bar.set_serial_ports)
        ctrl.nodedb_synced.connect(self._ingestor.seed_from_nodedb)
        ctrl.node_action_completed.connect(self._on_node_action_completed)
        ctrl.error_occurred.connect(self._on_error)
        ctrl.raw_packet.connect(self._ingestor.ingest_raw)

        # Ingestor signals
        self._ingestor.packet_ingested.connect(self._monitor_page.on_packet_ingested)
        self._ingestor.node_updated.connect(self._on_node_snapshot)
        self._ingestor.position_updated.connect(self._monitor_page.on_position_updated)
        self._ingestor.telemetry_updated.connect(self._on_telemetry)
        self._ingestor.stats_updated.connect(self._monitor_page.on_stats_updated)
        self._ingestor.nodedb_seeded.connect(self._on_nodedb_seeded)

        self._is_connected = False
        self._local_node_num: int | None = None

        # Populate the map/node list from persisted history immediately, even
        # before connecting this session — matches the official Meshtastic
        # app always showing its saved NodeDB rather than starting blank.
        self._ingestor.seed_from_store(self._store.read_nodes(), self._store.read_latest_positions())

        # Trim old packet/position/telemetry rows once per launch. Without
        # this the monitor tables grow without bound for as long as the app
        # is ever used. Node identities and chat history are never pruned.
        QTimer.singleShot(5_000, self._prune_old_data)

        # ── Status bar ────────────────────────────────────────────────
        self._status_bar = self.statusBar()
        self._status_bar.showMessage("Ready — connect to a Meshtastic radio to begin")

        # ── Menu ──────────────────────────────────────────────────────
        self._build_menu()

        # ── Restore geometry ──────────────────────────────────────────
        settings = QSettings()
        geom = settings.value(f"{_SETTINGS_KEY}/geometry")
        if geom:
            self.restoreGeometry(geom)

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("File")

        export_pkts_act = QAction("Export Packet Log to CSV…", self)
        export_pkts_act.triggered.connect(self._export_packets)
        file_menu.addAction(export_pkts_act)

        export_nodes_act = QAction("Export Nodes to CSV…", self)
        export_nodes_act.triggered.connect(self._export_nodes)
        file_menu.addAction(export_nodes_act)

        file_menu.addSeparator()

        quit_act = QAction("Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        help_menu = menu_bar.addMenu("Help")

        log_act = QAction("Open Log Folder", self)
        log_act.triggered.connect(self._open_log_folder)
        help_menu.addAction(log_act)

        diag_act = QAction("Copy Diagnostic Summary", self)
        diag_act.triggered.connect(self._copy_diagnostic)
        help_menu.addAction(diag_act)

        help_menu.addSeparator()

        about_act = QAction("About MeshChat", self)
        about_act.triggered.connect(self._show_about)
        help_menu.addAction(about_act)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _prune_old_data(self) -> None:
        """Queue retention cleanup onto the store's writer thread.

        QTimer.singleShot only defers this into the GUI event loop — it does
        not move the work off the GUI thread. Doing the DELETEs here would
        block the UI for as long as they take, so the store queues them onto
        the background writer instead and logs the outcome there.
        """
        self._store.prune_async()

    def _export_packets(self) -> None:
        from meshchat.services.export_service import ExportService

        rows = self._ingestor.get_recent_packets()
        if not rows:
            self._status_bar.showMessage("Nothing to export — no packets captured yet", 5000)
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Packet Log", "meshchat-packets.csv", "CSV files (*.csv)"
        )
        if not path:
            return

        # Message text is personal content, so it is opt-in rather than
        # silently written into a file the user may share.
        include_text = QMessageBox.question(
            self,
            "Include message text?",
            "Include the text of received messages in the export?\n\n"
            "Message content is personal — leave this out if you plan to share the file.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

        try:
            count = ExportService.export_packets_csv(rows, Path(path), include_text=include_text)
            self._status_bar.showMessage(f"Exported {count} packet(s) to {path}", 8000)
        except OSError as exc:
            log.exception("Packet export failed")
            QMessageBox.warning(self, "Export Failed", f"Could not write the file:\n{exc}")

    def _export_nodes(self) -> None:
        from meshchat.services.export_service import ExportService

        nodes = self._ingestor.get_nodes()
        if not nodes:
            self._status_bar.showMessage("Nothing to export — no nodes known yet", 5000)
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Nodes", "meshchat-nodes.csv", "CSV files (*.csv)"
        )
        if not path:
            return

        try:
            count = ExportService.export_nodes_csv(nodes, Path(path))
            self._status_bar.showMessage(f"Exported {count} node(s) to {path}", 8000)
        except OSError as exc:
            log.exception("Node export failed")
            QMessageBox.warning(self, "Export Failed", f"Could not write the file:\n{exc}")

    def _open_log_folder(self) -> None:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(_LOG_DIR))

    def _copy_diagnostic(self) -> None:
        from meshchat.version import VERSION
        import meshtastic
        lines = [
            f"MeshChat {VERSION}",
            f"Python {sys.version}",
            f"PySide6 {self._pyside6_ver()}",
            f"meshtastic {getattr(meshtastic, '__version__', '?')}",
        ]
        QApplication.clipboard().setText("\n".join(lines))
        self._status_bar.showMessage("Diagnostic summary copied to clipboard", 3000)

    @staticmethod
    def _pyside6_ver() -> str:
        try:
            import PySide6
            return PySide6.__version__
        except Exception:
            return "?"

    def _show_about(self) -> None:
        from meshchat.version import VERSION
        QMessageBox.about(
            self,
            "About MeshChat",
            f"<b>MeshChat for Windows</b><br>Version {VERSION}<br><br>"
            "Chat with your Meshtastic mesh network from Windows.<br><br>"
            "MeshChat connects to one nearby Meshtastic radio.<br>"
            "The radio — not the PC — sends and receives LoRa packets.<br><br>"
            "© 2026  GPLv3 (via Meshtastic Python library)",
        )

    # ------------------------------------------------------------------
    # Controller slots
    # ------------------------------------------------------------------

    def _on_state_changed(self, state: ConnectionState, detail: str) -> None:
        self._conn_bar.set_state(state, detail)
        self._monitor_page.set_connection_state(state)
        status_map = {
            ConnectionState.DISCONNECTED:  "Disconnected",
            ConnectionState.SCANNING:      "Scanning for BLE devices…",
            ConnectionState.CONNECTING:    f"Connecting to {detail}…",
            ConnectionState.SYNCING:       "Downloading radio configuration…",
            ConnectionState.CONNECTED:     f"Connected — {detail}",
            ConnectionState.DISCONNECTING: "Disconnecting…",
            ConnectionState.ERROR:         f"Error: {detail}",
        }
        self._status_bar.showMessage(status_map.get(state, state.value))
        self._is_connected = state == ConnectionState.CONNECTED
        if not self._is_connected:
            self._chat_view.set_send_enabled(False)

    def _on_connected(self, summary) -> None:
        # A fresh connect-to-disconnect run gets its own session id — without
        # this, reconnecting in the same app session would keep stamping
        # messages/packets with the FIRST connection's session_id forever
        # (self._session is only ever constructed once, in __init__).
        self._session.start_new()
        name = summary.long_name or summary.short_name or summary.node_id or "Radio"
        self._conn_bar.set_device_name(name)
        self._monitor_page.set_connection_state(ConnectionState.CONNECTED, name)
        self._monitor_page.set_local_node(summary.node_num)
        self._channel_list.set_local_node(summary.node_num)
        self._local_node_num = summary.node_num
        self._status_bar.showMessage(f"Connected to {name}")

    def _on_disconnected(self, reason: str) -> None:
        self._session.end()
        self._conn_bar.set_device_name("")
        self._channel_list.clear_channels()
        self._channel_list.set_local_node(None)
        self._channel_list.set_dm_nodes([])
        self._chat_view.clear_for_disconnect()
        self._monitor_page.clear_map()
        self._monitor_page.set_local_node(None)
        self._monitor_page.on_local_telemetry(None, None, None)
        self._local_node_num = None
        self._status_bar.showMessage(f"Disconnected — {reason}")

    def _on_telemetry(self, sample) -> None:
        # The dashboard header shows the connected radio's own environment
        # sensor readings (if it has a BME280/BMP280 etc. attached) — only
        # the local node's telemetry is relevant there. Per-node telemetry
        # for every other node is shown in the Node Inspector instead.
        if sample.node_num is not None and sample.node_num == self._local_node_num:
            self._monitor_page.on_local_telemetry(
                sample.temperature_c, sample.relative_humidity, sample.barometric_pressure_hpa
            )

    def _on_channels_updated(self, channels: list) -> None:
        self._channel_list.set_channels(channels)
        if channels:
            self._chat_view.set_channel(channels[0].index)
            self._chat_view.set_send_enabled(True)
        else:
            self._chat_view.set_no_channels_message()

    def _on_message_received(self, msg) -> None:
        # The controller/worker that constructs inbound messages doesn't know
        # about NetworkSession — stamp the real session id on before display
        # and persistence. ChatMessage is frozen, so this is a copy.
        import dataclasses
        msg = dataclasses.replace(msg, session_id=self._session.id)
        self._chat_view.add_message(msg)
        self._store.save_message(msg)

    def _on_message_status(self, packet_id: int, status: MessageStatus, detail: str) -> None:
        self._chat_view.update_message_status(packet_id, status)

    def _on_error(self, err) -> None:
        self._status_bar.showMessage(f"⚠  {err.title}: {err.message}", 8000)
        log.error("User-facing error [%s]: %s — %s", err.code.value, err.title, err.message)
        if err.code.value == "ble_pairing_required":
            self._show_pairing_required_dialog(err)
        elif err.recoverable:
            # Show in status bar only; don't pop a modal for every transient error
            pass
        else:
            QMessageBox.warning(self, err.title, err.message)

    def _show_pairing_required_dialog(self, err) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(err.title)
        box.setText(err.message)
        open_btn = box.addButton("Open Bluetooth Settings", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            self._open_windows_bluetooth_settings()

    def _open_windows_bluetooth_settings(self) -> None:
        try:
            if sys.platform == "win32":
                os.startfile("ms-settings:bluetooth")
            else:
                self._status_bar.showMessage("Bluetooth settings shortcut is only available on Windows", 5000)
        except OSError as exc:
            log.warning("Could not open Bluetooth settings: %s", exc)
            self._status_bar.showMessage("Could not open Bluetooth settings automatically", 5000)

    def _on_channel_selected(self, channel_index: int) -> None:
        self._chat_view.set_channel(channel_index)
        self._chat_view.set_send_enabled(self._is_connected)

    def _on_dm_selected(self, node_num: int, name: str) -> None:
        self._chat_view.set_dm_target(node_num, name)
        self._chat_view.set_send_enabled(self._is_connected)

    def _on_send(self, text: str, channel_index: int) -> None:
        import uuid
        from datetime import datetime, timezone
        from meshchat.controllers.meshtastic_controller import ChatMessage, MessageDirection, MessageStatus

        # Show outbound bubble immediately
        msg = ChatMessage(
            local_id=str(uuid.uuid4()),
            packet_id=None,
            channel_index=channel_index,
            direction=MessageDirection.OUTBOUND,
            sender_num=None,
            sender_id=None,
            sender_name="Me",
            text=text,
            timestamp=datetime.now(timezone.utc),
            status=MessageStatus.SENDING,
            session_id=self._session.id,
        )
        self._chat_view.add_message(msg)
        self._store.save_message(msg)
        self._controller.send_channel_text(text, channel_index)

    def _on_send_dm(self, text: str, destination_num: int) -> None:
        import uuid
        from datetime import datetime, timezone
        from meshchat.controllers.meshtastic_controller import ChatMessage, MessageDirection, MessageStatus

        msg = ChatMessage(
            local_id=str(uuid.uuid4()),
            packet_id=None,
            channel_index=0,
            direction=MessageDirection.OUTBOUND,
            sender_num=None,
            sender_id=None,
            sender_name="Me",
            text=text,
            timestamp=datetime.now(timezone.utc),
            status=MessageStatus.SENDING,
            destination_num=destination_num,
            session_id=self._session.id,
        )
        self._chat_view.add_message(msg)
        self._store.save_message(msg)
        self._controller.send_direct_text(text, destination_num)

    def _on_node_message_requested(self, node_num: int, name: str) -> None:
        """Jump to the Chat page with a DM thread open to this node."""
        self._chat_view.set_dm_target(node_num, name)
        self._chat_view.set_send_enabled(self._is_connected)
        self._nav_chat.setChecked(True)
        self._stack.setCurrentIndex(0)

    def _on_show_node_on_map(self, node_num: int) -> None:
        self._nav_monitor.setChecked(True)
        self._stack.setCurrentIndex(1)
        self._monitor_page.focus_node_on_map(node_num)

    def _on_node_action_completed(self, node_num: int, action: str, detail: str) -> None:
        self._status_bar.showMessage(detail, 6000)

    def _on_node_snapshot(self, snapshot) -> None:
        self._monitor_page.on_node_updated(snapshot)
        nodes_list = self._ingestor.get_nodes()
        nodes_dict = {n.node_num: n for n in nodes_list}
        self._nodes_page.update_nodes(nodes_dict)
        self._channel_list.set_dm_nodes(nodes_list)

    def _on_nodedb_seeded(self) -> None:
        """Called once after MeshtasticController.nodedb_synced has been fully
        merged into the ingestor — refresh dependent views and frame the map."""
        nodes_list = self._ingestor.get_nodes()
        nodes_dict = {n.node_num: n for n in nodes_list}
        self._nodes_page.update_nodes(nodes_dict)
        self._channel_list.set_dm_nodes(nodes_list)
        QTimer.singleShot(300, self._monitor_page.fit_map)

    # ------------------------------------------------------------------
    # Chat history persistence
    # ------------------------------------------------------------------

    def _load_message_history(self):
        from datetime import datetime
        from meshchat.controllers.meshtastic_controller import ChatMessage, MessageDirection, MessageStatus

        messages = []
        for row in self._store.read_messages():
            try:
                messages.append(ChatMessage(
                    local_id=row["local_id"] or "",
                    packet_id=row["packet_id"],
                    channel_index=row["channel_index"] or 0,
                    direction=MessageDirection(row["direction"]),
                    sender_num=row["sender_num"],
                    sender_id=row["sender_id"],
                    sender_name=row["sender_name"] or "Unknown",
                    text=row["text"] or "",
                    timestamp=datetime.fromisoformat(row["observed_at"]),
                    status=MessageStatus(row["status"]),
                    destination_num=row["destination_num"],
                    session_id=row["session_id"] or "",
                ))
            except (ValueError, KeyError, TypeError) as exc:
                log.debug("Skipping malformed persisted message: %s", exc)
        return messages

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        settings = QSettings()
        settings.setValue(f"{_SETTINGS_KEY}/geometry", self.saveGeometry())
        self._spectrum_page.shutdown()
        self._controller.shutdown()
        self._store.shutdown()
        super().closeEvent(event)
