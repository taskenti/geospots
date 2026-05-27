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
    """womostell: b_reservation, city, price → v4c/servicios_extras.

    normalize() ya captura: perros, wifi, electricidad, ducha, agua_potable,
    vaciado_*, wc_publico, acceso_grandes (← b_long_campers = "lange Wohnmobile"),
    temporada_apertura, num_plazas.
    Aquí añadimos los campos v4c que quedaron fuera.

    NOTA: b_long_campers = "lange Wohnmobile erlaubt" = autocaravanas largas,
    NO caravanas remolcadas. Ya va a acceso_grandes en normalize(); no lo
    duplicamos en acepta_caravanas.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {
        # b_reservation = se puede/debe reservar → online_booking
        # (normalize() lo guarda en reserva_req, campo distinto)
        "online_booking": _bool(raw.get("b_reservation")),
        # city = municipio real (normalize() lo mete en `region` erróneamente)
        "municipio":      _str_nonempty(raw.get("city")),
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


# ─── Extractors added in PR10 ────────────────────────────────────────────────


def extract_bobilguiden(raw: dict) -> dict:
    """bobilguiden: campos no capturados por normalize().

    normalize() ya extrae: wc_publico(3), ducha(2), electricidad(5), agua_potable(6),
    vaciado_grises(10), vaciado_negras(11), wifi(15) por facilityIds; email, telefono,
    web, region (addr.county||city), num_plazas (vehicleCount), precio/gratuito.

    Aquí añadimos: municipio (addr.city, más específico que county), caravanAllowed,
    y shortDescription si existe.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}

    # municipio: addr.city más específico que county (que va a region)
    loc = raw.get("location") or {}
    addr = loc.get("address") or {} if isinstance(loc, dict) else {}
    if isinstance(addr, dict):
        city = _str_nonempty(addr.get("city"))
        if city:
            out["municipio"] = city[:100]

    # acepta_caravanas: caravana remolcada (NO autocaravana). Defensivo.
    caravan = raw.get("caravanAllowed")
    if caravan is not None:
        b = _bool(caravan)
        if b is not None:
            out["acepta_caravanas"] = b

    # Descripción corta en noruego (bulk a veces trae este campo)
    short = _str_nonempty(raw.get("shortDescription"))
    if short:
        out["descripcion_no"] = short[:500]

    return out


def extract_campendium(raw: dict) -> dict:
    """campendium: campos del tile/place_detail no cubiertos por normalize().

    normalize() ya extrae: wc_publico, ducha, wifi, perros, acceso_grandes,
    electricidad, vaciado_* del place_detail embedded en el tile response.
    agua_potable está explícitamente a None (la API básica no lo da).

    Aquí buscamos campos opcionales que algunos tiles sí traen.
    """
    if not isinstance(raw, dict):
        return {}

    props = raw.get("properties", raw)
    if not isinstance(props, dict):
        props = raw
    pd = props.get("place_detail") or {}
    if not isinstance(pd, dict):
        pd = {}

    def _b(val) -> bool | None:
        if val is None:
            return None
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "yes", "1")
        return bool(val) if val in (0, 1) else None

    out: dict = {}

    # agua_potable: conocida laguna de la ruta básica (tile no trae esto,
    # pero el detail fetch posterior lo añade a normalized_data directamente)
    water = _b(pd.get("water")) or _b(pd.get("water_hookup"))
    if water is not None:
        out["agua_potable"] = water

    # piscina (pool / swimming)
    pool = _b(pd.get("pool")) or _b(pd.get("swimming_pool"))
    if pool is not None:
        out["piscina"] = pool

    # num_plazas
    cap = pd.get("capacity") or pd.get("sites") or props.get("capacity")
    if cap is not None:
        try:
            n = int(cap)
            if n > 0:
                out["num_plazas"] = n
        except (TypeError, ValueError):
            pass

    # municipio desde props.city / props.locality
    muni = _str_nonempty(props.get("city")) or _str_nonempty(props.get("locality"))
    if muni:
        out["municipio"] = muni[:100]

    return out


def extract_osm(raw: dict) -> dict:
    """osm: tags OSM no capturados por normalize().

    normalize() ya extrae: agua_potable, vaciado_negras/grises, wc_publico, ducha,
    electricidad, wifi, perros, temporada_apertura (opening_hours), email, web,
    telefono, region (addr:city||town), num_plazas (capacity).

    Aquí añadimos: acepta_caravanas, acceso_dificil, municipio, ev_charging, stars.
    """
    if not isinstance(raw, dict):
        return {}
    tags = raw.get("tags") or {}
    if not isinstance(tags, dict):
        return {}

    out: dict = {}

    # acepta_caravanas: OSM usa tag "caravans" (yes/no/permissive/private)
    caravans = (tags.get("caravans") or "").lower().strip()
    if caravans in ("yes", "permissive", "1"):
        out["acepta_caravanas"] = True
    elif caravans in ("no", "0", "private"):
        out["acepta_caravanas"] = False

    # acceso_dificil: surface no pavimentada → difícil para vehículos grandes
    surface = (tags.get("surface") or "").lower().strip()
    HARD_SURFACES = {"asphalt", "paved", "concrete", "sett", "cobblestone", "metal"}
    SOFT_SURFACES = {"unpaved", "gravel", "dirt", "grass", "ground", "mud",
                     "sand", "earth", "compacted", "fine_gravel", "wood"}
    if surface in SOFT_SURFACES:
        out["acceso_dificil"] = True
    elif surface in HARD_SURFACES:
        out["acceso_dificil"] = False

    # municipio: normalize() ya pone addr:city en region. Lo duplicamos en
    # municipio (columna separada, más específica que region en multi-fuente).
    muni = (tags.get("addr:city") or tags.get("addr:town")
            or tags.get("addr:municipality") or tags.get("addr:village"))
    if muni:
        out["municipio"] = str(muni).strip()[:100]

    # servicios_extras opcionales
    extras: dict = {}

    # Carga para vehículos eléctricos (tag motorhome:charging o similar)
    if (tags.get("motorhome:charging") or "").lower() in ("yes", "1", "service"):
        extras["ev_charging"] = True
    elif (tags.get("electric_vehicle:charging") or "").lower() in ("yes", "1"):
        extras["ev_charging"] = True

    # Estrellas oficiales (campings homologados)
    stars_raw = tags.get("stars")
    if stars_raw is not None:
        try:
            s = int(str(stars_raw).strip())
            if 1 <= s <= 5:
                extras["stars"] = s
        except (TypeError, ValueError):
            pass

    # Piscina (raro en spots pero existe en campings de 4+ estrellas)
    if (tags.get("swimming_pool") or "").lower() in ("yes", "1"):
        extras["pool"] = True

    if extras:
        out["servicios_extras"] = extras

    return out


