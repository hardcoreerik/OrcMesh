"""Tests for WaterfallView's pyqtgraph image axis order.

pyqtgraph's default ImageItem axis order is 'col-major' (data[x, y]).
WaterfallView's _history array is built as (time_samples, freq_bins) —
'row-major' semantics (data[y, x]) — which must be configured explicitly
or the waterfall renders with time and frequency swapped relative to its
own axis labels and the frequency-based channel markers drawn on top of
it.
"""
from __future__ import annotations

import pyqtgraph as pg

# Importing the module applies the pg.setConfigOptions() call at module
# scope — this is the actual behavior under test, not a side effect to
# work around.
import meshchat.ui.spectrum.waterfall_view  # noqa: F401


class TestImageAxisOrder:
    def test_row_major_is_configured(self):
        assert pg.getConfigOption("imageAxisOrder") == "row-major"
