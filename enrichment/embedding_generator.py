"""Phase 4 Google embeddings and hybrid semantic search helpers."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

EMBEDDING_MODEL = "text-embedding-004"
GOOGLE_EMBEDDING_MODEL = "models/text-embedding-004"
INTENT_MODEL = "gemini-2.0-flash"
EMBEDDING_DIMS = 768


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

QUERY DEL USUARIO: "{query}"

Responde SOLO JSON:
{{
  "sql_filters": {{"quietness_score_min": 0.7, "overnight_safe": true}},
  "semantic_query": "quiet beach with shade, dog friendly",
  "explanation": "busca playa tranquila con sombra y perro"
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

    if state.get("semantic_dsl"):
        partes.append(f"DSL: {state['semantic_dsl']}")

    return ". ".join(part for part in partes if part).strip()


def _embedding_values(embedding_obj: Any) -> list[float]:
    values = getattr(embedding_obj, "values", embedding_obj)
    return [float(v) for v in values]


async def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    client = _google_client()

    def _call():
        result = client.models.embed_content(model=GOOGLE_EMBEDDING_MODEL, contents=texts)
        return [_embedding_values(item) for item in result.embeddings]

    return await asyncio.to_thread(_call)


async def fetch_embedding_candidates(conn, batch_size: int, stale_only: bool = False) -> list[dict]:
    stale_clause = "AND se.spot_id IS NOT NULL AND sss.updated_at > se.created_at" if stale_only else "AND se.spot_id IS NULL"
    rows = await conn.fetch(
        f"""
        SELECT s.id, s.canonical_name, s.tipo, s.region, s.country_iso,
               s.gratuito, s.agua_potable, s.electricidad, s.ducha,
               s.wifi, s.perros, s.vaciado_negras,
               sss.quietness_score, sss.safety_score, sss.police_risk_score,
               sss.beauty_score, sss.crowd_level_score, sss.overnight_safe,
               sss.stealth_score, sss.signals_data,
               sss.summary_es, sss.summary_en, sss.tags, sss.best_for,
               sss.best_season, sss.semantic_dsl
        FROM spots s
        JOIN spot_semantic_state sss ON sss.spot_id = s.id
        LEFT JOIN spot_embeddings se ON se.spot_id = s.id
        WHERE s.activo = TRUE
          AND sss.total_observations > 0
          {stale_clause}
        ORDER BY s.total_reviews DESC NULLS LAST, s.id
        LIMIT $1
        """,
        batch_size,
    )
    return [dict(r) for r in rows]


async def generar_embeddings_batch(pool, batch_size: int = 100) -> dict:
    async with pool.acquire() as conn:
        spots = await fetch_embedding_candidates(conn, batch_size, stale_only=False)
    if not spots:
        return {"processed": 0, "model": EMBEDDING_MODEL}

    texts = [construir_texto_para_embedding(spot, spot) for spot in spots]
    embeddings = await embed_texts(texts)
    async with pool.acquire() as conn:
        async with conn.transaction():
            for spot, values, text in zip(spots, embeddings, texts):
                await conn.execute(
                    """
                    INSERT INTO spot_embeddings (spot_id, embedding, texto_fuente, model, created_at)
                    VALUES ($1, $2::vector, $3, $4, NOW())
                    ON CONFLICT (spot_id) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        texto_fuente = EXCLUDED.texto_fuente,
                        model = EXCLUDED.model,
                        created_at = NOW()
                    """,
                    spot["id"],
                    vector_literal(values),
                    text,
                    EMBEDDING_MODEL,
                )
    return {"processed": len(spots), "model": EMBEDDING_MODEL}


async def regenerar_embeddings_stale(pool, batch_size: int = 100) -> dict:
    async with pool.acquire() as conn:
        stale = await fetch_embedding_candidates(conn, batch_size, stale_only=True)
    if not stale:
        return {"processed": 0, "model": EMBEDDING_MODEL}
    texts = [construir_texto_para_embedding(spot, spot) for spot in stale]
    embeddings = await embed_texts(texts)
    async with pool.acquire() as conn:
        async with conn.transaction():
            for spot, values, text in zip(stale, embeddings, texts):
                await conn.execute(
                    """
                    UPDATE spot_embeddings
                    SET embedding = $2::vector,
                        texto_fuente = $3,
                        model = $4,
                        created_at = NOW()
                    WHERE spot_id = $1
                    """,
                    spot["id"],
                    vector_literal(values),
                    text,
                    EMBEDDING_MODEL,
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
    if not use_gemini:
        return extraer_intencion_heuristica(query)
    try:
        client = _google_client()
        prompt = INTENT_PROMPT.format(query=query.replace('"', '\\"'))

        def _call():
            return client.models.generate_content(model=INTENT_MODEL, contents=prompt)

        response = await asyncio.to_thread(_call)
        data = _parse_json_response(response.text or "")
        filters = data.get("sql_filters") or {}
        clean_filters = {k: v for k, v in filters.items() if k in FILTER_MAP and v is not None}
        return SearchIntent(
            sql_filters=clean_filters,
            semantic_query=data.get("semantic_query") or query,
            explanation=data.get("explanation", ""),
            source="gemini",
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
    query_embedding = vector_literal((await embed_texts([semantic_query]))[0])

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
            1 - (se.embedding <=> $1::vector) AS similarity,
            ST_Distance(s.geog, ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography) / 1000 AS dist_km
        FROM spots s
        JOIN spot_embeddings se ON se.spot_id = s.id
        JOIN spot_semantic_state sss ON sss.spot_id = s.id
        WHERE {where_clause}
        ORDER BY similarity DESC
        LIMIT ${idx}
        """,
        *params,
    )
    return [dict(r) for r in rows], intent


async def generar_respuesta_busqueda(query: str, spots: list[dict]) -> str:
    if not spots:
        return ""
    client = _google_client()
    contexto = "\n".join(
        (
            f"#{i + 1} {s['canonical_name']} ({s['tipo']}, {float(s['dist_km']):.1f}km, "
            f"sim={float(s['similarity']):.2f}): {s.get('semantic_dsl') or 'N/A'}"
        )
        for i, s in enumerate(spots[:10])
    )
    prompt = (
        f'El usuario busca: "{query}"\n\n'
        f"Top spots encontrados (DSL semantico):\n{contexto}\n\n"
        "Recomienda los mejores. Se conciso y directo. Usa el nombre del spot y la distancia."
    )

    def _call():
        return client.models.generate_content(model=INTENT_MODEL, contents=prompt)

    response = await asyncio.to_thread(_call)
    return response.text or ""
