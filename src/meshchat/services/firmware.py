"""Official Meshtastic firmware discovery, validation, and ESP32 flashing."""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import platformdirs


_RELEASES_API = "https://api.github.com/repos/meshtastic/firmware/releases?per_page=20"
_USER_AGENT = "OrcMesh-firmware/0.2"
_MAX_ASSET_BYTES = 300 * 1024 * 1024
_PORT_RE = re.compile(r"^COM\d+$", re.IGNORECASE)
_GITHUB_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
_CHIP_BY_PLATFORM = {"esp32s3": "esp32s3"}


class FirmwareError(RuntimeError):
    pass


@dataclass(frozen=True)
class FirmwareRelease:
    tag: str
    version: str
    prerelease: bool
    published_at: str
    asset_name: str
    asset_url: str
    asset_size: int
    asset_sha256: str
    platform: str


@dataclass(frozen=True)
class FirmwareBundle:
    release: FirmwareRelease
    root: Path
    pio_env: str
    hw_model: str
    requires_dfu: bool
    update_image: Path
    factory_image: Path
    ota_image: Path
    filesystem_image: Path
    ota_offset: str
    filesystem_offset: str
    file_md5: dict[str, str]


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _GITHUB_HOSTS:
        raise FirmwareError("Firmware network access is restricted to official GitHub HTTPS hosts.")


def _json(url: str):
    _validate_url(url)
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        _validate_url(response.geturl())
        return json.load(response)


def discover_release(pio_env: str, include_prerelease: bool = False) -> FirmwareRelease:
    if not pio_env or not re.fullmatch(r"[A-Za-z0-9_.-]+", pio_env):
        raise FirmwareError("The connected radio did not report a valid firmware target.")
    releases = _json(_RELEASES_API)
    release = next((
        item for item in releases
        if not item.get("draft") and (include_prerelease or not item.get("prerelease"))
    ), None)
    if release is None:
        raise FirmwareError("No matching Meshtastic firmware release is available.")
    version = str(release["tag_name"]).removeprefix("v")
    manifest_name = f"firmware-{version}.json"
    manifest_asset = next((a for a in release["assets"] if a["name"] == manifest_name), None)
    if manifest_asset is None:
        raise FirmwareError("The release does not contain a firmware target manifest.")
    manifest = _json(manifest_asset["browser_download_url"])
    target = next((t for t in manifest.get("targets", []) if t.get("board") == pio_env), None)
    if target is None:
        raise FirmwareError(f"Release {version} does not support {pio_env}.")
    platform = target["platform"]
    if platform not in _CHIP_BY_PLATFORM:
        raise FirmwareError(f"OrcMesh firmware flashing does not support platform {platform}.")
    asset_name = f"firmware-{platform}-{version}.zip"
    asset = next((a for a in release["assets"] if a["name"] == asset_name), None)
    if asset is None:
        raise FirmwareError(f"Release {version} is missing {asset_name}.")
    size = int(asset.get("size") or 0)
    if not 0 < size <= _MAX_ASSET_BYTES:
        raise FirmwareError("The firmware asset has an invalid size.")
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:"):
        raise FirmwareError("The official release asset does not provide a SHA-256 digest.")
    return FirmwareRelease(
        tag=release["tag_name"],
        version=version,
        prerelease=bool(release.get("prerelease")),
        published_at=release.get("published_at") or "",
        asset_name=asset_name,
        asset_url=asset["browser_download_url"],
        asset_size=size,
        asset_sha256=digest.removeprefix("sha256:"),
        platform=platform,
    )


