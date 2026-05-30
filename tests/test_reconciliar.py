"""Tests para scraper/reconciliar.py (PR11).

Cubre el voto ponderado (sin DB) — la lógica de overrides temporales se
verifica a mano contra la DB en smoke (requiere spot_semantic_state poblado).
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRAPER_DIR = os.path.abspath(os.path.join(HERE, "..", "scraper"))
if SCRAPER_DIR not in sys.path:
    sys.path.insert(0, SCRAPER_DIR)

from reconciliar import (  # noqa: E402
    KEEP_EXISTING,
    TIE_MARGIN,
    _reconciliar_campo,
    _reconciliar_campo_full,
    _detectar_conflictos,
)


# ─── _reconciliar_campo: voto ponderado ───────────────────────────────


def test_majority_vote_three_true_beats_one_false():
    """3 fuentes con peso 0.8 dicen True, 1 fuente con peso 0.9 dice False.
    Mayoría ponderada (2.4 vs 0.9) → True."""
    records = {
        "park4night":    {"agua_potable": True},
        "campercontact": {"agua_potable": True},
        "areasac":       {"agua_potable": True},
        "campingcarpark":{"agua_potable": False},
    }
    credibility = {
        "park4night": 0.8, "campercontact": 0.8, "areasac": 0.8, "campingcarpark": 0.9
    }
    val, _src = _reconciliar_campo(records, "agua_potable", credibility)
    assert val is True


def test_single_high_credibility_beats_lower_majority_under_margin():
    """1 fuente muy fiable (peso 0.95) dice False, 2 fuentes débiles (0.6) True.
    Pesos: True=1.2, False=0.95. Margen = (1.2-0.95)/2.15 = 11.6% > 10% → gana True.
    Esto verifica que el margen importa, no solo el ranking."""
    records = {
        "wikicamps":  {"agua_potable": True},
        "osm":        {"agua_potable": True},
        "campercontact": {"agua_potable": False},
    }
    credibility = {"wikicamps": 0.6, "osm": 0.6, "campercontact": 0.95}
    val, _src = _reconciliar_campo(records, "agua_potable", credibility)
    assert val is True


def test_tie_returns_keep_existing():
    """Margen < TIE_MARGIN (10%) → no tocar (KEEP_EXISTING)."""
    records = {
        "park4night":    {"agua_potable": True},
        "campercontact": {"agua_potable": False},
    }
    # Pesos casi iguales → margen tiny
    credibility = {"park4night": 0.85, "campercontact": 0.86}
    val, src = _reconciliar_campo(records, "agua_potable", credibility)
    assert val is KEEP_EXISTING
    assert src is None


def test_witness_source_returned_is_highest_ranked():
    """El witness (fuente devuelta) es la mejor ranked de las que votaron por
    el ganador, para trazabilidad."""
    records = {
        "campingcarpark": {"agua_potable": True},  # alto ranking
        "campy":          {"agua_potable": True},  # bajo ranking
        "osm":            {"agua_potable": False},
    }
    credibility = {"campingcarpark": 0.92, "campy": 0.75, "osm": 0.6}
    val, src = _reconciliar_campo(records, "agua_potable", credibility)
    assert val is True
    # Ambas True, pero campingcarpark está antes en CREDIBILITY["agua_potable"]
    assert src == "campingcarpark"


def test_unknown_source_gets_default_05():
    """Una fuente que no está en source_credibility recibe peso 0.5."""
    records = {
        "park4night":      {"agua_potable": True},
        "fuente_imaginaria":{"agua_potable": False},
    }
    credibility = {"park4night": 0.95}  # fuente_imaginaria ausente
    val, _src = _reconciliar_campo(records, "agua_potable", credibility)
    # Pesos: True=0.95, False=0.5. Margen >10% → True
    assert val is True


def test_only_one_source_returns_that_value():
    """Sin contradicción, devuelve el único valor disponible."""
    records = {"park4night": {"agua_potable": False}}
    val, src = _reconciliar_campo(records, "agua_potable", {"park4night": 0.9})
    assert val is False
    assert src == "park4night"


def test_all_none_returns_none():
    records = {
        "park4night":    {"agua_potable": None},
        "campercontact": {"agua_potable": None},
    }
    val, src = _reconciliar_campo(records, "agua_potable", {"park4night": 0.9})
    assert val is None
    assert src is None


def test_non_voted_field_falls_back_to_rank_first():
    """Las descripciones no se votan — gana la primera fuente del rank."""
    records = {
        "areasac": {"descripcion_es": "Bonito spot al sur"},
        "park4night": {"descripcion_es": "Otra descripción francesa"},
    }
    # park4night está antes en CREDIBILITY["descripcion_es"] que areasac
    val, src = _reconciliar_campo(records, "descripcion_es", {})
    assert "park4night" == src


def test_numeric_weighted_vote():
    """Voto sobre num_plazas — sigue siendo exact-match (12 ≠ 13)."""
    records = {
        "campingcarpark": {"num_plazas": 50},
        "campercontact":  {"num_plazas": 50},
        "park4night":     {"num_plazas": 45},
    }
    credibility = {"campingcarpark": 0.92, "campercontact": 0.90, "park4night": 0.92}
    val, _src = _reconciliar_campo(records, "num_plazas", credibility)
    # 50 acumula 1.82, 45 acumula 0.92 → margen 33% → gana 50
    assert val == 50


# ─── _reconciliar_campo_full: procedencia/confianza (Sprint 0) ────────


def test_full_weighted_confidence_and_supporting():
    """Voto ponderado: confidence = cuota de peso del ganador; supporting_sources
    lista todas las fuentes que votaron el valor ganador; conflict=True si hay
    más de un valor distinto."""
    records = {
        "park4night":    {"agua_potable": True},
        "campercontact": {"agua_potable": True},
        "areasac":       {"agua_potable": False},
    }
    credibility = {"park4night": 0.8, "campercontact": 0.8, "areasac": 0.85}
    val, witness, supporting, confidence, margin, conflict = \
        _reconciliar_campo_full(records, "agua_potable", credibility)
    assert val is True
    # True acumula 1.6 sobre total 2.45 → confidence ≈ 0.653
    assert abs(confidence - (1.6 / 2.45)) < 1e-6
    assert set(supporting) == {"park4night", "campercontact"}
    assert conflict is True
    assert margin is not None


def test_full_no_conflict_when_all_agree():
    records = {
        "park4night":    {"agua_potable": True},
        "campercontact": {"agua_potable": True},
    }
    val, _w, supporting, confidence, _m, conflict = _reconciliar_campo_full(
        records, "agua_potable", {"park4night": 0.8, "campercontact": 0.8}
    )
    assert val is True
    assert conflict is False
    assert set(supporting) == {"park4night", "campercontact"}
    assert confidence == 1.0  # único valor → 100% del peso


def test_full_rankfirst_confidence_is_base_score_margin_none():
    """Rank-first (contacto): confidence = base_score de la fuente ganadora,
    margin = None, conflict refleja discrepancia."""
    records = {
        "areasac":    {"telefono": "+34 111"},
        "park4night": {"telefono": "+34 222"},
    }
    credibility = {"areasac": 0.85, "park4night": 0.92}
    val, witness, supporting, confidence, margin, conflict = \
        _reconciliar_campo_full(records, "telefono", credibility)
    # areasac va antes que park4night en CREDIBILITY["telefono"]
    assert witness == "areasac"
    assert val == "+34 111"
    assert confidence == 0.85
    assert margin is None
    assert conflict is True
    assert supporting == ["areasac"]


# ─── _detectar_conflictos ─────────────────────────────────────────────


def test_conflicto_detectado_cuando_fuentes_discrepan():
    records = {
        "park4night":    {"agua_potable": True, "gratuito": True},
        "campercontact": {"agua_potable": False, "gratuito": True},
    }
    cs = _detectar_conflictos(records)
    fields = [c["campo"] for c in cs]
    assert "agua_potable" in fields
    assert "gratuito" not in fields  # ambos True → no conflicto


def test_sin_conflicto_si_solo_una_fuente():
    records = {"park4night": {"agua_potable": True}}
    assert _detectar_conflictos(records) == []


# ─── Sanity: TIE_MARGIN está donde esperamos ──────────────────────────


def test_tie_margin_is_10pct():
    assert TIE_MARGIN == 0.10
