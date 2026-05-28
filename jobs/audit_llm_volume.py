"""Auditoría de volumen LLM y cobertura de regex.

Conecta a la DB real y produce métricas concretas sobre:
  1. Cuántas reviews van al LLM vs quedan en regex-only.
  2. Distribución de longitud de texto y cobertura de regex.
  3. Efectividad del text_trimmer.
  4. Estimación de tokens y coste para el batch completo.
  5. Qué señales cubre el regex y cuáles nunca se activan.
  6. Muestra de reviews que van al LLM con 0 claims regex (las más caras).

Uso:
  python -m jobs.audit_llm_volume
  python -m jobs.audit_llm_volume --sample 5000   # tamaño muestra (default 3000)
  python -m jobs.audit_llm_volume --country ES     # solo un país
  python -m jobs.audit_llm_volume --full-scan      # sin límite de muestra (lento)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import Counter, defaultdict

import asyncpg
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enrichment.claim_extractor import PATTERNS, extract_claims_regex
from enrichment.text_trimmer import trim_for_llm

# ── Precios de referencia (input tokens, Mayo 2026) ──────────────────────────
# Ajustar si los precios cambian.
COST_PER_1M_INPUT = {
    "deepseek_v4_flash":    0.07,   # DeepSeek V4 Flash — opción bulk
    "gemini_25_flash_lite": 0.10,   # Gemini 2.5 Flash Lite (con billing)
    "gemini_25_flash":      0.30,   # Gemini 2.5 Flash full
}
# Tokens promedio de output por llamada LLM (review-level)
AVG_OUTPUT_TOKENS_REVIEW = 120
AVG_INPUT_TOKENS_SYSTEM = 400      # system prompt (cacheado si provider lo soporta)
AVG_INPUT_TOKENS_FRAME = 50        # instrucción + overhead JSON


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


# ── Lógica de enrutamiento (espejo exacto del worker) ────────────────────────

def would_go_to_llm(text: str, n_regex: int) -> bool:
    """¿Esta review iría al LLM con la lógica corregida?

    Reglas (en orden):
    1. Texto < 120 chars → nunca LLM, independientemente de claims.
    2. regex ≥ 3 claims → cobertura suficiente, no escalar.
    3. Texto ≥ 120 chars + regex 0-2 claims → LLM.
    """
    if len(text) < 120:
        return False
    if n_regex >= 3:
        return False
    return True


# ── Queries a la DB ───────────────────────────────────────────────────────────

AGGREGATE_QUERY = """
SELECT
    COUNT(*)                                                     AS total_reviews,
    COUNT(*) FILTER (WHERE llm_processed = TRUE)                 AS already_processed,
    COUNT(*) FILTER (WHERE llm_processed IS DISTINCT FROM TRUE)  AS pending,
    COUNT(*) FILTER (
        WHERE llm_processed IS DISTINCT FROM TRUE
          AND length(COALESCE(texto, texto_original, '')) <= 3
    )                                                            AS pending_empty,
    COUNT(*) FILTER (
        WHERE llm_processed IS DISTINCT FROM TRUE
          AND length(COALESCE(texto, texto_original, '')) > 3
    )                                                            AS pending_with_text,

    -- Distribución de longitud (pending con texto)
    COUNT(*) FILTER (
        WHERE llm_processed IS DISTINCT FROM TRUE
          AND length(COALESCE(texto, texto_original, '')) BETWEEN 4 AND 20
    )                                                            AS len_micro,
    COUNT(*) FILTER (
        WHERE llm_processed IS DISTINCT FROM TRUE
          AND length(COALESCE(texto, texto_original, '')) BETWEEN 21 AND 60
    )                                                            AS len_short,
    COUNT(*) FILTER (
        WHERE llm_processed IS DISTINCT FROM TRUE
          AND length(COALESCE(texto, texto_original, '')) BETWEEN 61 AND 120
    )                                                            AS len_medium,
    COUNT(*) FILTER (
        WHERE llm_processed IS DISTINCT FROM TRUE
          AND length(COALESCE(texto, texto_original, '')) BETWEEN 121 AND 400
    )                                                            AS len_substantial,
    COUNT(*) FILTER (
        WHERE llm_processed IS DISTINCT FROM TRUE
          AND length(COALESCE(texto, texto_original, '')) > 400
    )                                                            AS len_long,

    -- Estadísticas de longitud
    ROUND(AVG(length(COALESCE(texto, texto_original, '')))
        FILTER (WHERE llm_processed IS DISTINCT FROM TRUE
          AND length(COALESCE(texto, texto_original, '')) > 3))  AS avg_len,
    PERCENTILE_CONT(0.50) WITHIN GROUP (
        ORDER BY length(COALESCE(texto, texto_original, ''))
    ) FILTER (WHERE llm_processed IS DISTINCT FROM TRUE
          AND length(COALESCE(texto, texto_original, '')) > 3)   AS median_len,
    PERCENTILE_CONT(0.90) WITHIN GROUP (
        ORDER BY length(COALESCE(texto, texto_original, ''))
    ) FILTER (WHERE llm_processed IS DISTINCT FROM TRUE
          AND length(COALESCE(texto, texto_original, '')) > 3)   AS p90_len,

    -- Breakdown por fuente (top fuentes)
    COUNT(*) FILTER (WHERE source = 'park4night'
        AND llm_processed IS DISTINCT FROM TRUE)                 AS pend_park4night,
    COUNT(*) FILTER (WHERE source = 'campercontact'
        AND llm_processed IS DISTINCT FROM TRUE)                 AS pend_campercontact,
    COUNT(*) FILTER (WHERE source = 'furgovw'
        AND llm_processed IS DISTINCT FROM TRUE)                 AS pend_furgovw,
    COUNT(*) FILTER (WHERE source = 'campingcarinfos'
        AND llm_processed IS DISTINCT FROM TRUE)                 AS pend_campingcarinfos,
    COUNT(*) FILTER (WHERE source = 'freecampsites'
        AND llm_processed IS DISTINCT FROM TRUE)                 AS pend_freecampsites
