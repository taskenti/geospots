"""Phase 4 Google embeddings and hybrid semantic search helpers."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

# text-embedding-004 fue retirado de la API. gemini-embedding-001 es el GA actual;
# nativo 3072 dims pero soporta output_dimensionality → 768 (columna vector(768)).
# Usamos cosine (<=>) en pgvector, invariante a la magnitud, así que no hace falta
# normalizar al truncar dimensiones.
EMBEDDING_MODEL = "gemini-embedding-001"
GOOGLE_EMBEDDING_MODEL = "models/gemini-embedding-001"
EMBEDDING_DIMS = 768

# Intent/respuesta de búsqueda semántica: provider y modelo vienen de ENV
# (ENRICHMENT_PROVIDER + {GEMINI,DEEPSEEK}_ENRICHMENT_MODEL) vía llm_provider.


FILTER_MAP = {
    "quietness_score_min": ("sss.quietness_score >= ${}", float),
    "quietness_score_max": ("sss.quietness_score <= ${}", float),
    "safety_score_min": ("sss.safety_score >= ${}", float),
    "police_risk_score_max": ("COALESCE(sss.police_risk_score, 0) <= ${}", float),
    "beauty_score_min": ("sss.beauty_score >= ${}", float),
    "crowd_level_score_max": ("COALESCE(sss.crowd_level_score, 0) <= ${}", float),
    "overnight_safe": ("sss.overnight_safe = ${}", bool),
    "stealth_score_min": ("sss.stealth_score >= ${}", float),
    "gratuito": ("s.gratuito = ${}", bool),
    "agua_potable": ("s.agua_potable = ${}", bool),
    "electricidad": ("s.electricidad = ${}", bool),
    "ducha": ("s.ducha = ${}", bool),
    "wifi": ("s.wifi = ${}", bool),
    "perros": ("s.perros = ${}", bool),
    "tipo": ("s.tipo = ${}", str),
}

# Canal B — filtros de PROXIMIDAD (distancia máx en km al servicio más cercano).
# Consultan los diccionarios JSONB de spot_geo (sg). Un spot sin esa categoría
# cerca (clave ausente → NULL) queda EXCLUIDO cuando el usuario la exige.
# (filter_key, columna_jsonb, clave_categoria)
_GEO_FILTER_DEFS = [
    ("max_dist_agua_km",        "nearby_osm",   "drinking_water"),
    ("max_dist_vaciado_km",     "nearby_osm",   "dump_station"),
    ("max_dist_super_km",       "nearby_osm",   "supermarket"),
    ("max_dist_gasolinera_km",  "nearby_osm",   "fuel"),
    ("max_dist_farmacia_km",    "nearby_osm",   "pharmacy"),
    ("max_dist_panaderia_km",   "nearby_osm",   "bakery"),
    ("max_dist_restaurante_km", "nearby_osm",   "restaurant"),
    ("max_dist_lavanderia_km",  "nearby_osm",   "laundry"),
    ("max_dist_ev_km",          "nearby_osm",   "ev_charging"),
    ("max_dist_playa_km",       "nearby_osm",   "beach"),
    ("max_dist_mirador_km",     "nearby_osm",   "viewpoint"),
    ("max_dist_area_ac_km",     "nearby_spots", "area_ac"),
    ("max_dist_camping_km",     "nearby_spots", "camping"),
]
for _fk, _col, _jkey in _GEO_FILTER_DEFS:
    FILTER_MAP[_fk] = (f"(sg.{_col}->>'{_jkey}')::float <= ${{}}", float)

INTENT_PROMPT = """Analiza esta busqueda de un usuario que busca spots para pernoctar con autocaravana.
Extrae filtros SQL y una query semantica para busqueda vectorial.

