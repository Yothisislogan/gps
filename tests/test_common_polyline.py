"""Encoded-polyline codec tests, with the precision-6 trap called out."""

from __future__ import annotations

import pytest

from common.polyline import VALHALLA_PRECISION, decode, encode

MGA_TO_GRANADA = [(12.1415, -86.1682), (12.1000, -86.1000), (11.9344, -85.9560)]


def test_default_precision_is_valhallas():
    assert VALHALLA_PRECISION == 6


def test_round_trip_precision6():
    decoded = decode(encode(MGA_TO_GRANADA))
    assert decoded == pytest.approx(MGA_TO_GRANADA, abs=1e-6)


def test_round_trip_precision5():
    encoded = encode(MGA_TO_GRANADA, precision=5)
    assert decode(encoded, precision=5) == pytest.approx(MGA_TO_GRANADA, abs=1e-5)


def test_decoding_at_the_wrong_precision_is_off_by_ten():
    # The classic Valhalla bug: precision-6 shape read as precision-5 lands in
    # the Pacific.  Asserted so nobody "fixes" the default back to 5.
    wrong = decode(encode(MGA_TO_GRANADA), precision=5)
    assert wrong[0][0] == pytest.approx(MGA_TO_GRANADA[0][0] * 10, abs=1e-4)


def test_known_google_example_precision5():
    # Canonical example from the encoded-polyline spec.
    assert decode("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5) == pytest.approx(
        [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)], abs=1e-5
    )


def test_empty_and_single_point():
    assert decode("") == []
    assert encode([]) == ""
    assert decode(encode([(12.1415, -86.1682)])) == pytest.approx([(12.1415, -86.1682)], abs=1e-6)


def test_truncated_payload_returns_clean_prefix():
    encoded = encode(MGA_TO_GRANADA)
    assert len(decode(encoded[: len(encoded) // 2])) < len(MGA_TO_GRANADA)


def test_negative_and_zero_coordinates():
    coords = [(0.0, 0.0), (-12.5, 86.25), (12.5, -86.25)]
    assert decode(encode(coords)) == pytest.approx(coords, abs=1e-6)
