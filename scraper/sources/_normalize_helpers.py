"""Helpers compartidos para extraer servicios extra v4c desde raw_data.

Single source of truth para la lógica que también usa
`jobs/backfill_extra_services.py`. Cada scraper de los 6 que tienen raw_data
rico (park4night, campingcarpark, agricamper, caramaps, campy, campercontact)
importa el extractor correspondiente y lo mergea sobre su dict normalize().

Si cambias la lógica aquí, también cambia automáticamente lo que persisten los
nuevos scrapes — el backfill queda únicamente como herramienta de catch-up
para spots ya escritos antes de PR 8e.
"""

from __future__ import annotations

from typing import Any


# ─── Coercers ────────────────────────────────────────────────────────


def _bool(v: Any) -> bool | None:
    """Acepta los formatos que vemos en raw_data: '0','1','','None','true','false',
    True, False, 0, 1, None.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if v == 0:
            return False
        if v == 1:
            return True
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("", "none", "null", "nc"):
            return None
        if s in ("1", "true", "yes", "si", "sí"):
            return True
        if s in ("0", "false", "no"):
            return False
    return None


def _bool_any(*values) -> bool | None:
    """OR sobre múltiples valores. True si CUALQUIERA es True; False si TODOS
    son False; None si no info.
    """
    result = None
    for v in values:
        b = _bool(v)
        if b is True:
            return True
        if b is False:
            result = False
    return result


def _int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        i = int(v)
        return i if i > 0 else None
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in ("none", "null"):
            return None
        try:
            i = int(float(s))
            return i if i > 0 else None
        except ValueError:
            return None
    return None


def _str_nonempty(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in ("none", "null", "nc"):
            return None
        return s
    return None


def _lang_iso(label: str) -> str | None:
    """Mapea "English" / "Italian" / etc → 'en' / 'it'."""
    if not label:
        return None
    LANGS = {
        "english": "en", "italian": "it", "spanish": "es", "french": "fr",
        "german": "de", "dutch": "nl", "portuguese": "pt",
        "polish": "pl", "romanian": "ro", "czech": "cs", "swedish": "sv",
        "norwegian": "no", "finnish": "fi", "danish": "da", "russian": "ru",
        "greek": "el", "hungarian": "hu", "turkish": "tr",
    }
    return LANGS.get(label.strip().lower())


def _has(text_list: list[Any], *needles: str) -> bool | None:
    """¿Alguna entrada contiene alguno de los needles (case-insensitive)?"""
    if not text_list:
        return None
    lower = [str(t).lower() for t in text_list if t]
    if not lower:
        return None
    for needle in needles:
        n = needle.lower()
        if any(n in t for t in lower):
            return True
    return False


# ─── Extractors por fuente ───────────────────────────────────────────


def extract_park4night(raw: dict) -> dict:
    """park4night usa strings '0'/'1' para booleanos."""
    if not isinstance(raw, dict):
        return {}
    out = {
        "piscina":         _bool(raw.get("piscine")),
        "lavanderia":      _bool_any(raw.get("lavage"), raw.get("laverie")),
        "gas_recharge":    _bool_any(raw.get("gaz"), raw.get("gpl")),
        "juegos_ninos":    _bool(raw.get("jeux_enfants")),
        "mirador":         _bool(raw.get("point_de_vue")),
        "zona_protegida":  _bool(raw.get("nature_protect")),
        "online_booking":  _bool(raw.get("online_booking")),
        "winter_friendly": _bool(raw.get("caravaneige")),
        "apto_motos":      _bool(raw.get("moto")),
        "mtb_friendly":    _bool(raw.get("vtt")),
        "surf_friendly":   _bool_any(raw.get("windsurf"), raw.get("eaux_vives")),
        "fishing":         _bool_any(raw.get("peche"), raw.get("peche_pied")),
        "climbing":        _bool(raw.get("escalade")),
        "hiking_nearby":   _bool(raw.get("rando")),
    }

    extras: dict = {}
    pricing = {}
    ps = _str_nonempty(raw.get("prix_services"))
    pt = _str_nonempty(raw.get("prix_stationnement"))
    if ps:
        pricing["services"] = ps[:80]
    if pt:
        pricing["pernocta"] = pt[:80]
    if pricing:
        extras["pricing_breakdown"] = pricing
    if extras:
        out["servicios_extras"] = extras
    return out


def extract_campingcarpark(raw: dict) -> dict:
    """campingcarpark expone amperaje, prohibitions estructuradas, etc."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {
        "amperaje":        _int(raw.get("amperage")),
        "n_enchufes":      _int(raw.get("electricalOutletCount")),
        "max_noches":      _int(raw.get("maxNightCount")),
        "online_booking":  _bool(raw.get("isBookable")),
        # v4d: securiplace = vigilancia/cámaras. Mapea a la columna `seguridad`
        # que ya existía en spots.
        "seguridad":       _bool(raw.get("securiplace")),
    }

    extras: dict = {}
    prohibs = raw.get("prohibitions")
    if isinstance(prohibs, dict):
        plist = [k for k, v in prohibs.items() if v is True]
        if plist:
            extras["prohibitions"] = plist
    elif isinstance(prohibs, list) and prohibs:
        extras["prohibitions"] = [str(p)[:60] for p in prohibs if p]

    risks = raw.get("risks")
    if isinstance(risks, list) and risks:
        extras["risks"] = [str(r)[:80] for r in risks if r]

    descriptions = {}
    for k_raw, k_out in (
        ("sanitaryDescription", "sanitary"),
        ("surroundingsDescription", "surroundings"),
        ("events", "events"),
        ("specialInfo", "special_info"),
    ):
        v = _str_nonempty(raw.get(k_raw))
        if v:
            descriptions[k_out] = v[:800]
    if descriptions:
        extras["descriptions"] = descriptions

    sano = raw.get("sanitaryOpening")
    if isinstance(sano, dict):
        hours = {}
        from_, to = sano.get("from"), sano.get("to")
        if from_ and to:
            hours["sanitary_opening"] = f"{from_}-{to}"
        if sano.get("toiletCount") and isinstance(sano["toiletCount"], (int, float)) and sano["toiletCount"] > 0:
            hours["toilet_count"] = int(sano["toiletCount"])
        if sano.get("showerCount") and isinstance(sano["showerCount"], (int, float)) and sano["showerCount"] > 0:
            hours["shower_count"] = int(sano["showerCount"])
        if hours:
            extras["hours"] = hours

    tariffs = raw.get("tariffs")
    if isinstance(tariffs, list) and tariffs:
        pricing = {}
        for t in tariffs[:5]:
            if not isinstance(t, dict):
                continue
            label = _str_nonempty(t.get("label") or t.get("name"))
            amount = t.get("amount") or t.get("price")
            if label and amount is not None:
                pricing[label[:40]] = amount
        if pricing:
            extras["pricing_breakdown"] = pricing

    dest = raw.get("destinationTypes")
    if isinstance(dest, list) and dest:
        extras["destination_types"] = [str(d)[:30] for d in dest if d]

    # v4d: tipos de vehículo aceptados — solo guardamos los true
    auth = raw.get("authorizedVehicles")
    if isinstance(auth, dict):
        allowed = sorted(k for k, v in auth.items() if v is True)
        if allowed:
            extras["authorized_vehicles"] = allowed

    # v4d: clientela objetivo (ccowner, truck, van...)
    cust = raw.get("customersProfile")
    if isinstance(cust, list) and cust:
        extras["customer_profile"] = [str(c)[:30] for c in cust if c][:10]

    # v4d: tasa turística → suma a pricing_breakdown.tourist_tax
    taxes = raw.get("touristTaxes")
    if isinstance(taxes, list) and taxes:
        total_tax = 0.0
        for t in taxes:
            if isinstance(t, dict):
                amt = t.get("amount")
                if isinstance(amt, (int, float)):
                    total_tax += float(amt)
        if total_tax > 0:
            extras.setdefault("pricing_breakdown", {})["tourist_tax"] = round(total_tax, 2)

    # v4d: labels de calidad (Ville d'art et histoire, Jardin remarquable...)
    labels = raw.get("labels")
    if isinstance(labels, dict):
        current = labels.get("currentLabels") or []
        if isinstance(current, list):
            titles = [_str_nonempty((l or {}).get("title")) for l in current]
            titles = [t for t in titles if t]
            if titles:
                extras["quality_labels"] = titles[:10]

    # v4d: descripciones narrativas adicionales
    descs = extras.setdefault("descriptions", {})
    for k_raw, k_out in (
        ("benefits", "benefits"),
        ("shops", "shops"),
        ("access", "access"),
    ):
        v = _str_nonempty(raw.get(k_raw))
        if v and k_out not in descs:
            descs[k_out] = v[:800]
    if not descs:
        extras.pop("descriptions", None)

    if extras:
        out["servicios_extras"] = extras
    return out


