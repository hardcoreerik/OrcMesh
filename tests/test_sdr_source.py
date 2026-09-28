"""Tests for the RTL-SDR capture source and the native-tool helpers.

Everything here runs without a dongle attached: the I/Q conversion is a pure
function, the tool helpers are driven from output captured from real hardware,
and the capture loop is driven through a fake child process. The device probe
uses the verbatim text this machine's RTL-SDR Blog V4 produced, so a change in
what rtl_test reports gets noticed here rather than in the field.
"""
from __future__ import annotations

import io
import subprocess
import sys
import types

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.services import rtl_tools  # noqa: E402
from meshchat.services.sdr_source import (  # noqa: E402
    FFT_BINS,
    SdrWorker,
    iq_to_power_row,
)

#: Verbatim stdout from `rtl_test -t` on the RTL-SDR Blog V4 this was built
#: against, trimmed to the lines anything depends on.
_RTL_TEST_OUTPUT = """Found 1 device(s):
  0:  RTLSDRBlog, Blog V4L, SN: 00000001

Using device 0: Generic RTL2832U OEM
Found Rafael Micro R820T tuner
Supported gain values (29): 0.0 0.9 1.4 2.7 3.7 7.7 8.7 12.5 14.4 15.7
Sampling at 2048000 S/s.
No E4000 tuner found, aborting.
"""

#: 127/128 is silence in rtl_sdr's unsigned format — see iq_to_power_row.
_SILENCE_BYTE = 127


def _iq_bytes(samples: np.ndarray) -> bytes:
    """Interleave complex samples back into rtl_sdr's 8-bit I/Q wire format."""
    i = np.clip(np.round(samples.real * 127.5 + 127.5), 0, 255).astype(np.uint8)
    q = np.clip(np.round(samples.imag * 127.5 + 127.5), 0, 255).astype(np.uint8)
    return np.stack((i, q), axis=1).tobytes()


class _ExitedTool:
    """As much of a Popen as the capture loop touches, already exited.

    reads return EOF immediately, which is exactly how the loop discovers
    rtl_sdr is gone.
    """

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


class _StreamThatStops:
    """A stdout whose read() is where a user's stop() lands."""

    closed = False

    def __init__(self, worker: SdrWorker) -> None:
        self._worker = worker

    def read(self, count: int) -> bytes:
        self._worker._running = False  # simulate stop() being called mid-read
        return b""

    def close(self) -> None:
        self.closed = True


