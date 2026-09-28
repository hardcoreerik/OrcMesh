"""Recording and replaying raw I/Q, so a moment can be looked at again.

A waterfall only ever shows its last few hundred rows — once a burst has scrolled
off the bottom it is gone, and anything that happens while you are looking at the
other half of the screen is gone with it. Recording keeps the samples themselves,
so a capture can be replayed through the same FFT afterwards, without the dongle,
and at whatever speed suits.

The format is rtl_sdr's own: interleaved **unsigned 8-bit** I/Q — literally the
bytes the tool writes to stdout — so the file is also readable by anything else
that consumes rtl_sdr output. A sidecar ``.json`` records what the receiver was
set to, because samples mean nothing without knowing the centre frequency and
rate they were taken at.

Size is the number to watch: 8-bit I/Q at 2.56 MS/s is 5.12 MB/s, about 307 MB a
minute. `estimated_bytes_per_second` and the formatted helpers are here so a
caller can show that before recording, and `max_bytes` caps it.

Bytes only, no FFT: turning samples into rows is `sdr_source`'s job, which keeps
this module free of any dependency on the capture worker.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

#: rtl_sdr writes I and Q as one byte each.
BYTES_PER_SAMPLE = 2

#: Only this is supported; reading anything else would need a converter and
#: there is no producer of another format here.
SAMPLE_FORMAT = "u8"

_SIDECAR_SUFFIX = ".json"


class CaptureError(RuntimeError):
    """Raised when a capture file or its sidecar cannot be read or written."""


def estimated_bytes_per_second(sample_rate_hz: float) -> float:
    """Bytes a second of recording costs at this sample rate."""
    return sample_rate_hz * BYTES_PER_SAMPLE


def format_size(num_bytes: float) -> str:
    """Human-readable size. Deliberately decimal: drive capacities are."""
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if abs(num_bytes) >= scale:
            return f"{num_bytes / scale:.1f} {unit}"
    return f"{num_bytes:.0f} B"


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {remainder:04.1f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes):02d}m"


@dataclass(frozen=True)
class CaptureInfo:
    """What a capture file contains, and what the receiver was set to."""

    path: Path
    center_hz: float
    sample_rate_hz: float
    gain_db: float
    ppm: int
    samples: int
    started_at: datetime
    ended_at: datetime | None = None
    #: True when recording stopped at `max_bytes` rather than by the user, so a
    #: replayed file is not mistaken for the whole of what happened.
    truncated: bool = False

    @property
    def sample_format(self) -> str:
        return SAMPLE_FORMAT

    @property
    def size_bytes(self) -> int:
        return self.samples * BYTES_PER_SAMPLE

    @property
    def duration_s(self) -> float:
        if self.sample_rate_hz <= 0:
            return 0.0
        return self.samples / self.sample_rate_hz

    @property
    def sidecar_path(self) -> Path:
        return Path(f"{self.path}{_SIDECAR_SUFFIX}")

    def to_json(self) -> str:
        return json.dumps(
            {
                "center_hz": self.center_hz,
                "sample_rate_hz": self.sample_rate_hz,
                "gain_db": self.gain_db,
                "ppm": self.ppm,
                "sample_format": SAMPLE_FORMAT,
                "samples": self.samples,
                "started_at": self.started_at.isoformat(),
                "ended_at": self.ended_at.isoformat() if self.ended_at else None,
                "truncated": self.truncated,
                "tool": "rtl_sdr",
            },
            indent=2,
            sort_keys=True,
        )

    def describe(self) -> str:
        """One line for a status bar or a log."""
        span = f"{self.center_hz / 1e6:.3f} MHz"
        parts = [
            f"{self.duration_s:.1f}s",
            format_size(self.size_bytes),
            f"{self.center_hz / 1e6:.3f} MHz @ {self.sample_rate_hz / 1e6:.2f} MS/s",
            f"gain {self.gain_db:.1f} dB",
        ]
        if self.truncated:
            parts.append("TRUNCATED at the size limit")
        return f"{span}: " + ", ".join(parts)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def read_capture(path: Path) -> CaptureInfo:
    """Load a capture's sidecar.

    Falls back to measuring the file when the sidecar is missing or unreadable:
    the sample count is recoverable from the file size, and a capture whose
    centre frequency is unknown is still more useful to replay than one that
    refuses to open.
    """
    path = Path(path)
    sidecar = Path(f"{path}{_SIDECAR_SUFFIX}")
    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        raise CaptureError(f"Cannot read {path}: {exc}") from exc

    samples = size_bytes // BYTES_PER_SAMPLE
    if not sidecar.is_file():
        log.info("No sidecar for %s; replaying with unknown receiver settings", path.name)
        return CaptureInfo(
            path=path, center_hz=0.0, sample_rate_hz=0.0, gain_db=0.0, ppm=0,
            samples=samples, started_at=datetime.fromtimestamp(
                path.stat().st_mtime, tz=UTC
            ),
        )

    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaptureError(f"Cannot read {sidecar}: {exc}") from exc

    if not isinstance(data, dict):
        raise CaptureError(f"{sidecar} does not contain a capture description")

    recorded = data.get("samples")
    # The file on disk wins over the sidecar: if a recording was interrupted the
    # sidecar may never have been updated, and the samples that are actually
    # there are what a replay will produce.
    true_samples = samples
    if isinstance(recorded, int) and recorded > true_samples:
        log.warning(
            "%s says %d samples but only holds %d; using the file",
            sidecar.name, recorded, true_samples,
        )

    return CaptureInfo(
        path=path,
        center_hz=float(data.get("center_hz") or 0.0),
        sample_rate_hz=float(data.get("sample_rate_hz") or 0.0),
        gain_db=float(data.get("gain_db") or 0.0),
        ppm=int(data.get("ppm") or 0),
        samples=true_samples,
        started_at=_parse_datetime(data.get("started_at")) or datetime.fromtimestamp(
            path.stat().st_mtime, tz=UTC
        ),
        ended_at=_parse_datetime(data.get("ended_at")),
        truncated=bool(data.get("truncated", False)),
    )


def iter_capture_chunks(path: Path, chunk_bytes: int = 65_536) -> Iterator[bytes]:
    """Yield a capture file in chunks, aligned to whole samples.

    Alignment matters: an odd-length read would leave I and Q swapped for the
    rest of the file, which shows up as a scrambled spectrum rather than as an
    error. The trailing byte of an odd-sized file is dropped instead.
    """
    chunk_bytes = max(BYTES_PER_SAMPLE, chunk_bytes - (chunk_bytes % BYTES_PER_SAMPLE))
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                return
            if len(chunk) % BYTES_PER_SAMPLE:
                chunk = chunk[: len(chunk) - 1]
                if not chunk:
                    return
            yield chunk


class IqRecorder:
    """Writes a capture's byte stream to disk.

    Not a thread and not a QObject: the caller already has a thread reading the
    dongle, and this is just the file it tees into. That keeps the write on the
    same thread as the read, in stream order, with no queue to fall behind.
    """

    def __init__(
        self,
        path: Path,
        *,
        center_hz: float,
        sample_rate_hz: float,
        gain_db: float = 0.0,
        ppm: int = 0,
        max_bytes: int | None = None,
    ) -> None:
        self._path = Path(path)
        self._center_hz = center_hz
        self._sample_rate_hz = sample_rate_hz
        self._gain_db = gain_db
        self._ppm = ppm
        self._max_bytes = max_bytes
        self._samples = 0
        self._truncated = False
        self._closed = False
        self._started_at = datetime.now(UTC)
        self._handle = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self._path, "wb")
        except OSError as exc:
            raise CaptureError(f"Cannot write {self._path}: {exc}") from exc

    @property
    def path(self) -> Path:
        return self._path

    @property
    def samples(self) -> int:
        return self._samples

    @property
    def bytes_written(self) -> int:
        return self._samples * BYTES_PER_SAMPLE

    @property
    def duration_s(self) -> float:
        if self._sample_rate_hz <= 0:
            return 0.0
        return self._samples / self._sample_rate_hz

    @property
    def truncated(self) -> bool:
        """True once the size limit stopped the recording short."""
        return self._truncated

    @property
    def closed(self) -> bool:
        return self._closed

    def write(self, raw: bytes) -> None:
        """Append a chunk of the stream.

        A chunk that would cross the size limit is cut at a whole sample rather
        than dropped, so the file always ends on a Q byte.
        """
        if self._closed:
            raise CaptureError("recorder is closed")
        if not raw or self._truncated:
            return

        data = raw
        if len(data) % BYTES_PER_SAMPLE:
            # Keep the stream aligned even if the producer handed us an odd byte.
            data = data[: len(data) - 1]
        if not data:
            return

        if self._max_bytes is not None:
            remaining = self._max_bytes - self.bytes_written
            if remaining <= 0:
                self._truncated = True
                return
            if len(data) > remaining:
                data = data[: remaining - (remaining % BYTES_PER_SAMPLE)]
                self._truncated = True
                if not data:
                    return

        handle = self._handle
        if handle is None:  # pragma: no cover - guarded by the open in __init__
            raise CaptureError("recorder has no open file")
        try:
            handle.write(data)
        except OSError as exc:
            self._closed = True
            raise CaptureError(f"Recording to {self._path} failed: {exc}") from exc
        self._samples += len(data) // BYTES_PER_SAMPLE

    def close(self) -> CaptureInfo:
        """Finish the recording and write the sidecar. Safe to call twice."""
        if not self._closed:
            self._closed = True
            handle, self._handle = self._handle, None
            if handle is not None:
                try:
                    handle.flush()
                    handle.close()
                except OSError as exc:
                    log.warning("Closing %s failed: %s", self._path, exc)

        info = CaptureInfo(
            path=self._path,
            center_hz=self._center_hz,
            sample_rate_hz=self._sample_rate_hz,
            gain_db=self._gain_db,
            ppm=self._ppm,
            samples=self._samples,
            started_at=self._started_at,
            ended_at=datetime.now(UTC),
            truncated=self._truncated,
        )
        try:
            info.sidecar_path.write_text(info.to_json(), encoding="utf-8")
        except OSError as exc:
            # The samples are safely on disk, so this is worth reporting but is
            # not a reason to lose the recording.
            log.warning("Could not write %s: %s", info.sidecar_path, exc)
        return info

    def __enter__(self) -> IqRecorder:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