FILTROS DISPONIBLES:
- quietness_score_min, quietness_score_max: REAL 0-1
- safety_score_min: REAL 0-1
- police_risk_score_max: REAL 0-1
- beauty_score_min: REAL 0-1
- crowd_level_score_max: REAL 0-1
- overnight_safe: BOOLEAN
- stealth_score_min: REAL 0-1
- gratuito, agua_potable, electricidad, ducha, wifi, perros: BOOLEAN
- tipo: area_ac, camping, parking_publico, wild, naturaleza, parking

FILTROS DE PROXIMIDAD (km, distancia máxima al MÁS CERCANO; usar solo si el
usuario pide explícitamente algo "cerca"):
- max_dist_agua_km, max_dist_vaciado_km, max_dist_super_km, max_dist_gasolinera_km
- max_dist_farmacia_km, max_dist_panaderia_km, max_dist_restaurante_km
- max_dist_lavanderia_km, max_dist_ev_km, max_dist_playa_km, max_dist_mirador_km
- max_dist_area_ac_km, max_dist_camping_km
Distancia razonable por defecto si no la dan: 1.0 km a pie (agua, super, panadería,
farmacia), 2.0 km en coche (gasolinera, vaciado, playa).

QUERY DEL USUARIO: "{query}"

Responde SOLO JSON:
{{
  "sql_filters": {{"quietness_score_min": 0.7, "overnight_safe": true, "max_dist_super_km": 1.0}},
  "semantic_query": "quiet beach with supermarket nearby, dog friendly",
  "explanation": "playa tranquila con super a menos de 1km y perro"
}}"""


@dataclass(frozen=True)
class SearchIntent:
    sql_filters: dict[str, Any]
    semantic_query: str
    explanation: str = ""
    source: str = "heuristic"

    def as_dict(self) -> dict:
        return {
            "sql_filters": self.sql_filters,
            "semantic_query": self.semantic_query,
            "explanation": self.explanation,
            "source": self.source,
        }


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _google_client():
    _load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required for Phase 4 embeddings")
    from google import genai

    return genai.Client(api_key=api_key)


def vector_literal(values: list[float]) -> str:
    if len(values) != EMBEDDING_DIMS:
        raise ValueError(f"Expected {EMBEDDING_DIMS} dims, got {len(values)}")
    return "[" + ",".join(f"{float(v):.8f}" for v in values) + "]"


def _json_object(value: Any) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return dict(value)


def _score_from_signal(signals_data: dict, signal: str) -> Any:
    item = signals_data.get(signal)
    if isinstance(item, dict):
        return item.get("score")
    return item


# T1.6 — schema version: bumpar SOLO cuando cambie construir_texto_para_embedding
# o cuando cambie el modelo de embeddings. Cualquier cambio aquí invalida TODOS
# los fingerprints y dispara reembedding en la próxima ejecución del cron.
EMBEDDING_SCHEMA_VERSION = "v2"  # v2: contexto de proximidad (Canal A) en el texto


def compute_fingerprint(state_row: dict, *, schema_version: str = EMBEDDING_SCHEMA_VERSION) -> str:
    """T1.6: SHA1[:16] de los componentes relevantes para el embedding.

    Componentes incluidos en el hash (ordenados deterministas):
      spot_id, canonical_tags, active_alert_types, summary_en (o summary),
      best_for, best_season, avoid_season, schema_version.

    NO se incluyen scores continuos (quietness_score, beauty_score, etc.) ni
    `signals_data` — pequeños movimientos numéricos no deben invalidar el embedding
    (D3 del plan). Solo cambios en tags canónicos o estado operativo dispara
    re-embedding.

    Aceptamos dict simulando `state_row`. Si `spot_id` no está, se omite del
    payload (el fingerprint pierde unicidad pero sigue siendo estable).
    """
    import hashlib

    spot_id = state_row.get("spot_id") or state_row.get("id") or ""
    tags = sorted(state_row.get("tags") or [])
    alerts = sorted(state_row.get("active_alert_types") or [])
    summary = (
        state_row.get("summary_en") or state_row.get("summary")
        or state_row.get("summary_es") or ""
    )
    best_for = sorted(state_row.get("best_for") or [])
    best_season = state_row.get("best_season") or ""
    avoid_season = state_row.get("avoid_season") or ""

    payload = "|".join([
        f"spot:{spot_id}",
        f"tags:{tags}",
        f"alerts:{alerts}",
        f"summary:{summary}",
        f"best_for:{best_for}",
        f"best_season:{best_season}",
        f"avoid_season:{avoid_season}",
        f"schema:{schema_version}",
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def construir_texto_para_embedding(spot: dict, state: dict) -> str:
    partes: list[str] = []
    name = spot.get("canonical_name") or spot.get("name") or "Spot sin nombre"
    tipo = spot.get("tipo") or "spot"
    partes.append(f"{name} - {tipo}")

    if spot.get("region"):
        partes.append(f"en {spot['region']}, {(spot.get('country_iso') or '').upper()}")

    if state.get("summary_es"):
        partes.append(state["summary_es"])
    elif state.get("summary_en"):
        partes.append(state["summary_en"])

    if state.get("tags"):
        partes.append(f"Tags: {', '.join(state['tags'])}")
    if state.get("best_for"):
        partes.append(f"Ideal para: {', '.join(state['best_for'])}")
    if state.get("best_season"):
        partes.append(f"Mejor epoca: {state['best_season']}")

    signal_text: list[str] = []
    qs = state.get("quietness_score")
    if qs is not None:
        if qs > 0.7:
            signal_text.append("lugar muy tranquilo y silencioso")
        elif qs < 0.3:
            signal_text.append("ruidoso, cerca de carretera o zona urbana")

    bs = state.get("beauty_score")
    if bs is not None:
        if bs > 0.7:
            signal_text.append("entorno bonito con buenas vistas")
        elif bs < 0.3:
            signal_text.append("entorno poco atractivo")

    ss = state.get("safety_score")
    if ss is not None:
        if ss > 0.7:
            signal_text.append("zona segura")
        elif ss < 0.3:
            signal_text.append("zona con problemas de seguridad")

    ps = state.get("police_risk_score")
    if ps is not None:
        if ps > 0.5:
            signal_text.append("riesgo de control policial o multa")
        elif ps < 0.2:
            signal_text.append("sin problemas con policia")

    if state.get("overnight_safe") is True:
        signal_text.append("se puede pernoctar")
    elif state.get("overnight_safe") is False:
        signal_text.append("pernocta prohibida o arriesgada")

    stealth = state.get("stealth_score")
    if stealth is not None and stealth > 0.7:
        signal_text.append("discreto, bueno para pernocta libre")

    crowd = state.get("crowd_level_score")
    if crowd is not None:
        if crowd > 0.7:
            signal_text.append("muy masificado")
        elif crowd < 0.3:
            signal_text.append("poco frecuentado, solitario")

    sd = _json_object(state.get("signals_data"))
    if _score_from_signal(sd, "sea_view") is True:
        signal_text.append("vistas al mar")
    if _score_from_signal(sd, "mountain_view") is True:
        signal_text.append("vistas a montana")
    if _score_from_signal(sd, "lake_nearby") is True:
        signal_text.append("cerca de un lago")
    if _score_from_signal(sd, "shade_morning") is True:
        signal_text.append("sombra por la manana")
    if _score_from_signal(sd, "shade_afternoon") is True:
        signal_text.append("sombra por la tarde")
    wind = _score_from_signal(sd, "wind_exposure")
    if wind is not None and wind > 0.7:
        signal_text.append("expuesto al viento")
    road_quality = _score_from_signal(sd, "road_quality")
    if road_quality is not None and road_quality < 0.3:
        signal_text.append("acceso dificil, pista o camino en mal estado")
    large_vehicle = _score_from_signal(sd, "large_vehicle")
    if large_vehicle is not None and large_vehicle < 0.3:
        signal_text.append("no apto para vehiculos grandes")

    if signal_text:
        partes.append(". ".join(signal_text))

    servicios: list[str] = []
    for key, label in (
        ("agua_potable", "agua potable"),
        ("electricidad", "electricidad"),
        ("ducha", "ducha"),
        ("wifi", "wifi"),
        ("gratuito", "gratuito"),
        ("perros", "admite perros"),
        ("vaciado_negras", "vaciado de aguas negras"),
    ):
        if spot.get(key):
            servicios.append(label)
    if servicios:
        partes.append(f"Servicios: {', '.join(servicios)}")

    # Canal A — contexto de proximidad (nearby_osm 1a + nearby_spots 1b). Ordenado
    # por distancia, cap a 10 para no diluir el embedding. _NEARBY_LABELS/_fmt_km
    # son globals resueltos en runtime (definidos más abajo en el módulo).
    geo_parts: list[str] = []
    for blob_key in ("nearby_osm", "nearby_spots"):
        d = _json_object(spot.get(blob_key))
        items = sorted(
            ((c, v) for c, v in d.items() if isinstance(v, (int, float))),
            key=lambda kv: kv[1],
        )
        for cat, km in items:
            dist = _fmt_km(km)
            if dist:
                geo_parts.append(f"{_NEARBY_LABELS.get(cat, cat)} a {dist}")
    if geo_parts:
        partes.append("Cerca: " + ", ".join(geo_parts[:10]))

    if state.get("semantic_dsl"):
        partes.append(f"DSL: {state['semantic_dsl']}")

    return ". ".join(part for part in partes if part).strip()


def _embedding_values(embedding_obj: Any) -> list[float]:
    values = getattr(embedding_obj, "values", embedding_obj)
    return [float(v) for v in values]


async def embed_texts(texts: list[str], task_type: str | None = None,
                      chunk: int = 100) -> list[list[float]]:
    """Embeddings con gemini-embedding-001 a EMBEDDING_DIMS.

    task_type: 'RETRIEVAL_DOCUMENT' para spots almacenados, 'RETRIEVAL_QUERY' para
    la query de búsqueda (mejor recuperación). Trocea en sub-lotes (`chunk`) para
    no exceder límites por llamada.
    """
    if not texts:
        return []
    client = _google_client()
    from google.genai import types

    cfg_kwargs: dict = {"output_dimensionality": EMBEDDING_DIMS}
    if task_type:
        cfg_kwargs["task_type"] = task_type
    config = types.EmbedContentConfig(**cfg_kwargs)

    def _call(sub: list[str]):
        result = client.models.embed_content(
            model=GOOGLE_EMBEDDING_MODEL, contents=sub, config=config
        )
        return [_embedding_values(item) for item in result.embeddings]

    out: list[list[float]] = []
    for i in range(0, len(texts), chunk):
        out.extend(await asyncio.to_thread(_call, texts[i:i + chunk]))
    return out


async def fetch_embedding_candidates(conn, batch_size: int, stale_only: bool = False,
                                     country: str | None = None) -> list[dict]:
    # T1.6: semantic_fingerprint reemplaza el comparador `updated_at > created_at`.
    #   - stale_only=False → spots sin embedding (se.spot_id IS NULL).
    #   - stale_only=True  → spots cuyo fingerprint actual != el que generó su embedding.
    # Mientras no haya migrado todo el corpus a v6 (semantic_fingerprint NULL en
    # muchas filas), tratamos NULL como "drift" para forzar re-embedding una vez.
    if stale_only:
        stale_clause = (
            "AND se.spot_id IS NOT NULL "
            "AND (se.built_from_fingerprint IS NULL "
            "     OR sss.semantic_fingerprint IS NULL "
            "     OR se.built_from_fingerprint <> sss.semantic_fingerprint)"
        )
    else:
        stale_clause = "AND se.spot_id IS NULL"
    country_clause = "AND s.country_iso = $2" if country else ""
    params: list[Any] = [batch_size] + ([country.lower()] if country else [])
    rows = await conn.fetch(
        f"""
        SELECT s.id, s.canonical_name, s.tipo, s.region, s.country_iso,
               s.gratuito, s.agua_potable, s.electricidad, s.ducha,
               s.wifi, s.perros, s.vaciado_negras,
               sss.quietness_score, sss.safety_score, sss.police_risk_score,
               sss.beauty_score, sss.crowd_level_score, sss.overnight_safe,
               sss.stealth_score, sss.signals_data,
               sss.summary_es, sss.summary_en, sss.tags, sss.best_for,
               sss.best_season, sss.semantic_dsl,
               sss.active_alert_types, sss.semantic_fingerprint,
               sg.nearby_osm, sg.nearby_spots
        FROM spots s
        JOIN spot_semantic_state sss ON sss.spot_id = s.id
        LEFT JOIN spot_embeddings se ON se.spot_id = s.id
        LEFT JOIN spot_geo sg ON sg.spot_id = s.id
        WHERE s.activo = TRUE
          AND sss.total_observations > 0
          {stale_clause}
          {country_clause}
        ORDER BY s.total_reviews DESC NULLS LAST, s.id
        LIMIT $1
        """,
        *params,
    )
    return [dict(r) for r in rows]


async def generar_embeddings_batch(pool, batch_size: int = 100, country: str | None = None) -> dict:
    async with pool.acquire() as conn:
        spots = await fetch_embedding_candidates(conn, batch_size, stale_only=False, country=country)
    if not spots:
        return {"processed": 0, "model": EMBEDDING_MODEL}

    texts = [construir_texto_para_embedding(spot, spot) for spot in spots]
    # T1.6: snapshot del fingerprint del estado actual — se persiste con el embedding.
    fingerprints = [
        spot.get("semantic_fingerprint") or compute_fingerprint(spot)
        for spot in spots
    ]
    embeddings = await embed_texts(texts, task_type="RETRIEVAL_DOCUMENT")
    async with pool.acquire() as conn:
        async with conn.transaction():
            for spot, values, text, fp in zip(spots, embeddings, texts, fingerprints):
                await conn.execute(
                    """
                    INSERT INTO spot_embeddings
                        (spot_id, embedding, texto_fuente, model, built_from_fingerprint, created_at)
                    VALUES ($1, $2::vector, $3, $4, $5, NOW())
                    ON CONFLICT (spot_id) DO UPDATE SET
                        embedding              = EXCLUDED.embedding,
                        texto_fuente           = EXCLUDED.texto_fuente,
                        model                  = EXCLUDED.model,
                        built_from_fingerprint = EXCLUDED.built_from_fingerprint,
                        created_at             = NOW()
                    """,
                    spot["id"],
                    vector_literal(values),
                    text,
                    EMBEDDING_MODEL,
                    fp,
                )
    return {"processed": len(spots), "model": EMBEDDING_MODEL}


async def regenerar_embeddings_stale(pool, batch_size: int = 100, country: str | None = None) -> dict:
    async with pool.acquire() as conn:
        stale = await fetch_embedding_candidates(conn, batch_size, stale_only=True, country=country)
    if not stale:
        return {"processed": 0, "model": EMBEDDING_MODEL}
    texts = [construir_texto_para_embedding(spot, spot) for spot in stale]
    fingerprints = [
        spot.get("semantic_fingerprint") or compute_fingerprint(spot)
        for spot in stale
    ]
    embeddings = await embed_texts(texts, task_type="RETRIEVAL_DOCUMENT")
    async with pool.acquire() as conn:
        async with conn.transaction():
            for spot, values, text, fp in zip(stale, embeddings, texts, fingerprints):
                await conn.execute(
                    """
                    UPDATE spot_embeddings
                    SET embedding              = $2::vector,
                        texto_fuente           = $3,
                        model                  = $4,
                        built_from_fingerprint = $5,
                        created_at             = NOW()
                    WHERE spot_id = $1
                    """,
                    spot["id"],
                    vector_literal(values),
                    text,
                    EMBEDDING_MODEL,
                    fp,
                )
    return {"processed": len(stale), "model": EMBEDDING_MODEL}


def _parse_json_response(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.IGNORECASE | re.MULTILINE).strip()
    return json.loads(cleaned)


def extraer_intencion_heuristica(query: str) -> dict:
    q = query.lower()
    filters: dict[str, Any] = {}
    if any(word in q for word in ("tranquil", "tranquilo", "tranquila", "quiet", "calm", "silenc")):
        filters["quietness_score_min"] = 0.7
    if any(word in q for word in ("seguro", "safe", "safety", "sicher")):
        filters["safety_score_min"] = 0.6
    if any(word in q for word in ("bonito", "buenas vistas", "beautiful", "view", "vistas")):
        filters["beauty_score_min"] = 0.6
    if any(word in q for word in ("policia", "police", "multa", "fine", "molesten", "control")):
        filters["police_risk_score_max"] = 0.3
        filters["stealth_score_min"] = 0.6
    if any(word in q for word in ("pernoct", "overnight", "dormir", "sleep")):
        filters["overnight_safe"] = True
    if any(word in q for word in ("solitario", "solo", "poca gente", "sin gente", "uncrowded")):
        filters["crowd_level_score_max"] = 0.4
    if any(word in q for word in ("perro", "dog", "dogs", "chien", "hund")):
        filters["perros"] = True
    if any(word in q for word in ("gratis", "gratuito", "free")):
        filters["gratuito"] = True
    if "wifi" in q:
        filters["wifi"] = True
    if any(word in q for word in ("ducha", "shower", "douche")):
        filters["ducha"] = True
    if any(word in q for word in ("agua", "water", "eau", "wasser")):
        filters["agua_potable"] = True
    if any(word in q for word in ("camping", "campsite")):
        filters["tipo"] = "camping"
    elif any(word in q for word in ("wild", "naturaleza", "libre")):
        filters["tipo"] = "naturaleza"

    return SearchIntent(
        sql_filters=filters,
        semantic_query=query,
        explanation="heuristic fallback",
        source="heuristic",
    ).as_dict()


async def extraer_intencion(query: str, use_gemini: bool = True) -> dict:
    """Extrae intent vía LLM (provider activo via ENV). `use_gemini` mantiene el
    nombre por compat; activa el LLM (sea Gemini o DeepSeek)."""
    if not use_gemini:
        return extraer_intencion_heuristica(query)
    try:
        from .llm_provider import call_llm_sync
        prompt = INTENT_PROMPT.format(query=query.replace('"', '\\"'))
        resp = await asyncio.to_thread(
            call_llm_sync, prompt, system_prompt="", response_format="json",
        )
        data = _parse_json_response(resp.text or "")
        filters = data.get("sql_filters") or {}
        clean_filters = {k: v for k, v in filters.items() if k in FILTER_MAP and v is not None}
        return SearchIntent(
            sql_filters=clean_filters,
            semantic_query=data.get("semantic_query") or query,
            explanation=data.get("explanation", ""),
            source=resp.provider,
        ).as_dict()
    except Exception as exc:
        logger.warning(f"[semantic_search] Intent extraction fallback: {exc}")
        return extraer_intencion_heuristica(query)


async def buscar_spots(
    conn,
    query: str,
    lat: float,
    lon: float,
    radio_km: float = 50,
    limit: int = 20,
    use_gemini_intent: bool = True,
) -> tuple[list[dict], dict]:
    intent = await extraer_intencion(query, use_gemini=use_gemini_intent)
    semantic_query = intent.get("semantic_query") or query
    query_embedding = vector_literal((await embed_texts([semantic_query], task_type="RETRIEVAL_QUERY"))[0])

    where_parts = [
        "s.activo = TRUE",
        "ST_DWithin(s.geog, ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography, $4)",
    ]
    params: list[Any] = [query_embedding, lat, lon, radio_km * 1000]
    idx = 5

    for key, (template, cast) in FILTER_MAP.items():
        value = intent.get("sql_filters", {}).get(key)
        if value is None:
            continue
        where_parts.append(template.format(idx))
        params.append(cast(value))
        idx += 1

    params.append(limit)
    where_clause = " AND ".join(where_parts)
    rows = await conn.fetch(
        f"""
        SELECT
            s.id, s.canonical_name, s.tipo, s.lat, s.lon,
            s.gratuito, s.agua_potable, s.master_rating, s.total_reviews,
            s.perros, s.ducha, s.wifi, s.electricidad,
            sss.quietness_score, sss.safety_score, sss.beauty_score,
            sss.police_risk_score, sss.stealth_score, sss.crowd_level_score,
            sss.overnight_safe, sss.semantic_dsl,
            sss.summary_es, sss.tags, sss.best_for,
            sg.nearby_osm, sg.nearby_spots,
            1 - (se.embedding <=> $1::vector) AS similarity,
            ST_Distance(s.geog, ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography) / 1000 AS dist_km
        FROM spots s
        JOIN spot_embeddings se ON se.spot_id = s.id
        JOIN spot_semantic_state sss ON sss.spot_id = s.id
        LEFT JOIN spot_geo sg ON sg.spot_id = s.id
        WHERE {where_clause}
        ORDER BY similarity DESC
        LIMIT ${idx}
        """,
        *params,
    )
    return [dict(r) for r in rows], intent


# Etiquetas ES para las categorías de proximidad (osm + spots).
_NEARBY_LABELS = {
    "drinking_water": "agua", "dump_station": "vaciado", "supermarket": "super",
    "fuel": "gasolinera", "pharmacy": "farmacia", "viewpoint": "mirador",
    "bakery": "panaderia", "laundry": "lavanderia", "restaurant": "restaurante",
    "ev_charging": "recarga EV", "beach": "playa",
    "area_ac": "area AC", "camping": "camping", "spot_vaciado": "spot con vaciado",
}


def _fmt_km(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return f"{int(v*1000)}m" if v < 1 else f"{v:.1f}km"


def _entorno_str(s: dict) -> str:
    """Contexto de proximidad legible para el prompt (Canal C). Combina nearby_osm
    (1a) y nearby_spots (1b). Distancia en LÍNEA RECTA por ahora; pendiente
    carretera donde aplique (ver docs/diseno-distancias-y-contexto.md)."""
    parts: list[str] = []
    for blob in (s.get("nearby_osm"), s.get("nearby_spots")):
        d = _json_object(blob)
        for cat, km in d.items():
            label = _NEARBY_LABELS.get(cat, cat)
            dist = _fmt_km(km)
            if dist:
                parts.append(f"{label} {dist}")
    return "; ".join(parts)


async def generar_respuesta_busqueda(query: str, spots: list[dict]) -> str:
    if not spots:
        return ""
    from .llm_provider import call_llm_sync

    def _line(i: int, s: dict) -> str:
        base = (
            f"#{i + 1} {s['canonical_name']} ({s['tipo']}, {float(s['dist_km']):.1f}km, "
            f"sim={float(s['similarity']):.2f}): {s.get('semantic_dsl') or 'N/A'}"
        )
        entorno = _entorno_str(s)
        if entorno:
            base += f" | entorno: {entorno}"
        return base

    contexto = "\n".join(_line(i, s) for i, s in enumerate(spots[:10]))
    prompt = (
        f'El usuario busca: "{query}"\n\n'
        f"Top spots encontrados (DSL semantico + entorno cercano):\n{contexto}\n\n"
        "Recomienda los mejores. Se conciso y directo. Usa el nombre del spot y la "
        "distancia. Si el usuario pide servicios cercanos (agua, super, vaciado, etc.) "
        "y aparecen en 'entorno', mencionalos con su distancia."
    )
    resp = await asyncio.to_thread(
        call_llm_sync, prompt, system_prompt="", response_format="text",
    )
    return resp.text or ""