def extract_furgovw(raw: dict) -> dict:
    """furgovw: extrae perros, wifi, acepta_caravanas del body del post de foro.

    normalize() ya parsea el body vía _parsear_body() para: agua, wc, electricidad,
    ducha, vaciado_*, precio, tipo, gratuito, descripcion_es, acceso_grandes.
    Aquí añadimos los que _parsear_body() no cubre.

    NOTA: furgovw es un foro de furgonetas/autocaravanas. Las "caravanas remolcadas"
    son raras; solo marcamos acepta_caravanas si hay mención explícita y afirmativa.
    """
    if not isinstance(raw, dict):
        return {}

    body = raw.get("body") or ""
    if not isinstance(body, str):
        body = str(body)

    # Limpiar HTML/BBCode básico para búsqueda de keywords
    import re as _re
    body_clean = _re.sub(r'<[^>]+>', ' ', body)
    body_clean = _re.sub(r'\[[^\]]+\]', ' ', body_clean)  # BBCode
    body_low = body_clean.lower()

    out: dict = {}

    # ── perros ──────────────────────────────────────────────────────────
    PERROS_SI = [
        "perros permitidos", "perros sí", "perros si", "se admiten perros",
        "admiten perros", "perros bienvenidos", "dog friendly", "dogs welcome",
        "mascotas permitidas", "mascotas bienvenidas", "se admiten mascotas",
        "animales permitidos", "con perro", "con mascotas",
    ]
    PERROS_NO = [
        "no se admiten perros", "no admiten perros", "no perros",
        "perros no", "sin perros", "prohibido perros", "no mascotas",
        "no se admiten mascotas", "no animales", "sin mascotas",
    ]
    for kw in PERROS_NO:
        if kw in body_low:
            out["perros"] = False
            break
    if "perros" not in out:
        for kw in PERROS_SI:
            if kw in body_low:
                out["perros"] = True
                break

    # ── wifi ────────────────────────────────────────────────────────────
    WIFI_SI = ["wifi", "wi-fi", "internet disponible", "hay internet", "conexión a internet"]
    WIFI_NO = ["sin wifi", "sin wi-fi", "no hay wifi", "no wifi", "sin internet"]
    for kw in WIFI_NO:
        if kw in body_low:
            out["wifi"] = False
            break
    if "wifi" not in out:
        for kw in WIFI_SI:
            if kw in body_low:
                out["wifi"] = True
                break

    # ── acepta_caravanas ────────────────────────────────────────────────
    # Solo "caravana" en sentido remolcada (NO autocaravana, NO "caravanismo")
    # Negativo primero para evitar falsos positivos.
    # CUIDADO: "autocaravanas" contiene "caravanas" → usar regex con lookbehind negativo.
    import re as _re2
    def _caravana_match(kw: str, text: str) -> bool:
        """Comprueba que kw aparece en text pero NO precedido por 'auto'."""
        idx = text.find(kw)
        while idx != -1:
            if idx < 4 or text[idx-4:idx] != "auto":
                return True
            idx = text.find(kw, idx + 1)
        return False

    CARAVANA_NO = [
        "no caravanas", "sin caravanas", "caravanas no", "prohibido caravanas",
        "no se admiten caravanas",
    ]
    CARAVANA_SI = [
        "caravanas permitidas", "se admiten caravanas", "admiten caravanas",
        "caravanas bienvenidas", "caravanas sí", "caravanas si",
        "caravanas y autocaravanas",
    ]
    for kw in CARAVANA_NO:
        if _caravana_match(kw, body_low):
            out["acepta_caravanas"] = False
            break
    if "acepta_caravanas" not in out:
        for kw in CARAVANA_SI:
            if _caravana_match(kw, body_low):
                out["acepta_caravanas"] = True
                break

    return out


