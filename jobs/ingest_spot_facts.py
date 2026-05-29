"""Ingest scraped service facts from source_records into the semantic pipeline.

Convierte los campos estructurados que los scrapers ya conocen (agua_potable,
electricidad, campfire, environment_labels, etc.) en extracted_claims +
normalized_observations + spot_semantic_state, igual que hace worker.py con
las reviews, pero usando datos declarados por la fuente en lugar de texto libre.

Por qué esto importa
────────────────────
El pipeline de reviews extrae ~1 claim por review de texto. Pero la fuente ya
sabe muchas cosas con alta confianza: park4night sabe si hay agua potable (0.92),
roadsurfer sabe si hay campfire permitido, nomady sabe si hay cobertura móvil.
Estos datos nunca llegaban a spot_semantic_state. Este job lo corrige.

Cómo funciona
─────────────
1. Lee source_records donde extractor_name='scraped_facts_v1' aún no ha
   generado claims (marcador: extracted_claims.extractor_name).
2. Para cada source_record, aplica FIELD_SIGNAL_MAP y EXTRAS_SIGNAL_MAP para
   extraer claims del normalized_data.
3. Inserta en extracted_claims (review_id = NULL, extractor='scraped_facts_v1')
   + normalized_observations, con source_confidence = source_credibility.base_score.
4. Llama a update_semantic_state para la señal afectada.

Idempotencia
────────────
Si se corre dos veces sobre el mismo spot, la segunda vez no hay nada nuevo
(el WHERE excluye spots ya procesados). Para re-procesar, borrar los claims
con extractor_name = 'scraped_facts_v1' del spot y volver a correr.

Uso
───
  # Solo regex + scraped facts, sin LLM, sin reviews:
  python -m jobs.ingest_spot_facts

  # Batch limitado para probar:
  python -m jobs.ingest_spot_facts --batch-size 1000

  # Solo un país:
  python -m jobs.ingest_spot_facts --country ES

  # Dry-run (no escribe en DB):
  python -m jobs.ingest_spot_facts --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enrichment.observation_normalizer import normalize_claim
from enrichment.signal_registry import STATIC_SIGNALS
from enrichment.state_aggregator import update_semantic_state

EXTRACTOR_NAME = "scraped_facts_v1"
EXTRACTOR_VERSION = "2026-05-28"

# ── Mapping: campo en normalized_data → (signal, value_si_true, value_si_false, conf)
# value None = no generar claim para ese valor
FIELD_SIGNAL_MAP: list[tuple[str, str, str | None, str | None, float]] = [
    # (campo,              señal,                 val_true,  val_false, conf)
    ("agua_potable",       "water_working",        "true",    "false",   0.90),
    ("vaciado_negras",     "dump_station_working", "true",    "false",   0.88),
    ("vaciado_grises",     "dump_station_working", "true",    None,      0.82),  # grises solas = parcial
    ("electricidad",       "electricity_working",  "true",    "false",   0.88),
    ("ducha",              "shower_working",       "true",    "false",   0.85),
    ("wifi",               "cell_coverage",        "0.75",    None,      0.72),  # wifi ≠ cobertura pero proxy
    ("acceso_grandes",     "large_vehicle",        "0.85",    "0.15",    0.85),
    ("perros",             "dog_friendly",         "true",    "false",   0.88),
    ("iluminacion",        "stealth",              "0.1",     None,      0.68),  # iluminado = poco discreto
    ("seguridad",          "safety",               "0.8",     None,      0.72),
    ("hiking_nearby",      "hiking_nearby",        "true",    None,      0.80),
    ("mtb_friendly",       "cycling_nearby",       "true",    None,      0.80),
    ("surf_friendly",      "beach_access",         "true",    None,      0.72),
    ("juegos_ninos",       "family_friendly",      "true",    None,      0.82),
    ("mirador",            "beauty",               "0.85",    None,      0.68),
    ("zona_protegida",     "overnight_safe",       "false",   None,      0.72),  # zona prot=True → no pernocta
    ("piscina",            "swimming_access",      "true",    None,      0.88),
    ("climbing",           "hiking_nearby",        "true",    None,      0.68),  # climbing → rutas cerca
    ("reserva_req",        "overnight_safe",       "true",    None,      0.70),  # se puede pernoctar (con reserva)
    # acceso_dificil es columna calculada en algunos scrapers (no en spots canónico)
    ("acceso_dificil",     "road_quality",         "0.2",     "0.85",    0.78),
    ("acepta_caravanas",   "caravan_accepted",     "true",    "false",   0.85),
    ("accesibilidad_reducida", "accessible_pmr",   "true",    None,      0.85),
    ("winter_friendly",    "overnight_safe",       "true",    None,      0.62),  # abierto en invierno
]

# Mapping para campos numéricos directos
NUMERIC_FIELD_MAP: list[tuple[str, str, float]] = [
    # (campo,    señal,              conf)
    ("altura_max_m", "height_restriction", 0.95),
]

# Mapping para num_plazas → parking_capacity (text)
# (<= umbral_small, >= umbral_big)
PLAZAS_SMALL = 5
PLAZAS_BIG = 30

# ── Mapping: servicios_extras JSONB → claims
# Cada entrada: (key, valor_exacto_o_None, señal, valor_claim, conf)
# Si valor_exacto es None → se activa si la key existe y es truthy
# Si valor_exacto es False → se activa si la key existe y es falsy bool

EXTRAS_BOOL_MAP: list[tuple[str, bool | None, str, str, float]] = [
    # campfire
    ("campfire",        True,  "campfire_allowed", "true",  0.88),
    ("campfire",        False, "campfire_allowed", "false", 0.88),
    # bbq (implica algún tipo de fuego permitido)
    ("bbq",             True,  "campfire_allowed", "true",  0.72),
    # EV charging
    ("ev_charging",     True,  "ev_charging",      "true",  0.88),
    ("ev_charging",     False, "ev_charging",      "false", 0.80),
    # cobertura móvil
    ("cell_service",    True,  "cell_coverage",    "0.8",   0.82),
    ("cell_service",    False, "cell_coverage",    "0.1",   0.82),
    # sauna / hot_tub → no hay señal directa, skip
    # family / dogs
    ("family_friendly", True,  "family_friendly",  "true",  0.80),
    ("family_friendly", False, "family_friendly",  "false", 0.75),
    ("dogs_on_leash_only", True, "dog_friendly",   "true",  0.82),  # con correa = sí se admiten
    # hoguera-adjacent
    ("firewood_available", True, "campfire_allowed", "true", 0.68),
    # naturism suele implicar acampar libre
    ("naturism",        True,  "wild_camping_legal", "true", 0.62),
    # requires 4WD → acceso difícil para grandes vehículos
    ("requires_4wd",    True,  "road_quality",     "0.15",  0.85),
    ("requires_4wd",    True,  "large_vehicle",    "0.1",   0.80),
    # trash disposal (no señal directa, skip)
    # tent_friendly / tent_allowed
    ("tent_allowed",    True,  "wild_camping_legal", "true", 0.72),
    ("tent_friendly",   True,  "wild_camping_legal", "true", 0.68),
    # verified (spot oficial → probablemente overnight safe)
    ("verified",        True,  "overnight_safe",   "true",  0.62),
    # alcohol bans, etc. no tienen señal
]

EXTRAS_STRING_MAP: dict[str, dict[str, tuple[str, str, float]]] = {
    "mobile_signal": {
        "3g_4g": ("cell_coverage", "0.8", 0.80),
    },
    "ground": {
        "paved":     ("road_quality", "0.85", 0.80),
        "concrete":  ("road_quality", "0.85", 0.80),
        "asphalt":   ("road_quality", "0.85", 0.80),
        "loose":     ("road_quality", "0.25", 0.75),
        "dirt":      ("road_quality", "0.2",  0.80),
        "gravel":    ("road_quality", "0.3",  0.72),
        "grass":     ("road_quality", "0.5",  0.65),
        "lawn":      ("road_quality", "0.5",  0.65),
        "mud":       ("road_quality", "0.1",  0.78),
    },
}

# environment_labels → señales
ENVIRONMENT_LABEL_MAP: dict[str, tuple[str, str, float]] = {
    "beach":        ("beach_access",   "true",  0.82),
    "sea_coast":    ("beach_access",   "true",  0.78),
    "sea":          ("sea_view",       "true",  0.70),
    "sea_nearby":   ("sea_view",       "true",  0.68),
    "on_water":     ("river_nearby",   "true",  0.65),
    "river":        ("river_nearby",   "true",  0.82),
    "river_nearby": ("river_nearby",   "true",  0.82),
    "lake":         ("lake_nearby",    "true",  0.82),
    "lake_nearby":  ("lake_nearby",    "true",  0.82),
    "mountains":    ("mountain_view",  "true",  0.68),
    "secluded":     ("stealth",        "0.82",  0.78),
    "alleinlage":   ("stealth",        "0.85",  0.80),
    "calm":         ("quietness",      "0.8",   0.70),
    "forest":       ("stealth",        "0.65",  0.65),
    "urban":        ("crowd_level",    "0.7",   0.62),
    "near_road":    ("road_noise",     "0.75",  0.70),
    "carpark":      ("overnight_safe", "true",  0.62),
}

# vibes (alpacacamping, etc.) → señales
VIBE_MAP: dict[str, tuple[str, str, float]] = {
    "starry_sky":  ("dark_sky",  "true",  0.82),
    "pure_nature": ("beauty",    "0.82",  0.65),
    "wow_factor":  ("beauty",    "0.9",   0.68),
    "romantic":    ("beauty",    "0.8",   0.62),
    "secluded":    ("stealth",   "0.82",  0.72),
}

# prohibitions keywords → señales
PROHIBITION_MAP: list[tuple[str, str, str, float]] = [
    # (keyword_en_prohibition, señal, valor, conf)
    ("overnight",     "overnight_safe",    "false", 0.92),
    ("camping",       "overnight_safe",    "false", 0.90),
    ("dogs",          "dog_friendly",      "false", 0.90),
    ("fire",          "campfire_allowed",  "false", 0.90),
    ("campfire",      "campfire_allowed",  "false", 0.92),
    ("noise",         "party_noise",       "0.8",   0.70),
]

# max_vehicle_length_ft → large_vehicle (23ft ≈ 7m)
MAX_VEHICLE_FT_THRESHOLD = 23


# ── Helpers ──────────────────────────────────────────────────────────────────

def _b(v: Any) -> bool | None:
    """Coerce a boolean-like value."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v) if v in (0, 1) else None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1", "si", "sí"):
            return True
        if s in ("false", "no", "0"):
            return False
    return None


