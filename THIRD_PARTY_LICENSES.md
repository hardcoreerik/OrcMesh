# Third Party Licenses

OrcMesh is licensed **GPL-3.0-only** — see [LICENSE](LICENSE).
It is licensed this way because it links the official Meshtastic Python
package, which is GPL-3.0-only copyleft.

## Dependencies

| Package | License | Role |
|---|---|---|
| [meshtastic](https://github.com/meshtastic/python) | GPL-3.0-only | Radio protocol and device interface |
| [PySide6](https://wiki.qt.io/Qt_for_Python) | LGPL-3.0 | Qt GUI framework |
| [pyqtgraph](https://www.pyqtgraph.org/) | MIT | Charts and waterfall rendering |
| [numpy](https://numpy.org/) | BSD-3-Clause | Numerics |
| [platformdirs](https://github.com/platformdirs/platformdirs) | MIT | Per-user data/log paths |
| [PyPubSub](https://github.com/schollii/pypubsub) | BSD-2-Clause | Event distribution |
| [esptool](https://github.com/espressif/esptool) | GPL-2.0-or-later | ESP32 firmware flashing |
| [pyrtlsdr](https://github.com/pyrtlsdr/pyrtlsdr) | GPL-3.0 | Optional — RTL-SDR spectrum capture |

## Bundled web assets

The map view bundles these at build time via `scripts/fetch_vendors.py`:

| Asset | License |
|---|---|
| [Leaflet](https://leafletjs.com/) | BSD-2-Clause |
| [Leaflet.markercluster](https://github.com/Leaflet/Leaflet.markercluster) | MIT |
| `qwebchannel.js` (from Qt) | LGPL-3.0 |

Map tiles are served by [CARTO](https://carto.com/attributions) using
[OpenStreetMap](https://www.openstreetmap.org/copyright) data, subject to their
respective terms.
