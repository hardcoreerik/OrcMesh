"""MeshChat – DashboardScheduler: throttled UI refresh timers."""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal


class DashboardScheduler(QObject):
    """
    Manages render-rate timers for the Monitor dashboard.

    Produces signals at throttled rates so the UI does not redraw
    on every packet.
    """

    tick_1s  = Signal()   # 1-second: relative times, elapsed
    tick_2s  = Signal()   # 2-second: rankings
    tick_4hz = Signal()   # 250 ms: chart redraw
    tick_5s  = Signal()   # 5-second: distribution panels

    def __init__(self, parent=None):
        super().__init__(parent)

        self._t1  = QTimer(self); self._t1.timeout.connect(self.tick_1s);  self._t1.start(1_000)
        self._t2  = QTimer(self); self._t2.timeout.connect(self.tick_2s);  self._t2.start(2_000)
        self._t4  = QTimer(self); self._t4.timeout.connect(self.tick_4hz); self._t4.start(250)
        self._t5  = QTimer(self); self._t5.timeout.connect(self.tick_5s);  self._t5.start(5_000)

    def set_paused(self, paused: bool) -> None:
        for t in (self._t1, self._t2, self._t4, self._t5):
            if paused:
                t.stop()
            else:
                t.start()
