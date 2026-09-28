"""What can this machine actually do? Decide before promising anything.

OrcMaps' own provisioning spec splits its audience in two and warns that
conflating them is the main risk:

* **Population A** — deploy once, then done. Must never need Java, Planetiler,
  Python, a CLI, bbox coordinates, or any knowledge of PMTiles. For them the
  product is a *provisioner*: pick coverage, fetch it, verify it, done.
* **Population B** — wants to build. A heavy external toolchain is acceptable,
  but it must be *detected and reported honestly, never assumed*.

So the wizard's first job is to answer "what is possible here?" without raising,
without network calls it cannot afford, and without letting the user discover a
missing prerequisite halfway through a build. This module is that answer.
"""
from __future__ import annotations

from dataclasses import dataclass

from meshchat.services.orcmaps import OrcMapsPack, OrcMapsTools

#: How the wizard will get packs on this machine.
MODE_BUILD = "build"
MODE_DOWNLOAD = "download"
MODE_UNAVAILABLE = "unavailable"

MODE_LABELS: dict[str, str] = {
    MODE_BUILD: "Build them here",
    MODE_DOWNLOAD: "Download ready-made packs",
    MODE_UNAVAILABLE: "Maps cannot be set up yet",
}


def source_packs_for(packs: list[OrcMapsPack], max_zoom: int) -> list[OrcMapsPack]:
    """Packs deep enough to cut a ``max_zoom`` region out of, cheapest first.

    A cut can only reproduce zoom levels its source archive already holds, so a
    z0-9 pack cannot yield street detail no matter what is asked of it. Among
    the packs that do qualify, the smallest is preferred: any of them can serve
    the zoom, and the small one is the least work to read.
    """
    qualifying = [pack for pack in packs if pack.max_zoom >= max_zoom]
    # An unknown size sorts *last*, not first: `size_bytes or 0` would have made
    # a pack we know nothing about look like the cheapest one to cut from.
    return sorted(
        qualifying,
        key=lambda pack: (pack.size_bytes if pack.size_bytes else float("inf"), pack.stem),
    )


@dataclass(frozen=True)
class SetupCapability:
    """The honest answer to "what can we do here", with the reasons attached."""

    tools: OrcMapsTools | None
    packs: tuple[OrcMapsPack, ...]
    deepest_source_zoom: int
    catalog_available: bool
    reasons: tuple[str, ...]

    @property
    def can_build_locally(self) -> bool:
        """Cutting needs the provisioner script *and* a deep enough source."""
        if self.tools is None or self.tools.provision_script is None:
            return False
        if self.tools.pmtiles_cli is None:
            return False
        return self.deepest_source_zoom > 0

    @property
    def can_download(self) -> bool:
        return self.catalog_available

    @property
    def mode(self) -> str:
        """Build is preferred when possible: it needs no catalogue and no trust."""
        if self.can_build_locally:
            return MODE_BUILD
        if self.can_download:
            return MODE_DOWNLOAD
        return MODE_UNAVAILABLE

    @property
    def mode_label(self) -> str:
        return MODE_LABELS[self.mode]

    def source_for(self, max_zoom: int) -> OrcMapsPack | None:
        candidates = source_packs_for(list(self.packs), max_zoom)
        return candidates[0] if candidates else None


def detect_capability(
    tools: OrcMapsTools | None,
    packs: list[OrcMapsPack],
    *,
    catalog_available: bool = False,
    catalog_reason: str = "",
) -> SetupCapability:
    """Assemble the capability report. Never raises, never hits the network.

    ``packs`` is whatever discovery already found, so the caller stays the one
    place that decides where packs come from.
    """
    reasons: list[str] = []
    deepest = max((pack.max_zoom for pack in packs), default=0)

    if tools is None:
        reasons.append(
            "OrcMaps' host tools were not found, so packs cannot be built on "
            "this machine. Downloading ready-made packs does not need them."
        )
    else:
        if tools.provision_script is None:
            reasons.append(
                "OrcMaps' provision_pack.py was not found in the checkout, so "
                "packs cannot be cut here."
            )
        if tools.pmtiles_cli is None:
            reasons.append(
                "The pinned go-pmtiles binary was not found in the OrcMaps "
                "checkout, so packs cannot be cut here."
            )
        if deepest == 0:
            reasons.append(
                "No source archive is deep enough to cut from. Building a "
                "region needs a state-level z13 archive first."
            )
        elif deepest < 13:
            reasons.append(
                f"The deepest source archive here only reaches z{deepest}, so "
                "street-level detail cannot be cut from it."
            )
    if not catalog_available:
        reasons.append(
            catalog_reason
            or "No pack catalogue is configured, so ready-made packs cannot be "
               "downloaded."
        )

    return SetupCapability(
        tools=tools,
        packs=tuple(packs),
        deepest_source_zoom=deepest,
        catalog_available=catalog_available,
        reasons=tuple(reasons),
    )


def capability_summary(capability: SetupCapability) -> str:
    """One line for the wizard header, stating what will happen."""
    if capability.mode == MODE_BUILD:
        source = capability.source_for(13)
        where = f" from '{source.display_name}'" if source is not None else ""
        return f"Regions will be built on this machine{where}."
    if capability.mode == MODE_DOWNLOAD:
        return "Regions will be downloaded and verified before they are installed."
    return "Maps cannot be set up yet — see the details below."