class TestIqConversion:
    def test_a_tone_lands_on_the_bin_it_belongs_to(self):
        """A +fs/4 tone must land three quarters of the way across the row.

        fftshift puts DC in the middle, so a positive-frequency tone at cyclic
        bin k appears at index k + N/2. fs/4 is bin N/4, so 3N/4.
        """
        n = FFT_BINS
        index = np.arange(n)
        tone = np.exp(2j * np.pi * (n // 4) * index / n) * 0.9

        row = iq_to_power_row(_iq_bytes(tone))

        assert row is not None
        assert int(np.argmax(row)) == 3 * n // 4

    def test_the_tone_stands_out_above_the_floor(self):
        n = FFT_BINS
        index = np.arange(n)
        tone = np.exp(2j * np.pi * (n // 8) * index / n) * 0.9

        row = iq_to_power_row(_iq_bytes(tone))

        assert row is not None
        assert float(row.max()) > float(np.median(row)) + 40.0

    def test_silence_reads_as_a_flat_floor(self):
        """A quiet tuner reads as dither around 127/128 — not as zero.

        It is dither rather than a constant for a reason: a constant level is a
        DC offset, which is a term the tuner makes on its own and which this code
        now removes (see the offset tests below). What distinguishes silence is
        that the whole row is flat, so this checks flatness rather than an
        absolute number, which would only encode this test's synthetic noise power.
        """
        rng = np.random.default_rng(1234)
        dither = rng.integers(126, 130, size=FFT_BINS * 2 * 4, dtype=np.uint8).tobytes()

        row = iq_to_power_row(dither)

        assert row is not None
        assert float(row.max()) < 0.0, "midpoint dither must not read as a signal"
        assert float(row.max()) - float(np.median(row)) < 15.0, "silence should be flat, not spiky"

    def test_a_dc_offset_does_not_land_in_the_centre_bin(self):
        """The tuner's LO leak used to draw a plateau down the middle of the band.

        A constant offset is exactly what an RTL-SDR produces and exactly what the
        3D view turned into a wall of false signal, so a constant stream must now
        read as floor rather than as a carrier on the centre frequency.
        """
        row = iq_to_power_row(bytes([160]) * (FFT_BINS * 2 * 4))

        assert row is not None
        assert np.isfinite(row).all()
        centre = FFT_BINS // 2
        # Far below any real signal, and level with what is either side of it. Not
        # exactly flat: cancelling a constant in float32 leaves rounding residue, which
        # shows up as a scatter of -100 dB values rather than a row of -120s.
        assert float(row[centre]) < -60.0, "an all-DC stream is not a carrier"
        assert float(row[centre]) <= float(row[centre - 5:centre + 6].max()) + 0.01

    def test_the_centre_is_level_with_its_neighbours(self):
        """Removing the offset must not leave a trench where the plateau was.

        Measured before the repair: the centre bin comes back about 4.8 dB below
        its neighbours, which is just as wrong on screen as the spike.
        """
        rng = np.random.default_rng(7)
        quiet = (rng.integers(0, 256, size=FFT_BINS * 2 * 8, dtype=np.uint8))
        quiet[0::2] = np.clip(quiet[0::2].astype(int) + 9, 0, 255)   # a DC offset
        quiet[1::2] = np.clip(quiet[1::2].astype(int) + 9, 0, 255)

        row = iq_to_power_row(quiet.tobytes())

        assert row is not None
        centre = FFT_BINS // 2
        neighbours = np.concatenate([row[centre - 40:centre - 4], row[centre + 5:centre + 41]])
        assert row[centre] == pytest.approx(float(np.median(neighbours)), abs=2.0)

    def test_a_carrier_next_to_the_centre_is_left_alone(self):
        """Only the bins the DC term occupies are synthesised — nothing wider.

        The repair interpolates three bins. A real signal one bin further out than
        that has to survive it, or the fix would cost more than the artefact.
        """
        n = FFT_BINS
        index = np.arange(n)
        offset_bins = 6
        tone = np.exp(-2j * np.pi * offset_bins * index / n) * 0.6

        row = iq_to_power_row(_iq_bytes(tone))

        assert row is not None
        assert int(np.argmax(row)) == n // 2 - offset_bins
        assert float(row.max()) > float(np.median(row)) + 30.0

    def test_the_full_scale_rail_does_not_produce_infinities(self):
        """An all-255 stream is a +/-1.0 DC term, not an overflow."""
        row = iq_to_power_row(bytes([255]) * (FFT_BINS * 2 * 4))

        assert row is not None
        assert np.isfinite(row).all()

    def test_it_averages_the_whole_chunk_into_one_row(self):
        row = iq_to_power_row(bytes([_SILENCE_BYTE]) * (FFT_BINS * 2 * 8))

        assert row is not None
        assert row.shape == (FFT_BINS,)
        assert row.dtype == np.float32

    def test_a_short_chunk_is_refused_rather_than_padded(self):
        assert iq_to_power_row(b"") is None

        short = bytes([_SILENCE_BYTE]) * (FFT_BINS * 2 - 1)
        assert len(short) > 0, "the boundary must be a short-but-nonempty chunk"
        assert iq_to_power_row(short) is None

    def test_a_stray_trailing_byte_does_not_desynchronise_the_pairs(self):
        """The stream is byte pairs; one odd byte must not shift I into Q."""
        tone = np.full(FFT_BINS * 2, 0.5 + 0.5j)

        row = iq_to_power_row(_iq_bytes(tone)[:-1])

        assert row is not None, "one spare byte still leaves a whole frame"


class TestCaptureLoopSignals:
    """error and stopped are mutually exclusive for a single exit.

    A read failure used to emit BOTH for the same exit — SpectrumPage's
    stopped handler runs after its error handler and unconditionally
    overwrites the status label, so the "Error" text was silently replaced
    with "Capture stopped", hiding that anything had gone wrong even though
    the persistent notice panel still showed the real message.
    """

    def test_a_clean_self_exit_is_a_stop_not_a_failure(self):
        """rtl_sdr quits on its own with status 0 and says "User cancel".

        It does that even on a clean shutdown, and it still writes
        "rtlsdr_demod_write_reg failed with -9" to stderr as the device closes —
        so the exit code is the only trustworthy signal, not the last line.
        """
        worker = SdrWorker()
        tool = _ExitedTool(stderr_text="User cancel, exiting...\n"
                                      "rtlsdr_demod_write_reg failed with -9")
        tool.returncode = 0
        worker._proc = tool
        worker._running = True

        errors: list[str] = []
        stopped: list[str] = []
        worker.error.connect(errors.append)
        worker.stopped.connect(stopped.append)

        worker._capture_loop()

        assert errors == [], "a clean exit must not be reported as a failure"
        assert stopped == ["Capture stopped"]

    def test_a_tool_that_dies_emits_error_but_not_stopped(self):
        worker = SdrWorker()
        worker._proc = _ExitedTool()
        worker._running = True

        errors: list[str] = []
        stopped: list[str] = []
        worker.error.connect(errors.append)
        worker.stopped.connect(stopped.append)

        worker._capture_loop()

        assert len(errors) == 1
        assert stopped == [], (
            "stopped should not fire alongside error — it overwrites the error status"
        )

    def test_a_dead_tool_with_no_output_still_reports_something(self):
        worker = SdrWorker()
        worker._proc = _ExitedTool()
        worker._running = True
        errors: list[str] = []
        worker.error.connect(errors.append)

        worker._capture_loop()

        assert errors and errors[0].strip(), "an empty error message is not a bug report"

    def test_a_busy_dongle_is_explained_rather_than_dumped_raw(self):
        """The most likely real failure: SDR# or Gqrx already holds the dongle."""
        worker = SdrWorker()
        worker._proc = _ExitedTool()
        worker._running = True
        worker._stderr_lines.append("Failed to open rtlsdr device #0.")
        errors: list[str] = []
        worker.error.connect(errors.append)

        worker._capture_loop()

        assert errors and "already in use" in errors[0]

    def test_a_deliberate_stop_emits_stopped_but_not_error(self):
        worker = SdrWorker()
        tool = _ExitedTool()
        tool.stdout = _StreamThatStops(worker)
        worker._proc = tool
        worker._running = True

        errors: list[str] = []
        stopped: list[str] = []
        worker.error.connect(errors.append)
        worker.stopped.connect(stopped.append)

        worker._capture_loop()

        assert errors == []
        assert stopped == ["Capture stopped"]

    def test_the_process_is_released_after_a_stop(self):
        """Nothing may be left holding the dongle for the next capture."""
        worker = SdrWorker()
        worker._proc = _ExitedTool()
        worker._running = True

        worker._capture_loop()

        assert worker._proc is None

    def test_a_spawn_that_never_happened_gives_the_dongle_back(self, monkeypatch):
        """The lease is taken before the spawn, so a failed spawn must release it.

        Otherwise the dongle reads as busy for the rest of the session while nothing
        is actually holding it, and there is no capture left for the user to stop.
        """
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: rtl_tools.Path("rtl_sdr"))
        # An index no other test uses, so a lease held elsewhere cannot make this
        # assertion pass or fail for the wrong reason.
        device = 9
        rtl_tools.release_sdr("hygiene", device)

        def refuse(*_args, **_kwargs):
            raise OSError("no more handles")

        monkeypatch.setattr(subprocess, "Popen", refuse)

        worker = SdrWorker()
        errors: list[str] = []
        worker.error.connect(errors.append)

        worker.start(915_000_000.0, 2_048_000.0, 16.0, device)

        assert errors and "Could not start rtl_sdr" in errors[0]
        assert rtl_tools.sdr_owner(device) == "", (
            "a failed spawn left the dongle leased, so nothing can capture from it again"
        )


class TestCommandLine:
    def test_auto_gain_is_spelled_the_way_rtl_sdr_wants_it(self):
        command = SdrWorker()._command(915_000_000.0, 2_048_000.0, -1.0)

        assert command[command.index("-g") + 1] == "0", "rtl_sdr spells auto gain as 0"

    def test_a_manual_gain_is_passed_through(self):
        command = SdrWorker()._command(915_000_000.0, 2_048_000.0, 40.2)

        assert command[command.index("-g") + 1] == "40.2"

    def test_frequencies_are_integral_hertz(self):
        command = SdrWorker()._command(915_000_000.9, 2_048_000.4, -1.0)

        assert command[command.index("-f") + 1] == "915000000"
        assert command[command.index("-s") + 1] == "2048000"

    def test_iq_is_dumped_to_stdout_last(self):
        """'-' is positional: rtl_sdr ignores anything after it."""
        command = SdrWorker()._command(915_000_000.0, 2_048_000.0, -1.0)

        assert command[-1] == "-"

    def test_it_refuses_to_build_a_command_without_the_tool(self, monkeypatch):
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: None)

        with pytest.raises(FileNotFoundError):
            SdrWorker()._command(915_000_000.0, 2_048_000.0, -1.0)


class TestFailureMessages:
    def test_a_busy_dongle_says_what_is_holding_it(self):
        message = rtl_tools.explain_failure("Failed to open rtlsdr device #0.")

        assert "already in use" in message
        assert "Gqrx" in message

    def test_the_busy_message_is_recognised_case_insensitively(self):
        assert "already in use" in rtl_tools.explain_failure("FAILED TO OPEN RTLSDR DEVICE #0.")

    def test_a_missing_dongle_is_reported_as_such(self):
        assert "plugged in" in rtl_tools.explain_failure("No supported devices found.")

    def test_an_unrecognised_failure_is_passed_through_verbatim(self):
        """Better an unfamiliar message than a confident wrong one."""
        assert rtl_tools.explain_failure(
            "rtlsdr_demod_write_reg failed with -9"
        ) == "rtlsdr_demod_write_reg failed with -9"

    def test_no_output_still_produces_something_to_show(self):
        assert rtl_tools.explain_failure("") == "rtl_sdr stopped without reporting a reason."


class TestToolDiscovery:
    def test_missing_rtl_sdr_is_fatal_and_named(self, monkeypatch):
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: None)

        available, reason = rtl_tools.tools_available()

        assert available is False
        assert "rtl_sdr" in reason

    def test_rtl_sdr_alone_is_enough(self, monkeypatch):
        monkeypatch.setattr(
            rtl_tools, "find_tool",
            lambda name: rtl_tools.Path("C:/rtl/rtl_sdr.exe") if name == "rtl_sdr" else None,
        )

        available, reason = rtl_tools.tools_available()

        assert available is True
        assert "rtl_power and rtl_test are missing" in reason

    def test_the_reason_names_where_the_tool_was_found(self, monkeypatch):
        monkeypatch.setattr(
            rtl_tools, "find_tool", lambda name: rtl_tools.Path(f"C:/rtl/{name}.exe")
        )

        available, reason = rtl_tools.tools_available()

        assert available is True
        assert "rtl_power" in reason and "rtl_test" in reason


class TestFindTool:
    """The real lookup, rather than the stub the tests above install."""

    def test_path_wins_and_the_tools_directory_is_not_consulted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rtl_tools.shutil, "which", lambda name: "C:/rtl/rtl_sdr.exe")
        # A copy sits in our directory too. PATH still wins, so an install of ours can
        # never shadow the one the machine already had.
        monkeypatch.setattr(rtl_tools, "tools_directory", lambda: tmp_path)
        (tmp_path / "rtl_sdr.exe").write_bytes(b"")

        assert rtl_tools.find_tool("rtl_sdr") == rtl_tools.Path("C:/rtl/rtl_sdr.exe")

    def test_a_tool_in_the_per_user_directory_is_found_without_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rtl_tools.shutil, "which", lambda name: None)
        monkeypatch.setattr(rtl_tools, "tools_directory", lambda: tmp_path)
        installed = tmp_path / "rtl_433.exe"
        installed.write_bytes(b"")

        assert rtl_tools.find_tool("rtl_433") == installed

    def test_the_suffix_is_optional(self, monkeypatch, tmp_path):
        """shutil.which would supply the suffix; here it has to be spelled out."""
        monkeypatch.setattr(rtl_tools.shutil, "which", lambda name: None)
        monkeypatch.setattr(rtl_tools, "tools_directory", lambda: tmp_path)
        installed = tmp_path / "rtl_433"
        installed.write_bytes(b"")

        assert rtl_tools.find_tool("rtl_433") == installed

    def test_a_directory_of_the_right_name_is_not_a_tool(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rtl_tools.shutil, "which", lambda name: None)
        monkeypatch.setattr(rtl_tools, "tools_directory", lambda: tmp_path)
        (tmp_path / "rtl_433.exe").mkdir()

        assert rtl_tools.find_tool("rtl_433") is None

    def test_a_tool_that_is_simply_not_installed_is_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rtl_tools.shutil, "which", lambda name: None)
        monkeypatch.setattr(rtl_tools, "tools_directory", lambda: tmp_path)

        assert rtl_tools.find_tool("rtl_433") is None


