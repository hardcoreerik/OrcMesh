import hashlib
import json
import sys
import zipfile
from dataclasses import replace
from importlib.metadata import version
from types import SimpleNamespace

import pytest

from meshchat.services import firmware
from meshchat.controllers.firmware_controller import FirmwareController


def test_discovery_uses_manifest_target_and_official_digest(monkeypatch):
    release = {
        "tag_name": "v2.7.26.abc",
        "name": "Meshtastic Firmware 2.7.26.abc Beta",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-01-01T00:00:00Z",
        "assets": [
            {"name": "firmware-2.7.26.abc.json", "browser_download_url": "manifest"},
            {
                "name": "firmware-esp32s3-2.7.26.abc.zip",
                "browser_download_url": "bundle",
                "size": 123,
                "digest": "sha256:" + "a" * 64,
            },
        ],
    }
    monkeypatch.setattr(
        firmware, "_json",
        lambda url: [release] if url == firmware._RELEASES_API else {
            "targets": [{"board": "tbeam-s3-core", "platform": "esp32s3"}]
        },
    )
    found = firmware.discover_release("tbeam-s3-core")
    assert found.platform == "esp32s3"
    assert found.asset_sha256 == "a" * 64
    assert found.channel == "beta"


def test_discovery_rejects_target_not_in_manifest(monkeypatch):
    monkeypatch.setattr(firmware, "_json", lambda _url: [{
        "tag_name": "v1", "draft": False, "prerelease": False,
        "assets": [{"name": "firmware-1.json", "browser_download_url": "manifest"}],
    }] if _url == firmware._RELEASES_API else {"targets": []})
    with pytest.raises(firmware.FirmwareError, match="does not support"):
        firmware.discover_release("tbeam-s3-core")


def test_discovery_rejects_unsupported_platform(monkeypatch):
    release = {
        "tag_name": "v1", "draft": False, "prerelease": False,
        "assets": [{"name": "firmware-1.json", "browser_download_url": "manifest"}],
    }
    monkeypatch.setattr(
        firmware,
        "_json",
        lambda url: [release] if url == firmware._RELEASES_API else {
            "targets": [{"board": "other", "platform": "esp32"}]
        },
    )
    with pytest.raises(firmware.FirmwareError, match="does not support platform"):
        firmware.discover_release("other")


@pytest.mark.parametrize("url", ["http://github.com/file", "https://example.com/file"])
def test_firmware_urls_are_restricted_to_github_https(url):
    with pytest.raises(firmware.FirmwareError, match="official GitHub HTTPS"):
        firmware._validate_url(url)


def _release(tmp_path):
    return firmware.FirmwareRelease(
        tag="v1", version="1", prerelease=False, published_at="",
        asset_name="bundle.zip", asset_url="", asset_size=1,
        asset_sha256="a" * 64, platform="esp32s3",
    )


def test_prepare_bundle_checks_target_hardware_and_member_hashes(tmp_path, monkeypatch):
    release = _release(tmp_path)
    names = {
        "firmware-tbeam-s3-core-1.bin": b"update",
        "firmware-tbeam-s3-core-1.factory.bin": b"factory",
        "littlefs-tbeam-s3-core-1.bin": b"filesystem",
        "mt-esp32s3-ota.bin": b"ota",
    }
    metadata = {
        "platformioTarget": "tbeam-s3-core",
        "hwModelSlug": "LILYGO_TBEAM_S3_CORE",
        "activelySupported": True,
        "requiresDfu": True,
        "files": [
            {"name": name, "md5": hashlib.md5(data).hexdigest()}  # noqa: S324 - release format
            for name, data in names.items()
        ],
        "part": [
            {"subtype": "ota_1", "offset": "0x340000"},
            {"subtype": "spiffs", "offset": "0x670000"},
        ],
    }
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name, data in names.items():
            output.writestr(name, data)
        output.writestr("firmware-tbeam-s3-core-1.mt.json", json.dumps(metadata))
    monkeypatch.setattr(firmware, "_download", lambda _release, _progress: archive)
    monkeypatch.setattr(firmware, "_cache_root", lambda: tmp_path / "cache")
    bundle = firmware.prepare_bundle(release, "tbeam-s3-core", "LILYGO_TBEAM_S3_CORE")
    assert bundle.requires_dfu
    assert bundle.ota_offset == "0x340000"
    firmware.validate_bundle(bundle)