def extract_agricamper(raw: dict) -> dict:
    """agricamper expone servicios como listas de labels en inglés."""
    if not isinstance(raw, dict):
        return {}

    services = raw.get("fiche_service_label") or []
    products = raw.get("fiche_produit_label") or []
    languages = raw.get("fiche_langue_parlee_label") or []
    position = raw.get("fiche_position_label") or []
    typology = raw.get("fiche_typologie_label") or []

    out: dict = {
        "piscina":         _has(services, "pool", "swim"),
        "restaurant":      _has(services, "restaurant"),
        "lavanderia":      _has(services, "laundry"),
        "juegos_ninos":    _has(services, "playground", "child"),
        "hiking_nearby":   _has(services, "hiking", "trail"),
        "fishing":         _has(services, "fishing"),
        "mtb_friendly":    _has(services, "bike", "cycling"),
        # v4d: campos explícitos del schema agricamper
        "acepta_caravanas":       _bool(raw.get("accepte_caravanes")),
        "accesibilidad_reducida": _bool(raw.get("accepte_handicap")),
        # OR de los 3 flags de condición del camino
        "acceso_dificil":         _bool_any(
            raw.get("est_chemin_difficile"),
            raw.get("est_chemin_pente"),
            raw.get("est_chemin_pierre"),
        ),
        # municipio (ciudad real, mejor granularidad que provincia que vamos a region)
        "municipio":              _str_nonempty(raw.get("adresse_ville")),
    }
    iso_langs = sorted({l for l in (_lang_iso(lab) for lab in languages) if l})
    if iso_langs:
        out["idiomas_hablados"] = iso_langs
    if products:
        out["productos_venta"] = [str(p)[:60] for p in products if p][:10]

    extras: dict = {}
    if typology:
        extras["typology"] = [str(t).lower()[:30] for t in typology if t]
    if position:
        extras["position"] = [str(p).lower()[:30] for p in position if p]
    labels = raw.get("fiche_label_label")
    if isinstance(labels, list) and labels:
        extras["quality_labels"] = [str(l)[:60] for l in labels if l][:10]
    horaire = _str_nonempty(raw.get("horaire_arrivee_label"))
    if horaire:
        extras.setdefault("hours", {})["check_in"] = horaire[:20]
    days = raw.get("jours_fermeture")
    if isinstance(days, list) and days:
        extras.setdefault("hours", {})["days_closed"] = [str(d)[:30] for d in days if d][:50]

    if extras:
        out["servicios_extras"] = extras
    return out


