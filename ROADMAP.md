# Roadmap

This project started from two planning documents that are **not** part of
this repository (they're local planning material, not something every
clone/checkout of this repo has access to):

- `CLAUDE_MESHCHAT_WINDOWS_BUILD_SPEC.md` — the original build spec (MVP:
  channel chat over BLE/TCP) plus its §27 "optional phase-two features" list.
- `CLAUDE_MESHCHAT_NETWORK_MONITOR_ADDENDUM.md` — the Phase 2 spec for the
  network monitor dashboard (map, node table, rankings, distribution panels,
  telemetry).

This document tracks status against both, plus what's come up since. If
you don't have those source documents, everything you need is summarized
below — they're cited here for provenance, not as a dependency.

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
- Map pin labels, zoom-based clustering, click-to-inspect cross-page sync

**Beyond both original specs:**
- GPL-3.0 relicense (required once linking the GPL-3.0-only `meshtastic`
  package — see [ARCHITECTURE.md](ARCHITECTURE.md))
- Optional RTL-SDR spectrum waterfall, with Meshtastic + MeshCore-aware
  frequency-band markers (`analytics/lora_bands.py`)
- v0.1.0-alpha public release
- v0.1.1-alpha release — the `VERSION` `ImportError` crash fix (Help →
  About, diagnostic copy, session export) had been on `main` since PR #2
  but never made it into a downloadable build until this one; also the
  first release with a proper Windows installer (`MeshChat-Setup-*.exe`,
  Inno Setup) instead of a raw `dist/` folder

## In progress

Nothing currently in flight.

## Near-term

Nothing currently planned.

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
- Windows installer is unsigned (`packaging/installer.iss`, Inno Setup) —
  Windows SmartScreen shows an "unrecognized publisher" warning until it
  builds up download reputation. Actual code signing needs a paid
  certificate; not started.
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