FROM reviews
"""

AGGREGATE_COUNTRY_QUERY = """
SELECT
    COUNT(*)                                                     AS total_reviews,
    COUNT(*) FILTER (WHERE r.llm_processed IS DISTINCT FROM TRUE) AS pending,
    COUNT(*) FILTER (
        WHERE r.llm_processed IS DISTINCT FROM TRUE
          AND length(COALESCE(r.texto, r.texto_original, '')) > 3
    )                                                            AS pending_with_text,
    ROUND(AVG(length(COALESCE(r.texto, r.texto_original, '')))
        FILTER (WHERE r.llm_processed IS DISTINCT FROM TRUE
          AND length(COALESCE(r.texto, r.texto_original, '')) > 3)) AS avg_len
FROM reviews r
JOIN spots s ON s.id = r.spot_id
WHERE s.country_iso = $1
"""

SAMPLE_QUERY = """
SELECT
    r.id,
    r.source,
    COALESCE(r.texto, r.texto_original) AS texto
FROM reviews r
{country_join}
WHERE r.llm_processed IS DISTINCT FROM TRUE
  AND length(COALESCE(r.texto, r.texto_original, '')) > 3
  {country_filter}
ORDER BY RANDOM()
LIMIT $1
"""

# Top sources by pending count (para ordenar la tabla)
SOURCE_BREAKDOWN_QUERY = """
SELECT source, COUNT(*) AS pending
FROM reviews
WHERE llm_processed IS DISTINCT FROM TRUE
  AND length(COALESCE(texto, texto_original, '')) > 3
GROUP BY source
ORDER BY pending DESC
LIMIT 20
"""

# Spots sin enriquecimiento v2 (candidatos orchestrator_v2)
SPOTS_V2_QUERY = """
SELECT
    COUNT(*)                                     AS total_spots_active,
    COUNT(*) FILTER (WHERE sss.spot_id IS NULL)  AS spots_never_enriched,
    COUNT(*) FILTER (WHERE sss.spot_id IS NOT NULL
        AND sss.enrichment_version IS NULL)      AS spots_enriched_no_version,
    COUNT(*) FILTER (WHERE s.total_reviews >= 3) AS spots_enough_reviews