def extract_thedyrt(raw: dict) -> dict:
    """thedyrt: campos de attrs no mapeados en normalize().

    normalize() ya extrae: agua_potable (drinking-water/water-hookups), wc_publico
    (toilets), ducha (showers), wifi, perros (pets-allowed), acceso_grandes
    (big-rig-friendly), reserva_req (reservable/permit-required), vaciado_grises/negras
    (sanitary-dump), num_plazas (campsites-count), tipo, gratuito/precio_aprox/precio_info,
    rating_promedio, descripcion_en, fotos_urls, web, region, country_iso.

    Aquí añadimos: electricidad (electric-hookups), municipio (nearest-city-name),
    acceso_dificil (access-road), y servicios_extras con max_vehicle_length_ft,
    cell_service, campfire, max_nights.
    """
    if not isinstance(raw, dict):
        return {}
    attrs = raw.get("attributes") or {}
    if not isinstance(attrs, dict):
        return {}

    out: dict = {}

    # electricidad: normalize() no tiene electric-hookups
    elec = _bool(attrs.get("electric-hookups"))
    if elec is not None:
        out["electricidad"] = elec

    # municipio: nearest-city-name (normalize() solo guarda region-name)
    city = _str_nonempty(attrs.get("nearest-city-name"))
    if city:
        out["municipio"] = city[:100]

    # acceso_dificil: access-road (paved → fácil; dirt/gravel/unpaved → difícil)
    road = (attrs.get("access-road") or "").lower().strip()
    if road in ("dirt", "gravel", "unpaved", "rough"):
        out["acceso_dificil"] = True
    elif road in ("paved", "paved road"):
        out["acceso_dificil"] = False

    # servicios_extras opcionales
    extras: dict = {}

    # Longitud máxima de vehículo (en pies — estándar US/CA)
    mvl = _int(attrs.get("max-vehicle-length"))
    if mvl is not None:
        extras["max_vehicle_length_ft"] = mvl

    # Cobertura móvil
    cell = _bool(attrs.get("cell-service"))
    if cell is not None:
        extras["cell_service"] = cell

    # Hogueras permitidas
    campfire = _bool(attrs.get("campfire"))
    if campfire is not None:
        extras["campfire"] = campfire

    # Estancia máxima (noches)
    max_nights = _int(attrs.get("max-nights") or attrs.get("max-vehicle-nights"))
    if max_nights is not None:
        extras["max_nights"] = max_nights

    if extras:
        out["servicios_extras"] = extras

    return out


def extract_alpacacamping(raw: dict) -> dict:
    """alpacacamping: amenities_infos.title (alemán) + property_address + atributos.

    normalize() ya cubre vía amenities_infos.id: agua_potable (14/238), electricidad
    (13/223), wc_publico (16/284), ducha (17/476), wifi (1), perros (41/315/229/231
    vs 4), vaciado_grises (20), vaciado_negras (21), acceso_grandes (26), tipo
    (25/27/28), country_iso/region, descripcion_de, rating, precio_*, fotos.

    Aquí matcheamos los `title` (alemán) de amenities_infos contra patrones para sacar:
      v4c: lavanderia (waschmaschine), juegos_ninos (kinderfreundlich/familienfreundlich/
           für kinder), mtb_friendly (bikepark/radwege), hiking_nearby (für wanderfreunde/
           wandern), fishing (für angler/angeln), winter_friendly (wintercamping),
           mirador (tolle aussicht/weitblick/sonnenuntergang), acceso_dificil
           (erfordert allrad vs stellplatz befestigt), municipio (address.city).
      extras: environment_labels (wiese/hof/feld/wald/unter_bäumen/weinberg/see/fluss/
              meer/agua/secluded/shaded), vibes (romantic/cozy/laid_back/pure_nature/
              wow_factor/glamping/starry_sky), audience (adventurers/groups/hikers/
              anglers/wine_lovers/families/buddies), alpacas_onsite, horse_onsite,
              animals_onsite, farm_shop, campfire (feuerstelle vs kein lagerfeuer),
              bbq (holzkohlegrill vs kein grillen), firewood_available, kitchen
              (küche), outdoor_seating, car_at_pitch, free_parking, requires_4wd,
              ground (paved si stellplatz befestigt), no_grey_water_disposal,
              no_fruit_picking, max_persons (accommodates), area_sqm (space_amount),
              booking_type, bedrooms, bathrooms.
    """
    if not isinstance(raw, dict):
        return {}

    ai = raw.get("amenities_infos") or {}
    titles_raw = ai.get("title") if isinstance(ai, dict) else None
    if not isinstance(titles_raw, list):
        titles_raw = []
    titles = [str(t).strip().lower() for t in titles_raw if t]

    def has(*needles) -> bool:
        return any(any(n in t for t in titles) for n in needles)

    out: dict = {}

    # ── columnas v4c ───────────────────────────────────────────────────
    if has("waschmaschine"):
        out["lavanderia"] = True
    if has("kinderfreundlich", "familienfreundlich", "für kinder"):
        out["juegos_ninos"] = True
    if has("bikepark", "radwege"):
        out["mtb_friendly"] = True
    if has("für wanderfreunde", "wandern"):
        out["hiking_nearby"] = True
    if has("für angler", "angeln"):
        out["fishing"] = True
    if has("wintercamping"):
        out["winter_friendly"] = True
    if has("tolle aussicht", "weitblick", "sonnenuntergang"):
        out["mirador"] = True

    # 4WD requerido > stellplatz befestigt (en caso de conflicto, 4WD gana)
    if has("erfordert allrad"):
        out["acceso_dificil"] = True
    elif has("stellplatz befestigt"):
        out["acceso_dificil"] = False

    # municipio
    addr = raw.get("property_address") or {}
    if isinstance(addr, dict):
        muni = _str_nonempty(addr.get("city"))
        if muni:
            out["municipio"] = muni[:100]

    # ── servicios_extras ───────────────────────────────────────────────
    extras: dict = {}

    # Environment / location labels
    env_map = (
        ("auf einer wiese",         "meadow"),
        ("auf einem (bauern-) hof", "farm"),
        ("am feld",                 "field"),
        ("am wald",                 "forest"),
        ("unter bäumen",            "under_trees"),
        ("im weinberg",             "vineyard"),
        ("beim winzer",             "vineyard"),
        ("ab an den see",           "lake_nearby"),
        ("ab an den fluss",         "river_nearby"),
        ("ab ans meer",             "sea_nearby"),
        ("direkt am wasser",        "on_water"),
        ("wasser in der nähe",      "water_nearby"),
        ("alleinlage",              "secluded"),
        ("schattiges plätzchen",    "shaded"),
    )
    env_labels = sorted({label for kw, label in env_map if has(kw)})
    if env_labels:
        extras["environment_labels"] = env_labels

    # Vibe / atmosphere
    vibe_map = (
        ("romantisch", "romantic"),
        ("gemütlich",  "cozy"),
        ("lässig",     "laid_back"),
        ("natur pur",  "pure_nature"),
        ("wow",        "wow_factor"),
        ("glamping",   "glamping"),
        ("sterne gucken", "starry_sky"),
    )
    vibes = sorted({label for kw, label in vibe_map if has(kw)})
    if vibes:
        extras["vibes"] = vibes

    # Target audience
    audience_map = (
        ("für abenteurer",     "adventurers"),
        ("gemeinsam verreisen","groups"),
        ("für wanderfreunde",  "hikers"),
        ("für angler",         "anglers"),
        ("für weinliebhaber",  "wine_lovers"),
        ("familienfreundlich", "families"),
        ("kinderfreundlich",   "families"),
        ("unterwegs mit dem campingbuddy", "buddies"),
    )
    aud = sorted({label for kw, label in audience_map if has(kw)})
    if aud:
        extras["audience"] = aud

    # On-site animals / farm services
    if has("alpakas vor ort"):
        extras["alpacas_onsite"] = True
    if has("pferde vor ort"):
        extras["horse_onsite"] = True
    if has("tiere vor ort"):
        extras["animals_onsite"] = True
    if has("hofladen"):
        extras["farm_shop"] = True

    # Campfire / BBQ con negativos explícitos
    if has("kein lagerfeuer"):
        extras["campfire"] = False
    elif has("feuerstelle"):
        extras["campfire"] = True
    if has("kein grillen"):
        extras["bbq"] = False
    elif has("holzkohlegrill"):
        extras["bbq"] = True
    if has("brennholz"):
        extras["firewood_available"] = True

    # Otras facilities
    if has("küche"):
        extras["kitchen"] = True
    if has("sitzplätze im freien"):
        extras["outdoor_seating"] = True
    if has("auto am standplatz"):
        extras["car_at_pitch"] = True
    if has("kostenloser parkplatz"):
        extras["free_parking"] = True
    if has("erfordert allrad"):
        extras["requires_4wd"] = True
    if has("stellplatz befestigt"):
        extras["ground"] = "paved"
    if has("keine schmutzwasserentsorgung"):
        extras["no_grey_water_disposal"] = True
    if has("keine früchte ernten"):
        extras["no_fruit_picking"] = True

    # Capacidad / área desde campos top-level
    accommodates = raw.get("accommodates")
    if isinstance(accommodates, (int, float)) and not isinstance(accommodates, bool) and accommodates > 0:
        extras["max_persons"] = int(accommodates)

    space = raw.get("space_amount")
    if isinstance(space, (int, float)) and not isinstance(space, bool) and space > 0:
        extras["area_sqm"] = round(float(space), 1)

    booking_type = _str_nonempty(raw.get("booking_type"))
    if booking_type:
        extras["booking_type"] = booking_type[:30]

    for k_in, k_out in (("bedrooms", "bedrooms"), ("bathrooms", "bathrooms")):
        v = raw.get(k_in)
        if isinstance(v, int) and v > 0:
            extras[k_out] = v

    if extras:
        out["servicios_extras"] = extras

    return out


