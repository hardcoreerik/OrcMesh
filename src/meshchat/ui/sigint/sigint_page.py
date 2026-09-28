"""The SIGINT tab: what is on the air around me.

Three panes, in the order they make sense to read:

1. **Spectrum** — a live waterfall of the band from an RTL-SDR, in 2D with the 3D
   surface docked beside it. It opens on 906.875 MHz (US slot 19 at 250 kHz) over
   ±1 MHz.
2. **Band survey** — the whole region swept and stitched, with the mesh's channel
   slots ranked by how much energy each holds.
3. **Packet intelligence** — per-node airtime, signal trend and routing, plus any
   radio heard that this mesh has no history with. This pane needs no SDR at all:
   it comes from the connected radio, so the tab is useful even with no dongle
   plugged in.

Receive-only throughout. Nothing here transmits, and nothing tries to break
another mesh's channel key — a radio only ever decodes what its own key already
covers.

The page owns its controllers rather than borrowing MainWindow's, because the
Spectrum tab has its own and the two must not both hold the dongle. That
collision is handled in two layers: this page stops whichever of its own captures
is running before starting another, and `rtl_tools.acquire_sdr` is the backstop
that names the holder if two views ever do collide.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meshchat.analytics.lora_airtime import ModemParams, params_for_preset
from meshchat.analytics.lora_bands import MESHTASTIC_REGIONS, meshtastic_markers
from meshchat.analytics.packet_intel import TREND_RISING, IntelReport, analyse_packets
from meshchat.analytics.slot_occupancy import OccupancyReport, rank_slots
from meshchat.analytics.sdr_presets import (
    PRESETS,
    SdrPreset,
    default_preset,
    get_preset,
)
from meshchat.models.network_packet import NetworkPacket
from meshchat.services import rtl_tools
from meshchat.services.iq_recorder import (
    CaptureInfo,
    estimated_bytes_per_second,
    format_duration,
    format_size,
)
from meshchat.services.rf import pluto_tx
from meshchat.services.rtl_scan import BandPicture, ScanController, ScanRequest
from meshchat.services.sigint_tuning import SigintTuning
from meshchat.services.sdr_source import SdrController
from meshchat.ui.sigint.analysis_view import AnalysisView
from meshchat.ui.sigint.override_control import OverrideController, OverrideWarning
from meshchat.ui.sigint.waterfall3d_view import Waterfall3DView
from meshchat.ui.spectrum.waterfall_view import WaterfallView
from meshchat.ui.widgets.levels_control import LevelsControl

log = logging.getLogger(__name__)

#: Rows shown in the intelligence table. A busy mesh can carry hundreds of nodes
#: and the question is always "who is loudest", which is the top of the list.
MAX_INTEL_ROWS = 40

#: Slots listed in the occupancy table before it stops being readable.
MAX_SLOT_ROWS = 14

#: Where the tab opens. 906.875 MHz is US channel slot 19 at 250 kHz bandwidth —
#: the LONG_FAST preset a US mesh defaults to: 902 + 0.125 + 19 x 0.25.
_DEFAULT_CENTER_MHZ = 906.875

#: The sample rate *is* the displayed span here, so 2.0 MS/s is exactly ±1 MHz
#: around the centre. Measured on this machine's Blog V4: 2.0 MS/s tunes and
#: streams cleanly (rtl_sdr reports 2000000.05 Hz and wrote 8,000,000 bytes for
#: 2 s with no dropped-sample warning), so the narrow span costs nothing.
_DEFAULT_RATE_MSPS = 2.0

#: The allocation the default centre is a channel of, selected at startup so the
#: slot ranking and the display agree about which band is being watched.
_DEFAULT_REGION = "US"

_DEFAULT_GAIN_DB = rtl_tools.DEFAULT_GAIN_DB


class SigintPage(QWidget):
    """Receive-only signal intelligence: spectrum, band survey, packet intel."""

    def __init__(self, parent=None, *, store=None):
        super().__init__(parent)

        #: Optional settings store. Kept optional so the page works in tests and in any
        #: context without persistence, and so a failure to reach the store can never stop a
        #: capture from starting.
        self._store = store
        self._sdr: SdrController | None = None
        self._scan: ScanController | None = None
        self._capturing = False
        self._scanning = False
        self._recording = False
        self._last_capture: CaptureInfo | None = None
        # Constructed before the toolbar, because building the toolbar is what connects
        # the button to it.
        self._override = OverrideController(on_state=self._on_override_state)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_toolbar())
        layout.addWidget(self._build_waterfall(), 3)
        layout.addWidget(self._build_analysis(), 2)
        layout.addWidget(self._build_survey(), 2)
        layout.addWidget(self._build_intel(), 2)

        self._refresh_availability()
        self._show_startup_status()
        self._refresh_recording_hint()
        # Last, and silently: the controls already default to this preset's numbers,
        # and applying it is what lays its channel markers on the waterfall. The
        # status line stays with the toolchain summary rather than the preset note,
        # which the operator gets the moment they pick a preset themselves.
        self._apply_preset(default_preset(), announce=False)
        self._restore_tuning()

    # ------------------------------------------------------------------
    # Remembering the tuning
    # ------------------------------------------------------------------

    def _restore_tuning(self) -> None:
        """Put the controls back where the last session left them, if it left them anywhere.

        Applied control by control rather than wholesale, because a stored region or preset
        that no longer exists must not take the centre frequency down with it.
        """
        if self._store is None:
            return
        tuning = SigintTuning.load(self._store)
        if tuning is None:
            return
        self._center.setValue(tuning.centre_mhz)
        self._rate.setValue(tuning.rate_msps)
        self._gain.setValue(tuning.gain_db)
        if tuning.region:
            index = self._region.findText(tuning.region)
            if index >= 0:
                self._region.setCurrentIndex(index)
        if tuning.preset:
            index = self._preset.findText(tuning.preset)
            if index >= 0:
                self._preset.setCurrentIndex(index)
        log.debug("Restored SIGINT tuning: %s", tuning.describe())

    def _remember_tuning(self) -> None:
        """Store what is on the controls now. Called when a capture starts, not per keystroke."""
        if self._store is None:
            return
        SigintTuning(
            centre_mhz=self._center.value(),
            rate_msps=self._rate.value(),
            gain_db=self._gain.value(),
            region=self._region.currentText(),
            preset=self._preset.currentText(),
        ).save(self._store)
    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background: #0D1530; border-bottom: 1px solid #1A2448;")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        title = QLabel("SIGINT")
        title.setObjectName("monitorTitle")
        row.addWidget(title)

        # The preset is the answer to the four controls that follow, so it comes
        # first: pick a network and the centre, span, gain, region and channel
        # markers are all set to something that actually suits it.
        row.addWidget(QLabel("Preset:"))
        self._preset = QComboBox()
        self._preset.setFixedWidth(230)
        for preset in PRESETS:
            self._preset.addItem(preset.label, userData=preset.key)
        #: Sentinel last entry: selected whenever a tuning control is edited by
        #: hand, so the label never claims a preset the tab is no longer set up for.
        self._preset.addItem("Custom", userData=None)
        self._preset.currentIndexChanged.connect(self._on_preset_changed)
        row.addWidget(self._preset)

        # Which dongle this view captures from. The list is filled on demand rather
        # than here: enumerating means running rtl_test, which opens a device and
        # costs seconds, and building a page should not touch hardware.
        row.addWidget(QLabel("Dongle:"))
        self._dongle = QComboBox()
        self._dongle.setFixedWidth(150)
        self._dongle.addItem("0", userData=0)
        self._dongle.setToolTip(
            "Which RTL-SDR to capture from. Press ↻ to list what is attached."
        )
        row.addWidget(self._dongle)

        self._dongle_btn = QPushButton("↻")
        self._dongle_btn.setFixedWidth(30)
        self._dongle_btn.setToolTip("List the attached dongles")
        self._dongle_btn.clicked.connect(self._refresh_dongles)
        row.addWidget(self._dongle_btn)

        row.addWidget(QLabel("Region:"))
        self._region = QComboBox()
        self._region.setFixedWidth(110)
        for key in MESHTASTIC_REGIONS:
            self._region.addItem(key, userData=key)
        # The default centre is a US channel, so open on the US allocation rather
        # than whichever key happens to be first in the band table.
        us = self._region.findData(_DEFAULT_REGION)
        if us >= 0:
            self._region.setCurrentIndex(us)
        row.addWidget(self._region)

        row.addWidget(QLabel("Centre (MHz):"))
        self._center = QDoubleSpinBox()
        self._center.setDecimals(4)
        self._center.setRange(24.0, 1766.0)
        self._center.setValue(_DEFAULT_CENTER_MHZ)
        # Steps by one 250 kHz slot, so the arrows walk the mesh's channel plan
        # instead of landing between channels.
        self._center.setSingleStep(0.25)
        self._center.setFixedWidth(110)
        self._center.setToolTip(
            f"The default, {_DEFAULT_CENTER_MHZ:g} MHz, is US channel slot 19 at "
            "250 kHz (the LONG_FAST preset): 902 + 0.125 + 19 x 0.25."
        )
        row.addWidget(self._center)

        row.addWidget(QLabel("Rate (MS/s):"))
        self._rate = QDoubleSpinBox()
        self._rate.setDecimals(2)
        self._rate.setRange(0.25, 3.2)
        self._rate.setValue(_DEFAULT_RATE_MSPS)
        self._rate.setFixedWidth(80)
        # The rate is the span: the waterfall's x-axis is centre ± rate/2, so this
        # is the control that sets how many MHz are on screen.
        self._rate.setToolTip(
            "Sample rate, and therefore the displayed span: the view is "
            "centre ± rate/2. 2.00 MS/s shows ±1 MHz."
        )
        row.addWidget(self._rate)

        row.addWidget(QLabel("Gain (dB):"))
        self._gain = QDoubleSpinBox()
        self._gain.setDecimals(1)
        self._gain.setRange(0.0, 49.6)
        # Not automatic: auto resolves to near-maximum, which is the worst case
        # for headroom — measured on a Blog V4, auto put the noise floor at
        # +17 dB against -17 dB at 3.7 dB.
        self._gain.setValue(_DEFAULT_GAIN_DB)
        self._gain.setFixedWidth(70)
        self._gain.setToolTip(
            "Tuner gain. The tuner snaps to its own steps, so the nearest of "
            "these is what you get: "
            + ", ".join(f"{step:g}" for step in rtl_tools.R820T_GAIN_STEPS)
        )
        row.addWidget(self._gain)

        self._capture_btn = QPushButton("Start")
        self._capture_btn.setFixedWidth(70)
        self._capture_btn.clicked.connect(self._on_capture_clicked)
        row.addWidget(self._capture_btn)

        self._record_btn = QPushButton("Record")
        self._record_btn.setFixedWidth(76)
        self._record_btn.clicked.connect(self._on_record_clicked)
        row.addWidget(self._record_btn)

        # The only control here that puts energy on the air. Its warning is in
        # override_control.py; the button itself only ever asks for it.
        self._override_btn = QPushButton("Override 906.875")
        self._override_btn.setToolTip(
            "Transmit a continuous 30 s carrier on 906.875 MHz (US channel slot 19) from the "
            "Pluto's TX1, to test overriding nearby mesh radios. You are warned before "
            "anything is transmitted, and the button becomes Stop while it is running.",
        )
        self._override_btn.clicked.connect(self._on_override_clicked)
        row.addWidget(self._override_btn)

        # Its own label: the countdown rewrites often while a tone runs, and sharing
        # _status would erase whatever capture state was being shown.
        self._override_lbl = QLabel("")
        self._override_lbl.setStyleSheet("color: #FFB800; font-size: 11px;")
        row.addWidget(self._override_lbl)

        row.addStretch()

        self._probe_btn = QPushButton("Check dongle")
        self._probe_btn.clicked.connect(self._on_probe)
        row.addWidget(self._probe_btn)

        self._status = QLabel("")
        self._status.setStyleSheet("color: #5A6690; font-size: 11px;")
        row.addWidget(self._status)

        # Editing any tuning control by hand means the combo's label is no longer
        # true, so it drops back to Custom. It keeps whatever channel markers the
        # last preset laid down: those are a reference overlay, not a claim about
        # where the capture is tuned.
        self._center.valueChanged.connect(self._on_manual_tuning_change)
        self._rate.valueChanged.connect(self._on_manual_tuning_change)
        self._gain.valueChanged.connect(self._on_manual_tuning_change)
        self._region.currentIndexChanged.connect(self._on_manual_tuning_change)
        return bar

    def _build_waterfall(self) -> QWidget:
        """The waterfall pane: the 2D waterfall and the 3D surface, docked together.

        The 3D surface is a permanent part of the pane, not a window to open and
        close. It used to be a window of its own because a GL widget forces native
        OpenGL composition on whichever top-level window holds it, and the map's
        QWebEngineView wanted Qt Quick's own composition: in one window the map
        reported "Failed to get a QRhi from the top-level widget's window" and drew
        nothing. `QSG_RHI_BACKEND=opengl` in app.py settles that — with both stacks
        on OpenGL they coexist — which is what lets the two be docked side by side.

        Docking the 3D view next to the 2D one rather than under it keeps the pane's
        height, and the levels control stays inside the left column so it is
        unambiguous which view it scales.
        """
        row = QSplitter(Qt.Orientation.Horizontal)
        row.setChildrenCollapsible(False)

        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.setSpacing(0)
        self._waterfall2d = WaterfallView()
        left_box.addWidget(self._waterfall2d, 1)
        # Display range sits with the waterfall it applies to. The amplitude
        # window is what turns a saturated block of one colour back into a
        # spectrum — see LevelsControl's docstring.
        left_box.addWidget(LevelsControl(self._waterfall2d))
        row.addWidget(left)

        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.setSpacing(0)

        bar = QWidget()
        bar.setStyleSheet("background: #0D1530; border-bottom: 1px solid #1A2448;")
        bar_row = QHBoxLayout(bar)
        bar_row.setContentsMargins(8, 6, 8, 6)
        label = QLabel("3D SPECTRUM")
        label.setObjectName("monitorTitle")
        bar_row.addWidget(label)
        bar_row.addStretch()
        right_box.addWidget(bar)

        # Built here, but with no GL surface and no timer: the surface only appears
        # in activate(), once this tab is on screen. Constructing the object is
        # harmless — the GL context is what causes trouble, not the object.
        self._waterfall3d = Waterfall3DView()
        right_box.addWidget(self._waterfall3d, 1)
        row.addWidget(right)

        row.setSizes([620, 520])
        return row

    def showEvent(self, event) -> None:
        """The docked 3D pane is real from the moment the tab is looked at.

        Deferred to here rather than __init__ for the composition reason spelled out
        in _build_waterfall: a run that never opens this tab never touches OpenGL.
        """
        super().showEvent(event)
        self._activate_3d()

    def _build_analysis(self) -> QWidget:
        """The five analysis modes over the same capture the waterfall is showing.

        Its own panel rather than more controls in the toolbar, because each mode is a picture
        that needs the room; the mode selector is inside it so switching costs one click rather
        than a screen of choices.
        """
        panel = QWidget()
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._analysis = AnalysisView()
        outer.addWidget(self._analysis)
        return panel

    def _build_survey(self) -> QWidget:
        panel = QWidget()
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QWidget()
        bar.setStyleSheet("background: #0D1530; border-bottom: 1px solid #1A2448;")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        label = QLabel("BAND SURVEY")
        label.setObjectName("monitorTitle")
        row.addWidget(label)

        self._scan_btn = QPushButton("Scan region")
        self._scan_btn.clicked.connect(self._on_scan_clicked)
        row.addWidget(self._scan_btn)

        row.addWidget(QLabel("Bin (kHz):"))
        self._bin = QDoubleSpinBox()
        self._bin.setDecimals(0)
        # Well below a 125 kHz LoRa slot, so slots can actually be told apart:
        # the field is a maximum and finer bins are used when geometry allows.
        self._bin.setRange(5.0, 2500.0)
        self._bin.setValue(50.0)
        self._bin.setFixedWidth(70)
        row.addWidget(self._bin)

        row.addStretch()

        self._coverage = QLabel("")
        self._coverage.setStyleSheet("color: #00D4FF; font-size: 11px;")
        row.addWidget(self._coverage)

        self._scan_status = QLabel("")
        self._scan_status.setStyleSheet("color: #5A6690; font-size: 11px;")
        row.addWidget(self._scan_status)
        outer.addWidget(bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._band_plot = pg.PlotWidget()
        self._band_plot.setLabel("bottom", "Frequency", units="MHz")
        self._band_plot.setLabel("left", "Power", units="dB")
        self._band_plot.showGrid(x=True, y=True, alpha=0.2)
        self._band_curve = self._band_plot.plot(pen=pg.mkPen("#00D4FF", width=1))
        splitter.addWidget(self._band_plot)

        self._slot_table = QTableWidget(0, 5)
        self._slot_table.setHorizontalHeaderLabels(
            ["Slot", "MHz", "Peak", "Above floor", "State"]
        )
        self._slot_table.verticalHeader().setVisible(False)
        self._slot_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._slot_table.horizontalHeader().setStretchLastSection(True)
        splitter.addWidget(self._slot_table)
        splitter.setSizes([640, 460])
        outer.addWidget(splitter, 1)
        return panel

    def _build_intel(self) -> QWidget:
        panel = QWidget()
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QWidget()
        bar.setStyleSheet("background: #0D1530; border-bottom: 1px solid #1A2448;")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        label = QLabel("PACKET INTELLIGENCE")
        label.setObjectName("monitorTitle")
        row.addWidget(label)

        self._intel_note = QLabel("Needs no SDR — this comes from the connected radio.")
        self._intel_note.setStyleSheet("color: #5A6690; font-size: 11px;")
        row.addWidget(self._intel_note)

        row.addStretch()

        self._intel_summary = QLabel("")
        self._intel_summary.setStyleSheet("color: #5A6690; font-size: 11px;")
        row.addWidget(self._intel_summary)
        outer.addWidget(bar)

        self._intel_table = QTableWidget(0, 9)
        self._intel_table.setHorizontalHeaderLabels([
            "Node", "Airtime", "Share", "Pkts", "Pkts/min",
            "SNR", "Trend", "Routing", "Ports",
        ])
        self._intel_table.verticalHeader().setVisible(False)
        self._intel_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._intel_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        outer.addWidget(self._intel_table, 1)
        return panel

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------

    def _on_preset_changed(self, _index: int) -> None:
        key = self._preset.currentData()
        if key is None:
            return  # Custom: the operator is driving the controls
        preset = get_preset(str(key))
        if preset is not None:
            self._apply_preset(preset, announce=True)

    def _apply_preset(self, preset: SdrPreset, *, announce: bool) -> None:
        """Point the whole tab at one network.

        The controls are written with their signals blocked: each setValue would
        otherwise look like the operator editing a control by hand and knock the
        combo straight back to Custom.

        Nothing here touches the display ranges. The 2D waterfall fits its own dB
        window and the 3D surface anchors to the measured noise floor, so a preset
        that also set a level would be fighting both and would go stale the moment
        the gain changed.
        """
        controls = (self._region, self._center, self._rate, self._gain)
        for widget in controls:
            widget.blockSignals(True)
        try:
            if preset.region is not None:
                # Only Meshtastic presets carry one: the region menu drives the slot
                # ranking below, which means nothing for a single-frequency network.
                index = self._region.findData(preset.region)
                if index >= 0:
                    self._region.setCurrentIndex(index)
            self._center.setValue(preset.center_mhz)
            self._rate.setValue(preset.span_mhz)
            self._gain.setValue(preset.gain_db)
        finally:
            for widget in controls:
                widget.blockSignals(False)

        self._waterfall2d.set_markers(list(preset.markers))
        self._preset.setToolTip(preset.note)
        self._refresh_recording_hint()
        if announce:
            self._set_status(preset.describe())

    def _on_manual_tuning_change(self, *_args) -> None:
        """Stop claiming a preset once a control has been edited by hand."""
        custom = self._preset.count() - 1
        if self._preset.currentIndex() == custom:
            return
        self._preset.blockSignals(True)
        try:
            self._preset.setCurrentIndex(custom)
        finally:
            self._preset.blockSignals(False)

    # ------------------------------------------------------------------
    # Availability and status
    # ------------------------------------------------------------------

    def _refresh_availability(self) -> None:
        """Enable what can run, and only speak up when something cannot.

        Writing the toolchain summary unconditionally here wiped out whatever the
        last action had just said — a capture error would flash up and be replaced
        by "rtl_sdr found at ..." before it could be read. The summary is shown
        once at startup instead.
        """
        available, reason = self._sdr_state()
        self._capture_btn.setEnabled(available)
        self._scan_btn.setEnabled(available)
        self._record_btn.setEnabled(available)
        if not available:
            self._set_status(reason)

    def _show_startup_status(self) -> None:
        available, reason = self._sdr_state()
        if available:
            self._set_status(reason)

    @staticmethod
    def _sdr_state() -> tuple[bool, str]:
        """(available, reason) — whether the toolchain is present.

        Deliberately says nothing about the dongle lease: this page is often the
        holder, and disabling its own buttons because it holds the dongle would be
        nonsense. A real collision with another view is reported by the lease when
        the capture starts, which is the moment there is something to say.
        """
        return rtl_tools.tools_available()

    def _selected_device(self) -> int:
        """Index of the dongle the selector is on."""
        value = self._dongle.currentData()
        return int(value) if value is not None else 0

    def _refresh_dongles(self) -> None:
        """Re-list the attached dongles, on demand.

        Synchronous, like the device check next to it: this runs only when the
        operator asks, and it is the one action that has to open hardware to answer.
        """
        devices, message = rtl_tools.list_devices()
        chosen = self._selected_device()
        self._dongle.blockSignals(True)
        try:
            self._dongle.clear()
            if not devices:
                # Never leave the control empty: an absent dongle is a reason to keep
                # device 0 selectable and say so, not to remove the choice.
                self._dongle.addItem("0 (none detected)", userData=0)
            for device in devices:
                self._dongle.addItem(device.label, userData=device.index)
            index = self._dongle.findData(chosen)
            self._dongle.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._dongle.blockSignals(False)
        # The serials live in the tooltip: two dongles on this machine report the same
        # one, so the labels must lead with the index and stay short.
        self._dongle.setToolTip(
            "\n".join(device.describe() for device in devices) or message
        )
        self._set_status(message)

    def _refresh_recording_hint(self) -> None:
        """Say what a recording will cost before it is started."""
        per_second = estimated_bytes_per_second(self._rate.value() * 1e6)
        self._record_btn.setToolTip(
            f"Record raw I/Q: {format_size(per_second)}/s "
            f"({format_size(per_second * 60)} per minute)"
        )

    def _set_status(self, message: str) -> None:
        self._status.setText(message)

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _ensure_capture(self) -> SdrController:
        if self._sdr is None:
            self._sdr = SdrController(self, label="the SIGINT spectrum")
            self._sdr.row_ready.connect(self._on_row)
            self._sdr.started.connect(self._on_capture_started)
            self._sdr.stopped.connect(self._on_capture_stopped)
            self._sdr.error.connect(self._on_capture_error)
            self._sdr.recording_finished.connect(self._on_recording_finished)
            self._sdr.recording_failed.connect(self._on_recording_failed)
        return self._sdr

    def _on_capture_clicked(self) -> None:
        if self._capturing:
            self._stop_capture()
            return
        # One dongle, so anything else of ours holding it has to let go first.
        if self._scanning:
            self._stop_scan("stopped the band scan to free the dongle")
        center_hz = self._center.value() * 1e6
        rate_hz = self._rate.value() * 1e6
        self._waterfall2d.configure(center_hz, rate_hz, 1024)
        self._waterfall3d.configure(center_hz, rate_hz, 1024)
        # The docked 3D pane draws from the first row, whether or not the tab has
        # been shown yet (the timer starts here if it is not already running).
        self._activate_3d()
        self._ensure_capture().start(
            center_hz, rate_hz, self._gain.value(), self._selected_device()
        )
        self._capturing = True
        self._capture_btn.setText("Stop")

    def _stop_capture(self) -> None:
        if self._sdr is not None:
            self._sdr.stop()
        self._capturing = False
        self._capture_btn.setText("Start")
        if self._recording:
            self._recording = False
            self._record_btn.setText("Record")
        self._refresh_availability()

    def _on_row(self, row: np.ndarray) -> None:
        self._waterfall2d.push_row(row)
        self._waterfall3d.push_row(row)
        # The analysis modes fold the same rows the waterfall draws, so they describe what is
        # actually being captured rather than a second, separately-tuned receiver.
        self._analysis.push_row(row)

    def _on_capture_started(self, center_hz: float, rate_hz: float, bins: int) -> None:
        self._set_status(
            f"capturing {center_hz / 1e6:.3f} MHz at {rate_hz / 1e6:.2f} MS/s, {bins} bins"
        )
        # A new capture means a new centre and span, so the accumulated history describes a
        # different part of the band and must not be carried over.
        self._analysis.configure(center_hz, rate_hz, bins)
        self._remember_tuning()
        self._refresh_availability()

    def _on_capture_stopped(self, message: str) -> None:
        self._capturing = False
        self._capture_btn.setText("Start")
        self._set_status(message)
        # Redrawing stops with the capture, which leaves the last surface on screen
        # instead of clearing it — the docked pane keeps showing what was heard.
        self._waterfall3d.stop()
        self._refresh_availability()

    def _on_capture_error(self, message: str) -> None:
        self._capturing = False
        self._capture_btn.setText("Start")
        self._set_status(message.splitlines()[0])
        self._refresh_availability()

    def _activate_3d(self) -> bool:
        """Bring the docked 3D surface up, or explain inside the pane why not.

        The only reason this is not called from __init__ is composition: the GL
        widget is what forces native OpenGL on the main window. `activate()` is
        idempotent — it restarts the redraw timer when the surface already exists —
        and it puts its own explanation in the pane when OpenGL is unusable, so a
        failure needs no status line of its own.
        """
        return self._waterfall3d.activate()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _on_override_state(self, controller) -> None:
        """Keep the button and the countdown describing what the transmitter is doing.

        The button becomes Stop while a carrier is on the air, so the control that started
        it is the control that ends it. There is no state where the tone is running and the
        only way to stop it is to guess.
        """
        if controller.is_transmitting:
            self._override_btn.setText("Stop")
            self._override_btn.setStyleSheet("color: #FFB800; font-weight: bold;")
            # "configured", not "on air". The attributes are written and read back, but
            # whether energy leaves the connector has NOT been confirmed on this hardware:
            # measured 2026-09-28, the DDS tone and a cyclic stream both left the spectrum
            # unchanged while every register read back exactly as written. Saying "on air"
            # would be a claim the measurement does not support.
            self._override_lbl.setText(
                f"TX configured · 906.875 MHz · {controller.remaining_s:.0f} s left",
            )
            self._override_lbl.setToolTip(
                "The transmitter's settings are written and read back, but that this is\n"
                "radiating has not been verified on this board. Confirm it with an\n"
                "independent receiver before relying on it.",
            )
        else:
            self._override_btn.setText("Override 906.875")
            self._override_btn.setStyleSheet("")
            self._override_lbl.setText("")
            self._override_lbl.setToolTip("")

    def _on_override_clicked(self) -> None:
        """Warn, then transmit — or stop if a carrier is already on the air."""
        if self._override.is_transmitting:
            self._override.stop()
            return
        if not OverrideWarning.confirm(self):
            return
        try:
            plan = self._override.start()
        except (pluto_tx.ToneFailed, pluto_tx.AlreadyTransmitting) as exc:
            QMessageBox.critical(self, "Could not transmit", str(exc))
            return
        log.warning("Override carrier on the air: %s", plan.describe())

    def _on_record_clicked(self) -> None:
        if self._recording:
            if self._sdr is not None:
                self._sdr.stop_recording()
            self._recording = False
            self._record_btn.setText("Record")
            return

        chosen, _filter = QFileDialog.getSaveFileName(
            self, "Record I/Q to", str(Path.home() / "orcmesh-capture.iq"),
            "I/Q capture (*.iq)",
        )
        if not chosen:
            return

        # Arm before starting: the recorder is opened from the settings the
        # capture actually runs with, so arming mid-capture would only take effect
        # on the next start. Restarting is the honest way to begin now.
        controller = self._ensure_capture()
        if self._capturing:
            self._stop_capture()
        controller.arm_recording(Path(chosen))
        self._on_capture_clicked()
        if not self._capturing:
            return
        self._recording = True
        self._record_btn.setText("Stop rec")
        self._set_status(f"recording to {Path(chosen).name}")

    def _on_recording_finished(self, info: CaptureInfo) -> None:
        self._last_capture = info
        self._recording = False
        self._record_btn.setText("Record")
        self._set_status(f"recorded {info.describe()}")

    def _on_recording_failed(self, message: str) -> None:
        self._recording = False
        self._record_btn.setText("Record")
        self._set_status(message.splitlines()[0])

    # ------------------------------------------------------------------
    # Band survey
    # ------------------------------------------------------------------

    def _ensure_scan(self) -> ScanController:
        if self._scan is None:
            self._scan = ScanController(self, label="the SIGINT band scan")
            self._scan.band_ready.connect(self._on_band)
            self._scan.started.connect(self._on_scan_started)
            self._scan.stopped.connect(self._on_scan_stopped)
            self._scan.error.connect(self._on_scan_error)
        return self._scan

    def _selected_region(self) -> str:
        key = self._region.currentData()
        return str(key) if key else "US"

    def _on_scan_clicked(self) -> None:
        if self._scanning:
            self._stop_scan("scan stopped")
            return
        if self._capturing:
            self._stop_capture()
            self._set_status("stopped the capture to free the dongle")

        band = MESHTASTIC_REGIONS.get(self._selected_region())
        if band is None:
            self._set_status("pick a region to scan")
            return
        request = ScanRequest(
            low_hz=band.start_mhz * 1e6,
            high_hz=band.end_mhz * 1e6,
            bin_hz=self._bin.value() * 1e3,
            gain_db=self._gain.value(),
            device_index=self._selected_device(),
        )
        self._ensure_scan().start(request)
        self._scanning = True
        self._scan_btn.setText("Stop scan")

    def _stop_scan(self, message: str = "") -> None:
        if self._scan is not None:
            self._scan.stop()
        self._scanning = False
        self._scan_btn.setText("Scan region")
        if message:
            self._set_status(message)
        self._refresh_availability()

    def _on_scan_started(self, low_hz: float, high_hz: float, bin_hz: float) -> None:
        self._set_status(
            f"scanning {low_hz / 1e6:.1f}-{high_hz / 1e6:.1f} MHz"
        )
        self._scan_status.setText(f"{bin_hz / 1e3:.1f} kHz bins")
        self._refresh_availability()

    def _on_scan_stopped(self, message: str) -> None:
        self._scanning = False
        self._scan_btn.setText("Scan region")
        self._set_status(message)
        self._refresh_availability()

    def _on_scan_error(self, message: str) -> None:
        self._scanning = False
        self._scan_btn.setText("Scan region")
        self._set_status(message.splitlines()[0])
        self._refresh_availability()

    def _on_band(self, picture: BandPicture) -> None:
        """Draw the accumulated band and rank the mesh's slots inside it."""
        row = picture.row
        frequencies = row.frequencies
        self._band_curve.setData(frequencies / 1e6, row.power_db)
        self._coverage.setText(
            f"{picture.coverage * 100:.0f}% of bins measured over {picture.sweeps} sweeps"
        )
        self._refresh_slots(row.frequencies, row.power_db)

    def _refresh_slots(self, frequencies: np.ndarray, power_db: np.ndarray) -> None:
        try:
            report = rank_slots(
                frequencies,
                power_db,
                meshtastic_markers(
                    self._selected_region(), None, 0, include_neighbours=MAX_SLOT_ROWS
                ),
            )
        except Exception:
            log.exception("Could not rank slots")
            return
        self._fill_slot_table(report)

    def _fill_slot_table(self, report: OccupancyReport) -> None:
        slots = report.slots[:MAX_SLOT_ROWS]
        self._slot_table.setRowCount(len(slots))
        for index, slot in enumerate(slots):
            if slot.has_data:
                cells = (
                    slot.label,
                    f"{slot.center_hz / 1e6:.4f}",
                    f"{slot.peak_db:.1f}",
                    f"+{slot.peak_over_floor_db:.1f}",
                    slot.status,
                )
            else:
                # "unknown" rather than a zero: a slot nobody measured is not a
                # quiet slot.
                cells = (slot.label, f"{slot.center_hz / 1e6:.4f}", "—", "—", slot.status)
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if slot.looks_busy:
                    item.setForeground(Qt.GlobalColor.yellow)
                self._slot_table.setItem(index, column, item)

    # ------------------------------------------------------------------
    # Packet intelligence
    # ------------------------------------------------------------------

    def set_packets(
        self,
        packets: Sequence[NetworkPacket],
        *,
        known_nodes: Sequence[int] = (),
        labels: Mapping[int, str] | None = None,
        preset: str | None = None,
    ) -> None:
        """Recompute the node table from what the radio has heard.

        Called by MainWindow on its own refresh tick rather than this page keeping
        a timer of its own.
        """
        if not packets:
            self._intel_summary.setText("no traffic yet")
            self._intel_table.setRowCount(0)
            return
        params: ModemParams = params_for_preset(preset)
        try:
            report = analyse_packets(
                packets, params, labels=labels, known_nodes=known_nodes,
                params_label=preset or "",
            )
        except Exception:
            log.exception("Could not analyse packets")
            return
        self._fill_intel_table(report)

    def _fill_intel_table(self, report: IntelReport) -> None:
        nodes = report.nodes[:MAX_INTEL_ROWS]
        self._intel_table.setRowCount(len(nodes))
        for index, node in enumerate(nodes):
            signal = node.signal.median_snr
            cells = (
                node.label,
                f"{node.airtime_s:.2f} s",
                f"{node.airtime_share * 100:.0f}%",
                str(node.packets),
                f"{node.packets_per_minute:.1f}",
                f"{signal:+.1f}" if signal is not None else "—",
                node.snr_trend,
                self._routing_text(node),
                node.top_portnum,
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if node.is_foreign:
                    item.setForeground(Qt.GlobalColor.yellow)
                    item.setToolTip(
                        "Heard over RF, and this mesh has no prior record of it"
                    )
                elif node.snr_trend == TREND_RISING:
                    item.setToolTip("Signal strengthening")
                self._intel_table.setItem(index, column, item)

        summary = (
            f"{report.analysed_packets} packets over "
            f"{format_duration(report.window_s)} · channel busy "
            f"{report.channel_duty_cycle * 100:.1f}%"
        )
        if report.foreign:
            summary += f" · {len(report.foreign)} unfamiliar"
        if report.overhead > 0.01:
            # Undercounts are reported, not hidden: a packet with no payload size
            # contributes no airtime.
            summary += f" · {report.overhead * 100:.0f}% of packets had no payload size"
        self._intel_summary.setText(summary)

    @staticmethod
    def _routing_text(node) -> str:
        parts = []
        if node.direct_packets:
            parts.append(f"direct x{node.direct_packets}")
        if node.relayed_packets:
            parts.append(f"relayed x{node.relayed_packets}")
        if node.via_mqtt_packets:
            parts.append(f"mqtt x{node.via_mqtt_packets}")
        return ", ".join(parts) or "—"

    # ------------------------------------------------------------------
    # Device check
    # ------------------------------------------------------------------

    def _on_probe(self) -> None:
        """Open the dongle once and report what it is.

        Bounded by a timeout inside probe_device, because this runs a real tool
        against real hardware and a stuck driver must not stick the UI.
        """
        if self._capturing or self._scanning:
            self._set_status("stop the capture before checking the dongle")
            return
        self._probe_btn.setEnabled(False)
        self._set_status("checking the dongle…")
        try:
            _count, message = rtl_tools.probe_device()
        finally:
            self._probe_btn.setEnabled(True)
        self._set_status(message.splitlines()[0])

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop everything this page started, for window close.

        The override tone is stopped FIRST. Everything else here is a receiver, and a
        receiver left running is a wasted dongle; a transmitter left running is energy on
        the air, which is a different order of problem.
        """
        self._override.shutdown()
        self._waterfall3d.stop()
        if self._sdr is not None:
            self._sdr.shutdown()
            self._sdr = None
        if self._scan is not None:
            self._scan.shutdown()
            self._scan = None
        self._capturing = False
        self._scanning = False
