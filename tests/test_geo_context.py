"""Tests de las funciones puras del motor geoespacial OSM (Sprint 3)."""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRAPER_DIR = os.path.abspath(os.path.join(HERE, "..", "scraper"))
if SCRAPER_DIR not in sys.path:
    sys.path.insert(0, SCRAPER_DIR)

from geo_context import (  # noqa: E402
    _categorize,
    _nearest_by_category,
    _build_query,
    _haversine_km,
)


def test_categorize_known_tags():
    assert _categorize({"amenity": "drinking_water"}) == "drinking_water"
    assert _categorize({"amenity": "sanitary_dump_station"}) == "dump_station"
    assert _categorize({"shop": "supermarket"}) == "supermarket"
    assert _categorize({"tourism": "viewpoint"}) == "viewpoint"

def test_categorize_unknown_is_none():
    assert _categorize({"amenity": "bench"}) is None   # bench no es categoría
    assert _categorize({}) is None
    assert _categorize({"amenity": "restaurant"}) == "restaurant"  # sí lo es ahora

def test_haversine_zero_and_known():
    assert _haversine_km(40.0, -3.0, 40.0, -3.0) == 0.0
    # ~1 grado de latitud ≈ 111 km
    assert 110 < _haversine_km(40.0, -3.0, 41.0, -3.0) < 112

def test_nearest_picks_closest_per_category():
    spot_lat, spot_lon = 40.0, -3.0
    elements = [
        {"tags": {"amenity": "drinking_water"}, "lat": 40.001, "lon": -3.0},   # ~111 m
        {"tags": {"amenity": "drinking_water"}, "lat": 40.02, "lon": -3.0},    # ~2.2 km
        {"tags": {"shop": "supermarket"}, "center": {"lat": 40.005, "lon": -3.0}},  # ~555 m
        {"tags": {"amenity": "bench"}, "lat": 40.0, "lon": -3.0},              # ignorado
    ]
    near = _nearest_by_category(elements, spot_lat, spot_lon)
    assert "drinking_water" in near and "supermarket" in near
    assert "bench" not in near
    # Toma el agua más cercana (~0.11 km), no la lejana
    assert near["drinking_water"] < 0.2

def test_build_query_contains_filters():
    q = _build_query(40.0, -3.0, 3000)
    assert "around:3000,40.0,-3.0" in q
    assert "amenity=drinking_water" in q
    assert "shop=supermarket" in q
    assert "out center tags" in q
