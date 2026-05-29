"""Aggregate normalized observations into current spot semantic state."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Iterable

from .dsl_generator import generate_spot_dsl
from .signal_registry import STATIC_SIGNALS, SignalType

MATERIALIZED_SCORE_COLUMNS = {
    "quietness": "quietness_score",
    "safety": "safety_score",
    "police_risk": "police_risk_score",
    "beauty": "beauty_score",
    "crowd_level": "crowd_level_score",
    "stealth": "stealth_score",
}

# Señales numéricas v2 con columna materializada (además de las score_*)
MATERIALIZED_V2_NUMERIC = {
    "cell_coverage": "cell_coverage",
}
# Señales booleanas v2 con columna materializada
MATERIALIZED_V2_BOOL = {
    "wild_camping_legal": "wild_camping_legal",
}


def _json_object(value) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return dict(value)


# ── Recency boost (T2.3 — Tier 2 hardening) ──────────────────────────────────
# Además del decay por half-life, las observaciones MUY recientes reciben un
# empujón extra para que un cambio de régimen reciente (p.ej. obras nuevas) pese
# más rápido que lo que el half-life solo permitiría. Ventana corta (60d) y
# decae a 1.0 (sin boost) para observaciones antiguas.
#   recency_boost(Δt) = 1 + α · exp(-Δt / window)     # α=0.5, window=60d
RECENCY_BOOST_ALPHA = 0.5
RECENCY_BOOST_WINDOW_DAYS = 60.0


def recency_boost(age_days: float) -> float:
    """Factor multiplicativo >=1 que premia observaciones recientes (T2.3).

    age 0d   -> 1.5   (α=0.5)
    age 60d  -> ~1.18
    age 180d -> ~1.02
    age ≫    -> 1.0
    """
    age = max(0.0, age_days)
    return 1.0 + RECENCY_BOOST_ALPHA * math.exp(-age / RECENCY_BOOST_WINDOW_DAYS)


def _age_days(observed_at: datetime, now: datetime) -> float:
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - observed_at).total_seconds() / 86400.0)


def _max_real_obs_date(rows: Iterable[dict]) -> datetime | None:
    """Máxima `observed_at` (tz-aware) entre observaciones con FECHA REAL.

    Ignora observaciones con `date_estimated=True` (BUG-11): una review sin fecha
    no debe "reabrir" un cierre — su fecha es un placeholder (now), no actividad
    real posterior.
    """
    best: datetime | None = None
    for row in rows:
        if bool(row.get("date_estimated", False)):
            continue
        dt = row.get("observed_at")
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if best is None or dt > best:
            best = dt
    return best


def decayed_weight(weight: float, observed_at: datetime, half_life_days: int, now: datetime | None = None) -> float:
    """Decay por half-life puro: weight · 0.5^(Δt/half_life). Sin recency boost.

    Se mantiene como función separada (algunos call-sites/tests quieren el decay
    puro). El peso final usado por el agregador es `observation_weight_at`, que
    añade el recency boost encima de esto.
    """
    now = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - observed_at).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return weight
    return float(weight) * math.pow(0.5, age_days / half_life_days)


def observation_weight_at(weight: float, observed_at: datetime, half_life_days: int,
                          now: datetime | None = None,
                          date_estimated: bool = False) -> float:
    """Peso final de una observación (T2.3): decay half-life × recency boost.

    Espejo de la fórmula del plan:
        w_final = base_weight · 2^(-Δt/half_life) · recency_boost(Δt)
    (`base_weight` ya viene como source_confidence·extraction_confidence·… desde
    `normalized_observations.observation_weight`).

    Sprint 3 (BUG-10/17/22/31): NO se aplica recency boost cuando la fecha es
    estimada (review sin fecha / hecho scrapeado / futura saneada) ni cuando la
    fecha cae en el futuro respecto a `now`. Sin esta guarda una evidencia sin
    fecha real "gana" a una review datada por parecer fresca.
    """
    now = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    decayed = decayed_weight(weight, observed_at, half_life_days, now)
    is_future = (observed_at - now).total_seconds() > 0
    if date_estimated or is_future:
        return decayed  # boost neutro (×1.0)
    return decayed * recency_boost(_age_days(observed_at, now))


# ── Confianza: helpers centralizados (Sprint 4 — BUG-06 / BUG-25) ────────────
# Antes había DOS fórmulas de confianza divergentes (en aggregate_observations
# full-recompute y en update_semantic_state incremental). El audit las marcó como
# bug porque podían dar números distintos para el mismo estado. Se centralizan
# aquí para que ambos caminos sean idénticos por construcción.

# BUG-06: confianza booleana = acuerdo × volumen.
#   acuerdo  = |true - false| / support  (0 = empate, 1 = unanimidad)
#   volumen  = min(1, support / SAT)     (penaliza pocas observaciones)
# El bug era usar solo el acuerdo: 1 sola review "true" daba confidence=1.0.
BOOL_CONFIDENCE_SATURATION = 3.0


def boolean_confidence(true_support: float, false_support: float) -> float:
    """Confianza de una señal booleana: acuerdo entre observaciones × volumen.

    Una única observación (support≈1) nunca debería dar confianza máxima; el
    factor de volumen lo evita. A partir de ~3 observaciones concordantes el
    volumen satura a 1.0 y la confianza la marca el acuerdo.
    """
    support = true_support + false_support
    if support <= 0:
        return 0.0
    agreement = abs(true_support - false_support) / support
    volume = min(1.0, support / BOOL_CONFIDENCE_SATURATION)
    return agreement * volume


# BUG-25: consenso global = saturación logarítmica del peso total.
# El bug era `min(1, total_weight / (total_observations*2))`: dividir por el
# número de observaciones hacía que MÁS evidencia BAJASE el consenso (no
# monótono). Aquí el consenso crece monótonamente con el peso acumulado.
CONSENSUS_SATURATION_WEIGHT = 12.0


def consensus_confidence(total_weight: float) -> float:
    """Consenso global del spot: monótono creciente con el peso total acumulado.

    log1p(w)/log1p(SAT) → 0 sin evidencia, ~1 cuando el peso total alcanza SAT.
    A diferencia de la fórmula vieja, añadir observaciones nunca reduce el valor.
    """
    if total_weight <= 0:
        return 0.0
    return min(1.0, math.log1p(total_weight) / math.log1p(CONSENSUS_SATURATION_WEIGHT))


def semantic_distance(a: dict | None, b: dict | None, signal_types: dict[str, SignalType] | None = None) -> float:
    if not a or not b:
        return 0.0
    signal_types = signal_types or STATIC_SIGNALS
    keys = set(a) | set(b)
    total = 0.0
    for key in keys:
        av = a.get(key, {}).get("score") if isinstance(a.get(key), dict) else a.get(key)
        bv = b.get(key, {}).get("score") if isinstance(b.get(key), dict) else b.get(key)
        if isinstance(av, bool):
            av = 1.0 if av else 0.0
        if isinstance(bv, bool):
            bv = 1.0 if bv else 0.0
        if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            total += abs(float(av) - float(bv)) * signal_types.get(key, SignalType(key, "numeric", 365, "weighted_mean")).importance_weight
    return total


def aggregate_observations(rows: Iterable[dict], signal_types: dict[str, SignalType] | None = None) -> dict:
    signal_types = signal_types or STATIC_SIGNALS
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(row["signal_type"], []).append(row)

    # BUG-11: un spot marcado "cerrado" debe poder REABRIRSE. Si hay actividad
    # (observaciones de cualquier OTRA señal) fechada DESPUÉS del último reporte
    # de cierre, la gente sigue usando/reseñando el sitio → el cierre está
    # obsoleto y se descarta. Solo se consideran fechas reales (no estimadas).
    closure_rows = buckets.get("spot_closed", [])
    if closure_rows:
        latest_closure = _max_real_obs_date(
            r for r in closure_rows if bool(r.get("value_bool"))
        )
        latest_activity = _max_real_obs_date(
            r for sig, rs in buckets.items() if sig != "spot_closed" for r in rs
        )
        if (latest_closure is not None and latest_activity is not None
                and latest_activity > latest_closure):
            buckets.pop("spot_closed", None)

    signals_data: dict[str, dict] = {}
    total_weight = 0.0
    total_observations = 0
    for signal, obs_rows in buckets.items():
        stype = signal_types.get(signal)
        if not stype:
            continue
        weighted_values = []
        bool_support = {True: 0.0, False: 0.0}
        text_support: dict[str, float] = {}
        support = 0.0
        for row in obs_rows:
            observed_at = row["observed_at"]
            weight = observation_weight_at(
                float(row["observation_weight"]), observed_at, stype.half_life_days,
                date_estimated=bool(row.get("date_estimated", False)),
            )
            if weight <= 0:
                continue
            support += weight
            total_weight += weight
            total_observations += 1
            if stype.value_type == "boolean":
                bool_support[bool(row["value_bool"])] += weight
            elif stype.value_type == "numeric" and row["value_num"] is not None:
                weighted_values.append((float(row["value_num"]), weight))
            elif stype.value_type == "text":
                cat = row.get("value_text")
                if cat:
                    text_support[str(cat)] = text_support.get(str(cat), 0.0) + weight

        if support <= 0:
            continue
        if stype.value_type == "boolean":
            score_value = bool_support[True] >= bool_support[False]
            confidence = boolean_confidence(bool_support[True], bool_support[False])
            signals_data[signal] = {
                "score": score_value,
                "weight_support": round(support, 6),
                "n_observations": len(obs_rows),
                "confidence": round(confidence, 6),
            }
        elif stype.value_type == "text" and text_support:
            # BUG-16: moda ponderada — la categoría con más peso acumulado gana.
            # Surface en signals_data para que entre al DSL (las columnas
            # noise_sources/parking_capacity las materializa v2_materializer aparte).
            dominant = max(text_support.items(), key=lambda kv: kv[1])[0]
            signals_data[signal] = {
                "score": dominant,
                "weight_support": round(support, 6),
                "n_observations": len(obs_rows),
                "confidence": round(text_support[dominant] / support, 6),
            }
        elif weighted_values:
            score = sum(value * weight for value, weight in weighted_values) / support
            signals_data[signal] = {
                "score": round(score, 6),
                "weight_support": round(support, 6),
                "n_observations": len(obs_rows),
                "confidence": round(min(1.0, support / 5.0), 6),
            }

    materialized = {
        column: signals_data.get(signal, {}).get("score")
        for signal, column in MATERIALIZED_SCORE_COLUMNS.items()
    }
    materialized["overnight_safe"] = signals_data.get("overnight_safe", {}).get("score")
    for signal, column in MATERIALIZED_V2_NUMERIC.items():
        materialized[column] = signals_data.get(signal, {}).get("score")
    for signal, column in MATERIALIZED_V2_BOOL.items():
        materialized[column] = signals_data.get(signal, {}).get("score")
    consensus = consensus_confidence(total_weight)
    return {
        "signals_data": signals_data,
        "semantic_dsl": generate_spot_dsl(signals_data),
        "total_observations": total_observations,
        "weight_support": total_weight,
        "consensus_confidence": consensus,
        **materialized,
    }


# ── Detección de cambio de régimen (T2.5 — Tier 2 hardening) ─────────────────
# Detecta contradicciones temporales REALES (p.ej. Grau Roig: obras en 2025 →
# tranquilo en 2026) separando observaciones en dos clusters (reciente vs
# histórico) y comparando sus medias. Guardas para no generar ruido en spots
# con poca actividad (n bajo) ni confundir drift continuo con un salto de régimen.
REGIME_RECENT_WINDOW_DAYS = 180     # ≤180d = "reciente"; >180d = "histórico"
REGIME_MIN_CLUSTER_SIZE = 3         # cada cluster necesita ≥3 observaciones
REGIME_MIN_SEPARATION_DAYS = 90     # gap temporal mínimo entre clusters
REGIME_MIN_DELTA = 0.4              # salto mínimo de media para considerarlo cambio


def _regime_value(row: dict, value_type: str) -> float | None:
    """Extrae el valor numérico de una observación para el test de régimen."""
    if value_type == "boolean":
        v = row.get("value_bool")
        if v is None:
            return None
        return 1.0 if v else 0.0
    v = row.get("value_num")
    return float(v) if v is not None else None


def _weighted_mean(pairs: list[tuple[float, float]]) -> float | None:
    total_w = sum(w for _, w in pairs)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in pairs) / total_w


def detect_regime_change(observations: Iterable[dict], signal_type: str,
                         *, value_type: str = "numeric",
                         now: datetime | None = None) -> dict | None:
    """Detecta un cambio de régimen para UNA señal (T2.5).

    `observations` son las filas de `normalized_observations` de un solo
    `signal_type` (dicts con `observed_at`, `value_num`/`value_bool`,
    `observation_weight`). Particiona en reciente (≤180d) e histórico (>180d) y
    compara medias ponderadas. Devuelve None si no hay cambio significativo o si
    no se cumplen las guardas.

    Guardas (evitan falsos positivos):
      - cada cluster necesita ≥ REGIME_MIN_CLUSTER_SIZE observaciones.
      - separación temporal entre clusters ≥ REGIME_MIN_SEPARATION_DAYS (filtra
        drift continuo: si las observaciones cruzan el límite de 180d sin hueco,
        no es un salto de régimen).
      - |media_reciente − media_histórica| > REGIME_MIN_DELTA.

    Pesos: usa `observation_weight` SIN decay/recency — comparamos el valor
    intrínseco de cada periodo, no el peso decaído a hoy (decaer el histórico a
    cero distorsionaría su media).

    NOTA: la separación correcta es `min(fechas_recientes) − max(fechas_históricas)`
    (el histórico es MÁS antiguo). El pseudocódigo del plan tenía los operandos
    invertidos (`min(historical) − max(recent)`), siempre negativo → guard siempre
    activa. Corregido aquí (patrón de actuación).
    """
    now = now or datetime.now(timezone.utc)
    recent: list[dict] = []
    historical: list[dict] = []
    for row in observations:
        observed_at = row.get("observed_at")
        if observed_at is None:
            continue
        if _regime_value(row, value_type) is None:
            continue
        if _age_days(observed_at, now) <= REGIME_RECENT_WINDOW_DAYS:
            recent.append(row)
        else:
            historical.append(row)

    if len(recent) < REGIME_MIN_CLUSTER_SIZE or len(historical) < REGIME_MIN_CLUSTER_SIZE:
        return None

    def _norm_dt(dt: datetime) -> datetime:
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    recent_dates = [_norm_dt(r["observed_at"]) for r in recent]
    hist_dates = [_norm_dt(h["observed_at"]) for h in historical]
    separation_days = (min(recent_dates) - max(hist_dates)).total_seconds() / 86400.0
    if separation_days < REGIME_MIN_SEPARATION_DAYS:
        return None

    recent_mean = _weighted_mean(
        [(_regime_value(r, value_type), float(r["observation_weight"])) for r in recent]
    )
    hist_mean = _weighted_mean(
        [(_regime_value(h, value_type), float(h["observation_weight"])) for h in historical]
    )
    if recent_mean is None or hist_mean is None:
        return None
    if abs(recent_mean - hist_mean) <= REGIME_MIN_DELTA:
        return None

    return {
        "changed": True,
        "old": round(hist_mean, 4),
        "new": round(recent_mean, 4),
        "delta": round(recent_mean - hist_mean, 4),
        "since": min(recent_dates).date().isoformat(),
        "n_recent": len(recent),
        "n_historical": len(historical),
    }


def compute_signal_flux(rows: Iterable[dict],
                        signal_types: dict[str, SignalType] | None = None,
                        now: datetime | None = None) -> dict[str, dict]:
    """Aplica `detect_regime_change` a todas las señales numéricas/booleanas de
    un spot y devuelve el dict listo para `spot_semantic_state.signal_flux`.

    Las señales TEXT (recent_wins: noise_source, parking_capacity) se saltan — el
    test |Δmedia|>0.4 no aplica a categóricas libres.
    """
    signal_types = signal_types or STATIC_SIGNALS
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(row["signal_type"], []).append(row)

    flux: dict[str, dict] = {}
    for signal, obs_rows in buckets.items():
        stype = signal_types.get(signal)
        if not stype or stype.value_type == "text":
            continue
        change = detect_regime_change(obs_rows, signal, value_type=stype.value_type, now=now)
        if change:
            flux[signal] = change
    return flux


def needs_recompute(signal_half_lives: Iterable[int], days_since_last_aggregate: float) -> bool:
    """Gate de reprocesamiento condicionado (T2.3).

    El cron de decay-refresh solo debería re-agregar un spot si el decay/recency
    han cambiado de forma material desde el último agregado. Eso ocurre cuando
    AL MENOS UNA señal presente tiene un half-life menor que el tiempo
    transcurrido — las señales muy persistentes (beauty HL=36500d) no cambian
    en una semana, así que reprocesarlas es trabajo desperdiciado.

    Devuelve True si `min(half_lives) < days_since_last_aggregate`.
    Sin señales o spot recién agregado (elapsed≈0) → False (lo maneja el path
    incremental/`stale`, no este cron de refresco por tiempo).
    """
    hls = [h for h in signal_half_lives if h and h > 0]
    if not hls or days_since_last_aggregate <= 0:
        return False
    return min(hls) < days_since_last_aggregate


async def recompute_spot_state(conn, spot_id: int, snapshot_threshold: float = 0.15) -> dict:
    rows = await conn.fetch(
        """
        SELECT signal_type, value_num, value_bool, value_text, observation_weight, observed_at, date_estimated
        FROM normalized_observations
        WHERE spot_id = $1
        """,
        spot_id,
    )
    state = aggregate_observations([dict(r) for r in rows])
    
    # Calculate Phase 3 v2 materialized columns
    from .v2_materializer import (
        aggregate_noise_sources,
        aggregate_parking_capacity,
        compute_last_observation_at,
    )
    obs_dicts = [dict(r) for r in rows]
    noise_sources = aggregate_noise_sources(obs_dicts) or None
    parking_capacity = aggregate_parking_capacity(obs_dicts)
    last_obs_at = await compute_last_observation_at(conn, spot_id)

    # T2.5 — cambio de régimen (reciente vs histórico) sobre el set completo de
    # observaciones. Solo se computa en el recompute full (aquí están TODAS las
    # observaciones); el path incremental no lo toca y la columna se preserva.
    signal_flux = compute_signal_flux(obs_dicts)

    current = await conn.fetchrow("SELECT signals_data FROM spot_semantic_state WHERE spot_id = $1", spot_id)
    previous = _json_object(current["signals_data"]) if current else None
    distance = semantic_distance(previous, state["signals_data"])
    if previous and distance > snapshot_threshold:
        await conn.execute(
            """
            INSERT INTO spot_semantic_snapshots (spot_id, snapshot_date, semantic_data, trigger_reason, semantic_distance)
            VALUES ($1, CURRENT_DATE, $2::jsonb, 'semantic_shift', $3)
            ON CONFLICT (spot_id, snapshot_date) DO UPDATE SET
                semantic_data = EXCLUDED.semantic_data,
                semantic_distance = GREATEST(spot_semantic_snapshots.semantic_distance, EXCLUDED.semantic_distance)
            """,
            spot_id,
            json.dumps(previous),
            distance,
        )
    await conn.execute(
        """
        INSERT INTO spot_semantic_state (
            spot_id, quietness_score, safety_score, police_risk_score, beauty_score,
            crowd_level_score, overnight_safe, stealth_score, signals_data, semantic_dsl,
            total_observations, consensus_confidence, weight_support, last_snapshot_data,
            cell_coverage, wild_camping_legal, noise_sources, parking_capacity, last_observation_at,
            signal_flux, stale, updated_at, last_aggregated_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $9::jsonb,
            $14, $15, $16, $17, $18, $19::jsonb,
            FALSE, NOW(), NOW()
        )
        ON CONFLICT (spot_id) DO UPDATE SET
            quietness_score = EXCLUDED.quietness_score,
            safety_score = EXCLUDED.safety_score,
            police_risk_score = EXCLUDED.police_risk_score,
            beauty_score = EXCLUDED.beauty_score,
            crowd_level_score = EXCLUDED.crowd_level_score,
            overnight_safe = EXCLUDED.overnight_safe,
            stealth_score = EXCLUDED.stealth_score,
            signals_data = EXCLUDED.signals_data,
            semantic_dsl = EXCLUDED.semantic_dsl,
            total_observations = EXCLUDED.total_observations,
            consensus_confidence = EXCLUDED.consensus_confidence,
            weight_support = EXCLUDED.weight_support,
            last_snapshot_data = EXCLUDED.last_snapshot_data,
            cell_coverage = EXCLUDED.cell_coverage,
            wild_camping_legal = EXCLUDED.wild_camping_legal,
            noise_sources = EXCLUDED.noise_sources,
            parking_capacity = EXCLUDED.parking_capacity,
            last_observation_at = EXCLUDED.last_observation_at,
            signal_flux = EXCLUDED.signal_flux,
            stale = FALSE,
            updated_at = NOW(),
            last_aggregated_at = NOW()
        """,
        spot_id,
        state.get("quietness_score"),
        state.get("safety_score"),
        state.get("police_risk_score"),
        state.get("beauty_score"),
        state.get("crowd_level_score"),
        state.get("overnight_safe"),
        state.get("stealth_score"),
        json.dumps(state["signals_data"]),
        state["semantic_dsl"],
        state["total_observations"],
        state["consensus_confidence"],
        state["weight_support"],
        state.get("cell_coverage"),
        state.get("wild_camping_legal"),
        noise_sources,
        parking_capacity,
        last_obs_at,
        json.dumps(signal_flux),
    )
    state["signal_flux"] = signal_flux
    return state



async def update_semantic_state(conn, spot_id: int, observation: object | None = None) -> dict:
    if observation is None:
        return await recompute_spot_state(conn, spot_id)

    signal = observation.signal_type
    stype = STATIC_SIGNALS.get(signal)
    if not stype:
        return await recompute_spot_state(conn, spot_id)

    # Trigger full recompute for complex v2 signal types
    if signal in ("noise_source", "parking_capacity"):
        return await recompute_spot_state(conn, spot_id)

    row = await conn.fetchrow(
        """
        SELECT signals_data, total_observations, weight_support, last_snapshot_data,
               noise_sources, parking_capacity, last_observation_at
        FROM spot_semantic_state
        WHERE spot_id = $1
        """,
        spot_id,
    )
    previous_signals = _json_object(row["signals_data"]) if row else {}
    signals_data = dict(previous_signals)
    old_entry = dict(signals_data.get(signal, {}))
    old_support = float(old_entry.get("weight_support", 0.0) or 0.0)
    obs_weight = observation_weight_at(
        observation.observation_weight,
        observation.observed_at,
        stype.half_life_days,
        date_estimated=bool(getattr(observation, "date_estimated", False)),
    )

    if stype.value_type == "boolean":
        old_true = old_support if old_entry.get("score") is True else 0.0
        old_false = old_support if old_entry.get("score") is False else 0.0
        if observation.value_bool is True:
            old_true += obs_weight
        else:
            old_false += obs_weight
        support = old_true + old_false
        score = old_true >= old_false
        confidence = boolean_confidence(old_true, old_false)
    else:
        old_score = float(old_entry.get("score", 0.0) or 0.0)
        support = old_support + obs_weight
        value = observation.value_num if observation.value_num is not None else old_score
        score = ((old_score * old_support) + (float(value) * obs_weight)) / support if support else value
        confidence = min(1.0, support / 5.0)

    signals_data[signal] = {
        "score": score,
        "weight_support": round(support, 6),
        "n_observations": int(old_entry.get("n_observations", 0) or 0) + 1,
        "confidence": round(confidence, 6),
    }

    materialized = {
        column: signals_data.get(sig, {}).get("score")
        for sig, column in MATERIALIZED_SCORE_COLUMNS.items()
    }
    materialized["overnight_safe"] = signals_data.get("overnight_safe", {}).get("score")
    
    # Retrieve v2 columns from existing row or state
    noise_sources = row["noise_sources"] if row else None
    parking_capacity = row["parking_capacity"] if row else None
    last_obs_at = row["last_observation_at"] if row else None
    obs_date = observation.observed_at
    if last_obs_at is None or (obs_date and obs_date > last_obs_at):
        last_obs_at = obs_date

    cell_coverage = signals_data.get("cell_coverage", {}).get("score")
    wild_camping_legal = signals_data.get("wild_camping_legal", {}).get("score")

    semantic_dsl = generate_spot_dsl(signals_data)
    total_observations = (row["total_observations"] if row else 0) + 1
    weight_support = float(row["weight_support"] if row else 0.0) + obs_weight
    consensus = consensus_confidence(weight_support)
    snapshot_baseline = _json_object(row["last_snapshot_data"]) if row else {}
    if not snapshot_baseline:
        snapshot_baseline = previous_signals
    distance = semantic_distance(snapshot_baseline if row else None, signals_data)
    next_snapshot_data = snapshot_baseline or signals_data
    if row and distance > 0.15:
        await conn.execute(
            """
            INSERT INTO spot_semantic_snapshots (spot_id, snapshot_date, semantic_data, trigger_reason, semantic_distance)
            VALUES ($1, CURRENT_DATE, $2::jsonb, 'semantic_shift', $3)
            ON CONFLICT (spot_id, snapshot_date) DO UPDATE SET
                semantic_data = EXCLUDED.semantic_data,
                semantic_distance = GREATEST(spot_semantic_snapshots.semantic_distance, EXCLUDED.semantic_distance)
            """,
            spot_id,
            json.dumps(previous_signals),
            distance,
        )
        next_snapshot_data = signals_data

    state = {
        "signals_data": signals_data,
        "semantic_dsl": semantic_dsl,
        "total_observations": total_observations,
        "weight_support": weight_support,
        "consensus_confidence": consensus,
        **materialized,
    }
    await conn.execute(
        """
        INSERT INTO spot_semantic_state (
            spot_id, quietness_score, safety_score, police_risk_score, beauty_score,
            crowd_level_score, overnight_safe, stealth_score, signals_data, semantic_dsl,
            total_observations, consensus_confidence, weight_support, last_snapshot_data,
            cell_coverage, wild_camping_legal, noise_sources, parking_capacity, last_observation_at,
            stale, updated_at, last_aggregated_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $14::jsonb,
            $15, $16, $17, $18, $19,
            FALSE, NOW(), NOW()
        )
        ON CONFLICT (spot_id) DO UPDATE SET
            quietness_score = EXCLUDED.quietness_score,
            safety_score = EXCLUDED.safety_score,
            police_risk_score = EXCLUDED.police_risk_score,
            beauty_score = EXCLUDED.beauty_score,
            crowd_level_score = EXCLUDED.crowd_level_score,
            overnight_safe = EXCLUDED.overnight_safe,
            stealth_score = EXCLUDED.stealth_score,
            signals_data = EXCLUDED.signals_data,
            semantic_dsl = EXCLUDED.semantic_dsl,
            total_observations = EXCLUDED.total_observations,
            consensus_confidence = EXCLUDED.consensus_confidence,
            weight_support = EXCLUDED.weight_support,
            last_snapshot_data = EXCLUDED.last_snapshot_data,
            cell_coverage = EXCLUDED.cell_coverage,
            wild_camping_legal = EXCLUDED.wild_camping_legal,
            noise_sources = EXCLUDED.noise_sources,
            parking_capacity = EXCLUDED.parking_capacity,
            last_observation_at = EXCLUDED.last_observation_at,
            stale = FALSE,
            updated_at = NOW(),
            last_aggregated_at = NOW()
        """,
        spot_id,
        state.get("quietness_score"),
        state.get("safety_score"),
        state.get("police_risk_score"),
        state.get("beauty_score"),
        state.get("crowd_level_score"),
        state.get("overnight_safe"),
        state.get("stealth_score"),
        json.dumps(signals_data),
        semantic_dsl,
        total_observations,
        consensus,
        weight_support,
        json.dumps(next_snapshot_data),
        cell_coverage,
        wild_camping_legal,
        noise_sources,
        parking_capacity,
        last_obs_at,
    )
    return state

