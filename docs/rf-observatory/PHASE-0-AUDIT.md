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

### 4a. What the second radio changed — measured 2026-09-27

A second radio was attached, so the assumptions above are now tested rather than reasoned
about. Bench: **COM24** = `Hardcoreerik`/`hrdc`, node `2859752693`, **HELTEC_V4**, firmware
`2.8.0.47db0e3`; **COM16** = `hardcore_Tbeam`, node `1130080812`,
**LILYGO_TBEAM_S3_CORE**, firmware `2.7.26.54e0d8d`.

1. **Two radios connect at once — verified.** COM24 was opened, then COM16 was opened
   *while COM24 was still connected*, and both reported themselves connected with distinct
   node numbers. Mutual exclusion is therefore **ours, not the platform's**: there is no
   singleton in the library (the only module-level global, `mt_config`, holds CLI plumbing),
   and each `SerialInterface` owns its own port. The exclusivity is exactly the
   `_close_interface()` at `:613`, `:706`, `:773`, and nothing more.
2. **Opening the same port twice is refused by the OS** —
   `SerialException: PermissionError(13, 'Access is denied.')`. Worth knowing, because it
   makes a *port* lock redundant and hides the real hazard in the next point.
3. **One radio has two doors, and the OS protects only one.** The Heltec answers on COM24
   *and* advertises BLE `44:1B:F6:6F:81:BD`; the T-Beam answers on COM16 *and* advertises
   `48:CA:43:5B:AA:2D`. The port lock cannot see the Bluetooth door, so a lease keyed on
   the transport would let a cable session and a Bluetooth session drive one board. The
   lease is keyed on the **radio** for this reason.
4. **The serial number is a MAC, and the Bluetooth address is that MAC plus one.** Both
   boards, same offset — Heltec `44:1B:F6:6F:81:BC` vs `..:BD`; T-Beam `48CA435BAA2C` vs
   `48:CA:43:5B:AA:2D`. Two independent boards makes this a convention rather than a
   coincidence, and it is what lets the two doors be grouped **without opening anything**.
   It remains a hint for avoiding a double-open, never a statement of identity: identity is
   the node number, which only comes from connecting.
5. **`MeshInterface.isConnected` is a `threading.Event`, not a bool**
   (`mesh_interface.py:106`). The library itself only ever calls `.wait()`, `.is_set()` and
   `.clear()`. Any `if iface.isConnected:` would be **always true**, including while
   disconnected, because an Event object is truthy whether or not it is set. Nothing in
   OrcMesh does this today; it is recorded because multi-radio work is exactly where such a
   check gets written.
6. **A second serial connect costs ~13.6 s.** Any connect timeout must exceed that, and a
   candidate list must never connect on a refresh.
7. **A lesson about the first version of this code.** `mac_family` initially accepted only
   colon-separated addresses, so the T-Beam — which reports `48CA435BAA2C`, the same MAC
   with no separators — was listed twice and told it had no hardware address. The unit
   tests passed, because one of them *asserted that wrong answer* from a real fixture. The
   hardware disproved it. A fixture taken from reality only helps if the expectation
   written against it is derived rather than assumed.
8. **One attached device is not a radio and must not be probed.** COM17 is an Espressif
   `303A:1001` USB device that never completes the Meshtastic handshake, and it is
   explicitly off-limits. It is therefore listed as an unidentified candidate and never
   contacted. **Enumeration opens nothing** (`services/radios/registry.py`), which is why a
   refresh is instant and safe; identification is a separate, explicit act that must be
   cancellable and time-limited, because on this machine it would otherwise hang on a
   device the user asked to be left alone.

`services/radios/` now provides this layer: `base.py` (transports, preference, address
normalisation), `registry.py` (enumeration and grouping, no port opened), `identification.py`
(the one thing here that opens a port, and only when asked), `lease.py` (per-radio ownership,
plus a `hold()` context manager so a failed connect cannot leave a radio reading as busy — the
bug that had to be fixed by hand on the SDR side).

