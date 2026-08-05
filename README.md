# MeshChat for Windows

A native Windows desktop client for [Meshtastic](https://meshtastic.org) mesh
radio networks, with a dense network-monitoring dashboard.

MeshChat connects to one nearby Meshtastic radio over Bluetooth, USB, or TCP.
The radio — not the PC — sends and receives the actual LoRa packets.

## Features

**Chat**
- Channel messaging across the radio's configured Meshtastic channels
- Direct messages to any reachable node on the mesh
- Local message history that persists across restarts

**Network Monitor**
- Live KPI dashboard (node counts, packet rates, signal, hop analytics)
- Sortable rankings: last heard, most packets, nearby/farthest, signal, messages
- Packet log with per-packet detail
- Packet-type, channel, role, and hardware distribution panels
- Leaflet map with light/dark basemaps, populated from the radio's node database
  and from locally persisted position history

**Nodes**
- Full sortable node table with search
- Node inspector: identity, activity, signal, hops, telemetry
- Right-click actions: message, show on map, request position/telemetry,
  traceroute, favorite, remove from the radio's node database

**Spectrum** *(optional, requires RTL-SDR hardware)*
- Waterfall display of raw RF energy in the LoRa ISM bands
- Shows *that* activity is happening and where in the band — it does not decode
  LoRa packets

## Connecting

| Transport | Notes |
|---|---|
| USB / Serial | Most reliable. Pick the COM port and connect. |
| Bluetooth | The radio must be paired in Windows Settings first — Meshtastic radios require OS-level pairing before any app can talk to them. |
| Wi-Fi / TCP | Enter the radio's hostname or IP. |

## Installation

Requires Python 3.12+ (64-bit) on Windows.

```bash
.\scripts\bootstrap.ps1
```

## Running

```bash
.\scripts\run-dev.ps1
```

Add `-Debug` for verbose logging. Logs are written to
`%LOCALAPPDATA%\MeshChat\Logs\`.

## Building a standalone executable

```bash
.\scripts\build.ps1
```

Produces `dist\MeshChat\MeshChat.exe`.

## Tests

```bash
.\scripts\test.ps1
```

## Licensing

MeshChat interfaces with the official Meshtastic Python package, which is
licensed **GPL-3.0-only**. See [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
Anyone distributing builds of this application should confirm their obligations
under that license.
