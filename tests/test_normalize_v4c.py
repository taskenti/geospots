"""Tests para los extractores v4c en scraper/sources/_normalize_helpers.py.

Verifica que la lógica de mapeo raw_data → columnas v4c es la misma que
implementaba jobs/backfill_extra_services.py. Si pasan, los nuevos scrapes
escriben directamente los campos sin necesidad de correr el backfill.
"""

from __future__ import annotations

import os
import sys

# scraper/ no es un paquete (no tiene __init__.py). Añadimos scraper/ al path
# para que el import `from sources._normalize_helpers ...` resuelva igual que
# desde dentro del contenedor scraper.
HERE = os.path.dirname(os.path.abspath(__file__))
SCRAPER_DIR = os.path.abspath(os.path.join(HERE, "..", "scraper"))
if SCRAPER_DIR not in sys.path:
    sys.path.insert(0, SCRAPER_DIR)

from sources._normalize_helpers import (  # noqa: E402
    extract_agricamper,
    extract_alpacacamping,
    extract_bobilguiden,
    extract_campercontact,
    extract_campercontact_detail,
    extract_campendium,
    extract_campingcarpark,
    extract_campspace,
    extract_campy,
    extract_camperstop,
    extract_caramaps,
    extract_furgovw,
    extract_nomady,
    extract_osm,
    extract_park4night,
    extract_promobil,
    extract_roadsurfer,
    extract_searchforsites,
    extract_stayfree,
    extract_thedyrt,
    extract_vansite,
    extract_womostell,
    extract_wtmg,
    merge_extra,
)


# ─── park4night ───────────────────────────────────────────────────────


def test_park4night_booleans_from_string_flags():
    raw = {
        "piscine": "1",
        "lavage": "0",
        "laverie": "1",
        "gaz": "0",
        "gpl": "0",
        "jeux_enfants": "1",
        "point_de_vue": "1",
        "nature_protect": "0",
        "vtt": "1",
        "rando": "1",
        "escalade": "0",
        "peche": "1",
    }
    out = extract_park4night(raw)
    assert out["piscina"] is True
    assert out["lavanderia"] is True  # any True
    assert out["gas_recharge"] is False
    assert out["juegos_ninos"] is True
    assert out["mirador"] is True
    assert out["zona_protegida"] is False
    assert out["mtb_friendly"] is True
    assert out["hiking_nearby"] is True
    assert out["climbing"] is False
    assert out["fishing"] is True


def test_park4night_pricing_breakdown():
    raw = {"prix_services": "5 EUR", "prix_stationnement": "Free"}
    out = extract_park4night(raw)
    assert out["servicios_extras"]["pricing_breakdown"] == {
        "services": "5 EUR",
        "pernocta": "Free",
    }


def test_park4night_unknown_when_no_flags():
    out = extract_park4night({"id": 1})
    assert out["piscina"] is None
    assert "servicios_extras" not in out


# ─── campingcarpark ────────────────────────────────────────────────────


def test_campingcarpark_ints_and_booking():
    raw = {
        "amperage": 16,
        "electricalOutletCount": 50,
        "maxNightCount": 7,
        "isBookable": True,
    }
    out = extract_campingcarpark(raw)
    assert out["amperaje"] == 16
    assert out["n_enchufes"] == 50
    assert out["max_noches"] == 7
    assert out["online_booking"] is True


def test_campingcarpark_prohibitions_dict_true_only():
    raw = {"prohibitions": {"dog": True, "barbecue": False, "vehicleMore9m": True}}
    out = extract_campingcarpark(raw)
    assert set(out["servicios_extras"]["prohibitions"]) == {"dog", "vehicleMore9m"}


def test_campingcarpark_descriptions_and_tariffs():
    raw = {
        "sanitaryDescription": "Clean WC",
        "surroundingsDescription": "Nice view",
        "tariffs": [
            {"label": "1 noche", "amount": 12},
            {"label": "Servicios", "amount": 3},
        ],
        "destinationTypes": ["sea", "heritage"],
        "sanitaryOpening": {"from": "08:00", "to": "20:00", "toiletCount": 4, "showerCount": 2},
    }
    out = extract_campingcarpark(raw)
    extras = out["servicios_extras"]
    assert extras["descriptions"] == {"sanitary": "Clean WC", "surroundings": "Nice view"}
    assert extras["pricing_breakdown"] == {"1 noche": 12, "Servicios": 3}
    assert extras["destination_types"] == ["sea", "heritage"]
    assert extras["hours"]["sanitary_opening"] == "08:00-20:00"
    assert extras["hours"]["toilet_count"] == 4
    assert extras["hours"]["shower_count"] == 2


# ─── agricamper ───────────────────────────────────────────────────────


def test_agricamper_services_inference():
    raw = {
        "fiche_service_label": ["Swimming Pool", "Restaurant", "Hiking trails"],
        "fiche_langue_parlee_label": ["English", "Italian", "Spanish"],
        "fiche_produit_label": ["Wine", "Olive oil"],
        "fiche_typologie_label": ["Agricamping"],
        "fiche_position_label": ["Countryside"],
        "fiche_label_label": ["DOC"],
    }
    out = extract_agricamper(raw)
    assert out["piscina"] is True
    assert out["restaurant"] is True
    assert out["hiking_nearby"] is True
    assert out["lavanderia"] is False  # services list provided but no laundry mention
    assert out["idiomas_hablados"] == ["en", "es", "it"]
    assert out["productos_venta"] == ["Wine", "Olive oil"]
    assert out["servicios_extras"]["typology"] == ["agricamping"]
    assert out["servicios_extras"]["position"] == ["countryside"]
    assert out["servicios_extras"]["quality_labels"] == ["DOC"]


# ─── caramaps ─────────────────────────────────────────────────────────


def test_caramaps_attributes_inference():
    raw = {
        "attributes": [
            {"attribute": {"label": "Piscine"}},
            {"attribute": {"label": "Randonnée"}},
            {"attribute": {"label": "VTT"}},
        ]
    }
    out = extract_caramaps(raw)
    assert out["piscina"] is True
    assert out["hiking_nearby"] is True
    assert out["mtb_friendly"] is True
    assert out["restaurant"] is False  # mencionado en lista pero ausente
    assert out["climbing"] is False


def test_caramaps_empty_attributes():
    assert extract_caramaps({}) == {}
    assert extract_caramaps({"attributes": []}) == {}


# ─── campy ────────────────────────────────────────────────────────────


def test_campy_facilities_inference():
    raw = {"facilities": ["Swimming pool", "Bike rental", "Hiking"]}
    out = extract_campy(raw)
    assert out["piscina"] is True
    assert out["mtb_friendly"] is True
    assert out["hiking_nearby"] is True
    assert out["restaurant"] is False
    assert out["lavanderia"] is False


# ─── campercontact ────────────────────────────────────────────────────


def test_campercontact_price_breakdown():
    raw = {"priceBreakdown": {"totalPrice": 15, "pricePerNight": 12, "fee": 3}}
    out = extract_campercontact(raw)
    assert out["servicios_extras"]["pricing_breakdown"] == {
        "total": 15, "per_night": 12, "fee": 3,
    }


def test_campercontact_no_price_breakdown():
    assert extract_campercontact({}) == {}
    assert extract_campercontact({"priceBreakdown": None}) == {}


# ─── merge_extra ──────────────────────────────────────────────────────


def test_merge_extra_does_not_overwrite_existing():
    norm = {"piscina": False, "nombre": "Algo"}
    extra = {"piscina": True, "mirador": True}
    out = merge_extra(norm, extra)
    assert out["piscina"] is False  # existing wins
    assert out["mirador"] is True   # new key added
    assert out["nombre"] == "Algo"


def test_merge_extra_skips_none():
    norm = {"piscina": None}
    extra = {"piscina": True, "lavanderia": None}
    out = merge_extra(norm, extra)
    # piscina existía como None → se considera no-poblado, escribe nuevo
    assert out["piscina"] is True
    assert "lavanderia" not in out or out["lavanderia"] is None


def test_merge_extra_jsonb_shallow_merge_existing_wins():
    norm = {"servicios_extras": {"pricing_breakdown": {"services": "viejo"}}}
    extra = {"servicios_extras": {"pricing_breakdown": {"services": "nuevo"}, "risks": ["x"]}}
    out = merge_extra(norm, extra)
    # top-key existente conservada
    assert out["servicios_extras"]["pricing_breakdown"] == {"services": "viejo"}
    # nueva top-key añadida
    assert out["servicios_extras"]["risks"] == ["x"]


def test_merge_extra_handles_empty():
    norm = {"a": 1}
    assert merge_extra(norm, {}) == {"a": 1}


# ─── Wire end-to-end: park4night normalize llama merge_extra ─────────


# ─── v4d (audit capa 1) ──────────────────────────────────────────────