**Identification is the deliberate exception to opening nothing, and says so.** Its default
timeout is 45 s, deliberately above the measured 13.6 s connect, because a ten-second timeout
would report healthy radios as broken. It reports failure as *unidentified* rather than broken:
nothing can distinguish "not a radio" from "a radio that is unwell", so the message lists the
possibilities and names the port instead of guessing one. Verified live against both radios —
`COM24: Hardcoreerik (HELTEC_V4, fw 2.8.0.47db0e3)`, `COM16: hardcore_Tbeam
(LILYGO_TBEAM_S3_CORE, fw 2.7.26.54e0d8d)` — with the lease taken from the hardware address,
the node number learned, and every lease released afterwards. COM17 was not opened at any
point, which is why enumeration opens nothing in the first place.

**A radio's identity arrives in stages, and the lease follows it.** It is leased under its
hardware address, because that is all that is known before anything is connected; the node
number the mesh knows it by only appears *after* connecting. `add_identity()` extends the
existing lease to cover the new name, and releasing under **either** name releases the whole
radio — otherwise it would keep reading as busy under a name the user never saw. Verified live
against both radios: leased by address, connected, node numbers learned, four keys held for
two radios, and a second session refused through **both** the address and the node number.

Two things this does not claim. There is a **window** between acquiring and identifying during
which only the hardware address is held — harmless for every transport on this bench, because
the registry keys Bluetooth by its MAC family too so both doors collide on the address alone,
but not sufficient for a transport that can only report a node number. That is exactly what
`add_identity()` is for, and it is why the window is documented rather than glossed. And
identity is still *hinted* from the address before connecting: `mac_family` groups two
addresses without asking the radio, which is a shortcut for avoiding a double-open, never a
statement about which radio this is.

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

**Orphaned children — measured, then fixed.** The lease only records who *intends* to hold a
dongle; on its own it says nothing about which processes still exist. Proved with a control: a
child started through a plain `subprocess.Popen` and **not** tracked **survived its parent's
clean exit** and kept the device open, and the very next launch then enumerated the hardware as
`2 RTL-SDR dongle(s) found: , , SN: ÿ` — names blank, serial garbage. That is the mechanism
behind the "name unreadable (blank EEPROM, or the device is in use)" case recorded earlier,
which until now had no reproduction. The same test with `rtl_tools.spawn` left **no** surviving
process and the next launch enumerated both dongles correctly.

`rtl_tools` now keeps `_children` (pid → label, process), `spawn()` tracks every child and is
the only way the workers start one, `_close()` untracks, and `terminate_children()` is
registered with `atexit` on first track. **Not covered:** a hard kill of OrcMesh itself
(Task Manager, power loss) still orphans the child. A Windows Job Object would close that and
is the documented upgrade — it needs the Win32 API and a per-child assignment at spawn, so it
is recorded as future work rather than claimed.

**Backpressure:** `row_ready` is an **unbounded** queued Qt signal (~60 rows/s × 1024 float32)
with **no coalescing and no drop counter**. Everything else is fixed-size (`_MAX_PENDING_ROWS=64`,
widget histories of 300 rows, `BandAccumulator` fixed to sweep geometry). The brief's requirement
to *display* degradation is currently impossible — nothing counts drops.

**Counted, as of the health increment.** `SdrWorker` now keeps `received_bytes`, `rows` and the
time of the first delivered bytes, and reports a `CaptureHealth` about once a second plus a final
one at the end. It is arithmetic on quantities the loop owns, not a parsed diagnostic, for a
measured reason: `rtl_sdr` was given a real overrun (an unread stdout) and **said nothing** —
zero bytes in six seconds and no overrun line, because an unread pipe stalls the tool rather than
producing a message. There is no wording there worth depending on.

The two figures are deliberately not merged even though on this pipeline they are one measurement
in two units (the loop blocks on the pipe, so being behind *is* being short):

- **lag** — how stale the display is, in seconds. Answers "is the waterfall trailing the air".
- **shortfall** — samples not in hand. Some can still be in the driver's buffers (~3.8 MB, about
  0.8 s at 2.4 MSPS), so a shortfall is not proof of loss, and nothing here pretends to tell loss
  from queueing. That needs a buffer depth the tool's output does not contain.

