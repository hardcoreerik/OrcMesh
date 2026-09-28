# RF Observatory — Phase 0 audit

**Date:** 2026-09-27
**Branch:** `feat/rf-observatory` (from `origin/main` at `b97143e`)
**Scope:** what OrcMesh actually is today, measured against this machine, before any
architecture change.

Everything below was read out of the tree or measured on hardware. Where a claim could
not be verified it is marked **unverified**. Where a number was measured it says how.

---

## 1. Current architecture — reality report

**Runtime shape**

- Python 3.14, PySide6 (Qt6), pyqtgraph + `pyqtgraph.opengl`, numpy. SQLite via stdlib.
- Maps are a `QWebEngineView` + Leaflet, with OrcMaps driven as a **separate process**.
- `QSG_RHI_BACKEND=opengl` is set before `QApplication` (`app.py`) — this is what allows
  the WebEngine map and the GL 3D waterfall to coexist in one process. Do not remove it.

**Two independent RF pages exist today**

| Page | File | Dongle control | Owns |
|---|---|---|---|
| Spectrum | `ui/spectrum/spectrum_page.py` | **hardcoded device 0** (`:376`) | its own `SdrController` (`:367-376`) |
| SIGINT | `ui/sigint/sigint_page.py` | Dongle combo (`:159-163`) | its own `SdrController` (`:571`) + `ScanController` (`:697`) |

Both stack into the single `QStackedWidget` in `ui/main_window.py:290-296`. The nav rail has
**six** pages (Chat, Monitor, Nodes, Spectrum, SIGINT, Device), not four.

**The RF path is deliberately CLI-subprocess based**

OrcMesh never links an SDR library. It drives `rtl_sdr` / `rtl_power` / `rtl_test` as child
processes (`services/rtl_tools.py:5-11`). **`pyrtlsdr` is never imported anywhere in `src/`**
despite two documents claiming it is required. This path is hardware-verified on a Blog V4
and should be preserved.

**Threading convention** (holds throughout, and the new work must follow it)

- Controllers: `QThread` + `moveToThread`, one thread per controller.
- Plain daemon `threading.Thread` for blocking I/O (DB writer, stderr drain, tile HTTP, SDR reader).
- `PacketIngestor` deliberately stays on the **GUI thread** (`packet_ingestor.py:158-171`).
- Hand-back is always a queued Qt signal. `Q_ARG(object, x)` is unusable in PySide6 — use an
  `object`-typed signal plus `Qt.ConnectionType.QueuedConnection`.

---

## 2. What `b97143e` actually accomplishes

Verified against the tree, not the commit message.

| Claimed in the brief | Reality |
|---|---|
| per-dongle RTL-SDR enumeration | **True** — `SdrDevice`, `list_devices()`, `parse_device_list()` (`rtl_tools.py:179,261,213`) |
| per-device SDR leasing | **True** — `_sdr_owners: dict[int, tuple[str, str]]` (`rtl_tools.py:395-425`) |
| `rtl_sdr` / `rtl_power` device selection | **True** — `-d <index>` in both command builders (`sdr_source.py`, `rtl_scan.py:335`) |
| simultaneous ownership of different RTL devices | **Partly** — the lease is per index, so Spectrum(d0)+SIGINT(d1) can coexist, but SIGINT excludes its own capture from its own scan even on a second dongle (`sigint_page.py:588-590, 717-719`) |
| 2D waterfall | **True** — `ui/spectrum/waterfall_view.py` |
| 3D waterfall | **True** — `ui/sigint/waterfall3d_view.py`, docked, lazily activated |
| band survey | **True** — `rtl_scan.py` (`parse_power_row`, `merge_rows`, `BandAccumulator`, `ScanAssembler`, `ScanWorker`) |
| persistence/occupancy analysis | **True** — `analytics/slot_occupancy.py:rank_slots`, fed per sweep |
| capture presets | **True** — `analytics/sdr_presets.py`, 7 presets built from `lora_bands` |
| Meshtastic/MeshCore/Reticulum/bare-LoRa awareness | **True** — `analytics/lora_bands.py` (`MESHTASTIC_REGIONS`, `MESHCORE_PLANS`, `RETICULUM_PLANS`) |
| packet intelligence | **True** — `analytics/packet_intel.py:analyse_packets`, protocol-side only |
| LoRa airtime analysis | **True** — `analytics/lora_airtime.py` (Semtech AN1200.13); used only *by* `packet_intel` |
| IQ recording | **Write path only** — see §6 |
| amplitude controls | **True** — `ui/widgets/levels_control.py` |
| setup support | **True**, but `ui/setup/setup_wizard.py` has **no SDR/dongle UI at all**; `services/provisioning/*` is OrcMaps pack sizing, not RF |