def test_campingcarpark_v4d_securiplace_and_extras():
    raw = {
        "securiplace": True,
        "authorizedVehicles": {"tents": False, "campers": True, "caravans": True, "notAutonomousVans": False},
        "customersProfile": ["ccowner", "van"],
        "touristTaxes": [{"amount": 0.50, "beginsAt": "2026-01-01", "endsAt": "2026-12-31"}],
        "labels": {"currentLabels": [{"title": "Ville d'art et histoire"}, {"title": "Jardin Remarquable"}]},
        "benefits": "Cerca del tranvía",
        "shops": "Restaurantes a 500m",
        "access": "Por la D915",
    }
    out = extract_campingcarpark(raw)
    assert out["seguridad"] is True
    extras = out["servicios_extras"]
    assert extras["authorized_vehicles"] == ["campers", "caravans"]
    assert extras["customer_profile"] == ["ccowner", "van"]
    assert extras["pricing_breakdown"]["tourist_tax"] == 0.50
    assert extras["quality_labels"] == ["Ville d'art et histoire", "Jardin Remarquable"]
    assert extras["descriptions"]["benefits"] == "Cerca del tranvía"
    assert extras["descriptions"]["shops"] == "Restaurantes a 500m"
    assert extras["descriptions"]["access"] == "Por la D915"


def test_agricamper_v4d_municipio_caravanas_handicap_chemin():
    raw = {
        "adresse_ville": "Tuscania",
        "accepte_caravanes": 1,
        "accepte_handicap": 0,
        "est_chemin_difficile": 0,
        "est_chemin_pente": 1,
        "est_chemin_pierre": 0,
    }
    out = extract_agricamper(raw)
    assert out["municipio"] == "Tuscania"
    assert out["acepta_caravanas"] is True
    assert out["accesibilidad_reducida"] is False
    assert out["acceso_dificil"] is True  # OR de los 3, pente=1


def test_agricamper_v4d_chemin_all_false():
    raw = {"est_chemin_difficile": 0, "est_chemin_pente": 0, "est_chemin_pierre": 0}
    out = extract_agricamper(raw)
    assert out["acceso_dificil"] is False


def test_caramaps_v4d_email_and_opening():
    raw = {
        "contactInformation": {"email": "info@carinera.es"},
        "openingDates": [{"start": "2024-01-01T00:00:00+01:00", "end": "2024-12-31T00:00:00+01:00"}],
    }
    out = extract_caramaps(raw)
    assert out["email"] == "info@carinera.es"
    assert out["temporada_apertura"] == "all_year"


def test_caramaps_v4d_partial_opening():
    raw = {"openingDates": [{"start": "2024-04-15T00:00:00+01:00", "end": "2024-09-30T00:00:00+01:00"}]}
    out = extract_caramaps(raw)
    assert out["temporada_apertura"] == "2024-04-15/2024-09-30"


def test_campy_v4d_temporada_and_quality():
    raw = {
        "dateOpenFrom": "2025-01-01",
        "dateOpenTo": "2025-12-31",
        "isTopQuality": True,
        "camperSize": "up to 8m",
    }
    out = extract_campy(raw)
    assert out["temporada_apertura"] == "all_year"
    assert out["servicios_extras"]["top_quality"] is True
    assert out["servicios_extras"]["max_camper_size"] == "up to 8m"


def test_campercontact_detail_extras():
    poi = {
        "terrain": ["illuminated", "security", "shaded", "fenced"],
        "amenities": [
            {"type": "water", "priceStatus": "free"},
            {"type": "electricity", "priceStatus": "paid"},
            {"type": "shower", "priceStatus": None},  # debe ignorarse
        ],
    }
    out = extract_campercontact_detail(poi)
    extras = out["servicios_extras"]
    assert extras["terrain"] == ["illuminated", "security", "shaded", "fenced"]
    assert extras["amenity_pricing"] == {"water": "free", "electricity": "paid"}


def test_campercontact_detail_empty():
    assert extract_campercontact_detail({}) == {}
    assert extract_campercontact_detail({"terrain": [], "amenities": []}) == {}


def test_agricamper_normalize_emits_v4d():
    """Wire-end-to-end: AgricamperSource.normalize() debe emitir v4d cols."""
    from sources.agricamper import AgricamperSource
    raw = {
        "id": 999,
        "adresse_latitude": 42.5,
        "adresse_longitude": 11.5,
        "adresse_ville": "Tuscania",
        "adresse_province": "Viterbo (VT)",
        "accepte_caravanes": 1,
        "accepte_handicap": 1,
        "est_chemin_difficile": 0,
        "est_chemin_pente": 1,
        "est_chemin_pierre": 1,
        "fiche_typologie_label": ["Agricamping"],
        "fiche_service_label": ["Restaurant"],
        "fiche_langue_parlee_label": ["English", "Italian"],
    }
    out = AgricamperSource().normalize(raw)
    assert out is not None
    assert out["municipio"] == "Tuscania"
    assert out["acepta_caravanas"] is True
    assert out["accesibilidad_reducida"] is True
    assert out["acceso_dificil"] is True
    assert out["idiomas_hablados"] == ["en", "it"]
    assert out["restaurant"] is True


# ─── camperstop ───────────────────────────────────────────────────────


def test_camperstop_basic_fields():
    raw = {
        "animalsAllowed": 1,
        "winterSports": 0,
        "place": "Arques",
        "powerQuantity": 12,
        "groundType": "gravel",
        "environment": "urban",
    }
    out = extract_camperstop(raw)
    assert out["perros"] is True
    assert out["winter_friendly"] is False
    assert out["municipio"] == "Arques"
    assert out["n_enchufes"] == 12
    assert out["servicios_extras"]["ground_type"] == "gravel"
    assert out["servicios_extras"]["environment"] == "urban"


def test_camperstop_pricing_breakdown_skips_zeros():
    raw = {
        "waterPrice": "1.50",
        "powerPrice": "0.00",
        "showerPrice": "1.50",
        "toiletPrice": "0.00",
        "serviceRate": "water € 2,50",
    }
    out = extract_camperstop(raw)
    pb = out["servicios_extras"]["pricing_breakdown"]
    assert pb["water"] == 1.50
    assert "electricity" not in pb  # 0.00 se omite
    assert pb["shower"] == 1.50
    assert "toilet" not in pb       # 0.00 se omite
    assert pb["service_rate"] == "water € 2,50"


def test_camperstop_credit_card_and_payment():
    raw = {"creditCard": 1, "paymentType": "Reception/office"}
    out = extract_camperstop(raw)
    assert out["servicios_extras"]["credit_card"] is True
    assert out["servicios_extras"]["payment_type"] == "Reception/office"


def test_camperstop_remarks_as_descriptions():
    raw = {"remarks": ["behind camp site Beauséjour", "sanitary at campsite"]}
    out = extract_camperstop(raw)
    assert out["servicios_extras"]["descriptions"]["remarks"] == [
        "behind camp site Beauséjour",
        "sanitary at campsite",
    ]


def test_camperstop_environment_labels():
    raw = {
        "calm": 1, "loud": 0, "verySimple": 1,
        "forest": 0, "mountains": 1, "touristic": 0,
    }
    out = extract_camperstop(raw)
    labels = out["servicios_extras"]["environment_labels"]
    assert "calm" in labels
    assert "very_simple" in labels
    assert "mountains" in labels
    assert "loud" not in labels   # 0 → no añadido
    assert "forest" not in labels


def test_camperstop_no_extras_when_empty():
    out = extract_camperstop({})
    assert out == {"perros": None, "winter_friendly": None, "municipio": None, "n_enchufes": None}
    assert "servicios_extras" not in out


def test_camperstop_normalize_emits_v4e():
    """Wire end-to-end: CamperstopSource.normalize() emite campos v4e."""
    from sources.camperstop import CamperstopSource
    raw = {
        "id": "42",
        "name": "Aire de Beauvais",
        "latitude": 49.43,
        "longitude": 2.08,
        "camperStopTypeId": 1,
        "countryCode": "fr",
        "waterAvailable": True,
        "drainageAvailable": False,
        "chemicalAvailable": False,
        "powerAvailable": True,
        "toiletAvailable": False,
        "showerAvailable": False,
        "wifiAvailable": False,
        "images": [],
        "contactWebsite": "",
        "averageScore": 8,
        "totalReviews": 5,
        "camperRate": "free",
        # campos v4e
        "animalsAllowed": 1,
        "winterSports": 0,
        "place": "Beauvais",
        "powerQuantity": 6,
        "groundType": "asphalt",
        "environment": "urban",
        "waterPrice": "2.00",
        "creditCard": 0,
        "remarks": ["Next to the tourist office"],
        "calm": 1,
        "touristic": 1,
    }
    out = CamperstopSource().normalize(raw)
    assert out is not None
    assert out["perros"] is True
    assert out["winter_friendly"] is False
    assert out["municipio"] == "Beauvais"
    assert out["n_enchufes"] == 6
    assert out["servicios_extras"]["ground_type"] == "asphalt"
    assert out["servicios_extras"]["pricing_breakdown"]["water"] == 2.0
    assert out["servicios_extras"]["descriptions"]["remarks"] == ["Next to the tourist office"]
    labels = out["servicios_extras"]["environment_labels"]
    assert "calm" in labels
    assert "touristic" in labels


