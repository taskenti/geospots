"""Regression suite v1 para Phase 3 hardening pre-batch (T0.2).

Propósito
─────────
Detectar regresiones cualitativas en el output del pipeline LLM spot-level
(`orchestrator_v2`) ANTES y DESPUÉS de cada cambio del Sprint 1-3 del plan
`docs/fase-3-hardening-pre-batch.md`.

Diseño
──────
- Cada `Case` apunta a un `spot_id` real de la DB y declara aserciones
  agrupadas en 3 tiers:
    * `hard`   — invariantes duros. Si fallan, exit 1 (break build).
    * `bands`  — bandas estadísticas. Sólo warning.
    * `soft`   — notas para revisión humana mensual. No se chequean.
- Las aserciones leen de `spot_semantic_state` (+ `spot_alerts`/`spot_geo`
  cuando T1.4/T1.4b estén implementados). Si una columna/tabla no existe
  todavía, la aserción se marca SKIP (no falla).
- Modos CLI:
    list     → muestra todos los casos definidos
    check    → ejecuta aserciones contra el estado actual de la DB
    snapshot → guarda el output actual del spot como baseline JSON
- No llama al LLM. Para probar un cambio: re-enriquece manualmente los
  spots afectados con `orchestrator_v2 --force-spot-ids <ids>` y vuelve
  a correr `check`.

Cómo poblar TODOs
─────────────────
Varios casos requieren `spot_id` que aún no tengo identificado. Para cada
TODO, ejecutar la query de localización sugerida en el docstring del caso
y rellenar el campo `spot_id`. Una vez fijado, commit del cambio para
estabilizar la suite.

Uso
───
    python -m tests.regression.semantic_suite list
    python -m tests.regression.semantic_suite check
    python -m tests.regression.semantic_suite snapshot --case grau_roig_obras
    python -m tests.regression.semantic_suite check --category obras_temporales
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import asyncpg

# Permite ejecutar como módulo desde la raíz del proyecto
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from enrichment.worker import _dsn  # noqa: E402

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
SNAPSHOT_DIR.mkdir(exist_ok=True)


# ─────────────────────────────────────────────────────────────────────
# Tipos
# ─────────────────────────────────────────────────────────────────────

CheckResult = bool | str | None
# True       → pasa
# False      → falla sin mensaje (genérico)
# str        → falla con mensaje
# None       → SKIP (precondición no cumplida, ej. columna no existe aún)


@dataclass
class Assertion:
    name: str
    tier: str  # 'hard' | 'band' | 'soft'
    check: Callable[[dict], CheckResult]
    description: str = ""


@dataclass
class Case:
    case_id: str
    category: str
    description: str
    spot_id: int | None  # None = TODO; rellenar
    requires_tasks: tuple[str, ...] = ()
    hard: list[Assertion] = field(default_factory=list)
    bands: list[Assertion] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    locator_hint: str = ""  # Query SQL sugerida para encontrar el spot_id


# ─────────────────────────────────────────────────────────────────────
# Helpers de aserción
# ─────────────────────────────────────────────────────────────────────


def _parse_jsonb(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def signal_score_in(signal: str, lo: float, hi: float) -> Callable[[dict], CheckResult]:
    def check(state: dict) -> CheckResult:
        sd = _parse_jsonb(state.get("signals_data")) or {}
        entry = sd.get(signal)
        if entry is None:
            return f"signal '{signal}' ausente en signals_data"
        score = entry.get("score") if isinstance(entry, dict) else entry
        if score is None:
            return f"signal '{signal}' sin score"
        if isinstance(score, bool):
            return f"signal '{signal}' es boolean, no se puede comparar con rango"
        try:
            s = float(score)
        except (TypeError, ValueError):
            return f"signal '{signal}' score no numérico: {score!r}"
        if lo <= s <= hi:
            return True
        return f"score={s:.3f} fuera de [{lo}, {hi}]"
    return check


def signal_present(signal: str) -> Callable[[dict], CheckResult]:
    def check(state: dict) -> CheckResult:
        sd = _parse_jsonb(state.get("signals_data")) or {}
        if signal in sd:
            return True
        return f"signal '{signal}' ausente"
    return check


def summary_not_empty(state: dict) -> CheckResult:
    s = state.get("summary_en") or state.get("summary_es")
    if s and len(s.strip()) > 10:
        return True
    return f"summary vacío o demasiado corto: {s!r}"


def tags_count_in(lo: int, hi: int) -> Callable[[dict], CheckResult]:
    def check(state: dict) -> CheckResult:
        tags = state.get("tags") or []
        if isinstance(tags, str):
            tags = _parse_jsonb(tags) or []
        n = len(tags)
        if lo <= n <= hi:
            return True
        return f"tags count {n} fuera de [{lo}, {hi}]: {tags}"
    return check


def tag_not_contains(needle: str) -> Callable[[dict], CheckResult]:
    def check(state: dict) -> CheckResult:
        tags = state.get("tags") or []
        if isinstance(tags, str):
            tags = _parse_jsonb(tags) or []
        for t in tags:
            if needle.lower() in str(t).lower():
                return f"tag prohibido '{needle}' encontrado en {tags}"
        return True
    return check


def tag_contains(needle: str) -> Callable[[dict], CheckResult]:
    def check(state: dict) -> CheckResult:
        tags = state.get("tags") or []
        if isinstance(tags, str):
            tags = _parse_jsonb(tags) or []
        for t in tags:
            if needle.lower() in str(t).lower():
                return True
        return f"tag '{needle}' ausente en {tags}"
    return check


def summary_word_count_in(lo: int, hi: int) -> Callable[[dict], CheckResult]:
    def check(state: dict) -> CheckResult:
        s = state.get("summary_en") or state.get("summary_es") or ""
        n = len(s.split())
        if lo <= n <= hi:
            return True
        return f"summary tiene {n} palabras, fuera de [{lo}, {hi}]"
    return check


def chronology_not_inverted(state: dict) -> CheckResult:
    """Heurística simple: si el summary menciona 'construction'/'works' Y
    al mismo tiempo cita explícitamente un año pasado como 'recent', es sospechoso.
    No es perfecto — refuerza con revisión humana (soft tier).
    """
    s = (state.get("summary_en") or state.get("summary_es") or "").lower()
    if not s:
        return None
    construction_terms = ("construction", "works", "building", "obras", "bouwput", "baustelle", "chantier")
    has_construction = any(t in s for t in construction_terms)
    if not has_construction:
        return True  # No menciona obras, no aplica
    # Sospecha de inversión: año viejo descrito como reciente.
    # Se excluye el caso donde 2026 también aparece (LLM reconoce un año más reciente)
    # — eso indica cronología correcta (2025 = pasado, 2026 = reciente).
    suspicious = (
        ("recent" in s and "2024" in s and "2025" not in s and "2026" not in s)
        or ("recent" in s and "2025" in s and "2026" not in s and "now" not in s)
    )
    if suspicious:
        return f"summary podría invertir cronología: {s[:200]}"
    return True


def has_construction_alert(state: dict) -> CheckResult:
    """Requiere T1.4. Mira `spot_alerts` (inyectada en state por el runner)."""
    alerts = state.get("_alerts_rows")
    if alerts is None:
        return None  # spot_alerts no existe todavía → SKIP
    for a in alerts:
        if a.get("alert_type") == "construction" and not a.get("resolved", False):
            return True
    return "ninguna alerta activa con alert_type='construction'"


def active_alert_types_contains(needle: str) -> Callable[[dict], CheckResult]:
    """Requiere T1.4c."""
    def check(state: dict) -> CheckResult:
        col = state.get("active_alert_types")
        if col is None and "active_alert_types" not in state:
            return None  # columna no existe → SKIP
        col = col or []
        if needle in col:
            return True
        return f"'{needle}' no está en active_alert_types={col}"
    return check


def spot_function_is(expected: str) -> Callable[[dict], CheckResult]:
    """Requiere T1.4b."""
    def check(state: dict) -> CheckResult:
        if "spot_function" not in state:
            return None
        actual = state.get("spot_function")
        if actual == expected:
            return True
        return f"spot_function={actual!r}, esperado {expected!r}"
    return check


def spot_geo_elevation_around(meters: int, tol: int = 50) -> Callable[[dict], CheckResult]:
    """Requiere T1.4b (LLM emite elevation_m)."""
    def check(state: dict) -> CheckResult:
        geo = state.get("_spot_geo_row")
        if geo is None:
            return None
        elev = geo.get("elevation_m")
        if elev is None:
            return "spot_geo.elevation_m es NULL"
        if abs(elev - meters) <= tol:
            return True
        return f"elevation_m={elev}, esperado {meters}±{tol}"
    return check


def claims_review_id_not_null_except_scraped(state: dict) -> CheckResult:
    """Hard invariant T1.2: no debería haber claims con review_id IS NULL fuera de scraped_facts."""
    bad = state.get("_claims_review_id_null_non_scraped")
    if bad is None:
        return None
    if bad == 0:
        return True
    return f"{bad} claims con review_id NULL y extractor != scraped_facts_v1"


def all_tags_canonical(state: dict) -> CheckResult:
    """Hard invariant T1.5: todos los tags persistidos deben existir en canonical_tags."""
    unknown = state.get("_unknown_tag_count")
    if unknown is None:
        return None
    if unknown == 0:
        return True
    return f"{unknown} tags fuera de canonical_tags"


# ─────────────────────────────────────────────────────────────────────
# Casos
# ─────────────────────────────────────────────────────────────────────


CASES: list[Case] = [
    # ── 1. Obras temporales ─────────────────────────────────────────
    Case(
        case_id="grau_roig_obras",
        category="obras_temporales",
        description="Grau Roig (Andorra): 2025 con obras, 2026 tranquilo. "
                    "Pre-T1.4 esperamos quietness razonable; post-T1.4 esperamos "
                    "fila en spot_alerts con construction.",
        spot_id=85057,
        requires_tasks=("T1.4", "T1.4b", "T1.4c"),
        hard=[
            Assertion("summary_present", "hard", summary_not_empty,
                      "summary_en no debe estar vacío"),
            Assertion("chronology_ok", "hard", chronology_not_inverted,
                      "summary no invierte cronología sobre obras (heurística)"),
            Assertion("alert_construction_active", "hard", has_construction_alert,
                      "T1.4: existe spot_alerts row con alert_type='construction' y resolved=FALSE"),
            Assertion("active_alert_has_construction", "hard",
                      active_alert_types_contains("construction"),
                      "T1.4c: spot_semantic_state.active_alert_types incluye 'construction'"),
            Assertion("elevation_around_2110", "hard",
                      spot_geo_elevation_around(2110, tol=100),
                      "T1.4b: spot_geo.elevation_m ≈ 2110m"),
        ],
        bands=[
            Assertion("quietness_balanced", "band", signal_score_in("quietness", 0.4, 0.7),
                      "Quietness balanceado entre 2025 (ruidoso) y 2026 (tranquilo)"),
            Assertion("summary_length", "band", summary_word_count_in(60, 160),
                      "Summary de spot rico debería tener 60-160 palabras"),
        ],
        soft=[
            "Revisar manualmente que el summary cite la transicion 2025-2026.",
            "Confirmar que excerpt del claim sigue en idioma original (NL bouwput / FR / EN).",
        ],
    ),
    Case(
        case_id="obras_temporales_2",
        category="obras_temporales",
        description="Spot con obras documentadas en reviews recientes (NO Grau Roig).",
        spot_id=None,  # TODO
        locator_hint=(
            "SELECT r.spot_id, COUNT(*) FROM reviews r "
            "WHERE (r.texto ILIKE '%obras%' OR r.texto ILIKE '%construction%' "
            "OR r.texto ILIKE '%baustelle%' OR r.texto ILIKE '%chantier%' "
            "OR r.texto ILIKE '%bouwput%') AND r.fecha > '2025-01-01' "
            "GROUP BY r.spot_id HAVING COUNT(*) >= 2 LIMIT 5;"
        ),
        requires_tasks=("T1.4",),
        hard=[
            Assertion("summary_present", "hard", summary_not_empty, ""),
            Assertion("alert_construction_active", "hard", has_construction_alert, ""),
        ],
        bands=[],
        soft=["Revisar excerpts en idioma original."],
    ),

    # ── 2. Andorra Campers (taller mal clasificado) ─────────────────
    Case(
        case_id="andorra_campers_workshop",
        category="funcional_misclassified",
        description="Andorra Campers: taller/tienda mal clasificado como spot de pernocta.",
        spot_id=None,  # TODO
        locator_hint=(
            "SELECT id, canonical_name FROM spots WHERE country_iso='ad' "
            "AND canonical_name ILIKE '%andorra%camper%' LIMIT 5;"
        ),
        requires_tasks=("T1.4b",),
        hard=[
            Assertion("spot_function_workshop", "hard",
                      spot_function_is("shop_workshop"),
                      "T1.4b: spot_function='shop_workshop'"),
        ],
        bands=[],
        soft=["Verificar que is_overnight_viable=false."],
    ),

    # ── 3. Contradicción SERVICES vs reviews (T1.2) ─────────────────
    Case(
        case_id="services_water_contradiction",
        category="redundancia_circular",
        description="SERVICES dice agua=YES, varias reviews recientes dicen 'grifo roto'.",
        spot_id=None,  # TODO
        locator_hint=(
            "SELECT s.id, s.canonical_name FROM spots s JOIN reviews r ON r.spot_id=s.id "
            "WHERE s.agua_potable=TRUE AND (r.texto ILIKE '%grifo roto%' OR "
            "r.texto ILIKE '%water broken%' OR r.texto ILIKE '%tap not working%') "
            "GROUP BY s.id, s.canonical_name HAVING COUNT(*) >= 2 LIMIT 5;"
        ),
        requires_tasks=("T1.2",),
        hard=[
            Assertion("no_null_review_id_claims", "hard",
                      claims_review_id_not_null_except_scraped,
                      "T1.2: claims persistidos sin review_id deben venir solo de scraped_facts"),
        ],
        bands=[],
        soft=["Verificar manualmente que water_working=false aparece con review_id real."],
    ),
    Case(
        case_id="services_electric_contradiction",
        category="redundancia_circular",
        description="SERVICES electricidad=YES + reviews indican enchufes muertos.",
        spot_id=None,  # TODO
        locator_hint=(
            "SELECT s.id FROM spots s JOIN reviews r ON r.spot_id=s.id "
            "WHERE s.electricidad=TRUE AND (r.texto ILIKE '%no funciona%enchufe%' "
            "OR r.texto ILIKE '%electricity not%' OR r.texto ILIKE '%kein strom%') "
            "GROUP BY s.id HAVING COUNT(*) >= 2 LIMIT 5;"
        ),
        requires_tasks=("T1.2",),
        hard=[Assertion("no_null_review_id_claims", "hard",
                        claims_review_id_not_null_except_scraped, "")],
        bands=[],
        soft=[],
    ),
    Case(
        case_id="services_dump_contradiction",
        category="redundancia_circular",
        description="SERVICES tiene vaciado=YES pero reviews dicen 'sucio/cerrado'.",
        spot_id=None,  # TODO
        locator_hint=(
            "SELECT s.id FROM spots s JOIN reviews r ON r.spot_id=s.id "
            "WHERE s.vaciado_negras=TRUE AND r.texto ILIKE '%dump%closed%' "
            "GROUP BY s.id HAVING COUNT(*) >= 2 LIMIT 5;"
        ),
        requires_tasks=("T1.2",),
        hard=[Assertion("no_null_review_id_claims", "hard",
                        claims_review_id_not_null_except_scraped, "")],
        bands=[],
        soft=[],
    ),

    # ── 4. Multilingüe culturalmente cargado (T2.1 ideal, baseline ahora) ─
    Case(
        case_id="multi_nl_bouwput",
        category="multilingual",
        description="Spot con review en NL usando 'bouwput' (cultural strong negative).",
        spot_id=None,  # TODO
        locator_hint=(
            "SELECT spot_id, COUNT(*) FROM reviews WHERE texto ILIKE '%bouwput%' "
            "GROUP BY spot_id LIMIT 5;"
        ),
        hard=[Assertion("summary_present", "hard", summary_not_empty, "")],
        bands=[],
        soft=["Verificar que la cuantía cultural NL se refleja (severity > 0.7) "
              "y que el excerpt sigue en holandés."],
    ),
    Case(
        case_id="multi_de_baustelle",
        category="multilingual",
        description="Spot con review en DE usando 'Baustelle'.",
        spot_id=None,  # TODO
        locator_hint=(
            "SELECT spot_id FROM reviews WHERE texto ILIKE '%baustelle%' "
            "GROUP BY spot_id LIMIT 5;"
        ),
        hard=[Assertion("summary_present", "hard", summary_not_empty, "")],
        bands=[],
        soft=["Excerpt en alemán original."],
    ),
    Case(
        case_id="multi_fr_chantier",
        category="multilingual",
        description="Spot con review en FR usando 'chantier'.",
        spot_id=None,  # TODO
        locator_hint=(
            "SELECT spot_id FROM reviews WHERE texto ILIKE '%chantier%' "
            "GROUP BY spot_id LIMIT 5;"
        ),
        hard=[Assertion("summary_present", "hard", summary_not_empty, "")],
        bands=[],
        soft=["Excerpt en francés original."],
    ),

    # ── 5. Spots con 1 sola review (edge case agregación) ───────────
    Case(
        case_id="single_review_spot_a",
        category="edge_aggregation",
        description="Spot enriquecido con exactamente 1 review (riesgo de overfitting).",
        spot_id=None,  # TODO
        locator_hint=(
            "SELECT s.id, s.total_reviews FROM spots s JOIN spot_semantic_state sss "
            "ON sss.spot_id=s.id WHERE s.total_reviews = 1 "
            "AND sss.enrichment_version >= 4 LIMIT 5;"
        ),
        hard=[
            Assertion("summary_present", "hard", summary_not_empty, ""),
        ],
        bands=[
            Assertion("tags_modest", "band", tags_count_in(1, 5),
                      "Spot con 1 review no debería tener 8 tags"),
            Assertion("summary_short", "band", summary_word_count_in(15, 80),
                      "Summary corto para 1 review"),
        ],
        soft=["Confidence general debería ser baja."],
    ),
    Case(
        case_id="single_review_spot_b",
        category="edge_aggregation",
        description="Otro spot con 1 review, género distinto al anterior.",
        spot_id=None,  # TODO
        locator_hint="Igual que single_review_spot_a, escoger otro distinto.",
        hard=[Assertion("summary_present", "hard", summary_not_empty, "")],
        bands=[Assertion("tags_modest", "band", tags_count_in(1, 5), "")],
        soft=[],
    ),

    # ── 6. Cerrado permanentemente ─────────────────────────────────
    Case(
        case_id="permanently_closed",
        category="closure",
        description="Spot con múltiples reviews recientes reportando cierre definitivo.",
        spot_id=None,  # TODO
        locator_hint=(
            "SELECT spot_id FROM reviews WHERE (texto ILIKE '%permanently closed%' "
            "OR texto ILIKE '%cerrado definitivamente%' OR texto ILIKE '%no longer exists%') "
            "AND fecha > '2025-01-01' GROUP BY spot_id HAVING COUNT(*) >= 2 LIMIT 5;"
        ),
        requires_tasks=("T1.4",),
        hard=[
            Assertion("alert_closed_active", "hard",
                      lambda s: (
                          True if any(a.get("alert_type") == "permanently_closed"
                                      and not a.get("resolved", False)
                                      for a in (s.get("_alerts_rows") or []))
                          else (None if s.get("_alerts_rows") is None
                                else "ninguna alerta permanently_closed activa")
                      ),
                      "spot_alerts row con permanently_closed activo"),
        ],
        bands=[],
        soft=["Verificar que NO decae (permanent_* no aplica decay según D1)."],
    ),

    # ── 7. Estacionales ────────────────────────────────────────────
    Case(
        case_id="seasonal_summer_only",
        category="seasonal",
        description="Camping estacional (sólo verano).",
        spot_id=None,  # TODO
        locator_hint=(
            "SELECT id FROM spots WHERE temporada_apertura ILIKE '%june%' "
            "AND temporada_apertura ILIKE '%september%' AND total_reviews >= 5 LIMIT 5;"
        ),
        hard=[Assertion("summary_present", "hard", summary_not_empty, "")],
        bands=[],
        soft=["Best_season debería ser 'summer' o 'june-august'.",
              "Avoid_season debería ser 'winter'."],
    ),
    Case(
        case_id="seasonal_closed_winter",
        category="seasonal",
        description="Spot con cierre invernal en reviews recientes.",
        spot_id=None,  # TODO
        locator_hint=(
            "SELECT spot_id FROM reviews WHERE (texto ILIKE '%closed in winter%' "
            "OR texto ILIKE '%cerrado en invierno%') GROUP BY spot_id LIMIT 5;"
        ),
        requires_tasks=("T1.4",),
        hard=[],
        bands=[],
        soft=["spot_alerts.closed_season debería estar presente con valid_until anual."],
    ),

    # ── 8. Edge cases temporales (scores opuestos en misma semana) ──
    Case(
        case_id="temporal_opposite_same_week",
        category="edge_temporal",
        description="Spot con 2+ reviews de la misma semana con scores claramente opuestos.",
        spot_id=None,  # TODO
        locator_hint=(
            "Buscar manualmente spots con reviews de fechas cercanas y ratings 1 vs 5."
        ),
        hard=[Assertion("summary_present", "hard", summary_not_empty, "")],
        bands=[
            Assertion("varianza_alta_summary", "band", summary_word_count_in(40, 160),
                      "Summary debería mencionar la variabilidad de opiniones"),
        ],
        soft=["Verificar que el summary reconoce la disparidad."],
    ),
    Case(
        case_id="temporal_change_regime",
        category="edge_temporal",
        description="Spot donde reviews históricas y recientes divergen (cambio de régimen).",
        spot_id=None,  # TODO
        locator_hint=(
            "Spots con quietness_score histórico vs últimas 5 reviews distinto. "
            "Requiere query post-T2.5. Por ahora identificar manualmente."
        ),
        requires_tasks=("T2.5",),
        hard=[],
        bands=[],
        soft=["Tras T2.5: signal_flux debería tener entrada con changed=true."],
    ),

    # ── 9. Agregación con varianza alta ────────────────────────────
    Case(
        case_id="high_variance_quietness",
        category="aggregation_variance",
        description="Spot con quietness valoraciones muy dispares en reviews.",
        spot_id=None,  # TODO
        locator_hint=(
            "Spots populares con reviews polarizadas en ruido (carretera, "
            "ej. AC junto a autopista con reviews mixtas)."
        ),
        hard=[Assertion("summary_present", "hard", summary_not_empty, "")],
        bands=[
            Assertion("quietness_mid", "band", signal_score_in("quietness", 0.3, 0.7),
                      "Quietness ni alto ni bajo cuando hay alta varianza"),
        ],
        soft=["Confidence < 0.8 esperable."],
    ),
    Case(
        case_id="high_variance_safety",
        category="aggregation_variance",
        description="Spot con safety polarizado (algunos reportan robos, otros tranquilidad).",
        spot_id=None,  # TODO
        locator_hint="Manual.",
        hard=[Assertion("summary_present", "hard", summary_not_empty, "")],
        bands=[],
        soft=["Summary debería mencionar la disparidad."],
    ),

    # ── 10. Canonical tags (T1.5) ──────────────────────────────────
    Case(
        case_id="canonical_tags_grau_roig",
        category="canonicalization",
        description="Tags de Grau Roig deben ser canónicos tras T1.5.",
        spot_id=85057,
        requires_tasks=("T1.5",),
        hard=[
            Assertion("all_canonical", "hard", all_tags_canonical,
                      "T1.5: cero tags fuera de canonical_tags"),
        ],
        bands=[Assertion("tags_3_8", "band", tags_count_in(3, 8), "")],
        soft=[],
    ),

    # ── 11. Smoke generico — cualquier spot enriquecido v4 ──────────
    Case(
        case_id="smoke_v4_any",
        category="smoke",
        description="Cualquier spot enriquecido con v4: parse ok, summary presente, tags razonables.",
        spot_id=None,  # TODO: seleccionar spot estable (no Grau Roig)
        locator_hint=(
            "SELECT spot_id FROM spot_semantic_state WHERE enrichment_version >= 4 "
            "AND summary_en IS NOT NULL AND array_length(tags, 1) BETWEEN 4 AND 7 "
            "AND total_observations >= 10 LIMIT 5;"
        ),
        hard=[
            Assertion("summary_present", "hard", summary_not_empty, ""),
            Assertion("tags_in_range", "hard", tags_count_in(3, 8), ""),
            Assertion("no_marketing", "hard", tag_not_contains("amazing"),
                      "Tags no deberían tener tone marketing"),
            Assertion("no_marketing_2", "hard", tag_not_contains("wonderful"), ""),
        ],
        bands=[Assertion("summary_length", "band", summary_word_count_in(20, 200), "")],
        soft=[],
    ),
]


# ─────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────


async def _load_state_for_case(conn, spot_id: int) -> dict:
    """Carga el estado actual de la DB para un spot.

    Inyecta campos virtuales (con prefijo `_`) para aserciones que requieren
    tablas/columnas opcionales (T1.4, T1.4b, T1.5):
      - `_alerts_rows`        → list[dict] o None si tabla no existe
      - `_spot_geo_row`       → dict o None
      - `_claims_review_id_null_non_scraped` → int o None
      - `_unknown_tag_count`  → int o None (requiere canonical_tags)
    """
    row = await conn.fetchrow(
        """
        SELECT
            sss.spot_id, sss.summary_en, sss.summary_es, sss.tags, sss.best_for,
            sss.best_season, sss.avoid_season,
            sss.quietness_score, sss.safety_score, sss.police_risk_score,
            sss.beauty_score, sss.crowd_level_score, sss.overnight_safe,
            sss.stealth_score, sss.signals_data,
            sss.total_observations, sss.consensus_confidence,
            sss.enrichment_version, sss.llm_model,
            sss.stale, sss.last_aggregated_at
        FROM spot_semantic_state sss
        WHERE sss.spot_id = $1
        """,
        spot_id,
    )
    if not row:
        return {"_missing": True, "spot_id": spot_id}
    state = dict(row)

    # T1.4b: spot_function / is_overnight_viable / authorization_status (en spots)
    try:
        sp = await conn.fetchrow(
            """
            SELECT spot_function, is_overnight_viable, authorization_status
            FROM spots WHERE id = $1
            """,
            spot_id,
        )
        if sp:
            state.update(dict(sp))
    except asyncpg.UndefinedColumnError:
        pass  # columnas no existen aún

    # T1.4c: active_alert_types + signal_flux
    try:
        extra = await conn.fetchrow(
            "SELECT active_alert_types, signal_flux FROM spot_semantic_state WHERE spot_id = $1",
            spot_id,
        )
        if extra:
            state.update(dict(extra))
    except asyncpg.UndefinedColumnError:
        pass

    # T1.4: spot_alerts
    try:
        alerts = await conn.fetch(
            "SELECT alert_type, severity, valid_from, valid_until, confidence, resolved "
            "FROM spot_alerts WHERE spot_id = $1",
            spot_id,
        )
        state["_alerts_rows"] = [dict(a) for a in alerts]
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
        state["_alerts_rows"] = None

    # T1.4b: spot_geo
    try:
        geo = await conn.fetchrow(
            "SELECT elevation_m, slope_degrees, terrain_type FROM spot_geo WHERE spot_id = $1",
            spot_id,
        )
        state["_spot_geo_row"] = dict(geo) if geo else None
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
        state["_spot_geo_row"] = None

    # T1.2: claims sin review_id que no sean scraped_facts
    try:
        n = await conn.fetchval(
            """
            SELECT COUNT(*) FROM extracted_claims
            WHERE spot_id = $1 AND review_id IS NULL
              AND extractor_name NOT LIKE 'scraped_facts%'
            """,
            spot_id,
        )
        state["_claims_review_id_null_non_scraped"] = int(n or 0)
    except asyncpg.PostgresError:
        state["_claims_review_id_null_non_scraped"] = None

    # T1.5: tags no canónicos (sólo aplica si existe la tabla canonical_tags).
    # Replica el normalize_raw_tag de enrichment/tag_canonicalizer.py:
    #   lower → spaces/underscores/slashes a guiones → strip de caracteres no [a-z0-9-]
    # Esto absorbe tanto los tags PRE-v6 (con espacios y mayúsculas) como los nuevos.
    try:
        tags = state.get("tags") or []
        if isinstance(tags, str):
            tags = _parse_jsonb(tags) or []
        if tags:
            unknown = await conn.fetchval(
                """
                WITH normalized AS (
                    SELECT trim(both '-' from
                              regexp_replace(
                                regexp_replace(lower(tag), '[\\s_/]+', '-', 'g'),
                                '[^a-z0-9\\-]', '', 'g'
                              )
                           ) AS n
                    FROM unnest($1::text[]) AS t(tag)
                )
                SELECT COUNT(*) FROM normalized
                WHERE n <> ''
                  AND NOT EXISTS (
                    SELECT 1 FROM canonical_tags ct
                    WHERE ct.canonical_id = normalized.n
                       OR normalized.n = ANY(ct.aliases)
                  )
                """,
                tags,
            )
            state["_unknown_tag_count"] = int(unknown or 0)
        else:
            state["_unknown_tag_count"] = 0
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
        state["_unknown_tag_count"] = None

    return state


def _format_result(result: CheckResult) -> tuple[str, str]:
    """Devuelve (símbolo, texto) para un resultado."""
    if result is True:
        return "OK", ""
    if result is None:
        return "SKIP", "(precondición no cumplida)"
    if result is False:
        return "FAIL", "(sin mensaje)"
    return "FAIL", str(result)


def _strip_private(state: dict) -> dict:
    """Quita campos virtuales (prefijo _) para snapshots."""
    return {k: v for k, v in state.items() if not k.startswith("_")}


async def _cmd_check(pool, args) -> int:
    cases = _filter_cases(args)
    n_hard_fail = 0
    n_band_fail = 0
    n_skip = 0
    n_no_spot = 0
    n_no_state = 0

    print(f"\n=== regression check ({len(cases)} cases) ===\n")
    for case in cases:
        print(f"[{case.case_id}] {case.category}: {case.description}")
        if case.spot_id is None:
            print(f"  TODO: spot_id sin definir. Locator hint:")
            print(f"     {case.locator_hint}")
            n_no_spot += 1
            print()
            continue

        async with pool.acquire() as conn:
            state = await _load_state_for_case(conn, case.spot_id)

        if state.get("_missing"):
            print(f"  SKIP spot_id={case.spot_id} no tiene fila en spot_semantic_state. "
                  f"Enriquecer con orchestrator_v2 antes de validar.")
            n_no_state += 1
            print()
            continue

        print(f"  spot_id={case.spot_id} | enrichment_version={state.get('enrichment_version')} "
              f"| stale={state.get('stale')}")

        for tier_name, assertions in (("hard", case.hard), ("bands", case.bands)):
            for a in assertions:
                try:
                    result = a.check(state)
                except Exception as exc:
                    result = f"EXCEPTION: {type(exc).__name__}: {exc}"
                symbol, msg = _format_result(result)
                tag = f"[{tier_name}/{a.name}]"
                line = f"    {symbol:4} {tag} {msg}"
                print(line)
                if symbol == "FAIL":
                    if tier_name == "hard":
                        n_hard_fail += 1
                    else:
                        n_band_fail += 1
                elif symbol == "SKIP":
                    n_skip += 1

        if case.soft:
            for note in case.soft:
                print(f"    NOTE [soft] {note}")
        print()

    print("=" * 60)
    print(f"hard fails:     {n_hard_fail}")
    print(f"band warnings:  {n_band_fail}")
    print(f"skipped:        {n_skip} (precondiciones no cumplidas)")
    print(f"TODO cases:     {n_no_spot} (rellenar spot_id)")
    print(f"no state yet:   {n_no_state} (enriquecer spots primero)")
    print("=" * 60)
    return 1 if n_hard_fail > 0 else 0


async def _cmd_snapshot(pool, args) -> int:
    cases = _filter_cases(args)
    n_saved = 0
    for case in cases:
        if case.spot_id is None:
            continue
        async with pool.acquire() as conn:
            state = await _load_state_for_case(conn, case.spot_id)
        if state.get("_missing"):
            print(f"SKIP {case.case_id}: spot sin enriquecer")
            continue
        path = SNAPSHOT_DIR / f"{case.case_id}.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_strip_private(state), fh, indent=2, default=str, ensure_ascii=False)
        print(f"OK {case.case_id} -> {path.name}")
        n_saved += 1
    print(f"\n{n_saved} snapshots guardados en {SNAPSHOT_DIR}")
    return 0


def _cmd_list(args) -> int:
    cases = _filter_cases(args)
    by_cat: dict[str, list[Case]] = {}
    for c in cases:
        by_cat.setdefault(c.category, []).append(c)
    print(f"\n{len(cases)} casos definidos:\n")
    for cat, cs in sorted(by_cat.items()):
        print(f"## {cat}")
        for c in cs:
            sid = f"spot_id={c.spot_id}" if c.spot_id else "spot_id=TODO"
            reqs = f" requires={','.join(c.requires_tasks)}" if c.requires_tasks else ""
            print(f"  - {c.case_id:36}  {sid:18}{reqs}")
            print(f"      {c.description}")
        print()
    return 0


def _filter_cases(args) -> list[Case]:
    cases = CASES
    if getattr(args, "case", None):
        cases = [c for c in cases if c.case_id == args.case]
    if getattr(args, "category", None):
        cases = [c for c in cases if c.category == args.category]
    return cases


async def _async_main(args) -> int:
    if args.cmd == "list":
        return _cmd_list(args)

    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=1, max_size=4)
    try:
        if args.cmd == "check":
            return await _cmd_check(pool, args)
        if args.cmd == "snapshot":
            return await _cmd_snapshot(pool, args)
    finally:
        await pool.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="semantic_suite")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for cmd in ("list", "check", "snapshot"):
        sp = sub.add_parser(cmd)
        sp.add_argument("--case", help="Filtrar por case_id exacto")
        sp.add_argument("--category", help="Filtrar por categoría")
    args = parser.parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
