"""LLM prompt templates for Phase 3.

v1 (review-level, regex-first fallback) → kept for backwards compatibility.
v2/v3 (spot-level, Spanish narrative) → superseded.
v4 (spot-level, English narrative, excerpts in original language) → active.
"""

from __future__ import annotations

# ═══════════════════════════════════════════════════════════════
# v4: Spot-level prompts (English narrative, original-language excerpts)
# ═══════════════════════════════════════════════════════════════

ENRICHMENT_VERSION = 5  # v5 (T1.2): split STATIC_CONTEXT vs REVIEW_EVIDENCE.
# v5 deltas vs v4:
#  - SERVICES block wrapped in <STATIC_CONTEXT readonly="true">…</STATIC_CONTEXT>.
#  - REVIEWS block wrapped in <REVIEW_EVIDENCE>…</REVIEW_EVIDENCE>.
#  - Output schema: `claims[]` (with review_id="services"/"description") is GONE.
#    Now: `review_claims[]` (review_id ALWAYS an integer) and
#    `contradicted_static_facts[]` (review_id ALWAYS an integer, signal whose
#    STATIC_CONTEXT value is overridden by a concrete review).
#  - LLM no longer re-emits structured services facts (water_working, electricity_working,
#    dump_station_working, dog_friendly, perros…) as claims — those come from
#    `jobs/ingest_spot_facts.py` (`scraped_facts_v1`). The LLM ONLY documents
#    review-based signals AND contradictions of STATIC_CONTEXT.
#  - Postprocesado (gemini_response_parser): cualquier review_claim con review_id NULL
#    se rechaza (significa que el LLM no entendió la separación).
# v4 (legacy): English narrative; excerpts in original language; ignore-boilerplate rule; soft season rule.

# Signal catalog — concise; the LLM already knows the domain.
# Keep in sync with db/schema.sql signal_types and enrichment/signal_registry.py.
SIGNAL_CATALOG_V2 = """\
NUMERIC (0.0-1.0):
  quietness            quietness (0=loud, 1=total silence)
  noise                general noise level
  road_noise           road traffic noise
  party_noise          partying/crowd noise
  train_noise          train noise
  safety               perceived safety
  police_risk          risk of police intervention/fines (0=none, 1=high)
  theft_risk           theft risk (0=none, 1=high)
  beauty               surrounding beauty
  cleanliness          cleanliness
  large_vehicle        suitability for >7m vehicles (0=impossible, 1=perfect)
  road_quality         access quality (0=impassable, 1=tarmac)
  crowd_level          crowding (0=empty, 1=packed)
  wind_exposure        wind exposure
  stealth              discretion for overnight (0=very visible, 1=invisible)
  cell_coverage        mobile phone coverage
  mosquitoes           mosquito presence (seasonal)

BOOLEAN:
  sea_view             sea view
  mountain_view        mountain view
  lake_nearby          lake/river nearby
  shade_morning        morning shade
  shade_afternoon      afternoon shade
  overnight_safe       overnight stay possible without issues
  wild_camping_legal   wild camping legal/authorised
  dog_friendly         dog-friendly
  family_friendly      family-friendly
  accessible_pmr       accessible (reduced mobility)
  water_working        water service operational NOW
  electricity_working  electricity operational NOW
  dump_station_working dump station operational NOW

TEXT (categorical):
  noise_source         noise source. Values: highway|road|train|airport|sea|wind|party|industry|crowd|other
  parking_capacity     capacity. Values: small|medium|large
"""