def extract_roadsurfer(raw: dict) -> dict:
    """roadsurfer: detail rico con facilities/activities/placeSituations.

    normalize() ya extrae: agua_potable (drinkingWater), wc_publico (toilet/separateToilet/
    separateDryToilet), ducha (shower), wifi (wlan/internet), electricidad, perros (pets),
    vaciado_grises/negras (veStation), acceso_grandes/tipo (campingTypes), gratuito/precio_*,
    num_plazas, rating, descripcion_<lang>, fotos.

    Aquí añadimos:
      v4c: lavanderia (washingMachine/tumbleDryer/clothesline), juegos_ninos
           (childrenPlaygrounds), piscina (swimmingPool), accesibilidad_reducida
           (handicappedAccessible), mtb_friendly (bicycleCellar/selfServiceBicycles),
           hiking_nearby, fishing, climbing, surf_friendly (surfingSailing/sup),
           restaurant (barRestaurant), online_booking (isBookable),
           acceso_dificil (looseUnderground), municipio (city).
      extras: environment_labels (placeSituations), activities (resto),
              campfire, bbq (grillPlace), picnic_table, kitchen, fridge,
              shops_nearby, public_transit (trainOrBus), naturism (fkk),
              stable, horse_onsite, closed_area, parking, separate_entrance,
              ground (concrete/loose/lawn), min_nights, max_nights, hours,
              cancellation_policy, area_sqm, pitch_location, badge, labels,
              house_rules_link, addon_prices.
    """
    if not isinstance(raw, dict):
        return {}

    def _set(key):
        v = raw.get(key) or []
        if not isinstance(v, list):
            return set()
        return {x for x in v if isinstance(x, str)}

    fac = _set("facilities")
    act = _set("activities")
    plc = _set("placeSituations")

    out: dict = {}

    # ── columnas v4c ───────────────────────────────────────────────────
    if fac & {"washingMachine", "tumbleDryer", "clothesline"}:
        out["lavanderia"] = True
    if "childrenPlaygrounds" in fac:
        out["juegos_ninos"] = True
    if "swimmingPool" in fac:
        out["piscina"] = True
    if "handicappedAccessible" in fac:
        out["accesibilidad_reducida"] = True
    if fac & {"bicycleCellar", "selfServiceBicycles"}:
        out["mtb_friendly"] = True
    if "hikingTrails" in act:
        out["hiking_nearby"] = True
    if "fishing" in act:
        out["fishing"] = True
    if "climbing" in act:
        out["climbing"] = True
    if act & {"surfingSailing", "sup"}:
        out["surf_friendly"] = True
    if "barRestaurant" in act:
        out["restaurant"] = True
    if raw.get("isBookable") is True:
        out["online_booking"] = True
    if "looseUnderground" in fac:
        out["acceso_dificil"] = True

    city = _str_nonempty(raw.get("city"))
    if city:
        out["municipio"] = city[:100]

    # ── servicios_extras ───────────────────────────────────────────────
    extras: dict = {}

    # placeSituations → environment_labels (camelCase → snake)
    env_map = {
        "forest": "forest", "farm": "farm", "garden": "garden",
        "mountains": "mountains", "river": "river", "lake": "lake",
        "seaCoast": "sea_coast", "beach": "beach", "city": "urban",
        "country": "countryside", "carpark": "carpark",
        "hotelParking": "hotel_parking",
    }
    env_labels = sorted({env_map[p] for p in plc if p in env_map})
    if env_labels:
        extras["environment_labels"] = env_labels

    # Activities extra (no en v4c columns)
    v4c_acts = {"hikingTrails", "fishing", "climbing", "surfingSailing",
                "sup", "barRestaurant"}
    extra_acts = sorted(a for a in act if a not in v4c_acts)
    if extra_acts:
        extras["activities"] = extra_acts

    # Boolean facilities → extras
    bool_extras = (
        ("campfire",        "campfire"),
        ("grillPlace",      "bbq"),
        ("picnicTable",     "picnic_table"),
        ("fridge",          "fridge"),
        ("shops",           "shops_nearby"),
        ("trainOrBus",      "public_transit"),
        ("fkk",             "naturism"),
        ("stable",          "stable"),
        ("horse",           "horse_onsite"),
        ("closedArea",      "closed_area"),
        ("parking",         "parking"),
        ("separateEntrance","separate_entrance"),
    )
    for k_in, k_out in bool_extras:
        if k_in in fac:
            extras[k_out] = True

    # Kitchen (cualquier variante)
    if fac & {"kitchen", "separateKitchen"}:
        extras["kitchen"] = True

    # Ground type derivado
    if "concreteFloorSpace" in fac:
        extras["ground"] = "concrete"
    elif "looseUnderground" in fac:
        extras["ground"] = "loose"
    elif "lawnArea" in fac:
        extras["ground"] = "lawn"

    # Booking limits
    mn = raw.get("minNights")
    if isinstance(mn, (int, float)) and not isinstance(mn, bool) and mn > 0:
        extras["min_nights"] = int(mn)
    mx = raw.get("maxNights")
    if isinstance(mx, (int, float)) and not isinstance(mx, bool) and mx > 0:
        extras["max_nights"] = int(mx)

    # Hours (4 fields, ya vienen "HH:MM" o "")
    hours: dict = {}
    for k_raw, k_out in (
        ("checkInFrom",   "check_in_from"),
        ("checkInUntil",  "check_in_until"),
        ("checkOutFrom",  "check_out_from"),
        ("checkOutUntil", "check_out_by"),
    ):
        v = _str_nonempty(raw.get(k_raw))
        if v and ":" in v:
            hours[k_out] = v[:5]
    if hours:
        extras["hours"] = hours

    # Cancellation policy
    cp = _str_nonempty(raw.get("cancellationPolicy"))
    if cp:
        extras["cancellation_policy"] = cp[:80]

    # Área del spot
    area = raw.get("areaInQm")
    if isinstance(area, (int, float)) and not isinstance(area, bool) and area > 0:
        extras["area_sqm"] = round(float(area), 1)

    # Texto cortos
    pitch_loc = _str_nonempty(raw.get("pitchLocation"))
    if pitch_loc:
        extras["pitch_location"] = pitch_loc[:200]
    badge = _str_nonempty(raw.get("badgeLabel"))
    if badge:
        extras["badge"] = badge[:60]
    labels = raw.get("labels")
    if isinstance(labels, list) and labels:
        labs = [str(l)[:40] for l in labels if l]
        if labs:
            extras["labels"] = labs[:10]

    # House rules link (descargable o enlace)
    rules_link = _str_nonempty(raw.get("houseRulesLink")) or \
                 _str_nonempty(raw.get("houseRulesDownload"))
    if rules_link:
        extras["house_rules_link"] = rules_link[:300]

    # Addon prices (céntimos → euros)
    addon = {}
    for k_raw, k_out in (("adultAddonPrice", "adult_addon"),
                         ("childAddonPrice", "child_addon")):
        v = raw.get(k_raw)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            addon[k_out] = round(float(v) / 100.0, 2)
    if addon:
        extras["addon_prices"] = addon

    if extras:
        out["servicios_extras"] = extras

    return out