class TestToolsDirectory:
    def test_it_is_per_user_rather_than_machine_wide(self, monkeypatch, tmp_path):
        "Installing a decoder must not mean editing a machine-wide PATH."
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

        assert rtl_tools.tools_directory() == tmp_path / "OrcMesh" / "tools"

    def test_it_falls_back_when_localappdata_is_absent(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(rtl_tools.Path, "home", classmethod(lambda cls: tmp_path))

        assert rtl_tools.tools_directory() == tmp_path / ".local" / "share" / "OrcMesh" / "tools"


class TestDeviceProbe:
    def test_a_present_dongle_is_named(self, monkeypatch):
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: rtl_tools.Path("rtl_test"))
        monkeypatch.setattr(
            rtl_tools.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(stdout=_RTL_TEST_OUTPUT, stderr=""),
        )

        count, message = rtl_tools.probe_device()

        assert count == 1
        assert "RTLSDRBlog" in message

    def test_no_dongle_reports_zero(self, monkeypatch):
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: rtl_tools.Path("rtl_test"))
        monkeypatch.setattr(
            rtl_tools.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(stdout="", stderr="No supported devices found."),
        )

        count, message = rtl_tools.probe_device()

        assert count == 0
        assert "plugged in" in message

    def test_a_wedged_driver_does_not_hang_the_app(self, monkeypatch):
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: rtl_tools.Path("rtl_test"))

        def hang(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="rtl_test", timeout=1.0)

        monkeypatch.setattr(rtl_tools.subprocess, "run", hang)

        count, message = rtl_tools.probe_device()

        assert count == 0
        assert "did not finish" in message

    def test_a_missing_rtl_test_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: None)

        count, message = rtl_tools.probe_device()

        assert count == 0
        assert "rtl_test was not found" in message


