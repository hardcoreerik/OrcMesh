"""Display-range controls for a waterfall.

This is the **amplitude** window — the band of dB that the colours are spread
across — and it is the control that fixes a saturated display. Worth being clear
about because it is easily confused with zooming: narrowing it discards no data
at all, it simply spreads the same values over more of the colour ramp. A band
that looks like a solid block of the top colour is almost always this window
being wrong rather than the radio being too loud.

Frequency zooming is separate and lives with the capture settings, because the
span an RTL-SDR can see is its sample rate — no amount of plot zoom invents
resolution that was never sampled.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from meshchat.ui.spectrum.waterfall_view import WaterfallView


class LevelsControl(QWidget):
    """Auto-fit checkbox, manual dB limits, and a fit button, for one waterfall."""

    def __init__(self, view: WaterfallView, parent=None):
        super().__init__(parent)
        self._view = view
        #: Guards the spin boxes against re-emitting while they are being
        #: updated *from* the view, which would otherwise fight the auto-fit.
        self._syncing = False

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(6)

        row.addWidget(QLabel("Levels:"))

        self._auto = QCheckBox("Auto")
        self._auto.setChecked(view.auto_levels)
        self._auto.setToolTip(
            "Fit the display range to the signal. Turn off to set it by hand — "
            "a narrow window shows faint signals, a wide one shows strong ones."
        )
        self._auto.toggled.connect(self._on_auto_toggled)
        row.addWidget(self._auto)

        self._low = QDoubleSpinBox()
        self._low.setDecimals(1)
        self._low.setRange(-200.0, 200.0)
        self._low.setFixedWidth(78)
        self._low.setToolTip("Bottom of the display range (dB)")
        row.addWidget(self._low)

        row.addWidget(QLabel("to"))

        self._high = QDoubleSpinBox()
        self._high.setDecimals(1)
        self._high.setRange(-200.0, 200.0)
        self._high.setFixedWidth(78)
        self._high.setToolTip("Top of the display range (dB)")
        row.addWidget(self._high)

        row.addWidget(QLabel("dB"))

        self._fit = QPushButton("Fit")
        self._fit.setFixedWidth(50)
        self._fit.setToolTip("Re-fit the range to the current signal")
        self._fit.clicked.connect(self._view.fit_levels)
        row.addWidget(self._fit)

        self._reset = QPushButton("Reset view")
        self._reset.setFixedWidth(90)
        self._reset.setToolTip("Undo any mouse panning or zooming of the plot")
        self._reset.clicked.connect(self._view.reset_view)
        row.addWidget(self._reset)

        row.addStretch()

        for box in (self._low, self._high):
            box.valueChanged.connect(self._on_limits_edited)

        view.levels_changed.connect(self._on_view_levels_changed)
        self._sync_from_view(*view.levels())
        self._apply_enabled()

    # ------------------------------------------------------------------

    def _apply_enabled(self) -> None:
        manual = not self._auto.isChecked()
        self._low.setEnabled(manual)
        self._high.setEnabled(manual)
        # Fit stays available even in auto: it forces an immediate re-fit rather
        # than waiting for the next scheduled one.
        self._fit.setEnabled(True)

    def _sync_from_view(self, low_db: float, high_db: float) -> None:
        self._syncing = True
        try:
            self._low.setValue(low_db)
            self._high.setValue(high_db)
        finally:
            self._syncing = False

    def _on_view_levels_changed(self, low_db: float, high_db: float) -> None:
        self._sync_from_view(low_db, high_db)

    def _on_auto_toggled(self, enabled: bool) -> None:
        self._apply_enabled()
        if enabled:
            self._view.set_auto_levels(True)
        else:
            # Seed the manual window from whatever auto had settled on, so
            # turning it off does not jump the display somewhere else.
            self._view.set_auto_levels(False)
            self._on_limits_edited()

    def _on_limits_edited(self) -> None:
        if self._syncing or self._auto.isChecked():
            return
        low, high = self._low.value(), self._high.value()
        if high <= low:
            # Refusing silently would look broken; nudge the top instead so the
            # display always has a usable window.
            high = low + 1.0
            self._syncing = True
            self._high.setValue(high)
            self._syncing = False
        self._view.set_levels(low, high)