def extract_vansite(raw: dict) -> dict:
    """vansite (Sharetribe Flex Transit JSON): publicData rica con amenities,
    activities, surroundings, kfz, locationPlace, verified, self-sufficient, etc.

    raw_data en DB tiene keys con prefijo `~:` (formato Transit JSON ya decodificado).
    normalize() ya extrae: agua_potable (amenities.water), wc_publico (toilet/wc),
    ducha (shower), electricidad, wifi, perros (dog/pets), vaciado_grises/negras,
    acceso_grandes (kfz: motorhome/camper/bus/caravan), num_plazas, rating,
    precio_*, descripcion_<lang>, fotos.

    Aquí añadimos:
      v4c: lavanderia (wascher), juegos_ninos (kidsFriendly/playground), mtb_friendly,
           hiking_nearby, fishing, climbing, surf_friendly, acepta_caravanas (kfz),
           acceso_dificil (allWheelDrive=yes), municipio (locationPlace).
      extras: campfire (fireplace), trash_disposal (garbage), cell_service (signal),
              tent_friendly (kfz tent/carTent), environment_labels (surroundings),
              activities (rest), verified, self_sufficient_required (selfSufficient),
              requires_4wd (allWheelDrive), hours (check_in/check_out parsed from
              X_clock), min_nights (booking min_X), max_nights (limit_X),
              cancellation_policy (cancellationPolicyTier).
    """
    if not isinstance(raw, dict):
        return {}
    attrs = raw.get("~:attributes") or {}
    if not isinstance(attrs, dict):
        return {}
    pd = attrs.get("~:publicData") or {}
    if not isinstance(pd, dict):
        return {}

    def _as_set(key):
        v = pd.get(key) or []
        if not isinstance(v, list):
            return set()
        return {x for x in v if isinstance(x, str)}

    amen_set = _as_set("~:amenities")
    act_set = _as_set("~:activities")
    surr_set = _as_set("~:surroundings")
    kfz_set = _as_set("~:kfz")

    out: dict = {}

    # ── columnas v4c ───────────────────────────────────────────────────
    if "wascher" in amen_set:
        out["lavanderia"] = True
    if "kidsFriendly" in amen_set or "playground" in amen_set:
        out["juegos_ninos"] = True
    if "cycling" in act_set:
        out["mtb_friendly"] = True
    if "hiking" in act_set:
        out["hiking_nearby"] = True
    if "fishing" in act_set:
        out["fishing"] = True
    if "climbing" in act_set:
        out["climbing"] = True
    if "surfing" in act_set:
        out["surf_friendly"] = True
    if "caravan" in kfz_set:
        out["acepta_caravanas"] = True

    location_place = _str_nonempty(pd.get("~:locationPlace"))
    if location_place:
        out["municipio"] = location_place[:100]

    awd = pd.get("~:allWheelDrive")
    if isinstance(awd, str) and awd.lower() == "yes":
        out["acceso_dificil"] = True

    # ── servicios_extras ───────────────────────────────────────────────
    extras: dict = {}

    if "fireplace" in amen_set:
        extras["campfire"] = True
    if "garbage" in amen_set:
        extras["trash_disposal"] = True
    if "signal" in amen_set:
        extras["cell_service"] = True
    if "tent" in kfz_set or "carTent" in kfz_set:
        extras["tent_friendly"] = True

    # Environment labels
    env_map = {
        "forest": "forest", "forrest": "forest",
        "meadow": "meadow", "field": "field", "court": "yard",
        "house":  "near_house", "lake": "lake", "river": "river",
        "mountain": "mountain", "sea": "sea",
    }
    env_labels = sorted({env_map[s] for s in surr_set if s in env_map})
    if env_labels:
        extras["environment_labels"] = env_labels

    # Activities extra (no en columnas v4c)
    v4c_acts = {"hiking", "cycling", "fishing", "climbing", "surfing"}
    extra_acts = sorted(a for a in act_set if a not in v4c_acts)
    if extra_acts:
        extras["activities"] = extra_acts

    # Verified (sólo guardamos True; 0/false son no-info útil)
    v = pd.get("~:verified")
    if v == 1 or v is True:
        extras["verified"] = True

    # Self-sufficient required (autosuficiencia exigida al huésped)
    ss = pd.get("~:selfSufficient")
    if isinstance(ss, list) and ss:
        ss = ss[0]
    if isinstance(ss, bool):
        extras["self_sufficient_required"] = ss

    if awd and isinstance(awd, str) and awd.lower() == "yes":
        extras["requires_4wd"] = True

    # Horarios check-in / check-out (formato "10_clock" → "10:00")
    def _clock(val):
        if isinstance(val, str) and val.endswith("_clock"):
            try:
                return f"{int(val[:-6]):02d}:00"
            except ValueError:
                return None
        return None

    hours: dict = {}
    for raw_key, out_key in (
        ("~:earliestArrivalTime", "check_in_from"),
        ("~:latestArrivalTime",   "check_in_until"),
        ("~:latestDepartureTime", "check_out_by"),
    ):
        v = _clock(pd.get(raw_key))
        if v:
            hours[out_key] = v
    if hours:
        extras["hours"] = hours

    # Min/max nights
    bm = pd.get("~:bookingMinimum")
    if isinstance(bm, str) and bm.startswith("min_"):
        try:
            n = int(bm[4:])
            if n > 0:
                extras["min_nights"] = n
        except ValueError:
            pass

    bl = pd.get("~:bookingLimit")
    if isinstance(bl, list) and bl:
        bl = bl[0]
    if isinstance(bl, str) and bl.startswith("limit_") and not bl.startswith("limit_bigger_"):
        try:
            n = int(bl[6:])
            if n > 0:
                extras["max_nights"] = n
        except ValueError:
            pass

    # Cancellation policy
    cp = _str_nonempty(pd.get("~:cancellationPolicyTier"))
    if cp:
        extras["cancellation_policy"] = cp[:50]

    if extras:
        out["servicios_extras"] = extras

    return out


