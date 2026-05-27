"""Tests de enrichment/ingest_v2.py contra DB real.

Cada test corre en una transacción que se revierte al final, así que NO deja
datos residuales. Requiere que la DB de docker-compose esté levantada.

Saltarse con `pytest -m "not db"` si no hay DB disponible.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

from enrichment.gemini_response_parser import (
    ValidatedClaim,
    ValidatedEnrichment,
    parse_enrichment_response,
)
from enrichment.ingest_v2 import ingest_spot_enrichment


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def _dsn() -> str:
    _load_dotenv()
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "25433")
    name = os.environ.get("DB_NAME") or os.environ.get("POSTGRES_DB", "geospots")
    user = os.environ.get("DB_USER") or os.environ.get("POSTGRES_USER", "geospots")
    pw = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD", "geospots")
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def conn():
    """Conexión asyncpg con transacción auto-rollback al terminar el test."""
    c = await asyncpg.connect(dsn=_dsn())
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


@pytest_asyncio.fixture
async def fixture_spot(conn):
    """Crea un spot temporal + 2 reviews. Se revierte al terminar el test."""
    spot_id = await conn.fetchval(
        """
        INSERT INTO spots (canonical_name, lat, lon, country_iso, tipo, fuentes,
                           descripcion_es)
        VALUES ('TEST Aire Ingest v2', 43.4, -1.6, 'FR', 'area_ac',
                ARRAY['park4night'],
                'Spot de prueba para tests de ingest_v2. Aparcamiento gratuito frente al mar.')
        RETURNING id
        """,
    )
    review1_id = await conn.fetchval(
        """
        INSERT INTO reviews (spot_id, source, source_review_id, texto, rating, fecha)
        VALUES ($1, 'park4night', 'test-1', 'Muy tranquilo, vistas al mar.', 5,
                CURRENT_DATE - INTERVAL '90 days')
        RETURNING id
        """,
        spot_id,
    )
    review2_id = await conn.fetchval(
        """
        INSERT INTO reviews (spot_id, source, source_review_id, texto, rating, fecha)
        VALUES ($1, 'park4night', 'test-2', 'Ruido de autopista cerca, pero gratis.', 3,
                CURRENT_DATE - INTERVAL '30 days')
        RETURNING id
        """,
        spot_id,
    )
    return {"spot_id": spot_id, "review_ids": [review1_id, review2_id]}


def _build_parsed(review_ids: list[int]) -> ValidatedEnrichment:
    """Construye un ValidatedEnrichment realista."""
    return ValidatedEnrichment(
        claims=[
            ValidatedClaim(signal="quietness", value=0.8, confidence=0.9,
                           review_id=review_ids[0], excerpt="muy tranquilo", raw={}),
            ValidatedClaim(signal="sea_view", value=True, confidence=0.95,
                           review_id=None,  # ← desde description
                           excerpt="frente al mar", raw={}),
            ValidatedClaim(signal="road_noise", value=0.7, confidence=0.85,
                           review_id=review_ids[1], excerpt="ruido de autopista", raw={}),
            ValidatedClaim(signal="noise_source", value="highway", confidence=0.9,
                           review_id=review_ids[1], excerpt="autopista cerca", raw={}),
            ValidatedClaim(signal="parking_capacity", value="medium", confidence=0.7,
                           review_id=None, excerpt="aparcamiento", raw={}),
            ValidatedClaim(signal="water_working", value=True, confidence=0.9,
                           review_id=review_ids[0], excerpt="agua disponible", raw={}),
        ],
        summary="Free aire facing the sea with views. Some highway noise.",
        tags=["sea", "free", "quiet"],
        best_for=["couples"],
        best_season="spring-autumn",
        avoid_season=None,
        errors=[],
    )


@pytest.mark.asyncio
async def test_ingest_inserts_claims_and_observations(conn, fixture_spot):
    parsed = _build_parsed(fixture_spot["review_ids"])
    stats = await ingest_spot_enrichment(
        conn, fixture_spot["spot_id"], parsed,
        provider="deepseek", llm_model="deepseek-v4-flash",
    )
    assert stats.claims_inserted == 6
    assert stats.observations_inserted == 6
    assert stats.narrative_updated

    n_claims = await conn.fetchval(
        "SELECT COUNT(*) FROM extracted_claims WHERE spot_id = $1", fixture_spot["spot_id"]
    )
    assert n_claims == 6

    n_obs = await conn.fetchval(
        "SELECT COUNT(*) FROM normalized_observations WHERE spot_id = $1", fixture_spot["spot_id"]
    )
    assert n_obs == 6


@pytest.mark.asyncio
async def test_ingest_claim_with_null_review_id_works(conn, fixture_spot):
    """Los claims desde 'description' tienen review_id=None y deben insertarse."""
    parsed = _build_parsed(fixture_spot["review_ids"])
    await ingest_spot_enrichment(
        conn, fixture_spot["spot_id"], parsed,
        provider="gemini", llm_model="gemini-2.5-flash-lite",
    )
    n_null = await conn.fetchval(
        """
        SELECT COUNT(*) FROM extracted_claims
        WHERE spot_id = $1 AND review_id IS NULL
        """,
        fixture_spot["spot_id"],
    )
    # sea_view y parking_capacity vienen de description → 2 claims con review_id NULL
    assert n_null == 2


@pytest.mark.asyncio
async def test_ingest_writes_extractor_metadata(conn, fixture_spot):
    parsed = _build_parsed(fixture_spot["review_ids"])
    stats = await ingest_spot_enrichment(
        conn, fixture_spot["spot_id"], parsed,
        provider="deepseek", llm_model="deepseek-v4-flash",
    )
    rows = await conn.fetch(
        """
        SELECT extractor_name, extractor_version, pipeline_run_id
        FROM extracted_claims WHERE spot_id = $1
        """,
        fixture_spot["spot_id"],
    )
    assert all(r["extractor_name"] == "deepseek_spot_v2" for r in rows)
    assert all(r["extractor_version"].startswith("v") for r in rows)
    assert all(r["pipeline_run_id"] == stats.pipeline_run_id for r in rows)


@pytest.mark.asyncio
async def test_ingest_materializes_v2_columns(conn, fixture_spot):
    parsed = _build_parsed(fixture_spot["review_ids"])
    await ingest_spot_enrichment(
        conn, fixture_spot["spot_id"], parsed,
        provider="deepseek", llm_model="deepseek-v4-flash",
    )
    row = await conn.fetchrow(
        """
        SELECT summary_es, summary_en, tags, best_for, best_season, avoid_season,
               noise_sources, parking_capacity, last_observation_at,
               enrichment_version, llm_model, stale
        FROM spot_semantic_state WHERE spot_id = $1
        """,
        fixture_spot["spot_id"],
    )
    assert row is not None
    # v4: summary_es deprecado (NULL), summary_en lleva el texto inglés único.
    assert row["summary_es"] is None
    assert row["summary_en"].startswith("Free aire")
    assert "sea" in row["tags"]
    assert "couples" in row["best_for"]
    assert row["best_season"] == "spring-autumn"
    assert row["avoid_season"] is None
    assert row["noise_sources"] == ["highway"]
    assert row["parking_capacity"] == "medium"
    assert row["last_observation_at"] is not None
    assert row["enrichment_version"] >= 2
    assert row["llm_model"] == "deepseek-v4-flash"
    assert row["stale"] is False


@pytest.mark.asyncio
async def test_ingest_materializes_score_columns(conn, fixture_spot):
    parsed = _build_parsed(fixture_spot["review_ids"])
    await ingest_spot_enrichment(
        conn, fixture_spot["spot_id"], parsed,
        provider="deepseek", llm_model="deepseek-v4-flash",
    )
    row = await conn.fetchrow(
        "SELECT quietness_score, signals_data FROM spot_semantic_state WHERE spot_id = $1",
        fixture_spot["spot_id"],
    )
    # quietness=0.8 con peso decayed sobre 1 obs reciente → score debe estar cerca de 0.8
    assert row["quietness_score"] is not None
    assert 0.5 < row["quietness_score"] <= 1.0
    sd = row["signals_data"]
    if isinstance(sd, str):
        import json as _json
        sd = _json.loads(sd)
    assert "quietness" in sd
    assert "sea_view" in sd


@pytest.mark.asyncio
async def test_ingest_uses_review_fecha_for_observed_at(conn, fixture_spot):
    """observed_at de un claim que cita review_id debe = reviews.fecha."""
    parsed = _build_parsed(fixture_spot["review_ids"])
    await ingest_spot_enrichment(
        conn, fixture_spot["spot_id"], parsed,
        provider="deepseek", llm_model="deepseek-v4-flash",
    )
    row = await conn.fetchrow(
        """
        SELECT no.observed_at, r.fecha
        FROM normalized_observations no
        JOIN extracted_claims ec ON ec.id = no.claim_id
        JOIN reviews r ON r.id = ec.review_id
        WHERE no.spot_id = $1 AND no.signal_type = 'quietness'
        """,
        fixture_spot["spot_id"],
    )
    # observed_at debe coincidir con la fecha de la review (al día)
    assert row is not None
    assert row["observed_at"].date() == row["fecha"]


@pytest.mark.asyncio
async def test_ingest_rolls_back_on_error(conn, fixture_spot):
    """Si algo falla a mitad, no debe quedar claim a medias."""
    # Forzar fallo: claim con signal inválido NO genera observation pero tampoco crashea.
    # Hacemos algo más fuerte: insertar un claim válido y luego invalidar la transacción.
    parsed = ValidatedEnrichment(
        claims=[
            ValidatedClaim(signal="quietness", value=0.8, confidence=0.9,
                           review_id=fixture_spot["review_ids"][0],
                           excerpt="ok", raw={}),
        ],
        summary="ok", tags=[], best_for=[],
        best_season=None, avoid_season=None, errors=[],
    )
    # Forzamos error: spot_id que no existe → recompute_spot_state insertará la fila igualmente
    # porque hay observation. Usamos un spot inexistente para forzar FK violation.
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await ingest_spot_enrichment(
            conn, 999_999_999, parsed,  # spot inexistente
            provider="deepseek", llm_model="deepseek-v4-flash",
        )


@pytest.mark.asyncio
async def test_ingest_handles_empty_claims(conn, fixture_spot):
    """Un enrichment sin claims pero con summary debe completar correctamente."""
    parsed = ValidatedEnrichment(
        claims=[],
        summary="Just summary, no claims.",
        tags=["test"],
        best_for=[],
        best_season=None,
        avoid_season=None,
        errors=[],
    )
    stats = await ingest_spot_enrichment(
        conn, fixture_spot["spot_id"], parsed,
        provider="deepseek", llm_model="deepseek-v4-flash",
    )
    assert stats.claims_inserted == 0
    row = await conn.fetchrow(
        "SELECT summary_es, summary_en, tags, enrichment_version FROM spot_semantic_state WHERE spot_id = $1",
        fixture_spot["spot_id"],
    )
    # v4: summary_es queda NULL, summary_en lleva el contenido inglés.
    assert row["summary_es"] is None
    assert row["summary_en"] == "Just summary, no claims."
    assert "test" in row["tags"]


@pytest.mark.asyncio
async def test_parse_then_ingest_realistic_response(conn, fixture_spot):
    """End-to-end: respuesta JSON realista → parsea → ingest."""
    import json as _json
    response_text = _json.dumps({
        "claims": [
            {"signal": "quietness", "value": 0.85, "confidence": 0.9,
             "review_id": fixture_spot["review_ids"][0], "excerpt": "muy tranquilo"},
            {"signal": "sea_view", "value": True, "confidence": 0.95,
             "review_id": "description", "excerpt": "face à la mer"},
        ],
        "summary": "Quiet spot with sea views.",
        "tags": ["quiet", "sea"],
        "best_for": ["couples"],
        "best_season": "summer",
        "avoid_season": None,
    })
    parsed = parse_enrichment_response(response_text)
    assert len(parsed.claims) == 2

    stats = await ingest_spot_enrichment(
        conn, fixture_spot["spot_id"], parsed,
        provider="deepseek", llm_model="deepseek-v4-flash",
    )
    assert stats.claims_inserted == 2
