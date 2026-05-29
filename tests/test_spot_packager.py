"""Tests para enrichment/spot_packager.py."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from enrichment.spot_packager import (
    compute_richness,
    estimate_tokens,
    has_rich_description,
    select_reviews_for_prompt,
    should_enrich,
    summary_instruction_for,
    temporal_weight,
)
from enrichment.prompts import build_spot_user_prompt


def _days_ago(n: int) -> date:
    return (datetime.now(timezone.utc) - timedelta(days=n)).date()


# ─── temporal_weight ─────────────────────────────────────────────────


def test_temporal_weight_recent():
    assert temporal_weight(_days_ago(30)) == 1.0


def test_temporal_weight_one_year():
    assert temporal_weight(_days_ago(400)) == 0.8


def test_temporal_weight_two_years():
    assert temporal_weight(_days_ago(800)) == 0.5


def test_temporal_weight_four_years():
    assert temporal_weight(_days_ago(1500)) == 0.3


def test_temporal_weight_very_old():
    assert temporal_weight(_days_ago(3000)) == 0.1


def test_temporal_weight_none():
    assert temporal_weight(None) == 0.3


def test_temporal_weight_datetime_naive():
    # naive datetime asume UTC, no debe crashear
    dt = datetime.now() - timedelta(days=10)
    assert temporal_weight(dt) == 1.0


def test_temporal_weight_datetime_aware():
    dt = datetime.now(timezone.utc) - timedelta(days=10)
    assert temporal_weight(dt) == 1.0


def test_temporal_weight_future_date():
    # Fechas en el futuro (raras pero ocurren) → peso máximo
    assert temporal_weight(_days_ago(-5)) == 1.0


# ─── estimate_tokens ─────────────────────────────────────────────────


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_tokens_approx():
    # ~4 chars por token
    assert estimate_tokens("a" * 40) == 10


def test_estimate_tokens_minimum_one():
    # 3 chars deben dar al menos 1 token
    assert estimate_tokens("hi!") >= 1


# ─── select_reviews_for_prompt ──────────────────────────────────────


def _r(id_, fecha, texto, rating=None):
    return {"id": id_, "fecha": fecha, "texto_limpio": texto, "rating": rating, "source": "p4n"}


def test_select_recent_beats_old():
    recent = _r(1, _days_ago(30), "tranquilo y bonito" * 5, rating=4)
    old = _r(2, _days_ago(2000), "ruidoso y feo" * 5, rating=2)
    selected = select_reviews_for_prompt([old, recent], max_tokens=10_000)
    assert selected[0]["id"] == 1


def test_select_respects_budget():
    # texto informativo (~300 chars); "x"*300 lo descarta review_cleaner por repetitivo
    long_text = ("sitio tranquilo con buenas vistas al mar y agua potable cerca " * 5)[:300]
    reviews = [_r(i, _days_ago(30 + i), long_text, rating=4) for i in range(20)]
    selected = select_reviews_for_prompt(reviews, max_tokens=300)
    # 300 tokens / (~75 tokens por review + 20 overhead) → muy pocas
    assert 0 < len(selected) < 20


def test_select_filters_empty_text():
    reviews = [
        _r(1, _days_ago(30), "", rating=4),
        _r(2, _days_ago(30), "ab", rating=4),  # menos de 4 chars
        _r(3, _days_ago(30), "review con contenido suficiente", rating=4),
    ]
    selected = select_reviews_for_prompt(reviews, max_tokens=10_000)
    ids = {r["id"] for r in selected}
    assert ids == {3}


def test_select_truncates_long_text():
    long_text = "palabra " * 200  # ~1600 chars
    reviews = [_r(1, _days_ago(30), long_text, rating=4)]
    selected = select_reviews_for_prompt(reviews, max_tokens=10_000, text_char_limit=200)
    assert len(selected[0]["texto_limpio"]) <= 200


def test_select_forces_at_least_one_when_budget_tiny():
    # Budget ridículo: aún así debe devolver 1 review (truncada) para no descartar el spot
    long_text = ("sitio tranquilo con buenas vistas y agua potable cerca del pueblo " * 6)[:400]
    reviews = [_r(1, _days_ago(30), long_text, rating=4)]
    selected = select_reviews_for_prompt(reviews, max_tokens=5)
    assert len(selected) == 1


def test_select_handles_none_dates():
    # Reviews sin fecha tienen peso 0.3, deberían entrar si hay budget
    reviews = [_r(1, None, "review sin fecha pero util", rating=4)]
    selected = select_reviews_for_prompt(reviews, max_tokens=10_000)
    assert len(selected) == 1


def test_select_stable_order_by_weight():
    # Misma fecha → orden estable por rating, luego longitud
    fecha = _days_ago(100)
    reviews = [
        _r(1, fecha, "corto", rating=3),
        _r(2, fecha, "texto un poco más largo aquí", rating=5),
        _r(3, fecha, "medio largo este", rating=4),
    ]
    selected = select_reviews_for_prompt(reviews, max_tokens=10_000)
    assert selected[0]["id"] == 2  # rating 5 gana


def test_select_uses_texto_fallback():
    # Si no hay texto_limpio, debe usar texto o texto_original
    reviews = [{"id": 9, "fecha": _days_ago(30), "texto": "fallback al texto crudo", "source": "x"}]
    selected = select_reviews_for_prompt(reviews, max_tokens=10_000)
    assert len(selected) == 1
    assert "fallback" in selected[0]["texto_limpio"]


def test_select_stratifies_across_months():
    # Spot muy transitado: 60 reviews repartidas en 24 meses (2 por mes).
    # Con budget que sólo deja entrar ~24 reviews, la selección naive (top recencia)
    # cubriría ~12 meses; la estratificación round-robin debe estirar la muestra
    # para abarcar todo el rango (>= ~1 año cubierto desde ambos extremos).
    # Cada review con texto claramente distinto para que dedup (fuzzy) no las colapse.
    cuerpos = [
        "sitio tranquilo con buenas vistas al mar abierto",
        "parking amplio junto al rio con sombra agradable",
        "zona de montana fria de noche pero muy silenciosa",
        "area urbana ruidosa de dia comoda para servicios",
        "playa cercana ideal para banarse en verano caluroso",
        "bosque con senderos y fauna pajaros al amanecer",
    ]
    reviews = []
    rid = 0
    for month in range(24):           # 0 = más reciente, 23 = ~2 años atrás
        for k in range(2):
            rid += 1
            cuerpo = cuerpos[rid % len(cuerpos)]
            reviews.append(_r(rid, _days_ago(15 + month * 30),
                              f"{cuerpo} visita {rid} en el mes {month}", rating=4))
    # Budget para ~20-24 reviews (cada una ~37 tokens incl. overhead).
    selected = select_reviews_for_prompt(reviews, max_tokens=900, apply_dedup=False)
    assert len(selected) >= 12
    fechas = [r["fecha"] for r in selected]
    span_days = (max(fechas) - min(fechas)).days
    # Naive recency cubriría ~ len(selected)/2 meses (~5-6 meses con 12 reviews).
    # La estratificación debe estirar la cobertura a > 1 año.
    assert span_days > 365, f"cobertura temporal insuficiente: {span_days} días"


def test_select_single_month_preserves_weight_order():
    # Un solo mes (1 bucket) → la estratificación no debe alterar el orden por peso.
    fecha = _days_ago(20)
    reviews = [
        _r(1, fecha, "texto informativo de prueba uno", rating=3),
        _r(2, fecha, "texto informativo de prueba dos", rating=5),
        _r(3, fecha, "texto informativo de prueba tres", rating=4),
    ]
    selected = select_reviews_for_prompt(reviews, max_tokens=10_000)
    assert selected[0]["id"] == 2  # rating 5 gana, igual que sin estratificar


# ─── has_rich_description / should_enrich ───────────────────────────


def test_has_rich_description_true():
    spot = {"descripcion_es": "x" * 300}
    assert has_rich_description(spot) is True


def test_has_rich_description_concat():
    spot = {"descripcion_es": "x" * 100, "descripcion_en": "y" * 150}
    assert has_rich_description(spot) is True


def test_has_rich_description_false():
    spot = {"descripcion_es": "muy corto"}
    assert has_rich_description(spot) is False


def test_should_enrich_enough_reviews():
    spot = {}
    decision, reason = should_enrich(spot, n_reviews=5)
    assert decision is True
    assert "5" in reason


def test_should_enrich_rich_desc_no_reviews():
    spot = {"descripcion_es": "x" * 300}
    decision, reason = should_enrich(spot, n_reviews=0)
    assert decision is True
    assert reason == "rich_description_only"


def test_should_enrich_insufficient():
    spot = {"descripcion_es": "corto"}
    decision, reason = should_enrich(spot, n_reviews=1)
    assert decision is False


# ─── build_spot_user_prompt ─────────────────────────────────────────


def test_build_prompt_basic_structure():
    spot = {
        "id": 42,
        "canonical_name": "Aire de Belharra",
        "tipo": "aire_municipal",
        "country_iso": "FR",
        "lat": 43.39,
        "lon": -1.61,
        "fuentes": ["park4night", "campercontact"],
        "descripcion_fr": "Parking face à la mer, gratuit.",
    }
    reviews = [
        {"id": 100, "fecha": _days_ago(60), "texto_limpio": "Muy tranquilo, vistas al mar.", "source": "p4n", "rating": 5},
        {"id": 101, "fecha": _days_ago(120), "texto_limpio": "Lleno en agosto.", "source": "cc", "rating": 3},
    ]
    prompt = build_spot_user_prompt(spot, reviews)
    assert "SPOT id=42" in prompt
    assert "Aire de Belharra" in prompt
    assert "FR" in prompt
    assert "review_id=100" in prompt
    assert "review_id=101" in prompt
    assert "park4night, campercontact" in prompt
    assert "[FR] Parking face" in prompt


def test_build_prompt_no_reviews_only_descriptions():
    spot = {
        "id": 7,
        "canonical_name": "Test Spot",
        "tipo": "otro",
        "country_iso": "ES",
        "lat": 40.0,
        "lon": -3.0,
        "fuentes": [],
        "descripcion_es": "Aparcamiento con vistas.",
    }
    prompt = build_spot_user_prompt(spot, [])
    # v4: prompt headers are now in English
    assert "no reviews available" in prompt
    assert "[ES] Aparcamiento" in prompt


def test_build_prompt_handles_missing_fields():
    spot = {
        "id": 1,
        "canonical_name": None,
        "tipo": None,
        "country_iso": None,
        "lat": 0.0,
        "lon": 0.0,
        "fuentes": None,
    }
    prompt = build_spot_user_prompt(spot, [])
    assert "SPOT id=1" in prompt
    # v4: tipo default fallback is now "other" (English) when None
    assert "other" in prompt


def test_build_prompt_descriptions_native_plus_english_only():
    # País ES → solo [ES] + [EN]; [FR]/[DE]/[IT] redundantes se descartan.
    spot = {
        "id": 5, "canonical_name": "Test", "tipo": "camping",
        "country_iso": "ES", "lat": 40.0, "lon": -3.0, "fuentes": ["p4n"],
        "descripcion_es": "Camping junto al mar mediterraneo.",
        "descripcion_en": "Seaside campsite.",
        "descripcion_fr": "Camping au bord de la mer.",
        "descripcion_de": "Campingplatz am Meer.",
        "descripcion_it": "Campeggio sul mare.",
    }
    prompt = build_spot_user_prompt(spot, [])
    assert "[ES] Camping junto" in prompt
    assert "[EN] Seaside" in prompt
    assert "[FR]" not in prompt
    assert "[DE]" not in prompt
    assert "[IT]" not in prompt


def test_build_prompt_descriptions_fallback_when_no_native_no_english():
    # País NO (sin columna nativa) y sin EN → usa la primera descripción disponible.
    spot = {
        "id": 6, "canonical_name": "Fjord Spot", "tipo": "otro",
        "country_iso": "NO", "lat": 60.0, "lon": 5.0, "fuentes": ["p4n"],
        "descripcion_de": "Stellplatz am Fjord.",
    }
    prompt = build_spot_user_prompt(spot, [])
    assert "[DE] Stellplatz" in prompt


def test_build_prompt_emits_subtipo_location_rating():
    # Certifica que subtipo, municipio/region, master_rating y total_reviews llegan.
    spot = {
        "id": 9, "canonical_name": "Camping X", "tipo": "camping",
        "subtipo": "glamping", "country_iso": "ES",
        "region": "Andalucia", "municipio": "Tarifa",
        "lat": 36.0, "lon": -5.6, "fuentes": ["p4n"],
        "master_rating": 4.3, "total_reviews": 128,
    }
    prompt = build_spot_user_prompt(spot, [])
    assert "camping / glamping" in prompt
    assert "Tarifa, Andalucia" in prompt
    assert "4.3/5" in prompt
    assert "128 total reviews" in prompt


# ─── compute_richness (v4d) ──────────────────────────────────────────


def test_richness_minimal_spot():
    """Spot vacío: minimal."""
    spot = {"fuentes": []}
    score, level = compute_richness(spot, [])
    assert score == 0.0
    assert level == "minimal"


def test_richness_simple_spot():
    """3 reviews + 2 servicios + 1 fuente: simple."""
    spot = {
        "fuentes": ["park4night"],
        "agua_potable": True,
        "gratuito": True,
    }
    reviews = [{"id": i} for i in range(3)]
    score, level = compute_richness(spot, reviews)
    assert level == "simple"
    assert 0.10 <= score < 0.30


def test_richness_rich_camping():
    """Camping: 20 reviews + 14 servicios + 2 descripciones + 2 fuentes → rich."""
    spot = {
        "fuentes": ["campercontact", "park4night"],
        "gratuito": False, "precio_aprox": 25.0, "precio_info": "min 20 max 30",
        "agua_potable": True, "electricidad": True,
        "vaciado_grises": True, "vaciado_negras": True,
        "ducha": True, "wifi": True, "wc_publico": True,
        "perros": True, "iluminacion": True, "seguridad": True,
        "num_plazas": 40, "altura_max_m": 3.0,
        "descripcion_en": "Camping with full services",
        "descripcion_de": "Campingplatz mit allen Diensten",
    }
    reviews = [{"id": i} for i in range(20)]
    score, level = compute_richness(spot, reviews)
    assert level in ("rich", "very_rich")
    assert score >= 0.70


def test_richness_very_rich_camping():
    """Camping enorme: 30 reviews + 18 servicios + 4 idiomas + 3 fuentes."""
    spot = {
        "fuentes": ["campercontact", "park4night", "caramaps"],
        "gratuito": False, "precio_aprox": 25.0, "precio_info": "X",
        "agua_potable": True, "vaciado_grises": True, "vaciado_negras": True,
        "electricidad": True, "ducha": True, "wifi": True, "wc_publico": True,
        "perros": True, "iluminacion": True, "seguridad": True,
        "reserva_req": True, "num_plazas": 40, "altura_max_m": 3.0,
        "temporada_apertura": "all year", "acceso_grandes": True,
        "web": "https://x.com", "telefono": "+34123",
        "descripcion_en": "x", "descripcion_de": "x",
        "descripcion_fr": "x", "descripcion_it": "x",
    }
    reviews = [{"id": i} for i in range(30)]
    score, level = compute_richness(spot, reviews)
    assert level == "very_rich"
    assert score >= 0.85


def test_summary_instruction_lengths_match_level():
    """Las instrucciones deben mencionar longitudes coherentes con el level."""
    inst_min = summary_instruction_for("minimal")
    inst_simple = summary_instruction_for("simple")
    inst_rich = summary_instruction_for("rich")
    inst_vr = summary_instruction_for("very_rich")

    assert "1-2" in inst_min
    assert "2-3" in inst_simple
    assert "5-7" in inst_rich
    assert "6-8" in inst_vr
    # las largas deben tener anti-marketing reminder
    assert "marketing" in inst_rich.lower() or "factual" in inst_rich.lower()


def test_build_prompt_includes_summary_instruction():
    """v4d: el prompt debe incluir SUMMARY_INSTRUCTION."""
    spot = {
        "id": 1, "canonical_name": "Test", "tipo": "camping",
        "country_iso": "ES", "lat": 40.0, "lon": -3.0, "fuentes": ["park4night"],
        "agua_potable": True, "electricidad": True,
    }
    reviews = [{"id": i, "texto": f"review {i} text long enough", "fecha": _days_ago(30),
                "rating": 4, "source": "p4n"} for i in range(5)]
    selected = select_reviews_for_prompt(reviews)
    prompt = build_spot_user_prompt(spot, selected)
    assert "SUMMARY_RICHNESS:" in prompt
    assert "SUMMARY_INSTRUCTION:" in prompt