def _make_claim(signal: str, value: str, confidence: float,
                excerpt: str = "", extractor: str = EXTRACTOR_NAME) -> dict:
    return {
        "signal": signal,
        "value": value,
        "confidence": confidence,
        "excerpt": excerpt[:200],
        "extractor_name": extractor,
        "extractor_version": EXTRACTOR_VERSION,
    }


def extract_claims_from_source_record(norm: dict, source: str) -> list[dict]:
    """Extrae claims de un normalized_data de source_record.

    norm: el dict normalized_data / normalized (según columna disponible).
    source: nombre de la fuente (para el excerpt).
    Devuelve lista de dicts claim (misma estructura que claim_extractor).
    """
    claims: list[dict] = []
    # Rastreamos por signal (no por signal+value): un source_record solo aporta
    # UN claim por señal. El primero en procesarse gana (orden = prioridad).
    # Esto evita que bbq=True sobreescriba campfire=False para campfire_allowed,
    # y que vaciado_negras y vaciado_grises generen claims duplicados.
    seen: set[str] = set()

    def add(signal: str, value: str, conf: float, note: str = ""):
        if signal in seen:
            return
        if signal not in STATIC_SIGNALS:
            return  # señal desconocida — skip silencioso
        seen.add(signal)
        excerpt = f"[{source}] {note}" if note else f"[{source}]"
        claims.append(_make_claim(signal, value, conf, excerpt))

    # ── Campos booleanos canónicos ──────────────────────────────────────
    for field, signal, val_true, val_false, conf in FIELD_SIGNAL_MAP:
        raw = norm.get(field)
        if raw is None:
            continue
        b = _b(raw)
        if b is True and val_true is not None:
            add(signal, val_true, conf, f"{field}=true")
        elif b is False and val_false is not None:
            add(signal, val_false, conf, f"{field}=false")

    # ── Campos numéricos directos ───────────────────────────────────────
    for field, signal, conf in NUMERIC_FIELD_MAP:
        raw = norm.get(field)
        if raw is None:
            continue
        try:
            num = float(raw)
            if num > 0:
                add(signal, str(round(num, 1)), conf, f"{field}={num}")
        except (TypeError, ValueError):
            pass

    # ── num_plazas → parking_capacity ──────────────────────────────────
    plazas = norm.get("num_plazas")
    if plazas is not None:
        try:
            n = int(plazas)
            if n > 0:
                if n <= PLAZAS_SMALL:
                    add("parking_capacity", "small", 0.85, f"num_plazas={n}")
                elif n >= PLAZAS_BIG:
                    add("parking_capacity", "big", 0.85, f"num_plazas={n}")
        except (TypeError, ValueError):
            pass

    # ── servicios_extras JSONB ──────────────────────────────────────────
    extras = norm.get("servicios_extras") or {}
    if isinstance(extras, str):
        try:
            extras = json.loads(extras)
        except (json.JSONDecodeError, TypeError):
            extras = {}
    if not isinstance(extras, dict):
        extras = {}

    # Booleanos directos en extras
    for key, expected_bool, signal, value, conf in EXTRAS_BOOL_MAP:
        raw = extras.get(key)
        if raw is None:
            continue
        b = _b(raw)
        if b is None:
            # para bbq/campfire a veces viene como string "true"/"false"
            b = _b(str(raw).lower())
        if b is None:
            continue
        if expected_bool is True and b is True:
            add(signal, value, conf, f"extras.{key}=true")
        elif expected_bool is False and b is False:
            add(signal, value, conf, f"extras.{key}=false")
        elif expected_bool is None and b:
            add(signal, value, conf, f"extras.{key}")

    # Strings mapeados en extras
    for key, string_map in EXTRAS_STRING_MAP.items():
        raw = extras.get(key)
        if raw is None:
            continue
        s = str(raw).strip().lower()
        if s in string_map:
            signal, value, conf = string_map[s]
            add(signal, value, conf, f"extras.{key}={s}")

    # environment_labels → señales de entorno
    env_labels = extras.get("environment_labels") or []
    if isinstance(env_labels, list):
        for label in env_labels:
            entry = ENVIRONMENT_LABEL_MAP.get(str(label).lower())
            if entry:
                signal, value, conf = entry
                add(signal, value, conf, f"env={label}")

    # vibes → señales de ambiente
    vibes = extras.get("vibes") or []
    if isinstance(vibes, list):
        for vibe in vibes:
            entry = VIBE_MAP.get(str(vibe).lower())
            if entry:
                signal, value, conf = entry
                add(signal, value, conf, f"vibe={vibe}")

    # prohibitions → señales negativas
    prohibitions = extras.get("prohibitions") or []
    if isinstance(prohibitions, list):
        for p in prohibitions:
            p_low = str(p).lower()
            for kw, signal, value, conf in PROHIBITION_MAP:
                if kw in p_low:
                    add(signal, value, conf, f"prohibition={p}")

    # max_vehicle_length_ft → large_vehicle
    mvl = extras.get("max_vehicle_length_ft")
    if mvl is not None:
        try:
            ft = float(mvl)
            if ft >= MAX_VEHICLE_FT_THRESHOLD:
                add("large_vehicle", "0.85", 0.85, f"max_vehicle={ft}ft")
            elif ft > 0:
                add("large_vehicle", "0.15", 0.88, f"max_vehicle={ft}ft (<{MAX_VEHICLE_FT_THRESHOLD}ft)")
        except (TypeError, ValueError):
            pass

    # activities adicionales → señales
    activities = extras.get("activities") or []
    if isinstance(activities, list):
        acts_low = [str(a).lower() for a in activities]
        if any(a in acts_low for a in ("skiing", "sledding")):
            add("winter_friendly", "true", 0.75, "activity=skiing/sledding")
        if any(a in acts_low for a in ("swimming", "sup", "canoeing", "boating")):
            add("swimming_access", "true", 0.78, f"activity=water_sports")
        if any(a in acts_low for a in ("hiking", "trekking")):
            add("hiking_nearby", "true", 0.78, "activity=hiking")
        if any(a in acts_low for a in ("cycling", "biking", "mtb")):
            add("cycling_nearby", "true", 0.78, "activity=cycling")

    return claims


