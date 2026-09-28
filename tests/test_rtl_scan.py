"""Tests for the wideband scan: parsing, stitching, sweeps and the worker.

The two rows below are verbatim stdout from ``rtl_power`` on the RTL-SDR Blog V4
this was built against (``-f 902M:928M:125k -g 40 -i 1``), so the parser is
tested against the real format rather than a guess at it. Everything runs
without a dongle.
"""
from __future__ import annotations

import io
import sys
import threading
import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.services import rtl_scan, rtl_tools  # noqa: E402
from meshchat.services.rtl_scan import (  # noqa: E402
    PowerRow,
    ScanAssembler,
    ScanController,
    ScanRequest,
    ScanWorker,
    merge_rows,
    parse_power_row,
)

#: Consecutive real rows: 2.6 MHz each, sharing the 904.600 MHz endpoint, 33
#: bins apiece (inclusive of both ends at 81.25 kHz spacing).
_REAL_ROW_1 = (
    "2026-09-27, 08:41:30, 902000000, 904600000, 81250.00, 256, "
    "-5.03, -3.66, -2.69, -1.17, -0.54, 0.50, 0.54, 0.56, 0.89, 0.37, 1.16, "
    "0.46, 0.80, 0.24, 1.17, 1.59, 0.90, 0.90, 1.56, 0.63, 0.97, 0.21, 0.77, "
    "0.10, 0.58, 0.68, 0.72, 0.53, -0.41, -1.20, -2.88, -4.15, -4.15"
)
_REAL_ROW_2 = (
    "2026-09-27, 08:41:30, 904600000, 907200000, 81250.00, 256, "
    "-6.78, -5.47, -4.30, -2.72, 0.11, 2.48, -0.54, -1.00, -0.76, -0.73, "
    "-1.32, -0.81, -0.07, 0.36, -0.75, -0.80, -0.31, -0.31, -0.61, -1.03, "
    "-0.94, -0.52, -0.94, -0.63, -0.73, -0.30, 2.71, 2.21, -1.42, -3.27, "
    "-4.14, -5.64, -5.64"
)

#: A row with the non-numbers rtl_power really emits for bins it cannot
#: compute (seen live, as runs of adjacent bins where the tuner was retuning).
_NAN_ROW = (
    "2026-09-26, 23:53:38, 902000000, 904600000, 81250.00, 256, "
    "-0.43, -0.42, 1.34, 2.67, 3.78, 4.27, 4.54, 4.61, 4.92, 4.89, 4.79, "
    "4.89, -nan(ind), -nan(ind), -nan(ind), -nan(ind), 5.01, 5.01, -nan(ind), "
    "5.10, 5.20, 5.30, 5.40, 5.50, 5.60, 5.70, 5.80, 5.90, 6.00, 6.10, 6.20, "
    "6.30, 6.40"
)


def _row(low_hz: float, high_hz: float, values, step_hz: float = 125_000.0) -> PowerRow:
    return PowerRow(low_hz, high_hz, step_hz, 256, np.array(values, dtype=np.float32))


class _ExitedTool:
    """As much of a Popen as the scan loop touches, already exited."""

    returncode = 1

    def __init__(self, stderr_text: str = "") -> None:
        self.stdout: io.BytesIO = io.BytesIO(b"")
        self.stderr: io.BytesIO = io.BytesIO(stderr_text.encode())

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


