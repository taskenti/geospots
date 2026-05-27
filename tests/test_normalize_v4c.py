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
    extract_campercontact,
    extract_campercontact_detail,
    extract_campingcarpark,
    extract_campy,
    extract_camperstop,
    extract_caramaps,
    extract_park4night,
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