# ── DB helpers ───────────────────────────────────────────────────────────────

async def _bootstrap_signal_types(conn) -> None:
    """Asegura que todos los signals de STATIC_SIGNALS existen en signal_types.

    Idempotente — ON CONFLICT DO NOTHING garantiza que es seguro llamarlo
    siempre al inicio del job, sin sobrescribir configuración existente.

    Por qué es necesario: extracted_claims.signal_type tiene FK a signal_types.
    Si se añaden nuevas señales a STATIC_SIGNALS sin aplicar la migración SQL
    correspondiente, el job falla con FK violations. Este bootstrap lo previene.
    """
    def _decay_class(half_life_days: int) -> str:
        if half_life_days >= 36500:
            return "permanent"
        if half_life_days >= 365:
            return "slow"
        return "volatile"

    inserted = 0
    for sid, sig in STATIC_SIGNALS.items():
        display_name = sid.replace("_", " ").title()
        decay = _decay_class(sig.half_life_days)
        result = await conn.execute(
            """
            INSERT INTO signal_types (
                id, parent_id, display_name, value_type, decay_class,
                half_life_days, aggregation_strategy, contradiction_strategy,
                importance_weight
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'recent_wins', $8)
            ON CONFLICT (id) DO NOTHING
            """,
            sid,
            sig.parent_id,
            display_name,
            sig.value_type,
            decay,
            sig.half_life_days,
            sig.aggregation_strategy,
            float(sig.importance_weight),
        )
        # asyncpg devuelve "INSERT 0 N" — si N=1 es nuevo
        if result and result.endswith("1"):
            inserted += 1

    if inserted:
        logger.info(f"[facts] Bootstrap: {inserted} nuevas señales insertadas en signal_types")
    else:
        logger.debug(f"[facts] Bootstrap: todas las señales ya existían en signal_types")


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