class TestParsingRealOutput:
    def test_a_real_row_parses_into_its_geometry(self):
        row = parse_power_row(_REAL_ROW_1)

        assert row is not None
        assert row.low_hz == 902_000_000
        assert row.high_hz == 904_600_000
        assert row.step_hz == 81_250
        assert row.samples == 256

    def test_the_bin_count_is_inclusive_of_both_ends(self):
        """2.6 MHz at 81.25 kHz is 33 bins, not 32 — rtl_power repeats the last."""
        row = parse_power_row(_REAL_ROW_1)

        assert row is not None
        assert row.bin_count == 33
        assert row.bin_count == int(round((row.high_hz - row.low_hz) / row.step_hz)) + 1

    def test_the_frequency_axis_starts_at_low_and_ends_at_high(self):
        row = parse_power_row(_REAL_ROW_1)

        assert row is not None
        frequencies = row.frequencies
        assert frequencies[0] == 902_000_000
        assert frequencies[-1] == 904_600_000

    def test_the_second_real_row_also_parses(self):
        row = parse_power_row(_REAL_ROW_2)

        assert row is not None
        assert row.low_hz == 904_600_000
        assert row.high_hz == 907_200_000
        assert row.bin_count == 33

    def test_real_power_values_survive_intact(self):
        row = parse_power_row(_REAL_ROW_1)

        assert row is not None
        assert row.power_db[0] == pytest.approx(-5.03)
        assert np.isfinite(row.power_db).all()


class TestNanHandling:
    def test_the_live_nan_placeholders_become_nan(self):
        """rtl_power's -nan(ind) is not a float and would otherwise raise."""
        row = parse_power_row(_NAN_ROW)

        assert row is not None
        assert row.bin_count == 33
        nan_count = int(np.isnan(row.power_db).sum())
        assert nan_count == 5, "4 adjacent placeholders plus one more later"

    def test_the_real_values_around_the_gaps_survive(self):
        row = parse_power_row(_NAN_ROW)

        assert row is not None
        assert row.power_db[0] == pytest.approx(-0.43)
        assert row.power_db[16] == pytest.approx(5.01)

    def test_it_does_not_raise_on_any_nan_spelling(self):
        for token in ("-nan(ind)", "nan(ind)", "nan", "-nan", "N/A", "inf", "-inf"):
            assert np.isnan(rtl_scan._parse_power(token)), token


class TestMalformedInput:
    @pytest.mark.parametrize("line", [
        "",
        "hello",
        "2026-09-27, 08:41:30, 902000000",
        "date, time, not_a_number, 904600000, 81250.00, 256, 1.0, 2.0",
        "2026-09-27, 08:41:30, 902000000, 904600000, 0, 256, 1.0, 2.0",
        "2026-09-27, 08:41:30, 904600000, 902000000, 81250.00, 256, 1.0, 2.0",
        "2026-09-27, 08:41:30, 902000000, 904600000, 81250.00, 256",
        "2026-09-27, 08:41:30, 902000000, 904600000, 81250.00, 256, 1.0",
    ])
    def test_a_bad_line_costs_one_row_not_the_scan(self, line):
        assert parse_power_row(line) is None

    def test_the_real_rows_are_not_rejected(self):
        assert parse_power_row(_REAL_ROW_1) is not None
        assert parse_power_row(_REAL_ROW_2) is not None


