"""Motor de reconciliación multi-fuente para GeoSpots — PR11.

Cambios PR11 vs versión rank-first:
  - `_reconciliar_campo` ahora hace **votación ponderada** por
    source_credibility.base_score (ya no "primera fuente del ranking gana").
  - Empate técnico (margen < 10%) → no toca la columna, asume que el valor
    actual en spots es correcto.
  - Se preserva la lista CREDIBILITY hardcoded como **desempate de coherencia**:
    si dos fuentes tienen el mismo peso (típico cuando no están en
    source_credibility), gana la que aparezca antes en la lista.
  - Nuevo `compute_temporal_overrides`: lee `spot_semantic_state.signals_data`
    para `water_working` / `electricity_working` / `dump_station_working` y,
    si el consenso semántico negativo es fuerte, crea/refresca una fila en
    `spot_field_overrides` con TTL = half_life del signal.
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from loguru import logger

from db import _limpiar_web


# ─────────────────────────────────────────────────────────────────────
# Ranking hardcoded — usado como desempate cuando 2 fuentes empatan en
# weighted vote (típicamente porque no están en source_credibility o
# porque tienen el mismo base_score). NO se usa como prioridad principal.
# ─────────────────────────────────────────────────────────────────────
CREDIBILITY = {
    "tipo":               ["campingcarpark", "campercontact", "bobilguiden", "park4night", "thedyrt", "campendium", "freecampsites", "promobil", "areasac", "campingcarinfos", "agricamper", "campy", "furgovw", "stayfree", "wtmg", "campspace", "alpacacamping", "vansite", "roadsurfer", "womostell", "searchforsites", "osm", "amigosac", "ioverlander"],
    "precio_info":        ["campingcarpark", "campercontact", "bobilguiden", "promobil", "thedyrt", "campendium", "freecampsites", "alpacacamping", "womostell", "areasac", "campingcarinfos", "agricamper", "campy", "park4night", "stayfree", "vansite", "roadsurfer", "searchforsites", "furgovw", "campspace", "amigosac"],
    "precio_aprox":       ["campingcarpark", "campercontact", "bobilguiden", "promobil", "thedyrt", "campendium", "freecampsites", "alpacacamping", "womostell", "areasac", "campingcarinfos", "agricamper", "campy", "park4night", "stayfree", "vansite", "roadsurfer", "searchforsites", "furgovw", "campspace", "amigosac"],
    "gratuito":           ["campingcarpark", "campercontact", "bobilguiden", "promobil", "thedyrt", "campendium", "freecampsites", "alpacacamping", "womostell", "areasac", "campingcarinfos", "agricamper", "campy", "park4night", "stayfree", "vansite", "roadsurfer", "searchforsites", "furgovw", "wtmg", "campspace", "amigosac", "ioverlander"],
    "agua_potable":       ["campingcarpark", "promobil", "areasac", "campingcarinfos", "agricamper", "campercontact", "bobilguiden", "campy", "park4night", "thedyrt", "campendium", "freecampsites", "womostell", "stayfree", "alpacacamping", "wtmg", "vansite", "roadsurfer", "searchforsites", "campspace", "osm", "amigosac", "ioverlander"],
    "electricidad":       ["campingcarpark", "promobil", "areasac", "campingcarinfos", "agricamper", "campercontact", "bobilguiden", "campy", "park4night", "thedyrt", "campendium", "freecampsites", "womostell", "stayfree", "alpacacamping", "wtmg", "vansite", "roadsurfer", "searchforsites", "campspace", "osm", "amigosac", "ioverlander"],
    "ducha":              ["campingcarpark", "promobil", "areasac", "campingcarinfos", "agricamper", "campercontact", "bobilguiden", "campy", "park4night", "thedyrt", "campendium", "freecampsites", "womostell", "stayfree", "alpacacamping", "wtmg", "vansite", "roadsurfer", "searchforsites", "campspace", "amigosac", "ioverlander"],
    "wifi":               ["campingcarpark", "promobil", "campercontact", "bobilguiden", "campy", "park4night", "thedyrt", "campendium", "freecampsites", "womostell", "campingcarinfos", "agricamper", "vansite", "roadsurfer", "alpacacamping", "searchforsites", "campspace", "amigosac", "ioverlander"],
    "wc_publico":         ["campingcarpark", "promobil", "areasac", "campingcarinfos", "agricamper", "campercontact", "bobilguiden", "campy", "park4night", "thedyrt", "campendium", "freecampsites", "womostell", "stayfree", "alpacacamping", "wtmg", "vansite", "roadsurfer", "searchforsites", "campspace", "amigosac", "ioverlander"],
    "vaciado_negras":     ["campingcarpark", "promobil", "areasac", "campingcarinfos", "agricamper", "campercontact", "bobilguiden", "campy", "park4night", "thedyrt", "campendium", "womostell", "stayfree", "alpacacamping", "roadsurfer", "searchforsites", "campspace", "amigosac"],
    "vaciado_grises":     ["campingcarpark", "promobil", "areasac", "campingcarinfos", "agricamper", "campercontact", "bobilguiden", "campy", "park4night", "thedyrt", "campendium", "womostell", "stayfree", "alpacacamping", "roadsurfer", "searchforsites", "campspace", "amigosac"],
    "num_plazas":         ["campingcarpark", "promobil", "campercontact", "bobilguiden", "thedyrt", "campendium", "areasac", "campingcarinfos", "agricamper", "campy", "park4night", "womostell", "wtmg", "vansite", "roadsurfer", "campspace"],
    "acceso_grandes":     ["campingcarpark", "promobil", "campercontact", "areasac", "campingcarinfos", "agricamper", "campy", "park4night", "thedyrt", "campendium", "freecampsites", "womostell", "wtmg", "vansite", "roadsurfer", "alpacacamping", "searchforsites", "campspace"],
    "perros":             ["campingcarpark", "promobil", "campercontact", "park4night", "thedyrt", "campendium", "freecampsites", "womostell", "campingcarinfos", "agricamper", "campy", "wtmg", "vansite", "roadsurfer", "alpacacamping", "searchforsites", "campspace", "amigosac", "ioverlander"],
    "altura_max_m":       ["park4night", "campercontact"],
    "reserva_req":        ["campspace", "thedyrt", "campendium", "campercontact", "park4night", "womostell"],
    "iluminacion":        ["campercontact", "park4night"],
    "seguridad":          ["campercontact", "park4night"],
    "canonical_name":     ["google_maps", "campingcarpark", "promobil", "campercontact", "bobilguiden", "park4night", "thedyrt", "campendium", "freecampsites", "areasac", "campingcarinfos", "agricamper", "campy", "furgovw", "stayfree", "alpacacamping", "womostell", "wtmg", "vansite", "roadsurfer", "searchforsites", "osm", "amigosac", "ioverlander"],
    "temporada_apertura": ["campercontact", "park4night", "areasac", "womostell", "searchforsites"],
    "descripcion_es":     ["google_maps", "furgovw", "stayfree", "park4night", "areasac", "campingcarinfos", "agricamper", "wtmg", "campercontact", "vansite", "amigosac"],
    "descripcion_en":     ["google_maps", "thedyrt", "campendium", "freecampsites", "park4night", "stayfree", "campercontact", "bobilguiden", "campy", "agricamper", "wtmg", "vansite", "roadsurfer", "searchforsites", "campspace", "ioverlander"],
    "descripcion_fr":     ["google_maps", "campingcarpark", "park4night", "campingcarinfos", "agricamper", "wtmg", "campercontact", "vansite"],
    "descripcion_de":     ["google_maps", "promobil", "campy", "alpacacamping", "womostell", "park4night", "agricamper", "wtmg", "campercontact", "vansite", "roadsurfer"],
    "master_rating":      ["google_maps_api", "google_maps", "campingcarpark", "promobil", "campercontact", "bobilguiden", "park4night", "thedyrt", "campendium", "freecampsites", "stayfree", "alpacacamping", "womostell", "areasac", "campingcarinfos", "agricamper", "campy", "vansite", "roadsurfer", "searchforsites", "furgovw", "wtmg", "campspace"],
    # ── Contacto ──────────────────────────────────────────────────────
    # Telefono: fuentes oficiales/locales primero; Google como apoyo fiable.
    "telefono":           ["areasac", "campingcarpark", "agricamper", "campercontact", "promobil", "google_maps_api", "park4night", "thedyrt", "campendium", "campy", "bobilguiden", "womostell", "searchforsites"],
    # Web: la web oficial scrapeada por fuentes locales gana; Google de apoyo.
    # _limpiar_web() se aplica en job_reconciliar para descartar dominios de agregador.
    "web":                ["areasac", "agricamper", "campingcarpark", "google_maps_api", "promobil", "campercontact", "park4night", "thedyrt", "campendium", "campy", "bobilguiden", "womostell", "searchforsites"],
    # Direccion formateada: solo Google la provee de momento.
    "direccion_formateada": ["google_maps_api", "campingcarinfos", "campercontact", "areasac", "park4night"],
}

CONFLICT_FIELDS = ["gratuito", "precio_info", "agua_potable", "electricidad", "num_plazas", "tipo"]

DB_TO_NORM_KEY = {
    "canonical_name": "nombre",
    "master_rating": "rating_promedio",
    "total_reviews": "num_reviews",
}

# Campos donde el voto ponderado es semánticamente correcto. Para textos
# largos (descripciones) el voto exacto no tiene sentido (cada fuente tiene
# su prosa) → caemos en rank-first.
WEIGHTED_VOTE_FIELDS = {
    "tipo", "gratuito", "agua_potable", "electricidad", "ducha", "wifi",
    "wc_publico", "vaciado_negras", "vaciado_grises", "acceso_grandes",
    "perros", "reserva_req", "iluminacion", "seguridad",
    "num_plazas", "altura_max_m", "precio_aprox",
}

# Margen mínimo (% del peso total) para que el ganador "limpio" se imponga.
# Por debajo de esto consideramos empate técnico → no tocar la columna.
TIE_MARGIN = 0.10

# Sentinel para "no actualices este campo" devuelto por _reconciliar_campo
# cuando hay empate técnico.
KEEP_EXISTING = object()

# Mapeo signal_type → campo canónico para los overrides temporales.
# dump_station_working es genérico — afecta a ambos campos de vaciado.
SIGNAL_TO_FIELDS: dict[str, tuple[str, ...]] = {
    "water_working":        ("agua_potable",),
    "electricity_working":  ("electricidad",),
    "dump_station_working": ("vaciado_negras", "vaciado_grises"),
}

# Umbral strict para disparar un override temporal (decisión PR11).
OVERRIDE_MIN_OBSERVATIONS = 2
OVERRIDE_MIN_WEIGHT       = 1.5
OVERRIDE_MIN_CONFIDENCE   = 0.7

# Cache de signal_types.half_life_days. Se carga una vez por job.
_HALF_LIVES_CACHE: dict[str, int] = {}


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _vote_key(v) -> str:
    """Serializa un valor para usarlo como clave de voto. Booleanos y nums
    quedan como str(); dicts/listas como JSON canónico."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, default=str)
    return str(v)


