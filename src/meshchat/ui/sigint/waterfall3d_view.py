"""A 3D waterfall: frequency across, time receding, power as height.

The 2D waterfall shows the same data, but a third dimension makes structure that
is hard to see in a scrolling image pop out — a drifting carrier, a hop between
channels, two signals trading places — because you can look along the time axis
instead of watching it slide past.

Built on pyqtgraph's OpenGL surface, which needs PyOpenGL. That import is guarded
and happens at construction, never at module import, because OpenGL is optional
and fails in more ways than one: the package can be absent, the driver can be
broken, or a remote session can have no GL context at all. Any of those must leave
the app running with the 2D waterfall instead of failing to start, so
`opengl_available()` reports which it is and the caller falls back.

The two pure helpers — decimation and normalisation — are separated out because
they decide what the picture looks like and can be tested without a GL context.
"""
from __future__ import annotations

import logging
import math

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QVector3D
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from meshchat.ui.spectrum.waterfall_view import build_colormap

log = logging.getLogger(__name__)

#: Rows of history kept. Mirrors the 2D waterfall so a switch between the two
#: views covers the same span of time.
HISTORY_ROWS = 300

#: How many rows and bins the surface is built from. A GL surface is rebuilt
#: wholesale on every frame, so this is a straight trade of detail against frame
#: rate; 96 x 160 is ~15k vertices, which redraws comfortably.
MAX_SURFACE_ROWS = 96
MAX_SURFACE_BINS = 160

#: Redraws per second. Rows arrive at roughly 60/s, and rebuilding the surface for
#: each one would spend the whole frame budget on buffers the eye cannot resolve.
FRAMES_PER_SECOND = 10

#: Camera: low and nearly frontal, so frequency reads left to right across the width
#: of the pane and the traces recede up and back. A steep azimuth turns the same
#: surface side-on and it reads as a wall.
_CAMERA_ELEVATION = 22.0
_CAMERA_AZIMUTH = -14.0

#: GLViewWidget's vertical field of view, and how much room to leave around the
#: surface once it has been fitted to the pane.
_FOV_DEG = 60.0
_CAMERA_MARGIN = 1.15

#: Percentile the noise floor is measured at. Low enough that signals cannot drag
#: it upward, high enough that one quiet outlier cannot define it.
_FLOOR_PERCENTILE = 20.0

#: Height aperture: how many dB above the floor reach full scale.
#:
#: Fixed rather than fitted to the data, and that is the difference between a
#: landscape and a hedge. Fitting to the data's own percentiles means that on a
#: band with no signals on it — which 902-928 MHz is, nearly always — the
#: percentiles ARE the noise, so a 3 dB noise spread gets stretched over the whole
#: height and every bin stands up as a full-height needle. Anchored to the floor
#: with a fixed aperture the noise is a low carpet and the height is left for
#: signals: a packet 20 dB over the floor reaches two thirds scale.
_SPAN_DB = 30.0

#: How far BELOW the measured noise floor the bottom of the scale sits.
#:
#: This is what gives the surface bulk. With the floor pinned to the bottom of the
#: plot, a noise-only band collapses to a flat sheet with no thickness, and that is
#: most of what made the pane read as a thin sliver. Anchoring the scale 6 dB under
#: the floor puts the noise carpet a fifth of the way up, so the surface is a solid
#: sheet with signals standing out of it — the look a 3D spectrum view has.
_FLOOR_OFFSET_DB = 6.0

#: Gamma applied to strength for the COLOUR ramp only, never to height.
#:
#: Colour and relief are not read the same way: the eye takes in a ridge a fifth of
#: full height, but a colour that far up a ramp whose bottom is near-black is
#: invisible. Raising strength to this power lifts the carpet into the visible part
#: of the palette while leaving the heights, and the order of the peaks, untouched.
_COLOUR_GAMMA = 0.7

#: Height, in world units, that a full-scale bin is drawn at. Purely cosmetic: the
#: x axis is in MHz and the y axis is a row index, so z only has to be big enough
#: to read as a landscape.
_Z_SCALE = 100.0