def extract_caramaps(raw: dict) -> dict:
    """caramaps tiene `attributes` con labels variables."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}

    # v4d: email tirado a la basura hasta ahora (100% pop donde existe)
    contact = raw.get("contactInformation")
    if isinstance(contact, dict):
        email = _str_nonempty(contact.get("email"))
        if email:
            out["email"] = email

    # v4d: openingDates → temporada_apertura legible
    opening = raw.get("openingDates")
    if isinstance(opening, list) and opening:
        first = opening[0]
        if isinstance(first, dict):
            start = (first.get("start") or "")[:10]
            end = (first.get("end") or "")[:10]
            if start and end:
                # Si abarca todo el año natural, usar abreviatura
                if start.endswith("-01-01") and end.endswith("-12-31"):
                    out["temporada_apertura"] = "all_year"
                else:
                    out["temporada_apertura"] = f"{start}/{end}"

    attrs = raw.get("attributes") or []
    if isinstance(attrs, list):
        labels = []
        for a in attrs:
            if isinstance(a, dict):
                attr = a.get("attribute") or {}
                lbl = _str_nonempty(attr.get("label"))
                if lbl:
                    labels.append(lbl.lower())
        if labels:
            out["piscina"]       = _has(labels, "piscine", "pool")
            out["restaurant"]    = _has(labels, "restaurant")
            out["lavanderia"]    = _has(labels, "laverie", "laundry")
            out["mirador"]       = _has(labels, "vue", "viewpoint")
            out["hiking_nearby"] = _has(labels, "randonn", "hiking")
            out["mtb_friendly"]  = _has(labels, "vtt", "bike")
            out["surf_friendly"] = _has(labels, "surf", "windsurf")
            out["fishing"]       = _has(labels, "pêche", "fishing")
            out["climbing"]      = _has(labels, "escalad", "climbing")
    return out


def extract_campy(raw: dict) -> dict:
    """campy: facilities list + temporada."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    facilities = raw.get("facilities") or []
    if isinstance(facilities, list) and facilities:
        labels = [str(f).lower() for f in facilities if f]
        out["piscina"]       = _has(labels, "pool", "swim")
        out["restaurant"]    = _has(labels, "restaurant")
        out["lavanderia"]    = _has(labels, "laundry")
        out["juegos_ninos"]  = _has(labels, "playground")
        out["mtb_friendly"]  = _has(labels, "bike", "cycling")
        out["hiking_nearby"] = _has(labels, "hiking")

    # v4d: temporada construida de dateOpenFrom/dateOpenTo
    df = _str_nonempty(raw.get("dateOpenFrom"))
    dt = _str_nonempty(raw.get("dateOpenTo"))
    if df and dt:
        df_d, dt_d = df[:10], dt[:10]
        if df_d.endswith("-01-01") and dt_d.endswith("-12-31"):
            out["temporada_apertura"] = "all_year"
        else:
            out["temporada_apertura"] = f"{df_d}/{dt_d}"

    # v4d: extras secundarios
    extras: dict = {}
    cs = _str_nonempty(raw.get("camperSize"))
    if cs:
        extras["max_camper_size"] = cs[:30]
    if raw.get("isTopQuality") is True:
        extras["top_quality"] = True
    if extras:
        out["servicios_extras"] = extras
    return out


