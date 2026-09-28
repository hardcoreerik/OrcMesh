"""Tests for capability probing.

The range strings below are verbatim from ``iio_attr`` on this bench, in both of the shapes
it produces: the bare form when a named attribute is asked for, and the verbose form when a
whole channel is listed. The channel-format string is verbatim from ``iio_info``.
"""
from __future__ import annotations

import pytest

from meshchat.services.rf import capabilities
from meshchat.services.rf.base import MEASURED_LIMITS, ETHERNET, USB, USB_GADGET


class TestParseRange:
    def test_the_bare_form_from_a_named_attribute(self):
        assert capabilities.parse_range("[2083333 1 61440000]") == (2083333.0, 1.0, 61440000.0)

    def test_the_verbose_form_from_a_channel_listing(self):
        """Verbatim: this is what `-c ad9361-phy voltage0` with no attribute prints."""
        line = (
            "dev 'ad9361-phy', channel 'voltage0' (input), attr "
            "'sampling_frequency_available', value '[2083333 1 61440000]'"
        )

        assert capabilities.parse_range(line) == (2083333.0, 1.0, 61440000.0)

    def test_a_single_value_is_a_range_of_one_not_a_failure(self):
        """The transmit attenuation really does report one value: [-89.750000]."""
        assert capabilities.parse_range("[-89.750000]") == (-89.75, 0.0, -89.75)

    def test_a_fractional_step_survives(self):
        assert capabilities.parse_range("[0.0 0.1 20.0]") == (0.0, 0.1, 20.0)

    def test_nonsense_is_none_rather_than_a_wrong_number(self):
        assert capabilities.parse_range("") is None
        assert capabilities.parse_range("no range here") is None

    def test_the_real_gain_range_includes_its_negative_end(self):
        """Verbatim: RX gain on this board is -3 to 71 dB, so the sign must survive."""
        assert capabilities.parse_range("[-3 1 71]") == (-3.0, 1.0, 71.0)


class TestParseSampleFormat:
    def test_the_receive_dma_format_is_the_real_one(self):
        """Verbatim shape: 12 significant bits in a 16-bit container."""
        listing = (
            "        iio:device3: cf-ad9361-lpc (buffer capable)\n"
            "                4 channels found:\n"
            "                        voltage0:  (input, index: 0, format: le:S12/16>>0)\n"
        )

        assert capabilities.parse_sample_format(listing) == (12, 16)

    def test_the_transmit_dma_listed_first_does_not_answer_for_the_receiver(self):
        """The trap the live probe hit: TX is listed before RX and is 16-bit.

        Taking the first `format:` in the listing reported 16 bits for a 12-bit receiver,
        which reads as plausible and inflates nothing except its own credibility.
        """
        listing = (
            "        iio:device2: cf-ad9361-dds-core-lpc (buffer capable)\n"
            "                12 channels found:\n"
            "                        voltage0:  (output, index: 0, format: le:S16/16>>0)\n"
            "        iio:device3: cf-ad9361-lpc (buffer capable)\n"
            "                4 channels found:\n"
            "                        voltage0:  (input, index: 0, format: le:S12/16>>0)\n"
        )

        assert capabilities.parse_sample_format(listing) == (12, 16)

    def test_a_device_that_is_not_listed_is_none_rather_than_another_device_s_format(self):
        assert capabilities.parse_sample_format("voltage0:  (input, format: le:S16/16>>0)") is None

    def test_no_format_at_all_is_none(self):
        assert capabilities.parse_sample_format("        iio:device3: cf-ad9361-lpc\n") is None


class TestReadFirstLine:
    def test_it_takes_the_first_value_not_the_last(self):
        """Asked for one attribute, iio_attr prints input then output. Input is the RX side.

        Reading the last line reports the transmit rate and makes a successful receive
        setting look like it silently failed -- which is exactly what happened here once.
        """
        assert capabilities.read_first_line("30720000\n30720000\n") == "30720000"

    def test_leading_blank_lines_are_skipped(self):
        assert capabilities.read_first_line("\n\n  [-3 1 71]\n") == "[-3 1 71]"

    def test_nothing_at_all_is_none(self):
        assert capabilities.read_first_line("") is None


