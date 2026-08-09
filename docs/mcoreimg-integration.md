# MCoreIMG Integration — Design Discussion

Status: **exploratory — no implementation started.** This document captures the
research and open decisions from an initial discussion about bringing
[MCoreIMG](https://github.com/Aaron-KE9ETA/MCoreIMG) (a compact vector-image
transport protocol built for MeshCore) into OrcMesh.

---

## What MCoreIMG is

MCoreIMG encodes vector artwork (imported from SVG) into a strict message
budget — **10 messages of 150 characters each** — and reconstructs it on the
receiving end into a PNG. It's built by Aaron Cocanower (KE9ETA), MIT licensed,
protocol v5, and organized as four layered Python modules with no third layer
reaching into the one below it:

| Module | Role | Depends on |
|---|---|---|
| `MCoreIMG-model.py` | Opcodes, commands, geometry, colour quantization | stdlib only |
| `MCoreIMG-compression.py` | Bitstream codec, record selection, **MeshCore framing** | model |
| `MCoreIMG-Constructor.py` | SVG import, rendering, authoring GUI | model, compression |
| `MCoreIMG-Reconstructor.py` | Frame decode, PNG render, JSON export | model, compression, constructor |

### How it compresses

- **Palette** — every colour quantized to RGB565 + 4-bit alpha, capped at 32
  entries; commands reference palette indices instead of carrying colour data.
- **Stateful delta coding** — coordinates are usually deltas against the
  previous point for the same opcode (Rice / Exp-Golomb coded), falling back
  to absolute only when that's cheaper.
- **Repeat detection** — translated copies of a single command, or of a
  contiguous run of commands, are replaced with a short back-reference plus a
  delta instead of re-sending the geometry.
- **Local-space SVG groups** — an imported SVG is normalized into a stable
  local coordinate box, sent once, and placed with a fixed-width transform.
  Moving/resizing costs nothing extra; duplicated SVGs reuse the definition.
- **Primitive pricing** — a small library of compact shapes (Yagi antenna,
  dish, moon, etc.) is only used when it's *strictly smaller* than the generic
  vector expansion it represents.
- **Precision search** — the encoder walks a ladder of coordinate precisions
  and picks the highest one that still fits the 10-message budget.

Record stream: 2-bit tag per record (`REC_NORMAL`, `REC_SINGLE_REPEAT`,
`REC_GROUP_REPEAT`, `REC_TRANSFORM_GROUP`) → CRC-32 over the whole stream →
Base91-encoded → split across frames, each with its own 15-byte header and
CRC-16.

### Licensing

MIT. No legal obstacle to forking or vendoring any part of it.

---

## Why it doesn't drop in as-is: MeshCore ≠ Meshtastic

MCoreIMG's framing constants are tuned to MeshCore's transport:

| | MeshCore (MCoreIMG's target) | Meshtastic (OrcMesh's target) |
|---|---|---|
| Message size limit | 150 characters | 200 **bytes** (UTF-8; OrcMesh already enforces this in the composer) |
| Frame header | 15 chars, MeshCore-specific | would need a Meshtastic-specific design |
| Per-frame integrity | CRC-16 | reusable as-is |
| Message addressing | MeshCore's own | Meshtastic channel index / direct-message node ID (OrcMesh already models both — see `ChatMessage.destination_num`) |

The **framing layer is the MeshCore-specific part** and can't be reused
directly — the byte budget, header format, and message-count math would all
need to be re-derived for a 200-byte Meshtastic message.

The **model + compression core is transport-agnostic by the project's own
architecture** (their `Architecture.md` explicitly separates "codec" from
"framing" as a design principle). That's the ~100KB of code that's
realistically reusable: palette assembly, delta coding, repeat detection,
local-space groups, primitive pricing. None of it knows or cares what carries
the bytes.

**Bottom line:** this is a vendor-and-adapt situation, not a drop-in dependency.
The reusable core is real and substantial; the framing/transport layer and all
of the OrcMesh-side UI would be new work.

---

## The "QR code" question — worth clarifying explicitly

The idea raised was: *could an SVG encode more data than its file size, the
way a QR code seems to?*

QR codes don't get extra data for free — they encode exactly as many bits as
their grid capacity allows, with real overhead spent on error correction. The
reason they feel like "more than the file size" is that the reader is a
**camera performing an optical scan** of a printed pattern; a 2D grid of
black/white cells is a much denser *visual* channel than, say, a 1D barcode,
for that specific transport method (printed ink, camera sensor).