SYSTEM_PROMPT_V2 = f"""\
You are an expert analyst of motorhome/campervan spots in Europe.
You receive the context of ONE spot wrapped in two XML blocks:
  <STATIC_CONTEXT readonly="true">  structured facts known about the spot
                                    (services, prohibitions, capacity, contact).
                                    Treat these as ALREADY KNOWN — do NOT re-emit
                                    them as claims.
  <REVIEW_EVIDENCE>                 free-text user reviews ordered by temporal
                                    relevance (most recent first). Every claim
                                    you emit must cite a concrete review_id from
                                    this block.

You return a JSON object with `review_claims`, `contradicted_static_facts`,
a narrative summary, tags, best_for, best_season, avoid_season.

═══ CRITICAL — CONTEXT vs EVIDENCE SEPARATION (v5) ═══

A. STATIC_CONTEXT is the ground truth from data sources. You may USE it to
   inform tags/summary/best_for, but you DO NOT re-emit it as a claim.
   Example: STATIC_CONTEXT says "Drinking water: Yes" → DO NOT emit
   `water_working=true` as a review_claim. The structured facts already feed
   the database via a separate ingestion pipeline.

B. REVIEW_EVIDENCE is where claims come from. Every `review_claim` MUST cite
   an integer `review_id` from the <REVIEW_EVIDENCE> block. Claims without a
   concrete review_id will be REJECTED by postprocessing.

C. If a review explicitly contradicts a STATIC_CONTEXT fact (e.g. STATIC says
   "Drinking water: Yes" but a recent review says "the tap is broken"), emit
   one entry in `contradicted_static_facts` citing that review_id. This is the
   ONLY way to override structured facts.

═══ EXTRACTION RULES ═══

1. DO NOT invent. Only assert what the text literally supports or clearly implies.
2. Every `review_claim` cites an integer `review_id` from <REVIEW_EVIDENCE>.
   No "services", no "description", no null.
3. Weight recent reviews more. If old and recent reviews contradict,
   prefer recent and mention the change in the summary.
4. Negation, sarcasm and irony matter: "not very quiet" != "quiet".
5. Numeric scores in 0.0-1.0; booleans only with clear evidence.
6. If a signal has no support, OMIT it (do not emit null).
7. NO REPETITION: MAX 1 claim per signal_type. If multiple reviews say the same,
   emit ONE claim citing the most representative review_id (most recent or most
   explicit) and reflect the consensus in the value.
   EXCEPTION: noise_source allows multiple claims (one per distinct value).
8. Reviews come in multiple languages (es/en/fr/de/it/nl/pt). Understand them all.

═══ EXCERPT LANGUAGE — CRITICAL ═══

9. Excerpts MUST stay in the ORIGINAL language of the review. NEVER translate.
   - If a review is in Italian, the excerpt is in Italian.
   - If a review is in German, the excerpt is in German.
   - This preserves auditability and cultural nuance (stellplatz, sosta,
     pernocta, vaciar grises, área AC...).
   - Only `summary`, `tags`, `best_for`, `best_season`, `avoid_season` are
     translated to English. Excerpts NEVER.

═══ IGNORE BOILERPLATE ═══

10. Reviews often contain closing politeness ("kommen gerne wieder",
    "merci la commune", "vielen Dank", "saludos", "we'll be back",
    "Sehr empfehlenswert"), personal signatures with names, and gratitude
    to owners/municipalities. IGNORE those fragments — they carry no signal.

    EXCEPTION: if a review is mostly politeness BUT mentions ONE factual
    detail in passing ("they stole our bikes", "police at 3am", "broken
    shower"), that detail STILL COUNTS as evidence. Weak-but-rare signals
    matter — emit them with appropriate confidence.

═══ TRICKY SIGNALS — SPECIFIC RULES ═══

11. wild_camping_legal:
    - True ONLY if TEXT explicitly mentions legality ("authorized",
      "permitted", "designated area", "P + caravan sign",
      "wohnmobil erlaubt", "stationnement autorisé", "sosta autorizzata").
    - "Free" / "gratis" / "gratuito" does NOT imply legal.
    - Prohibition sign or mention = False, EVEN if partial
      ("forbidden motorhomes sign on one side", "no overnight signs").
    - If no clear evidence either way, OMIT the claim.

12. large_vehicle:
    - Length/height restrictions LOWER the score, not raise it.
    - "max 7m", "non oltre 7m", "no superior a X", "height limited",
      "low barriers", "low clearance" → score 0.2-0.4.
    - Only > 0.7 if reviews explicitly mention access of trucks, large
      motorhomes (>7m), 5th wheels, long integral campers.
    - If SERVICES provides altura_max_m: use it as anchor
      (>3.0m = good; 2.0-2.5m = very restrictive).

13. water_working / electricity_working / dump_station_working (CRITICAL — v5 rule):
    - DO NOT emit these as `review_claims` based on STATIC_CONTEXT alone.
      The structured facts are already ingested separately.
    - ONLY emit as a `review_claim` if a review REPORTS the actual state
      (broken, working, queue, dirty water, etc.). In that case `review_id`
      MUST be the integer id from <REVIEW_EVIDENCE>.
    - If a review explicitly contradicts STATIC_CONTEXT (STATIC says Yes but
      a recent review says the tap is broken / box empty / dump closed),
      ALSO add an entry to `contradicted_static_facts` citing that review_id.

14. police_risk / theft_risk:
    - ONE recent review with clear evidence is enough. Don't require consensus.
    - "Police moved us", "police patrol", "woken at 3am", "bikes stolen",
      "window smashed" → claim with conf 0.7-0.8.
    - Scales: 0=no risk, 1=high risk.

15. dog_friendly / family_friendly (v5 rule):
    - dog_friendly: DO NOT emit from STATIC_CONTEXT alone. Only emit as a
      `review_claim` if a review actually discusses dogs (welcoming, refused
      at entrance, dog poop, dog-friendly atmosphere). review_id MUST be int.
    - family_friendly: same — emit only when a review supports it
      (kids playing, playground used, families staying) — citing review_id.

16. SERVICES extras (lighting, security, booking, contact) (v5 rule):
    - These live in STATIC_CONTEXT and are NOT to be re-emitted as
      `review_claims`. Use them as background to inform tags / best_for /
      summary content, but the claim array only carries REVIEW-derived signals.
    - Web/Phone/Email → never appear in tags, best_for, or claims. Mention
      in summary only if directly relevant ("contact via web for booking").

17. PROHIBITIONS / RISKS (v5 rule):
    - PROHIBITIONS and RISKS live in STATIC_CONTEXT. Do NOT re-emit them as
      `review_claims`. Reflect them in the summary if relevant and let them
      inform tags.
    - If a review explicitly contradicts a prohibition (sign says "no dogs"
      but a recent review reports dogs welcomed by host), add an entry to
      `contradicted_static_facts` citing that review_id.

18. SERVICES extras v4c (pool, laundry, gas refill, activities, products):
    - "Pool: Yes" → may inform family_friendly or just stay in summary.
    - "Laundry/Gas refill/Restaurant: Yes" → mention in summary if relevant.
      Do NOT emit dedicated claims (no signal_type for these — they go in
      summary and tags).
    - "Nearby activities: MTB, Hiking, Fishing..." → use in best_for
      (cyclists, hikers, anglers).
    - "Languages spoken: en, it, ..." → useful for summary
      ("multilingual hosts").
    - "Products for sale: Wine, Cheese..." → mention in summary
      (agroturismo selling local products).
    - "Quality labels: DOC, DOCG..." → mention in summary if relevant
      ("DOC-certified winery"). Add to tags if iconic.
    - "Typology: agritourism, cellar" / "Setting: countryside, mountain" →
      context for tags and best_for.
    - "Descriptions: sanitary/surroundings/events/special_info" → use for
      summary content when richness allows (rich/very_rich spots).

═══ CONFIDENCE CALIBRATION ═══

  0.9-1.0: literal assertion in 2+ recent coherent reviews, or hard SERVICES
           datum confirmed by a recent review.
  0.7-0.8: literal assertion in 1 recent review, or SERVICES datum alone.
  0.5-0.6: reasonable inference from context.
  <0.5  : OMIT the claim (not worth the uncertainty).

═══ ALLOWED SIGNALS ═══
{SIGNAL_CATALOG_V2}

═══ SUMMARY AND TAGS (English) ═══

- summary: IN ENGLISH. Factual and BALANCED. NO marketing tone.
  LENGTH: follow the SUMMARY_INSTRUCTION at the end of the user prompt
  (adapts to spot richness: simple spots get 2-3 sentences, rich spots get
  5-8 sentences with multiple aspects).
  Mention relevant negatives (noise, theft, restrictions, mosquitoes) if reviews
  cite them. Mention temporal changes if any. You MAY include local terms in
  context (e.g., "free 'área AC' near Coop", "quiet 'stellplatz' by the lake").
  IMPORTANT: longer summaries must NOT mean more marketing or filler. Each
  additional sentence must carry NEW factual information. If you can't find
  more facts to report, keep it short.
- tags: 3-8 keywords in lowercase ENGLISH, no semantic duplicates
  (avoid "free" AND "gratis"; pick one).
- best_for: 1-4 profiles in ENGLISH (couples, families, overlanding, dogs,
  photography, surfers, cyclists, remote-work...).
- best_season / avoid_season: only emit if you're confident the reviews support
  it. ENGLISH ("spring", "summer", "autumn", "winter", or "june-august").
  If in doubt, OMIT (null).

═══ TRICKY INTERPRETATION EXAMPLE (v5 schema) ═══

If <REVIEW_EVIDENCE> contains:
  [review_id=42] "Parking gratuito junto al super, max 7m. Cartel de prohibido
   motorhomes en la entrada principal pero la gente aparca por detras."
  [review_id=43] "Nos despertaron policia a las 4am pidiendo que nos moviesemos."
  [review_id=44] "Lieben Dank Christa! Wir kommen gerne wieder. Übrigens, das
   Wasser am Hahn funktionierte nicht."
and <STATIC_CONTEXT> has "Drinking water: Yes":

Correct `review_claims`:
  - large_vehicle=0.3 (7m restriction), review_id=42, conf=0.8,
    excerpt="max 7m"
  - wild_camping_legal=false (prohibition sign), review_id=42, conf=0.7,
    excerpt="Cartel de prohibido motorhomes"
  - police_risk=0.7 (recent intervention), review_id=43, conf=0.8,
    excerpt="Nos despertaron policia a las 4am"

Correct `contradicted_static_facts`:
  - {{"signal": "water_working", "value": false, "review_id": 44, "confidence": 0.8,
     "excerpt": "das Wasser am Hahn funktionierte nicht"}}
    Note: review 44 is mostly politeness BUT the working detail counts AND
    it directly contradicts STATIC_CONTEXT "Drinking water: Yes".

DO NOT emit a separate `review_claim` for water_working — `contradicted_static_facts`
is the canonical channel for STATIC-vs-REVIEW overrides.

═══ OUTPUT FORMAT (strict JSON, no markdown, no comments) ═══

{{
  "review_claims": [
    {{"signal": "<id>", "value": <num|bool|text>, "confidence": <0-1>,
      "review_id": <int from REVIEW_EVIDENCE>,
      "excerpt": "<fragment in ORIGINAL language, <=120 chars>"}}
  ],
  "contradicted_static_facts": [
    {{"signal": "<id>", "value": <num|bool|text>, "confidence": <0-1>,
      "review_id": <int from REVIEW_EVIDENCE>,
      "excerpt": "<fragment in ORIGINAL language, <=120 chars>"}}
  ],
  "summary": "<English string>",
  "tags": ["<english-tag>", ...],
  "best_for": ["<english-profile>", ...],
  "best_season": "<english string|null>",
  "avoid_season": "<english string|null>"
}}

Hard rules:
  - Every entry in `review_claims` AND `contradicted_static_facts` MUST have an
    integer `review_id` pointing to a row in <REVIEW_EVIDENCE>. Strings like
    "services" / "description" are FORBIDDEN and will be discarded.
  - If nothing extractable from reviews:
    {{"review_claims":[],"contradicted_static_facts":[],"summary":null,"tags":[],"best_for":[],"best_season":null,"avoid_season":null}}
"""