def _cache_root() -> Path:
    root = Path(platformdirs.user_cache_dir("OrcMesh", appauthor=False)) / "firmware"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _hash(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(release: FirmwareRelease, progress: Callable[[int, int], None]) -> Path:
    destination = _cache_root() / release.asset_name
    if destination.exists() and destination.stat().st_size == release.asset_size:
        if _hash(destination, "sha256") == release.asset_sha256:
            progress(release.asset_size, release.asset_size)
            return destination
        destination.unlink()
    partial = destination.with_suffix(destination.suffix + ".partial")
    _validate_url(release.asset_url)
    request = urllib.request.Request(release.asset_url, headers={"User-Agent": _USER_AGENT})
    received = 0
    with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
        _validate_url(response.geturl())
        while block := response.read(1024 * 1024):
            output.write(block)
            received += len(block)
            if received > _MAX_ASSET_BYTES:
                raise FirmwareError("Firmware download exceeded the safety limit.")
            progress(received, release.asset_size)
    if received != release.asset_size or _hash(partial, "sha256") != release.asset_sha256:
        partial.unlink(missing_ok=True)
        raise FirmwareError("Firmware download failed its size or SHA-256 verification.")
    os.replace(partial, destination)
    return destination


def prepare_bundle(
    release: FirmwareRelease,
    pio_env: str,
    hw_model: str,
    progress: Callable[[int, int], None] = lambda _done, _total: None,
) -> FirmwareBundle:
    archive = _download(release, progress)
    destination = _cache_root() / release.version / pio_env
    destination.mkdir(parents=True, exist_ok=True)
    metadata_name = f"firmware-{pio_env}-{release.version}.mt.json"
    wanted_prefixes = (
        f"firmware-{pio_env}-{release.version}.bin",
        f"firmware-{pio_env}-{release.version}.factory.bin",
        f"littlefs-{pio_env}-{release.version}.bin",
        f"mt-{release.platform}-ota.bin",
        metadata_name,
    )
    with zipfile.ZipFile(archive) as bundle_zip:
        members = {Path(info.filename).name: info for info in bundle_zip.infolist()}
        missing = [name for name in wanted_prefixes if name not in members]
        if missing:
            raise FirmwareError("Firmware bundle is missing: " + ", ".join(missing))
        for name in wanted_prefixes:
            info = members[name]
            if info.file_size > 32 * 1024 * 1024:
                raise FirmwareError(f"Unexpectedly large firmware member: {name}")
            target = destination / name
            with bundle_zip.open(info) as source, target.open("wb") as output:
                while block := source.read(1024 * 1024):
                    output.write(block)

    metadata = json.loads((destination / metadata_name).read_text(encoding="utf-8"))
    if metadata.get("platformioTarget") != pio_env:
        raise FirmwareError("Firmware target metadata does not match the connected radio.")
    expected_hw = metadata.get("hwModelSlug") or ""
    if hw_model and expected_hw and expected_hw != hw_model:
        raise FirmwareError(f"Firmware hardware model {expected_hw} does not match {hw_model}.")
    if metadata.get("activelySupported") is False:
        raise FirmwareError("This firmware target is no longer actively supported.")
    hashes = {item["name"]: item["md5"] for item in metadata.get("files", []) if item.get("md5")}
    for name in wanted_prefixes[:-1]:
        expected = hashes.get(name)
        if not expected or not re.fullmatch(r"[0-9a-fA-F]{32}", expected):
            raise FirmwareError(f"Firmware metadata is missing a valid image hash: {name}")
        if _hash(destination / name, "md5") != expected.lower():
            raise FirmwareError(f"Firmware member failed verification: {name}")
    parts = {part["subtype"]: part["offset"] for part in metadata.get("part", [])}
    if "ota_1" not in parts or "spiffs" not in parts:
        raise FirmwareError("Firmware partition metadata is incomplete.")
    for subtype in ("ota_1", "spiffs"):
        offset = str(parts[subtype])
        if not re.fullmatch(r"0x[0-9a-fA-F]+", offset) or not 0 < int(offset, 16) < 32 * 1024 * 1024:
            raise FirmwareError(f"Firmware partition offset is invalid: {subtype}")
    return FirmwareBundle(
        release=release,
        root=destination,
        pio_env=pio_env,
        hw_model=expected_hw or hw_model,
        requires_dfu=bool(metadata.get("requiresDfu")),
        update_image=destination / wanted_prefixes[0],
        factory_image=destination / wanted_prefixes[1],
        filesystem_image=destination / wanted_prefixes[2],
        ota_image=destination / wanted_prefixes[3],
        ota_offset=parts["ota_1"],
        filesystem_offset=parts["spiffs"],
        file_md5=hashes,
    )


def validate_bundle(bundle: FirmwareBundle) -> None:
    for path in (
        bundle.update_image, bundle.factory_image,
        bundle.ota_image, bundle.filesystem_image,
    ):
        expected = bundle.file_md5.get(path.name)
        if (
            not path.is_file()
            or not expected
            or not re.fullmatch(r"[0-9a-fA-F]{32}", expected)
            or _hash(path, "md5") != expected.lower()
        ):
            raise FirmwareError(f"Firmware file is missing or changed: {path.name}")


def flash_bundle(
    bundle: FirmwareBundle,
    port: str,
    full_install: bool,
    expected_usb: tuple[int | None, int | None, str | None] | None = None,
    output: Callable[[str], None] = lambda _line: None,
) -> None:
    if os.name == "nt" and not _PORT_RE.fullmatch(port):
        raise FirmwareError("Select one explicit Windows COM port before flashing.")
    if expected_usb is not None:
        from serial.tools import list_ports
        current = next((item for item in list_ports.comports() if item.device == port), None)
        if current is None:
            raise FirmwareError(f"The verified radio is no longer present on {port}.")
        expected_vid, expected_pid, expected_serial = expected_usb
        if expected_vid is not None and current.vid != expected_vid:
            raise FirmwareError("The USB vendor changed after disconnect; refusing to flash.")
        if expected_pid is not None and current.pid != expected_pid:
            raise FirmwareError("The USB product changed after disconnect; refusing to flash.")
        if expected_serial and current.serial_number != expected_serial:
            raise FirmwareError("A different USB device now owns the selected COM port.")
    validate_bundle(bundle)
    chip = _CHIP_BY_PLATFORM.get(bundle.release.platform)
    if chip is None:
        raise FirmwareError(
            f"OrcMesh cannot verify platform {bundle.release.platform}; refusing to flash."
        )
    try:
        import esptool
    except ImportError as exc:
        raise FirmwareError("esptool is not installed in this OrcMesh build.") from exc

    def run(args: list[str]) -> None:
        output("esptool " + " ".join(Path(arg).name if "firmware" in arg else arg for arg in args))
        try:
            esptool.main([
                "--chip", chip, "--port", port, "--baud", "115200", *args,
            ])
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise FirmwareError(f"esptool failed with exit code {exc.code}") from exc
        except Exception as exc:
            raise FirmwareError(str(exc)) from exc

    try:
        run(["chip-id"])
    except FirmwareError as exc:
        if bundle.requires_dfu:
            raise FirmwareError(
                "Could not enter the ESP32 bootloader. Hold BOOT, tap RESET, release BOOT, "
                "then retry with the radio's COM port."
            ) from exc
        raise

    if full_install:
        run(["erase-flash"])
        run(["write-flash", "0x0", str(bundle.factory_image)])
        run(["write-flash", bundle.ota_offset, str(bundle.ota_image)])
        run(["write-flash", bundle.filesystem_offset, str(bundle.filesystem_image)])
    else:
        run(["write-flash", "0x10000", str(bundle.update_image)])
    output("Firmware flash completed; waiting for the radio to reboot.")
