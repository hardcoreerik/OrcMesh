"""Tests for identifying a radio.

`identify` is the only thing in `services/radios/` that opens a port, so these tests are
mostly about what it does when the port does not answer — which is the case that happens on
real hardware here, with an Espressif device attached that is not a radio.
"""
from __future__ import annotations

import pytest

from meshchat.services import radios
from meshchat.services.radios import identification

COM16 = radios.RadioTransport(kind=radios.SERIAL, address="COM16", label="COM16")


class _Metadata:
    """The protobuf shape. Note it is an object, not a mapping — `.get()` would raise."""

    firmware_version = "2.8.0.47db0e3"


class _MyInfo:
    my_node_num = 2859752693


class _Interface:
    def __init__(self, *, answered: bool = True) -> None:
        self.metadata = _Metadata()
        self.myInfo = _MyInfo()
        self.closed = False
        self._answered = answered

    def getMyNodeInfo(self):
        if not self._answered:
            return None
        return {
            "user": {
                "longName": "Hardcoreerik",
                "shortName": "hrdc",
                "hwModel": "HELTEC_V4",
            },
        }

    def close(self) -> None:
        self.closed = True


def _opener(interface: _Interface):
    def open_it(transport, timeout_s):
        return interface

    return open_it


class TestIdentify:
    def test_a_radio_that_answers_is_described(self):
        info = radios.identify(COM16, opener=_opener(_Interface()))

        assert info.node_num == 2859752693
        assert info.long_name == "Hardcoreerik"
        assert info.hardware == "HELTEC_V4"
        assert info.firmware == "2.8.0.47db0e3"

    def test_the_key_is_the_node_number_which_is_the_strongest_identity(self):
        info = radios.identify(COM16, opener=_opener(_Interface()))

        assert info.key == "radio:2859752693"

    def test_the_interface_is_closed_afterwards(self):
        """A radio left open by an identification would be unusable to everything else."""
        interface = _Interface()

        radios.identify(COM16, opener=_opener(interface))

        assert interface.closed

    def test_the_interface_is_closed_even_when_reading_it_fails(self):
        class _ExplodingNode:
            @property
            def my_node_num(self):
                raise RuntimeError("the link died mid-handshake")

        class _Broken(_Interface):
            def __init__(self):
                super().__init__()
                self.myInfo = _ExplodingNode()

        interface = _Broken()

        with pytest.raises(radios.RadioUnidentified):
            radios.identify(COM16, opener=_opener(interface))

        assert interface.closed

    def test_the_description_names_the_radio_and_its_firmware(self):
        info = radios.identify(COM16, opener=_opener(_Interface()))

        assert "Hardcoreerik" in info.describe()
        assert "2.8.0.47db0e3" in info.describe()

    def test_a_firmware_version_that_is_missing_does_not_print_as_none(self):
        class _NoFirmware:
            firmware_version = ""

        class _Quiet(_Interface):
            def __init__(self):
                super().__init__()
                self.metadata = _NoFirmware()

        info = radios.identify(COM16, opener=_opener(_Quiet()))

        assert "unknown" in info.describe()
        assert "None" not in info.describe()


class TestNotARadio:
    """The real case on this bench: a device that never answers, and must not be probed."""

    def _refuse(self, *_args, **_kwargs):
        raise TimeoutError("Timed out waiting for connection completion")

    def test_a_failure_raises_rather_than_returning_nothing(self):
        with pytest.raises(radios.RadioUnidentified):
            radios.identify(COM16, opener=self._refuse)

    def test_the_message_offers_every_likely_reason_rather_than_one_guess(self):
        """Nothing here can tell "not a radio" from "a radio that is unwell".

        Claiming one of them would put a wrong diagnosis in front of a user, so the message
        lists the possibilities and names the port.
        """
        with pytest.raises(radios.RadioUnidentified) as caught:
            radios.identify(COM16, opener=self._refuse)

        message = str(caught.value)
        assert "COM16" in message
        assert "not a Meshtastic radio" in message
        assert "busy elsewhere" in message

    def test_the_underlying_reason_is_kept_for_the_log(self):
        with pytest.raises(radios.RadioUnidentified) as caught:
            radios.identify(COM16, opener=self._refuse)

        assert isinstance(caught.value.__cause__, TimeoutError)

    def test_an_unknown_transport_is_refused_by_name(self):
        with pytest.raises(radios.RadioUnidentified) as caught:
            identification.identify(radios.RadioTransport(kind="carrier-pigeon", address="X"))

        assert "carrier-pigeon" in str(caught.value)


class TestTimeout:
    def test_the_default_timeout_exceeds_a_measured_serial_connect(self):
        """13.6 s measured here. A ten-second timeout would call healthy radios broken."""
        assert radios.CONNECT_TIMEOUT_S > 13.6

    def test_the_timeout_reaches_the_opener(self):
        seen = {}

        def opener(transport, timeout_s):
            seen["timeout_s"] = timeout_s
            return _Interface()

        radios.identify(COM16, opener=opener)

        assert seen["timeout_s"] == radios.CONNECT_TIMEOUT_S

    def test_a_caller_can_ask_for_less(self):
        seen = {}

        def opener(transport, timeout_s):
            seen["timeout_s"] = timeout_s
            return _Interface()

        radios.identify(COM16, timeout_s=5.0, opener=opener)

        assert seen["timeout_s"] == 5.0
