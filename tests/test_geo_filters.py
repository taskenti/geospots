"""Canal B — filtros de proximidad en la búsqueda semántica (FILTER_MAP geo)."""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from enrichment.embedding_generator import FILTER_MAP, _GEO_FILTER_DEFS  # noqa: E402


def test_all_geo_filters_registered():
    for fk, _col, _jkey in _GEO_FILTER_DEFS:
        assert fk in FILTER_MAP, f"{fk} no está en FILTER_MAP"
    assert len(_GEO_FILTER_DEFS) == 13


def test_osm_filter_template_formats_to_sql():
    template, cast = FILTER_MAP["max_dist_super_km"]
    assert template.format(5) == "(sg.nearby_osm->>'supermarket')::float <= $5"
    assert cast is float


def test_spots_filter_template_uses_nearby_spots():
    template, cast = FILTER_MAP["max_dist_area_ac_km"]
    assert template.format(9) == "(sg.nearby_spots->>'area_ac')::float <= $9"
    assert cast is float


def test_no_brace_collisions_in_geo_templates():
    # Cada template geo debe tener exactamente un placeholder ${} → un solo $N.
    for fk, _c, _j in _GEO_FILTER_DEFS:
        template, _ = FILTER_MAP[fk]
        assert template.count("{}") == 1
        assert template.format(3).endswith("<= $3")