class TestMeasuredLimits:
    def test_ethernet_is_the_lossless_fast_path(self):
        assert MEASURED_LIMITS[ETHERNET].lossless_rate_hz == 15_000_000

    def test_the_gadget_is_a_third_of_ethernet(self):
        assert MEASURED_LIMITS[USB_GADGET].lossless_rate_hz == 5_000_000

    def test_raw_usb_is_the_fastest_and_marked_unreliable(self):
        """The fastest transport is deliberately not the one to choose."""
        assert MEASURED_LIMITS[USB].sustained_mb_per_s > MEASURED_LIMITS[USB_GADGET].sustained_mb_per_s
        assert MEASURED_LIMITS[USB].reliable is False
        assert MEASURED_LIMITS[USB_GADGET].reliable is True


def _runner_from(answers: dict[str, str], fail_on: str | None = None):
    """A stand-in for the tool runner, keyed on the attribute being asked for."""

    def run(args, timeout_s):
        joined = " ".join(str(part) for part in args)
        if fail_on is not None and fail_on in joined:
            raise OSError("pretend the tool is not there")
        for needle, answer in answers.items():
            if needle in joined:
                return answer
        return ""

    return run


_REAL_ANSWERS = {
    "sampling_frequency_available": "[2083333 1 61440000]\n[2083333 1 61440000]\n",
    "hardwaregain_available": "[-3 1 71]\n[-89.750000]\n",
    "rf_bandwidth_available": "[200000 1 56000000]\n",
    "iio_info": (
        "        iio:device2: cf-ad9361-dds-core-lpc (buffer capable)\n"
        "                        voltage0:  (output, index: 0, format: le:S16/16>>0)\n"
        "        iio:device3: cf-ad9361-lpc (buffer capable)\n"
        "                        voltage0:  (input, index: 0, format: le:S12/16>>0)\n"
    ),
}


class TestProbeLibiio:
    def test_it_reads_the_real_numbers_off_a_device(self, monkeypatch):
        from meshchat.services import rtl_tools

        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: "C:/tools/" + name)

        result = capabilities.probe_libiio("ip:192.168.1.50", run=_runner_from(_REAL_ANSWERS))

        assert result is not None
        assert result.min_rate_hz == 2083333.0
        assert result.max_rate_hz == 61440000.0
        assert (result.min_gain_db, result.max_gain_db) == (-3.0, 71.0)
        assert result.max_bandwidth_hz == 56000000.0

    def test_bytes_per_sample_accounts_for_the_container_not_the_resolution(self, monkeypatch):
        """12 bits in a 16-bit container is 4 bytes per complex sample, not 3."""
        from meshchat.services import rtl_tools

        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: "C:/tools/" + name)

        result = capabilities.probe_libiio("ip:192.168.1.50", run=_runner_from(_REAL_ANSWERS))

        assert result is not None
        assert result.bits == 12
        assert result.bytes_per_complex_sample == 4

    def test_the_unusable_advertised_minimum_is_flagged(self, monkeypatch):
        """The board advertises 2,083,333 Hz and refuses anything under about 3 MSPS."""
        from meshchat.services import rtl_tools

        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: "C:/tools/" + name)

        result = capabilities.probe_libiio("ip:192.168.1.50", run=_runner_from(_REAL_ANSWERS))

        assert result is not None
        assert any("advertised minimum" in note for note in result.notes)

    def test_a_device_that_answers_nothing_is_reported_not_invented(self, monkeypatch):
        from meshchat.services import rtl_tools

        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: "C:/tools/" + name)

        result = capabilities.probe_libiio("ip:192.168.2.1", run=_runner_from({}))

        assert result is not None
        assert result.min_rate_hz is None
        assert any("did not report" in note for note in result.notes)

    def test_a_missing_tool_is_none_rather_than_a_guess(self, monkeypatch):
        from meshchat.services import rtl_tools

        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: None)

        assert capabilities.probe_libiio("ip:192.168.1.50", run=_runner_from(_REAL_ANSWERS)) is None

    def test_a_tool_that_raises_does_not_escape(self, monkeypatch):
        """A wedged device must cost an empty capability, not a traceback up the stack."""
        from meshchat.services import rtl_tools

        monkeypatch.setattr(rtl_tools, "find_tool", lambda name: "C:/tools/" + name)

        result = capabilities.probe_libiio(
            "ip:192.168.1.50", run=_runner_from(_REAL_ANSWERS, fail_on="hardwaregain"),
        )

        assert result is not None
        assert result.max_gain_db is None

    def test_a_non_callable_runner_is_refused(self):
        assert capabilities.probe_libiio("ip:192.168.1.50", run=None) is None


@pytest.mark.parametrize("kind", [ETHERNET, USB_GADGET, USB])
def test_every_measured_limit_records_how_it_was_measured(kind):
    """A number without its conditions is a number nobody can check."""
    assert MEASURED_LIMITS[kind].note
