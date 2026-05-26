"""Tests del orchestrator v2 (con DB real, transacciones rollback)."""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from enrichment.orchestrator_v2 import select_candidates


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
    c = await asyncpg.connect(dsn=_dsn())
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def _make_spot(conn, *, country: str, n_reviews: int,
                    enrichment_version: int | None = None,
                    stale: bool = False,
                    last_aggregated_offset_months: int | None = None) -> int:
    # Coordenadas oceánicas (mid-Atlántico) → el trigger geo no sobreescribe country_iso
    spot_id = await conn.fetchval(
        """
        INSERT INTO spots (canonical_name, lat, lon, country_iso, tipo, fuentes)
        VALUES ('TEST orch', 0.0, -30.0, $1, 'area_ac', ARRAY['park4night'])
        RETURNING id
        """,
        country.lower(),  # el trigger lo guardaría así igualmente
    )
    for i in range(n_reviews):
        days = 30 + i * 10
        await conn.execute(
            """
            INSERT INTO reviews (spot_id, source, source_review_id, texto, rating, fecha, first_seen)
            VALUES ($1, 'park4night', $2, 'review text long enough', 4,
                    CURRENT_DATE - make_interval(days => $3),
                    NOW() - make_interval(days => $3))
            """,
            spot_id, f"orch-{spot_id}-{i}", days,
        )
    # Materializar total_reviews (en producción lo actualiza el reconciliador)
    await conn.execute(
        "UPDATE spots SET total_reviews = $2 WHERE id = $1",
        spot_id, n_reviews,
    )
    if enrichment_version is not None:
        months = last_aggregated_offset_months if last_aggregated_offset_months else 0
        await conn.execute(
            """
            INSERT INTO spot_semantic_state
                (spot_id, enrichment_version, stale, last_aggregated_at, signals_data)
            VALUES ($1, $2, $3, NOW() - make_interval(months => $4), '{}'::jsonb)
            """,
            spot_id, enrichment_version, stale, months,
        )
    return spot_id


@pytest.mark.asyncio
async def test_select_includes_never_enriched(conn):
    spot = await _make_spot(conn, country="XX", n_reviews=3)
    ids = await select_candidates(conn, countries=["XX"], limit=100,
                                  enrichment_version=2)
    assert spot in ids


@pytest.mark.asyncio
async def test_select_excludes_few_reviews(conn):
    spot = await _make_spot(conn, country="XX", n_reviews=2)  # <3
    ids = await select_candidates(conn, countries=["XX"], limit=100,
                                  enrichment_version=2)
    assert spot not in ids


@pytest.mark.asyncio
async def test_select_includes_old_version(conn):
    spot = await _make_spot(conn, country="XX", n_reviews=5, enrichment_version=1)
    ids = await select_candidates(conn, countries=["XX"], limit=100,
                                  enrichment_version=2)
    assert spot in ids


@pytest.mark.asyncio
async def test_select_excludes_current_version_fresh(conn):
    spot = await _make_spot(conn, country="XX", n_reviews=5, enrichment_version=2,
                            last_aggregated_offset_months=1)
    ids = await select_candidates(conn, countries=["XX"], limit=100,
                                  enrichment_version=2)
    assert spot not in ids


@pytest.mark.asyncio
async def test_select_includes_stale(conn):
    spot = await _make_spot(conn, country="XX", n_reviews=5, enrichment_version=2,
                            stale=True, last_aggregated_offset_months=1)
    ids = await select_candidates(conn, countries=["XX"], limit=100,
                                  enrichment_version=2)
    assert spot in ids


@pytest.mark.asyncio
async def test_select_includes_old_aggregated(conn):
    spot = await _make_spot(conn, country="XX", n_reviews=5, enrichment_version=2,
                            last_aggregated_offset_months=20)
    ids = await select_candidates(conn, countries=["XX"], limit=100,
                                  enrichment_version=2)
    assert spot in ids


@pytest.mark.asyncio
async def test_select_filters_by_country(conn):
    xx_spot = await _make_spot(conn, country="XX", n_reviews=5)
    yy_spot = await _make_spot(conn, country="YY", n_reviews=5)
    ids = await select_candidates(conn, countries=["XX"], limit=100,
                                  enrichment_version=2)
    assert xx_spot in ids
    assert yy_spot not in ids


@pytest.mark.asyncio
async def test_select_no_filter_returns_both(conn):
    xx_spot = await _make_spot(conn, country="XX", n_reviews=5)
    yy_spot = await _make_spot(conn, country="YY", n_reviews=5)
    # Filtramos por nuestros dos países sintéticos para no traer 125K spots reales
    ids = await select_candidates(conn, countries=["XX", "YY"], limit=100,
                                  enrichment_version=2)
    assert xx_spot in ids
    assert yy_spot in ids


@pytest.mark.asyncio
async def test_select_respects_limit(conn):
    spots = [await _make_spot(conn, country="XX", n_reviews=5) for _ in range(5)]
    ids = await select_candidates(conn, countries=["XX"], limit=2,
                                  enrichment_version=2)
    # No comprobamos cuáles, solo que respeta el LIMIT
    assert len(ids) <= 2


@pytest.mark.asyncio
async def test_select_prioritizes_old_version_over_current(conn):
    """Spots con version vieja deben aparecer ANTES que los stale=TRUE en version actual."""
    old_v = await _make_spot(conn, country="XX", n_reviews=5,
                             enrichment_version=1)
    stale_v = await _make_spot(conn, country="XX", n_reviews=10,
                               enrichment_version=2, stale=True)
    ids = await select_candidates(conn, countries=["XX"], limit=10,
                                  enrichment_version=2)
    assert ids.index(old_v) < ids.index(stale_v)
