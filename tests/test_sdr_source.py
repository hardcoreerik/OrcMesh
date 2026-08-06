"""Tests for SdrWorker._capture_loop()'s error/stopped signal exclusivity.

A read failure used to emit BOTH error and stopped for the same exit —
SpectrumPage._on_stopped()'s handler runs after _on_error()'s and
unconditionally overwrites the status label, so the "Error" text was
silently replaced with "Capture stopped", hiding that anything had gone
wrong even though the persistent notice panel still showed the real
message.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.services.sdr_source import SdrWorker  # noqa: E402


class _FailingSdr:
    def read_samples(self, n):
        raise RuntimeError("device unplugged")

    def close(self):
        pass


class TestCaptureLoopErrorHandling:
    def test_read_failure_emits_error_but_not_stopped(self):
        worker = SdrWorker()
        worker._sdr = _FailingSdr()
        worker._running = True

        errors = []
        stopped = []
        worker.error.connect(errors.append)
        worker.stopped.connect(stopped.append)

        worker._capture_loop()

        assert len(errors) == 1
        assert stopped == [], "stopped should not fire alongside error — it overwrites the error status"

    def test_normal_stop_still_emits_stopped_not_error(self):
        worker = SdrWorker()

        class _EmptySdr:
            def read_samples(self, n):
                worker._running = False  # simulate stop() being called mid-read
                return []

            def close(self):
                pass

        worker._sdr = _EmptySdr()
        worker._running = True

        errors = []
        stopped = []
        worker.error.connect(errors.append)
        worker.stopped.connect(stopped.append)

        worker._capture_loop()

        assert errors == []
        assert stopped == ["Capture stopped"]
