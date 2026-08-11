from types import SimpleNamespace

import pytest

from meshtastic.protobuf import channel_pb2, localonly_pb2

from meshchat.services.device_config import apply_section, build_snapshot


class _Node:
    def __init__(self):
        self.localConfig = localonly_pb2.LocalConfig()
        self.moduleConfig = localonly_pb2.LocalModuleConfig()
        self.channels = [channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY)]
        self.writes = []

    def writeConfig(self, name):
        self.writes.append(name)


def _interface():
    node = _Node()
    node.localConfig.network.wifi_ssid = "mesh-lan"
    node.localConfig.network.wifi_psk = "never expose this"
    node.localConfig.lora.ignore_incoming.extend([1, 2])
    node.moduleConfig.mqtt.password = "also secret"
    return SimpleNamespace(
        localNode=node,
        myInfo=SimpleNamespace(my_node_num=123, pio_env="tbeam-s3-core"),
        metadata=SimpleNamespace(
            firmware_version="2.7.26.54e0d8d",
            can_shutdown=True,
            has_wifi=True,
            has_bluetooth=True,
        ),
        nodesByNum={123: {"user": {
            "id": "!0000007b", "longName": "Test", "shortName": "TST",
            "hwModel": "LILYGO_TBEAM_S3_CORE",
        }}},
    )


def test_snapshot_redacts_credentials_and_preserves_editable_values():
    snapshot = build_snapshot(_interface(), "COM8")
    assert snapshot.pio_env == "tbeam-s3-core"
    assert snapshot.serial_port == "COM8"
    network = next(section for section in snapshot.sections if section.name == "network")
    values = {field.name: field for field in network.fields}
    assert values["wifi_ssid"].value == "mesh-lan"
    assert values["wifi_psk"].value == ""
    assert values["wifi_psk"].write_only
    assert "never expose this" not in repr(snapshot)
    assert "also secret" not in repr(snapshot)


def test_apply_section_coerces_values_and_writes_only_requested_section():
    node = _Node()
    apply_section(node, "lora", {
        "hop_limit": "5",
        "tx_enabled": False,
        "ignore_incoming": [7, "9"],
    })
    assert node.localConfig.lora.hop_limit == 5
    assert node.localConfig.lora.tx_enabled is False
    assert list(node.localConfig.lora.ignore_incoming) == [7, 9]
    assert node.writes == ["lora"]


def test_apply_section_does_not_clear_write_only_field_when_left_blank():
    node = _Node()
    node.localConfig.network.wifi_psk = "keep-me"
    apply_section(node, "network", {"wifi_psk": ""})
    assert node.localConfig.network.wifi_psk == "keep-me"


def test_cryptographic_key_fields_are_not_editable():
    node = _Node()
    with pytest.raises(ValueError, match="not editable"):
        apply_section(node, "security", {"private_key": b"bad"})


def test_apply_section_rejects_names_outside_allowlist():
    with pytest.raises(ValueError, match="Unknown configuration section"):
        apply_section(_Node(), "version", {})