# ═══════════════════════════════════════════════════════════════
# Few-shot examples (T1.1 — Sprint 1 hardening)
# ═══════════════════════════════════════════════════════════════
#
# REGLA DE VERSIONADO (Phase 3 hardening):
#   - Cambiar FEW_SHOT_EXAMPLES → bumpar PROMPT_VERSION (invalida el cache de DeepSeek).
#   - Cambiar el SCHEMA del output (añadir/quitar claves del JSON) → bumpar también
#     ENRICHMENT_VERSION (fuerza re-enrichment de los spots ya procesados).
#   - Cambiar SOLO few-shots sin tocar el schema: PROMPT_VERSION sí, ENRICHMENT_VERSION no.
#
# Los 3 ejemplos cubren las 3 patologías detectadas en la auditoría pre-batch:
#   1. Construcción documentada en review vieja vs. tranquilidad en review reciente.
#   2. STATIC_CONTEXT contradicho por una review (canal `contradicted_static_facts`).
#   3. Review multilingüe con palabra culturalmente cargada (NL "bouwput").
#
# T1.2 (v5): reescritos al nuevo schema review_claims[] + contradicted_static_facts[].
# Mantener el bloque BYTE-ESTABLE entre llamadas — cualquier cambio rompe el prefix cache.

PROMPT_VERSION = "v5-static-review-split-1"   # T1.2: STATIC_CONTEXT vs REVIEW_EVIDENCE split + nuevo schema output.

