"""Wire-encoding contract.

These tests pin the two properties a port has to reproduce: every finite
double survives a store round-trip unchanged, and the accepted input grammar
is exactly the documented one — not whatever the host language's float parser
happens to allow.
"""

from __future__ import annotations

import math

import pytest

from pretrade_risk.encoding import (
    decode_bool,
    decode_float,
    decode_positive_float,
    encode_bool,
    encode_float,
)


class TestFloatRoundTrip:
    @pytest.mark.parametrize(
        "value",
        [
            0.0,
            -0.0,
            1.0,
            -12.5,
            0.1,
            0.1 + 0.2,  # 0.30000000000000004 — the classic non-representable sum
            1e-300,
            1e300,
            -1e300,
            math.pi,
            123456789.123456789,
            5e-324,  # smallest subnormal
            1.7976931348623157e308,  # largest finite double
        ],
    )
    def test_round_trips_bit_exactly(self, value: float) -> None:
        assert decode_float(encode_float(value)) == value

    def test_accumulated_error_survives_a_reload(self) -> None:
        # A day of counter arithmetic must reload to the SAME double, or the
        # halt boundary moves across a restart.
        total = 0.0
        for _ in range(1000):
            total += 0.1
        assert decode_float(encode_float(total)) == total


class TestFloatEncodingIsPortable:
    def test_non_finite_encodes_fail_safe(self) -> None:
        # A persisted infinity would read back as unlimited headroom.
        assert encode_float(math.inf) == "0"
        assert encode_float(-math.inf) == "0"
        assert encode_float(math.nan) == "0"

    def test_uses_seventeen_significant_digits(self) -> None:
        # %.17g, not the host language's default float formatting.
        assert encode_float(0.1) == "0.10000000000000001"


class TestGrammarIsExplicit:
    @pytest.mark.parametrize(
        "raw",
        ["0", "-0", "1", "-12.5", "1.", ".5", "-.5", "1e3", "1E3", "1e+3", "1e-3", "+2.5"],
    )
    def test_accepts_the_documented_grammar(self, raw: str) -> None:
        assert decode_float(raw) is not None

    @pytest.mark.parametrize(
        "raw",
        [
            "nan",
            "NaN",
            "inf",
            "-inf",
            "Infinity",  # Python's float() accepts all of these
            "1_0",  # Python underscore separator
            " 1.0",
            "1.0 ",
            "1.0\n",  # Python strips surrounding whitespace
            "0x1p3",  # C strtod accepts hex floats
            "１２",  # non-ASCII digits
            "",
            "abc",
            "1,5",
            "--1",
            "1e",
            "e5",
            "1.2.3",
        ],
    )
    def test_rejects_everything_outside_it(self, raw: str) -> None:
        assert decode_float(raw) is None

    def test_absent_key_is_none(self) -> None:
        assert decode_float(None) is None


class TestPositiveFloat:
    @pytest.mark.parametrize("raw", ["0", "-0", "-1", "-0.5", "", None, "junk"])
    def test_non_positive_and_unusable_read_as_unset(self, raw: str | None) -> None:
        assert decode_positive_float(raw) is None

    def test_positive_passes_through(self) -> None:
        assert decode_positive_float("2.5") == 2.5


class TestBool:
    def test_round_trip(self) -> None:
        assert decode_bool(encode_bool(True)) is True
        assert decode_bool(encode_bool(False)) is False

    @pytest.mark.parametrize("raw", ["0", "", "true", "TRUE", "yes", "2", "-1", None, " 1"])
    def test_anything_but_exactly_one_is_false(self, raw: str | None) -> None:
        # A damaged flag must never read as "control relaxed".
        assert decode_bool(raw) is False