def test_park4night_normalize_emits_v4c():
    """Verifica que el normalize() real (no solo el extractor) emite los v4c."""
    from sources.park4night import Park4NightSource
    raw = {
        "id": 12345,
        "name": "Test spot",
        "code": "P",
        "latitude": "43.5",
        "longitude": "-1.2",
        "prix": "0",
        "piscine": "1",
        "vtt": "1",
        "rando": "1",
        "moto": "0",
        "prix_services": "Free",
    }
    out = Park4NightSource().normalize(raw)
    assert out is not None
    assert out["piscina"] is True
    assert out["mtb_friendly"] is True
    assert out["hiking_nearby"] is True
    assert out["apto_motos"] is False
    assert out["servicios_extras"]["pricing_breakdown"] == {"services": "Free"}


# ─── womostell ────────────────────────────────────────────────────────


def test_womostell_basic():
    raw = {
        "b_long_campers": "1",   # lange Wohnmobile → acceso_grandes (handled in normalize)
        "b_reservation": "1",
        "city": "Freiburg",
        "price": "12.50",
    }
    out = extract_womostell(raw)
    # b_long_campers must NOT map to acepta_caravanas (it means long motorhomes, not trailers)
    assert "acepta_caravanas" not in out
    assert out["online_booking"] is True
    assert out["municipio"] == "Freiburg"
    assert out["servicios_extras"]["pricing_breakdown"]["pernocta"] == 12.50


def test_womostell_free_no_pricing():
    raw = {"price": "0", "b_long_campers": "0", "city": ""}
    out = extract_womostell(raw)
    # b_long_campers not in extractor output
    assert "acepta_caravanas" not in out
    # price == 0 should NOT produce a pricing_breakdown entry
    assert "servicios_extras" not in out or "pricing_breakdown" not in out.get("servicios_extras", {})


def test_womostell_no_price():
    raw = {"b_reservation": "0"}
    out = extract_womostell(raw)
    assert out["online_booking"] is False
    assert "servicios_extras" not in out


def test_womostell_normalize_emits_v4c():
    """WomoStellplatz normalize() emite municipio y acceso_grandes desde b_long_campers."""
    from sources.womostell import WomoStellplatzSource
    raw = {
        "place_id": "9999",
        "name": "Test Stellplatz",
        "latitude": "48.1",
        "longitude": "11.5",
        "place_type_id": 2,
        "b_long_campers": "1",
        "city": "München",
        "price": "8.00",
    }
    out = WomoStellplatzSource().normalize(raw)
    assert out is not None
    # b_long_campers → acceso_grandes (large motorhomes), NOT acepta_caravanas
    assert out["acceso_grandes"] is True
    assert "acepta_caravanas" not in out
    assert out["municipio"] == "München"
    assert out["servicios_extras"]["pricing_breakdown"]["pernocta"] == 8.0


# ─── stayfree ─────────────────────────────────────────────────────────


def test_stayfree_services_and_activities():
    raw = {
        "features": {
            "SERVICE_ANIMALS": True,
            "SERVICE_CARAVANS": True,
            "SERVICE_WIFI": True,
            "ACTIVITY_HIKING": True,
            "ACTIVITY_BIKING": True,
            "ACTIVITY_FISHING": False,
            "ROAD_UNPAVED": True,
        }
    }
    out = extract_stayfree(raw)
    assert out["perros"] is True
    assert out["acepta_caravanas"] is True
    assert out["wifi"] is True
    assert out["hiking_nearby"] is True
    assert out["mtb_friendly"] is True
    assert out.get("fishing") is False
    assert out["acceso_dificil"] is True


def test_stayfree_paved_road_not_difficult():
    raw = {
        "features": {
            "ROAD_PAVED": True,
            "ROAD_UNPAVED": False,
        }
    }
    out = extract_stayfree(raw)
    assert out["acceso_dificil"] is False


def test_stayfree_environment_labels_in_extras():
    raw = {
        "features": {
            "ENVIRONMENT_FOREST": True,
            "ENVIRONMENT_MOUNTAINS": True,
            "ENVIRONMENT_SEA": False,
            "SERVICE_MOBILE_3G_4G": True,
        }
    }
    out = extract_stayfree(raw)
    labels = out["servicios_extras"]["environment_labels"]
    assert "forest" in labels
    assert "mountains" in labels
    assert "sea" not in labels
    assert out["servicios_extras"]["mobile_signal"] == "3g_4g"


def test_stayfree_always_open():
    raw = {"is_always_open": "yes", "features": {}}
    out = extract_stayfree(raw)
    assert out["temporada_apertura"] == "all_year"


def test_stayfree_municipio_from_address():
    raw = {"address": "123 Main St, Sevilla, Andalucía, Spain", "features": {}}
    out = extract_stayfree(raw)
    assert out["municipio"] == "Sevilla"


def test_stayfree_empty_features():
    out = extract_stayfree({"features": None})
    assert isinstance(out, dict)


# ─── promobil ─────────────────────────────────────────────────────────


def test_promobil_basic():
    raw = {
        "caravan": True,
        "city": "Heidelberg",
    }
    out = extract_promobil(raw)
    assert out["acepta_caravanas"] is True
    assert out["municipio"] == "Heidelberg"


def test_promobil_beergarden_in_extras():
    raw = {"beergarden": True, "caravan": False}
    out = extract_promobil(raw)
    assert out["acepta_caravanas"] is False
    assert out["servicios_extras"]["beer_garden"] is True


def test_promobil_leisure_text():
    raw = {
        "_de": {
            "leisureActivitiesText": "Wandern, Radfahren, Schwimmen",
        }
    }
    out = extract_promobil(raw)
    assert out["servicios_extras"]["descriptions"]["leisure"] == "Wandern, Radfahren, Schwimmen"


def test_promobil_ai_highlights():
    raw = {
        "_de": {
            "aiHighlights": {
                "highlights": ["Schwarzwald", "Rhein", "Schloss"]
            }
        }
    }
    out = extract_promobil(raw)
    assert "Schwarzwald" in out["servicios_extras"]["descriptions"]["nearby_highlights"]


def test_promobil_no_extras():
    raw = {"caravan": None, "city": ""}
    out = extract_promobil(raw)
    assert "servicios_extras" not in out
    assert out.get("municipio") is None


def test_promobil_normalize_emits_v4c():
    from sources.promobil import PromobilSource
    raw = {
        "id": "42",
        "gps": [48.5, 9.2],
        "caravan": True,
        "city": "Stuttgart",
        "beergarden": True,
        "_de": {"name": "Schöner Platz"},
        "pitchType": "Stellplatz",
    }
    out = PromobilSource().normalize(raw)
    assert out is not None
    assert out["acepta_caravanas"] is True
    assert out["municipio"] == "Stuttgart"
    assert out["servicios_extras"]["beer_garden"] is True


# ─── searchforsites ───────────────────────────────────────────────────


def test_searchforsites_lavanderia_and_juegos():
    raw = {"facilities": "1,4,18,20"}
    out = extract_searchforsites(raw)
    assert out["lavanderia"] is True
    assert out["juegos_ninos"] is True


def test_searchforsites_perros_from_facilities():
    raw = {"facilities": "8,1"}
    out = extract_searchforsites(raw)
    assert out["perros"] is True


def test_searchforsites_municipio_from_address():
    raw = {"address": "Llanberis, Gwynedd, Wales"}
    out = extract_searchforsites(raw)
    assert out["municipio"] == "Llanberis"


def test_searchforsites_pricing_with_currency():
    raw = {
        "cost": {"min": "5.0", "max": "15.0", "sym": "£"},
    }
    out = extract_searchforsites(raw)
    pb = out["servicios_extras"]["pricing_breakdown"]
    assert pb["min"] == 5.0
    assert pb["max"] == 15.0
    assert pb["currency_sym"] == "£"


def test_searchforsites_no_extras_when_empty():
    raw = {"facilities": "1,4,6"}
    out = extract_searchforsites(raw)
    # facs 1,4,6 → water, wc, shower: no lavanderia/juegos/perros → no extras, no servicios_extras
    assert "lavanderia" not in out
    assert "juegos_ninos" not in out
    assert "servicios_extras" not in out