@pytest.mark.parametrize("name", ["rtl_sdr", "rtl_power", "rtl_test"])
def test_the_driven_tool_names_are_the_expected_ones(name):
    """Guards against a rename in one place but not the other."""
    assert name in rtl_tools.TOOL_NAMES


@pytest.fixture
def clean_lease():
    """The lease is module state, so a test that leaks it breaks the next one.

    Both devices: ownership is per dongle now, so a test that holds device 1 leaks
    just as badly as one that holds device 0.
    """
    yield
    for device in (0, 1):
        rtl_tools.release_sdr("owner-a", device)
        rtl_tools.release_sdr("owner-b", device)


class TestDongleLease:
    """Only one holder at a time, and it must name *who* holds it.

    Without this, OrcMesh's own two spectrum views collided and the user was told
    to close SDR# — advice that was both wrong and useless.
    """

    def test_an_idle_dongle_has_no_owner(self, clean_lease):
        assert rtl_tools.sdr_owner() == ""

    def test_the_first_claim_succeeds(self, clean_lease):
        held, complaint = rtl_tools.acquire_sdr("owner-a", "the SIGINT spectrum")

        assert held is True
        assert complaint == ""
        assert rtl_tools.sdr_owner() == "the SIGINT spectrum"

    def test_a_second_claim_is_refused_and_names_the_holder(self, clean_lease):
        rtl_tools.acquire_sdr("owner-a", "the SIGINT spectrum")

        held, complaint = rtl_tools.acquire_sdr("owner-b", "the band scan")

        assert held is False
        assert "the SIGINT spectrum" in complaint
        assert "one place at a time" in complaint

    def test_the_same_owner_can_re_claim(self, clean_lease):
        """A restart inside one feature need not release first."""
        rtl_tools.acquire_sdr("owner-a", "the SIGINT spectrum")

        held, _ = rtl_tools.acquire_sdr("owner-a", "the SIGINT spectrum")

        assert held is True

    def test_releasing_frees_it_for_the_next_claim(self, clean_lease):
        rtl_tools.acquire_sdr("owner-a", "the SIGINT spectrum")
        rtl_tools.release_sdr("owner-a")

        held, complaint = rtl_tools.acquire_sdr("owner-b", "the band scan")

        assert held is True
        assert complaint == ""
        assert rtl_tools.sdr_owner() == "the band scan"

    def test_releasing_someone_elses_lease_does_nothing(self, clean_lease):
        """Otherwise a stale release from a finished capture would free it early."""
        rtl_tools.acquire_sdr("owner-a", "the SIGINT spectrum")

        rtl_tools.release_sdr("owner-b")

        assert rtl_tools.sdr_owner() == "the SIGINT spectrum"

    def test_releasing_twice_is_harmless(self, clean_lease):
        rtl_tools.acquire_sdr("owner-a", "the SIGINT spectrum")

        rtl_tools.release_sdr("owner-a")
        rtl_tools.release_sdr("owner-a")

        assert rtl_tools.sdr_owner() == ""

    def test_a_carrier_on_the_centre_frequency_is_not_painted_over(self):
        """The guard on the repair, which matters because the presets tune there.

        A preset centres the window on the channel of interest, so a carrier one bin
        off the middle is a real possibility — and interpolating across three bins
        would draw over it. With the offset gone, anything still standing above its
        neighbours at the centre is signal.
        """
        n = FFT_BINS
        index = np.arange(n)
        tone = np.exp(2j * np.pi * 1 * index / n) * 0.9   # one bin off the middle

        row = iq_to_power_row(_iq_bytes(tone))

        assert row is not None
        centre = n // 2
        assert int(np.argmax(row)) == centre + 1, "the carrier must still be there"
        assert float(row[centre + 1]) > float(np.median(row)) + 20.0