---

## 3. Documentation that is stale

| Location | Problem |
|---|---|
| `README.md:118-120`, `ARCHITECTURE.md:124-126` | Claim Spectrum "requires an RTL-SDR dongle plus `pyrtlsdr`". Nothing imports `pyrtlsdr`; the CLI tools are used. |
| `ARCHITECTURE.md:94` | "a `QStackedWidget` with four pages" — there are six. |
| `ARCHITECTURE.md:122-126` | Describes Spectrum as the only RF view; omits SIGINT entirely. |
| `README.md` features | No mention of SIGINT, band survey, presets or packet intel. |
| `THIRD_PARTY_LICENSES.md:18`, `pyproject.toml:29`, `requirements.txt:18` | Still list `pyrtlsdr`. |
| No document anywhere | Describes the SIGINT tab, the 3D waterfall, multi-dongle support or IQ capture. |

No `STATUS.md` exists in OrcMesh (only in OrcMaps). There are **zero** project-authored
TODO/FIXME markers in `src/`.

---

## 4. Single-radio assumptions (the change list for Stage C)

- One `MeshtasticWorker` + one `QThread` + one `_interface`: `controllers/meshtastic_controller.py:1219-1224, :360`.
- Every `connect_*` begins with `_close_interface()` (`:613, :706, :773`) — **radios are mutually exclusive by construction**.
- One service graph in `MainWindow.__init__` (`ui/main_window.py:118-125`); one `_device_page`, one `_device_snapshot`.
- `PacketIngestor` holds a single session, a single dedup cache, a single node map, a single ring (`packet_ingestor.py:181,186,190,197`).
- One saved connection profile, persisted as `connection.*` keys in `app_settings` (`connection_supervisor.py:167-173`); `MainWindow` reads the private `_profile` (`:1000`).
- **No table has a device/source column** (`database/schema.py:66,93,109`).
- Counter-example already done right: SDR leases *are* per device index.

---

## 5. Multi-RTL capability today — and the bugs the audit found

**Hardware on this machine (measured):** exactly **two** dongles — `0: Blog V4` (R828D,
"RTL-SDR Blog V4 Detected", 29 gain steps 0.0–49.6) and `1: Blog V4L` (R820T). Both report
`SN: 00000001`, so index is the only reliable selector.

The brief's role table assumes RTL #0/#1/#2 (three). Plan for two; let the model scale.

**Two real defects found, both worth fixing before any abstraction is added:**

1. **Lease leak — FIXED on this branch.** If `subprocess.Popen` raises `OSError` after a
   successful acquire, neither worker released the device (`sdr_source.py:237→244-252`,
   `rtl_scan.py:360→368-376`), so the dongle stayed marked busy until the app restarted.
   Both now release in the failure path. Confirmed by disabling `release_sdr` and starting a
   capture whose spawn raises: dongle 3's owner stayed `"the spectrum view"` before the fix,
   and is empty after it. Regression tests in `test_sdr_source.py` and `test_rtl_scan.py`.
2. **Orphaned children.** There is no Windows job object, no `atexit`, no process group. If
   OrcMesh is killed, `rtl_sdr`/`rtl_power` survive and keep the dongle. The only teardown is
   `closeEvent` → `sigint_page.shutdown()` / `spectrum_page.shutdown()`.

**What actually blocks a second simultaneous stream:** the lease (same index only), the
Spectrum page hardcoding device 0, and SIGINT's capture/scan mutual exclusion. Nothing else —
each `SdrController` owns its thread and subprocess, and the FFT is stateless.

