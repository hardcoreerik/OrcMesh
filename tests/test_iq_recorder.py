"""Tests for I/Q recording and replay.

No dongle: a capture is just bytes, so these write synthetic tones and read them
back. The last test goes the whole way round — synthesise I/Q, record it, replay
it through the FFT — which is what catches an alignment mistake that would
otherwise only show up as a scrambled spectrum on a real capture.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from meshchat.services.iq_recorder import (
    BYTES_PER_SAMPLE,
    CaptureError,
    IqRecorder,
    estimated_bytes_per_second,
    format_duration,
    format_size,
    iter_capture_chunks,
    read_capture,
)
from meshchat.services.sdr_source import FFT_BINS, replay_rows

_CENTER_HZ = 915.0e6
_RATE_HZ = 2.56e6


def _tone_bytes(samples: int, bin_index: int = FFT_BINS // 4, amplitude: float = 0.9) -> bytes:
    """Interleaved 8-bit I/Q of a pure tone, the way rtl_sdr would write it."""
    index = np.arange(samples)
    tone = np.exp(2j * np.pi * bin_index * index / FFT_BINS) * amplitude
    i = np.clip(np.round(tone.real * 127.5 + 127.5), 0, 255).astype(np.uint8)
    q = np.clip(np.round(tone.imag * 127.5 + 127.5), 0, 255).astype(np.uint8)
    return np.stack((i, q), axis=1).tobytes()


def _recorder(path: Path, **kwargs) -> IqRecorder:
    params = {"center_hz": _CENTER_HZ, "sample_rate_hz": _RATE_HZ, "gain_db": 16.0}
    params.update(kwargs)
    return IqRecorder(path, **params)


class TestSizing:
    def test_bytes_per_second_for_eight_bit_iq(self):
        assert estimated_bytes_per_second(_RATE_HZ) == pytest.approx(5.12e6)

    def test_a_minute_of_recording_is_about_307_mb(self):
        """The number a caller needs before promising to record anything."""
        per_minute = estimated_bytes_per_second(_RATE_HZ) * 60

        assert format_size(per_minute) == "307.2 MB"

    @pytest.mark.parametrize("value,expected", [
        (500.0, "500 B"),
        (1_500.0, "1.5 kB"),
        (2_500_000.0, "2.5 MB"),
        (3_500_000_000.0, "3.5 GB"),
    ])
    def test_sizes_read_naturally(self, value, expected):
        assert format_size(value) == expected

    def test_sizes_are_decimal_because_drives_are(self):
        assert format_size(1_000_000) == "1.0 MB"

    @pytest.mark.parametrize("seconds,expected", [
        (0.0, "0.0 s"),
        (45.5, "45.5 s"),
        (90.0, "1m 30.0s"),
        (3700.0, "1h 01m"),
    ])
    def test_durations_read_naturally(self, seconds, expected):
        assert format_duration(seconds) == expected


class TestRecording:
    def test_it_writes_the_bytes_it_is_given(self, tmp_path):
        path = tmp_path / "cap.iq"
        recorder = _recorder(path)

        recorder.write(b"\x01\x02\x03\x04")
        info = recorder.close()

        assert path.read_bytes() == b"\x01\x02\x03\x04"
        assert info.samples == 2

    def test_it_counts_samples_not_bytes(self, tmp_path):
        recorder = _recorder(tmp_path / "cap.iq")

        recorder.write(_tone_bytes(1000))
        info = recorder.close()

        assert info.samples == 1000
        assert info.size_bytes == 2000

    def test_an_odd_chunk_is_cut_to_whole_samples(self, tmp_path):
        """A stray byte would swap I and Q for the rest of the file."""
        path = tmp_path / "cap.iq"
        recorder = _recorder(path)

        recorder.write(b"\x01\x02\x03")
        info = recorder.close()

        assert path.read_bytes() == b"\x01\x02"
        assert info.samples == 1

    def test_the_parent_directory_is_created(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "cap.iq"

        recorder = _recorder(path)
        recorder.write(b"\x01\x02")
        recorder.close()

        assert path.is_file()

    def test_the_sidecar_records_what_the_receiver_was_set_to(self, tmp_path):
        path = tmp_path / "cap.iq"
        _recorder(path, gain_db=21.5, ppm=3).close()

        data = json.loads(Path(f"{path}.json").read_text(encoding="utf-8"))

        assert data["center_hz"] == _CENTER_HZ
        assert data["sample_rate_hz"] == _RATE_HZ
        assert data["gain_db"] == 21.5
        assert data["ppm"] == 3
        assert data["sample_format"] == "u8"
        assert data["tool"] == "rtl_sdr"

    def test_duration_comes_from_the_rate(self, tmp_path):
        recorder = _recorder(tmp_path / "cap.iq")

        recorder.write(_tone_bytes(int(_RATE_HZ / 2)))
        info = recorder.close()

        assert info.duration_s == pytest.approx(0.5, abs=0.01)

    def test_closing_twice_is_safe(self, tmp_path):
        recorder = _recorder(tmp_path / "cap.iq")
        recorder.write(b"\x01\x02")

        first = recorder.close()
        second = recorder.close()

        assert first.samples == second.samples == 1

    def test_writing_after_close_is_an_error(self, tmp_path):
        recorder = _recorder(tmp_path / "cap.iq")
        recorder.close()

        with pytest.raises(CaptureError):
            recorder.write(b"\x01\x02")

    def test_it_works_as_a_context_manager(self, tmp_path):
        path = tmp_path / "cap.iq"

        with _recorder(path) as recorder:
            recorder.write(b"\x01\x02")

        assert path.is_file()
        assert Path(f"{path}.json").is_file()

    def test_an_unwritable_path_is_reported_as_a_capture_error(self, tmp_path):
        """A parent that is a file rather than a directory cannot be created."""
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a folder", encoding="utf-8")

        with pytest.raises(CaptureError):
            _recorder(blocker / "cap.iq")


class TestSizeLimit:
    def test_recording_stops_at_the_limit(self, tmp_path):
        path = tmp_path / "cap.iq"
        recorder = _recorder(path, max_bytes=8)

        recorder.write(b"\x00" * 20)
        info = recorder.close()

        assert info.samples == 4, "eight bytes is four samples"
        assert info.truncated is True
        assert path.stat().st_size == 8

    def test_it_never_writes_a_half_sample_to_fill_the_limit(self, tmp_path):
        path = tmp_path / "cap.iq"
        recorder = _recorder(path, max_bytes=7)

        recorder.write(b"\x00" * 20)
        info = recorder.close()

        assert path.stat().st_size == 6, "an odd limit must round down to a whole sample"
        assert info.samples == 3

    def test_later_writes_are_ignored_once_full(self, tmp_path):
        path = tmp_path / "cap.iq"
        recorder = _recorder(path, max_bytes=4)

        recorder.write(b"\x00" * 4)
        recorder.write(b"\xff" * 4)
        recorder.close()

        assert path.read_bytes() == b"\x00" * 4

    def test_an_untruncated_recording_says_so(self, tmp_path):
        recorder = _recorder(tmp_path / "cap.iq", max_bytes=1000)

        recorder.write(b"\x00" * 8)
        info = recorder.close()

        assert info.truncated is False

    def test_a_truncated_capture_is_flagged_when_described(self, tmp_path):
        recorder = _recorder(tmp_path / "cap.iq", max_bytes=4)
        recorder.write(b"\x00" * 8)
        info = recorder.close()

        assert "TRUNCATED" in info.describe()


class TestReadingCaptures:
    def test_a_recording_round_trips(self, tmp_path):
        path = tmp_path / "cap.iq"
        recorder = _recorder(path, gain_db=16.0)
        recorder.write(_tone_bytes(500))
        written = recorder.close()

        read_back = read_capture(path)

        assert read_back.samples == written.samples
        assert read_back.center_hz == _CENTER_HZ
        assert read_back.sample_rate_hz == _RATE_HZ
        assert read_back.gain_db == 16.0

    def test_a_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(CaptureError):
            read_capture(tmp_path / "nope.iq")

    def test_a_missing_sidecar_falls_back_to_the_file_size(self, tmp_path):
        """Samples are recoverable from the size, so this stays replayable."""
        path = tmp_path / "cap.iq"
        path.write_bytes(b"\x00" * 400)

        info = read_capture(path)

        assert info.samples == 200
        assert info.center_hz == 0.0, "unknown, and reported as unknown"

    def test_a_sidecar_claiming_more_than_the_file_holds_loses(self, tmp_path):
        """An interrupted recording may never have updated its sidecar."""
        path = tmp_path / "cap.iq"
        path.write_bytes(b"\x00" * 200)
        Path(f"{path}.json").write_text(
            json.dumps({"samples": 99_999, "center_hz": _CENTER_HZ}), encoding="utf-8"
        )

        info = read_capture(path)

        assert info.samples == 100, "the file is what a replay will actually produce"

    def test_a_sidecar_claiming_fewer_than_the_file_holds_also_loses(self, tmp_path):
        path = tmp_path / "cap.iq"
        path.write_bytes(b"\x00" * 200)
        Path(f"{path}.json").write_text(json.dumps({"samples": 3}), encoding="utf-8")

        assert read_capture(path).samples == 100

    def test_a_corrupt_sidecar_is_reported(self, tmp_path):
        path = tmp_path / "cap.iq"
        path.write_bytes(b"\x00" * 200)
        Path(f"{path}.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(CaptureError):
            read_capture(path)

    def test_a_sidecar_that_is_not_an_object_is_reported(self, tmp_path):
        path = tmp_path / "cap.iq"
        path.write_bytes(b"\x00" * 200)
        Path(f"{path}.json").write_text("[1, 2]", encoding="utf-8")

        with pytest.raises(CaptureError):
            read_capture(path)

    def test_an_empty_recording_reads_as_zero_samples(self, tmp_path):
        path = tmp_path / "cap.iq"
        recorder = _recorder(path)
        info = recorder.close()

        assert info.samples == 0
        assert read_capture(path).samples == 0
        assert info.duration_s == 0.0

    def test_describe_names_the_receiver_settings(self, tmp_path):
        recorder = _recorder(tmp_path / "cap.iq")
        recorder.write(_tone_bytes(2560))
        info = recorder.close()

        described = info.describe()

        assert "915.000 MHz" in described
        assert "2.56 MS/s" in described


class TestChunking:
    def test_chunks_are_whole_samples(self, tmp_path):
        path = tmp_path / "cap.iq"
        path.write_bytes(b"\x00" * 1000)

        for chunk in iter_capture_chunks(path, chunk_bytes=100):
            assert len(chunk) % BYTES_PER_SAMPLE == 0

    def test_an_odd_chunk_size_is_rounded_to_whole_samples(self, tmp_path):
        path = tmp_path / "cap.iq"
        path.write_bytes(b"\x00" * 1000)

        chunks = list(iter_capture_chunks(path, chunk_bytes=101))

        assert all(len(chunk) % 2 == 0 for chunk in chunks)

    def test_a_trailing_odd_byte_is_dropped_not_passed_on(self, tmp_path):
        path = tmp_path / "cap.iq"
        path.write_bytes(b"\x01" * 999)

        recovered = b"".join(iter_capture_chunks(path, chunk_bytes=100))

        assert len(recovered) == 998
        assert len(recovered) % 2 == 0

    def test_the_whole_file_comes_back(self, tmp_path):
        path = tmp_path / "cap.iq"
        payload = bytes(range(256)) * 4
        path.write_bytes(payload)

        assert b"".join(iter_capture_chunks(path, chunk_bytes=64)) == payload

    def test_a_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            list(iter_capture_chunks(tmp_path / "nope.iq"))


class TestReplay:
    def test_a_recorded_tone_replays_to_the_right_bin(self, tmp_path):
        """The whole way round: synthesise, record, replay, check the peak.

        This is what catches an off-by-one in the chunking, which would show up
        on a real capture as a spectrum that looks nearly right.
        """
        path = tmp_path / "cap.iq"
        with _recorder(path) as recorder:
            recorder.write(_tone_bytes(FFT_BINS * 8, bin_index=FFT_BINS // 4))

        rows = list(replay_rows(path))

        assert rows, "a recording of a tone must produce rows"
        peak = int(np.argmax(rows[0]))
        assert peak == 3 * FFT_BINS // 4, "a +fs/4 tone lands three quarters across"

    def test_a_replay_of_quiet_input_is_flat(self, tmp_path):
        """Quiet input is dither around the midpoint, not a constant level.

        A constant is a DC term and correctly piles its energy into the centre
        bin, so it would not read as flat — see the equivalent test in
        test_sdr_source.py.
        """
        rng = np.random.default_rng(7)
        dither = rng.integers(126, 130, size=FFT_BINS * 2 * 4, dtype=np.uint8).tobytes()
        path = tmp_path / "cap.iq"
        with _recorder(path) as recorder:
            recorder.write(dither)

        rows = list(replay_rows(path))

        assert rows
        assert float(rows[0].max()) < 0.0
        assert float(rows[0].max()) - float(np.median(rows[0])) < 15.0

    def test_an_empty_recording_replays_to_nothing(self, tmp_path):
        path = tmp_path / "cap.iq"
        with _recorder(path) as recorder:
            recorder.write(b"")

        assert list(replay_rows(path)) == []

    def test_a_short_recording_yields_nothing_rather_than_a_wrong_row(self, tmp_path):
        path = tmp_path / "cap.iq"
        with _recorder(path) as recorder:
            recorder.write(b"\x00" * 100)

        assert list(replay_rows(path)) == []

    def test_a_replay_can_be_limited_to_smaller_chunks(self, tmp_path):
        path = tmp_path / "cap.iq"
        with _recorder(path) as recorder:
            recorder.write(_tone_bytes(FFT_BINS * 8))

        coarse = list(replay_rows(path, chunk_samples=FFT_BINS * 8))
        fine = list(replay_rows(path, chunk_samples=FFT_BINS * 2))

        assert len(fine) > len(coarse), "smaller chunks give more, finer rows"

    def test_the_row_count_follows_the_chunk_size(self, tmp_path):
        """Rows are per read, not per frame: one read of a whole file is one row."""
        path = tmp_path / "cap.iq"
        with _recorder(path) as recorder:
            recorder.write(_tone_bytes(FFT_BINS * 4))

        assert len(list(replay_rows(path))) == 1, "the default chunk takes the lot"
        assert len(list(replay_rows(path, chunk_samples=FFT_BINS))) == 4


class TestCaptureDatetime:
    def test_started_at_is_recorded_and_read_back(self, tmp_path):
        path = tmp_path / "cap.iq"
        recorder = _recorder(path)
        recorder.write(b"\x01\x02")
        written = recorder.close()

        read_back = read_capture(path)

        assert read_back.started_at.tzinfo is not None, "captures are time-stamped in UTC"
        assert abs((read_back.started_at - written.started_at).total_seconds()) < 1.0

    def test_a_bad_timestamp_falls_back_to_the_file_mtime(self, tmp_path):
        path = tmp_path / "cap.iq"
        path.write_bytes(b"\x00" * 200)
        Path(f"{path}.json").write_text(
            json.dumps({"started_at": "not a date", "center_hz": _CENTER_HZ}),
            encoding="utf-8",
        )

        info = read_capture(path)

        assert isinstance(info.started_at, datetime)
        assert info.started_at.tzinfo is not None
        assert info.center_hz == _CENTER_HZ

    def test_the_sidecar_is_utf8_json_that_any_tool_can_read(self, tmp_path):
        path = tmp_path / "cap.iq"
        info = _recorder(path).close()

        raw = Path(f"{path}.json").read_text(encoding="utf-8")
        data = json.loads(raw)

        assert raw == info.to_json()
        assert data["started_at"], "a capture is timestamped so it can be placed in time"