def _reconciliar_campo(records: dict, campo: str, credibility: dict[str, float]):
    """Devuelve (valor, fuente) reconciliado, o (KEEP_EXISTING, None) si empate.

    Estrategia:
      - Para campos en WEIGHTED_VOTE_FIELDS: voto ponderado por credibilidad.
        Empate técnico (margen < TIE_MARGIN) → KEEP_EXISTING.
      - Para los demás (descripciones, nombre, etc.): rank-first sobre la
        lista CREDIBILITY (comportamiento histórico, que para texto es lo
        que tiene sentido — no votamos prosa).
    """
    norm_key = DB_TO_NORM_KEY.get(campo, campo)

    # ── Texto / nombre / descripciones: rank-first (comportamiento histórico) ──
    if campo not in WEIGHTED_VOTE_FIELDS:
        for fuente in CREDIBILITY.get(campo, []):
            data = records.get(fuente, {})
            val = data.get(norm_key)
            if val is not None:
                return val, fuente
        for fuente, data in records.items():
            val = data.get(norm_key)
            if val is not None:
                return val, fuente
        return None, None

    # ── Voto ponderado ──
    votes: dict[str, float] = defaultdict(float)
    # Por cada bucket de voto, guardamos un (source, original_value) representativo
    # para poder devolver el tipo Python original.
    witnesses: dict[str, tuple[str, object]] = {}

    rank = CREDIBILITY.get(campo, [])
    rank_pos = {src: i for i, src in enumerate(rank)}  # menor = más preferente

    for source, data in records.items():
        val = data.get(norm_key)
        if val is None:
            continue
        weight = credibility.get(source, 0.5)
        key = _vote_key(val)
        votes[key] += weight
        # Witness: el primero por rank-position (más alto del ranking hardcoded)
        cur = witnesses.get(key)
        if cur is None or rank_pos.get(source, 1e9) < rank_pos.get(cur[0], 1e9):
            witnesses[key] = (source, val)

    if not votes:
        return None, None

    total = sum(votes.values())
    sorted_votes = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
    winner_key, winner_w = sorted_votes[0]
    second_w = sorted_votes[1][1] if len(sorted_votes) > 1 else 0.0
    margin = (winner_w - second_w) / total if total > 0 else 1.0

    if margin < TIE_MARGIN:
        return KEEP_EXISTING, None

    src, val = witnesses[winner_key]
    return val, src


