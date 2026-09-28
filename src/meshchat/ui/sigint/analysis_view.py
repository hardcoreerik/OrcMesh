"""Five ways of looking at the same spectrum, for SIGINT work.

A live waterfall answers "what is on the air right now". Most SIGINT questions are not that.
The signals worth finding are usually intermittent, or weak and constant, or a pattern across
the mesh's own channels — none of which are visible in the moment. Each mode here answers a
different question that the waterfall cannot:

============  =================================================================
Peak hold     what has *ever* been here — catches the emitter that will not sit still
Occupancy     how *often* — separates a weak constant signal from a strong rare one
Envelope      what is typical versus unusual, per bin, from the distribution
Channels      which of the receiver's own channel slots carry traffic
Events        what *happened* — a log of bursts with duration and width
============  =================================================================

Two decisions shared by all five, both about honesty as much as performance:

* **Nothing is drawn as a number it does not have.** A bin never measured is NaN and stays
  blank. Zero dB is a real signal level; a hole is not, and drawing one as a value invents data.
* **Redraws are throttled.** Rows arrive at tens per second and several of these modes fold the
  whole window every time, so the display updates on a timer rather than per row. The numbers
  stay exact; only the drawing is coarse. That is what keeps a mode that recomputes percentiles
  from locking the GUI thread at 60 Hz.
"""
from __future__ import annotations

import logging
from enum import Enum

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meshchat.analytics.spectral_history import (
    Burst,
    BurstDetector,
    SpectralHistory,
    bursts_csv,
    slot_power_grid,
    spectrum_csv,
)

log = logging.getLogger(__name__)

#: How often the drawing catches up with the data. Fast enough to look live, slow enough that
#: folding a 600-row window per frame does not matter.
REDRAW_MS = 250

#: Bursts kept for the event mode. Bounded because a busy band produces them continuously and
#: an unbounded log would be a memory leak with a nice name.
MAX_BURSTS = 400

#: Duty cycle at which the occupancy mode draws its reference line. Below this a bin is usually
#: idle; above it something is making regular use of it.
BUSY_DUTY = 0.10

MODE_HELP: dict[str, str] = {
    "Peak hold": "The highest level each bin has reached since the last reset. An emitter that "
                 "appears for a fraction of a second still leaves a mark here, which is the "
                 "point: it is invisible on a live spectrum most of the time.",
    "Occupancy": "The share of the run each bin spent above its own noise floor. Strength and "
                 "persistence are different facts — a weak carrier that never stops and a "
                 "strong one that appeared once look identical on peak hold, and opposite here.",
    "Envelope": "The 5th, 50th and 95th percentile per bin, so a level reads as typical or "
                "unusual rather than as a single sample. A gap between the 95th and the median "
                "means something intermittent; all three together means steady.",
    "Channels": "Power folded onto channel slots over time. This asks a question about the "
                "mesh's own layout — which of its channels is carrying traffic — rather than "
                "about the spectrum. A slot nobody measured stays blank.",
    "Events": "Discrete bursts, as a log rather than a picture: when, how wide, how long, how "
              "far above the floor. A burst is only reported once it ends, because its duration "
              "is not known until then.",
}


class AnalysisMode(Enum):
    """The five views. Order is the order they appear in the selector."""

    PEAK_HOLD = "Peak hold"
    OCCUPANCY = "Occupancy"
    ENVELOPE = "Envelope"
    CHANNELS = "Channels"
    EVENTS = "Events"