class TestMerging:
    def test_two_real_rows_stitch_into_one_continuous_sweep(self):
        first = parse_power_row(_REAL_ROW_1)
        second = parse_power_row(_REAL_ROW_2)
        assert first is not None and second is not None

        merged = merge_rows([first, second])

        assert merged.low_hz == 902_000_000
        assert merged.high_hz == 907_200_000
        # 5.2 MHz at 81.25 kHz, inclusive.
        assert merged.bin_count == 65
        assert np.isfinite(merged.power_db).all(), "the shared bin must be filled"

    def test_the_shared_endpoint_bin_takes_the_first_row(self):
        """Documents which row wins where two cover the same frequency."""
        first = parse_power_row(_REAL_ROW_1)
        second = parse_power_row(_REAL_ROW_2)
        assert first is not None and second is not None

        merged = merge_rows([first, second])

        assert merged.power_db[32] == pytest.approx(-4.15), "row 1's last value"
        # Row 2's first value (-6.78) is the same shared frequency as index 32,
        # so it loses to row 1 and index 33 is row 2's *second* value.
        assert merged.power_db[33] == pytest.approx(-5.47)

    def test_a_gap_in_one_row_is_filled_by_the_row_that_covers_it(self):
        """The reason NaN bins are skipped rather than propagated."""
        left = _row(0.0, 1000.0, [1.0, 2.0, float("nan")], step_hz=500.0)
        right = _row(1000.0, 1500.0, [30.0, 40.0], step_hz=500.0)

        merged = merge_rows([left, right])

        assert merged.bin_count == 4
        assert merged.power_db[2] == pytest.approx(30.0), "the neighbour fills the gap"

    def test_a_frequency_no_row_covers_stays_a_gap(self):
        """Leaving a hole is honest; inventing a value would not be.

        The rows have to be more than one step apart for a gap to exist: on a
        uniform grid, two rows one step apart simply meet with no cell between
        them, so this is not a case of the merge filling something in.
        """
        left = _row(0.0, 500.0, [1.0, 2.0], step_hz=500.0)
        right = _row(2000.0, 2500.0, [3.0, 4.0], step_hz=500.0)

        merged = merge_rows([left, right])

        assert merged.bin_count == 6
        assert np.isnan(merged.power_db[2]), "nothing measured 1000-1500 Hz"
        assert np.isnan(merged.power_db[3])
        assert merged.power_db[4] == pytest.approx(3.0), "the far row still lands"

    def test_a_differing_step_is_dropped_rather_than_shearing_the_axis(self):
        good = _row(0.0, 1000.0, [1.0, 2.0, 3.0], step_hz=500.0)
        odd = _row(1000.0, 1200.0, [9.0, 9.0, 9.0], step_hz=100.0)

        merged = merge_rows([good, odd])

        assert merged.bin_count == 3, "the odd row must not stretch the grid"
        assert not np.any(merged.power_db == 9.0), "its values must not be placed"

    def test_merging_nothing_is_an_error_not_a_silent_empty_row(self):
        with pytest.raises(ValueError):
            merge_rows([])


class TestSweepAssembly:
    def test_ascending_rows_accumulate_without_finishing(self):
        assembler = ScanAssembler()
        first = parse_power_row(_REAL_ROW_1)
        second = parse_power_row(_REAL_ROW_2)
        assert first is not None and second is not None

        assert assembler.add(first) is None
        assert assembler.add(second) is None
        assert assembler.pending_rows == 2

    def test_a_frequency_drop_completes_the_sweep(self):
        """A sweep is the ascending run; the drop is what ends it."""
        assembler = ScanAssembler()
        first = parse_power_row(_REAL_ROW_1)
        second = parse_power_row(_REAL_ROW_2)
        assert first is not None and second is not None
        assembler.add(first)
        assembler.add(second)

        finished = assembler.add(first)  # the next sweep begins

        assert finished is not None
        assert finished.low_hz == 902_000_000
        assert finished.high_hz == 907_200_000
        assert assembler.pending_rows == 1

    def test_a_single_row_span_still_produces_sweeps(self):
        """A narrow span fits in one row, so every interval is a whole sweep."""
        assembler = ScanAssembler()
        row = parse_power_row(_REAL_ROW_1)
        assert row is not None
        assembler.add(row)

        finished = assembler.add(row)

        assert finished is not None
        assert finished.bin_count == 33

    def test_the_pending_buffer_cannot_grow_without_bound(self):
        """A span that never wraps must not leak memory."""
        assembler = ScanAssembler(max_rows=3)
        row = parse_power_row(_REAL_ROW_1)
        assert row is not None
        ascending = [
            _row(902_000_000.0 + i * 2_600_000, 904_600_000.0 + i * 2_600_000,
                 [float(i)] * 33)
            for i in range(20)
        ]

        for each in ascending:
            assert assembler.add(each) is None

        assert assembler.pending_rows == 3