def extract_campercontact(raw: dict) -> dict:
    """campercontact: poco más que ya reconciliamos. priceBreakdown JSONB."""
    if not isinstance(raw, dict):
        return {}
    extras: dict = {}
    pb = raw.get("priceBreakdown")
    if isinstance(pb, dict):
        breakdown = {}
        if pb.get("totalPrice") is not None:
            breakdown["total"] = pb["totalPrice"]
        if pb.get("pricePerNight") is not None:
            breakdown["per_night"] = pb["pricePerNight"]
        if pb.get("fee") is not None:
            breakdown["fee"] = pb["fee"]
        if breakdown:
            extras["pricing_breakdown"] = breakdown
    if extras:
        return {"servicios_extras": extras}
    return {}


def extract_campercontact_detail(poi: dict) -> dict:
    """campercontact: enriquecimiento del flujo de detalle (poiV2).

    Llamado desde campercontact.CamperContactSource._normalize_detail tras la
    extracción de campos canónicos (agua/wifi/etc.). Sólo aporta v4c/v4d:
      - extras.terrain: lista completa de features del terreno (no solo
        illuminated/security que ya van a iluminacion/seguridad como bools)
      - extras.amenity_pricing: dict {water: "free", electricity: "paid", ...}
    """
    if not isinstance(poi, dict):
        return {}
    extras: dict = {}

    terrain = poi.get("terrain")
    if isinstance(terrain, list) and terrain:
        extras["terrain"] = [str(t)[:30] for t in terrain if t][:20]

    amenities = poi.get("amenities")
    if isinstance(amenities, list) and amenities:
        pricing = {}
        for a in amenities:
            if not isinstance(a, dict):
                continue
            t = _str_nonempty(a.get("type"))
            ps = _str_nonempty(a.get("priceStatus"))
            if t and ps:
                pricing[t[:40]] = ps[:20]
        if pricing:
            extras["amenity_pricing"] = pricing

    if extras:
        return {"servicios_extras": extras}
    return {}