---

## 6. SDR resource-management model

- Process-global `_sdr_owners: dict[int, tuple[str, str]]` + `_sdr_lock` (`rtl_tools.py:395-396`).
- `acquire_sdr(owner, label, device)` returns `(bool, str)`; re-acquire by the same owner
  succeeds; a different owner is refused. **No timeout, no TTL, no preemption, no queue.**
- `release_sdr` silently no-ops on owner mismatch (`:417`).
- Owners are per-instance strings (`f"sdr-{id(self)}"`), so a dead worker's stale lease is
  never reclaimed.
- Release happens only in `_close()` (`sdr_source.py:410`, `rtl_scan.py:433`).

**Backpressure:** `row_ready` is an **unbounded** queued Qt signal (~60 rows/s × 1024 float32)
with **no coalescing and no drop counter**. Everything else is fixed-size (`_MAX_PENDING_ROWS=64`,
widget histories of 300 rows, `BandAccumulator` fixed to sweep geometry). The brief's requirement
to *display* degradation is currently impossible — nothing counts drops.

---

## 7. Packet deduplication behaviour

- TTL `_DEDUP_TTL_S = 120` (`packet_ingestor.py:27`).
- Key: `f"{sender}:{pid}:{portnum}:{channel}"` when sender and pid are known; otherwise a
  sha256 of the payload **plus a 5-second `monotonic()` bucket** (`:124-148`).
- A duplicate is **silently dropped** (`:212-213`) — not counted, not stored, not emitted.
- The cache is **not keyed by radio** (`:186`, cap 5000, evict oldest 2500).
  **A second radio hearing the same packet inside 120 s has its observation discarded.**
- Per-packet RSSI/SNR *are* stored (`packets.rx_snr`, `packets.rx_rssi`, `schema.py:39-40`,
  filled at `packet_ingestor.py:345-346`), but node-level `last_snr`/`last_rssi` are
  **overwritten** on every observation (`:383-384`).

This is the single most important thing to change for the observatory: today duplicate
receptions are treated as noise to be removed rather than as evidence. No `ReceptionObservation`
equivalent exists.

---

## 8. Thread / process model

- Controllers: `QThread` + `moveToThread` (`meshtastic_controller.py:1222`, `firmware_controller.py:98`,
  `orcmaps_controller.py:144`, `rtl_scan.py:486`, `sdr_source.py:465`).
- Blocking I/O daemon threads: DB writer (`monitor_store.py:104`), stderr drain (`rtl_tools.py:357`),
  tile HTTP (`orcmaps.py:951`), SDR reader (`sdr_source.py:257`).
- One capture path: button → `SdrController.start` → `invokeMethod(QueuedConnection, Q_ARG(float×3,int))`
  → worker thread `Popen` → blocking `stdout.read(65536)` → `iq_to_power_row` (1024-bin FFT) →
  `row_ready` → GUI slot → widget.
- `rtl_sdr` costs a **fixed ~3.25 s to start** regardless of sample count (`rtl_scan.py:8-12`).
- Shutdown order is explicit in `main_window.py:1334-1367`: pages, then controller, then store.

---

## 9. Database / event schema

- File: `platformdirs.user_data_dir("MeshChat") / "monitor.db"` (`monitor_store.py:73-77`) —
  note the application name is still **"MeshChat"**.
- Tables (`database/schema.py:14`): `sessions`, `packets`, `nodes`, `positions`, `telemetry`,
  `messages`, `app_settings`, plus indexes and `_POST_MIGRATE_INDEXES`.
- Versioning is real and good: `PRAGMA user_version` + a `_MIGRATIONS` list with a
  pre-migration **file backup to `backups/`** (`schema.py:217-222, 251-266, 274`).
- **No device/source/observer column anywhere.** `nodes` PK is `node_num` alone; `positions`
  and `telemetry` are keyed by `node_num` alone; `packets` is scoped by `session_id` only.

There is no RF-event table of any kind — no burst, no occupancy, no capture, no receiver health.

---

## 10. PlutoSDR on Windows — options, and what this machine actually has