Verified not to cry wolf: three real captures (5 s at 2.048 MS/s, 5 s at 2.4 MS/s, 20 s at
2.4 MS/s) all reported **lag 0 ms and 0.000% shortfall** with 224/260/1364 rows. The
`effective_rate_hz` figure reads **+0.87% / +0.47% / +0.21%** high across those three, shrinking
with duration — a window offset, not the hardware, since 2,405,133 S/s would be ~2,140 ppm of
crystal error, some forty times what a dongle's crystal does. The split between the read-timestamp
offset and the pipe buffer is **UNKNOWN**; the figure is documented as good to a percent and not
a calibrated frequency measurement.

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

**Ethernet: RESOLVED — and it is the fast path.** The board's `eth0` was on **DHCP** (the documented
default; `ipaddr_eth` blank in the config it serves on its USB drive). DHCP is not wrong, but it meant
the address **churned between boots**, so every address probed — `192.168.1.202` included — was a stale
lease from an earlier boot. The services were never unreachable; we were knocking on an address the
board had already left. The vendor's own documentation warns about exactly this: *"A fresh random MAC
every boot means the router sees a new device each time, hands out a new lease, and a DHCP reservation is
impossible."*

Diagnosing needed **no password at all**: the board exposes a **USB drive** (`PlutoSDR`, 30 MB) holding
`config.txt`, generated from its live environment. That is where the blank `ipaddr_eth` and
`hostname = pluto` were read from — the latter also explains why `ip:fishball.local` never resolved:
the default hostname is `pluto`, and patch `0013` is what renames it to `fishball`.

Fixed by pinning a **static** `ipaddr_eth = 192.168.1.50` through the vendor's own no-terminal route
(edit `config.txt`, set `reset = 1`, eject). It is safe by construction: `ipaddr_eth` only touches
`eth0`, and the docs are explicit that *"the USB interface keeps its own static address no matter what
you did to Ethernet, so a USB cable is always the way back in."* The regenerated `config.txt` now reads
`ipaddr_eth = 192.168.1.50` with `reset = 0` — the request was consumed.

**Ethernet throughput — the payoff** (same BIST-tone drop method, `ip:192.168.1.50`, 1 s captures):

| Rate | Wall for 1 s | Delivered | Phase jumps | Verdict |
|---|---|---|---|---|
| 5 MSPS | 1.22 s | 16.3 MB/s | **0** | continuous |
| 10 MSPS | 1.21 s | 32.9 MB/s | **0** | continuous |
| **15 MSPS** | 1.21 s | **49.5 MB/s** | **0** | **continuous** |
| 20 MSPS | 1.57 s | 50.9 MB/s | 11 | discontinuous |
| 30 MSPS | 2.23 s | 53.8 MB/s | 23 | discontinuous |
| 40 MSPS | 2.82 s | 56.8 MB/s | 29 | discontinuous |

**And it sustains:** 10 MSPS for 60 s (2400 MB) completed in **60.25 s — 1.004× real time at
39.8 MB/s**, with the board healthy afterwards.

So the lossless envelope is **15 MSPS, a 3× gain over the 5 MSPS available on USB** — and notably
**not the 2× this audit previously predicted**. Peak delivery roughly doubles, from ~25 to ~57 MB/s.
15 MSPS is ±7.5 MHz, i.e. **more than half of the 26 MHz US allocation in one look**, and it also
exceeds the vendor's own reference figure of ~40 MB/s for receive alone. It is still nowhere near the
converter's 61.44 MSPS, and copying the whole band over any host link remains impossible — but "the
Pluto can only do 5 MSPS" is now superseded.

**Address churn also settles the identity question.** With the link up, libiio reports the **same board
three times over** — `192.168.1.50`, `192.168.2.1` (the USB gadget) and `usb:` — every one carrying the
same serial `b0d85d89…`. A registry keyed on URI would show three receivers and one radio.

Note also that the board's port 80 serves **ADI's stock static Pluto web page**: it contains the vendor's
own tutorial text (a developer's shell prompt, `rgetz@brain`), so its "kernel 4.9.0 … 2018" line is
boilerplate and **not** this board. The live value is the IIO context attribute `local,kernel: 5.15.0`.

**Until that is resolved the fast path cannot be used, and every throughput figure above is a
USB-gadget figure.**

**Dropped samples and behaviour above 10 MSPS — now measured** (2026-09-27, `ip:` transport). There
is no drop counter in the toolchain, so this uses the phase-continuity method the firmware project
documents: a tone is injected by the AD9361's **BIST** (digitally into RX — no RF, nothing
transmitted), the DC bins are blanked before the tone search, the tone must clear 30 dB above the
floor, and a detector that trips on more than a fifth of blocks reports **inconclusive** rather than
"broken".

