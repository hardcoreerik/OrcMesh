# Roadmap

Source documents for this project's original intent live outside this repo,
at `F:\Ai\MeshMonitor\`:

- `CLAUDE_MESHCHAT_WINDOWS_BUILD_SPEC.md` — the original build spec (MVP:
  channel chat over BLE/TCP) plus its §27 "optional phase-two features" list.
- `CLAUDE_MESHCHAT_NETWORK_MONITOR_ADDENDUM.md` — the Phase 2 spec for the
  network monitor dashboard (map, node table, rankings, distribution panels,
  telemetry).

This document tracks status against both, plus what's come up since.

## Shipped

**MVP (original build spec, §1–26):**
- BLE, TCP, and USB/Serial transport
- Channel messaging and direct messages
- Node list with last heard, SNR/RSSI, hardware model
- Persistent SQLite history (messages, nodes, packets, positions, telemetry)
- Live mesh map (Leaflet, clustered, light/dark basemap)
- Telemetry view (battery, voltage, channel/air utilization, environment)
- Optional USB serial transport
- Export conversation / packet log to CSV

**Network monitor (addendum, Phase 2):**
- Live KPI dashboard, packet-activity chart, packet log
- Rankings (last heard, most packets, nearby/farthest, signal, messages)
- Distribution panels (packet type, channel, role, hardware)
- Node inspector (identity, signal/hops, telemetry)

**Beyond both original specs:**
- GPL-3.0 relicense (required once linking the GPL-3.0-only `meshtastic`
  package — see [ARCHITECTURE.md](ARCHITECTURE.md))
- Optional RTL-SDR spectrum waterfall, with Meshtastic + MeshCore-aware
  frequency-band markers (`analytics/lora_bands.py`)
- v0.1.0-alpha public release

## In progress

- PR #6 — map pin labels, zoom-based clustering, click-to-inspect
  cross-page sync (branch `feature/map-pin-labels`)

## Near-term

- **v0.1.1-alpha release.** The `VERSION` `ImportError` crash fix (Help →
  About, diagnostic copy, session export) has been on `main` since PR #2,
  but the binary attached to the published `v0.1.0-alpha` GitHub release
  still crashes on those paths. Needs a new tagged release with the fix
  actually in the downloadable build.

## Remaining phase-two items (from the original build spec, §27)

Not yet built, in no particular priority order:
- Desktop notifications
- Reconnect policy controls
- System tray operation
- Multiple profiles
- Multiple simultaneously connected local radios
- Message search
- Channel configuration (from the app)
- QR/channel URL import (needs explicit security warnings per the spec —
  channel URLs embed the PSK)
- Signed Windows installer
- Automatic update checking

## Deferred (scoped, not started)

- **Full MeshCore protocol support.** A `MeshCoreController` parallel to
  `MeshtasticController`, using the `meshcore` PyPI package's async API.
  Bigger than the other items here — needs an explicit decision on how to
  bridge that package's asyncio model into this app's Qt/PubSub-signal
  pattern before implementation starts. The Spectrum page's frequency
  markers are already MeshCore-aware (`analytics/lora_bands.py`); the radio
  protocol itself is not yet implemented.
- **LoRa decoder proof-of-concept.** Investigated, not built. Recommended
  first step: an offline IQ-capture-then-decode PoC using `lora-phy` to
  validate feasibility before any UI is built around it — the Spectrum page
  currently shows *that* RF activity exists, not decoded packet content.
- **Editing LoRa region/preset from the app.** Needs real guard rails —
  region selection has RF-regulatory meaning, not just a UI preference.
- **Compact image transmission over the mesh** — see
  [docs/mcoreimg-integration.md](docs/mcoreimg-integration.md).

## Explicitly out of scope (per the original build spec, §1/§26)

- Firmware flashing
- Reimplementing Meshtastic protobuf framing (use the official `meshtastic`
  package unless an upstream defect makes that unavoidable)
- Storing/logging channel PSKs, Bluetooth PINs, or Wi-Fi credentials