def extract_camperstop(raw: dict) -> dict:
    """camperstop: structured services, pricing per amenity, environment labels."""
    if not isinstance(raw, dict):
        return {}

    out: dict = {
        "perros":          _bool(raw.get("animalsAllowed")),
        "winter_friendly": _bool(raw.get("winterSports")),
        "municipio":       _str_nonempty(raw.get("place")),
        "n_enchufes":      _int(raw.get("powerQuantity")),
    }

    extras: dict = {}

    # Tipo de suelo y entorno físico
    gt = _str_nonempty(raw.get("groundType"))
    if gt:
        extras["ground_type"] = gt[:30]

    env = _str_nonempty(raw.get("environment"))
    if env:
        extras["environment"] = env[:30]

    # Precios por servicio (solo los > 0)
    pricing: dict = {}
    for field, label in (
        ("waterPrice",  "water"),
        ("powerPrice",  "electricity"),
        ("showerPrice", "shower"),
        ("toiletPrice", "toilet"),
    ):
        val = raw.get(field)
        if val is not None:
            try:
                f = float(val)
                if f > 0:
                    pricing[label] = round(f, 2)
            except (TypeError, ValueError):
                pass
    sr = _str_nonempty(raw.get("serviceRate"))
    if sr:
        pricing["service_rate"] = sr[:80]
    if pricing:
        extras["pricing_breakdown"] = pricing

    # Tarjeta de crédito y forma de pago
    if _bool(raw.get("creditCard")):
        extras["credit_card"] = True
    pt = _str_nonempty(raw.get("paymentType"))
    if pt:
        extras["payment_type"] = pt[:60]

    # Remarks: notas textuales del operador
    remarks = raw.get("remarks")
    if isinstance(remarks, list) and remarks:
        texts = [str(r)[:200] for r in remarks if r][:10]
        if texts:
            extras.setdefault("descriptions", {})["remarks"] = texts

    # Etiquetas de ambiente/carácter (flags 0/1 del API)
    env_labels = []
    for field, label in (
        ("calm",         "calm"),
        ("loud",         "loud"),
        ("verySimple",   "very_simple"),
        ("comfortable",  "comfortable"),
        ("luxurious",    "luxurious"),
        ("forest",       "forest"),
        ("mountains",    "mountains"),
        ("touristic",    "touristic"),
        ("healthResort", "health_resort"),
        ("shoppingCity", "shopping_city"),
    ):
        if _bool(raw.get(field)):
            env_labels.append(label)
    if env_labels:
        extras["environment_labels"] = env_labels

    if extras:
        out["servicios_extras"] = extras

    return out