def _dsn() -> str:
    _load_dotenv()
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "25433")
    name = os.environ.get("DB_NAME") or os.environ.get("POSTGRES_DB", "geospots")
    user = os.environ.get("DB_USER") or os.environ.get("POSTGRES_USER", "geospots")
    password = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD", "geospots")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


async def _fetch_pending_spots(conn, batch_size: int, country: str | None) -> list[dict]:
    """Spots que aún no tienen claims de scraped_facts_v1."""
    where_country = "AND s.country_iso = $2" if country else ""
    params = [batch_size]
    if country:
        params.append(country.upper())

    rows = await conn.fetch(
        f"""
        SELECT s.id AS spot_id, s.country_iso
        FROM spots s
        WHERE s.activo = TRUE
          AND NOT EXISTS (
              SELECT 1 FROM extracted_claims ec
              WHERE ec.spot_id = s.id
                AND ec.extractor_name = '{EXTRACTOR_NAME}'
          )
          {where_country}
        ORDER BY s.total_reviews DESC NULLS LAST, s.id DESC
        LIMIT $1
        """,
        *params,
    )
    return [dict(r) for r in rows]


async def _fetch_source_records(conn, spot_id: int) -> list[dict]:
    """Todos los source_records de un spot con su normalized_data y credibilidad."""
    rows = await conn.fetch(
        """
        SELECT sr.source, sr.normalized_data, sr.normalized,
               sr.last_seen,
               COALESCE(sc.base_score, 0.7) AS source_confidence
        FROM source_records sr
        LEFT JOIN source_credibility sc ON sc.source = sr.source
        WHERE sr.spot_id = $1
          AND (sr.normalized_data IS NOT NULL OR sr.normalized IS NOT NULL)
        """,
        spot_id,
    )
    return [dict(r) for r in rows]


