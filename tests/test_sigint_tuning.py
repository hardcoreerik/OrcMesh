"""Tests for remembering the SIGINT tuning.

The rules that matter are about what happens to a stored value that is *wrong*: from another
machine, from an older version, or edited by hand. Restoring a bad centre silently would put the
receiver somewhere nobody asked for, and clamping it would be worse — a centre moved by a
hundred megahertz without saying so looks like it worked.
"""
from __future__ import annotations

import pytest

from meshchat.services.sigint_tuning import (
    MAX_CENTRE_MHZ,
    MIN_CENTRE_MHZ,
    SigintTuning,
)


class FakeStore:
    """The two calls the module needs, plus a record of what was written."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.writes: list[tuple[str, str]] = []

    def get_setting(self, key: str) -> str | None:
        return self.values.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self.values[key] = value
        self.writes.append((key, value))


class TestRoundTrip:
    def test_what_is_saved_is_what_comes_back(self):
        store = FakeStore()
        saved = SigintTuning(centre_mhz=906.875, rate_msps=2.0, gain_db=16.0,
                             region="US", preset="US mesh")

        saved.save(store)
        loaded = SigintTuning.load(store)

        assert loaded is not None
        assert loaded.centre_mhz == pytest.approx(906.875)
        assert loaded.rate_msps == pytest.approx(2.0)
        assert loaded.gain_db == pytest.approx(16.0)
        assert loaded.region == "US"
        assert loaded.preset == "US mesh"

    def test_an_empty_store_returns_nothing_rather_than_defaults(self):
        """So a caller can tell 'never set up' from 'set up and stored these'."""
        assert SigintTuning.load(FakeStore()) is None

    def test_every_value_is_stored_under_the_pages_own_prefix(self):
        store = FakeStore()

        SigintTuning(centre_mhz=906.875, rate_msps=2.0, gain_db=16.0).save(store)

        assert all(key.startswith("sigint.") for key, _ in store.writes)
        # Nothing from the radio connection is touched: this is a different concern.
        assert not any(key.startswith("connection.") for key, _ in store.writes)

    def test_the_description_reads_like_the_controls(self):
        text = SigintTuning(centre_mhz=906.875, rate_msps=2.0, gain_db=16.0).describe()

        assert "906.875 MHz" in text
        assert "2.00 MS/s" in text
        assert "16.0 dB" in text


class TestBadValues:
    """Stored values come from another session and are not trusted back."""

    def _stored(self, **overrides: str) -> FakeStore:
        values = {
            "sigint.centre_mhz": "906.875",
            "sigint.rate_msps": "2.0",
            "sigint.gain_db": "16.0",
        }
        values.update({f"sigint.{k}": v for k, v in overrides.items()})
        return FakeStore(values)

    def test_a_centre_outside_the_control_range_drops_the_whole_tuning(self):
        """Not clamped: a centre moved silently is worse than one obviously not restored."""
        assert SigintTuning.load(self._stored(centre_mhz="4000")) is None

    def test_a_negative_centre_is_refused_too(self):
        assert SigintTuning.load(self._stored(centre_mhz="-100")) is None

    def test_a_rate_beyond_what_the_hardware_does_is_refused(self):
        assert SigintTuning.load(self._stored(rate_msps="40")) is None

    def test_a_gain_beyond_the_tuners_table_is_refused(self):
        assert SigintTuning.load(self._stored(gain_db="99")) is None

    def test_a_value_that_is_not_a_number_is_refused(self):
        assert SigintTuning.load(self._stored(centre_mhz="nine hundred")) is None

    def test_a_partly_stored_tuning_is_not_restored_at_all(self):
        """Half a tuning would leave the receiver somewhere neither session intended."""
        store = FakeStore({"sigint.centre_mhz": "906.875"})

        assert SigintTuning.load(store) is None

    def test_the_boundaries_themselves_are_accepted(self):
        store = self._stored(centre_mhz=str(MIN_CENTRE_MHZ), rate_msps="0.25", gain_db="0")

        loaded = SigintTuning.load(store)

        assert loaded is not None
        assert loaded.centre_mhz == pytest.approx(MIN_CENTRE_MHZ)

    def test_the_top_of_the_range_is_accepted(self):
        store = self._stored(centre_mhz=str(MAX_CENTRE_MHZ), rate_msps="3.2", gain_db="49.6")

        assert SigintTuning.load(store) is not None


class TestTextFields:
    def test_a_region_or_preset_that_vanished_does_not_break_the_numbers(self):
        """A name no longer offered must not take the centre frequency down with it."""
        store = FakeStore({
            "sigint.centre_mhz": "906.875",
            "sigint.rate_msps": "2.0",
            "sigint.gain_db": "16.0",
            "sigint.region": "ATLANTIS",
            "sigint.preset": "no such preset",
        })

        loaded = SigintTuning.load(store)

        assert loaded is not None
        assert loaded.centre_mhz == pytest.approx(906.875)
        assert loaded.region == "ATLANTIS"

    def test_missing_text_fields_come_back_empty_not_none(self):
        store = FakeStore({
            "sigint.centre_mhz": "906.875",
            "sigint.rate_msps": "2.0",
            "sigint.gain_db": "16.0",
        })

        loaded = SigintTuning.load(store)

        assert loaded is not None
        assert loaded.region == ""
        assert loaded.preset == ""


class TestDeviceIndex:
    def _with_index(self, raw: str) -> SigintTuning | None:
        return SigintTuning.load(FakeStore({
            "sigint.centre_mhz": "906.875",
            "sigint.rate_msps": "2.0",
            "sigint.gain_db": "16.0",
            "sigint.device_index": raw,
        }))

    def test_the_dongle_index_is_remembered(self):
        loaded = self._with_index("1")

        assert loaded is not None and loaded.device_index == 1

    def test_a_nonsense_index_falls_back_to_the_first_dongle(self):
        """Unlike the numbers, a bad index is safe to default: it cannot move the receiver."""
        loaded = self._with_index("banana")

        assert loaded is not None and loaded.device_index == 0

    def test_an_out_of_range_index_falls_back(self):
        loaded = self._with_index("99")

        assert loaded is not None and loaded.device_index == 0


class TestStoreFailures:
    def test_a_store_that_refuses_a_write_does_not_raise(self):
        """A settings write must never be able to break a capture."""

        class Refusing(FakeStore):
            def set_setting(self, key: str, value: str) -> None:
                raise OSError("disk full")

        SigintTuning(centre_mhz=906.875, rate_msps=2.0, gain_db=16.0).save(Refusing())

    def test_a_store_that_refuses_the_first_write_stops_trying(self):
        """No point writing five more keys to a store that has just failed."""

        class Refusing(FakeStore):
            def set_setting(self, key: str, value: str) -> None:
                self.writes.append((key, value))
                raise OSError("disk full")

        store = Refusing()
        SigintTuning(centre_mhz=906.875, rate_msps=2.0, gain_db=16.0).save(store)

        assert len(store.writes) == 1
