# Architecture

MeshChat is a PySide6 (Qt 6) desktop app that connects to **one** Meshtastic
radio at a time over USB/Serial, Bluetooth, or TCP. The radio does the actual
LoRa transmit/receive; MeshChat's job is to talk to it, keep a local record of
everything it reports, and present that as a chat client plus a network
monitor.

This document describes how the pieces fit together and the conventions that
keep them safe to extend. It is a snapshot of intent, not a changelog — see
[ROADMAP.md](ROADMAP.md) for what's planned/in-flight, and `git log` for what
actually shipped when.

## Process and threading model

- The whole UI runs on Qt's main/GUI thread. **No widget is ever touched from
  another thread** — this is a hard rule, not a preference.
- `controllers/meshtastic_controller.py` (`MeshtasticController`) owns the
  connection to the `meshtastic` Python library, which is blocking and
  callback-driven (PyPubSub). It runs that on a dedicated `QThread` and
  marshals every inbound event back to the GUI thread as a Qt signal —
  `connection_state_changed`, `message_received`, `raw_packet`, etc. Nothing
  downstream of the controller needs to know PubSub exists.
- Background write work (SQLite inserts, retention pruning) is similarly kept
  off the GUI thread by `services/monitor_store.py`, queued onto its own
  writer rather than blocking the event loop.

## Packet flow (the spine of the app)

```
Controller (QThread, PubSub → Qt signals)
        │  raw_packet(dict)
        ▼
PacketIngestor.ingest_raw()          services/packet_ingestor.py
        │  dedupe → normalize → NetworkPacket
        ├─ _update_node()             always runs first — every packet
        │       │                     updates/creates a NodeSnapshot and
        │       │                     emits node_updated
        │       ▼
        ├─ _handle_position() ──────► position_updated(PositionSample)
        ├─ _handle_telemetry() ─────► telemetry_updated(TelemetrySample)
        ├─ MonitorStore.save_packet/save_position/upsert_node (SQLite)
        └─ packet_ingested / stats_updated
```

`_update_node()` running unconditionally before any type-specific handler is
a load-bearing invariant: it's what guarantees `node_updated` reaches
`MonitorPage`/`NodesPage` *before* a `position_updated`/`telemetry_updated`
for the same node, so a node snapshot always exists by the time UI code
reacts to it. Two startup seeding paths mirror this ordering:
`seed_from_nodedb()` (from the radio's own NodeDB on connect) and
`seed_from_store()` (from `MonitorStore`'s persisted history at launch, so
the app never starts blank).

All Qt signal emission in this chain happens synchronously on the GUI
thread — same-thread connections are direct calls, not queued — so a single
`ingest_raw()` call fully propagates before returning.

## UI shell

`ui/main_window.py` (`MainWindow`) owns a nav rail plus a `QStackedWidget`
with four pages: Chat, Monitor, Nodes, Spectrum. It's the wiring hub —
controller/ingestor signals fan out to whichever pages care, and cross-page
navigation (e.g. "show this node on the map", a map pin click populating the
Nodes table selection) is wired here rather than pages reaching into each
other directly.

- **Chat** (`ui/chat_view.py` + `ui/widgets/channel_list.py`) — per-channel
  and per-node DM threads. History is persisted locally
  (`MonitorStore`/SQLite) since Meshtastic radios don't retain message
  history themselves.
- **Monitor** (`ui/monitor/monitor_page.py`) — the dashboard: KPI cards
  (`dashboard_header.py`), the map (`ui/map/`), a live packet-activity chart
  (`packet_activity_chart.py`, pyqtgraph), a packet log (`packet_log_view.py`),
  ranking panels (`rankings_panel.py` — Last Heard / Most Packets / Nearby /
  Signal / Messages), distribution panels (`distribution_panel.py` — packet
  type, channel, role, hardware breakdowns), and a `NodeInspector` detail
  panel. `MonitorPage` keeps its own `self._nodes: dict[int, NodeSnapshot]`
  fed by `node_updated`, used to resolve a clicked node (map pin or ranking
  row) to full detail.
- **Nodes** (`ui/nodes/nodes_page.py`) — the full sortable/searchable node
  table. `NodeTableModel` (`QAbstractTableModel`) + a `QSortFilterProxyModel`
  for search, `NodeInspector` for detail on the selected row. Selection is
  intentionally sticky: `QAbstractItemModel.beginResetModel()`/
  `endResetModel()` (called on every live node refresh) clears Qt's current
  index, so `NodesPage` remembers the selected `node_num` and reapplies it
  after every `update_nodes()`/search-filter change — otherwise a
  just-made selection silently disappears within a few seconds on a live
  mesh.
- **Spectrum** (`ui/spectrum/spectrum_page.py`, optional) — an RTL-SDR
  waterfall of raw RF energy in the LoRa ISM bands. Shows *that* activity is
  happening, not decoded packets. Fully optional: `services/sdr_source.py`
  reports unavailable and the page degrades gracefully if `pyrtlsdr`/the
  native `librtlsdr` driver isn't present.

## The map

`ui/map/` embeds Leaflet.js in a `QWebEngineView` (`map_widget.py`), bridged
to Python via `QWebChannel` (`map_bridge.py` ↔ `web/map.js`). Python calls
into JS through buffered `runJavaScript()` calls (buffered until the JS side
signals `mapReady()`, since the page loads asynchronously); JS calls back
into Python via `Slot`-decorated methods on `MapBridge`, forwarded as Qt
signals (`node_clicked`, `theme_changed`).

Markers use `Leaflet.markercluster`. Two things worth knowing before
touching `web/map.js`:

- `maxClusterRadius` is read **once per zoom level**, inside the library's
  `_generateInitialClusters()`, which only runs from `onAdd()` (i.e. once,
  when the group is first added to the map) and `clearLayers()`. It cannot
  safely depend on live, mutable state (e.g. current node count) — a value
  read at `onAdd()` time will never update afterward. Use a pure
  zoom-only function.
- `spiderfyOnMaxZoom` must stay enabled. Nodes sharing the exact same
  lat/lon (multiple radios at one fixed site is a real case) have 0px
  distance regardless of zoom, so zoom-based decluster alone can never
  separate them — spiderfy is the only way those pins stay individually
  clickable at max zoom.

Any string from node data that reaches the page (badge text, tooltip,
popup) goes through `escapeHtml()` in `map.js` first — it's rendered via
`innerHTML`/Leaflet's `html:`/tooltip content, not text nodes.

## Data models and persistence

`models/` holds plain dataclasses — `NodeSnapshot`, `PositionSample`,
`TelemetrySample`, `NetworkPacket`, `NetworkSession` — with no Qt or I/O
dependencies, so they're cheap to construct, copy, and test. `NodeSnapshot`
in particular is deliberately lightweight to construct (only `node_num` is
required) since it's synthesized in several places (live ingest, both
seeding paths) with only partial data available at each site.