async def _insert_fact_claim(conn, spot_id: int, claim: dict,
                             pipeline_run_id: str) -> int:
    return await conn.fetchval(
        """
        INSERT INTO extracted_claims (
            review_id, spot_id, signal_type, raw_value, extraction_confidence,
            extractor_name, extractor_version, pipeline_run_id, excerpt
        ) VALUES (NULL, $1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        spot_id,
        claim["signal"],
        str(claim["value"]),
        float(claim.get("confidence", 0.9)),
        claim.get("extractor_name", EXTRACTOR_NAME),
        claim.get("extractor_version", EXTRACTOR_VERSION),
        pipeline_run_id,
        claim.get("excerpt"),
    )


async def _insert_observation(conn, claim_id: int, spot_id: int, obs) -> int:
    return await conn.fetchval(
        """
        INSERT INTO normalized_observations (
            claim_id, spot_id, signal_type, value_num, value_bool, value_text,
            extraction_confidence, source_confidence, reviewer_confidence,
            observation_weight, observed_at, date_estimated
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        RETURNING id
        """,
        claim_id, spot_id,
        obs.signal_type, obs.value_num, obs.value_bool, obs.value_text,
        obs.extraction_confidence, obs.source_confidence, obs.reviewer_confidence,
        obs.observation_weight, obs.observed_at, obs.date_estimated,
    )


# ── Procesamiento de un spot ─────────────────────────────────────────────────

async def process_spot(conn, spot_id: int, pipeline_run_id: str,
                       dry_run: bool = False) -> dict:
    source_records = await _fetch_source_records(conn, spot_id)
    if not source_records:
        return {"claims": 0, "observations": 0}

    total_claims = 0
    total_obs = 0

    async with conn.transaction():
        for sr in source_records:
            norm = sr.get("normalized_data") or sr.get("normalized") or {}
            if isinstance(norm, str):
                try:
                    norm = json.loads(norm)
                except (json.JSONDecodeError, TypeError):
                    norm = {}
            if not isinstance(norm, dict) or not norm:
                continue

            source = sr["source"]
            source_conf = float(sr.get("source_confidence") or 0.7)
            # Sprint 3 (BUG-22): `last_seen` es el timestamp de ingesta del
            # scraper, NO la fecha de publicación del hecho. Anclar el decay a
            # esa fecha hacía que TODA señal scrapeada pareciera "recién vista" y
            # ganara a reviews datadas reales (fast-decay: agua/elec/ducha 60d).
            # La marcamos estimada → sin recency boost y con peso penalizado.
            observed_at = sr.get("last_seen") or datetime.now(timezone.utc)

            claims = extract_claims_from_source_record(norm, source)
            if not claims:
                continue

            for claim in claims:
                obs = normalize_claim(
                    claim,
                    source_confidence=source_conf,
                    reviewer_confidence=1.0,
                    observed_at=observed_at,
                    signal_types=STATIC_SIGNALS,
                    date_estimated=True,  # BUG-22: last_seen no es fecha de publicación
                )
                if obs is None:
                    continue

                if not dry_run:
                    claim_id = await _insert_fact_claim(conn, spot_id, claim, pipeline_run_id)
                    await _insert_observation(conn, claim_id, spot_id, obs)
                    await update_semantic_state(conn, spot_id, obs)

                total_claims += 1
                total_obs += 1

    return {"claims": total_claims, "observations": total_obs}


# ── Batch principal ───────────────────────────────────────────────────────────

async def run_ingest(
    batch_size: int = 5000,
    dry_run: bool = False,
    country: str | None = None,
    concurrency: int = 4,
) -> dict:
    pipeline_run_id = str(uuid.uuid4())
    stats = {
        "spots_processed": 0, "spots_with_claims": 0,
        "total_claims": 0, "total_observations": 0,
        "errors": 0, "pipeline_run_id": pipeline_run_id,
    }

    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=1, max_size=concurrency + 2)
    try:
        async with pool.acquire() as conn:
            await _bootstrap_signal_types(conn)
            pending = await _fetch_pending_spots(conn, batch_size, country)

        if not pending:
            logger.info("[facts] No hay spots pendientes de ingestión.")
            return stats

        total = len(pending)
        logger.info(
            f"[facts] {total} spots pendientes | "
            f"country={country or 'ALL'} | dry_run={dry_run} | "
            f"run={pipeline_run_id[:8]}"
        )
        t_start = time.monotonic()
        semaphore = asyncio.Semaphore(concurrency)

        async def _process_one(spot: dict) -> dict:
            async with semaphore:
                async with pool.acquire() as conn:
                    return await process_spot(conn, spot["spot_id"], pipeline_run_id, dry_run)

        tasks = [_process_one(s) for s in pending]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                stats["errors"] += 1
                logger.warning(f"[facts] Error en spot {pending[i]['spot_id']}: {result}")
            else:
                stats["spots_processed"] += 1
                if result["claims"] > 0:
                    stats["spots_with_claims"] += 1
                stats["total_claims"] += result["claims"]
                stats["total_observations"] += result["observations"]

        elapsed = time.monotonic() - t_start
        logger.info(
            f"[facts] Completado en {elapsed:.0f}s | "
            f"spots={stats['spots_processed']} con_claims={stats['spots_with_claims']} "
            f"claims={stats['total_claims']} obs={stats['total_observations']} "
            f"errors={stats['errors']}"
        )
    finally:
        await pool.close()

    return stats


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest scraped spot facts into semantic pipeline")
    parser.add_argument("--batch-size", type=int, default=5000,
                        help="Spots a procesar (default 5000)")
    parser.add_argument("--country", type=str, default=None,
                        help="Filtrar por country_iso (ej: ES, FR, DE)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Procesa pero no escribe en DB")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Conexiones DB en paralelo (default 4)")
    args = parser.parse_args(argv)

    stats = await run_ingest(
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        country=args.country,
        concurrency=args.concurrency,
    )
    logger.info(f"[facts] stats={stats}")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