| Rate | Wall for 1 s of samples | Delivered | Phase jumps | Verdict |
|---|---|---|---|---|
| 3 MSPS | 1.31 s | 9.2 MB/s | **0** | continuous |
| 5 MSPS | 1.30 s | 15.3 MB/s | **0** | continuous |
| 10 MSPS | 1.82 s | 22.0 MB/s | 4 | **discontinuous — samples dropped** |
| 15 MSPS | 2.57 s | 23.4 MB/s | 7 | discontinuous |
| 20 MSPS | 3.34 s | 24.0 MB/s | 17 | discontinuous |
| 30 MSPS | 4.78 s | 25.1 MB/s | 19 | discontinuous |
| 40 MSPS | 6.37 s | 25.1 MB/s | 27 | discontinuous |
| **61.44 MSPS** (the converter's own spec maximum) | 9.51 s | 25.8 MB/s | 53 | discontinuous |

Read correctly:

- **3 and 5 MSPS are provably clean** — zero phase steps across 3,000 and 5,000 blocks. The ~0.3 s
  beyond one second is fixed context/buffer start-up, not lost data, which the 60 s soak independently
  confirms (60.45 s for 60 s).
- **At 10 MSPS and above the DMA genuinely overflows and samples are discarded** — the first real
  drops appear at 10 MSPS and rise with rate.
- **Delivery plateaus at ~25 MB/s no matter what rate is asked for**, which is the USB-gadget link
  ceiling. Above roughly 5–7 MSPS the radio outruns the link and the surplus is thrown away.
- **`ip:` survives 40 MSPS without wedging** — the board was still fully present afterwards. That is
  the useful contrast with `usb:`, which died at far lower rates.

- **The link, not the radio, is the limit, and the number is identical at every rate.** 61.44 MSPS —
  the converter's spec maximum — was **accepted and run**: 1 s of samples demands 246 MB, the capture
  took 9.51 s, and 246 ÷ 25.8 MB/s = 9.53 s. Delivery sat at ~25–26 MB/s from 10 MSPS all the way to
  61.44. So the radio produced every sample and the link discarded roughly three quarters of them.

So the Pluto's honest working envelope on this host today is **≤5 MSPS with no loss**, with **~25 MB/s**
as the hard delivery ceiling — which is USB 2.0, not the AD9361. The published specifications
(70 MHz–6 GHz, 61.44 MS/s, 2RX/2TX) describe **the radio**, and the radio meets them: the firmware
project measures **49.8 MS/s sustained for one channel** with the capture running *on the board*,
where no link is involved. What no host link can do is carry 61.44 MSPS of one channel — that needs
246 MB/s against gigabit's 125 MB/s — which is why the vendor's own table tops out at 31.25 MS/s over
 gigabit and ~10 MSPS clean in practice. Their documentation says it plainly: *"What you will
actually get is set by the link to your host, not by the board."*

**Design consequence for wideband work.** Streaming raw IQ cannot deliver wideband coverage on this
hardware — not at ~6 MSPS over USB, and not at ~10 MSPS over Ethernet. The only route to genuine
wideband detection is to **decimate or detect in the FPGA fabric and send only events across the
link**, which the board is built for (dual ARM plus Z7020 fabric; the vendor documents an FM
channelizer and notes that "filtering or decimating in the fabric means fewer bytes ever need to
cross"). Their HDL is GPL-2.0, so it stays a separate artifact and is never vendored into GPL-3.0
OrcMesh.

**`pseudorandom_err_check`** exists on the DMA device but is a *test* facility rather than a passive
counter: idle it reports `CH0..CH3 : PN9 : Out of Sync : PN Error`, which only means no PN9 pattern is
being fed. It would need BIST in a pseudorandom mode to mean anything, so the phase-continuity method
above is the one to rely on.

**The socket is USB-C, but the interface behind it is USB 2.0 — a chip limit, not a cable choice.**
USB-C is a connector standard and says nothing about speed; USB 2.0 over USB-C is entirely normal. The
Zynq **XC7Z020**'s processing system has **USB 2.0 OTG only** — USB 3.x arrives with Zynq UltraScale+,
not Zynq-7000 — and the firmware project's README calls the data port "the USB 2.0 socket" (the other
USB-C is `DEBUG`, an FT2232H, also USB 2.0). Measured confirmation: the RNDIS gadget negotiates
**426 Mbps**, i.e. USB 2.0 High Speed less overhead, where SuperSpeed would report gigabits. The
~25 MB/s ceiling is therefore the physical interface, not a configuration mistake.

The board's fast paths are **not USB**: gigabit Ethernet (RTL8211F; the vendor measures ~10 MSPS clean
for one channel, i.e. **2× the 5 MSPS proven lossless here**, and double the span at ±5 MHz), capture to
the on-board microSD (**49.8 MS/s**, but recorded rather than live), or decimating/processing in the
fabric. No host link can carry the converter's 61.44 MSPS: 246 MB/s against gigabit's 125 MB/s.