def extract_womostell(raw: dict) -> dict:
    """womostell: b_long_campers, b_reservation, city, price → v4c/servicios_extras.

    normalize() ya captura: perros, wifi, electricidad, ducha, agua_potable,
    vaciado_*, wc_publico, acceso_grandes, temporada_apertura, num_plazas.
    Aquí añadimos los campos v4c que quedaron fuera.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {
        # b_long_campers = "lange Wohnmobile erlaubt" → también acepta caravanas
        # (normalize() lo pone en acceso_grandes; aquí lo duplicamos en acepta_caravanas)
        "acepta_caravanas": _bool(raw.get("b_long_campers")),
        # b_reservation = se puede/debe reservar → online_booking
        # (normalize() lo guarda en reserva_req, campo distinto)
        "online_booking":   _bool(raw.get("b_reservation")),
        # city = municipio real (normalize() lo mete en `region` erróneamente)
        "municipio":        _str_nonempty(raw.get("city")),
    }
    extras: dict = {}
    price = raw.get("price")
    if price is not None:
        try:
            f = float(price)
            if f > 0:
                extras["pricing_breakdown"] = {"pernocta": round(f, 2)}
        except (TypeError, ValueError):
            pass
    if extras:
        out["servicios_extras"] = extras
    return out


def extract_stayfree(raw: dict) -> dict:
    """stayfree: features dict → v4c columns + environment/signal servicios_extras.

    FEATURE_MAP en stayfree.py ya cubre SANITARY_* → canonical fields.
    Aquí añadimos SERVICE_*, ACTIVITY_*, ROAD_*, ENVIRONMENT_*.
    """
    if not isinstance(raw, dict):
        return {}
    features = raw.get("features") or {}
    if not isinstance(features, dict):
        features = {}

    out: dict = {}

    # Mapeo directo feature flag → columna v4c
    BOOL_MAP = {
        "SERVICE_ANIMALS":  "perros",
        "SERVICE_CARAVANS": "acepta_caravanas",
        "SERVICE_WIFI":     "wifi",        # no está en FEATURE_MAP del scraper
        "ACTIVITY_HIKING":  "hiking_nearby",
        "ACTIVITY_BIKING":  "mtb_friendly",
        "ACTIVITY_FISHING": "fishing",
        "ACTIVITY_CLIMBING":"climbing",
        "ACTIVITY_SWIMMING":"piscina",
    }
    for feat, col in BOOL_MAP.items():
        val = features.get(feat)
        if val is not None:
            out[col] = bool(val)

    # acceso_dificil: unpaved road = difícil; solo paved (sin unpaved) = fácil
    if features.get("ROAD_UNPAVED"):
        out["acceso_dificil"] = True
    elif features.get("ROAD_PAVED") and not features.get("ROAD_UNPAVED"):
        out["acceso_dificil"] = False

    # temporada_apertura
    if raw.get("is_always_open") in ("yes", "YES", True, 1, "1"):
        out["temporada_apertura"] = "all_year"

    # municipio: parsear de address "Building, Street?, City, State ZIP, Country"
    # Saltamos partes que empiezan con dígito (números de calle)
    address = _str_nonempty(raw.get("address"))
    if address:
        parts = [p.strip() for p in address.split(",") if p.strip()]
        for part in parts[1:]:  # partes[0] = nombre del lugar
            if part and not part[0].isdigit() and 3 <= len(part) <= 60:
                out["municipio"] = part[:100]
                break

    extras: dict = {}
    # environment_labels desde ENVIRONMENT_* features
    env_labels = []
    for feat, label in (
        ("ENVIRONMENT_COUNTRYSIDE", "countryside"),
        ("ENVIRONMENT_FOREST",      "forest"),
        ("ENVIRONMENT_MOUNTAINS",   "mountains"),
        ("ENVIRONMENT_SEA",         "sea"),
        ("ENVIRONMENT_BEACH",       "beach"),
        ("ENVIRONMENT_LAKE",        "lake"),
        ("ENVIRONMENT_RIVER",       "river"),
    ):
        if features.get(feat):
            env_labels.append(label)
    if env_labels:
        extras["environment_labels"] = env_labels
    if features.get("SERVICE_MOBILE_3G_4G"):
        extras["mobile_signal"] = "3g_4g"
    if extras:
        out["servicios_extras"] = extras

    return out


def extract_promobil(raw: dict) -> dict:
    """promobil: caravan, city, beergarden, leisure/aiHighlights → v4c columns."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {
        "acepta_caravanas": _bool(raw.get("caravan")),
        "municipio":        _str_nonempty(raw.get("city")),
    }
    extras: dict = {}

    if raw.get("beergarden") is True:
        extras["beer_garden"] = True

    # Texto de actividades de ocio cercanas — muy útil para el enrichment LLM
    de_dict = raw.get("_de") if isinstance(raw.get("_de"), dict) else {}
    leisure = _str_nonempty(de_dict.get("leisureActivitiesText"))
    if leisure:
        extras.setdefault("descriptions", {})["leisure"] = leisure[:500]

    # Highlights AI generados por Promobil sobre puntos de interés cercanos
    ai = de_dict.get("aiHighlights") if isinstance(de_dict.get("aiHighlights"), dict) else {}
    highlights = ai.get("highlights")
    if isinstance(highlights, list) and highlights:
        extras.setdefault("descriptions", {})["nearby_highlights"] = [
            str(h)[:200] for h in highlights[:5] if h
        ]

    if extras:
        out["servicios_extras"] = extras
    return out