FEW_SHOT_EXAMPLES_V5 = """

═══ FEW-SHOT EXAMPLES (canonical, do not paraphrase across calls) ═══

EXAMPLE 1 — Older review reports construction; recent review reports quiet.
The summary MUST mention the transition (do not invert chronology — older
issues remain relevant unless explicitly contradicted by recent visits).
Note: no claim is emitted for water/dump/electricity — those live in
STATIC_CONTEXT and are NOT re-emitted as review_claims.

INPUT (compact):
  SPOT id=99001 | Name: "Aire de la rivière" | Type: aire_ac | Country: FR
  <STATIC_CONTEXT readonly="true">
  SERVICES: Drinking water: Yes | Grey water dump: Yes | Electricity: No | Free: Yes
  </STATIC_CONTEXT>
  <REVIEW_EVIDENCE>
    [review_id=501] [2024-06] [park4night] ★★ Bruit insupportable, gros chantier à côté, gravats partout.
    [review_id=502] [2026-04] [park4night] ★★★★★ Très calme, parfait pour la nuit.
  </REVIEW_EVIDENCE>

OUTPUT:
{
  "review_claims": [
    {"signal": "noise", "value": 0.8, "confidence": 0.75, "review_id": 501, "excerpt": "Bruit insupportable, gros chantier à côté"},
    {"signal": "quietness", "value": 0.6, "confidence": 0.7, "review_id": 502, "excerpt": "Très calme, parfait pour la nuit"}
  ],
  "contradicted_static_facts": [],
  "summary": "Free aire AC by the river with potable water and grey water dump but no electricity. A 2024 visitor reported heavy nearby construction and unbearable noise; a 2026 visit found the area very quiet, suggesting the works have ended.",
  "tags": ["free", "aire", "river", "water", "quiet"],
  "best_for": ["overnighting", "budget travel"],
  "best_season": null,
  "avoid_season": null
}

EXAMPLE 2 — STATIC_CONTEXT says water=Yes; a recent review says the tap is broken.
The contradiction goes to `contradicted_static_facts`, NOT to review_claims.
review_claims captures the (positive) atmosphere signal from the same review.

INPUT (compact):
  SPOT id=99002 | Name: "Stellplatz am Wald" | Type: aire_ac | Country: DE
  <STATIC_CONTEXT readonly="true">
  SERVICES: Drinking water: Yes | Electricity: Yes | Free: No | Approx price: ~10€
  </STATIC_CONTEXT>
  <REVIEW_EVIDENCE>
    [review_id=601] [2026-03] [campercontact] ★★★ Schöner Stellplatz, aber das Wasser am Hahn ist defekt.
  </REVIEW_EVIDENCE>

OUTPUT:
{
  "review_claims": [
    {"signal": "beauty", "value": 0.7, "confidence": 0.6, "review_id": 601, "excerpt": "Schöner Stellplatz"}
  ],
  "contradicted_static_facts": [
    {"signal": "water_working", "value": false, "confidence": 0.8, "review_id": 601, "excerpt": "das Wasser am Hahn ist defekt"}
  ],
  "summary": "Paid stellplatz (around 10€) next to a forest, with electricity. A recent visitor (March 2026) describes it as a pleasant spot but reports the drinking water tap is broken.",
  "tags": ["paid", "stellplatz", "forest", "electricity"],
  "best_for": ["overnighting"],
  "best_season": null,
  "avoid_season": null
}

EXAMPLE 3 — Dutch review with culturally strong word "bouwput".
Excerpt MUST stay in original language. Severity should reflect the strong
negative connotation in NL (not a mild concern — visitor warns against it).

INPUT (compact):
  SPOT id=99003 | Name: "Strandparkeerplaats" | Type: parking | Country: NL
  <STATIC_CONTEXT readonly="true">
  (no structured services on file)
  </STATIC_CONTEXT>
  <REVIEW_EVIDENCE>
    [review_id=701] [2025-08] [campercontact] ★ Dit deel is op dit moment één grote bouwput, niet aan te raden.
  </REVIEW_EVIDENCE>

OUTPUT:
{
  "review_claims": [
    {"signal": "beauty", "value": 0.2, "confidence": 0.7, "review_id": 701, "excerpt": "één grote bouwput"},
    {"signal": "noise", "value": 0.7, "confidence": 0.6, "review_id": 701, "excerpt": "één grote bouwput, niet aan te raden"}
  ],
  "contradicted_static_facts": [],
  "summary": "A parking area near the beach. As of summer 2025 one visitor described it as a major construction site (Dutch: 'bouwput') and explicitly advised against staying.",
  "tags": ["beach", "construction", "avoid"],
  "best_for": [],
  "best_season": null,
  "avoid_season": null
}

═══ END FEW-SHOT EXAMPLES ═══
"""