**What 480 Mbps actually buys.** USB 2.0 High Speed is **480 Mbps of signalling — 60 MB/s — and that
is a line rate, not a data rate.** Token packets, handshakes, bit stuffing and inter-packet gaps put
real bulk throughput nearer **35–40 MB/s**, and RNDIS framing costs a little more on top. Measured
here: **~25 MB/s through the RNDIS gadget** and **~30 MB/s** through libiio's raw USB backend (which is
also the transport that wedged the board twice, so its extra speed is not free). At 4 bytes per complex
sample that is 6.25 and 7.5 MSPS respectively.

The decisive figure: **even USB 2.0 at its full 60 MB/s nominal with zero overhead would carry only
15 MSPS — a quarter of the converter's 61.44 MSPS.** No amount of tuning the USB-C port changes that,
which is precisely what the RJ45 is for.

**This is normal for the Pluto family, not a clone defect.** A genuine ADALM-PLUTO is also USB 2.0 (Zynq
Z7010 processing system) with the same 61.44 MSPS converter, and its advertised rate is not streamable
either. If anything this board's *radio* is the better one — AD9361 (70 MHz–6 GHz, up to 56 MHz analogue
bandwidth) against the reference design's AD9363 (325 MHz–3.8 GHz, 20 MHz). The radio is the upgrade; the
interface is the shared constraint.

**The three ways to actually get a high rate**, best first: (1) **Ethernet**, ~10 MSPS live, once the
service is reachable; (2) **record to the board's own microSD**, 49.8 MS/s — near the radio's maximum,
but recorded rather than live, then copied off; (3) **process in the fabric**, decimating or detecting
on the board and sending only events, which is the only route to genuine wideband coverage.

**Why a high rate is advertised at all, and when it is real.** The rate is a *converter* capability, not a
promise about the link, and it is genuinely usable — just not by streaming raw samples. It buys
**instantaneous coverage** rather than throughput: record on-board (49.8 MS/s measured), or decimate in
the fabric, where configuring 61.44 MSPS and sending only 7.68 MSPS to the host is normal and still sees
the whole ~60 MHz in one look. This is the norm across the SDR world rather than anything clone-specific: a
genuine ADALM-PLUTO advertises 61.44 MSPS over USB 2.0 on the same terms, and a HackRF One advertises
20 MSPS over USB 2.0. Vendors quote the chip because it is the one comparable, host-independent figure; the
sustainable rate depends on the host, the link and what else is on the bus. The fair criticism is that this
is **under-specified rather than false** — hence the two budgets kept separate throughout this audit, and
every number labelled verified, measured or unmeasured.

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
   row's edge. In a quiet survey the artefact is **the brightest thing present** (masking the
   ten trios drops the peak from −31.91 to −36.43 dB).
   **The centre trio is now corrected** (`rtl_scan.repair_row_artefact`), per row during the
   merge — after stitching there is no way to tell a row's centre from anywhere else in the
   band. Two thresholds, and both would cause damage if moved for tidiness: a **ceiling at
   8 dB**, because a survey cannot separate a carrier from the artefact by shape but can by
   size (the artefact never exceeds 5.9 dB), and that ceiling must sit *above* the artefact or
   a 3 dB guard would classify it as a carrier and preserve exactly what the fix removes; and a
   **floor at 2 dB**, because the artefact was never once negative, so a centre differing from
   its shoulders only by noise must be left alone — without the floor every quiet row was
   "corrected", replacing honest samples with an invented estimate. That second one was caught
   by a test asserting a quiet row is not touched.
   **The edge droop is still not corrected, deliberately.** A fixed −3.2 dB offset was measured
   at one bandwidth, gain and sample rate, and applying it blindly would distort real signals
   near a row's edge on any other setting. It remains a documented caveat: a 250 kHz slot
   landing on a row boundary reads about 3 dB cooler than one mid-row, so compare slots within
   a row before comparing across one. Occupancy conclusions inherit this today.