**Attached, identified and then detached by the streaming test below (2026-09-27).** It is a
**clone**, not an ADI reference Pluto: `hw_model: FISH Ball PlutoSDR Rev.A (Z7020-AD9361)`
(sold as "7020-SDR" / PlutoSky / Fish-Wan), `hw_serial: b0d85d89da56de55b2ee997b00499360`,
`fw_version: 95aad-dirty` — a locally-built firmware from the
[fishball7020-fpga-devkit](https://github.com/matsvandamme/fishball7020-fpga-devkit)
reconstruction, not an ADI release. **Never label it "ADALM-Pluto" in the UI**: over USB its
descriptors *do* say `Analog Devices Inc. PlutoSDR (ADALM-PLUTO)`, while over IP it reports the
FISH Ball string. It is reachable by **two URIs at once** with the same serial, so identity must
come from `hw_serial`, never from the URI, or one board will appear as two receivers.

libiio 0.26 is installed (`iio_info`, `iio_attr`, `iio_readdev`, `iio_writedev`, `iio_reg` in
`C:\Windows\System32`; backends `xml ip usb serial`). Windows drivers were already healthy: the
board enumerated with **no unknown or errored devices**, as RNDIS, IIO, mass storage and a serial
console.

Options assessed:

| Path | Windows reality | Verdict |
|---|---|---|
| **libiio direct** | Official Windows x64 builds (v1.0.0); in-tree Windows USB/network backends; **LGPL-2.1** | **Preferred.** Device opened by URI (`ip:192.168.2.1` or `usb:`). Helper process keeps native crashes out of the GUI. |
| SoapySDR + SoapyPlutoSDR | Only via the dated **Pothos SDR** bundle | Optional later; heavier. Not installed here. |
| GNU Radio | No longer WSL-only, but native Windows is a **conda/radioconda** second-class path; `gr-lora_sdr` is Linux-first | **Do not make it the runtime.** |
| SDRangel | Genuinely first-class on Windows | Useful as an external reference/verification tool, not a dependency. |

**The clone is 2 TX / 2 RX, not 1+1.** `cf-ad9361-lpc` exposes **four** input channels
(`voltage0..3`, `le:S12/16`) = two complex RX streams; the TX DMA likewise has four. The
firmware documents "two receivers that both survive decimation" and warns that **stock ADI
wiring filters only channel 0, leaving channel 1 aliased by 70 dB** unless an optional patch is
applied — so RX2 must not be trusted for RF truth until that is established on this board.

**Its real streaming limits** (the firmware project's own measurements, on their bench — **not
yet reproduced here**):

| Path | 1 RX channel | 2 RX channels |
|---|---|---|
| Capture run on the board, no link involved | 49.8 MS/s | 46.2 MS/s each |
| Gigabit Ethernet | ~11.3 MS/s on one example link | — |
| **USB gadget** | **~1.7 MS/s** | — |

Two consequences that change the plan:

- **This box currently sees the board only over the USB gadget**, so its usable instantaneous
  bandwidth here is roughly **±0.85 MHz** — nowhere near the 20 MHz the brief assumes, and **not
  even wide enough for a 2 MHz LoRa window**. Wideband scouting requires the board on
  **Ethernet**.
- **The libiio buffer size dominates throughput.** A small buffer is measured to cost *roughly
  two thirds* of the rate; `-b 1048576` or larger is recommended, and past a few Msamples it
  stops helping. Their own capture command is
  `iio_readdev -u ip:fishball.local -b 1048576 -s 33554432 cf-ad9361-lpc voltage0 voltage1`.

**Measured on this machine (2026-09-27, after reseating; 2 s reads, `-b 1048576`, one complex RX
stream, `cf-ad9361-lpc voltage0 voltage1`):**

| Transport | Rate asked | Rate taken | Wall for 2 s of samples | Delivered (MB/s) | Verdict |
|---|---|---|---|---|---|
| `ip:` (RNDIS) | 0.5 MSPS | **30.72 MSPS** (rejected) | 10.0 s | 24.5 | link-limited |
| `ip:` | 2.083 MSPS | **30.72 MSPS** (rejected) | 9.5 s | 25.9 | link-limited |
| `ip:` | 3 MSPS | 3 MSPS | 2.35 s | 10.2 | ~real time |
| `ip:` | 5 MSPS | 5 MSPS | 2.36 s | 16.9 | ~real time |
| `ip:` | 10 MSPS | 10 MSPS | 3.42 s | 23.4 | link-limited (1.7×) |
| `usb:` (libiio USB) | 0.5 / 2.083 MSPS | 30.72 MSPS | ~8.1 s | 30.3 | link-limited, **then wedged** |

Three findings, all of which shape the backend:

1. **The `usb:` transport is the unstable one.** The board has now wedged and left the bus twice,
   and both times the failing run was over `usb:` — the first incident above was also `usb:`. The
   RNDIS `ip:` transport survived an identical rate ladder including 10 MSPS. So **prefer `ip:` and
   treat the Windows libiio USB backend as unreliable with this board.**
2. **The host link ceiling is roughly 25–30 MB/s ≈ 6–7.5 MSPS of complex samples**, nowhere near
   the 20 MHz the brief assumes. 5 MSPS runs close to real time; 10 MSPS does not.
3. **Rates below about 3 MSPS are silently rejected and the device keeps its previous setting.**
   Asking for 2,083,333 — the exact minimum `sampling_frequency_available` advertises — left the
   device at 30.72 MSPS. That advertised minimum is therefore misleading for this firmware, and the
   practical floor lies somewhere between 2.083 and 3 MSPS (not yet bisected).

**Parsing note for anyone driving `iio_attr`:** with a specific attribute named it prints *bare
values*, one line per matching channel, **input first then output**. A naive "last value" read
reports the TX side and makes a successful RX rate change look like it never happened — which is
exactly the error the first attempt made, and it is why the failing rate change looked ineffective.

**Sustained soak — PASSED** (2026-09-27, `ip:` transport only, after reseating). 5 MSPS for 60 s
(1.2 GB) completed in **60.45 s wall — 1.007× real time at 19.9 MB/s** — and the device was still
readable afterwards with both contexts present. So the USB-gadget `ip:` path **sustains 5 MSPS
continuously**, which is enough to watch a 2 MHz LoRa window with headroom. The `usb:` transport has
never survived an equivalent load.

**Retune latency (RX_LO, `ip:` transport):** first write 51.4 ms (cold), then **32.9 ms and 32.5 ms**
steady state, each confirmed by read-back. Fast enough for scheduled survey hopping; **not** fast
enough for gap-free retune-scanning of a whole band.

**Ethernet: attached, but not yet usable.** With the cable in, the board's LAN interface answers
**ICMP at 192.168.1.202** and its MAC (`58-d9-d5-1d-f0-47`) appears on the host's LAN adapter — but
**SSH (22), HTTP (80) and the IIO port (30431) are all closed there**, while all three are open on the
USB-gadget address. The board is on the network but its services are not bound to the Ethernet
address, and libiio's scan still lists only `192.168.2.1`. Most likely the interface carries the
firmware's own default `192.168.2.1` (which the services bind to) alongside a DHCP lease, or it needs
a reboot with the cable present. **Until that is resolved the fast path cannot be used, and every
throughput figure above is a USB-gadget figure.**

**Design cautions carried forward:** the ADI Windows USB driver installer is v0.9 (Win8.1-era
signed INFs); libiio has an **0.x → 1.0 ABI break** and this machine has **0.26**; prefer the
`ip:` transport; and **isolate libiio in a helper process**, because this device has already
demonstrated that it can take its host transport down with it.

**Transmitter safety (for the future LAB mode):** the receive port survives **+2.5 dBm** (the
AD9361's absolute maximum) while this board's PA variant reaches about **+19 dBm** — never loop TX
into RX without at least 20 dB of attenuation, and never transmit into an open port. TX mutes
within 250 ms if the feeding program dies, **except for a cyclic transmit, which never starves and
so keeps transmitting indefinitely** unless `tx_cyclic_timeout_ms` is set first.

**Licensing:** the devkit's own scripts, patches and documentation are **GPL-2.0**, which is *not*
combinable with OrcMesh's GPL-3.0. Treat it as interop-only: drive the board through libiio and do
not copy its code in. Vivado/Vitis and AMD IP are proprietary and irrelevant here.

---

## 11. Competing / relevant open-source projects

| Project | What it does | Hardware | PHY decode | Multi-RX | Storage | API | License | Lesson for OrcMesh |
|---|---|---|---|---|---|---|---|---|
| **IronGiu/MeshStation** | Passive Meshtastic observatory with **no Meshtastic hardware** | RTL-SDR | Full decode + decrypt w/ known key | Multi-preset scanning | Node DB (format unverified) | None yet | **GPL-3.0** | Treat an SDR as a first-class data source; "scan all presets" as an acquisition layer |
| **Ixitxachitl/MeshRF** | Cross-platform Meshtastic **transceiver** (software LoRa modem) | HackRF (TX/RX), RTL-SDR, SX1262 | Full demod + mod | Per-channel demod chain | SQLite + IQ `.cf32` + JSON sidecar | YAML automation, MQTT, HTTP | **GPL-3.0-or-later** | Both PHY backends emit identical *frame events*; IQ sidecar JSON |
| **persistentcache/Lora-Wideband-Decoder** | Whole-band concurrent LoRa intercept | bladeRF/USRP/HackRF/Soapy incl. Pluto | Full, multi-SF × multi-BW | One wideband capture, all channels | Capture-to-disk + `lora_unknown_report.jsonl` | Headless + Flask | **PolyForm Noncommercial** | **Reference only — never copy.** Scan the whole sampled band instead of hopping; gate releases on off-air corpora |
| **meshtastic/meshtastic-mcp** | MCP server/agent tooling for Meshtastic | RTL-SDR *optional* | **No PHY decode** — SDR used as an **RF compliance oracle** | One at a time | `packets.jsonl`, SQLite captures | MCP + CLI + web | GPL-3.0-only | **Replay is the exact inverse of the recorder** (serve a capture as a fake radio over TCP); SDR-as-oracle is precisely the brief's closed-loop check |
| **arall/sigint** | Distributed multi-protocol SIGINT + multilateration | HackRF + RTL-SDR nodes | Detection-first; some parsers full | Distributed nodes + channelizer | Per-session SQLite, WAL, restart-safe | CLI + web + CoT | **No LICENSE file — unverified** | Hybrid trigger model (short bursts autonomous, long signals centrally tasked); SQL-first restart safety |
| **KrakenRF KrakenSDR** | Coherent direction finding | KrakenSDR 5-ch coherent | No protocol decode | Coherent array | Not documented | HTTP + REST + TAK | GPL-3.0 | **Split acquisition from processing.** Requires coherent channels — see §12 |

---

## 12. Hardware limits that must not be misrepresented

1. **Independent RTL dongles are not coherent.** They are separate tuners, separate clocks, no
   shared sample clock. There is **no sample-level synchronisation**, so:
   - **No direction finding / DOA.** KrakenSDR-style DF needs coherent channels.
   - What they *are* good for: parallel frequency coverage, antenna/filter/receiver comparison,
     independent confirmation of an event, diversity observation, dedicated roles.
2. **Two dongles here, not three.** The brief's role table cannot be fully populated.
3. **PlutoSDR specs are official ceiling values, not measured:** 325 MHz–3.8 GHz, up to 20 MHz
   instantaneous RF bandwidth, 12-bit ADC/DAC, 1 RX + 1 TX, USB 2.0. **Host streaming rarely
   sustains 20 MHz losslessly over USB 2.0.** Nothing here has been benchmarked — no Pluto is attached.
4. **US 902–928 MHz is 26 MHz wide.** A 20 MHz window **cannot** view the whole allocation at
   once. Any "whole band" claim must be either retune-scheduled or explicitly partial.
5. **`rtl_sdr` has a ~3.25 s fixed start cost** and `rtl_power` retunes internally — sweeping by
   relaunching `rtl_sdr` per step is not viable.
6. **`rtl_power` artefacts are measured, not theoretical:** a **3-bin (243.75 kHz) artefact at
   every row centre, +4.33..+5.89 dB, in 200/200 rows**, and a ~−3.2 dB IF-filter droop at each
   row's edges. In a quiet survey the artefact is **the brightest thing present** (masking the
   ten trios drops the peak from −31.91 to −36.43 dB). Occupancy conclusions inherit this today.
7. **Per-receiver absolute power is not comparable** without per-device calibration: two dongles
   differ in gain table, front end and filtering, and `rtl_power`'s dB scale is not absolute.

---

## 13. Proposed architecture

Follows existing house conventions rather than importing a new one: `controllers/` for
Qt-facades, `services/` for backends, `analytics/` for pure computation, `database/` for schema.

```
services/rf/                     NEW — device layer, no Qt
  base.py            RfDeviceInfo, RfBackend protocol (open/stream/stop/capabilities)
  registry.py        what is attached, capability-oriented; wraps existing rtl_tools discovery
  lease.py           generalised ownership: per-device, with TTL + release-on-error + reclaim
  rtl_backend.py     WRAPS the existing, hardware-verified rtl_sdr/rtl_power subprocess path
  pluto_backend.py   libiio over URI (ip: preferred), helper process
  replay_backend.py  file/IQ source, so every DSP stage is testable offline

controllers/
  rf_orchestrator.py  roles, leases, scheduling, failure/restart; emits Qt signals
  radio_manager.py    multi-radio Meshtastic: one worker/thread/interface per radio

analytics/
  rf_events.py       RfEvent normalised model + MEASURED/DERIVED/INFERRED/UNKNOWN labelling
  correlation.py     associate protocol packets with RF observations
  rf_health.py       fuse Meshtastic telemetry with independent SDR measurement

database/
  schema.py          new tables (migration v3+): rf_events, reception_observations,
                     captures, receiver_health, rf_devices — all with device_id
```

**Two-layer evidence model (Stage C) — the core semantic change:**

- `LogicalPacket` — protocol identity: sender, destination, packet id, portnum, channel,
  payload hash, session, event time. *One per distinct on-air packet.*
- `ReceptionObservation` — `logical_packet_id`, `source_device_id`, source type, timestamp,
  RSSI, SNR, hop/routing, frequency/context, receive path, confidence.

`logical_packet / ReceptionObservation` is 1:N. A packet heard by three receivers becomes
**one packet and three observations** — not three packets, and not one observation with two
thrown away. The existing 120 s dedup cache becomes the *IdentityResolver* that mints logical
packets; it stops discarding evidence and starts attaching it.

Legacy behaviour is preserved: a single radio produces one logical packet with one observation,
and every existing code path that reads packets keeps working.

**Explicitly rejected:** a single mega-page; forcing Pluto into the RTL abstraction; requiring
GNU Radio; measuring DOA from independent dongles; claiming whole-band coverage.

---

## 14. Proposed milestones

Sequenced so that measured defects are fixed before abstractions are built on top of them.

| Stage | Content | Hardware needed |
|---|---|---|
| **A** | This audit + architecture docs | none |
| **B** | RF device registry/capability model, **lease leak + orphaned-child fixes**, drop/frame accounting, replay backend + fixtures | none (unit) |
| **C** | Multi-radio radios + `LogicalPacket`/`ReceptionObservation` + migration v3 | 1–2 radios |
| **D** | Orchestrator + roles on the existing RTL backend; remove capture/scan exclusion | 2 RTLs |
| **E** | Pluto backend + measured capability benchmark | **Pluto — currently absent, stage blocked for verification** |
| **F** | Normalised RF event model + persistence | 1 RTL |
| **G** | Packet↔RF correlation | 1 RTL + 1 radio |
| **H** | Rolling IQ flight recorder + replay integration (SigMF export evaluated) | 1 RTL |
| **I** | RF health engine | 1 RTL + 1 radio |
| **J** | Pluto wideband LoRa detector/channeliser (detection before decode) | Pluto |
| **K** | RF Lab A/B workflow | 2 RTLs (A/B antennas) |
| **L** | Classification — only once a labelled fixture corpus exists | — |

---

## 15. Risks and open questions

1. **Pluto absent** → Stage E can be written but not verified. Does the Pluto physically exist
   and can it be attached? Which USB mode (network RNDIS vs USB)? Is 0.26 acceptable or should
   libiio be upgraded (ABI break)?
2. **Licensing:** Lora-Wideband-Decoder is PolyForm Noncommercial (**reference only**);
   arall/sigint has **no licence file**; the GPL-3.0 projects must never be vendored.
   OrcMaps stays AGPL/separate-process. GTK: the bundled GPL-3.0 must remain intact.
3. **Schema migration risk** — adding device attribution to `nodes`/`positions`/`telemetry`
   touches the existing DB. The `user_version` + backup machinery is good, but the migration
   must be additive and must not require a re-sync to be useful.
4. **GUI-thread pressure** — the observatory multiplies stream count. `row_ready` is already
   unbounded; subviews and drop accounting must land *before* more streams do.
5. **Two dongles only** — role assignment, A/B comparison and "independent confirmation" are all
   constrained. Is more hardware coming?
6. **Radio count** — how many Meshtastic radios are actually available to test with? Multi-radio
   verification needs a second radio.
7. **`rtl_power` artefact** must be repaired (or the occupancy maths must be) before the health
   engine draws conclusions from occupancy, otherwise it will report the artefact as traffic.
8. **Coherence is off the table** — worth stating in the UI, not just the docs, so a future
   reader does not read "two receivers" as "a bearing".
9. **Naming debt:** the app-dir is still `MeshChat` (`monitor_store.py:73-77`) — renaming changes
   where the DB lives and would strand existing data.
10. **CI has no hardware.** Offline replay fixtures (Stage B) are the only way to test the DSP
    chain in CI; that is why the replay backend comes early rather than late.

---

## Hardware validation matrix (what is and is not verified)

| Path | Status | Evidence |
|---|---|---|
| `rtl_sdr` capture, blog V4 | **Verified** | 2.0 MS/s, exact rate 2000000.05 Hz, 8,000,000 bytes for 2 s |
| `rtl_sdr -d 1` on the second dongle | **Verified** | 400,000 samples captured, R820T, exact 2000000.05 Hz |
| Device enumeration, free path | **Verified** | `0 · Blog V4`, `1 · Blog V4L`, SN 00000001 |
| Device enumeration, busy path | **Verified** | "name unreadable (blank EEPROM, or the device is in use)" |
| DC/LO repair in `iq_to_power_row` | **Verified** | centre −0.02 dB vs neighbours, was +10.7 dB |
| `rtl_power` band survey | **Verified** | 200 rows, 33 bins, 81.25 kHz, 0% non-finite |
| `rtl_power` centre-trio artefact | **Measured, not fixed** | +4.33..+5.89 dB, 200/200 rows, 3 bins |
| 3D waterfall rendering | **Verified** | distinct framebuffer colours 1 → 2851 |
| Simultaneous capture on both dongles | **Not yet done** | blocked by SIGINT's capture/scan exclusion |
| Multi-radio Meshtastic | **Not possible today** | radios are mutually exclusive by construction |
| Pluto clone: identity, dual transport, 2RX/2TX, drivers | **Verified** | `iio_info -s` + `iio_attr` dumps; FISH Ball Z7020/AD9361, fw `95aad-dirty`, serial over both URIs |
| Pluto clone: host link ceiling | **Measured** | 5 MSPS sustained 60 s at 1.007× real time (19.9 MB/s); 25–30 MB/s peak; 10 MSPS link-limited |
| Pluto clone: `usb:` transport stability | **Failed twice** | device left the bus on both `usb:` runs; RNDIS `ip:` survived an identical ladder *and* a 60 s soak |
| Pluto clone: retune latency | **Measured** | 51 ms cold, ~33 ms steady state, read-back confirmed |
| Pluto clone: dropped-sample counting | **Not measured** | nothing in the toolchain reports it directly; needs an on-board counter read over SSH |
| Pluto clone: Ethernet path | **Not usable yet** | answers ICMP at 192.168.1.202; ports 22/80/30431 closed there, all open on the gadget address |