#: Verbatim ``rtl_test -t`` output from this machine with both dongles attached.
_REAL_DEVICE_LIST = """Found 2 device(s):
  0:  RTLSDRBlog, Blog V4, SN: 00000001
  1:  RTLSDRBlog, Blog V4L, SN: 00000001

Using device 0: Generic RTL2832U OEM
Found Rafael Micro R828D tuner
RTL-SDR Blog V4 Detected
Supported gain values (29): 0.0 0.9 1.4 2.7
Sampling at 2048000 S/s.
"""


class TestDeviceListing:
    """The dongle list, which is what a dongle selector is built from."""

    def test_it_reads_the_real_table(self):
        devices = rtl_tools.parse_device_list(_REAL_DEVICE_LIST)

        assert [d.index for d in devices] == [0, 1]
        assert devices[0].manufacturer == "RTLSDRBlog"
        assert devices[0].product == "Blog V4"
        assert devices[1].product == "Blog V4L"

    def test_the_rest_of_the_output_is_not_mistaken_for_devices(self):
        """The gain table and the tuner report follow the list and are not dongles."""
        devices = rtl_tools.parse_device_list(_REAL_DEVICE_LIST)

        assert len(devices) == 2

    def test_two_dongles_with_one_serial_are_still_told_apart(self):
        """This machine's two dongles both report SN 00000001, hence the index."""
        devices = rtl_tools.parse_device_list(_REAL_DEVICE_LIST)

        assert devices[0].serial == devices[1].serial == "00000001"
        assert devices[0].label != devices[1].label

    def test_the_label_leads_with_the_index(self):
        assert rtl_tools.parse_device_list(_REAL_DEVICE_LIST)[0].label == "0 · Blog V4"

    def test_a_blank_eeprom_does_not_invent_a_name(self):
        """A clone lists as ", , SN: " — empty, not a device called "SN:"."""
        devices = rtl_tools.parse_device_list("Found 1 device(s):\n  0:  , , SN: \n")

        assert len(devices) == 1
        assert devices[0].manufacturer == ""
        assert devices[0].product == ""
        assert devices[0].serial == ""
        assert devices[0].label == "0 · RTL-SDR"

    def test_a_product_with_a_comma_in_it_survives(self):
        devices = rtl_tools.parse_device_list(
            "Found 1 device(s):\n  3:  Acme, Widget, Mk II, SN: 42\n"
        )

        assert devices[0].product == "Widget, Mk II"
        assert devices[0].serial == "42"

    def test_a_listing_that_names_nothing_is_empty(self):
        assert rtl_tools.parse_device_list("No supported devices found.") == []

    def test_the_description_names_the_dongle(self):
        assert rtl_tools.parse_device_list(_REAL_DEVICE_LIST)[1].describe() == (
            "RTLSDRBlog Blog V4L (SN 00000001)"
        )

    def test_listing_uses_rtl_test_and_reports_what_it_found(self, monkeypatch):
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: rtl_tools.Path("rtl_test"))
        monkeypatch.setattr(
            rtl_tools.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(stdout=_REAL_DEVICE_LIST, stderr=""),
        )

        devices, message = rtl_tools.list_devices()

        assert len(devices) == 2
        assert "2" in message

    def test_an_absent_dongle_is_explained(self, monkeypatch):
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: rtl_tools.Path("rtl_test"))
        monkeypatch.setattr(
            rtl_tools.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(
                stdout="", stderr="No supported devices found."
            ),
        )

        devices, message = rtl_tools.list_devices()

        assert devices == []
        assert "plugged in" in message

    def test_a_wedged_driver_does_not_hang_the_listing(self, monkeypatch):
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: rtl_tools.Path("rtl_test"))

        def hang(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="rtl_test", timeout=1.0)

        monkeypatch.setattr(rtl_tools.subprocess, "run", hang)

        devices, message = rtl_tools.list_devices()

        assert devices == []
        assert "did not finish" in message

    def test_a_missing_rtl_test_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: None)

        devices, message = rtl_tools.list_devices()

        assert devices == []
        assert "rtl_test was not found" in message


