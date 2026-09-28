"""OrcMesh – MainWindow: application shell with nav rail and page stack."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import platformdirs
from PySide6.QtCore import QObject, Qt, QSettings, QThread, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
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
from meshchat.controllers.orcmaps_controller import OrcMapsController
from meshchat.models.connection_profile import ConnectionProfile
from meshchat.models.network_session import NetworkSession
from meshchat.services import orcmaps
from meshchat.services.connection_supervisor import ConnectionSupervisor
from meshchat.services.monitor_store import MonitorStore
from meshchat.services.packet_ingestor import PacketIngestor
from meshchat.services.provisioning.capability import detect_capability
from meshchat.services.provisioning.destinations import default_map_dir
from meshchat.ui.chat_view import ChatView
from meshchat.ui.monitor.monitor_page import MonitorPage
from meshchat.ui.nodes.nodes_page import NodesPage
from meshchat.ui.setup.setup_wizard import SetupWizard
from meshchat.ui.theme import global_stylesheet
from meshchat.ui.widgets.channel_list import ChannelList
from meshchat.ui.widgets.connection_bar import ConnectionBar

log = logging.getLogger(__name__)

_LOG_DIR = Path(platformdirs.user_data_dir("MeshChat", appauthor=False)) / "logs"
_SETTINGS_KEY = "MeshChat/MainWindow"

#: Persisted basemap choice: "online", or the stem of an OrcMaps pack.
_BASEMAP_SETTINGS_KEY = "MeshChat/Map/source"
_ONLINE_BASEMAP_LABEL = "Online · OpenStreetMap / CARTO tiles"

#: Set once the setup wizard has been completed. Absent means "never offered",
#: which is what makes a fresh install open the wizard exactly once.
_SETUP_COMPLETE_KEY = "MeshChat/Setup/complete"


# ── Packet export worker ──────────────────────────────────────────────────
# A full-session export can be up to read_packets_as_objects()'s 200,000-row
# cap — CSV-formatting and writing that many rows is real, unbounded disk
# I/O that would otherwise block the GUI thread (input, repaints) for as
# long as it takes. Runs on its own QThread; the row list itself was
# already gathered on the GUI thread (a single bounded SQLite read, fast
# enough not to need its own worker) before this is started.

class _PacketExportWorker(QObject):
    finished = Signal(int)   # rows written
    failed = Signal(str)     # error message

    def __init__(self, rows, path: Path, include_text: bool, parent=None):
        super().__init__(parent)
        self._rows = rows
        self._path = path
        self._include_text = include_text

    def run(self) -> None:
        from meshchat.services.export_service import ExportService
        try:
            count = ExportService.export_packets_csv(
                self._rows, self._path, include_text=self._include_text,
            )
            self.finished.emit(count)
        except Exception as exc:
            # Not narrowed to OSError: any uncaught exception here would
            # otherwise leave this slot without ever emitting finished/
            # failed, so the caller's thread.quit() (only wired to those
            # two signals) never runs — the worker thread's event loop
            # stays up forever and the Export menu action stays disabled
            # for the rest of the session.
            log.exception("Packet export worker failed")
            self.failed.emit(str(exc))


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
        self.setWindowTitle("OrcMesh — LoRa Mesh Operations Console")
        self.setMinimumSize(1100, 700)
        self.resize(1400, 900)
        self.setStyleSheet(global_stylesheet())

        # ── Services ──────────────────────────────────────────────────
        self._controller = MeshtasticController(self)
        from meshchat.controllers.firmware_controller import FirmwareController
        self._firmware_controller = FirmwareController(self)
        self._orcmaps = OrcMapsController(self)
        self._session = NetworkSession.new()
        self._store = MonitorStore()
        self._ingestor = PacketIngestor(self._session, self._store)
        self._supervisor = ConnectionSupervisor(self._controller, self._store, parent=self)
        self._export_thread: QThread | None = None
        self._export_worker: _PacketExportWorker | None = None
        self._device_snapshot = None
        self._pending_flash = None
        self._pending_probe = None
        self._pending_flash_backup = None
        self._pending_serial_console = None
        self._flash_port: str | None = None
        #: Regions the setup wizard asked for, provisioned one at a time
        #: because OrcMaps' controller runs a single operation at a time.
        self._setup_region_queue: list[dict] = []
        #: Last position reported for this radio, so setup and the pack cutter
        #: can both offer "around here" without asking the radio twice.
        self._last_local_position: tuple[float, float] | None = None

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
        self._nav_sigint   = _NavButton("🛰", "SIGINT")
        self._nav_device   = _NavButton("⚙", "Device")
        self._nav_chat.setChecked(True)

        for btn in (
            self._nav_chat, self._nav_monitor, self._nav_nodes,
            self._nav_spectrum, self._nav_sigint, self._nav_device,
        ):
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
        self._conn_bar.connect_ble_requested.connect(self._on_connect_ble_requested)
        self._conn_bar.connect_tcp_requested.connect(self._on_connect_tcp_requested)
        self._conn_bar.connect_serial_requested.connect(self._on_connect_serial_requested)
        self._conn_bar.list_serial_ports_requested.connect(self._controller.list_serial_ports)
        self._conn_bar.disconnect_requested.connect(self._on_disconnect_requested)
        content_layout.addWidget(self._conn_bar)

        # Restore last-used connection profile into the bar so the user
        # doesn't have to re-type host/port on every launch.
        _saved_profile = ConnectionSupervisor.load_profile(self._store)
        if _saved_profile is not None:
            self._conn_bar.restore_profile(_saved_profile)

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

        # SIGINT page: spectrum, band survey and packet intelligence. Owns its own
        # SDR controllers, because the dongle can only be held in one place at a
        # time and the Spectrum tab has its own.
        from meshchat.ui.sigint.sigint_page import SigintPage
        self._sigint_page = SigintPage(store=self._store)
        self._lora_summary = None

        from meshchat.ui.device.device_page import DevicePage
        self._device_page = DevicePage()
        self._device_page.refresh_requested.connect(self._controller.refresh_device_controls)
        self._device_page.save_section_requested.connect(self._controller.apply_device_section)
        self._device_page.owner_requested.connect(self._controller.set_owner)
        self._device_page.channel_requested.connect(self._controller.update_channel)
        self._device_page.reboot_requested.connect(self._controller.reboot_device)
        self._device_page.shutdown_requested.connect(self._controller.shutdown_device)
        self._device_page.reset_nodedb_requested.connect(self._controller.reset_nodedb)
        self._device_page.factory_reset_requested.connect(self._controller.factory_reset)
        self._device_page.fixed_position_requested.connect(self._controller.set_fixed_position)
        self._device_page.remove_fixed_position_requested.connect(
            self._controller.remove_fixed_position
        )
        self._device_page.firmware_discover_requested.connect(
            self._firmware_controller.discover
        )
        self._device_page.firmware_prepare_requested.connect(
            self._firmware_controller.prepare
        )
        self._device_page.firmware_flash_requested.connect(
            self._on_firmware_flash_requested
        )
        self._device_page.firmware_probe_requested.connect(
            self._on_firmware_probe_requested
        )
        self._device_page.firmware_backup_requested.connect(
            self._on_firmware_backup_requested
        )
        self._device_page.profile_backup_requested.connect(
            self._controller.backup_device_profile
        )
        self._device_page.profile_restore_requested.connect(
            self._controller.restore_device_profile
        )
        self._device_page.serial_console_start_requested.connect(
            self._on_serial_console_requested
        )
        self._device_page.serial_console_stop_requested.connect(
            self._on_serial_console_stopped
        )

        # Stacked widget
        self._stack = QStackedWidget()
        self._stack.addWidget(chat_container)        # index 0
        self._stack.addWidget(self._monitor_page)    # index 1
        self._stack.addWidget(self._nodes_page)      # index 2
        self._stack.addWidget(self._spectrum_page)   # index 3
        self._stack.addWidget(self._device_page)     # index 4
        self._stack.addWidget(self._sigint_page)     # index 5
        content_layout.addWidget(self._stack, 1)

        root.addWidget(content, 1)

        # Nav connections
        self._nav_chat.clicked.connect(lambda: self._stack.setCurrentIndex(0))
        self._nav_monitor.clicked.connect(lambda: self._stack.setCurrentIndex(1))
        self._nav_nodes.clicked.connect(lambda: self._stack.setCurrentIndex(2))
        self._nav_spectrum.clicked.connect(lambda: self._stack.setCurrentIndex(3))
        self._nav_device.clicked.connect(lambda: self._stack.setCurrentIndex(4))
        self._nav_sigint.clicked.connect(lambda: self._stack.setCurrentIndex(5))

        # ── Controller signals ────────────────────────────────────────
        ctrl = self._controller
        ctrl.connection_state_changed.connect(self._on_state_changed)
        ctrl.connected.connect(self._on_connected)
        ctrl.disconnected.connect(self._on_disconnected)
        ctrl.channels_updated.connect(self._on_channels_updated)
        ctrl.channels_updated.connect(self._monitor_page.set_channels)
        ctrl.lora_config_updated.connect(self._monitor_page.set_radio_config)
        ctrl.lora_config_updated.connect(self._spectrum_page.set_radio_config)
        ctrl.lora_config_updated.connect(self._remember_lora_config)
        ctrl.message_received.connect(self._on_message_received)
        ctrl.message_status_changed.connect(self._on_message_status)
        ctrl.ble_scan_finished.connect(self._conn_bar.set_ble_devices)
        ctrl.serial_ports_found.connect(self._conn_bar.set_serial_ports)
        ctrl.nodedb_synced.connect(self._ingestor.seed_from_nodedb)
        ctrl.node_action_completed.connect(self._on_node_action_completed)
        ctrl.error_occurred.connect(self._on_error)
        ctrl.raw_packet.connect(self._ingestor.ingest_raw)
        ctrl.device_controls_updated.connect(self._on_device_controls_updated)
        ctrl.device_operation_completed.connect(self._on_device_operation)
        ctrl.profile_operation_completed.connect(self._device_page.profile_completed)

        firmware = self._firmware_controller
        firmware.release_found.connect(self._device_page.set_firmware_release)
        firmware.progress.connect(self._device_page.set_firmware_progress)
        firmware.bundle_ready.connect(self._device_page.set_firmware_bundle)
        firmware.log.connect(self._device_page.append_firmware_log)
        firmware.completed.connect(self._on_firmware_completed)

        # Maps tab: OrcMaps pack management. MainWindow owns discovery (it knows
        # where the checkout and packs are) and the Device page owns the UI.
        self._device_page.maps_refresh_requested.connect(self._refresh_map_packs)
        self._device_page.maps_verify_requested.connect(self._verify_map_directory)
        self._device_page.maps_provision_requested.connect(self._provision_map_pack)
        self._orcmaps.log.connect(self._device_page.append_maps_log)
        self._orcmaps.completed.connect(self._on_orcmaps_completed)
        self._ingestor.position_updated.connect(self._on_position_for_device_page)

        # Ingestor signals
        self._ingestor.packet_ingested.connect(self._monitor_page.on_packet_ingested)
        self._ingestor.node_updated.connect(self._on_node_snapshot)
        self._ingestor.position_updated.connect(self._monitor_page.on_position_updated)
        self._ingestor.telemetry_updated.connect(self._on_telemetry)
        self._ingestor.stats_updated.connect(self._monitor_page.on_stats_updated)
        self._ingestor.nodedb_seeded.connect(self._on_nodedb_seeded)

        self._is_connected = False
        self._local_node_num: int | None = None

        # Coalesces Nodes-table/DM-sidebar rebuilds from node_updated —
        # see _on_node_snapshot.
        self._nodes_dirty = False
        self._node_refresh_timer = QTimer(self)
        self._node_refresh_timer.timeout.connect(self._flush_node_updates)
        # Shares the node-refresh tick rather than running a second timer: both
        # exist to keep a lazily-updated table current.
        self._node_refresh_timer.timeout.connect(self._refresh_sigint_intel)
        self._node_refresh_timer.start(2000)

        # Populate the map/node list from persisted history immediately, even
        # before connecting this session — matches the official Meshtastic
        # app always showing its saved NodeDB rather than starting blank.
        stored_nodes = self._store.read_nodes()
        #: Node numbers the NodeDB already held when this session began. The SIGINT
        #: tab treats anything outside this set as unfamiliar, which is what makes
        #: "a radio appeared that this mesh has no history with" answerable rather
        #: than a guess.
        self._known_node_nums = {
            int(row["node_num"]) for row in stored_nodes
            if row.get("node_num") is not None
        }
        self._node_labels = {
            int(row["node_num"]): str(row.get("long_name") or row.get("short_name") or "")
            for row in stored_nodes
            if row.get("node_num") is not None
        }
        self._ingestor.seed_from_store(stored_nodes, self._store.read_latest_positions())

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

        # Re-apply the saved basemap source: an offline OrcMaps pack, or the
        # online tiles. Runs after the map view exists.
        self._restore_basemap_source()

        # Populate the Maps tab with the packs OrcMaps can see.
        self._refresh_map_packs()

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("File")

        self._export_pkts_act = QAction("Export Packet Log to CSV…", self)
        self._export_pkts_act.triggered.connect(self._export_packets)
        file_menu.addAction(self._export_pkts_act)

        export_nodes_act = QAction("Export Nodes to CSV…", self)
        export_nodes_act.triggered.connect(self._export_nodes)
        file_menu.addAction(export_nodes_act)

        file_menu.addSeparator()

        quit_act = QAction("Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        # ── Map menu ──────────────────────────────────────────────────
        # Basemap source: the built-in online tiles, or a local OrcMaps pack
        # rendered on this machine by OrcMaps' own host renderer. OrcMaps is a
        # separate AGPL-3 project that OrcMesh drives as a child process and
        # never links — see docs/orcmaps-integration.md.
        map_menu = menu_bar.addMenu("Map")

        self._basemap_act = QAction("Basemap Source…", self)
        self._basemap_act.setToolTip("Choose the online basemap or an offline OrcMaps pack")
        self._basemap_act.triggered.connect(self._choose_basemap_source)
        map_menu.addAction(self._basemap_act)

        map_menu.addSeparator()

        setup_act = QAction("Set Up OrcMesh…", self)
        setup_act.setToolTip("Choose your radio and install map regions for where you are")
        setup_act.triggered.connect(lambda: self.maybe_run_first_run(force=True))
        map_menu.addAction(setup_act)

        help_menu = menu_bar.addMenu("Help")

        log_act = QAction("Open Log Folder", self)
        log_act.triggered.connect(self._open_log_folder)
        help_menu.addAction(log_act)

        diag_act = QAction("Copy Diagnostic Summary", self)
        diag_act.triggered.connect(self._copy_diagnostic)
        help_menu.addAction(diag_act)

        help_menu.addSeparator()

        about_act = QAction("About OrcMesh", self)
        about_act.triggered.connect(self._show_about)
        help_menu.addAction(about_act)

    # ------------------------------------------------------------------
    # Map basemap source (online tiles ↔ an offline OrcMaps pack)
    # ------------------------------------------------------------------

    def _discover_map_packs(self) -> tuple[orcmaps.OrcMapsTools | None, list[orcmaps.OrcMapsPack], str]:
        """Return (tools, packs, reason); reason explains an empty result."""
        tools = orcmaps.find_tools()
        if tools is None:
            return None, [], (
                "OrcMaps' host tools were not found, so offline map packs cannot be "
                "rendered.\n\nBuild them from your OrcMaps checkout:\n\n"
                "  cmake -S <orcmaps>\\tools\\pack-inspect -B <orcmaps>\\build-pack-inspect\n"
                "  cmake --build <orcmaps>\\build-pack-inspect --config Release\n\n"
                "Or set ORCMESH_ORCMAPS_HOME to that checkout."
            )
        directories = orcmaps.default_pack_directories(tools)
        packs = orcmaps.discover_packs(directories)
        if not packs:
            log.info("No OrcMaps map packs found in %s", directories)
            looked = "\n".join(f"  {directory}" for directory in directories)
            return tools, [], (
                "No OrcMaps map packs were found. A pack is a .pmtiles archive with "
                "a matching .manifest.json next to it.\n\nLooked in:\n" + looked
            )
        log.info(
            "Map packs available: %s",
            "; ".join(f"{pack.display_name} ({pack.zoom_label}, {pack.pack_class})" for pack in packs),
        )
        return tools, packs, ""

    @staticmethod
    def _pack_label(pack: orcmaps.OrcMapsPack) -> str:
        size = f", {pack.size_bytes / 1e6:.0f} MB" if pack.size_bytes else ""
        return f"Offline · {pack.display_name} ({pack.zoom_label}, {pack.pack_class}{size})"

    def _choose_basemap_source(self) -> None:
        tools, packs, reason = self._discover_map_packs()
        if tools is None or not packs:
            QMessageBox.information(self, "Offline Maps Unavailable", reason)
            return

        labels = [_ONLINE_BASEMAP_LABEL] + [self._pack_label(pack) for pack in packs]
        active = self._monitor_page.map_widget.offline_pack
        current = next(
            (i for i, pack in enumerate(packs, start=1) if active and pack.stem == active.stem),
            0,
        )
        choice, accepted = QInputDialog.getItem(
            self,
            "Basemap Source",
            "Offline packs are rendered locally by OrcMaps — no network is used.",
            labels,
            current,
            False,
        )
        if accepted:
            self._apply_basemap_source(labels.index(choice), tools, packs)

    def _apply_basemap_source(
        self, index: int, tools: orcmaps.OrcMapsTools, packs: list[orcmaps.OrcMapsPack],
    ) -> None:
        """Switch basemap by dialog index (0 = online), remembering the choice."""
        settings = QSettings()
        map_widget = self._monitor_page.map_widget
        if index <= 0:
            map_widget.show_online_basemap()
            settings.setValue(_BASEMAP_SETTINGS_KEY, "online")
            self._status_bar.showMessage("Basemap: online tiles", 6000)
            return

        pack = packs[index - 1]
        try:
            map_widget.show_offline_pack(tools, pack, packs)
        except orcmaps.OrcMapsError as exc:
            log.exception("Offline basemap failed for pack %s", pack.stem)
            QMessageBox.warning(
                self, "Offline Basemap Failed",
                f"Could not render tiles from '{pack.display_name}'.\n\n{exc}\n\n"
                "Falling back to the online basemap.",
            )
            map_widget.show_online_basemap()
            settings.setValue(_BASEMAP_SETTINGS_KEY, "online")
            return

        settings.setValue(_BASEMAP_SETTINGS_KEY, pack.stem)
        # The manifest's required attribution is drawn on the map itself too;
        # repeating it here keeps it visible when that control is collapsed.
        self._status_bar.showMessage(
            f"Basemap: {pack.display_name} ({pack.zoom_label}, {pack.pack_class}) — "
            f"{pack.attribution_text}",
            15000,
        )

    def _restore_basemap_source(self) -> None:
        """Re-apply the saved basemap choice, falling back to online tiles."""
        stem = str(QSettings().value(_BASEMAP_SETTINGS_KEY, "online") or "online")
        if stem == "online":
            return
        tools, packs, _reason = self._discover_map_packs()
        if tools is None:
            return
        pack = next((p for p in packs if p.stem == stem), None)
        if pack is None:
            log.info("Saved basemap pack %r is no longer available; using online tiles", stem)
            QSettings().setValue(_BASEMAP_SETTINGS_KEY, "online")
            return
        self._apply_basemap_source(packs.index(pack) + 1, tools, packs)

    # ------------------------------------------------------------------
    # Maps tab — OrcMaps pack management
    # ------------------------------------------------------------------

    def _refresh_map_packs(self) -> None:
        tools, packs, reason = self._discover_map_packs()
        self._device_page.set_map_packs(
            packs, tools_available=tools is not None, reason=reason,
        )

    def _verify_map_directory(self, directory: str) -> None:
        tools, _packs, reason = self._discover_map_packs()
        if tools is None:
            self._device_page.set_maps_busy(False)
            QMessageBox.information(self, "Offline Maps Unavailable", reason)
            return
        self._device_page.set_maps_busy(True)
        # The service resolves a card root to its orcmaps/ directory — handing
        # the root straight to the tool reports "no usable packs" for a good card.
        self._orcmaps.verify(tools, Path(directory))

    def _provision_map_pack(self, params: dict) -> None:
        tools, _packs, reason = self._discover_map_packs()
        if tools is None or tools.provision_script is None:
            self._device_page.set_maps_busy(False)
            QMessageBox.information(self, "Offline Maps Unavailable", reason)
            return
        request = orcmaps.PinPackRequest(
            source_manifest=Path(str(params["source_manifest"])),
            lat=float(params["lat"]),
            lon=float(params["lon"]),
            radius_km=float(params["radius_km"]),
            name=str(params["name"]),
            display_name=str(params.get("display_name") or ""),
            card_root=Path(str(params["card_root"])),
            dry_run=bool(params.get("dry_run", False)),
        )
        self._orcmaps.provision(tools, request)

    def _on_orcmaps_completed(self, operation: str, success: bool, detail: str) -> None:
        self._device_page.maps_operation_completed(operation, success, detail)
        self._status_bar.showMessage(detail, 10000 if success else 15000)
        if success and operation == "provision":
            # The card's contents may have changed; keep the pack list honest.
            self._refresh_map_packs()
        if operation == "provision" and self._setup_region_queue:
            if not success and self._discover_map_packs()[0] is None:
                # The tools themselves have gone away (checkout moved, drive
                # unmounted). Draining avoids a modal dialog per queued region.
                self._setup_region_queue.clear()
                return
            self._provision_next_setup_region()

    def _on_position_for_device_page(self, sample) -> None:
        """Keep the pack cutter's "around here" prefill current."""
        if sample.node_num is not None and sample.node_num == self._local_node_num:
            self._last_local_position = (sample.latitude, sample.longitude)
            self._device_page.set_local_position(sample.latitude, sample.longitude)

    def _remember_lora_config(self, summary) -> None:
        """Hold the radio's modem preset for the SIGINT tab's airtime maths."""
        self._lora_summary = summary

    def _refresh_sigint_intel(self) -> None:
        """Feed the SIGINT tab the traffic this session has heard.

        Skipped while the tab is not on screen: analysing up to a session's worth
        of packets every two seconds to update a table nobody is looking at would
        be wasted work.
        """
        if self._stack.currentWidget() is not self._sigint_page:
            return
        summary = self._lora_summary
        preset = None
        if summary is not None and getattr(summary, "use_preset", False):
            preset = getattr(summary, "modem_preset", None)
        self._sigint_page.set_packets(
            self._ingestor.get_recent_packets(),
            known_nodes=self._known_node_nums,
            labels=self._node_labels,
            preset=preset,
        )

    # ------------------------------------------------------------------
    # First-run setup
    # ------------------------------------------------------------------

    def maybe_run_first_run(self, force: bool = False) -> None:
        """Offer the setup wizard on a fresh install.

        Deliberately *not* called from ``__init__``: it opens a modal dialog,
        and MainWindow is constructed all over the test suite. ``app.py`` calls
        it once the window is on screen, and ``force`` lets the Map menu offer
        it again at any time.

        Not gated on "no map packs found" either — an upgrade that already has
        packs still benefits from choosing a radio, and a developer's machine
        with a full OrcMaps checkout is exactly where this needs to be
        exercisable.
        """
        settings = QSettings()
        if not force and settings.value(_SETUP_COMPLETE_KEY, False, type=bool):
            return

        tools, packs, _reason = self._discover_map_packs()
        capability = detect_capability(tools, packs)
        wizard = SetupWizard(capability, self)
        wizard.set_destination(default_map_dir())
        if self._last_local_position is not None:
            wizard.set_centre(*self._last_local_position)

        # The controller outlives the dialog, so these are wired for the
        # dialog's lifetime only. A signal left connected to a destroyed
        # widget raises the next time it fires — which here would be the next
        # time the user pressed Scan, long after setup had closed.
        ctrl = self._controller
        ctrl.serial_ports_found.connect(wizard.set_serial_ports)
        ctrl.ble_scan_finished.connect(wizard.set_ble_devices)
        try:
            # A finished setup should be remembered even if the user declined,
            # or they are asked the same question on every single launch.
            settings.setValue(_SETUP_COMPLETE_KEY, True)
            accepted = wizard.exec() == QDialog.DialogCode.Accepted
        finally:
            ctrl.serial_ports_found.disconnect(wizard.set_serial_ports)
            ctrl.ble_scan_finished.disconnect(wizard.set_ble_devices)

        if accepted:
            self._apply_setup_choice(wizard)

    def _apply_setup_choice(self, wizard: SetupWizard) -> None:
        """Apply what the wizard collected: radio first, then the regions."""
        transport, target = wizard.selected_device()
        if target:
            profile = ConnectionProfile(transport=transport)
            if transport == "ble":
                profile.ble_address = target
            else:
                profile.serial_port = target
            # Prefilled rather than auto-connected: a Bluetooth connect takes
            # ~25 s and belongs behind the user's own press of Connect.
            self._conn_bar.restore_profile(profile)
            self._status_bar.showMessage(
                f"Setup saved {transport} device {target} — press Connect to link it", 15000
            )

        capability = wizard.capability()
        queued = 0
        for region in wizard.regions():
            source = capability.source_for(int(region["max_zoom"]))
            if source is None:
                # Deepest available source is shallower than the tier asked
                # for. Say so per region rather than silently cutting a
                # coarser map under the user's chosen name.
                log.info(
                    "Setup region %s wants z%d; no source deep enough", region["name"],
                    region["max_zoom"],
                )
                self._status_bar.showMessage(
                    f"'{region['name']}' needs z{region['max_zoom']} detail, which no "
                    "available source pack has — skipped", 15000,
                )
                continue
            self._setup_region_queue.append({
                "source_manifest": source.manifest,
                "lat": region["lat"],
                "lon": region["lon"],
                "radius_km": region["radius_km"],
                "name": region["name"],
                "display_name": region["name"],
                "card_root": wizard.destination(),
            })
            queued += 1

        if queued:
            self._status_bar.showMessage(
                f"Building {queued} region pack(s) — this can take a few minutes", 15000
            )
            self._provision_next_setup_region()

    def _provision_next_setup_region(self) -> None:
        """Start the next queued region, or stop when the queue is empty.

        OrcMaps' controller runs one operation at a time, so this is driven by
        the completion signal rather than by a loop.
        """
        if not self._setup_region_queue:
            return
        self._provision_map_pack(self._setup_region_queue.pop(0))

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
        if self._export_thread is not None:
            self._status_bar.showMessage("A packet export is already in progress", 5000)
            return

        # PacketIngestor.get_recent_packets() is a bounded 10,000-packet
        # in-memory ring buffer — everything still in it is authoritative
        # (it's the exact same data ChatView/rankings use, complete and
        # with text) and always included as-is. MonitorStore only fills in
        # whatever fell out of that buffer on a long/busy session: every
        # ingested packet is durably written to the packets table, but that
        # write is asynchronous and the table has no text column at all
        # (message content lives in `messages`), so store rows are only
        # used for packets NOT already covered by the in-memory set —
        # never to replace or race against it.
        recent = self._ingestor.get_recent_packets()
        recent_keys = {(p.sender_num, p.packet_id, p.observed_at) for p in recent}
        store_rows = self._store.read_packets_as_objects(self._session.id)
        older = [p for p in store_rows if (p.sender_num, p.packet_id, p.observed_at) not in recent_keys]
        # read_packets_as_objects() returns newest-first, get_recent_packets()
        # returns insertion order — without this the merged CSV would have a
        # reverse-chronological historical section followed by a
        # chronological recent one.
        rows = sorted(older + recent, key=lambda pkt: pkt.observed_at)

        if not rows:
            self._status_bar.showMessage("Nothing to export — no packets captured yet", 5000)
            return

        # read_packets_as_objects() caps at 200,000 rows — warn rather than
        # silently truncate on the rare session that exceeds it, instead of
        # exporting a file that looks complete but isn't.
        total = self._store.packet_count(self._session.id)
        if total > len(rows):
            proceed = QMessageBox.question(
                self,
                "Partial Export",
                f"This session has captured {total} packets, more than this export "
                f"can include at once ({len(rows)} available) — the earliest packets "
                "will be missing.\n\nExport the available packets anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if proceed != QMessageBox.StandardButton.Yes:
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
            "Message content is personal — leave this out if you plan to share the file.\n\n"
            "Text is only filled in for packets still held in this session's "
            "in-memory buffer (the most recent ~10,000) — older rows in this "
            "export will have it blank.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

        # CSV-formatting and writing up to 200,000 rows is real, unbounded
        # disk I/O — done on a background QThread so it can't freeze the
        # GUI (input, repaints) for as long as it takes on a large session.
        # Not parented to `self`/MainWindow: run() is a blocking write, so
        # if the window closes before it finishes, closeEvent must be able
        # to wait it out without this thread being a child object Qt might
        # try to tear down mid-run.
        self._status_bar.showMessage(f"Exporting {len(rows)} packet(s)…")
        self._export_pkts_act.setEnabled(False)
        self._export_thread = QThread()
        self._export_worker = _PacketExportWorker(rows, Path(path), include_text)
        self._export_worker.moveToThread(self._export_thread)
        self._export_thread.started.connect(self._export_worker.run)
        self._export_worker.finished.connect(self._on_packet_export_finished)
        self._export_worker.failed.connect(self._on_packet_export_failed)
        self._export_worker.finished.connect(self._export_thread.quit)
        self._export_worker.failed.connect(self._export_thread.quit)
        # deleteLater from the object's OWN thread while its event loop is
        # still running it (worker: still on _export_thread when finished/
        # failed fires; thread: on the GUI thread when its `finished` fires)
        # — not from _on_packet_export_thread_finished after the fact, which
        # runs after the worker's thread affinity/event loop is already gone.
        self._export_worker.finished.connect(self._export_worker.deleteLater)
        self._export_worker.failed.connect(self._export_worker.deleteLater)
        self._export_thread.finished.connect(self._export_thread.deleteLater)
        self._export_thread.finished.connect(self._on_packet_export_thread_finished)
        self._export_thread.start()

    def _on_packet_export_finished(self, count: int) -> None:
        self._status_bar.showMessage(f"Exported {count} packet(s)", 8000)

    def _on_packet_export_failed(self, message: str) -> None:
        log.error("Packet export failed: %s", message)
        QMessageBox.warning(self, "Export Failed", f"Could not write the file:\n{message}")

    def _on_packet_export_thread_finished(self) -> None:
        # Runs after both finished/failed have already updated the status
        # bar — this only clears the tracking references and re-enables the
        # menu action, regardless of which outcome occurred. deleteLater
        # for both objects is already wired above; don't call it again here.
        self._export_worker = None
        self._export_thread = None
        self._export_pkts_act.setEnabled(True)

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
            f"OrcMesh {VERSION}",
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
            "About OrcMesh",
            f"<b>OrcMesh</b><br>LoRa Mesh Operations Console<br>Version {VERSION}<br><br>"
            "Map, monitor, and message your Meshtastic network from Windows.<br><br>"
            "OrcMesh connects to one nearby Meshtastic radio.<br>"
            "The radio — not the PC — sends and receives LoRa packets.<br><br>"
            "© 2026  GPLv3 (via Meshtastic Python library)",
        )

    # ------------------------------------------------------------------
    # Controller slots
    # ------------------------------------------------------------------

    def _on_state_changed(self, state: ConnectionState, detail: str) -> None:
        self._conn_bar.set_state(state, detail)
        self._monitor_page.set_connection_state(state)
        self._status_bar.showMessage(self._status_message(state, detail))
        self._is_connected = state == ConnectionState.CONNECTED
        if not self._is_connected:
            self._chat_view.set_send_enabled(False)

    def _status_message(self, state: ConnectionState, detail: str) -> str:
        """Status-bar text for a connection state.

        The CONNECTING case spells out how long Bluetooth takes: discovery plus
        pairing plus config sync runs into tens of seconds, and "Connecting to
        <address>…" on its own for that long is indistinguishable from a
        connection that has already failed silently.
        """
        message = {
            ConnectionState.DISCONNECTED:  "Disconnected",
            ConnectionState.SCANNING:      "Scanning for BLE devices…",
            ConnectionState.CONNECTING:    f"Connecting to {detail}…",
            ConnectionState.SYNCING:       "Downloading radio configuration…",
            ConnectionState.CONNECTED:     f"Connected — {detail}",
            ConnectionState.DISCONNECTING: "Disconnecting…",
            ConnectionState.ERROR:         f"Error: {detail}",
        }.get(state, state.value)
        if state is ConnectionState.CONNECTING:
            expectation = self._conn_bar.connecting_expectation()
            if expectation:
                message += f"  ({expectation})"
        return message

    def _on_connected(self, summary) -> None:
        name = summary.long_name or summary.short_name or summary.node_id or "Radio"
        self._conn_bar.set_device_name(name)
        self._monitor_page.set_connection_state(ConnectionState.CONNECTED, name)
        self._monitor_page.set_local_node(summary.node_num)
        self._channel_list.set_local_node(summary.node_num)
        self._local_node_num = summary.node_num
        # Prefill the Maps tab's pack cutter from this radio's last known
        # position, so it is usable before any live position arrives.
        if summary.node_num is not None:
            latest = self._store.read_latest_position(summary.node_num)
            if latest and latest.get("latitude") is not None and latest.get("longitude") is not None:
                self._device_page.set_local_position(latest["latitude"], latest["longitude"])
        self._status_bar.showMessage(f"Connected to {name}")
        # Stamp the session with transport details from the active profile.
        if self._supervisor._profile is not None:
            p = self._supervisor._profile
            self._session.transport = p.transport
            self._session.connection_target = p.connection_target
            self._store.save_session(self._session)

    def _on_disconnected(self, reason: str) -> None:
        self._conn_bar.set_device_name("")
        self._channel_list.clear_channels()
        self._channel_list.set_local_node(None)
        self._channel_list.set_dm_nodes([])
        self._chat_view.clear_for_disconnect()
        self._monitor_page.clear_map()
        self._monitor_page.set_local_node(None)
        self._monitor_page.on_local_telemetry(None, None, None)
        self._local_node_num = None
        self._device_page.set_connected(False)
        self._device_snapshot = None
        self._status_bar.showMessage(f"Disconnected — {reason}")
        if self._pending_flash is not None:
            bundle, full_install, expected_usb = self._pending_flash
            self._pending_flash = None
            QTimer.singleShot(
                500,
                lambda: self._firmware_controller.flash(
                    bundle, self._flash_port or "", full_install, expected_usb
                ),
            )
        elif self._pending_probe is not None:
            expected_usb = self._pending_probe
            self._pending_probe = None
            QTimer.singleShot(
                500,
                lambda: self._firmware_controller.probe(
                    self._flash_port or "", expected_usb
                ),
            )
        elif self._pending_flash_backup is not None:
            destination, expected_usb = self._pending_flash_backup
            self._pending_flash_backup = None
            QTimer.singleShot(
                500,
                lambda: self._firmware_controller.backup(
                    self._flash_port or "", destination, expected_usb
                ),
            )
        elif self._pending_serial_console is not None:
            port, baud = self._pending_serial_console
            self._pending_serial_console = None
            QTimer.singleShot(500, lambda: self._start_serial_console(port, baud))

    def _on_device_operation(self, _operation: str, detail: str) -> None:
        self._device_page.show_operation(detail)
        self._status_bar.showMessage(detail, 6000)

    def _on_device_controls_updated(self, snapshot) -> None:
        self._device_snapshot = snapshot
        self._device_page.set_snapshot(snapshot)

    def _on_firmware_flash_requested(self, bundle, full_install: bool, expected_usb) -> None:
        snapshot = self._device_snapshot
        if not self._is_connected or snapshot is None or not snapshot.serial_port:
            QMessageBox.warning(self, "USB Radio Required", "Reconnect the radio over USB before flashing.")
            return
        self._flash_port = snapshot.serial_port
        self._pending_flash = (bundle, full_install, expected_usb)
        self._supervisor.cancel()
        self._status_bar.showMessage("Releasing the USB port for firmware flashing…")
        self._controller.disconnect()

    def _release_for_firmware(self, detail: str) -> bool:
        snapshot = self._device_snapshot
        if not self._is_connected or snapshot is None or not snapshot.serial_port:
            QMessageBox.warning(
                self, "USB Radio Required", "Reconnect the radio over USB before continuing."
            )
            return False
        self._flash_port = snapshot.serial_port
        self._supervisor.cancel()
        self._status_bar.showMessage(detail)
        return True

    def _on_firmware_probe_requested(self, expected_usb) -> None:
        if self._release_for_firmware("Releasing USB for device probe…"):
            self._pending_probe = expected_usb
            self._controller.disconnect()

    def _on_firmware_backup_requested(self, destination: str, expected_usb) -> None:
        if self._release_for_firmware("Releasing USB for raw flash backup…"):
            self._pending_flash_backup = (destination, expected_usb)
            self._controller.disconnect()

    def _on_serial_console_requested(self, port: str, baud: int) -> None:
        snapshot = self._device_snapshot
        if not self._is_connected or snapshot is None or snapshot.serial_port != port:
            QMessageBox.warning(self, "USB Radio Required", "Reconnect the selected USB radio first.")
            return
        self._pending_serial_console = (port, baud)
        self._supervisor.cancel()
        self._status_bar.showMessage("Releasing USB for read-only serial console…")
        self._controller.disconnect()

    def _start_serial_console(self, port: str, baud: int) -> None:
        if self._device_page.start_serial_console(port, baud):
            self._status_bar.showMessage(f"Read-only serial console open on {port}")
        else:
            self._controller.connect_serial(port)

    def _on_serial_console_stopped(self, port: str) -> None:
        if port:
            QTimer.singleShot(500, lambda: self._controller.connect_serial(port))

    def _on_firmware_completed(self, operation: str, success: bool, detail: str) -> None:
        self._device_page.firmware_completed(operation, success, detail)
        self._status_bar.showMessage(detail, 10000)
        if operation in ("flash", "probe", "backup_flash") and self._flash_port:
            port = self._flash_port
            self._flash_port = None
            wait = 7000 if operation == "flash" and success else 1500
            QTimer.singleShot(wait, lambda: self._controller.connect_serial(port))

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

    def _on_message_status(self, local_id: str, packet_id: int | None, status: MessageStatus, detail: str) -> None:
        self._chat_view.update_message_status(local_id, packet_id, status)
        self._store.update_message_status(local_id, packet_id, status)

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
        self._controller.send_channel_text(text, channel_index, msg.local_id)

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
        self._controller.send_direct_text(text, destination_num, msg.local_id)

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
        # node_updated fires on every ingested packet (see ARCHITECTURE.md),
        # so on a busy mesh this can be many times a second. MonitorPage
        # already coalesces its own repaint behind a periodic timer
        # (on_node_updated just stores the dict entry); do the same here
        # instead of rebuilding the whole Nodes table (a full model reset,
        # clearing/reapplying selection) and the DM sidebar synchronously
        # per packet.
        self._monitor_page.on_node_updated(snapshot)
        self._nodes_dirty = True

    def _flush_node_updates(self) -> None:
        if not self._nodes_dirty:
            return
        self._nodes_dirty = False
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
    # Connection supervisor handlers
    # ------------------------------------------------------------------

    def _on_connect_tcp_requested(self, host: str, port: int) -> None:
        self._supervisor.set_profile(ConnectionProfile(transport="tcp", tcp_host=host, tcp_port=port))
        self._controller.connect_tcp(host, port)

    def _on_connect_ble_requested(self, address: str) -> None:
        self._supervisor.set_profile(ConnectionProfile(transport="ble", ble_address=address))
        self._controller.connect_ble(address)

    def _on_connect_serial_requested(self, port: str) -> None:
        self._supervisor.set_profile(ConnectionProfile(transport="serial", serial_port=port))
        self._controller.connect_serial(port)

    def _on_disconnect_requested(self) -> None:
        self._supervisor.cancel()
        self._controller.disconnect()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        settings = QSettings()
        settings.setValue(f"{_SETTINGS_KEY}/geometry", self.saveGeometry())
        self._spectrum_page.shutdown()
        self._sigint_page.shutdown()
        self._firmware_controller.shutdown()
        # Stops the offline basemap's tile server thread (OrcMaps renders).
        self._monitor_page.shutdown_map()
        # Terminates any running OrcMaps pack build rather than blocking exit.
        self._orcmaps.shutdown()
        self._controller.shutdown()
        self._store.shutdown()
        if self._export_thread is not None:
            # Closing mid-export: block until the write actually finishes
            # rather than destroying a still-running QThread out from under
            # it, which Qt warns about and can crash on some platforms.
            #
            # The explicit quit() here is required, not redundant with the
            # worker's finished/failed -> thread.quit connections: those are
            # QUEUED (the QThread object lives on this GUI thread, the
            # worker emits from _export_thread), so they only get delivered
            # once THIS thread's event loop is pumping — which it isn't
            # while blocked in wait() below. Without this direct call, a
            # write that finishes during that wait() would leave the
            # worker thread parked in exec() forever with nothing left to
            # ever tell it to quit, hanging app shutdown indefinitely.
            # quit() itself does not interrupt the blocking write in
            # progress — only ensures the thread doesn't idle in its event
            # loop once that write actually returns — so this still waits
            # as long as the write itself takes.
            self._export_thread.quit()
            self._export_thread.wait()
        super().closeEvent(event)
