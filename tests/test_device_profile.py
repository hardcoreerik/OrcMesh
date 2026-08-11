import hashlib
from types import SimpleNamespace

from meshtastic.protobuf.clientonly_pb2 import DeviceProfile
from meshtastic.protobuf.localonly_pb2 import LocalConfig, LocalModuleConfig

from meshchat.services import device_profile


def test_save_profile_writes_meshtastic_cfg_atomically(tmp_path, monkeypatch):
    raw = b"meshtastic-profile"
    monkeypatch.setattr("meshtastic.__main__.export_profile", lambda _interface: raw)
    destination = tmp_path / "radio.cfg"

    digest = device_profile.save_profile(object(), destination)

    assert destination.read_bytes() == raw
    assert digest == hashlib.sha256(raw).hexdigest()
    assert not destination.with_suffix(".cfg.partial").exists()


def test_restore_profile_creates_safety_backup_before_writes(tmp_path, monkeypatch):
    profile = DeviceProfile(long_name="Orc Radio", short_name="ORC", channel_url="https://channel")
    profile.config.lora.region = 1
    profile.config.position.fixed_position = True
    profile.fixed_position.latitude_i = 340000000
    profile.fixed_position.longitude_i = -1180000000
    profile.module_config.telemetry.device_update_interval = 60
    source = tmp_path / "restore.cfg"
    source.write_bytes(profile.SerializeToString())
    events = []

    class Node:
        localConfig = LocalConfig()
        moduleConfig = LocalModuleConfig()

        def beginSettingsTransaction(self): events.append("begin")
        def setOwner(self, **values): events.append(("owner", values))
        def setURL(self, _url): events.append("url")
        def setFixedPosition(self, *_position): events.append("position")
        def writeConfig(self, section): events.append(("config", section))
        def commitSettingsTransaction(self): events.append("commit")

    monkeypatch.setattr(device_profile.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        device_profile,
        "save_profile",
        lambda _interface, destination: events.append(("safety", destination.name)) or "digest",
    )

    safety = device_profile.restore_profile(SimpleNamespace(localNode=Node()), source)

    assert events[0][0] == "safety"
    assert events[1] == "begin"
    assert events[-1] == "commit"
    assert safety.name.startswith("restore.pre-restore-")
    assert ("config", "lora") in events
    assert ("config", "position") in events
    assert ("config", "telemetry") in events


def test_load_profile_rejects_empty_profile(tmp_path):
    source = tmp_path / "empty.cfg"
    source.write_bytes(b"")

    try:
        device_profile.load_profile(source)
    except ValueError as exc:
        assert "empty or too large" in str(exc)
    else:
        raise AssertionError("empty profile was accepted")