class TestPerDeviceLease:
    """With two dongles attached, two features can capture at once — one each."""

    def test_the_two_devices_are_held_independently(self, clean_lease):
        first, _ = rtl_tools.acquire_sdr("owner-a", "the SIGINT spectrum", 0)
        second, complaint = rtl_tools.acquire_sdr("owner-b", "a GPS capture", 1)

        assert first and second, complaint
        assert rtl_tools.sdr_owner(0) == "the SIGINT spectrum"
        assert rtl_tools.sdr_owner(1) == "a GPS capture"

    def test_the_same_device_is_still_refused(self, clean_lease):
        rtl_tools.acquire_sdr("owner-a", "the SIGINT spectrum", 1)

        held, complaint = rtl_tools.acquire_sdr("owner-b", "the band scan", 1)

        assert held is False
        assert "Dongle 1" in complaint
        assert "the SIGINT spectrum" in complaint

    def test_releasing_one_device_leaves_the_other_held(self, clean_lease):
        rtl_tools.acquire_sdr("owner-a", "one", 0)
        rtl_tools.acquire_sdr("owner-b", "two", 1)

        rtl_tools.release_sdr("owner-a", 0)

        assert rtl_tools.sdr_owner(0) == ""
        assert rtl_tools.sdr_owner(1) == "two"

    def test_releasing_someone_elses_device_does_nothing(self, clean_lease):
        rtl_tools.acquire_sdr("owner-a", "one", 1)

        rtl_tools.release_sdr("owner-b", 1)

        assert rtl_tools.sdr_owner(1) == "one"

    def test_the_default_device_is_the_first_one(self, clean_lease):
        """Every existing caller passes no index, so this must keep meaning 0."""
        rtl_tools.acquire_sdr("owner-a", "default")

        assert rtl_tools.sdr_owner() == "default"
        assert rtl_tools.sdr_owner(0) == "default"

    def test_control_characters_from_an_unopenable_device_are_dropped(self):
        """Verbatim from this machine with device 0 held by OrcMesh itself.

        A dongle that cannot be opened reports unread buffer bytes instead of its
        EEPROM strings, and the driver still lists it. Those bytes must never reach
        the selector as a device name.
        """
        devices = rtl_tools.parse_device_list(
            "Found 2 device(s):\n"
            "  0:  \x01, , SN: \xff\n"
            "  1:  RTLSDRBlog, Blog V4L, SN: 00000001\n"
        )

        assert len(devices) == 2
        assert devices[0].manufacturer == ""
        assert devices[0].product == ""
        assert devices[0].serial == ""
        assert devices[0].label == "0 · RTL-SDR"
        assert devices[1].product == "Blog V4L"

    def test_it_says_when_a_name_could_not_be_read(self):
        """Rather than inventing a name or showing mojibake."""
        devices = rtl_tools.parse_device_list("Found 1 device(s):\n  0:  , , SN: \n")

        assert "unreadable" in devices[0].describe()

    def test_the_listing_is_read_from_either_stream(self, monkeypatch):
        """rtl_test writes the table to stderr, and the decoder to stdout."""
        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: rtl_tools.Path("rtl_test"))
        monkeypatch.setattr(
            rtl_tools.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(
                stdout="",
                stderr=(
                    "Found 1 device(s):\n"
                    "  0:  RTLSDRBlog, Blog V4, SN: 00000001\n"
                    "Using device 0: Generic RTL2832U OEM\n"
                ),
            ),
        )

        devices, _message = rtl_tools.list_devices()

        assert [d.product for d in devices] == ["Blog V4"]
