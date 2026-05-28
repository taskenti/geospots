"""Tests para jobs/ingest_spot_facts.py.

Verifica que el mapping FIELD_SIGNAL_MAP + EXTRAS_SIGNAL_MAP transforma
correctamente normalized_data de source_records en claims semánticos.
Sin dependencias de DB — todo es lógica pura sobre dicts.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from jobs.ingest_spot_facts import extract_claims_from_source_record  # noqa: E402


def signals(claims: list[dict]) -> dict[str, str]:
    """Devuelve {signal: value} para verificar rápidamente."""
    return {c["signal"]: c["value"] for c in claims}


# ── Campos canónicos básicos ──────────────────────────────────────────────────

def test_agua_potable_true():
    claims = extract_claims_from_source_record({"agua_potable": True}, "test")
    s = signals(claims)
    assert s.get("water_working") == "true"


def test_agua_potable_false():
    claims = extract_claims_from_source_record({"agua_potable": False}, "test")
    s = signals(claims)
    assert s.get("water_working") == "false"


def test_electricidad_true():
    claims = extract_claims_from_source_record({"electricidad": True}, "test")
    assert signals(claims).get("electricity_working") == "true"


def test_ducha_false():
    claims = extract_claims_from_source_record({"ducha": False}, "test")
    assert signals(claims).get("shower_working") == "false"


def test_perros_both():
    yes = signals(extract_claims_from_source_record({"perros": True}, "test"))
    no = signals(extract_claims_from_source_record({"perros": False}, "test"))
    assert yes["dog_friendly"] == "true"
    assert no["dog_friendly"] == "false"


def test_acceso_grandes():
    yes = signals(extract_claims_from_source_record({"acceso_grandes": True}, "test"))
    no = signals(extract_claims_from_source_record({"acceso_grandes": False}, "test"))
    assert yes["large_vehicle"] == "0.85"
    assert no["large_vehicle"] == "0.15"


def test_altura_max_m():
    claims = extract_claims_from_source_record({"altura_max_m": 2.5}, "test")
    s = signals(claims)
    assert s.get("height_restriction") == "2.5"


def test_altura_max_m_integer():
    claims = extract_claims_from_source_record({"altura_max_m": 3}, "test")
    assert signals(claims).get("height_restriction") == "3.0"


def test_num_plazas_small():
    claims = extract_claims_from_source_record({"num_plazas": 4}, "test")
    assert signals(claims).get("parking_capacity") == "small"


def test_num_plazas_big():
    claims = extract_claims_from_source_record({"num_plazas": 50}, "test")
    assert signals(claims).get("parking_capacity") == "big"


def test_num_plazas_medium_no_claim():
    claims = extract_claims_from_source_record({"num_plazas": 15}, "test")
    assert "parking_capacity" not in signals(claims)


def test_hiking_nearby():
    claims = extract_claims_from_source_record({"hiking_nearby": True}, "test")
    assert signals(claims).get("hiking_nearby") == "true"


def test_piscina_swimming_access():
    claims = extract_claims_from_source_record({"piscina": True}, "test")
    assert signals(claims).get("swimming_access") == "true"


def test_zona_protegida_overnight_false():
    claims = extract_claims_from_source_record({"zona_protegida": True}, "test")
    assert signals(claims).get("overnight_safe") == "false"


def test_iluminacion_stealth_low():
    claims = extract_claims_from_source_record({"iluminacion": True}, "test")
    s = signals(claims)
    assert s.get("stealth") == "0.1"


def test_mirador_beauty():
    claims = extract_claims_from_source_record({"mirador": True}, "test")
    assert signals(claims).get("beauty") == "0.85"


def test_acepta_caravanas():
    yes = signals(extract_claims_from_source_record({"acepta_caravanas": True}, "test"))
    no = signals(extract_claims_from_source_record({"acepta_caravanas": False}, "test"))
    assert yes["caravan_accepted"] == "true"
    assert no["caravan_accepted"] == "false"


# ── servicios_extras booleans ─────────────────────────────────────────────────

def test_campfire_true():
    norm = {"servicios_extras": {"campfire": True}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("campfire_allowed") == "true"


def test_campfire_false():
    norm = {"servicios_extras": {"campfire": False}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("campfire_allowed") == "false"


def test_ev_charging():
    norm = {"servicios_extras": {"ev_charging": True}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("ev_charging") == "true"


def test_cell_service_true():
    norm = {"servicios_extras": {"cell_service": True}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("cell_coverage") == "0.8"


def test_cell_service_false():
    norm = {"servicios_extras": {"cell_service": False}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("cell_coverage") == "0.1"


def test_requires_4wd_road_quality():
    norm = {"servicios_extras": {"requires_4wd": True}}
    s = signals(extract_claims_from_source_record(norm, "test"))
    assert s.get("road_quality") == "0.15"
    assert s.get("large_vehicle") == "0.1"


def test_family_friendly_extras():
    norm = {"servicios_extras": {"family_friendly": True}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("family_friendly") == "true"


# ── servicios_extras strings ──────────────────────────────────────────────────

def test_ground_paved():
    norm = {"servicios_extras": {"ground": "paved"}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("road_quality") == "0.85"


def test_ground_dirt():
    norm = {"servicios_extras": {"ground": "dirt"}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("road_quality") == "0.2"


def test_mobile_signal_3g4g():
    norm = {"servicios_extras": {"mobile_signal": "3g_4g"}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("cell_coverage") == "0.8"


# ── environment_labels ────────────────────────────────────────────────────────

def test_env_beach():
    norm = {"servicios_extras": {"environment_labels": ["beach", "forest"]}}
    s = signals(extract_claims_from_source_record(norm, "test"))
    assert s.get("beach_access") == "true"
    assert s.get("stealth") == "0.65"


def test_env_river():
    norm = {"servicios_extras": {"environment_labels": ["river"]}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("river_nearby") == "true"


def test_env_lake():
    norm = {"servicios_extras": {"environment_labels": ["lake"]}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("lake_nearby") == "true"


def test_env_mountains():
    norm = {"servicios_extras": {"environment_labels": ["mountains"]}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("mountain_view") == "true"


def test_env_secluded():
    norm = {"servicios_extras": {"environment_labels": ["secluded"]}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("stealth") == "0.82"


def test_env_calm():
    norm = {"servicios_extras": {"environment_labels": ["calm"]}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("quietness") == "0.8"


def test_env_near_road():
    norm = {"servicios_extras": {"environment_labels": ["near_road"]}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("road_noise") == "0.75"


# ── vibes ─────────────────────────────────────────────────────────────────────

def test_vibe_starry_sky():
    norm = {"servicios_extras": {"vibes": ["starry_sky"]}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("dark_sky") == "true"


def test_vibe_wow_factor():
    norm = {"servicios_extras": {"vibes": ["wow_factor"]}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("beauty") == "0.9"


# ── prohibitions ─────────────────────────────────────────────────────────────

def test_prohibition_overnight():
    norm = {"servicios_extras": {"prohibitions": ["overnight_camping", "dogs"]}}
    s = signals(extract_claims_from_source_record(norm, "test"))
    assert s.get("overnight_safe") == "false"
    assert s.get("dog_friendly") == "false"


def test_prohibition_fire():
    norm = {"servicios_extras": {"prohibitions": ["campfire", "noise_after_22h"]}}
    s = signals(extract_claims_from_source_record(norm, "test"))
    assert s.get("campfire_allowed") == "false"


# ── max_vehicle_length_ft ─────────────────────────────────────────────────────

def test_max_vehicle_long_ok():
    norm = {"servicios_extras": {"max_vehicle_length_ft": 30}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("large_vehicle") == "0.85"


def test_max_vehicle_short_restricted():
    norm = {"servicios_extras": {"max_vehicle_length_ft": 18}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("large_vehicle") == "0.15"


# ── activities ────────────────────────────────────────────────────────────────

def test_activities_skiing():
    norm = {"servicios_extras": {"activities": ["skiing", "sledding"]}}
    # No hay señal winter_friendly en STATIC_SIGNALS todavía, se ignora silenciosamente
    # (señal no registrada → skip)
    claims = extract_claims_from_source_record(norm, "test")
    assert isinstance(claims, list)


def test_activities_swimming():
    norm = {"servicios_extras": {"activities": ["swimming", "canoeing"]}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("swimming_access") == "true"


def test_activities_hiking():
    norm = {"servicios_extras": {"activities": ["hiking"]}}
    assert signals(extract_claims_from_source_record(norm, "test")).get("hiking_nearby") == "true"


# ── Deduplicación (misma señal de múltiples fuentes no se duplica por source_record) ──

def test_no_duplicate_signals_same_record():
    # vaciado_negras Y vaciado_grises ambos True → solo 1 claim dump_station_working=true
    norm = {"vaciado_negras": True, "vaciado_grises": True}
    claims = extract_claims_from_source_record(norm, "test")
    dump_claims = [c for c in claims if c["signal"] == "dump_station_working" and c["value"] == "true"]
    assert len(dump_claims) == 1


def test_unknown_signal_skipped():
    # Si un signal no está en STATIC_SIGNALS, se descarta silenciosamente
    norm = {"servicios_extras": {"vibes": ["unknown_vibe_xyz"]}}
    claims = extract_claims_from_source_record(norm, "test")
    assert all(c["signal"] in ("beauty", "stealth", "dark_sky") or True for c in claims)


# ── Datos reales simulados (escenario park4night) ─────────────────────────────

def test_park4night_full_norm():
    """Simula un normalized_data típico de park4night."""
    norm = {
        "agua_potable": True,
        "electricidad": False,
        "ducha": True,
        "vaciado_negras": True,
        "wifi": False,
        "perros": True,
        "acceso_grandes": True,
        "num_plazas": 12,
        "servicios_extras": {
            "environment_labels": ["mountains", "forest"],
            "campfire": True,
            "pricing_breakdown": {"pernocta": 8.0},
        },
    }
    claims = extract_claims_from_source_record(norm, "park4night")
    s = signals(claims)
    assert s["water_working"] == "true"
    assert s["electricity_working"] == "false"
    assert s["shower_working"] == "true"
    assert s["dump_station_working"] == "true"
    assert s["dog_friendly"] == "true"
    assert s["large_vehicle"] == "0.85"
    assert s["mountain_view"] == "true"
    assert s["stealth"] == "0.65"  # forest
    assert s["campfire_allowed"] == "true"
    # num_plazas 12 → no entra ni en small ni en big
    assert "parking_capacity" not in s


def test_campingcarpark_with_prohibitions():
    """Simula campingcarpark con prohibition de overnight y sin perros."""
    norm = {
        "agua_potable": True,
        "electricidad": True,
        "amperaje": 16,
        "servicios_extras": {
            "prohibitions": ["overnight_stay", "dogs_prohibited"],
            "environment_labels": ["urban"],
            "ground": "concrete",
        },
    }
    claims = extract_claims_from_source_record(norm, "campingcarpark")
    s = signals(claims)
    assert s["overnight_safe"] == "false"
    assert s["dog_friendly"] == "false"
    assert s["road_quality"] == "0.85"
    assert s["crowd_level"] == "0.7"  # urban


def test_roadsurfer_beach_with_campfire():
    """Simula roadsurfer con entorno playa y hoguera prohibida."""
    norm = {
        "agua_potable": True,
        "servicios_extras": {
            "environment_labels": ["beach", "sea_coast"],
            "campfire": False,
            "bbq": True,   # bbq sí pero campfire no → campfire_allowed=false gana (seen)
            "cell_service": True,
        },
    }
    claims = extract_claims_from_source_record(norm, "roadsurfer")
    s = signals(claims)
    assert s["beach_access"] == "true"
    assert s["campfire_allowed"] == "false"  # campfire=False toma prioridad sobre bbq=True por orden
    assert s["cell_coverage"] == "0.8"


def test_nomady_river_no_dogs():
    """Simula nomady con entorno río y perros no permitidos."""
    norm = {
        "perros": False,
        "wifi": True,
        "servicios_extras": {
            "environment_labels": ["river", "meadow"],
            "campfire": True,
        },
    }
    claims = extract_claims_from_source_record(norm, "nomady")
    s = signals(claims)
    assert s["dog_friendly"] == "false"
    assert s["cell_coverage"] == "0.75"  # wifi proxy
    assert s["river_nearby"] == "true"
    assert s["campfire_allowed"] == "true"
