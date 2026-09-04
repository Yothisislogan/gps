"""Encoded-polyline codec.

Valhalla returns route shapes as **precision-6** encoded polylines, not the
precision-5 flavour Google popularised.  Decoding at the wrong precision yields
a route squashed into a tenth of its extent near null island, which is a
uniquely confusing bug — so precision is an explicit argument everywhere and
:data:`VALHALLA_PRECISION` is the constant to pass.
"""

from __future__ import annotations

__all__ = ["VALHALLA_PRECISION", "decode", "encode"]

VALHALLA_PRECISION = 6


def decode(encoded: str, precision: int = VALHALLA_PRECISION) -> list[tuple[float, float]]:
    """Decode to ``[(lat, lon), ...]``."""
    factor = float(10**precision)
    coords: list[tuple[float, float]] = []
    index = lat = lon = 0
    length = len(encoded)

    while index < length:
        for is_lat in (True, False):
            shift = result = 0
            while True:
                if index >= length:
                    return coords  # truncated payload: return what decoded cleanly
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if is_lat:
                lat += delta
            else:
                lon += delta
        coords.append((lat / factor, lon / factor))
    return coords


def encode(coords: list[tuple[float, float]], precision: int = VALHALLA_PRECISION) -> str:
    """Encode ``[(lat, lon), ...]``; inverse of :func:`decode`."""
    factor = float(10**precision)
    out: list[str] = []
    prev_lat = prev_lon = 0

    for lat, lon in coords:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            value = ~(delta << 1) if delta < 0 else (delta << 1)
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)