def test_prepare_bundle_rejects_missing_image_hash(tmp_path, monkeypatch):
    release = _release(tmp_path)
    names = {
        "firmware-tbeam-s3-core-1.bin": b"update",
        "firmware-tbeam-s3-core-1.factory.bin": b"factory",
        "littlefs-tbeam-s3-core-1.bin": b"filesystem",
        "mt-esp32s3-ota.bin": b"ota",
    }
    metadata = {
        "platformioTarget": "tbeam-s3-core",
        "hwModelSlug": "LILYGO_TBEAM_S3_CORE",
        "files": [],
        "part": [
            {"subtype": "ota_1", "offset": "0x340000"},
            {"subtype": "spiffs", "offset": "0x670000"},
        ],
    }
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name, data in names.items():
            output.writestr(name, data)
        output.writestr("firmware-tbeam-s3-core-1.mt.json", json.dumps(metadata))
    monkeypatch.setattr(firmware, "_download", lambda _release, _progress: archive)
    monkeypatch.setattr(firmware, "_cache_root", lambda: tmp_path / "cache")
    with pytest.raises(firmware.FirmwareError, match="missing a valid image hash"):
        firmware.prepare_bundle(release, "tbeam-s3-core", "LILYGO_TBEAM_S3_CORE")


def test_flash_update_uses_only_verified_update_offset(tmp_path, monkeypatch):
    files = {}
    for name in ("update.bin", "factory.bin", "ota.bin", "fs.bin"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        files[name] = path
    calls = []
    def esptool_main(args):
        calls.append(args)

    monkeypatch.setitem(sys.modules, "esptool", SimpleNamespace(main=esptool_main))
    bundle = firmware.FirmwareBundle(
        release=_release(tmp_path), root=tmp_path, pio_env="target", hw_model="model",
        requires_dfu=False, update_image=files["update.bin"], factory_image=files["factory.bin"],
        ota_image=files["ota.bin"], filesystem_image=files["fs.bin"],
        ota_offset="0x340000", filesystem_offset="0x670000",
        file_md5={name: hashlib.md5(path.read_bytes()).hexdigest() for name, path in files.items()},  # noqa: S324
    )
    firmware.flash_bundle(bundle, "COM8", False)
    prefix = ["--verbose", "--chip", "esp32s3", "--port", "COM8", "--baud", "115200"]
    assert calls == [
        [*prefix, "--before", "default-reset", "--after", "no-reset", "chip-id"],
        [*prefix, "--before", "no-reset", "--after", "hard-reset", "write-flash", "0x10000", str(files["update.bin"])],
    ]


def test_flash_refuses_unsupported_platform(tmp_path, monkeypatch):
    files = {}
    for name in ("update.bin", "factory.bin", "ota.bin", "fs.bin"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        files[name] = path
    bundle = firmware.FirmwareBundle(
        release=replace(_release(tmp_path), platform="esp32"),
        root=tmp_path, pio_env="target", hw_model="model",
        requires_dfu=False, update_image=files["update.bin"], factory_image=files["factory.bin"],
        ota_image=files["ota.bin"], filesystem_image=files["fs.bin"],
        ota_offset="0x340000", filesystem_offset="0x670000",
        file_md5={name: hashlib.md5(path.read_bytes()).hexdigest() for name, path in files.items()},  # noqa: S324
    )
    with pytest.raises(firmware.FirmwareError, match="cannot verify platform"):
        firmware.flash_bundle(bundle, "COM8", False)


def test_full_install_erases_then_writes_verified_partition_offsets(tmp_path, monkeypatch):
    files = {}
    for name in ("update.bin", "factory.bin", "ota.bin", "fs.bin"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        files[name] = path
    calls = []

    def esptool_main(args):
        calls.append(args)

    monkeypatch.setitem(sys.modules, "esptool", SimpleNamespace(main=esptool_main))
    bundle = firmware.FirmwareBundle(
        release=_release(tmp_path), root=tmp_path, pio_env="target", hw_model="model",
        requires_dfu=True, update_image=files["update.bin"], factory_image=files["factory.bin"],
        ota_image=files["ota.bin"], filesystem_image=files["fs.bin"],
        ota_offset="0x340000", filesystem_offset="0x670000",
        file_md5={name: hashlib.md5(path.read_bytes()).hexdigest() for name, path in files.items()},  # noqa: S324
    )
    firmware.flash_bundle(bundle, "COM8", True)
    prefix = ["--verbose", "--chip", "esp32s3", "--port", "COM8", "--baud", "115200"]
    assert calls == [
        [*prefix, "--before", "default-reset", "--after", "no-reset", "chip-id"],
        [*prefix, "--before", "no-reset", "--after", "no-reset", "erase-flash"],
        [*prefix, "--before", "no-reset", "--after", "no-reset", "write-flash", "0x0", str(files["factory.bin"])],
        [*prefix, "--before", "no-reset", "--after", "no-reset", "write-flash", "0x340000", str(files["ota.bin"])],
        [*prefix, "--before", "no-reset", "--after", "hard-reset", "write-flash", "0x670000", str(files["fs.bin"])],
    ]


def test_dfu_failure_returns_recovery_instructions(tmp_path, monkeypatch):
    files = {}
    for name in ("update.bin", "factory.bin", "ota.bin", "fs.bin"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        files[name] = path
    monkeypatch.setitem(
        sys.modules,
        "esptool",
        SimpleNamespace(main=lambda _args: (_ for _ in ()).throw(RuntimeError("no sync"))),
    )
    monkeypatch.setattr(
        firmware,
        "_automatic_bootloader_port",
        lambda *_args: (_ for _ in ()).throw(firmware.FirmwareError("no DFU port")),
    )
    bundle = firmware.FirmwareBundle(
        release=_release(tmp_path), root=tmp_path, pio_env="target", hw_model="model",
        requires_dfu=True, update_image=files["update.bin"], factory_image=files["factory.bin"],
        ota_image=files["ota.bin"], filesystem_image=files["fs.bin"],
        ota_offset="0x340000", filesystem_offset="0x670000",
        file_md5={name: hashlib.md5(path.read_bytes()).hexdigest() for name, path in files.items()},  # noqa: S324
    )
    with pytest.raises(firmware.FirmwareError, match="Hold BOOT, tap RESET"):
        firmware.flash_bundle(bundle, "COM8", False)


def test_dfu_failure_retries_on_automatic_bootloader_port(tmp_path, monkeypatch):
    files = {}
    for name in ("update.bin", "factory.bin", "ota.bin", "fs.bin"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        files[name] = path
    calls = []

    def esptool_main(args):
        calls.append(args)
        if args[-1] == "chip-id" and "COM8" in args:
            raise RuntimeError("no sync")

    monkeypatch.setitem(sys.modules, "esptool", SimpleNamespace(main=esptool_main))
    monkeypatch.setattr(firmware, "_automatic_bootloader_port", lambda *_args: "COM9")
    bundle = firmware.FirmwareBundle(
        release=_release(tmp_path), root=tmp_path, pio_env="target", hw_model="model",
        requires_dfu=True, update_image=files["update.bin"], factory_image=files["factory.bin"],
        ota_image=files["ota.bin"], filesystem_image=files["fs.bin"],
        ota_offset="0x340000", filesystem_offset="0x670000",
        file_md5={name: hashlib.md5(path.read_bytes()).hexdigest() for name, path in files.items()},  # noqa: S324
    )

    firmware.flash_bundle(bundle, "COM8", False)

    assert [call[4] for call in calls] == ["COM8", "COM9", "COM9"]


def test_automatic_bootloader_uses_1200_baud_and_follows_new_port(monkeypatch):
    opened = []

    class FakeSerial:
        def __init__(self, **kwargs):
            opened.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    import serial
    from serial.tools import list_ports

    monkeypatch.setattr(serial, "Serial", FakeSerial)
    monkeypatch.setattr(firmware.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        list_ports,
        "comports",
        lambda: [SimpleNamespace(device="COM9", vid=0x303A, pid=0x1001, serial_number="radio")],
    )

    port = firmware._automatic_bootloader_port(
        "COM8", (0x303A, 0x1001, "radio"), lambda _line: None
    )

    assert port == "COM9"
    assert opened == [{"port": "COM8", "baudrate": 1200, "timeout": 1}]


def test_probe_runs_read_only_diagnostics_and_resets_last(monkeypatch):
    calls = []
    monkeypatch.setattr(firmware, "_verify_usb", lambda *_args: None)
    monkeypatch.setattr(firmware, "_bootloader_port", lambda *_args: "COM9")
    monkeypatch.setattr(
        firmware,
        "_run_esptool",
        lambda chip, port, args, _output, before="default-reset", after="no-reset": calls.append(
            (chip, port, args, before, after)
        ),
    )

    firmware.probe_device("COM8")

    assert calls == [
        ("auto", "COM9", ["read-mac"], "no-reset", "no-reset"),
        ("auto", "COM9", ["flash-id"], "no-reset", "no-reset"),
        ("auto", "COM9", ["get-security-info"], "no-reset", "hard-reset"),
    ]


def test_raw_flash_backup_is_atomic_and_writes_manifest(tmp_path, monkeypatch):
    calls = []
    destination = tmp_path / "radio.bin"
    monkeypatch.setattr(firmware, "_verify_usb", lambda *_args: None)
    monkeypatch.setattr(firmware, "_bootloader_port", lambda *_args: "COM9")

    def run(_chip, _port, args, _output, before="default-reset", after="no-reset"):
        calls.append((args, before, after))
        if args[0] == "read-flash":
            firmware.Path(args[-1]).write_bytes(b"raw flash")

    monkeypatch.setattr(firmware, "_run_esptool", run)

    firmware.backup_flash("COM8", destination, (0x303A, 0x1001, "radio"))

    assert destination.read_bytes() == b"raw flash"
    assert not destination.with_suffix(".bin.partial").exists()
    manifest = json.loads(destination.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["sha256"] == hashlib.sha256(b"raw flash").hexdigest()
    assert manifest["usb_serial"] == "radio"
    assert calls == [(["read-flash", "0", "ALL", str(destination.with_suffix('.bin.partial'))], "no-reset", "hard-reset")]


def test_installed_esptool_accepts_commands_used_by_flasher():
    esptool = pytest.importorskip("esptool")
    assert int(version("esptool").split(".", 1)[0]) >= 5
    prefix = [
        "--verbose", "--chip", "esp32s3", "--port", "COM8", "--baud", "115200",
        "--before", "default-reset", "--after", "no-reset",
    ]
    for command in (
        "chip-id", "read-mac", "flash-id", "get-security-info",
        "read-flash", "erase-flash", "write-flash",
    ):
        esptool.main([*prefix, command, "--help"])


def test_firmware_shutdown_waits_for_active_worker():
    waits = []
    thread = SimpleNamespace(
        quit=lambda: None,
        wait=lambda timeout=None: waits.append(timeout) or timeout is None,
    )
    FirmwareController.shutdown(SimpleNamespace(_thread=thread))
    assert waits == [5000, None]