7. **Per-receiver absolute power is not comparable** without per-device calibration: two dongles
   differ in gain table, front end and filtering, and `rtl_power`'s dB scale is not absolute.
8. **What a device advertises is not what it will do, in both directions.** The Pluto reports a
   minimum sample rate of 2,083,333 Hz that this firmware **silently refuses** — requests below
   about 3 MSPS are rejected and the device keeps its previous rate, with no error. At the other
   end it accepts its full 61,440,000 Hz while the link discards roughly three quarters of the
   samples. A capability therefore has to carry **two** numbers, the device's claim and the
   measured limit, and the UI has to be able to show both. `MEASURED_LIMITS` in
   `services/rf/base.py` holds the measured half; `probe_libiio` fills the claimed half.
9. **The sample format must be read from the receive DMA, not from the listing.** `iio_info`
   prints the transmit DMA (`cf-ad9361-dds-core-lpc`, `le:S16/16`) *before* the receive one
   (`cf-ad9361-lpc`, `le:S12/16`). Reading the first `format:` in the output — the obvious
   implementation, and the one this probe shipped with for one run — reports a **16-bit
   receiver**. The live probe on this board returned exactly that until it was corrected. The
   truth is 12 bits in a 16-bit container, so a complex sample is 4 bytes and every throughput
   figure here divides by 4.

---

## 13. Proposed architecture

Follows existing house conventions rather than importing a new one: `controllers/` for
Qt-facades, `services/` for backends, `analytics/` for pure computation, `database/` for schema.