def extract_campspace(raw: dict) -> dict:
    """campspace: amenities + surroundings lists del scraping Phase 2.

    `_normalize_detail()` del scraper ya cubre lo básico (agua_potable, wc_publico,
    ducha, wifi, electricidad, vaciado_*, perros, seguridad, iluminacion, num_plazas).
    normalize() de Phase 1 cubre nombre, lat/lon, precio_*, web.

    Aquí añadimos columnas v4c desde amenities/surroundings y servicios_extras:
      v4c: piscina, juegos_ninos, mirador, accesibilidad_reducida, mtb_friendly,
           hiking_nearby, fishing, climbing, surf_friendly.
      extras: environment_labels, activities, farm_animals, campfire, bbq,
              picnic_table, kitchen, sauna, hot_tub, ev_charging, naturism, shelter,
              cell_service, heating, glamping_linen, rv_hookup, rv_dump,
              pricing_breakdown (de raw.price).
    """
    if not isinstance(raw, dict):
        return {}

    amenities = raw.get("amenities") or []
    surroundings = raw.get("surroundings") or []
    if not isinstance(amenities, list):
        amenities = []
    if not isinstance(surroundings, list):
        surroundings = []

    a_low = [str(a).lower() for a in amenities if a]
    s_low = [str(s).lower() for s in surroundings if s]

    def amen(*needles) -> bool:
        return any(any(n in a for a in a_low) for n in needles)

    def surr(*needles) -> bool:
        return any(any(n in s for s in s_low) for n in needles)

    out: dict = {}

    # ── columnas v4c ───────────────────────────────────────────────────
    if amen("swimming pool"):
        out["piscina"] = True
    if amen("playground", "trampoline"):
        out["juegos_ninos"] = True
    if amen("view"):  # sunset/sunrise/forest/field/hill/mountain/river/lake/sea view
        out["mirador"] = True
    if amen("barrier-free"):
        out["accesibilidad_reducida"] = True
    if amen("bicycle storage") or surr("cycling"):
        out["mtb_friendly"] = True
    if surr("hiking"):
        out["hiking_nearby"] = True
    if surr("fishing"):
        out["fishing"] = True
    if surr("climbing"):
        out["climbing"] = True
    if surr("surfing"):
        out["surf_friendly"] = True

    # ── servicios_extras ───────────────────────────────────────────────
    extras: dict = {}

    env_map = (
        ("forest",             "forest"),
        ("meadow or plain",    "meadow"),
        ("river or stream",    "river"),
        ("hills",              "hills"),
        ("beach or seaside",   "beach"),
        ("urban area",         "urban"),
    )
    env_labels = [label for kw, label in env_map if surr(kw)]
    if env_labels:
        extras["environment_labels"] = env_labels

    act_map = (
        ("making a campfire",   "campfire"),
        ("swimming",            "swimming"),
        ("canoeing or kayaking","canoeing"),
        ("boating",             "boating"),
        ("horseback riding",    "horseback_riding"),
        ("outdoor cooking",     "outdoor_cooking"),
        ("wine or beer tasting","wine_beer_tasting"),
        ("sightseeing",         "sightseeing"),
        ("wildlife watching",   "wildlife_watching"),
    )
    activities = [label for kw, label in act_map if surr(kw)]
    if activities:
        extras["activities"] = activities

    farm_map = (("cows", "cows"), ("sheep", "sheep"), ("chickens", "chickens"),
                ("horses or ponies", "horses"), ("deer", "deer"), ("birds", "birds"))
    farm_animals = [out_label for kw, out_label in farm_map if surr(kw)]
    if farm_animals:
        extras["farm_animals"] = farm_animals

    # Boolean amenities → extras (solo si True; no marcamos False porque la
    # ausencia de un amenity no implica que esté prohibido)
    if amen("fireplace", "fire basket", "fire bowl", "firewood", "wood burner", "wood-fired oven"):
        extras["campfire"] = True
    if amen("bbq"):
        extras["bbq"] = True
    if amen("picnic table"):
        extras["picnic_table"] = True
    if amen("kitchen", "refrigerator", "gas stove", "cooking basics", "dishes and cutlery"):
        extras["kitchen"] = True
    if amen("sauna"):
        extras["sauna"] = True
    if amen("hot tub"):
        extras["hot_tub"] = True
    if amen("ev car charging"):
        extras["ev_charging"] = True
    if amen("naturism"):
        extras["naturism"] = True
    if amen("rain shelter"):
        extras["shelter"] = True
    if amen("mobile reception"):
        extras["cell_service"] = True
    if amen("heating", "electrical heating", "wood burner"):
        extras["heating"] = True
    if amen("bed linen") or amen("towels"):
        extras["glamping_linen"] = True
    if amen("rv electricity"):
        extras["rv_hookup"] = True
    if amen("rv dump"):
        extras["rv_dump"] = True

    # Pricing de raw.price (Phase 1) — "from € 18", "£ 22.50", etc.
    price_str = raw.get("price")
    if isinstance(price_str, str) and price_str:
        import re as _re
        m = _re.search(r"(\d+(?:[.,]\d+)?)", price_str)
        if m:
            try:
                val = float(m.group(1).replace(",", "."))
                if val > 0:
                    currency = "€"
                    if "£" in price_str:
                        currency = "£"
                    elif "$" in price_str:
                        currency = "$"
                    extras["pricing_breakdown"] = {
                        "from": round(val, 2),
                        "currency": currency,
                    }
            except ValueError:
                pass

    if extras:
        out["servicios_extras"] = extras

    return out


