"""Shared pytest fixtures.

Rules of the house: unit tests never touch the network, a database, or Docker.
Anything that needs a live service is marked ``@pytest.mark.docker`` or
``@pytest.mark.network`` and skipped unless the marker is selected explicitly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Read ``tests/fixtures/<name>``; JSON is parsed, anything else returned raw."""
    path = FIXTURES / name
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix in {".json", ".geojson"} else text


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch):
    """Keep tests off the developer's ``.env`` and off real service hostnames."""
    monkeypatch.setenv("NICANAV_ENV_FILE", str(FIXTURES / "empty.env"))
    for key in list(os.environ):
        if key.startswith("NICANAV_") and key != "NICANAV_ENV_FILE":
            monkeypatch.delenv(key, raising=False)
    from common.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def mga() -> tuple[float, float]:
    """Augusto C. Sandino International Airport, the centre of the circle."""
    return (12.1415, -86.1682)


@pytest.fixture
def sample_gazetteer() -> list[dict[str, Any]]:
    """A handful of real Managua landmarks, in the shape the geocoder expects.

    Coordinates are approximate to a few hundred metres — good enough for the
    unit tests, which assert on offsets and bearings rather than on absolute
    positions.
    """
    return [
        {
            "id": "gz-rotonda-gueguense",
            "name": "Rotonda El Güegüense",
            "name_alt": ["Rotonda Plaza España", "El Güegüense"],
            "kind": "rotonda",
            "lat": 12.1352,
            "lon": -86.2807,
            "city": "Managua",
            "former": False,
            "popularity": 0.9,
        },
        {
            "id": "gz-rotonda-centroamerica",
            "name": "Rotonda Centroamérica",
            "name_alt": ["Rotonda Centro América"],
            "kind": "rotonda",
            "lat": 12.1094,
            "lon": -86.2545,
            "city": "Managua",
            "former": False,
            "popularity": 0.95,
        },
        {
            "id": "gz-metrocentro",
            "name": "Metrocentro",
            "name_alt": ["Centro Comercial Metrocentro", "Rotonda Metrocentro"],
            "kind": "centro_comercial",
            "lat": 12.1246,
            "lon": -86.2686,
            "city": "Managua",
            "former": False,
            "popularity": 1.0,
        },
        {
            "id": "gz-cine-cabrera",
            "name": "Cine Cabrera",
            "name_alt": ["donde fue el Cine Cabrera"],
            "kind": "edificio",
            "lat": 12.1543,
            "lon": -86.2733,
            "city": "Managua",
            "former": True,
            "era": "hasta 1972",
            "popularity": 0.4,
        },
        {
            "id": "gz-parque-central-granada",
            "name": "Parque Central de Granada",
            "name_alt": ["Parque Colón"],
            "kind": "parque",
            "lat": 11.9299,
            "lon": -85.9560,
            "city": "Granada",
            "former": False,
            "popularity": 0.8,
        },
    ]


@pytest.fixture
def sample_pois() -> list[dict[str, Any]]:
    """Minimal POI rows spanning the sources the conflator has to reconcile."""
    return [
        {
            "source": "osm",
            "source_id": "node/1",
            "name": "Restaurante El Zaguán",
            "category": "restaurante",
            "lat": 11.9302,
            "lon": -85.9553,
            "phone": None,
            "facebook": None,
        },
        {
            "source": "overture",
            "source_id": "08f2ab",
            "name": "El Zaguan",
            "category": "restaurante",
            "lat": 11.9303,
            "lon": -85.9552,
            "phone": "+50525522522",
            "facebook": "https://facebook.com/elzaguan",
        },
        {
            "source": "overture",
            "source_id": "08f2ac",
            "name": "Fritanga La Fe",
            "category": "fritanga",
            "lat": 12.1200,
            "lon": -86.2700,
            "phone": None,
            "facebook": None,
        },
    ]