def test_searchforsites_normalize_emits_v4c():
    from sources.searchforsites import SearchForSitesSource
    raw = {
        "ID": "77",
        "Name": "Test Site",
        "latlng": {"lat": "51.5", "lng": "-3.2"},
        "Type": "CL",
        "facilities": "1,18,20",
        "address": "Cardiff, South Wales",
        "cost": {"min": "10", "max": "20", "sym": "£"},
        "dog": "1",
    }
    out = SearchForSitesSource().normalize(raw)
    assert out is not None
    assert out["lavanderia"] is True
    assert out["juegos_ninos"] is True
    assert out["municipio"] == "Cardiff"
    pb = out["servicios_extras"]["pricing_breakdown"]
    assert pb["min"] == 10.0
    assert pb["currency_sym"] == "£"


# ─── bobilguiden ──────────────────────────────────────────────────────


def test_bobilguiden_municipio_from_city():
    raw = {
        "location": {"address": {"city": "Bergen", "county": "Vestland"}},
        "facilityIds": [],
    }
    out = extract_bobilguiden(raw)
    assert out["municipio"] == "Bergen"


def test_bobilguiden_caravan_allowed():
    raw = {"location": {}, "caravanAllowed": True, "facilityIds": []}
    out = extract_bobilguiden(raw)
    assert out["acepta_caravanas"] is True


def test_bobilguiden_caravan_not_allowed():
    raw = {"caravanAllowed": False, "facilityIds": []}
    out = extract_bobilguiden(raw)
    assert out["acepta_caravanas"] is False


def test_bobilguiden_short_description():
    raw = {"shortDescription": "Fantastisk utsikt over fjorden.", "facilityIds": []}
    out = extract_bobilguiden(raw)
    assert out["descripcion_no"] == "Fantastisk utsikt over fjorden."


def test_bobilguiden_empty_raw():
    out = extract_bobilguiden({})
    assert isinstance(out, dict)


def test_bobilguiden_normalize_emits_v4c():
    from sources.bobilguiden import BobilguidenSource
    raw = {
        "id": "123",
        "name": "Camping Bergen",
        "type": "CAMPING_SITE",
        "location": {
            "coordinates": {"latitude": 60.4, "longitude": 5.3},
            "address": {"city": "Bergen", "county": "Vestland", "countryId": 1},
        },
        "facilityIds": [2, 3, 5, 6],
        "caravanAllowed": True,
        "shortDescription": "Rolig plass ved vannet.",
    }
    out = BobilguidenSource().normalize(raw)
    assert out is not None
    assert out["municipio"] == "Bergen"
    assert out["acepta_caravanas"] is True
    assert out["descripcion_no"] == "Rolig plass ved vannet."


# ─── campendium ───────────────────────────────────────────────────────


def test_campendium_pool_and_water():
    raw = {
        "properties": {
            "place_detail": {"pool": True, "water": True},
        }
    }
    out = extract_campendium(raw)
    assert out["piscina"] is True
    assert out["agua_potable"] is True


def test_campendium_municipio_from_city():
    raw = {"properties": {"city": "Tucson", "state": "AZ"}}
    out = extract_campendium(raw)
    assert out["municipio"] == "Tucson"


def test_campendium_num_plazas_from_capacity():
    raw = {"properties": {"place_detail": {"capacity": "48"}}}
    out = extract_campendium(raw)
    assert out["num_plazas"] == 48


def test_campendium_empty():
    out = extract_campendium({})
    assert isinstance(out, dict)


# ─── osm ──────────────────────────────────────────────────────────────


def test_osm_acepta_caravanas_yes():
    raw = {"tags": {"caravans": "yes"}}
    out = extract_osm(raw)
    assert out["acepta_caravanas"] is True


def test_osm_acepta_caravanas_no():
    raw = {"tags": {"caravans": "no"}}
    out = extract_osm(raw)
    assert out["acepta_caravanas"] is False


def test_osm_acceso_dificil_gravel():
    raw = {"tags": {"surface": "gravel"}}
    out = extract_osm(raw)
    assert out["acceso_dificil"] is True


def test_osm_acceso_facil_asphalt():
    raw = {"tags": {"surface": "asphalt"}}
    out = extract_osm(raw)
    assert out["acceso_dificil"] is False


def test_osm_municipio():
    raw = {"tags": {"addr:city": "Malaga"}}
    out = extract_osm(raw)
    assert out["municipio"] == "Malaga"


def test_osm_ev_charging():
    raw = {"tags": {"motorhome:charging": "yes"}}
    out = extract_osm(raw)
    assert out["servicios_extras"]["ev_charging"] is True


def test_osm_stars():
    raw = {"tags": {"stars": "3"}}
    out = extract_osm(raw)
    assert out["servicios_extras"]["stars"] == 3


def test_osm_stars_out_of_range():
    raw = {"tags": {"stars": "9"}}
    out = extract_osm(raw)
    assert "stars" not in out.get("servicios_extras", {})


def test_osm_empty_tags():
    out = extract_osm({"tags": {}})
    assert isinstance(out, dict)


def test_osm_normalize_emits_v4c():
    from sources.osm import OSMSource
    raw = {
        "type": "node",
        "id": 12345,
        "lat": 36.7,
        "lon": -4.4,
        "tags": {
            "tourism": "caravan_site",
            "caravans": "yes",
            "surface": "gravel",
            "addr:city": "Marbella",
            "stars": "4",
            "motorhome:charging": "yes",
        },
    }
    out = OSMSource().normalize(raw)
    assert out is not None
    assert out["acepta_caravanas"] is True
    assert out["acceso_dificil"] is True
    assert out["municipio"] == "Marbella"
    assert out["servicios_extras"]["stars"] == 4
    assert out["servicios_extras"]["ev_charging"] is True


# ─── furgovw ──────────────────────────────────────────────────────────


def test_furgovw_perros_positivo():
    raw = {"body": "Agua: si\nPerros permitidos\nElectricidad: no"}
    out = extract_furgovw(raw)
    assert out["perros"] is True


def test_furgovw_perros_negativo():
    raw = {"body": "No se admiten perros en este lugar."}
    out = extract_furgovw(raw)
    assert out["perros"] is False


def test_furgovw_wifi():
    raw = {"body": "Hay wifi gratuito para los campistas."}
    out = extract_furgovw(raw)
    assert out["wifi"] is True


def test_furgovw_sin_wifi():
    raw = {"body": "Sin wifi en este área."}
    out = extract_furgovw(raw)
    assert out["wifi"] is False


def test_furgovw_acepta_caravanas():
    raw = {"body": "Caravanas permitidas junto a autocaravanas."}
    out = extract_furgovw(raw)
    assert out["acepta_caravanas"] is True


def test_furgovw_autocaravanas_no_contamina_caravanas():
    """Mencionar autocaravanas NO debe activar acepta_caravanas."""
    raw = {"body": "Autocaravanas bienvenidas. Furgonetas sí."}
    out = extract_furgovw(raw)
    assert "acepta_caravanas" not in out


def test_furgovw_body_none():
    out = extract_furgovw({"body": None})
    assert isinstance(out, dict)


# ─── thedyrt ──────────────────────────────────────────────────────────


def test_thedyrt_electricidad():
    raw = {"attributes": {"electric-hookups": True}}
    out = extract_thedyrt(raw)
    assert out["electricidad"] is True


def test_thedyrt_municipio_from_nearest_city():
    raw = {"attributes": {"nearest-city-name": "Moab"}}
    out = extract_thedyrt(raw)
    assert out["municipio"] == "Moab"


def test_thedyrt_acceso_dificil_dirt():
    raw = {"attributes": {"access-road": "dirt"}}
    out = extract_thedyrt(raw)
    assert out["acceso_dificil"] is True


def test_thedyrt_acceso_facil_paved():
    raw = {"attributes": {"access-road": "paved"}}
    out = extract_thedyrt(raw)
    assert out["acceso_dificil"] is False


def test_thedyrt_servicios_extras():
    raw = {
        "attributes": {
            "max-vehicle-length": 35,
            "cell-service": True,
            "campfire": False,
            "max-nights": 14,
        }
    }
    out = extract_thedyrt(raw)
    se = out["servicios_extras"]
    assert se["max_vehicle_length_ft"] == 35
    assert se["cell_service"] is True
    assert se["campfire"] is False
    assert se["max_nights"] == 14


def test_thedyrt_empty_attrs():
    out = extract_thedyrt({"attributes": {}})
    assert isinstance(out, dict)
    assert "servicios_extras" not in out


def test_thedyrt_no_attrs_key():
    out = extract_thedyrt({})
    assert isinstance(out, dict)


# ─── alpacacamping ────────────────────────────────────────────────────


def _alpaca_raw(titles: list[str], **extra_fields) -> dict:
    """Helper: construye raw con amenities_infos.title."""
    d = {"amenities_infos": {"title": titles}}
    d.update(extra_fields)
    return d