#: Width of the drawn surface, in world units.
#:
#: The axes are deliberately NOT in their natural units. Drawn in MHz against a
#: row-index depth and _Z_SCALE height, a 2 MHz span makes the surface 2 units
#: wide, 96 deep and 100 tall — a curtain seen almost edge-on, which is what the
#: pane showed: a thin ribbon down the middle and nothing readable about it.
#: Normalising the frequency axis to this width makes the surface's shape a
#: property of the pane instead of an accident of units.
#:
#: Frequency is still recoverable: world x runs 0 at the start of the capture to
#: _SURFACE_WIDTH at the end, so a bin's world x is its position across the span
#: scaled by _SURFACE_WIDTH. Anything later drawn on the mesh (a channel marker)
#: must go through the same mapping.
_SURFACE_WIDTH = 96.0


def opengl_available() -> tuple[bool, str]:
    """Return (available, reason), naming which of the failure modes it is."""
    try:
        import OpenGL  # noqa: F401
    except ImportError:
        return False, (
            "PyOpenGL is not installed, so the 3D waterfall is unavailable.\n\n"
            "Install it with:  pip install PyOpenGL"
        )
    try:
        from pyqtgraph.opengl import GLViewWidget  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on the GL stack
        return False, f"pyqtgraph's OpenGL module could not be loaded: {exc}"
    return True, "OpenGL surface available"