def extract_searchforsites(raw: dict) -> dict:
    """searchforsites: facility codes extra + municipio desde address."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}

    # Códigos de facilidades no mapeados en normalize():
    # 18=lavandería (229/500 spots), 20=juegos niños, 8=perros (refuerza dog field)
    facs_raw = raw.get("facilities", "")
    if isinstance(facs_raw, (int, float)):
        facs_set = {str(int(facs_raw))}
    elif isinstance(facs_raw, str):
        facs_set = {f.strip() for f in facs_raw.split(",") if f.strip()}
    else:
        facs_set = set()

    if "18" in facs_set:
        out["lavanderia"] = True
    if "20" in facs_set:
        out["juegos_ninos"] = True
    if "8" in facs_set:
        out["perros"] = True  # confirmación; `dog` field en normalize() ya lo hace

    # municipio: primera parte de "City, State, ZIP"  (normalize() toma parts[1] = State)
    address = _str_nonempty(raw.get("address"))
    if address:
        parts = [p.strip() for p in address.split(",") if p.strip()]
        if parts and not parts[0][0].isdigit() and len(parts[0]) >= 2:
            out["municipio"] = parts[0][:100]

    # Pricing con símbolo de moneda
    cost = raw.get("cost")
    if isinstance(cost, dict):
        try:
            mn_f = float(cost["min"]) if cost.get("min") is not None else None
            mx_f = float(cost["max"]) if cost.get("max") is not None else None
        except (TypeError, ValueError):
            mn_f = mx_f = None
        sym = _str_nonempty(cost.get("sym"))
        pb: dict = {}
        if mn_f is not None and mn_f > 0:
            pb["min"] = round(mn_f, 2)
        if mx_f is not None and mx_f > 0:
            pb["max"] = round(mx_f, 2)
        if sym:
            pb["currency_sym"] = sym
        if pb:
            out.setdefault("servicios_extras", {})["pricing_breakdown"] = pb

    return out


def merge_extra(norm: dict, extra: dict) -> dict:
    """Mergea el output de un extractor sobre el dict de normalize().

    Reglas:
      - Solo escribe keys con valor distinto de None.
      - Si ya hay valor en norm, NO lo pisa (respeta lo que la lógica
        específica del scraper hubiera fijado primero).
      - servicios_extras: si el scraper ya emitió alguno (no es el caso hoy),
        se hace shallow merge a nivel top-key con prioridad al existente.
    """
    if not extra:
        return norm
    for k, v in extra.items():
        if v is None:
            continue
        if k == "servicios_extras":
            existing = norm.get("servicios_extras")
            if isinstance(existing, dict) and isinstance(v, dict):
                merged = dict(v)
                merged.update(existing)  # existing wins per-key
                norm[k] = merged
            elif k not in norm or norm.get(k) is None:
                norm[k] = v
        elif k not in norm or norm.get(k) is None:
            norm[k] = v
    return norm
