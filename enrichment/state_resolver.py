"""Resolver determinista de `spot_alerts` (T1.4 — Sprint 2 hardening).

Responsabilidades:
  1. `parse_valid_from(text)` — convertir "YYYY-MM" o "YYYY-MM-DD" del LLM a `date`.
  2. `upsert_alert(conn, spot_id, alert, detected_by)` — insertar/actualizar una
     alerta. Si ya existe una alerta activa del mismo `alert_type` para el spot,
     **mergea** las fuentes (review_ids unión) y actualiza confidence/severity en
     vez de duplicar fila.
  3. `apply_decay(conn, alert_row, current_date)` — decay 0.85^meses + marca
     resolved si confidence<0.3 y >=180 días desde valid_from.
  4. `decay_all_active(conn)` — barrido del cron diario sobre todas las alertas
     no-resueltas.

Reglas (cerradas en docs/fase-3-hardening-pre-batch.md, D1+D2):
  - Decay multiplicativo 0.85^n donde n=meses desde `last_decay_at`.
  - Marcar `resolved=TRUE` cuando `confidence < 0.3` Y `(current - valid_from).days >= 180`.
  - `permanently_closed` y `permanent_*` NO decaen. Solo manual.
  - Al marcar resolved, fijar `valid_until = current_date`.

NO llama al LLM. NO escribe en `spot_semantic_state` (eso es responsabilidad de
`refresh_active_alert_types` invocado tras cada upsert/decay).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

from loguru import logger

# ── Constantes de decay (D1 + D2 en el plan) ──────────────────────────
DECAY_FACTOR_MONTHLY = 0.85           # 0.85^n por mes desde last_decay_at
RESOLVE_CONFIDENCE_THRESHOLD = 0.30   # marcar resolved si confidence cae aquí
RESOLVE_MIN_DAYS_FROM_START = 180     # …Y han pasado ≥180d desde valid_from
PERMANENT_PREFIXES = ("permanent_", "permanently_")


# ─────────────────────────────────────────────────────────────────────
# 1. Parser de valid_from
# ─────────────────────────────────────────────────────────────────────

def parse_valid_from(text: str | None) -> date | None:
    """Acepta 'YYYY-MM' o 'YYYY-MM-DD'. Devuelve `date` o None si no parseable.

    Para 'YYYY-MM' usamos día 01.
    """
    if not text:
        return None
    s = text.strip()
    # Probar YYYY-MM-DD
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        pass
    # Probar YYYY-MM
    try:
        return datetime.strptime(s, "%Y-%m").date()
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────
# 2. Upsert idempotente de una alerta
# ─────────────────────────────────────────────────────────────────────

@dataclass
class AlertPayload:
    """DTO simple para `upsert_alert` — desacopla del parser del LLM.

    Usar `AlertPayload.from_validated(va)` cuando vengas del parser v6.
    """
    alert_type: str
    severity: float
    valid_from: date
    confidence: float
    source_review_ids: list[int]
    summary: str | None
    detected_at: datetime

    @classmethod
    def from_validated(cls, va, *, detected_at: datetime | None = None) -> "AlertPayload | None":
        """Construye payload desde `ValidatedAlert` del parser. Devuelve None si
        `valid_from_inferred` no es parseable (el parser ya validó vocab/rangos)."""
        vf = parse_valid_from(va.valid_from)
        if vf is None:
            return None
        return cls(
            alert_type=va.alert_type,
            severity=va.severity,
            valid_from=vf,
            confidence=va.confidence,
            source_review_ids=list(va.source_review_ids),
            summary=va.summary,
            detected_at=detected_at or datetime.now(timezone.utc),
        )


async def upsert_alert(
    conn,
    spot_id: int,
    payload: AlertPayload,
    *,
    detected_by: str,
) -> int:
    """Inserta o actualiza una alerta activa del mismo tipo para el spot.

    Idempotencia: si ya existe una fila con `(spot_id, alert_type, resolved=FALSE)`
    se **mergea** — unión de `source_review_ids`, max(confidence,severity),
    `detected_at` se actualiza al más reciente. No se duplica fila.

    Si todas las review_ids del payload ya estaban en la fila activa y nada
    cambia, sigue siendo un UPDATE no-op (idempotente en re-runs del LLM).

    Devuelve el `id` de la fila resultante (creada o actualizada).
    """
    existing = await conn.fetchrow(
        """
        SELECT id, severity, confidence, source_review_ids, source_observations,
               detected_at, valid_from, summary
        FROM spot_alerts
        WHERE spot_id = $1 AND alert_type = $2 AND resolved = FALSE
        ORDER BY created_at DESC
        LIMIT 1
        """,
        spot_id, payload.alert_type,
    )

    if existing is None:
        row = await conn.fetchrow(
            """
            INSERT INTO spot_alerts (
                spot_id, alert_type, severity, detected_at, valid_from, valid_until,
                confidence, source_observations, source_review_ids, detected_by,
                summary, resolved, last_decay_at
            ) VALUES ($1, $2, $3, $4, $5, NULL, $6, '{}', $7, $8, $9, FALSE, NULL)
            RETURNING id
            """,
            spot_id, payload.alert_type, payload.severity, payload.detected_at,
            payload.valid_from, payload.confidence,
            payload.source_review_ids, detected_by, payload.summary,
        )
        return int(row["id"])

    # Merge: unión review_ids, max conf/sev, valid_from = earliest
    merged_review_ids = sorted(set(list(existing["source_review_ids"] or [])) | set(payload.source_review_ids))
    new_confidence = max(float(existing["confidence"]), payload.confidence)
    new_severity = max(float(existing["severity"]), payload.severity)
    new_valid_from = min(existing["valid_from"], payload.valid_from)
    # detected_at se actualiza si el nuevo es más reciente — el más reciente es
    # el que ancla la línea de decay
    new_detected_at = max(existing["detected_at"], payload.detected_at)
    # summary: si la fila existente no tiene, usar la nueva
    new_summary = existing["summary"] or payload.summary

    await conn.execute(
        """
        UPDATE spot_alerts
        SET severity = $1,
            confidence = $2,
            source_review_ids = $3,
            valid_from = $4,
            detected_at = $5,
            summary = $6,
            last_decay_at = NULL    -- forzar próximo recálculo de decay
        WHERE id = $7
        """,
        new_severity, new_confidence, merged_review_ids,
        new_valid_from, new_detected_at, new_summary, existing["id"],
    )
    return int(existing["id"])


async def refresh_active_alert_types(conn, spot_id: int) -> None:
    """Recalcula `spot_semantic_state.active_alert_types` desde `spot_alerts`.

    Wrap a la función SQL homónima creada en `migration_phase3_v6.sql`.
    Llamarlo tras cualquier INSERT/UPDATE/RESOLVE de alerts.
    """
    await conn.execute("SELECT refresh_active_alert_types($1::BIGINT)", spot_id)


# ─────────────────────────────────────────────────────────────────────
# 3. Decay determinista (D1 + D2)
# ─────────────────────────────────────────────────────────────────────

def _months_between(start: datetime, end: datetime) -> float:
    """Meses calendario aproximados (30.44 días/mes) entre dos timestamps."""
    if start is None or end is None:
        return 0.0
    delta_days = (end - start).total_seconds() / 86400.0
    return max(0.0, delta_days / 30.44)


def compute_decayed_confidence(
    current_confidence: float,
    last_decay_at: datetime | None,
    detected_at: datetime,
    current_ts: datetime,
) -> tuple[float, float]:
    """Aplica decay 0.85^n donde n = meses desde `last_decay_at` (o `detected_at`
    si nunca se ha aplicado).

    Returns (new_confidence, months_elapsed).
    """
    anchor = last_decay_at or detected_at
    months = _months_between(anchor, current_ts)
    if months <= 0:
        return current_confidence, 0.0
    new_conf = current_confidence * (DECAY_FACTOR_MONTHLY ** months)
    return new_conf, months


def is_permanent(alert_type: str) -> bool:
    """`permanently_closed` y cualquier `permanent_*` futuro no decaen."""
    return any(alert_type.startswith(p) for p in PERMANENT_PREFIXES)


@dataclass
class DecayDecision:
    alert_id: int
    spot_id: int
    alert_type: str
    old_confidence: float
    new_confidence: float
    months_elapsed: float
    resolved: bool  # True si se acaba de marcar resolved
    skipped_permanent: bool = False


async def apply_decay(conn, alert_row: dict, current_ts: datetime | None = None) -> DecayDecision:
    """Aplica decay a UNA alerta. Persiste cambios en DB.

    No invoca `refresh_active_alert_types` — eso lo hace el caller en batch
    para no reescribir N veces el mismo spot.
    """
    if current_ts is None:
        current_ts = datetime.now(timezone.utc)

    alert_id = int(alert_row["id"])
    spot_id = int(alert_row["spot_id"])
    alert_type = alert_row["alert_type"]
    old_conf = float(alert_row["confidence"])

    if is_permanent(alert_type):
        # Permanente: solo refrescar last_decay_at para no recorrer en el siguiente cron
        await conn.execute(
            "UPDATE spot_alerts SET last_decay_at = $1 WHERE id = $2",
            current_ts, alert_id,
        )
        return DecayDecision(
            alert_id=alert_id, spot_id=spot_id, alert_type=alert_type,
            old_confidence=old_conf, new_confidence=old_conf,
            months_elapsed=0.0, resolved=False, skipped_permanent=True,
        )

    new_conf, months = compute_decayed_confidence(
        old_conf,
        alert_row["last_decay_at"],
        alert_row["detected_at"],
        current_ts,
    )

    # Round defensive (NUMERIC(3,2) en DB)
    new_conf = round(max(0.0, min(1.0, new_conf)), 2)

    valid_from: date = alert_row["valid_from"]
    days_since_start = (current_ts.date() - valid_from).days
    should_resolve = (
        new_conf < RESOLVE_CONFIDENCE_THRESHOLD
        and days_since_start >= RESOLVE_MIN_DAYS_FROM_START
    )

    if should_resolve:
        await conn.execute(
            """
            UPDATE spot_alerts
            SET confidence = $1,
                last_decay_at = $2,
                resolved = TRUE,
                valid_until = $3
            WHERE id = $4
            """,
            new_conf, current_ts, current_ts.date(), alert_id,
        )
    else:
        await conn.execute(
            """
            UPDATE spot_alerts
            SET confidence = $1, last_decay_at = $2
            WHERE id = $3
            """,
            new_conf, current_ts, alert_id,
        )

    return DecayDecision(
        alert_id=alert_id, spot_id=spot_id, alert_type=alert_type,
        old_confidence=old_conf, new_confidence=new_conf,
        months_elapsed=months, resolved=should_resolve,
    )


async def decay_all_active(conn, *, current_ts: datetime | None = None,
                           batch_size: int = 2000) -> dict:
    """Cron diario: aplica decay a todas las alertas no-resueltas.

    Devuelve stats agregados. Itera en lotes para no cargar millones de filas
    en memoria. Tras procesar cada batch, refresca `active_alert_types` de los
    spots cuyos alerts se hayan resuelto.
    """
    if current_ts is None:
        current_ts = datetime.now(timezone.utc)
    stats = {
        "scanned": 0, "decayed": 0, "resolved": 0,
        "skipped_permanent": 0, "errors": 0,
    }
    spots_to_refresh: set[int] = set()
    last_id = 0
    while True:
        rows = await conn.fetch(
            """
            SELECT id, spot_id, alert_type, severity, confidence,
                   detected_at, valid_from, valid_until, last_decay_at
            FROM spot_alerts
            WHERE resolved = FALSE AND id > $1
            ORDER BY id
            LIMIT $2
            """,
            last_id, batch_size,
        )
        if not rows:
            break
        for r in rows:
            try:
                dec = await apply_decay(conn, dict(r), current_ts=current_ts)
                stats["scanned"] += 1
                if dec.skipped_permanent:
                    stats["skipped_permanent"] += 1
                else:
                    stats["decayed"] += 1
                if dec.resolved:
                    stats["resolved"] += 1
                    spots_to_refresh.add(dec.spot_id)
            except Exception as e:
                stats["errors"] += 1
                logger.exception(f"[state_resolver] decay error en alert id={r['id']}: {e}")
        last_id = int(rows[-1]["id"])

    # Refrescar active_alert_types de los spots cuyas alertas se resolvieron
    for spot_id in spots_to_refresh:
        try:
            await refresh_active_alert_types(conn, spot_id)
        except Exception as e:
            stats["errors"] += 1
            logger.exception(f"[state_resolver] refresh_active_alert_types error en spot={spot_id}: {e}")

    logger.info(
        f"[state_resolver] decay batch done: scanned={stats['scanned']} "
        f"decayed={stats['decayed']} resolved={stats['resolved']} "
        f"perm_skipped={stats['skipped_permanent']} errors={stats['errors']}"
    )
    return stats