def _stride(size: int, limit: int) -> int:
    """A stride that keeps `size` samples within `limit`.

    Ceiling, not floor: taking every `size // limit`-th sample overshoots the
    limit (300 rows at a stride of 3 is 100, not 96).
    """
    if limit <= 0:
        return 1
    return max(1, -(-size // limit))


def decimate(history: np.ndarray, max_rows: int, max_bins: int) -> np.ndarray:
    """Thin a history buffer down to a manageable display grid.

    Strides rather than averages: the point of the display is to show where bursts
    are, and averaging neighbouring rows would smear a short transmission across
    the time axis until it disappeared. The newest row and the last bin are always
    kept, so the row being watched is never the one thinned away.
    """
    if history.size == 0:
        return history
    rows, bins = history.shape

    row_step = _stride(rows, max_rows)
    bin_step = _stride(bins, max_bins)
    # Offset the stride so the final row and final bin are landed on rather than
    # possibly stepped over. On a waterfall the final row is the live edge.
    row_offset = (rows - 1) % row_step
    bin_offset = (bins - 1) % bin_step

    thinned = history[row_offset::row_step, bin_offset::bin_step]
    return np.ascontiguousarray(thinned)


def normalise(
    power: np.ndarray,
    floor_percentile: float = _FLOOR_PERCENTILE,
    span_db: float = _SPAN_DB,
    floor_offset_db: float = _FLOOR_OFFSET_DB,
) -> np.ndarray:
    """Scale power into 0..1 as dB above this band's own noise floor.

    The floor comes from the data — so the view adapts to whatever gain the dongle
    is running at — but the aperture above it is fixed, and the scale starts
    `floor_offset_db` BELOW the floor so the noise carpet has thickness. See
    _SPAN_DB and _FLOOR_OFFSET_DB for why neither is fitted to the data.

    NaN means "never measured" and lands at the bottom, not at the top: the history
    starts NaN-initialised, and the 2D waterfall draws those rows as no data.
    """
    finite = power[np.isfinite(power)]
    if finite.size == 0:
        return np.zeros(power.shape, dtype=np.float32)

    floor = float(np.percentile(finite, floor_percentile)) - floor_offset_db
    scaled = (power - floor) / span_db
    return np.clip(np.nan_to_num(scaled, nan=0.0), 0.0, 1.0).astype(np.float32)


class Waterfall3DView(QWidget):
    """OpenGL waterfall, or a notice explaining why there isn't one.

    Deliberately presents the same `configure` / `push_row` / `clear` surface as
    the 2D `WaterfallView`, so a caller can substitute one for the other.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        self._bins = 0
        self._history: np.ndarray | None = None
        self._center_hz = 0.0
        self._span_hz = 0.0
        self._surface = None
        self._gl = None
        self._notice = None
        self._timer = None
        #: The camera cannot be framed until there is data to frame, so the first
        #: redraw after every configure() does it.
        self._camera_pending = True

        # Only the availability *check* happens here: it imports modules, which is
        # harmless. Nothing that creates a GL context does — see activate().
        self._available, self._reason = opengl_available()

    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """False when OpenGL cannot be used here."""
        return self._available

    @property
    def active(self) -> bool:
        """True once the GL surface exists."""
        return self._surface is not None

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    def activate(self) -> bool:
        """Create the OpenGL surface and start redrawing. Returns success.

        Deliberately not done in __init__, and the reason is not thrift. Making a
        GLViewWidget makes a QOpenGLWidget, which forces **native OpenGL
        composition on whichever top-level window contains it** — while the map's
        QWebEngineView needs Qt Quick's composition instead. Measured in this app:
        with the GL widget created at startup, the WebEngine view reported "Failed
        to get a QRhi from the top-level widget's window" and could not render at
        all. So the surface is only built once the caller has given this view a
        window of its own, where the two cannot collide.
        """
        if self._surface is not None:
            self._start_timer()
            return True
        if not self._available:
            self._show_notice()
            return False
        try:
            self._build_surface()
        except Exception as exc:  # pragma: no cover - depends on the GL stack
            log.exception("Could not create the OpenGL surface")
            self._available = False
            self._reason = f"Could not create the OpenGL surface: {exc}"
            self._show_notice()
            return False
        self._start_timer()
        return True

    def _start_timer(self) -> None:
        if self._timer is None:
            # Redraw on a timer rather than per row: see FRAMES_PER_SECOND.
            self._timer = QTimer(self)
            self._timer.setInterval(int(1000 / FRAMES_PER_SECOND))
            self._timer.timeout.connect(self._redraw)
        self._timer.start()

    def _show_notice(self) -> None:
        if self._notice is not None:
            return
        self._notice = QLabel(
            f"3D waterfall unavailable.\n\n{self._reason}\n\n"
            "The 2D waterfall shows the same data."
        )
        self._notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._notice.setWordWrap(True)
        self._notice.setStyleSheet("color: #5A6690; font-size: 13px; padding: 30px;")
        self._layout.addWidget(self._notice)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Re-fit the camera: the pane lives in a splitter, so its aspect changes.

        The fit depends on how wide the pane is (see _frame_camera), so a stale one
        leaves the surface the wrong size and off to one side the moment the splitter
        handle is dragged. A resize is an explicit layout decision, which is why it is
        safe to overrule whatever the camera was doing.
        """
        super().resizeEvent(event)
        self._camera_pending = True

    def _build_surface(self) -> None:
        import pyqtgraph.opengl as gl

        self._gl = gl.GLViewWidget()
        # Only the background is set here. Framing the camera takes the data's own
        # bounds, which do not exist yet — see _frame_camera.
        self._gl.setBackgroundColor((7, 13, 31))
        self._surface = gl.GLSurfacePlotItem(shader="shaded", smooth=False)
        # Data before display: an empty surface makes pyqtgraph's mesh item try to
        # build face normals from nothing, which logs a TypeError traceback on the
        # first paint and reads like a crash in the log.
        self._surface.setData(
            x=np.zeros(2),
            y=np.zeros(2),
            z=np.zeros((2, 2)),
            colors=np.zeros((4, 4), dtype=np.ubyte),
        )
        self._gl.addItem(self._surface)
        self._layout.addWidget(self._gl)

    def configure(self, center_hz: float, span_hz: float, bins: int) -> None:
        """Reset for a new capture geometry, exactly like the 2D view."""
        self._center_hz = center_hz
        self._span_hz = span_hz
        self._bins = bins
        self._history = np.zeros((HISTORY_ROWS, bins), dtype=np.float32)
        # A new capture can be anywhere in the band, so the mesh has to be framed
        # again: the old centre may be hundreds of MHz away.
        self._camera_pending = True

    def push_row(self, power_db: np.ndarray) -> None:
        """Append one FFT power row.

        Buffered whether or not the surface exists yet, so opening the 3D window
        mid-capture shows the history already gathered rather than an empty grid.
        """
        if self._history is None or power_db.size != self._bins:
            return
        self._history[:-1] = self._history[1:]
        self._history[-1] = power_db

    def clear(self) -> None:
        if self._history is not None:
            self._history.fill(0.0)
            if self._available:
                self._redraw()

    def stop(self) -> None:
        """Stop redrawing — called when the window or the page goes away."""
        if self._timer is not None:
            self._timer.stop()

    # ------------------------------------------------------------------

    def _redraw(self) -> bool:
        """Rebuild the surface. Returns False when nothing was drawn.

        Returning a result rather than swallowing everything is deliberate: this
        method previously caught every exception and logged it, which meant a
        shape mistake in the surface data failed on every frame while the tests
        stayed green. A caller (and a test) can now tell that nothing was drawn.
        """
        if self._surface is None or self._history is None or self._bins == 0:
            return False

        grid = decimate(self._history, MAX_SURFACE_ROWS, MAX_SURFACE_BINS)
        rows, bins = grid.shape
        if rows < 2 or bins < 2:
            return False

        # Built from the span rather than from the bin stride: decimation decides
        # how many points there are, but the axis still has to cover exactly the
        # frequencies that were captured, or the display would sit under the
        # wrong labels. It is drawn 0.._SURFACE_WIDTH rather than in MHz — see
        # _SURFACE_WIDTH for why.
        x = np.linspace(0.0, _SURFACE_WIDTH, bins)
        # Row index along the time axis: oldest at the far edge, newest nearest.
        y = np.arange(rows, dtype=np.float32)

        if self._camera_pending:
            self._frame_camera(x, rows)

        scaled = normalise(grid)
        # pyqtgraph wants z[i, j] for x[i] and y[j]. x is frequency and y is time,
        # so the (time, frequency) grid has to be transposed — without this every
        # frame raises "Z values must have shape (len(x), len(y))" and the surface
        # stays empty.
        flat = scaled.T
        z = (flat * _Z_SCALE).astype(np.float32)
        # Colour is the same strength, gamma-shaped: see _COLOUR_GAMMA. Ravel order
        # matches flat's (bins, rows), which is what setData's colours argument has
        # to line up with.
        colours = build_colormap().map(
            np.power(flat, _COLOUR_GAMMA).ravel(), mode="byte"
        )

        try:
            self._surface.setData(x=x, y=y, z=z, colors=colours)
        except Exception:
            # A GL context can be lost (driver reset, session change) and every
            # later frame would raise the same way. Report once and stop rather
            # than flooding the log at ten frames a second.
            log.exception("3D waterfall redraw failed; stopping")
            self.stop()
            return False
        return True

    def _frame_camera(self, x: np.ndarray, rows: int) -> None:
        """Aim the camera at the mesh instead of at the origin, and size it to fit.

        Two things had to be true for this pane to show anything. First, the camera
        has to look at the mesh: the x axis is real frequency in MHz, so the mesh sits
        around x = 906 while pyqtgraph's camera looks at (0, 0, 0), which puts the
        whole surface off the side of the frustum however far back it stands.

        Second, it has to stand the right distance away, which depends on the pane's
        shape — the pane is one column of a splitter, so it is wide rather than square.
        A distance that fits the surface's diagonal wastes most of a wide pane, and one
        that fits the width alone crops the ridges off the top.
        """
        span = float(x[-1] - x[0])
        azimuth = math.radians(_CAMERA_AZIMUTH)
        elevation = math.radians(_CAMERA_ELEVATION)
        # What the surface covers on screen: its width, plus the depth it gains from
        # being turned away from us; and its height, plus the depth it gains from
        # being looked down on.
        screen_width = abs(span * math.cos(azimuth)) + abs(rows * math.sin(azimuth))
        screen_height = abs(_Z_SCALE * math.cos(elevation)) + abs(rows * math.sin(elevation))

        half_fov = math.radians(_FOV_DEG / 2)
        height_px = self._gl.height()
        aspect = self._gl.width() / height_px if height_px else 1.0
        # Whichever of the two runs out of room first decides how far back to stand.
        distance = max(
            (screen_height / 2) / math.tan(half_fov),
            (screen_width / 2) / (aspect * math.tan(half_fov)),
        ) * _CAMERA_MARGIN

        self._gl.setCameraPosition(
            pos=QVector3D(float(x[0]) + span / 2, rows / 2.0, _Z_SCALE / 2),
            distance=distance,
            elevation=_CAMERA_ELEVATION,
            azimuth=_CAMERA_AZIMUTH,
        )
        self._camera_pending = False