```
services/rf/                     NEW — device layer, no Qt
  base.py            RfDeviceInfo, RfBackend protocol (open/stream/stop/capabilities)
  registry.py        what is attached, capability-oriented; wraps existing rtl_tools discovery
  lease.py           generalised ownership: per-device, TTL + release-on-error + reclaim,
                     and the tracked-child registry that reaps orphans at exit
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
| **B** | RF device registry/capability model — **identity + transports landed** (`services/rf/`), capabilities next; **lease-leak fix landed**; orphaned-child reaping; drop/frame accounting; replay backend + fixtures | none (unit) |
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
| `rtl_power` centre-trio artefact | **Corrected** | was `+4.33..+5.89 dB in 200/200 rows, not fixed`; now repaired per row during the merge, with a ceiling above the artefact and a floor below it |
| `rtl_power` row-edge droop | **Measured, deliberately not corrected** | ~−3.2 dB at one bandwidth/gain/rate; applying a fixed offset measured on one setting would distort real signals on any other |
| Pluto transmit: DDS tone | **Does not radiate** | attenuator read back 0 dB, TX LO 905.874998 MHz, DDS scale 0.75 — every register held, **no carrier at 906.875 MHz** |
| Pluto transmit: cyclic stream | **Does not radiate** | `iio_writedev -c` (cyclic, correct syntax, device positional) produced nothing either |
| Pluto transmit: other bands | **Nothing at 906.875 or 433.92 MHz** | widths stayed at the full 2048 kHz capture span, which is what noise looks like; 2.45 GHz untestable with an RTL-SDR |
| Pluto transmit: on-chip diagnostics | **Unavailable** | `loopback` and RX `rf_port_select = TX_MONITOR1` both exist and both **refuse the write** (rc=1) |
| Pluto transmit: TX power detector | **Inconclusive** | reads 0.00 dB in every state, which may only mean the detector is not enabled |
| Pluto transmit: independent confirmation | **Two observers agree** | the operator's own receiver saw nothing, as did the RTL-SDR |
| SIGINT analysis modes | **Implemented** | peak hold, occupancy, envelope, channel slots, event log — over the same row stream the waterfall draws |
| 3D waterfall rendering | **Verified** | distinct framebuffer colours 1 → 2851 |
| Simultaneous capture on both dongles | **Not yet done** | blocked by SIGINT's capture/scan exclusion |
| Multi-radio Meshtastic | **Not possible today** | radios are mutually exclusive by construction |
| Pluto clone: identity, dual transport, 2RX/2TX, drivers | **Verified** | `iio_info -s` + `iio_attr` dumps; FISH Ball Z7020/AD9361, fw `95aad-dirty`, serial over both URIs |
| Pluto clone: USB link ceiling | **Measured** | ~25 MB/s whatever rate is asked; 5 MSPS sustained 60 s at 1.007× real time |
| Pluto clone: Ethernet link ceiling | **Measured** | **15 MSPS lossless, ~57 MB/s peak; 10 MSPS sustained 60 s at 1.004× real time (39.8 MB/s)** |
| Pluto clone: `usb:` transport stability | **Failed twice** | device left the bus on both `usb:` runs; the `ip:` transports survived identical ladders *and* 60 s soaks |
| Pluto clone: retune latency | **Measured** | 51 ms cold, ~33 ms steady state, read-back confirmed |
| Pluto clone: dropped samples | **Measured** | USB: 0 jumps at 3 and 5 MSPS, 4 at 10 rising to 27 at 40. Ethernet: 0 at 5, 10 and 15 MSPS, first drops at 20 |
| Pluto clone: Ethernet path | **Working and measured** | pinned at `192.168.1.50` by a static `ipaddr_eth`; the cause was a churning DHCP address, not a service-binding fault |
| Pluto clone: capability probe | **Verified on hardware** | `2,083,333 .. 61,440,000 Hz`, gain `-1 .. 73 dB`, bandwidth to `56 MHz`, **12 bits in 16-bit containers = 4 bytes/complex**; the 12-bit answer only appeared after the receive-DMA fix |
| Capability probe never raises | **Verified** | a runner that fails degrades to an empty attribute plus a note; probing a wedged board must not throw from a listing refresh |
| Child reaping: tracked child | **Verified on hardware** | `rtl_power` (pid 32672) running and holding a dongle, parent exited, **no surviving process**, next launch enumerated both dongles |
| Child reaping: untracked control | **Verified on hardware** | same child via plain `Popen` **survived** the parent and made the next launch read `2 dongle(s) found: , , SN: ÿ` |
| Child reaping, hard kill | **Not covered** | `atexit` does not run on a Task Manager kill; a Job Object is needed and is not claimed |
| Capture health on real hardware | **Verified, no false alarm** | 5 s @ 2.048 MS/s, 5 s @ 2.4 MS/s, 20 s @ 2.4 MS/s: all **lag 0 ms, 0.000% shortfall**, 224/260/1364 rows |
| `effective_rate_hz` accuracy | **Measured, and biased high** | +0.87% / +0.47% / +0.21% over those runs; shrinks with duration, so a window offset rather than the hardware |
| `rtl_sdr` overrun reporting | **Absent** | an unread stdout stalls it: 0 bytes in 6 s and no overrun line on stderr, so loss cannot be parsed from its output |
| Two Meshtastic radios at once | **Verified** | COM24 (Heltec V4, node 2859752693) and COM16 (T-Beam S3 Core, node 1130080812) connected simultaneously; second open took 13.6 s |
| Same serial port twice | **Refused by the OS** | `SerialException: PermissionError(13, 'Access is denied.')` |
| One radio, two transports | **Verified on both boards** | serial MAC vs BLE MAC+1: Heltec `..81:BC`/`..81:BD`, T-Beam `48CA435BAA2C`/`48:CA:43:5B:AA:2D`; the OS protects only the port |
| Radio grouping without opening anything | **Verified live** | enumerates 3 candidates (2 radios + 1 non-radio device), each radio once with both doors, no port opened |
| `MultiInterface.isConnected` truthiness | **A trap, not a bug yet** | it is a `threading.Event`; `if iface.isConnected:` is always true. OrcMesh does not do this today |
| Multi-radio in the app (Stage C proper) | **Not started** | the lease and registry land; the controller still holds one interface and closes it on every connect |