def _detectar_conflictos(records: dict) -> list[dict]:
    conflictos = []
    for campo in CONFLICT_FIELDS:
        valores = {}
        for fuente, data in records.items():
            v = data.get(campo)
            if v is not None:
                valores[fuente] = v
        if len(valores) >= 2 and len(set(str(v) for v in valores.values())) > 1:
            conflictos.append({
                "campo": campo,
                "fuentes": valores,
                "detectado_en": datetime.now(timezone.utc).isoformat(),
            })
    return conflictos


# ─────────────────────────────────────────────────────────────────────
# Carga de credibilidad por fuente desde source_credibility
# ─────────────────────────────────────────────────────────────────────


async def load_credibility(conn) -> dict[str, float]:
    """Lee source_credibility.base_score → {source: weight}. Fuentes
    desconocidas asumirán 0.5 por defecto en _reconciliar_campo."""
    rows = await conn.fetch(
        "SELECT source, base_score FROM source_credibility WHERE active = TRUE"
    )
    return {r["source"]: float(r["base_score"]) for r in rows}


async def _load_half_lives(conn) -> dict[str, int]:
    """Carga half_life_days de los signals relevantes para overrides."""
    global _HALF_LIVES_CACHE
    if _HALF_LIVES_CACHE:
        return _HALF_LIVES_CACHE
    rows = await conn.fetch(
        "SELECT id, half_life_days FROM signal_types WHERE id = ANY($1::text[])",
        list(SIGNAL_TO_FIELDS.keys()),
    )
    _HALF_LIVES_CACHE = {r["id"]: int(r["half_life_days"]) for r in rows}
    return _HALF_LIVES_CACHE