def extract_nomady(raw: dict) -> dict:
    """nomady: API rica DACH/CH. Muchas facilities/activities/pricing.

    normalize() ya extrae: agua_potable (drinkingWater), wc_publico (regular/outdoor
    Toilet), ducha (regular/outdoorShower), electricidad (power), vaciado_negras/grises
    (black/greyWater), country_iso, rating, fotos, web, num_reviews, tipo.

    Aquí añadimos columnas v4c y servicios_extras rico:
      - wifi, perros, lavanderia, restaurant, mtb_friendly, hiking_nearby, fishing,
        climbing, winter_friendly, online_booking
      - municipio (city)
      - servicios_extras: environment_labels, food_nearby, activities, pricing_breakdown,
        min_nights, max_nights, cell_service, campfire, heating, kitchen, verified,
        family_friendly, dogs_on_leash_only, picnic_table, trash_disposal, shelter, ground.
    """
    if not isinstance(raw, dict):
        return {}

    def _flag(*keys) -> bool | None:
        """True si CUALQUIERA es truthy; False si TODOS están presentes y falsy;
        None si NINGUNO está presente."""
        seen = False
        for k in keys:
            if k in raw:
                seen = True
                if raw[k]:
                    return True
        return False if seen else None

    out: dict = {
        "wifi":            _flag("wifi"),
        "perros":          _flag("dogsAllowed"),
        "lavanderia":      _flag("washingMachine"),
        "restaurant":      _flag("foodRestaurant"),
        "mtb_friendly":    _flag("activitiesBiking"),
        "hiking_nearby":   _flag("activitiesHiking"),
        "fishing":         _flag("activitiesFishing"),
        "climbing":        _flag("activitiesClimbing"),
        "winter_friendly": _flag("isWinterReady"),
        "online_booking":  _flag("directBookings"),
    }

    city = _str_nonempty(raw.get("city"))
    if city:
        out["municipio"] = city[:100]

    extras: dict = {}

    # Entorno físico
    env_map = (
        ("surroundingsFarm",   "farm"),
        ("surroundingsForest", "forest"),
        ("surroundingsLake",   "lake"),
        ("surroundingsMeadow", "meadow"),
        ("surroundingsRiver",  "river"),
        ("surroundingsRoad",   "near_road"),
    )
    env_labels = [label for k, label in env_map if raw.get(k)]
    if env_labels:
        extras["environment_labels"] = env_labels

    # Actividades adicionales no mapeadas a v4c
    act_map = (
        ("activitiesSwimming", "swimming"),
        ("activitiesSkiing",   "skiing"),
        ("activitiesSledding", "sledding"),
        ("activitiesSUP",      "sup"),
        ("activitiesHockey",   "hockey"),
    )
    activities = [label for k, label in act_map if raw.get(k)]
    if activities:
        extras["activities"] = activities

    # Comida cercana (cafe/bakery/farm shop/alp + restaurant ya en columna)
    food_map = (
        ("foodBakery",     "bakery"),
        ("foodCafe",       "cafe"),
        ("foodFarmShop",   "farm_shop"),
        ("foodRestaurant", "restaurant"),
        ("foodAlp",        "alp"),
    )
    food = [label for k, label in food_map if raw.get(k)]
    if food:
        extras["food_nearby"] = food

    # Pricing detallado
    pricing: dict = {}
    price_map = (
        ("priceAdult",    "adult"),
        ("priceChild",    "child"),
        ("priceTeen",     "teen"),
        ("priceInfant",   "infant"),
        ("priceDog",      "dog"),
        ("pricePower",    "electricity"),
        ("priceFireWood", "firewood"),
    )
    for k, label in price_map:
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            pricing[label] = round(float(v), 2)
    base = _str_nonempty(raw.get("priceBaseUnit"))
    if base:
        pricing["base_unit"] = base[:30]
    if pricing:
        extras["pricing_breakdown"] = pricing

    # Estancia
    min_n = raw.get("minNights")
    if isinstance(min_n, (int, float)) and not isinstance(min_n, bool) and min_n > 0:
        extras["min_nights"] = int(min_n)
    max_n = raw.get("maxNights")
    if isinstance(max_n, (int, float)) and not isinstance(max_n, bool) and max_n > 0:
        extras["max_nights"] = int(max_n)

    # Booleans → servicios_extras
    bool_extras = (
        ("mobileReception", "cell_service"),
        ("fireplace",       "campfire"),
        ("heating",         "heating"),
        ("kitchen",         "kitchen"),
        ("picknickTable",   "picnic_table"),
        ("trash",           "trash_disposal"),
        ("shelter",         "shelter"),
    )
    for k_raw, k_out in bool_extras:
        v = raw.get(k_raw)
        if isinstance(v, bool):
            extras[k_out] = v

    # Solo si True
    if raw.get("isVerified"):
        extras["verified"] = True
    if raw.get("popularityAward"):
        extras["popularity_award"] = True
    if raw.get("isFamilyFriendly"):
        extras["family_friendly"] = True
    if raw.get("dogsOnlyOnleash"):
        extras["dogs_on_leash_only"] = True

    # Tipo de suelo
    ground = _str_nonempty(raw.get("ground"))
    if ground:
        extras["ground"] = ground[:30]

    if extras:
        out["servicios_extras"] = extras

    return {k: v for k, v in out.items() if v is not None}