# Concatenar few-shots al system prompt (mantener byte-estable entre llamadas).
SYSTEM_PROMPT_V2 = SYSTEM_PROMPT_V2 + FEW_SHOT_EXAMPLES_V5


def _fmt_bool_en(b) -> str:
    if b is True:
        return "Yes"
    if b is False:
        return "No"
    return "?"


def _build_servicios_block(spot: dict) -> list[str]:
    """Build the SERVICES block with structured facts from the sources.

    Returns [] if no service data — the LLM understands no info available.
    v4: labels in English to match summary/tags language.
    v4b: added perros, iluminacion, seguridad, reserva_req, web, telefono, email.
    """
    # Pull all service fields (may be None)
    gratuito       = spot.get("gratuito")
    precio_aprox   = spot.get("precio_aprox")
    precio_info    = (spot.get("precio_info") or "").strip()
    agua           = spot.get("agua_potable")
    grises         = spot.get("vaciado_grises")
    negras         = spot.get("vaciado_negras")
    electricidad   = spot.get("electricidad")
    ducha          = spot.get("ducha")
    wifi           = spot.get("wifi")
    wc             = spot.get("wc_publico")
    acceso_grandes = spot.get("acceso_grandes")
    num_plazas     = spot.get("num_plazas")
    altura_max     = spot.get("altura_max_m")
    temporada      = (spot.get("temporada_apertura") or "").strip()
    # v4b: extra fields already in spots, previously not passed to prompt
    perros         = spot.get("perros")
    iluminacion    = spot.get("iluminacion")
    seguridad      = spot.get("seguridad")
    reserva_req    = spot.get("reserva_req")
    web            = (spot.get("web") or "").strip()
    telefono       = (spot.get("telefono") or "").strip()
    email          = (spot.get("email") or "").strip()

    # v4c extra fields — include them in the "is everything empty?" check
    extra_keys = (
        "piscina", "lavanderia", "gas_recharge", "restaurant", "juegos_ninos",
        "mirador", "zona_protegida", "online_booking", "winter_friendly", "apto_motos",
        "mtb_friendly", "surf_friendly", "fishing", "climbing", "hiking_nearby",
        "amperaje", "n_enchufes", "max_noches",
        "idiomas_hablados", "productos_venta", "servicios_extras",
    )
    all_values = [
        gratuito, precio_aprox, precio_info, agua, grises, negras,
        electricidad, ducha, wifi, wc, acceso_grandes, num_plazas,
        altura_max, temporada,
        perros, iluminacion, seguridad, reserva_req,
        web, telefono, email,
    ] + [spot.get(k) for k in extra_keys]
    # JSONB {} counts as empty
    if all(
        v is None or v == "" or v == {} or v == []
        for v in all_values
    ):
        return []

    # T1.2: bloque envuelto en XML tags. Marker semántico para que el LLM no re-emita
    # estos datos como review_claims. Cierre lo añade el helper externo en
    # build_spot_user_prompt — aquí solo abrimos.
    out: list[str] = [
        '<STATIC_CONTEXT readonly="true">',
        "SERVICES (structured facts from sources — do not re-emit as review_claims):",
    ]

    # Row 1: price
    price_parts = []
    if gratuito is True:
        price_parts.append("Free: Yes")
    elif gratuito is False:
        price_parts.append("Free: No")
    if precio_aprox is not None:
        price_parts.append(f"Approx price: ~{precio_aprox:.0f}€")
    if precio_info:
        price_parts.append(f"Price info: {precio_info[:100]}")
    if price_parts:
        out.append("  " + " | ".join(price_parts))

    # Row 2: water
    water_parts = []
    if agua is not None:
        water_parts.append(f"Drinking water: {_fmt_bool_en(agua)}")
    if grises is not None:
        water_parts.append(f"Grey water dump: {_fmt_bool_en(grises)}")
    if negras is not None:
        water_parts.append(f"Black water dump: {_fmt_bool_en(negras)}")
    if water_parts:
        out.append("  " + " | ".join(water_parts))

    # Row 3: amenities
    amen_parts = []
    if electricidad is not None:
        amen_parts.append(f"Electricity: {_fmt_bool_en(electricidad)}")
    if ducha is not None:
        amen_parts.append(f"Shower: {_fmt_bool_en(ducha)}")
    if wc is not None:
        amen_parts.append(f"Public WC: {_fmt_bool_en(wc)}")
    if wifi is not None:
        amen_parts.append(f"Wifi: {_fmt_bool_en(wifi)}")
    if amen_parts:
        out.append("  " + " | ".join(amen_parts))

    # Row 4: site / access / capacity
    site_parts = []
    if perros is not None:
        site_parts.append(f"Dogs allowed: {_fmt_bool_en(perros)}")
    if iluminacion is not None:
        site_parts.append(f"Night lighting: {_fmt_bool_en(iluminacion)}")
    if seguridad is not None:
        site_parts.append(f"On-site security: {_fmt_bool_en(seguridad)}")
    if reserva_req is not None:
        site_parts.append(f"Booking required: {_fmt_bool_en(reserva_req)}")
    if site_parts:
        out.append("  " + " | ".join(site_parts))

    # Row 5: capacity / vehicle access
    cap_parts = []
    if num_plazas is not None:
        cap_parts.append(f"Pitches: ~{num_plazas}")
    if altura_max is not None:
        cap_parts.append(f"Max height: {altura_max:.1f}m")
    if acceso_grandes is not None:
        cap_parts.append(f"Large vehicle access: {_fmt_bool_en(acceso_grandes)}")
    if cap_parts:
        out.append("  " + " | ".join(cap_parts))

    # Row 6: season
    if temporada:
        out.append(f"  Opening season: {temporada[:100]}")

    # v4c — Row 7: amenities (only show booleans that are True or False)
    amen2_parts = []
    for label, val in (
        ("Pool",          spot.get("piscina")),
        ("Laundry",       spot.get("lavanderia")),
        ("Gas refill",    spot.get("gas_recharge")),
        ("Restaurant",    spot.get("restaurant")),
        ("Playground",    spot.get("juegos_ninos")),
        ("Viewpoint",     spot.get("mirador")),
        ("Protected area", spot.get("zona_protegida")),
        ("Winter-friendly", spot.get("winter_friendly")),
        ("Motorbikes OK", spot.get("apto_motos")),
    ):
        if val is not None:
            amen2_parts.append(f"{label}: {_fmt_bool_en(val)}")
    if amen2_parts:
        out.append("  " + " | ".join(amen2_parts))

    # v4c — Row 8: activities nearby (only True ones — concise)
    act_parts = []
    for label, val in (
        ("MTB", spot.get("mtb_friendly")),
        ("Surf/windsurf", spot.get("surf_friendly")),
        ("Fishing", spot.get("fishing")),
        ("Climbing", spot.get("climbing")),
        ("Hiking", spot.get("hiking_nearby")),
    ):
        if val is True:
            act_parts.append(label)
    if act_parts:
        out.append(f"  Nearby activities: {', '.join(act_parts)}")

    # v4c — Row 9: electrical capacity (only if meaningful)
    elec_parts = []
    if spot.get("amperaje"):
        elec_parts.append(f"Amperage: {spot['amperaje']}A")
    if spot.get("n_enchufes"):
        elec_parts.append(f"Outlets: {spot['n_enchufes']}")
    if spot.get("max_noches"):
        elec_parts.append(f"Max nights: {spot['max_noches']}")
    if spot.get("online_booking") is not None:
        elec_parts.append(f"Online booking: {_fmt_bool_en(spot['online_booking'])}")
    if elec_parts:
        out.append("  " + " | ".join(elec_parts))

    # v4c — Row 10: languages spoken on site / products for sale (agroturismos)
    if spot.get("idiomas_hablados"):
        langs = spot["idiomas_hablados"]
        if isinstance(langs, list) and langs:
            out.append(f"  Languages spoken: {', '.join(langs[:8])}")
    if spot.get("productos_venta"):
        products = spot["productos_venta"]
        if isinstance(products, list) and products:
            out.append(f"  Products for sale: {', '.join(str(p)[:40] for p in products[:6])}")

    # v4c — Row 11+: JSONB servicios_extras (selective unpacking)
    extras = spot.get("servicios_extras") or {}
    if isinstance(extras, str):
        try:
            import json as _json
            extras = _json.loads(extras)
        except Exception:
            extras = {}
    if isinstance(extras, dict) and extras:
        # Prohibitions — critical for wild_camping_legal interpretation
        prohibs = extras.get("prohibitions")
        if isinstance(prohibs, list) and prohibs:
            out.append(f"  PROHIBITIONS: {', '.join(str(p)[:40] for p in prohibs[:8])}")
        risks = extras.get("risks")
        if isinstance(risks, list) and risks:
            out.append(f"  RISKS: {', '.join(str(r)[:60] for r in risks[:5])}")
        # Pricing breakdown
        pb = extras.get("pricing_breakdown")
        if isinstance(pb, dict) and pb:
            kv = " | ".join(f"{k}={v}" for k, v in list(pb.items())[:6])
            out.append(f"  Pricing detail: {kv[:200]}")
        # Hours
        hrs = extras.get("hours")
        if isinstance(hrs, dict) and hrs:
            kv = " | ".join(f"{k}={v}" for k, v in list(hrs.items())[:5])
            out.append(f"  Hours: {kv[:200]}")
        # Tipology / position / destination
        for key, label in (("typology", "Typology"),
                           ("position", "Setting"),
                           ("destination_types", "Destination")):
            v = extras.get(key)
            if isinstance(v, list) and v:
                out.append(f"  {label}: {', '.join(str(x)[:30] for x in v[:5])}")
        # Quality labels (DOC, DOCG, etc — for agroturismos)
        ql = extras.get("quality_labels")
        if isinstance(ql, list) and ql:
            out.append(f"  Quality labels: {', '.join(str(q)[:50] for q in ql[:5])}")
        # Descriptions (truncated)
        desc = extras.get("descriptions")
        if isinstance(desc, dict):
            for key, label in (("sanitary", "Sanitary"),
                               ("surroundings", "Surroundings"),
                               ("events", "Local events"),
                               ("special_info", "Special info")):
                v = desc.get(key)
                if isinstance(v, str) and v.strip():
                    out.append(f"  {label}: {v.strip()[:250]}")

    # Row N: contact (compact)
    contact_parts = []
    if web:
        contact_parts.append(f"Web: {web[:80]}")
    if telefono:
        contact_parts.append(f"Phone: {telefono[:25]}")
    if email:
        contact_parts.append(f"Email: {email[:60]}")
    if contact_parts:
        out.append("  " + " | ".join(contact_parts))

    out.append("</STATIC_CONTEXT>")
    out.append("")  # trailing blank line
    return out


