"""Tests del parser de respuestas Gemini (offline)."""

from __future__ import annotations

import json

import pytest

from enrichment.gemini_response_parser import (
    ParseError,
    parse_enrichment_response,
)


# ─── Casos felices ────────────────────────────────────────────────


def test_parse_minimal_valid():
    text = json.dumps({
        "claims": [],
        "summary": None,
        "tags": [],
        "best_for": [],
        "best_season": None,
        "avoid_season": None,
    })
    r = parse_enrichment_response(text)
    assert r.claims == []
    assert r.tags == []
    assert r.summary is None
    assert r.errors == []


def test_parse_full_response_v4():
    """v4 native format: single English summary."""
    text = json.dumps({
        "claims": [
            {"signal": "quietness", "value": 0.8, "confidence": 0.9, "review_id": 100, "excerpt": "muy tranquilo"},
            {"signal": "sea_view", "value": True, "confidence": 0.95, "review_id": "description", "excerpt": "face à la mer"},
            {"signal": "noise_source", "value": "highway", "confidence": 0.85, "review_id": 101, "excerpt": "ruido de autopista"},
            {"signal": "parking_capacity", "value": "medium", "confidence": 0.7, "review_id": "description", "excerpt": "20 plazas"},
        ],
        "summary": "Seafront aire.",
        "tags": ["sea", "free"],
        "best_for": ["couples"],
        "best_season": "spring",
        "avoid_season": "august",
    })
    r = parse_enrichment_response(text)
    assert len(r.claims) == 4
    assert r.claims[0].signal == "quietness"
    assert r.claims[0].value == 0.8
    assert r.claims[0].review_id == 100
    assert r.claims[1].value is True
    assert r.claims[1].review_id is None  # "description" → None
    assert r.claims[2].value == "highway"
    assert r.claims[3].value == "medium"
    assert r.summary == "Seafront aire."
    # v4 compat shims: summary_es deprecated (None), summary_en maps to summary
    assert r.summary_es is None
    assert r.summary_en == "Seafront aire."
    assert r.tags == ["sea", "free"]
    assert r.errors == []


def test_parse_legacy_v3_response_falls_back_to_summary_en():
    """Backward compat: a model still emitting summary_en gets read into .summary."""
    text = json.dumps({
        "claims": [],
        "summary_en": "Quiet spot near the sea.",
        "summary_es": "Spot tranquilo junto al mar.",  # ignored in v4
    })
    r = parse_enrichment_response(text)
    assert r.summary == "Quiet spot near the sea."
    # Shim: summary_es returns None always (v4 deprecation)
    assert r.summary_es is None


def test_parse_strips_markdown_fence():
    text = "```json\n" + json.dumps({"claims": []}) + "\n```"
    r = parse_enrichment_response(text)
    assert r.claims == []


def test_parse_strips_bare_fence():
    text = "```\n" + json.dumps({"claims": []}) + "\n```"
    r = parse_enrichment_response(text)
    assert r.claims == []


# ─── Coercion / saneamiento ────────────────────────────────────────


def test_clamps_numeric_out_of_range():
    text = json.dumps({"claims": [
        {"signal": "quietness", "value": 1.5, "confidence": 0.9, "review_id": 1, "excerpt": "x"}
    ]})
    r = parse_enrichment_response(text)
    assert len(r.claims) == 1
    assert r.claims[0].value == 1.0
    assert any("fuera de rango" in e for e in r.errors)


def test_coerce_numeric_from_string():
    text = json.dumps({"claims": [
        {"signal": "quietness", "value": "0.7", "confidence": 0.8, "review_id": 1, "excerpt": "x"}
    ]})
    r = parse_enrichment_response(text)
    assert r.claims[0].value == 0.7


def test_coerce_bool_from_string():
    text = json.dumps({"claims": [
        {"signal": "sea_view", "value": "true", "confidence": 0.9, "review_id": 1, "excerpt": "x"},
        {"signal": "mountain_view", "value": "no", "confidence": 0.8, "review_id": 1, "excerpt": "y"},
    ]})
    r = parse_enrichment_response(text)
    assert r.claims[0].value is True
    assert r.claims[1].value is False


def test_review_id_string_numeric():
    text = json.dumps({"claims": [
        {"signal": "quietness", "value": 0.5, "confidence": 0.8, "review_id": "42", "excerpt": "x"}
    ]})
    r = parse_enrichment_response(text)
    assert r.claims[0].review_id == 42