class AnalysisView(QWidget):
    """Five SIGINT views over one spectrum, selected by mode.

    Fed by the same rows the waterfall gets, so it describes exactly what is being displayed
    rather than a second, separately-tuned receiver.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bins = 0
        self._centre_hz = 0.0
        self._span_hz = 0.0
        self._history: SpectralHistory | None = None
        self._detector: BurstDetector | None = None
        self._bursts: list[Burst] = []
        self._row_seconds = 0.0
        self._dirty = False
        self._frozen = False
        self._build()

        self._timer = QTimer(self)
        self._timer.setInterval(REDRAW_MS)
        self._timer.timeout.connect(self._redraw_if_dirty)
        self._timer.start()

    # ── construction ───────────────────────────────────────────────────────────
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 2, 6, 2)
        title = QLabel("Analysis")
        title.setStyleSheet("font-weight: 600; color: #C8D2E8;")
        row.addWidget(title)

        self._mode = QComboBox()
        for mode in AnalysisMode:
            self._mode.addItem(mode.value, mode)
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        row.addWidget(self._mode)

        self._explain = QLabel(MODE_HELP[AnalysisMode.PEAK_HOLD.value])
        self._explain.setWordWrap(True)
        self._explain.setStyleSheet("color: #5A6690; font-size: 11px;")
        row.addWidget(self._explain, 1)

        self._reset_btn = QPushButton("Reset")
        self._reset_btn.setFixedWidth(64)
        self._reset_btn.setToolTip(
            "Forget the accumulated history and start again. An intermittent emitter found by "
            "peak hold stays found until this is pressed, which is deliberate — but it also "
            "means the view describes the whole session, not the last minute.",
        )
        self._reset_btn.clicked.connect(self.reset)
        row.addWidget(self._reset_btn)

        # Freeze holds the accumulated picture while the capture keeps running. A live view is
        # the wrong thing to read numbers off: the row you are looking at is gone by the time
        # you have read it, and comparing anything means being able to stop.
        self._freeze_btn = QPushButton("Freeze")
        self._freeze_btn.setFixedWidth(66)
        self._freeze_btn.setCheckable(True)
        self._freeze_btn.setToolTip(
            "Stop folding new rows in, so the picture holds still while the capture keeps\n"
            "running. Peak hold and occupancy describe the whole session, so freezing does\n"
            "not change what they say — it stops them changing while you read them.",
        )
        self._freeze_btn.toggled.connect(self._on_freeze_toggled)
        row.addWidget(self._freeze_btn)

        self._export_btn = QPushButton("Export")
        self._export_btn.setFixedWidth(66)
        self._export_btn.setToolTip(
            "Write the per-bin statistics and the event log to two CSV files, with\n"
            "frequencies and seconds rather than bin indices so the evidence stands alone.",
        )
        self._export_btn.clicked.connect(self._on_export)
        row.addWidget(self._export_btn)

        self._counts = QLabel("")
        self._counts.setStyleSheet("color: #5A6690; font-size: 11px;")
        row.addWidget(self._counts)
        layout.addWidget(bar)

        self._plot = pg.PlotWidget()
        self._plot.setBackground("#0B0F1A")
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        layout.addWidget(self._plot, 1)

        # Persistent items, created once. Anything a mode adds per frame goes in `_transient`
        # and is removed at the start of the next frame — creating and destroying plot items
        # per redraw leaks graphics objects over a session meant to run for hours.
        self._image = pg.ImageItem()
        self._plot.addItem(self._image)
        self._curves: dict[str, pg.PlotDataItem] = {}
        for name, colour, width in (
            ("primary", "#00D4FF", 2),
            ("secondary", "#FFB800", 1),
            ("tertiary", "#7CE8B0", 1),
            ("context", "#5A6690", 1),
        ):
            self._curves[name] = self._plot.plot(pen=pg.mkPen(colour, width=width))
        self._transient: list[pg.GraphicsObject] = []

    # ── input ──────────────────────────────────────────────────────────────────
    def configure(self, centre_hz: float, span_hz: float, bins: int) -> None:
        """Point the view at a capture. Resets, because the history describes the old one."""
        if bins <= 0:
            return
        self._centre_hz = centre_hz
        self._span_hz = span_hz
        self._bins = bins
        self._history = SpectralHistory(bins)
        self._detector = BurstDetector(bins)
        self._bursts = []
        self._redraw()

    def set_row_interval(self, seconds: float) -> None:
        """How long one row represents, so durations can be reported in seconds."""
        self._row_seconds = max(0.0, seconds)

    def push_row(self, row: np.ndarray) -> None:
        """Fold one spectrum in. Cheap: the drawing happens on the timer, not here."""
        if self._frozen:
            return
        if self._history is None or self._detector is None:
            return
        values = np.asarray(row)
        if values.shape != (self._bins,):
            return
        self._history.add(values)
        self._bursts.extend(self._detector.push(values))
        if len(self._bursts) > MAX_BURSTS:
            del self._bursts[: len(self._bursts) - MAX_BURSTS]
        self._dirty = True

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    def _on_freeze_toggled(self, frozen: bool) -> None:
        self._frozen = bool(frozen)
        self._freeze_btn.setText("Resume" if self._frozen else "Freeze")

    def _on_export(self) -> None:
        """Ask for a name, then write both files beside it."""
        if self._history is None or self._history.observed == 0:
            QMessageBox.information(
                self, "Nothing to export",
                "No spectra have been folded in yet, so there is no evidence to write.",
            )
            return
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Export analysis", "sigint-analysis.csv", "CSV (*.csv)",
        )
        if not chosen:
            return
        base = chosen[:-4] if chosen.lower().endswith(".csv") else chosen
        wrote: list[str] = []
        try:
            for suffix, text in (
                ("spectrum", spectrum_csv(self._history, centre_hz=self._centre_hz,
                                           span_hz=self._span_hz)),
                ("events", bursts_csv(self._bursts, centre_hz=self._centre_hz,
                                      span_hz=self._span_hz, bins=self._bins,
                                      row_seconds=self._row_seconds or 1.0)),
            ):
                path = f"{base}-{suffix}.csv"
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
                wrote.append(path)
        except OSError as exc:
            QMessageBox.critical(self, "Could not export", str(exc))
            return
        log.info("Exported analysis to %s", ", ".join(wrote))
        QMessageBox.information(self, "Exported", "\n".join(wrote))

    def reset(self) -> None:
        """Forget the accumulated history. Called by the button and on a retune."""
        if self._history is None:
            return
        self._history.clear()
        self._detector = BurstDetector(self._bins)
        self._bursts = []
        self._dirty = True

    @property
    def mode(self) -> AnalysisMode:
        return self._mode.currentData() or AnalysisMode.PEAK_HOLD

    def set_mode(self, mode: AnalysisMode) -> None:
        index = self._mode.findData(mode)
        if index >= 0:
            self._mode.setCurrentIndex(index)

    def _on_mode_changed(self) -> None:
        self._explain.setText(MODE_HELP.get(self.mode.value, ""))
        self._redraw()

    # ── drawing ────────────────────────────────────────────────────────────────
    def _frequencies(self) -> np.ndarray:
        if self._bins <= 0:
            return np.zeros(0)
        return (self._centre_hz - self._span_hz / 2
                + (np.arange(self._bins) + 0.5) * (self._span_hz / self._bins)) / 1e6

    def _redraw_if_dirty(self) -> None:
        if self._dirty:
            self._redraw()

    def _reset_scene(self) -> None:
        """Return the plot to a neutral state before a mode draws into it."""
        for item in self._transient:
            self._plot.removeItem(item)
        self._transient.clear()
        for curve in self._curves.values():
            curve.setVisible(False)
        self._image.setVisible(False)
        self._plot.invertY(False)
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        self._plot.setLabel("bottom", "Frequency (MHz)")
        self._plot.setLabel("left", "dB")
        self._plot.setTitle("")

    def _redraw(self) -> None:
        self._dirty = False
        history = self._history
        if history is None or self._bins <= 0:
            return
        self._reset_scene()
        try:
            match self.mode:
                case AnalysisMode.PEAK_HOLD:
                    self._draw_peak_hold(history)
                case AnalysisMode.OCCUPANCY:
                    self._draw_occupancy(history)
                case AnalysisMode.ENVELOPE:
                    self._draw_envelope(history)
                case AnalysisMode.CHANNELS:
                    self._draw_channels(history)
                case AnalysisMode.EVENTS:
                    self._draw_events()
        except Exception:  # pragma: no cover - a drawing fault must not kill the capture
            # Never swallow silently: a broad except once hid a wrong-shaped array on every
            # frame while the tests stayed green. This logs with the traceback.
            log.exception("Analysis redraw failed in %s mode", self.mode.value)
        self._counts.setText(
            f"{history.observed} rows · {len(self._bursts)} events",
        )

    def _draw_peak_hold(self, history: SpectralHistory) -> None:
        freqs = self._frequencies()
        self._curves["primary"].setData(freqs, history.peak_hold())
        self._curves["primary"].setVisible(True)

        rows = history.recent_rows()
        if rows.shape[0]:
            self._curves["secondary"].setData(freqs, rows[-1])
            self._curves["secondary"].setVisible(True)

        floor = history.floor_db()
        if np.isfinite(floor):
            self._curves["context"].setData(freqs, np.full(self._bins, floor))
            self._curves["context"].setVisible(True)
        self._plot.setTitle("Peak hold (bold), newest row, and the floor (dim)")

    def _draw_occupancy(self, history: SpectralHistory) -> None:
        freqs = self._frequencies()
        duty = history.occupancy() * 100.0
        width = (freqs[1] - freqs[0]) if self._bins > 1 else 1.0
        bars = pg.BarGraphItem(x=freqs, height=duty, width=width, brush="#00D4FF", pen=None)
        self._plot.addItem(bars)
        self._transient.append(bars)

        self._curves["secondary"].setData(
            [freqs[0], freqs[-1]], [BUSY_DUTY * 100.0, BUSY_DUTY * 100.0],
        )
        self._curves["secondary"].setVisible(True)
        self._plot.setLabel("left", "Duty cycle (%)")
        self._plot.setTitle(f"Duty cycle per bin — the line is {BUSY_DUTY:.0%}")

    def _draw_envelope(self, history: SpectralHistory) -> None:
        freqs = self._frequencies()
        bands = history.percentiles()

        self._curves["primary"].setData(freqs, bands[50.0])
        self._curves["primary"].setVisible(True)
        self._curves["secondary"].setData(freqs, bands[95.0])
        self._curves["secondary"].setVisible(True)
        self._curves["tertiary"].setData(freqs, bands[5.0])
        self._curves["tertiary"].setVisible(True)
        self._plot.setTitle("Median (bold) with the 5th and 95th percentiles")

    def _draw_channels(self, history: SpectralHistory) -> None:
        rows = history.recent_rows()
        if rows.shape[0] == 0:
            self._plot.setTitle("Channel slots — no rows yet")
            return
        grid, _centres = slot_power_grid(
            rows, centre_hz=self._centre_hz, span_hz=self._span_hz,
        )
        if grid.size == 0:
            self._plot.setTitle("Channel slots — the span is too small to divide")
            return

        floor = history.floor_db()
        # Relative to the floor, so the picture is about where energy is rather than about
        # whatever gain the receiver happens to be at.
        relative = np.where(np.isnan(grid), np.nan, grid - floor)
        self._image.setImage(relative.T, autoLevels=False, levels=(-5, 35))
        # Slot index on x, time on y: a slot carrying traffic is a stripe down the picture.
        self._image.setRect(QRectF(0, 0, grid.shape[0], grid.shape[1]))
        self._image.setVisible(True)
        self._plot.showGrid(x=False, y=False)
        self._plot.setLabel("bottom", "Channel slot (low to high)")
        self._plot.setLabel("left", "Rows")
        self._plot.setTitle(
            f"Slot activity — {grid.shape[0]} slots over {grid.shape[1]} rows, "
            f"dB over the {floor:.0f} dB floor",
        )

    def _draw_events(self) -> None:
        if not self._bursts:
            self._plot.setTitle("Events — nothing above the floor yet")
            return
        freqs = self._frequencies()
        last_row = max(1, max(burst.start_row for burst in self._bursts))
        for burst in self._bursts:
            first = min(burst.first_bin, self._bins - 1)
            last = min(burst.last_bin, self._bins - 1)
            # One horizontal segment per event: frequency extent across, time down the axis.
            # A log of what happened is what a picture of the moment cannot give.
            segment = pg.PlotDataItem(
                [freqs[first], freqs[last]], [burst.start_row, burst.start_row],
                pen=pg.mkPen("#7CE8B0", width=2),
            )
            self._plot.addItem(segment)
            self._transient.append(segment)
        self._plot.setLabel("left", "Row index")
        self._plot.setTitle(f"{len(self._bursts)} events, oldest at the top")
        self._plot.setYRange(0, last_row)
        self._plot.invertY(True)

    def events_summary(self) -> list[str]:
        """The burst log as tab-separated lines, for the CSV export."""
        bin_hz = self._span_hz / self._bins if self._bins else 0.0
        row_s = self._row_seconds or 1.0
        return [
            f"{burst.start_row}\t{burst.end_row}\t{burst.first_bin}\t{burst.last_bin}\t"
            f"{burst.peak_db:.2f}\t{burst.over_floor_db:.2f}\t"
            f"{burst.describe(bin_hz=bin_hz, row_s=row_s)}"
            for burst in self._bursts
        ]