class TestScanRequest:
    def test_span_is_the_difference(self):
        assert ScanRequest(low_hz=902e6, high_hz=928e6).span_hz == 26e6

    def test_the_defaults_are_the_us_band(self):
        request = ScanRequest(low_hz=902e6, high_hz=928e6)

        assert request.bin_hz == 125_000.0

    def test_the_default_gain_is_fixed_not_automatic(self):
        """Auto resolves to near-maximum, which is the worst case for headroom.

        Measured on a Blog V4 at 915 MHz: auto put the noise floor at +17 dB and
        3.7 dB put it at -17 dB, so auto is the setting most likely to clip a
        strong local signal. Unlike the Spectrum page, which passes -1 for auto,
        a survey wants the widest headroom.
        """
        assert ScanRequest(low_hz=902e6, high_hz=928e6).gain_db > 0.0


class TestScanWorkerCommand:
    def test_the_frequency_flag_carries_the_whole_geometry(self):
        command = ScanWorker()._command(ScanRequest(low_hz=902e6, high_hz=928e6, bin_hz=125_000))

        assert command[command.index("-f") + 1] == "902000000:928000000:125000"

    def test_auto_gain_is_spelled_the_way_rtl_power_wants_it(self):
        command = ScanWorker()._command(ScanRequest(low_hz=902e6, high_hz=928e6, gain_db=-1.0))

        assert command[command.index("-g") + 1] == "0"

    def test_a_manual_gain_is_passed_through(self):
        command = ScanWorker()._command(ScanRequest(low_hz=902e6, high_hz=928e6, gain_db=28.0))

        assert command[command.index("-g") + 1] == "28.0"

    def test_it_never_passes_an_exit_timer(self):
        """-e would end the scan on a timer; it should end when told to."""
        command = ScanWorker()._command(ScanRequest(low_hz=902e6, high_hz=928e6))

        assert "-e" not in command

    def test_a_hamming_window_is_requested(self):
        """The default window is rectangle, which leaves the band full of holes.

        Measured on a Blog V4 over 902-928 MHz at gain 20, counting bins
        rtl_power reported as -nan(ind): the default left 41% of them unusable,
        hamming left 2%. The documented alternative (-F 9 with -c 50%) only
        reached 11% and halved the bins per row, so the window is the fix and
        the cropping options are deliberately not used.
        """
        command = ScanWorker()._command(ScanRequest(low_hz=902e6, high_hz=928e6))

        assert command[command.index("-w") + 1] == "hamming"
        assert "-c" not in command, "cropping halves the bins per row for no gain"

    def test_the_csv_goes_to_stdout_last(self):
        command = ScanWorker()._command(ScanRequest(low_hz=902e6, high_hz=928e6))

        assert command[-1] == "-"

    def test_it_refuses_to_build_a_command_without_the_tool(self, monkeypatch):
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: None)

        with pytest.raises(FileNotFoundError):
            ScanWorker()._command(ScanRequest(low_hz=902e6, high_hz=928e6))