def _fs_val(field: Any) -> Any:
    """Desenvuelve un valor de Firestore (`{stringValue: "x"}` → `"x"`, etc.)."""
    if not isinstance(field, dict):
        return None
    if "stringValue" in field:
        return field["stringValue"]
    if "booleanValue" in field:
        return bool(field["booleanValue"])
    if "doubleValue" in field:
        try:
            return float(field["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "integerValue" in field:
        try:
            return int(field["integerValue"])
        except (TypeError, ValueError):
            return None
    if "mapValue" in field:
        inner = (field.get("mapValue") or {}).get("fields") or {}
        return {k: _fs_val(v) for k, v in inner.items()}
    if "arrayValue" in field:
        values = (field.get("arrayValue") or {}).get("values") or []
        return [_fs_val(v) for v in values]
    if "nullValue" in field:
        return None
    return None


def extract_wtmg(raw: dict) -> dict:
    """wtmg (WelcomeToMyGarden): facilities.bonfire/tent no capturados en normalize().

    raw es la respuesta Firestore: `{document: {name, fields: {...}}}`. normalize() ya
    extrae agua_potable (drinkableWater), wc_publico (toilet), electricidad (electricity),
    ducha (shower), num_plazas (capacity), perros y acceso_grandes (inferidos del
    description), fotos, descripcion_<lang>.

    Aquí añadimos:
      - servicios_extras.campfire (facilities.bonfire) — hoguera permitida
      - servicios_extras.tent_allowed (facilities.tent) — caben tiendas
    """
    if not isinstance(raw, dict):
        return {}
    doc = raw.get("document") or {}
    fields = doc.get("fields") if isinstance(doc, dict) else None
    if not isinstance(fields, dict):
        return {}

    facs = _fs_val(fields.get("facilities")) or {}
    if not isinstance(facs, dict):
        return {}

    extras: dict = {}
    bonfire = facs.get("bonfire")
    if isinstance(bonfire, bool):
        extras["campfire"] = bonfire
    tent = facs.get("tent")
    if isinstance(tent, bool):
        extras["tent_allowed"] = tent

    if extras:
        return {"servicios_extras": extras}
    return {}


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