def test_alpaca_basic_v4c_columns():
    raw = _alpaca_raw([
        "Waschmaschine", "Kinderfreundlich", "Bikepark in der Nähe",
        "Für Wanderfreunde", "Für Angler", "Wintercamping",
        "Tolle Aussicht ",
    ])
    out = extract_alpacacamping(raw)
    assert out["lavanderia"] is True
    assert out["juegos_ninos"] is True
    assert out["mtb_friendly"] is True
    assert out["hiking_nearby"] is True
    assert out["fishing"] is True
    assert out["winter_friendly"] is True
    assert out["mirador"] is True


def test_alpaca_acceso_dificil_priority():
    """Erfordert Allrad gana sobre Stellplatz befestigt."""
    raw1 = _alpaca_raw(["Erfordert Allrad"])
    assert extract_alpacacamping(raw1)["acceso_dificil"] is True
    raw2 = _alpaca_raw(["Stellplatz befestigt"])
    assert extract_alpacacamping(raw2)["acceso_dificil"] is False
    raw3 = _alpaca_raw(["Erfordert Allrad", "Stellplatz befestigt"])
    out3 = extract_alpacacamping(raw3)
    assert out3["acceso_dificil"] is True  # 4WD wins
    assert out3["servicios_extras"]["ground"] == "paved"
    assert out3["servicios_extras"]["requires_4wd"] is True


def test_alpaca_municipio_from_address():
    raw = _alpaca_raw(["Wohnmobil & Van"], property_address={"city": "Markt Nordheim"})
    out = extract_alpacacamping(raw)
    assert out["municipio"] == "Markt Nordheim"


def test_alpaca_environment_labels():
    raw = _alpaca_raw(["Auf einer Wiese", "Am Wald", "Direkt am Wasser",
                       "Im Weinberg", "Alleinlage"])
    labels = extract_alpacacamping(raw)["servicios_extras"]["environment_labels"]
    assert set(labels) == {"meadow", "forest", "on_water", "vineyard", "secluded"}


def test_alpaca_vineyard_from_two_sources():
    """Tanto 'Im Weinberg' como 'Beim Winzer' mapean a vineyard sin duplicar."""
    raw = _alpaca_raw(["Im Weinberg", "Beim Winzer / Weingut"])
    labels = extract_alpacacamping(raw)["servicios_extras"]["environment_labels"]
    assert labels == ["vineyard"]


def test_alpaca_vibes():
    raw = _alpaca_raw(["Romantisch", "Gemütlich", "WOW-Faktor", "Natur pur ",
                       "Sterne gucken", "Glamping"])
    vibes = extract_alpacacamping(raw)["servicios_extras"]["vibes"]
    assert "romantic" in vibes
    assert "cozy" in vibes
    assert "wow_factor" in vibes
    assert "pure_nature" in vibes
    assert "starry_sky" in vibes
    assert "glamping" in vibes


def test_alpaca_audience():
    raw = _alpaca_raw(["Für Abenteurer", "Für Weinliebhaber", "Familienfreundlich",
                       "Gemeinsam verreisen"])
    aud = extract_alpacacamping(raw)["servicios_extras"]["audience"]
    assert set(aud) == {"adventurers", "wine_lovers", "families", "groups"}


def test_alpaca_audience_families_dedup():
    """'Familienfreundlich' y 'Kinderfreundlich' apuntan a families (no duplica)."""
    raw = _alpaca_raw(["Familienfreundlich", "Kinderfreundlich"])
    aud = extract_alpacacamping(raw)["servicios_extras"]["audience"]
    assert aud == ["families"]


def test_alpaca_onsite_animals():
    raw = _alpaca_raw(["Alpakas vor Ort", "Pferde vor Ort", "Tiere vor Ort", "Hofladen"])
    se = extract_alpacacamping(raw)["servicios_extras"]
    assert se["alpacas_onsite"] is True
    assert se["horse_onsite"] is True
    assert se["animals_onsite"] is True
    assert se["farm_shop"] is True


def test_alpaca_campfire_negative_overrides_positive():
    """'kein Lagerfeuer' debe ganar sobre 'Feuerstelle'."""
    raw = _alpaca_raw(["Feuerstelle", "kein Lagerfeuer"])
    se = extract_alpacacamping(raw)["servicios_extras"]
    assert se["campfire"] is False


def test_alpaca_bbq_negative_overrides_positive():
    raw = _alpaca_raw(["Holzkohlegrill erlaubt", "kein Grillen"])
    se = extract_alpacacamping(raw)["servicios_extras"]
    assert se["bbq"] is False


def test_alpaca_facilities_to_extras():
    raw = _alpaca_raw(["Küche", "Sitzplätze im Freien", "Auto am Standplatz erlaubt",
                       "Kostenloser Parkplatz", "Brennholz verfügbar",
                       "Keine Schmutzwasserentsorgung", "keine Früchte Ernten"])
    se = extract_alpacacamping(raw)["servicios_extras"]
    assert se["kitchen"] is True
    assert se["outdoor_seating"] is True
    assert se["car_at_pitch"] is True
    assert se["free_parking"] is True
    assert se["firewood_available"] is True
    assert se["no_grey_water_disposal"] is True
    assert se["no_fruit_picking"] is True


def test_alpaca_capacity_and_area():
    raw = _alpaca_raw([], accommodates=4, space_amount=80.0, bedrooms=2, bathrooms=1)
    se = extract_alpacacamping(raw)["servicios_extras"]
    assert se["max_persons"] == 4
    assert se["area_sqm"] == 80.0
    assert se["bedrooms"] == 2
    assert se["bathrooms"] == 1


def test_alpaca_booking_type():
    raw = _alpaca_raw([], booking_type="instant")
    assert extract_alpacacamping(raw)["servicios_extras"]["booking_type"] == "instant"


def test_alpaca_empty_raw():
    assert extract_alpacacamping({}) == {}
    assert extract_alpacacamping(None) == {}
    assert extract_alpacacamping({"amenities_infos": None}) == {}


def test_alpaca_normalize_emits_v4c():
    from sources.alpacacamping import AlpacaCampingSource
    raw = {
        "id": 9999,
        "name": "Bauernhof in Bayern",
        "property_address": {
            "latitude": 49.5,
            "longitude": 11.0,
            "city": "Nuremberg",
            "country": "DE",
            "state": "Bayern",
        },
        "amenities_infos": {
            "id": [14, 13, 16, 17, 25, 41],
            "title": ["Wasser", "Strom", "WC", "Dusche", "Wohnmobil & Van",
                      "Hunde willkommen", "Waschmaschine", "Auf einer Wiese",
                      "Pferde vor Ort", "Feuerstelle", "Tolle Aussicht ",
                      "Für Wanderfreunde", "Familienfreundlich"],
        },
        "accommodates": 6,
        "space_amount": 120.0,
        "booking_type": "instant",
        "photos": [],
    }
    out = AlpacaCampingSource().normalize(raw)
    assert out is not None
    # normalize() canónicos por id
    assert out["agua_potable"] is True
    assert out["electricidad"] is True
    # del extractor
    assert out["lavanderia"] is True
    assert out["hiking_nearby"] is True
    assert out["juegos_ninos"] is True
    assert out["mirador"] is True
    assert out["municipio"] == "Nuremberg"
    se = out["servicios_extras"]
    assert "meadow" in se["environment_labels"]
    assert "families" in se["audience"]
    assert se["horse_onsite"] is True
    assert se["campfire"] is True
    assert se["max_persons"] == 6
    assert se["area_sqm"] == 120.0


# ─── roadsurfer ───────────────────────────────────────────────────────


def test_roadsurfer_facilities_to_v4c():
    raw = {"facilities": ["washingMachine", "childrenPlaygrounds", "swimmingPool",
                          "handicappedAccessible", "bicycleCellar", "looseUnderground"]}
    out = extract_roadsurfer(raw)
    assert out["lavanderia"] is True
    assert out["juegos_ninos"] is True
    assert out["piscina"] is True
    assert out["accesibilidad_reducida"] is True
    assert out["mtb_friendly"] is True
    assert out["acceso_dificil"] is True


def test_roadsurfer_activities_to_v4c():
    raw = {"activities": ["hikingTrails", "fishing", "climbing", "surfingSailing",
                          "sup", "barRestaurant"]}
    out = extract_roadsurfer(raw)
    assert out["hiking_nearby"] is True
    assert out["fishing"] is True
    assert out["climbing"] is True
    assert out["surf_friendly"] is True
    assert out["restaurant"] is True


def test_roadsurfer_environment_labels():
    raw = {"placeSituations": ["forest", "farm", "seaCoast", "country", "garden"]}
    labels = extract_roadsurfer(raw)["servicios_extras"]["environment_labels"]
    assert set(labels) == {"forest", "farm", "sea_coast", "countryside", "garden"}


def test_roadsurfer_activities_split():
    raw = {"activities": ["hikingTrails", "fishing", "cultural", "wellbeing",
                          "skiing", "boat"]}
    extras_acts = extract_roadsurfer(raw)["servicios_extras"]["activities"]
    # v4c activities filtradas
    assert "hikingTrails" not in extras_acts
    assert "fishing" not in extras_acts
    # resto va a extras
    assert "cultural" in extras_acts
    assert "wellbeing" in extras_acts
    assert "skiing" in extras_acts
    assert "boat" in extras_acts