class TestScanWorkerSignals:
    def test_a_tool_that_dies_emits_error_but_not_stopped(self):
        worker = ScanWorker()
        worker._proc = _ExitedTool()
        worker._running = True

        errors: list[str] = []
        stopped: list[str] = []
        worker.error.connect(errors.append)
        worker.stopped.connect(stopped.append)

        worker._scan_loop()

        assert len(errors) == 1
        assert stopped == [], (
            "stopped should not fire alongside error — it overwrites the error status"
        )

    def test_the_scan_failure_blames_rtl_power_not_rtl_sdr(self):
        worker = ScanWorker()
        worker._proc = _ExitedTool()
        worker._running = True
        errors: list[str] = []
        worker.error.connect(errors.append)

        worker._scan_loop()

        assert errors and "rtl_power" in errors[0]

    def test_a_clean_self_exit_is_a_stop_not_a_failure(self):
        worker = ScanWorker()
        tool = _ExitedTool()
        tool.returncode = 0
        worker._proc = tool
        worker._running = True

        errors: list[str] = []
        stopped: list[str] = []
        worker.error.connect(errors.append)
        worker.stopped.connect(stopped.append)

        worker._scan_loop()

        assert errors == []
        assert stopped == ["Scan stopped"]

    def test_a_stopped_scan_emits_stopped_but_not_error(self):
        worker = ScanWorker()
        worker._proc = _ExitedTool()
        worker._running = False

        errors: list[str] = []
        stopped: list[str] = []
        worker.error.connect(errors.append)
        worker.stopped.connect(stopped.append)

        worker._scan_loop()

        assert errors == []
        assert stopped == ["Scan stopped"]

    def test_a_sweep_is_emitted_once_the_next_one_begins(self):
        """Feeds real rows through the loop and checks a sweep comes out."""
        rows = [_REAL_ROW_1, _REAL_ROW_2, _REAL_ROW_1, _REAL_ROW_2]
        worker = ScanWorker()
        tool = _ExitedTool()
        tool.stdout = io.BytesIO("".join(line + "\n" for line in rows).encode())
        worker._proc = tool
        worker._running = True

        sweeps: list[PowerRow] = []
        worker.sweep_ready.connect(sweeps.append)

        worker._scan_loop()

        assert len(sweeps) == 1
        assert sweeps[0].bin_count == 65
        assert sweeps[0].low_hz == 902_000_000

    def test_the_process_is_released_after_a_stop(self):
        worker = ScanWorker()
        worker._proc = _ExitedTool()
        worker._running = True

        worker._scan_loop()

        assert worker._proc is None

    def test_a_spawn_that_never_happened_gives_the_dongle_back(self, monkeypatch):
        """The capture worker had the same defect: the lease precedes the spawn."""
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: rtl_tools.Path("rtl_power"))
        device = 8
        rtl_tools.release_sdr("hygiene", device)

        def refuse(*_args, **_kwargs):
            raise OSError("no more handles")

        monkeypatch.setattr(rtl_scan.subprocess, "Popen", refuse)

        worker = ScanWorker()
        errors: list[str] = []
        worker.error.connect(errors.append)

        worker.start(ScanRequest(low_hz=902e6, high_hz=928e6, device_index=device))

        assert errors and "Could not start rtl_power" in errors[0]
        assert rtl_tools.sdr_owner(device) == "", (
            "a failed spawn left the dongle leased, so nothing can scan it again"
        )


class TestScanControllerThreading:
    """The click of "Scan region" hands the request to the worker across threads.

    This is the path that broke live: QMetaObject.invokeMethod with
    Q_ARG(object, request) raised "qArgDataFromPyType: Unable to find a QMetaType
    for ...", because a Python object has no registered meta type to marshal.
    """

    def test_the_request_reaches_the_worker_on_its_own_thread(self, monkeypatch):
        seen: list[tuple[ScanRequest, int]] = []

        def no_tool(self, request):
            seen.append((request, threading.get_ident()))
            # Stops short of the dongle, on a path the worker already reports.
            raise FileNotFoundError("rtl_power is not installed")

        monkeypatch.setattr(ScanWorker, "_command", no_tool)
        controller = ScanController()
        errors: list[str] = []
        controller.error.connect(errors.append)

        try:
            request = ScanRequest(
                low_hz=902.0e6, high_hz=928.0e6, bin_hz=50.0e3, gain_db=16.0
            )
            controller.start(request)   # used to raise RuntimeError right here
            deadline = time.monotonic() + 5.0
            while not errors and time.monotonic() < deadline:
                _app.processEvents()
                time.sleep(0.01)
        finally:
            controller.shutdown()

        assert seen, "the request never reached the worker"
        assert seen[0][0] == request
        assert seen[0][1] != threading.get_ident(), "it must run on the worker thread"
        assert errors and "rtl_power" in errors[0]