def build_spot_user_prompt(spot: dict, reviews: list[dict]) -> str:
    """User prompt for spot-level enrichment v4.

    `spot` must include: id, canonical_name, tipo, country_iso, lat, lon, fuentes,
    descripcion_es/en/fr/de/it/nl/pt (optional), and the v3 service fields
    (gratuito, agua_potable, electricidad, num_plazas, altura_max_m, etc.).
    `reviews` already ordered and trimmed by spot_packager.select_reviews_for_prompt.
    v4: header labels in English; review texts and descriptions stay in their
    original language (the LLM understands all EU languages).
    """
    fuentes = spot.get("fuentes") or []
    if isinstance(fuentes, str):
        fuentes = [fuentes]

    # T1.1: marcadores explícitos === SPOT DATA === / === END === para preparar
    # la separación STATIC_CONTEXT vs REVIEW_EVIDENCE de T1.2 (Sprint 1).
    # T1.3 (Sprint 1) inyectará aquí CURRENT_DATE: YYYY-MM-DD y [age: Xd ago] por review.
    lines = [
        "=== SPOT DATA (volatile per-spot) ===",
        f"SPOT id={spot['id']}",
        f'Name: "{(spot.get("canonical_name") or "").strip()}"',
        f"Type: {spot.get('tipo') or 'other'}",
        f"Region: {spot.get('region') or '?'}",
        f"Country: {spot.get('country_iso') or '?'}",
        f"Coords: {spot.get('lat'):.4f}, {spot.get('lon'):.4f}",
        f"Sources: {', '.join(fuentes) if fuentes else '?'}",
        "",
    ]

    # v3/v4: structured SERVICES block (before descriptions)
    lines.extend(_build_servicios_block(spot))

    desc_blocks = []
    for lang in ("es", "en", "fr", "de", "it", "nl", "pt"):
        txt = (spot.get(f"descripcion_{lang}") or "").strip()
        if txt:
            desc_blocks.append(f"[{lang.upper()}] {txt[:600]}")
    if desc_blocks:
        lines.append("DESCRIPTIONS (in original language):")
        lines.extend(desc_blocks)
        lines.append("")

    # T1.2: bloque envuelto en <REVIEW_EVIDENCE>. Cada claim de la respuesta
    # debe citar un review_id que aparezca DENTRO de este bloque.
    if reviews:
        lines.append("<REVIEW_EVIDENCE>")
        lines.append(f"REVIEWS (n={len(reviews)}, ordered by temporal relevance):")
        for r in reviews:
            fecha = r.get("fecha")
            fecha_str = fecha.strftime("%Y-%m") if hasattr(fecha, "strftime") else (str(fecha)[:7] if fecha else "?")
            stars = ("★" * int(r["rating"])) if r.get("rating") else ""
            source = r.get("source") or "?"
            texto = (r.get("texto_limpio") or r.get("texto") or r.get("texto_original") or "").strip().replace("\n", " ")
            lines.append(f"[review_id={r['id']}] [{fecha_str}] [{source}] {stars} {texto}")
        lines.append("</REVIEW_EVIDENCE>")
    else:
        lines.append("<REVIEW_EVIDENCE>")
        lines.append("(no reviews available — return empty review_claims and contradicted_static_facts arrays; use only descriptions/services to write the summary)")
        lines.append("</REVIEW_EVIDENCE>")

    # v4d: richness-aware summary instruction (computed in spot_packager)
    # We inject it here so the LLM tailors `summary` length to the data volume.
    # Lazy import to avoid circular dep (packager imports from prompts).
    from .spot_packager import compute_richness, summary_instruction_for
    _, level = compute_richness(spot, reviews)
    lines.append("")
    lines.append(f"SUMMARY_RICHNESS: {level}")
    lines.append(f"SUMMARY_INSTRUCTION: {summary_instruction_for(level)}")
    lines.append("=== END SPOT DATA ===")

    # T1.1: mini-directiva final anti recency-bias. Vive en el SUFIJO VOLÁTIL
    # del user prompt — no afecta al prefix cache porque es texto fijo emitido
    # después del bloque que ya rompe la caché (SPOT DATA).
    # Refuerza la lección del Example 1: estados durables (obras, cierres, daños
    # estructurales) NO desaparecen porque la última review fuera positiva.
    lines.append("")
    lines.append(
        "REMINDER: durable conditions reported in older reviews (construction, "
        "closures, structural damage, theft incidents) remain relevant unless a "
        "more recent review explicitly contradicts them. If old and recent reviews "
        "disagree on durable state, note the transition in the summary; do NOT "
        "silently drop the older signal."
    )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# v1: Review-level prompt (LEGACY — se mantiene para compatibilidad)