`services/monitor_store.py` is the SQLite persistence layer (schema in
`database/schema.py`): nodes, positions, telemetry, packets, and chat
messages. It's what makes the app "remember" across restarts — the map,
node table, and chat history are all populated from here before any radio
is even connected this session.

## Analytics

`analytics/` is pure, Qt-free computation consumed by the UI layer:
rolling packet rates, activity windows, distance calculations, hop metrics,
signal metrics, packet classification, and LoRa/MeshCore band awareness
(`lora_bands.py`, used by the Spectrum page's frequency markers). Keeping
this Qt-free is deliberate — it's the easiest part of the codebase to unit
test in isolation, and it should stay that way.

## Testing conventions

- `tests/conftest.py` constructs the process-wide `QApplication` before any
  test module loads. `QApplication`/`QCoreApplication` share one
  process-wide singleton; whichever gets constructed *first* wins it, and a
  lightweight `QCoreApplication` winning means any later test that builds a
  real `QWidget` crashes the interpreter natively (no Python traceback).
  Constructing the strictly-stronger `QApplication` in `conftest.py`
  guarantees it always wins.
- Widgets that pool/reuse rows across refreshes (e.g. `RankingsPanel`) get
  regression tests that reproduce the *actual* race with a real `QTimer`
  and `QTest.mouseClick()` — not just synchronous calls to the update
  method — because the bug class this guards against (a click landing on a
  widget mid-teardown) only reproduces through the real Qt event system.
- Use `shiboken6.isValid(obj)` before touching a `QObject` from a code path
  that might run after that object's C++ side was destroyed (e.g. a click
  handler on a widget that a refresh cycle might have just torn down).

## Packaging

PyInstaller (`scripts/build.ps1`) builds a windowed (non-console) exe. Two
PySide6/QtWebEngine artifacts — `QtWebEngineProcess.exe` and `icudtl.dat` —
need a manual post-build copy into `dist/MeshChat/` since PyInstaller's
default hooks don't always pick them up. Because the release build is
windowed, `app.py` installs a global excepthook so unhandled exceptions
produce a visible crash dialog instead of silently exiting — this is how
several real bugs in this project were first surfaced by user reports.

## Licensing constraint

MeshChat links the official `meshtastic` Python package, which is
GPL-3.0-only. That makes GPL-3.0-only the only license this project can
legally ship under — this is a hard constraint on any future dependency
choice too, not just the project's own license.
