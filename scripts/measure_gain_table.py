"""Re-measure the gain table now that a 915 MHz whip is attached.

Uses rtl_sdr + the app's own FFT, so the numbers are on the same scale the
waterfall displays (`10*log10(mean|FFT|^2)`, where a full-scale sine is ~+54 dB).
rtl_power's scale is deliberately not used: it is not absolute and does not track
gain, which is why a recommendation cannot be made from it.
"""
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from meshchat.services import rtl_tools  # noqa: E402
from meshchat.services.sdr_source import iq_to_power_row  # noqa: E402

tool = rtl_tools.find_tool("rtl_sdr")
if tool is None:
    raise SystemExit("rtl_sdr not found")

#: A full-scale sine into this FFT.
FULL_SCALE_DB = 10 * np.log10((1024 / 2) ** 2)

print(f"scale reference: a full-scale sine reads about {FULL_SCALE_DB:+.0f} dB\n")
print(f"{'gain':>6} {'floor':>8} {'median':>8} {'p95':>7} {'max':>7} "
      f"{'spread':>7} {'headroom to full scale':>23}")

for gain in ("0", "3.7", "7.7", "12.5", "15.7", "19.7", "28.0", "40.2"):
    proc = subprocess.Popen(
        [str(tool), "-f", "915000000", "-s", "2560000", "-g", gain, "-p", "0",
         "-n", "1024000", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=rtl_tools.creation_flags(),
    )
    rows = []
    while True:
        raw = proc.stdout.read(32768 * 2)
        if not raw:
            break
        row = iq_to_power_row(raw)
        if row is not None:
            rows.append(row)
    proc.stderr.read()
    proc.wait()
    if not rows:
        print(f"{gain:>6}   no data")
        continue

    flat = np.concatenate(rows)
    p5, median, p95 = np.percentile(flat, [5, 50, 95])
    headroom = FULL_SCALE_DB - float(p95)
    print(f"{gain:>6} {p5:>8.1f} {median:>8.1f} {p95:>7.1f} {flat.max():>7.1f} "
          f"{p95 - p5:>7.1f} {headroom:>20.0f} dB")
    time.sleep(0.2)