That trick doesn't apply here. OrcMesh has no optical scan step — the
Meshtastic text messages *are* the data channel directly, with no image
representation in between. Drawing our compressed bitstream as a QR-style
raster pattern wouldn't add capacity; MCoreIMG's existing Base91 text encoding
is already a *more* efficient use of a 200-byte message than a QR bitmap
would be, because there's no camera/decode-noise margin to spend bits on.

### The idea that *does* hold up

MCoreIMG's real contribution isn't "images" specifically — it's a general
pattern: **compress structured, semantically-known data into a tight
bitstream, then ship it across N radio messages with CRC/framing.** The vector
drawing vocabulary (opcodes, palette, delta coding) is one instance of
"structured data" that happens to compress well under that pattern. The same
machinery could just as easily pack:

- Waypoint sets / breadcrumb trails (lat/lon deltas compress far better than
  raw text coordinates)
- Small structured reports (sensor snapshots, status forms — anything with
  known fields)
- Actual pictures (the original use case)

This reframes the real architectural decision (see below): build **image
messaging** specifically, or a **generic compact-data channel** that images
are just one payload type of.

---

## Open decisions

These were raised in discussion and are **not yet resolved** — this document
exists to make them explicit, not to answer them unilaterally.

### 1. Scope: images-only vs. generic data channel

| Option | Tradeoff |
|---|---|
| **Images first** | Smallest useful scope — compose/send a picture in chat, receive and render it. Proves the codec-port + framing + UI pipeline end-to-end. A generic channel could reuse this foundation later without having over-built upfront. |
| **Generic data channel first** | Bigger upfront design (a payload-type-agnostic framing envelope), but avoids re-architecting the framing layer later if a non-image use case is already wanted. |
| **Images only, no generalization** | Simplest surface area, matches MCoreIMG's original design intent exactly. |

### 2. Fork vs. vendor

| Option | Tradeoff |
|---|---|
| **Vendor a one-time copy** | Copy `model` + `compression` into OrcMesh, adapt freely for our framing. No dependency on Aaron's repo staying maintained or stable. Loses future upstream compression improvements automatically. |
| **Real fork, track upstream** | Can pull in future protocol/compression improvements. More coordination overhead once OrcMesh's framing diverges from MeshCore's — merges get harder the more the two projects' transport layers differ. |

Given the framing layer *must* diverge regardless (Meshtastic's byte budget is
different from MeshCore's), a real fork mostly buys future improvements to the
**codec**, not the framing. That's a real but narrower benefit than "stay in
sync with the whole project."

---

## If/when this moves to implementation — rough shape

Not a commitment, just what the work would concretely involve, to size it:

1. **Port the codec.** Vendor or fork `MCoreIMG-model.py` and the
   non-framing parts of `MCoreIMG-compression.py` (palette, delta coding,
   repeat detection, local-space groups, primitive pricing, Base91).
2. **New Meshtastic framing.** Design a frame header/budget sized to 200
   bytes/message instead of 150 chars, reusing CRC-16 per-frame + CRC-32
   stream-level integrity. Needs a message-count strategy (fixed N like
   MCoreIMG's "10", or variable based on content).
3. **New message kind in OrcMesh's chat model.** `ChatMessage` currently
   models plain text; an image transfer needs to be recognizable as
   "part of image transfer #X, frame N/M" so it doesn't render as garbled
   text in the chat bubble, and so out-of-order/partial arrivals can be
   buffered correctly (Meshtastic doesn't guarantee delivery order).
4. **Compose UI.** Import/select an SVG (or a simple drawing tool) in
   `ChatView`, live message-count budget as the user composes — mirroring
   MCoreIMG's Constructor GUI feedback loop, adapted to OrcMesh's theme
   and widget set.
5. **Receive UI.** Detect an in-progress transfer, show a progress
   indicator across incoming frames, decode with the vendored Reconstructor
   logic once complete, render inline in the chat bubble (not a separate
   window).
6. **Decide fork vs. vendor** (see above) before step 1, since it affects
   how the vendored files are organized in the OrcMesh tree.

Sizing note: the codec port (step 1–2) is moderate, well-scoped work given the
existing clean module boundaries. The UI (steps 3–5) is the larger piece —
compose-time budget feedback and receive-time partial-transfer handling are
both real state machines, not just plumbing.

---

## Sources

- https://github.com/Aaron-KE9ETA/MCoreIMG (MIT license, protocol v5,
  `2026-08-05`)
- `Architecture.md`, `Compression_readme.md`, `README.md` from that repo