def test_roadsurfer_facilities_to_extras():
    raw = {"facilities": ["campfire", "grillPlace", "picnicTable", "fridge",
                          "shops", "trainOrBus", "fkk", "stable", "horse",
                          "closedArea", "parking", "separateEntrance"]}
    se = extract_roadsurfer(raw)["servicios_extras"]
    assert se["campfire"] is True
    assert se["bbq"] is True
    assert se["picnic_table"] is True
    assert se["fridge"] is True
    assert se["shops_nearby"] is True
    assert se["public_transit"] is True
    assert se["naturism"] is True
    assert se["stable"] is True
    assert se["horse_onsite"] is True
    assert se["closed_area"] is True
    assert se["parking"] is True
    assert se["separate_entrance"] is True


def test_roadsurfer_ground_priority_concrete_over_lawn():
    """Si hay concreteFloorSpace, gana sobre lawn/loose."""
    raw = {"facilities": ["concreteFloorSpace", "lawnArea"]}
    assert extract_roadsurfer(raw)["servicios_extras"]["ground"] == "concrete"
    raw2 = {"facilities": ["looseUnderground", "lawnArea"]}
    assert extract_roadsurfer(raw2)["servicios_extras"]["ground"] == "loose"
    raw3 = {"facilities": ["lawnArea"]}
    assert extract_roadsurfer(raw3)["servicios_extras"]["ground"] == "lawn"


def test_roadsurfer_kitchen_any_variant():
    raw1 = {"facilities": ["kitchen"]}
    raw2 = {"facilities": ["separateKitchen"]}
    assert extract_roadsurfer(raw1)["servicios_extras"]["kitchen"] is True
    assert extract_roadsurfer(raw2)["servicios_extras"]["kitchen"] is True


def test_roadsurfer_online_booking_and_municipio():
    raw = {"isBookable": True, "city": "Maissana"}
    out = extract_roadsurfer(raw)
    assert out["online_booking"] is True
    assert out["municipio"] == "Maissana"


def test_roadsurfer_hours_parsing():
    raw = {"checkInFrom": "16:00", "checkInUntil": "", "checkOutFrom": "08:30",
           "checkOutUntil": "10:00"}
    hours = extract_roadsurfer(raw)["servicios_extras"]["hours"]
    # empty checkInUntil queda omitido (no ":" → no se guarda)
    assert hours["check_in_from"] == "16:00"
    assert "check_in_until" not in hours
    assert hours["check_out_from"] == "08:30"
    assert hours["check_out_by"] == "10:00"


def test_roadsurfer_booking_limits():
    raw = {"minNights": 2, "maxNights": 14}
    se = extract_roadsurfer(raw)["servicios_extras"]
    assert se["min_nights"] == 2
    assert se["max_nights"] == 14


def test_roadsurfer_area_and_pitch_location():
    raw = {"areaInQm": 70.0, "pitchLocation": "On a hill with view"}
    se = extract_roadsurfer(raw)["servicios_extras"]
    assert se["area_sqm"] == 70.0
    assert se["pitch_location"] == "On a hill with view"


def test_roadsurfer_labels_and_badge():
    raw = {"labels": ["Most Booked", "Eco-friendly"], "badgeLabel": "Top Pick"}
    se = extract_roadsurfer(raw)["servicios_extras"]
    assert se["labels"] == ["Most Booked", "Eco-friendly"]
    assert se["badge"] == "Top Pick"


def test_roadsurfer_addon_prices_cents_to_euros():
    raw = {"adultAddonPrice": 1500, "childAddonPrice": 750}
    addon = extract_roadsurfer(raw)["servicios_extras"]["addon_prices"]
    assert addon["adult_addon"] == 15.0
    assert addon["child_addon"] == 7.5


def test_roadsurfer_house_rules_link():
    raw = {"houseRulesLink": "https://example.com/rules.pdf"}
    se = extract_roadsurfer(raw)["servicios_extras"]
    assert se["house_rules_link"] == "https://example.com/rules.pdf"
    # Fallback a download
    raw2 = {"houseRulesDownload": "https://example.com/rules2.pdf"}
    assert extract_roadsurfer(raw2)["servicios_extras"]["house_rules_link"] == "https://example.com/rules2.pdf"


def test_roadsurfer_cancellation_policy():
    raw = {"cancellationPolicy": "Free cancellation up to 7 days before arrival"}
    se = extract_roadsurfer(raw)["servicios_extras"]
    assert se["cancellation_policy"] == "Free cancellation up to 7 days before arrival"


def test_roadsurfer_empty_raw():
    assert extract_roadsurfer({}) == {}
    assert extract_roadsurfer(None) == {}


def test_roadsurfer_isbookable_false():
    """isBookable=False NO debe poner online_booking=False (sin info → omit)."""
    raw = {"isBookable": False}
    out = extract_roadsurfer(raw)
    assert "online_booking" not in out


# ─── vansite ──────────────────────────────────────────────────────────


def _vansite_raw(public_data: dict) -> dict:
    """Helper: envuelve un publicData dict en la estructura Transit decoded."""
    return {"~:attributes": {"~:publicData": public_data}}


def test_vansite_basic_v4c_columns():
    raw = _vansite_raw({
        "~:amenities": ["wascher", "playground", "fireplace"],
        "~:activities": ["cycling", "hiking", "fishing", "climbing", "surfing"],
        "~:kfz": ["caravan", "camper", "tent"],
        "~:locationPlace": "Hennef",
    })
    out = extract_vansite(raw)
    assert out["lavanderia"] is True
    assert out["juegos_ninos"] is True
    assert out["mtb_friendly"] is True
    assert out["hiking_nearby"] is True
    assert out["fishing"] is True
    assert out["climbing"] is True
    assert out["surf_friendly"] is True
    assert out["acepta_caravanas"] is True
    assert out["municipio"] == "Hennef"


def test_vansite_allwheeldrive_implies_acceso_dificil():
    raw = _vansite_raw({"~:allWheelDrive": "yes"})
    out = extract_vansite(raw)
    assert out["acceso_dificil"] is True
    assert out["servicios_extras"]["requires_4wd"] is True


def test_vansite_environment_labels():
    raw = _vansite_raw({"~:surroundings": ["forest", "meadow", "lake", "court"]})
    labels = extract_vansite(raw)["servicios_extras"]["environment_labels"]
    # court → yard alias
    assert set(labels) == {"forest", "meadow", "lake", "yard"}


def test_vansite_activities_split():
    raw = _vansite_raw({"~:activities": [
        "hiking", "cycling",  # v4c
        "swimming", "wellness", "wine_tasting", "alpaca",  # extras
    ]})
    out = extract_vansite(raw)
    extras_acts = out["servicios_extras"]["activities"]
    assert "swimming" in extras_acts
    assert "wellness" in extras_acts
    assert "alpaca" in extras_acts
    assert "hiking" not in extras_acts  # ya en v4c column
    assert "cycling" not in extras_acts


def test_vansite_amenities_to_extras():
    raw = _vansite_raw({"~:amenities": ["fireplace", "garbage", "signal"]})
    se = extract_vansite(raw)["servicios_extras"]
    assert se["campfire"] is True
    assert se["trash_disposal"] is True
    assert se["cell_service"] is True


def test_vansite_clock_parsing():
    raw = _vansite_raw({
        "~:earliestArrivalTime": "10_clock",
        "~:latestArrivalTime":   "20_clock",
        "~:latestDepartureTime": "15_clock",
    })
    hours = extract_vansite(raw)["servicios_extras"]["hours"]
    assert hours["check_in_from"] == "10:00"
    assert hours["check_in_until"] == "20:00"
    assert hours["check_out_by"] == "15:00"


def test_vansite_booking_min_max():
    raw = _vansite_raw({"~:bookingMinimum": "min_2", "~:bookingLimit": "limit_5"})
    se = extract_vansite(raw)["servicios_extras"]
    assert se["min_nights"] == 2
    assert se["max_nights"] == 5


def test_vansite_booking_limit_bigger_means_no_max():
    """'limit_bigger_4' = sin tope; NO debe guardar max_nights."""
    raw = _vansite_raw({"~:bookingLimit": "limit_bigger_4"})
    se = extract_vansite(raw).get("servicios_extras", {})
    assert "max_nights" not in se


def test_vansite_booking_limit_as_list():
    """bookingLimit puede venir como list ['limit_3']."""
    raw = _vansite_raw({"~:bookingLimit": ["limit_3"]})
    assert extract_vansite(raw)["servicios_extras"]["max_nights"] == 3