FROM spots s
LEFT JOIN spot_semantic_state sss ON sss.spot_id = s.id
WHERE s.activo = TRUE
"""

# ── Análisis local de la muestra ─────────────────────────────────────────────

def analyse_sample(rows: list[dict]) -> dict:
    """Corre regex + trim sobre cada review de la muestra y devuelve stats."""
    n_total = len(rows)
    if n_total == 0:
        return {}

    llm_calls = 0
    regex_only = 0
    skipped_empty = 0         # texto vacío o muy corto (ya filtrado en la query, pero por si acaso)

    claim_counts = Counter()  # distribución de nº de claims regex
    signal_hits = Counter()   # cuántas veces activa cada señal el regex
    zero_claim_long = []      # textos que van al LLM con 0 claims regex (los más caros)
    one_two_long = []         # textos con 1-2 claims y texto largo

    total_chars_before_trim = 0
    total_chars_after_trim = 0
    trim_ratios = []

    len_buckets = Counter()   # distribución de longitudes (para las que van al LLM)

    for row in rows:
        text = (row.get("texto") or "").strip()
        if len(text) <= 3:
            skipped_empty += 1
            continue

        # Regex
        claims = extract_claims_regex(text)
        n = len(claims)
        claim_counts[n] += 1

        for c in claims:
            signal_hits[c["signal"]] += 1

        # Decisión LLM
        goes_to_llm = would_go_to_llm(text, n)

        if goes_to_llm:
            llm_calls += 1
            # Trim
            trimmed = trim_for_llm(text)
            total_chars_before_trim += len(text)
            total_chars_after_trim += len(trimmed)
            ratio = 1.0 - len(trimmed) / max(len(text), 1)
            trim_ratios.append(ratio)

            # Longitud bucket (del texto ya trimmed, que es lo que se manda)
            tl = len(trimmed)
            if tl <= 120:
                len_buckets["≤120 chars"] += 1
            elif tl <= 300:
                len_buckets["121–300 chars"] += 1
            elif tl <= 600:
                len_buckets["301–600 chars"] += 1
            else:
                len_buckets[">600 chars"] += 1

            if n == 0 and len(text) >= 120:
                zero_claim_long.append((text, row.get("source", "?")))
            elif n in (1, 2):
                one_two_long.append((text, claims, row.get("source", "?")))
        else:
            regex_only += 1

    # Trim stats
    avg_trim = sum(trim_ratios) / len(trim_ratios) if trim_ratios else 0
    trim_gt10 = sum(1 for r in trim_ratios if r > 0.10)
    trim_gt30 = sum(1 for r in trim_ratios if r > 0.30)

    # Token estimates (chars / 4 = tokens — aprox para texto latino/europeo)
    avg_tokens_before = (total_chars_before_trim / llm_calls / 4) if llm_calls else 0
    avg_tokens_after  = (total_chars_after_trim  / llm_calls / 4) if llm_calls else 0

    return {
        "n_total": n_total,
        "skipped_empty": skipped_empty,
        "llm_calls": llm_calls,
        "regex_only": regex_only,
        "claim_counts": dict(claim_counts),
        "signal_hits": dict(signal_hits.most_common(30)),
        "zero_claim_long": zero_claim_long[:8],   # muestra para inspección
        "one_two_long": one_two_long[:5],
        "avg_trim": avg_trim,
        "trim_gt10_pct": trim_gt10 / llm_calls * 100 if llm_calls else 0,
        "trim_gt30_pct": trim_gt30 / llm_calls * 100 if llm_calls else 0,
        "avg_tokens_before_trim": avg_tokens_before,
        "avg_tokens_after_trim":  avg_tokens_after,
        "token_savings_pct": (1 - avg_tokens_after / avg_tokens_before) * 100 if avg_tokens_before else 0,
        "len_buckets": dict(len_buckets),
    }


# ── Formateo del informe ──────────────────────────────────────────────────────

def _pct(n, total):
    return f"{n/total*100:.1f}%" if total else "N/A"

def _bar(n, total, width=30):
    if not total:
        return ""
    filled = int(n / total * width)
    return "█" * filled + "░" * (width - filled)


def print_report(agg: dict, sample_stats: dict, source_rows: list[dict],
                 country: str | None = None, sample_size: int = 0):
    sep = "═" * 72

    print(f"\n{sep}")
    print(f"  AUDITORÍA LLM VOLUME — GeoSpots   [{country or 'GLOBAL'}]")
    print(sep)

    # ── Sección 1: Volumen total ──────────────────────────────────────────
    total  = agg["total_reviews"]
    done   = agg["already_processed"]
    pend   = agg["pending"]
    empty  = agg["pending_empty"]
    p_text = agg["pending_with_text"]

    print(f"\n{'─'*40}")
    print("  1. VOLUMEN DE REVIEWS")
    print(f"{'─'*40}")
    print(f"  Total reviews en DB:          {total:>10,}")
    print(f"  Ya procesadas (llm_processed):{done:>10,}  ({_pct(done,total)})")
    print(f"  Pendientes total:             {pend:>10,}  ({_pct(pend,total)})")
    print(f"  Pendientes vacías (skip):     {empty:>10,}  ({_pct(empty,pend)})")
    print(f"  Pendientes con texto útil:    {p_text:>10,}  ← estas son las que importan")

    # Distribución de longitud
    print(f"\n  Distribución de longitud (pendientes con texto):")
    buckets = [
        ("micro  (4–20 chars)",   agg["len_micro"]),
        ("corto  (21–60 chars)",  agg["len_short"]),
        ("medio  (61–120 chars)", agg["len_medium"]),
        ("sust. (121–400 chars)", agg["len_substantial"]),
        ("largo  (>400 chars)",   agg["len_long"]),
    ]
    for label, n in buckets:
        bar = _bar(n, p_text)
        print(f"    {label}: {n:>8,}  {_pct(n,p_text):>6}  {bar}")
    print(f"\n    Longitud media:   {agg.get('avg_len', 0):.0f} chars")
    print(f"    Mediana:          {agg.get('median_len', 0):.0f} chars")
    print(f"    Percentil 90:     {agg.get('p90_len', 0):.0f} chars")

    # ── Sección 2: Análisis de la muestra ─────────────────────────────────
    if not sample_stats:
        print("\n  [sin muestra analizada]")
        return

    ss = sample_stats
    n_s  = ss["n_total"]
    llm  = ss["llm_calls"]
    reg  = ss["regex_only"]
    llm_rate = llm / n_s if n_s else 0

    print(f"\n{'─'*40}")
    print(f"  2. ENRUTAMIENTO LLM (muestra n={n_s:,})")
    print(f"{'─'*40}")
    print(f"  → Solo regex (n_regex≥3 o texto<120):  {reg:>7,}  ({_pct(reg, n_s)})")
    print(f"  → Al LLM (≤2 claims o texto largo):   {llm:>7,}  ({_pct(llm, n_s)})")
    print(f"  ⚠  Tasa de LLM: {llm_rate*100:.1f}%")

    # Desglose de los que van al LLM
    cc = ss["claim_counts"]
    llm_0 = cc.get(0, 0)
    llm_12 = sum(cc.get(i, 0) for i in (1, 2))
    print(f"\n  De las que van al LLM:")
    print(f"    - 0 claims regex (el LLM hace todo el trabajo): {llm_0:>6,}  ({_pct(llm_0, llm)})")
    print(f"    - 1–2 claims regex (LLM complementa):          {llm_12:>6,}  ({_pct(llm_12, llm)})")

    print(f"\n  Distribución completa de claims regex (toda la muestra):")
    for k in sorted(cc.keys()):
        bar = _bar(cc[k], n_s, 25)
        label = f"{k} claims" if k < 5 else "5+ claims"
        print(f"    {label}: {cc[k]:>7,}  ({_pct(cc[k], n_s):>6})  {bar}")

    # Buckets de longitud para las que van al LLM (texto post-trim)
    lb = ss.get("len_buckets", {})
    if lb:
        print(f"\n  Longitud del texto enviado al LLM (post-trim):")
        for bname in ("≤120 chars", "121–300 chars", "301–600 chars", ">600 chars"):
            n = lb.get(bname, 0)
            print(f"    {bname}: {n:>6,}  ({_pct(n, llm)})")

    # ── Sección 3: Text trimmer ────────────────────────────────────────────
    print(f"\n{'─'*40}")
    print("  3. EFECTIVIDAD DEL TEXT_TRIMMER")
    print(f"{'─'*40}")
    at_b = ss["avg_tokens_before_trim"]
    at_a = ss["avg_tokens_after_trim"]
    print(f"  Tokens medios pre-trim:   {at_b:.0f}")
    print(f"  Tokens medios post-trim:  {at_a:.0f}  (-{ss['token_savings_pct']:.1f}%)")
    print(f"  Reviews con >10% trim:    {ss['trim_gt10_pct']:.1f}%")
    print(f"  Reviews con >30% trim:    {ss['trim_gt30_pct']:.1f}%")
    if ss["token_savings_pct"] < 3:
        print("  ⚠  Ahorro mínimo — el trimmer apenas recorta. Revisar patrones filler.")
    elif ss["token_savings_pct"] < 8:
        print("  ℹ  Ahorro moderado. Hay margen para expandir patrones filler.")
    else:
        print("  ✓  Ahorro relevante. El trimmer funciona bien.")

    # ── Sección 4: Estimación de coste ────────────────────────────────────
    print(f"\n{'─'*40}")
    print("  4. ESTIMACIÓN DE COSTE — BATCH COMPLETO")
    print(f"{'─'*40}")

    # Extrapolamos la tasa de LLM de la muestra al total pendiente
    estimated_llm_calls = int(p_text * llm_rate)
    print(f"  Reviews pendientes con texto: {p_text:,}")
    print(f"  Tasa LLM estimada:            {llm_rate*100:.1f}%  (de la muestra)")
    print(f"  → LLM calls estimadas:        {estimated_llm_calls:,}")

    # Tokens por llamada (input = system + frame + texto trimmed; output fijo)
    input_tok = AVG_INPUT_TOKENS_SYSTEM + AVG_INPUT_TOKENS_FRAME + at_a
    output_tok = AVG_OUTPUT_TOKENS_REVIEW
    total_input_M  = estimated_llm_calls * input_tok / 1_000_000
    total_output_M = estimated_llm_calls * output_tok / 1_000_000
    print(f"\n  Tokens por llamada (estimados):")
    print(f"    Input:  ~{input_tok:.0f} (system≈{AVG_INPUT_TOKENS_SYSTEM} + frame≈{AVG_INPUT_TOKENS_FRAME} + texto≈{at_a:.0f})")
    print(f"    Output: ~{output_tok} (JSON claims)")
    print(f"  Total input tokens:   {total_input_M:.1f}M")
    print(f"  Total output tokens:  {total_output_M:.1f}M")

    print(f"\n  Coste estimado por provider (solo input+output):")
    for provider, price_in in COST_PER_1M_INPUT.items():
        # Output aprox al 50% del precio del input para la mayoría de modelos
        cost = total_input_M * price_in + total_output_M * (price_in * 0.5)
        print(f"    {provider:<28} → ${cost:.1f}  (~${cost/1.15:.0f} si Gemini cachea system prompt)")
    print()
    print("  ⚠  Estos números NO incluyen el pipeline spot-level (orchestrator_v2).")
    print("     Ver sección 6 para esa estimación separada.")

    # ── Sección 5: Señales del regex ──────────────────────────────────────
    print(f"\n{'─'*40}")
    print("  5. COBERTURA DE SEÑALES (regex, en la muestra)")
    print(f"{'─'*40}")

    sh = ss["signal_hits"]
    # Señales definidas en PATTERNS
    signals_in_patterns = sorted({s for s, _, _, _ in PATTERNS})
    # Señales en STATIC_SIGNALS pero NO en PATTERNS
    try:
        from enrichment.signal_registry import STATIC_SIGNALS
        all_signals = sorted(STATIC_SIGNALS.keys())
        not_in_regex = [s for s in all_signals if s not in {s for s, _, _, _ in PATTERNS}]
    except Exception:
        all_signals = signals_in_patterns
        not_in_regex = []

    print(f"  Señales en PATTERNS:     {len(signals_in_patterns)}")
    print(f"  Señales totales (STATIC): {len(all_signals)}")
    print(f"  Señales sin regex:       {len(not_in_regex)}")

    print(f"\n  Top señales detectadas en muestra:")
    max_hits = max(sh.values()) if sh else 1
    for sig, hits in sorted(sh.items(), key=lambda x: -x[1])[:20]:
        bar = _bar(hits, max_hits, 20)
        rate = hits / n_s * 100
        print(f"    {sig:<28} {hits:>5,}  ({rate:.1f}%)  {bar}")

    if not_in_regex:
        print(f"\n  Señales SIN COBERTURA REGEX (solo vía LLM o scraped_facts):")
        for s in not_in_regex:
            print(f"    ✗ {s}")

    # Señales en PATTERNS pero con 0 hits en la muestra (posible regex muerto)
    zero_hit = [s for s in signals_in_patterns if sh.get(s, 0) == 0]
    if zero_hit:
        print(f"\n  Señales en PATTERNS con 0 hits en muestra (posible regex débil):")
        for s in zero_hit:
            print(f"    ⚠ {s}")

    # ── Sección 6: Pipeline spot-level (v2) ───────────────────────────────
    print(f"\n{'─'*40}")
    print("  6. ESTIMACIÓN PIPELINE SPOT-LEVEL (orchestrator_v2)")
    print(f"{'─'*40}")
    print("  [requiere consulta adicional — ver sección 'Spots sin enriquecer' más abajo]")

    # ── Sección 7: Breakdown por fuente ───────────────────────────────────
    print(f"\n{'─'*40}")
    print("  7. PENDIENTES POR FUENTE")
    print(f"{'─'*40}")
    for r in source_rows:
        src   = r["source"] or "?"
        n     = r["pending"]
        pct   = n / p_text * 100 if p_text else 0
        bar   = _bar(n, p_text or 1, 25)
        print(f"  {src:<22} {n:>8,}  ({pct:.1f}%)  {bar}")

    # ── Sección 8: Ejemplos de reviews que van al LLM sin ningún claim regex ──
    print(f"\n{'─'*40}")
    print("  8. MUESTRA: reviews sin claims regex → LLM hace TODO (las más caras)")
    print(f"{'─'*40}")
    for i, (text, source) in enumerate(ss.get("zero_claim_long", [])[:6], 1):
        preview = text[:200].replace("\n", " ")
        trimmed = trim_for_llm(text)
        ratio = 1 - len(trimmed) / len(text)
        print(f"\n  [{i}] {source}  (len={len(text)}, trim={ratio:.0%})")
        print(f"  ┆ {preview}")
        if ratio > 0.05:
            print(f"  ┆ trimmed: {trimmed[:200].replace(chr(10), ' ')}")

    # ── Sección 9: Resumen y recomendaciones ──────────────────────────────
    print(f"\n{'─'*40}")
    print("  9. RESUMEN Y RECOMENDACIONES")
    print(f"{'─'*40}")

    recs = []

    if llm_rate > 0.60:
        recs.append(
            "⚠  ALTA tasa LLM (>60%). El regex cubre poco. Expandir PATTERNS o"
            " subir el umbral mínimo de longitud para escalar al LLM."
        )
    elif llm_rate > 0.40:
        recs.append(
            "ℹ  Tasa LLM moderada (40-60%). Hay margen. Identificar los 5-10 patrones"
            " más frecuentes en las reviews de 0-claim y añadirlos al regex."
        )
    else:
        recs.append(
            "✓  Tasa LLM razonable (<40%). El regex absorbe la mayoría de los casos comunes."
        )

    llm_0_pct = (llm_0 / n_s * 100) if n_s else 0
    if llm_0_pct > 20:
        recs.append(
            f"⚠  {llm_0_pct:.0f}% del total son reviews sin ningún claim regex y texto largo."
            " Estas consumen >90% del coste LLM sin contexto previo. Analizar patrones"
            " frecuentes de este grupo con --show-zero-samples."
        )

    if ss["token_savings_pct"] < 3:
        recs.append(
            "ℹ  El text_trimmer ahorra <3% de tokens. Puede que la mayoría del filler"
            " ya esté en texto corto que no llega al LLM. Revisar con muestra más grande."
        )

    if zero_hit:
        recs.append(
            f"ℹ  {len(zero_hit)} señales tienen regex en PATTERNS pero 0 hits en la muestra."
            " Pueden ser señales raras (OK) o patrones demasiado específicos (revisar)."
        )

    if len(not_in_regex) > 5:
        recs.append(
            f"ℹ  {len(not_in_regex)} señales del registry no tienen regex."
            " Las críticas (dark_sky, shower_working, etc.) deberían tenerlo"
            " para reducir la carga LLM."
        )

    for r in recs:
        print(f"\n  {r}")

    print(f"\n{sep}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_audit(sample_size: int, country: str | None, full_scan: bool) -> None:
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            logger.info("Consultando estadísticas agregadas...")
            t0 = time.monotonic()

            # 1. Agregado global (o por país)
            if country:
                row = await conn.fetchrow(AGGREGATE_COUNTRY_QUERY, country.upper())
                agg = dict(row) if row else {}
                # Rellenar campos que no están en la query de país
                agg.setdefault("already_processed", 0)
                agg.setdefault("pending_empty", 0)
                for k in ("len_micro","len_short","len_medium","len_substantial","len_long"):
                    agg.setdefault(k, 0)
                agg.setdefault("avg_len", 0)
                agg.setdefault("median_len", 0)
                agg.setdefault("p90_len", 0)
                for k in ("pend_park4night","pend_campercontact","pend_furgovw",
                          "pend_campingcarinfos","pend_freecampsites"):
                    agg.setdefault(k, 0)
            else:
                row = await conn.fetchrow(AGGREGATE_QUERY)
                agg = dict(row) if row else {}

            logger.info(f"Agregado en {time.monotonic()-t0:.1f}s")

            # 2. Muestra aleatoria para análisis local
            if full_scan:
                actual_sample = agg.get("pending_with_text", sample_size)
                logger.info(f"Full scan: leyendo {actual_sample:,} reviews...")
            else:
                actual_sample = min(sample_size, agg.get("pending_with_text", sample_size))
                logger.info(f"Leyendo muestra aleatoria de {actual_sample:,} reviews...")

            t1 = time.monotonic()
            country_join = "JOIN spots s ON s.id = r.spot_id" if country else ""
            country_filter = "AND s.country_iso = $2" if country else ""
            q = SAMPLE_QUERY.format(
                country_join=country_join,
                country_filter=country_filter,
            )
            if country:
                sample_rows = await conn.fetch(q, actual_sample, country.upper())
            else:
                sample_rows = await conn.fetch(q, actual_sample)
            logger.info(f"Muestra obtenida en {time.monotonic()-t1:.1f}s ({len(sample_rows):,} filas)")

            # 3. Sources breakdown
            source_rows = await conn.fetch(SOURCE_BREAKDOWN_QUERY)

            # 4. Spots v2 (tabla puede no tener todas las columnas — tolerante)
            try:
                spots_row = await conn.fetchrow(SPOTS_V2_QUERY)
            except Exception as e:
                logger.warning(f"spots_v2 query falló: {e}")
                spots_row = None

        # Análisis local (sin DB)
        logger.info("Analizando muestra con regex + trimmer...")
        t2 = time.monotonic()
        sample_stats = analyse_sample([dict(r) for r in sample_rows])
        logger.info(f"Análisis completado en {time.monotonic()-t2:.1f}s")

        # Sección 6 datos (spots)
        if spots_row:
            s6 = dict(spots_row)
            total_active   = s6.get("total_spots_active", 0)
            never_enriched = s6.get("spots_never_enriched", 0)
            enough_reviews = s6.get("spots_enough_reviews", 0)
            # Estimación tokens spot-level: ~8000 tokens/spot (system 2500 + reviews 5500)
            est_spot_tokens_M = enough_reviews * 8000 / 1_000_000
            print(f"\n  -- Spots sin enriquecimiento (orchestrator_v2) --")
            print(f"  Spots activos totales:           {total_active:>8,}")
            print(f"  Sin spot_semantic_state:         {never_enriched:>8,}")
            print(f"  Con ≥3 reviews (candidatos v2):  {enough_reviews:>8,}")
            print(f"  Tokens estimados (v2 batch):     {est_spot_tokens_M:.0f}M")
            for prov, price in COST_PER_1M_INPUT.items():
                cost = est_spot_tokens_M * price
                print(f"    {prov:<28} → ${cost:.0f}")

        # Imprimir informe
        print_report(agg, sample_stats, [dict(r) for r in source_rows],
                     country=country, sample_size=actual_sample)

    finally:
        await pool.close()


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auditoría de volumen LLM y cobertura regex")
    parser.add_argument("--sample", type=int, default=3000,
                        help="Tamaño de la muestra aleatoria (default 3000)")
    parser.add_argument("--country", type=str, default=None,
                        help="Filtrar por country_iso (ej: ES, FR)")
    parser.add_argument("--full-scan", action="store_true",
                        help="Analizar todas las reviews pendientes (lento para 5M rows)")
    args = parser.parse_args(argv)

    await run_audit(
        sample_size=args.sample,
        country=args.country,
        full_scan=args.full_scan,
    )
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