def test_review_id_description_variants():
    for variant in ("description", "Description", "desc", "DESCRIPTIONS"):
        text = json.dumps({"claims": [
            {"signal": "quietness", "value": 0.5, "confidence": 0.8, "review_id": variant, "excerpt": "x"}
        ]})
        r = parse_enrichment_response(text)
        assert r.claims[0].review_id is None, f"falló para {variant!r}"


def test_noise_source_unknown_becomes_other():
    text = json.dumps({"claims": [
        {"signal": "noise_source", "value": "weird_unknown", "confidence": 0.7, "review_id": 1, "excerpt": "x"}
    ]})
    r = parse_enrichment_response(text)
    assert r.claims[0].value == "other"
    assert any("fuera de vocabulario" in e for e in r.errors)


def test_parking_capacity_unknown_discarded():
    text = json.dumps({"claims": [
        {"signal": "parking_capacity", "value": "huge", "confidence": 0.7, "review_id": 1, "excerpt": "x"}
    ]})
    r = parse_enrichment_response(text)
    assert len(r.claims) == 0
    assert any("parking_capacity" in e for e in r.errors)


def test_unknown_signal_discarded():
    text = json.dumps({"claims": [
        {"signal": "invented_signal", "value": 0.5, "confidence": 0.9, "review_id": 1, "excerpt": "x"},
        {"signal": "quietness", "value": 0.5, "confidence": 0.9, "review_id": 1, "excerpt": "y"},
    ]})
    r = parse_enrichment_response(text)
    assert len(r.claims) == 1
    assert r.claims[0].signal == "quietness"
    assert any("invented_signal" in e for e in r.errors)


def test_excerpt_truncated():
    long_excerpt = "x" * 1000
    text = json.dumps({"claims": [
        {"signal": "quietness", "value": 0.5, "confidence": 0.9, "review_id": 1, "excerpt": long_excerpt}
    ]})
    r = parse_enrichment_response(text)
    assert len(r.claims[0].excerpt) == 500


def test_tags_lowercase_and_limited():
    text = json.dumps({
        "claims": [],
        "tags": ["MAR", "Surf", "  bonito  ", *[f"tag{i}" for i in range(20)]],
    })
    r = parse_enrichment_response(text)
    assert "mar" in r.tags
    assert "surf" in r.tags
    assert "bonito" in r.tags
    assert len(r.tags) <= 12


def test_summary_truncated():
    text = json.dumps({
        "claims": [],
        "summary": "x" * 5000,
    })
    r = parse_enrichment_response(text)
    assert len(r.summary) == 1000


# ─── Errores y resiliencia ────────────────────────────────────────


def test_empty_response_raises():
    with pytest.raises(ParseError):
        parse_enrichment_response("")


def test_malformed_json_raises():
    with pytest.raises(ParseError):
        parse_enrichment_response("{not valid json")


def test_non_object_root_raises():
    with pytest.raises(ParseError):
        parse_enrichment_response("[1, 2, 3]")


def test_claims_not_list_does_not_crash():
    text = json.dumps({"claims": "no soy una lista"})
    r = parse_enrichment_response(text)
    assert r.claims == []
    assert any("claims" in e for e in r.errors)


def test_claim_not_object_skipped():
    text = json.dumps({"claims": [
        "not an object",
        {"signal": "quietness", "value": 0.5, "confidence": 0.9, "review_id": 1, "excerpt": "ok"},
    ]})
    r = parse_enrichment_response(text)
    assert len(r.claims) == 1
    assert r.claims[0].signal == "quietness"


def test_claim_missing_value_skipped():
    text = json.dumps({"claims": [
        {"signal": "quietness", "confidence": 0.9, "review_id": 1, "excerpt": "x"},
        {"signal": "safety", "value": 0.5, "confidence": 0.9, "review_id": 1, "excerpt": "y"},
    ]})
    r = parse_enrichment_response(text)
    assert len(r.claims) == 1
    assert r.claims[0].signal == "safety"


def test_confidence_clamped_and_defaulted():
    text = json.dumps({"claims": [
        {"signal": "quietness", "value": 0.5, "confidence": 2.0, "review_id": 1, "excerpt": "x"},
        {"signal": "safety", "value": 0.5, "confidence": "abc", "review_id": 1, "excerpt": "y"},
    ]})
    r = parse_enrichment_response(text)
    assert r.claims[0].confidence == 1.0
    assert r.claims[1].confidence == 0.5  # default tras fallo de parseo


def test_optional_fields_missing():
    text = json.dumps({"claims": []})
    r = parse_enrichment_response(text)
    assert r.summary is None
    assert r.summary_es is None  # compat shim
    assert r.tags == []
    assert r.best_for == []