# ─────────────────────────────────────────────────────────────────────
# Overrides temporales desde spot_semantic_state
# ─────────────────────────────────────────────────────────────────────


async def compute_temporal_overrides(conn, spot_id: int) -> int:
    """Lee spot_semantic_state y crea/refresca overrides en spot_field_overrides.

    Aplica el umbral STRICT (decisión PR11):
      - n_observations ≥ 2
      - weight_support ≥ 1.5
      - confidence ≥ 0.7
      - score == false (consenso "no funciona")

    Devuelve el número de overrides insertados o refrescados.
    """
    row = await conn.fetchrow(
        "SELECT signals_data FROM spot_semantic_state WHERE spot_id = $1 AND stale = FALSE",
        spot_id,
    )
    if not row or not row["signals_data"]:
        return 0
    signals = row["signals_data"]
    if isinstance(signals, str):
        signals = json.loads(signals)

    half_lives = await _load_half_lives(conn)
    count = 0

    for signal_id, fields in SIGNAL_TO_FIELDS.items():
        s = signals.get(signal_id)
        if not isinstance(s, dict):
            continue

        score = s.get("score")
        if score is not False:  # debe ser explícitamente False
            continue

        try:
            n_obs = int(s.get("n_observations", 0))
            weight_sup = float(s.get("weight_support", 0.0))
            conf = float(s.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue

        if n_obs < OVERRIDE_MIN_OBSERVATIONS:
            continue
        if weight_sup < OVERRIDE_MIN_WEIGHT:
            continue
        if conf < OVERRIDE_MIN_CONFIDENCE:
            continue

        ttl_days = half_lives.get(signal_id, 60)

        for field in fields:
            canonical = await conn.fetchval(
                f"SELECT {field} FROM spots WHERE id = $1", spot_id
            )
            await conn.execute(
                """
                INSERT INTO spot_field_overrides
                  (spot_id, field, canonical_value, overridden_value,
                   reason, source_signal_type,
                   confidence, weight_support, n_observations,
                   expires_at, last_seen)
                VALUES ($1, $2, $3, FALSE,
                        $4, $5,
                        $6, $7, $8,
                        NOW() + ($9 || ' days')::interval, NOW())
                ON CONFLICT (spot_id, field, source_signal_type) DO UPDATE SET
                  canonical_value  = EXCLUDED.canonical_value,
                  overridden_value = EXCLUDED.overridden_value,
                  confidence       = EXCLUDED.confidence,
                  weight_support   = EXCLUDED.weight_support,
                  n_observations   = EXCLUDED.n_observations,
                  expires_at       = EXCLUDED.expires_at,
                  last_seen        = NOW()
                """,
                spot_id, field, canonical,
                f"semantic_signal:{signal_id}", signal_id,
                conf, weight_sup, n_obs,
                str(ttl_days),
            )
            count += 1

    return count


# ─────────────────────────────────────────────────────────────────────
# Job principal
# ─────────────────────────────────────────────────────────────────────


async def job_reconciliar(pool) -> dict:
    """Recorre spots multi-fuente, aplica voto ponderado + overrides temporales."""
    inicio = datetime.now()
    stats = {
        "procesados": 0, "actualizados": 0, "conflictos_total": 0,
        "overrides_creados": 0, "empates_tecnicos": 0, "errores": 0,
    }

    async with pool.acquire() as conn:
        credibility = await load_credibility(conn)
        logger.info(f"Reconciliación: credibilidad cargada de {len(credibility)} fuentes")

        # Reset cache de half_lives por job
        global _HALF_LIVES_CACHE
        _HALF_LIVES_CACHE = {}

        spot_ids = await conn.fetch("""
            SELECT id FROM spots
            WHERE activo = TRUE AND array_length(fuentes, 1) > 1
        """)

        logger.info(f"Reconciliación: {len(spot_ids)} spots multi-fuente")

        for row in spot_ids:
            spot_id = row["id"]
            try:
                records_raw = await conn.fetch(
                    "SELECT source, normalized_data FROM source_records WHERE spot_id = $1",
                    spot_id,
                )
                if len(records_raw) < 2:
                    continue

                records = {}
                for r in records_raw:
                    nd = r["normalized_data"]
                    if isinstance(nd, str):
                        nd = json.loads(nd)
                    records[r["source"]] = nd

                updates = {}
                for campo in CREDIBILITY:
                    val, _src = _reconciliar_campo(records, campo, credibility)
                    if val is KEEP_EXISTING:
                        stats["empates_tecnicos"] += 1
                        continue
                    if val is None:
                        continue
                    # La web reconciliada puede ser un dominio de agregador
                    # (park4night.com, etc.) → descartarla en vez de pisar la real.
                    if campo == "web":
                        val = _limpiar_web(val)
                        if val is None:
                            continue
                    updates[campo] = val

                conflictos = _detectar_conflictos(records)

                if updates or conflictos:
                    sets, vals = [], []
                    idx = 1
                    for campo, valor in updates.items():
                        sets.append(f"{campo} = ${idx}")
                        vals.append(valor)
                        idx += 1

                    sets.append(f"conflictos = ${idx}::jsonb")
                    vals.append(json.dumps(conflictos))
                    idx += 1
                    vals.append(spot_id)

                    query = f"""
                        UPDATE spots SET {', '.join(sets)}, updated_at = NOW()
                        WHERE id = ${idx}
                    """
                    await conn.execute(query, *vals)
                    stats["actualizados"] += 1
                    stats["conflictos_total"] += len(conflictos)

                # Overrides temporales (paso PR11)
                stats["overrides_creados"] += await compute_temporal_overrides(conn, spot_id)

                stats["procesados"] += 1
            except Exception as e:
                logger.error(f"Error reconciliando spot {spot_id}: {e}")
                stats["errores"] += 1

    dur = (datetime.now() - inicio).seconds
    logger.info(f"Reconciliación completada en {dur}s: {stats}")
    return stats