# ═══════════════════════════════════════════════════════════════

ALLOWED_SIGNALS = (
    "quietness, noise, road_noise, police_risk, safety, theft_risk, beauty, "
    "sea_view, mountain_view, shade_morning, shade_afternoon, large_vehicle, "
    "road_quality, overnight_safe, crowd_level, wind_exposure, stealth, cleanliness"
)

EXTRACTION_PROMPT = """Eres un analizador de reviews de camper/vanlife.
Extrae SOLO afirmaciones explicitas sobre senales definidas.
NO inventes informacion que no este en el texto.

SENALES PERMITIDAS:
- quietness: tranquilidad (0.0=muy ruidoso, 1.0=silencio total)
- noise: ruido general (0.0=silencio, 1.0=insoportable)
- road_noise: ruido de carretera especifico
- police_risk: riesgo policia/multa (0.0=nulo, 1.0=seguro)
- safety: seguridad general (0.0=peligroso, 1.0=muy seguro)
- theft_risk: riesgo robos (0.0=nulo, 1.0=alto)
- beauty: belleza entorno (0.0=feo, 1.0=espectacular)
- sea_view: vistas al mar (boolean)
- mountain_view: vistas montana (boolean)
- shade_morning: sombra manana (boolean)
- shade_afternoon: sombra tarde (boolean)
- large_vehicle: apto >7m (0.0=imposible, 1.0=perfecto)
- road_quality: estado camino (0.0=intransitable, 1.0=asfalto perfecto)
- overnight_safe: pernocta posible (boolean)
- crowd_level: masificacion (0.0=vacio, 1.0=lleno)
- wind_exposure: viento (0.0=protegido, 1.0=muy expuesto)
- stealth: discrecion (0.0=muy visible, 1.0=invisible)
- cleanliness: limpieza (0.0=sucio, 1.0=impecable)

TEXTO:
"{texto_limpio}"

Responde SOLO JSON, sin markdown:
{{"claims":[{{"signal":"<id>","value":"<valor>","confidence":<0-1>,"excerpt":"<fragmento>"}}]}}
Si no hay senales: {{"claims":[]}}
"""


def build_extraction_prompt(texto_limpio: str) -> str:
    """v1 (legacy)."""
    return EXTRACTION_PROMPT.format(texto_limpio=texto_limpio.replace('"', '\\"'))