def test_vansite_verified_true_only():
    """verified=1 o True → guardado; 0/False → no se guarda."""
    assert extract_vansite(_vansite_raw({"~:verified": 1}))["servicios_extras"]["verified"] is True
    assert extract_vansite(_vansite_raw({"~:verified": True}))["servicios_extras"]["verified"] is True
    assert "verified" not in extract_vansite(_vansite_raw({"~:verified": 0})).get("servicios_extras", {})


def test_vansite_self_sufficient_list():
    raw = _vansite_raw({"~:selfSufficient": [True]})
    assert extract_vansite(raw)["servicios_extras"]["self_sufficient_required"] is True
    raw2 = _vansite_raw({"~:selfSufficient": [False]})
    assert extract_vansite(raw2)["servicios_extras"]["self_sufficient_required"] is False


def test_vansite_tent_friendly_from_kfz():
    raw = _vansite_raw({"~:kfz": ["tent"]})
    assert extract_vansite(raw)["servicios_extras"]["tent_friendly"] is True
    raw2 = _vansite_raw({"~:kfz": ["carTent"]})
    assert extract_vansite(raw2)["servicios_extras"]["tent_friendly"] is True


def test_vansite_cancellation_policy():
    raw = _vansite_raw({"~:cancellationPolicyTier": "flexible_24h"})
    assert extract_vansite(raw)["servicios_extras"]["cancellation_policy"] == "flexible_24h"


def test_vansite_empty_raw():
    assert extract_vansite({}) == {}
    assert extract_vansite({"~:attributes": {}}) == {}
    assert extract_vansite({"~:attributes": {"~:publicData": {}}}) == {}


def test_vansite_malformed_publicdata():
    """publicData no-dict no debe petar."""
    raw = {"~:attributes": {"~:publicData": "broken"}}
    assert extract_vansite(raw) == {}


def test_vansite_normalize_emits_v4c():
    from sources.vansite import VansiteSource
    raw = {
        "~:id": "~uabc123",
        "~:attributes": {
            "~:title": "Bauernhof am See",
            "~:description": "Quiet farm by the lake.",
            "~:geolocation": ["~:geolocation", [50.4, 7.5]],
            "~:price": ["~:m", [1500, "EUR"]],
            "~:publicData": {
                "~:amenities": ["electricity", "water", "wascher", "fireplace", "signal", "playground"],
                "~:activities": ["hiking", "cycling", "swimming", "wellness"],
                "~:surroundings": ["forest", "meadow", "lake"],
                "~:kfz": ["camper", "caravan", "tent"],
                "~:locationPlace": "Bonn",
                "~:verified": 1,
                "~:selfSufficient": [True],
                "~:allWheelDrive": "no",
                "~:earliestArrivalTime": "14_clock",
                "~:latestDepartureTime": "11_clock",
                "~:bookingMinimum": "min_1",
                "~:bookingLimit": "limit_4",
                "~:category": "campsite",
                "~:amountOfSeats": 5,
            },
        },
    }
    out = VansiteSource().normalize(raw)
    assert out is not None
    # campos de normalize()
    assert out["electricidad"] is True
    assert out["agua_potable"] is True
    assert out["acceso_grandes"] is True  # de kfz
    assert out["num_plazas"] == 5
    # de extractor
    assert out["lavanderia"] is True
    assert out["juegos_ninos"] is True
    assert out["mtb_friendly"] is True
    assert out["hiking_nearby"] is True
    assert out["acepta_caravanas"] is True
    assert out["municipio"] == "Bonn"
    se = out["servicios_extras"]
    assert "lake" in se["environment_labels"]
    assert "swimming" in se["activities"]
    assert se["campfire"] is True
    assert se["cell_service"] is True
    assert se["verified"] is True
    assert se["self_sufficient_required"] is True
    assert se["min_nights"] == 1
    assert se["max_nights"] == 4
    assert se["hours"]["check_in_from"] == "14:00"
    assert se["hours"]["check_out_by"] == "11:00"


# ─── campspace ────────────────────────────────────────────────────────


def test_campspace_swimming_pool_and_playground():
    raw = {"amenities": ["Swimming pool", "Playground for children", "Toilet"]}
    out = extract_campspace(raw)
    assert out["piscina"] is True
    assert out["juegos_ninos"] is True


def test_campspace_mirador_any_view():
    raw = {"amenities": ["Sunset view", "BBQ"]}
    out = extract_campspace(raw)
    assert out["mirador"] is True


def test_campspace_barrier_free_accessibility():
    raw = {"amenities": ["Barrier-free access to pitch", "Toilet"]}
    out = extract_campspace(raw)
    assert out["accesibilidad_reducida"] is True


def test_campspace_bike_storage_or_cycling():
    """Bicycle storage en amenities O cycling en surroundings → mtb_friendly."""
    out1 = extract_campspace({"amenities": ["Bicycle storage"]})
    out2 = extract_campspace({"surroundings": ["Cycling"]})
    assert out1["mtb_friendly"] is True
    assert out2["mtb_friendly"] is True


def test_campspace_surroundings_activities():
    raw = {"surroundings": ["Hiking", "Fishing", "Climbing", "Surfing"]}
    out = extract_campspace(raw)
    assert out["hiking_nearby"] is True
    assert out["fishing"] is True
    assert out["climbing"] is True
    assert out["surf_friendly"] is True


def test_campspace_environment_labels():
    raw = {"surroundings": ["Forest", "Meadow or plain", "Beach or seaside", "Urban area"]}
    labels = extract_campspace(raw)["servicios_extras"]["environment_labels"]
    assert set(labels) == {"forest", "meadow", "beach", "urban"}


def test_campspace_activities_in_extras():
    raw = {"surroundings": ["Swimming", "Canoeing or kayaking", "Making a campfire", "Horseback riding"]}
    activities = extract_campspace(raw)["servicios_extras"]["activities"]
    assert "swimming" in activities
    assert "canoeing" in activities
    assert "campfire" in activities
    assert "horseback_riding" in activities


def test_campspace_farm_animals():
    raw = {"surroundings": ["Cows", "Sheep", "Horses or ponies"]}
    fa = extract_campspace(raw)["servicios_extras"]["farm_animals"]
    assert set(fa) == {"cows", "sheep", "horses"}


def test_campspace_campfire_signals():
    raw = {"amenities": ["Fireplace", "Firewood available"]}
    se = extract_campspace(raw)["servicios_extras"]
    assert se["campfire"] is True


def test_campspace_kitchen_signals():
    raw = {"amenities": ["Refrigerator", "Gas stove"]}
    se = extract_campspace(raw)["servicios_extras"]
    assert se["kitchen"] is True


def test_campspace_rv_signals():
    raw = {"amenities": ["RV electricity", "RV Dump station"]}
    se = extract_campspace(raw)["servicios_extras"]
    assert se["rv_hookup"] is True
    assert se["rv_dump"] is True


def test_campspace_glamping_linen():
    raw = {"amenities": ["Bed linen, pillow and duvet", "Towels"]}
    se = extract_campspace(raw)["servicios_extras"]
    assert se["glamping_linen"] is True


def test_campspace_pricing_from_phase1():
    raw = {"price": "from £ 22.50"}
    pb = extract_campspace(raw)["servicios_extras"]["pricing_breakdown"]
    assert pb["from"] == 22.5
    assert pb["currency"] == "£"


def test_campspace_empty_raw():
    assert extract_campspace({}) == {}
    assert extract_campspace({"amenities": [], "surroundings": []}) == {}


def test_campspace_normalize_phase1_noop_pricing():
    """Phase 1 normalize() pasa por extract_campspace pero raw mínimo: solo pricing."""
    from sources.campspace import CampspaceSource
    raw = {"id": 99, "lat": 50.0, "lng": 4.0, "title": "X", "price": "from € 30"}
    out = CampspaceSource().normalize(raw)
    assert out is not None
    # piscina/etc no aparecen porque no hay amenities en Phase 1 raw
    assert "piscina" not in out
    # pero servicios_extras.pricing_breakdown sí se rellena
    assert out["servicios_extras"]["pricing_breakdown"]["from"] == 30.0


# ─── nomady ───────────────────────────────────────────────────────────


def test_nomady_basic_flags():
    raw = {
        "wifi": True,
        "dogsAllowed": True,
        "washingMachine": False,
        "directBookings": True,
    }
    out = extract_nomady(raw)
    assert out["wifi"] is True
    assert out["perros"] is True
    assert out["lavanderia"] is False
    assert out["online_booking"] is True


def test_nomady_activities_split():
    """Algunas activities mapean a v4c, otras a servicios_extras.activities."""
    raw = {
        "activitiesBiking": True,
        "activitiesHiking": True,
        "activitiesFishing": False,
        "activitiesClimbing": True,
        "activitiesSwimming": True,
        "activitiesSkiing": True,
        "activitiesSUP": False,
    }
    out = extract_nomady(raw)
    assert out["mtb_friendly"] is True
    assert out["hiking_nearby"] is True
    assert out["fishing"] is False
    assert out["climbing"] is True
    activities = out["servicios_extras"]["activities"]
    assert "swimming" in activities
    assert "skiing" in activities
    assert "sup" not in activities


