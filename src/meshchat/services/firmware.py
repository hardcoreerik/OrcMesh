"""Official Meshtastic firmware discovery, validation, and ESP32 flashing."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import platformdirs


_RELEASES_API = "https://api.github.com/repos/meshtastic/firmware/releases?per_page=20"
_USER_AGENT = "OrcMesh-firmware/0.2"
_MAX_ASSET_BYTES = 300 * 1024 * 1024
_PORT_RE = re.compile(r"^COM\d+$", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
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
    channel: str = "stable"


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
    release_name = str(release.get("name") or "").lower()
    channel = "alpha" if release.get("prerelease") or "alpha" in release_name else (
        "beta" if "beta" in release_name else "stable"
    )
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
        channel=channel,
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


def _automatic_bootloader_port(
    port: str,
    expected_usb: tuple[int | None, int | None, str | None] | None,
    output: Callable[[str], None],
) -> str:
    from serial import Serial, SerialException
    from serial.tools import list_ports

    output("Normal reset failed; trying automatic 1200-bps bootloader entry.")
    try:
        with Serial(port=port, baudrate=1200, timeout=1):
            time.sleep(0.5)
    except SerialException:
        # The normal reset may already have started USB re-enumeration.
        pass

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        candidates = list(list_ports.comports())
        if expected_usb is None:
            match = next((item for item in candidates if item.device == port), None)
        else:
            expected_vid, expected_pid, expected_serial = expected_usb
            matches = [
                item for item in candidates
                if (expected_vid is None or item.vid == expected_vid)
                and (expected_pid is None or item.pid == expected_pid)
                and (not expected_serial or item.serial_number == expected_serial)
            ]
            match = next((item for item in matches if item.device == port), None)
            if match is None and len(matches) == 1:
                match = matches[0]
        if match is not None:
            output(f"Bootloader available on {match.device}.")
            return str(match.device)
        time.sleep(0.25)
    raise FirmwareError("The radio did not reappear in bootloader mode.")


class _LineWriter:
    def __init__(self, output: Callable[[str], None]):
        self._output = output
        self._buffer = ""

    def write(self, value: str) -> int:
        self._buffer += _ANSI_RE.sub("", value).replace("\r", "\n")
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        for line in lines:
            if line.strip():
                self._output(line.rstrip())
        return len(value)

    def flush(self) -> None:
        if self._buffer.strip():
            self._output(self._buffer.rstrip())
        self._buffer = ""


def _verify_usb(
    port: str,
    expected_usb: tuple[int | None, int | None, str | None] | None,
) -> None:
    if os.name == "nt" and not _PORT_RE.fullmatch(port):
        raise FirmwareError("Select one explicit Windows COM port.")
    if expected_usb is None:
        return
    from serial.tools import list_ports
    current = next((item for item in list_ports.comports() if item.device == port), None)
    if current is None:
        raise FirmwareError(f"The verified radio is no longer present on {port}.")
    expected_vid, expected_pid, expected_serial = expected_usb
    if expected_vid is not None and current.vid != expected_vid:
        raise FirmwareError("The USB vendor changed after disconnect; refusing the operation.")
    if expected_pid is not None and current.pid != expected_pid:
        raise FirmwareError("The USB product changed after disconnect; refusing the operation.")
    if expected_serial and current.serial_number != expected_serial:
        raise FirmwareError("A different USB device now owns the selected COM port.")


def _run_esptool(
    chip: str,
    port: str,
    args: list[str],
    output: Callable[[str], None],
    before: str = "default-reset",
    after: str = "no-reset",
) -> None:
    try:
        import esptool
    except ImportError as exc:
        raise FirmwareError("esptool is not installed in this OrcMesh build.") from exc
    shown = [Path(arg).name if ("/" in arg or "\\" in arg) else arg for arg in args]
    output(
        f"esptool --verbose --chip {chip} --port {port} --before {before} "
        f"--after {after} " + " ".join(shown)
    )
    writer = _LineWriter(output)
    try:
        with redirect_stdout(writer), redirect_stderr(writer):
            esptool.main([
                "--verbose", "--chip", chip, "--port", port, "--baud", "115200",
                "--before", before, "--after", after, *args,
            ])
    except SystemExit as exc:
        if exc.code not in (None, 0):
            raise FirmwareError(f"esptool failed with exit code {exc.code}") from exc
    except Exception as exc:
        raise FirmwareError(str(exc)) from exc
    finally:
        writer.flush()


def _bootloader_port(
    chip: str,
    port: str,
    expected_usb: tuple[int | None, int | None, str | None] | None,
    output: Callable[[str], None],
    automatic_retry: bool,
) -> str:
    try:
        _run_esptool(chip, port, ["chip-id"], output)
        return port
    except FirmwareError:
        if not automatic_retry:
            raise
    try:
        port = _automatic_bootloader_port(port, expected_usb, output)
        _run_esptool(chip, port, ["chip-id"], output)
        return port
    except FirmwareError as exc:
        raise FirmwareError(
            "Automatic bootloader entry failed. Hold BOOT, tap RESET, release BOOT, "
            "then retry with the radio's COM port."
        ) from exc


def probe_device(
    port: str,
    expected_usb: tuple[int | None, int | None, str | None] | None = None,
    output: Callable[[str], None] = lambda _line: None,
) -> None:
    _verify_usb(port, expected_usb)
    port = _bootloader_port("auto", port, expected_usb, output, True)
    for command in ("read-mac", "flash-id"):
        _run_esptool("auto", port, [command], output, "no-reset")
    _run_esptool(
        "auto", port, ["get-security-info"], output, "no-reset", "hard-reset"
    )
    output("Read-only device probe completed.")


def backup_flash(
    port: str,
    destination: Path,
    expected_usb: tuple[int | None, int | None, str | None] | None = None,
    output: Callable[[str], None] = lambda _line: None,
) -> Path:
    _verify_usb(port, expected_usb)
    destination = destination.resolve()
    if destination.suffix.lower() != ".bin" or not destination.parent.is_dir():
        raise FirmwareError("Choose a .bin backup file in an existing folder.")
    partial = destination.with_suffix(".bin.partial")
    partial.unlink(missing_ok=True)
    port = _bootloader_port("auto", port, expected_usb, output, True)
    try:
        _run_esptool(
            "auto", port, ["read-flash", "0", "ALL", str(partial)],
            output, "no-reset", "hard-reset"
        )
        if not partial.is_file() or partial.stat().st_size == 0:
            raise FirmwareError("The raw flash backup is empty.")
        digest = _hash(partial, "sha256")
        os.replace(partial, destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    manifest = {
        "format": "OrcMesh raw flash backup v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "image": destination.name,
        "bytes": destination.stat().st_size,
        "sha256": digest,
        "usb_vid": expected_usb[0] if expected_usb else None,
        "usb_pid": expected_usb[1] if expected_usb else None,
        "usb_serial": expected_usb[2] if expected_usb else None,
    }
    destination.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    output(f"Raw flash backup saved as {destination.name} (SHA-256 {digest}).")
    return destination


def flash_bundle(
    bundle: FirmwareBundle,
    port: str,
    full_install: bool,
    expected_usb: tuple[int | None, int | None, str | None] | None = None,
    output: Callable[[str], None] = lambda _line: None,
) -> None:
    _verify_usb(port, expected_usb)
    validate_bundle(bundle)
    chip = _CHIP_BY_PLATFORM.get(bundle.release.platform)
    if chip is None:
        raise FirmwareError(
            f"OrcMesh cannot verify platform {bundle.release.platform}; refusing to flash."
        )
    port = _bootloader_port(chip, port, expected_usb, output, bundle.requires_dfu)

    if full_install:
        _run_esptool(chip, port, ["erase-flash"], output, "no-reset")
        _run_esptool(
            chip, port, ["write-flash", "0x0", str(bundle.factory_image)], output, "no-reset"
        )
        _run_esptool(
            chip, port, ["write-flash", bundle.ota_offset, str(bundle.ota_image)],
            output, "no-reset"
        )
        _run_esptool(chip, port, [
            "write-flash", bundle.filesystem_offset, str(bundle.filesystem_image)
        ], output, "no-reset", "hard-reset")
    else:
        _run_esptool(
            chip, port, ["write-flash", "0x10000", str(bundle.update_image)],
            output, "no-reset", "hard-reset"
        )
    output("Firmware flash completed; waiting for the radio to reboot.")