def test_nomady_environment_labels():
    raw = {
        "surroundingsForest": True,
        "surroundingsLake": True,
        "surroundingsMeadow": False,
        "surroundingsRoad": True,
    }
    labels = extract_nomady(raw)["servicios_extras"]["environment_labels"]
    assert "forest" in labels
    assert "lake" in labels
    assert "near_road" in labels
    assert "meadow" not in labels


def test_nomady_food_nearby_and_restaurant_column():
    raw = {
        "foodRestaurant": True,
        "foodCafe": True,
        "foodBakery": False,
        "foodFarmShop": True,
    }
    out = extract_nomady(raw)
    assert out["restaurant"] is True
    food = out["servicios_extras"]["food_nearby"]
    assert set(food) == {"restaurant", "cafe", "farm_shop"}


def test_nomady_pricing_breakdown():
    raw = {
        "priceAdult": 12.5,
        "priceChild": 6.0,
        "priceDog": 5,
        "priceInfant": 0,  # 0 → excluido
        "pricePower": 3.5,
        "priceBaseUnit": "per_night",
    }
    pb = extract_nomady(raw)["servicios_extras"]["pricing_breakdown"]
    assert pb["adult"] == 12.5
    assert pb["child"] == 6.0
    assert pb["dog"] == 5.0
    assert pb["electricity"] == 3.5
    assert pb["base_unit"] == "per_night"
    assert "infant" not in pb


def test_nomady_stay_limits():
    raw = {"minNights": 2, "maxNights": 14}
    se = extract_nomady(raw)["servicios_extras"]
    assert se["min_nights"] == 2
    assert se["max_nights"] == 14


def test_nomady_municipio_and_winter():
    raw = {"city": "Andermatt", "isWinterReady": True}
    out = extract_nomady(raw)
    assert out["municipio"] == "Andermatt"
    assert out["winter_friendly"] is True


def test_nomady_only_truthy_flags():
    raw = {
        "isVerified": True,
        "popularityAward": False,
        "isFamilyFriendly": True,
        "dogsOnlyOnleash": True,
    }
    se = extract_nomady(raw)["servicios_extras"]
    assert se["verified"] is True
    assert "popularity_award" not in se  # solo si truthy
    assert se["family_friendly"] is True
    assert se["dogs_on_leash_only"] is True


def test_nomady_flag_returns_none_when_absent():
    """Si la clave no está, _flag debe devolver None y la columna se omite."""
    out = extract_nomady({"city": "x"})
    assert "wifi" not in out
    assert "perros" not in out


def test_nomady_empty_raw():
    assert extract_nomady({}) == {}
    assert extract_nomady(None) == {}


def test_nomady_normalize_emits_v4c():
    from sources.nomady import NomadySource
    raw = {
        "id": 7890,
        "title": "Alphütte Engelberg",
        "latitude": 46.82,
        "longitude": 8.40,
        "country": "ch",
        "slug": "alphuette-engelberg",
        "types": ["tent", "caravan"],
        "drinkingWater": True,
        "regularToilet": True,
        "outdoorShower": True,
        "power": True,
        "wifi": False,
        "dogsAllowed": True,
        "isWinterReady": True,
        "directBookings": True,
        "city": "Engelberg",
        "activitiesHiking": True,
        "activitiesBiking": True,
        "surroundingsForest": True,
        "surroundingsLake": False,
        "priceAdult": 18,
        "priceDog": 5,
        "minNights": 1,
        "maxNights": 7,
        "fireplace": True,
        "isVerified": True,
        "ground": "grass",
        "imageUrls": ["a.jpg", "b.jpg"],
    }
    out = NomadySource().normalize(raw)
    assert out is not None
    # campos de normalize()
    assert out["agua_potable"] is True
    assert out["country_iso"] == "ch"
    # de extractor
    assert out["wifi"] is False
    assert out["perros"] is True
    assert out["municipio"] == "Engelberg"
    assert out["winter_friendly"] is True
    assert out["mtb_friendly"] is True
    assert out["hiking_nearby"] is True
    se = out["servicios_extras"]
    assert "forest" in se["environment_labels"]
    assert se["pricing_breakdown"]["adult"] == 18.0
    assert se["min_nights"] == 1 and se["max_nights"] == 7
    assert se["campfire"] is True
    assert se["verified"] is True
    assert se["ground"] == "grass"


# ─── wtmg ─────────────────────────────────────────────────────────────


def _wtmg_raw(facilities: dict) -> dict:
    """Helper: envuelve un dict de facilities en el formato Firestore."""
    def wrap_bool(v):
        return {"booleanValue": v}
    def wrap_int(v):
        return {"integerValue": str(v)}
    fac_fields = {}
    for k, v in facilities.items():
        if isinstance(v, bool):
            fac_fields[k] = wrap_bool(v)
        elif isinstance(v, int):
            fac_fields[k] = wrap_int(v)
    return {
        "document": {
            "name": "projects/wtmg-production/databases/(default)/documents/campsites/abc123",
            "fields": {
                "facilities": {"mapValue": {"fields": fac_fields}},
            },
        }
    }


def test_wtmg_campfire_from_bonfire_true():
    raw = _wtmg_raw({"bonfire": True, "tent": False})
    out = extract_wtmg(raw)
    assert out["servicios_extras"]["campfire"] is True
    assert out["servicios_extras"]["tent_allowed"] is False


def test_wtmg_campfire_from_bonfire_false():
    raw = _wtmg_raw({"bonfire": False})
    out = extract_wtmg(raw)
    assert out["servicios_extras"]["campfire"] is False


def test_wtmg_tent_allowed():
    raw = _wtmg_raw({"tent": True})
    out = extract_wtmg(raw)
    assert out["servicios_extras"]["tent_allowed"] is True


def test_wtmg_no_relevant_facilities():
    """Si facilities sólo trae cosas ya cubiertas por normalize(), extra={}."""
    raw = _wtmg_raw({"toilet": True, "shower": False, "capacity": 2})
    out = extract_wtmg(raw)
    assert out == {}


def test_wtmg_empty_raw():
    assert extract_wtmg({}) == {}
    assert extract_wtmg({"document": {}}) == {}
    assert extract_wtmg({"document": {"fields": {}}}) == {}


def test_wtmg_malformed_facilities():
    """facilities sin mapValue → no peta, devuelve {}."""
    raw = {"document": {"fields": {"facilities": {"stringValue": "broken"}}}}
    assert extract_wtmg(raw) == {}


def test_wtmg_normalize_emits_v4c():
    from sources.wtmg import WelcomeToMyGardenSource
    raw = {
        "document": {
            "name": "projects/wtmg-production/databases/(default)/documents/campsites/abc",
            "fields": {
                "location": {"mapValue": {"fields": {
                    "latitude": {"doubleValue": 50.1},
                    "longitude": {"doubleValue": 4.2},
                }}},
                "listed": {"booleanValue": True},
                "facilities": {"mapValue": {"fields": {
                    "drinkableWater": {"booleanValue": True},
                    "toilet": {"booleanValue": True},
                    "shower": {"booleanValue": False},
                    "electricity": {"booleanValue": True},
                    "bonfire": {"booleanValue": True},
                    "tent": {"booleanValue": True},
                    "capacity": {"integerValue": "3"},
                }}},
                "description": {"stringValue": "A quiet garden with welcoming hosts."},
            },
        }
    }
    out = WelcomeToMyGardenSource().normalize(raw)
    assert out is not None
    assert out["agua_potable"] is True
    assert out["wc_publico"] is True
    assert out["electricidad"] is True
    assert out["num_plazas"] == 3
    se = out["servicios_extras"]
    assert se["campfire"] is True
    assert se["tent_allowed"] is True


def test_thedyrt_normalize_emits_v4c():
    from sources.thedyrt import TheDyrtSource
    raw = {
        "id": "abc123",
        "attributes": {
            "location-id": "abc123",
            "name": "Arches Dispersed",
            "latitude": 38.68,
            "longitude": -109.59,
            "category": "dispersed",
            "price-low": 0,
            "rating": 4.5,
            "reviews-count": 120,
            "electric-hookups": False,
            "nearest-city-name": "Moab",
            "access-road": "gravel",
            "big-rig-friendly": False,
            "cell-service": False,
            "campfire": True,
            "max-nights": 7,
        },
    }
    out = TheDyrtSource().normalize(raw)
    assert out is not None
    assert out["tipo"] == "wild"
    assert out["electricidad"] is False
    assert out["municipio"] == "Moab"
    assert out["acceso_dificil"] is True
    se = out["servicios_extras"]
    assert se["cell_service"] is False
    assert se["campfire"] is True
    assert se["max_nights"] == 7
