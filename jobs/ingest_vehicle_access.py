"""Sprint 2 (Tier-1) — Puebla spot_vehicle_access desde campos estructurados de raw_data.

Recupera la señal de acceso POLARIDAD-CORRECTA que ya estaba almacenada e inmutable en
source_records.raw_data pero que normalize() nunca extrajo (ver
docs/auditoria-compatibilidad-vehiculos.md §8, Tier-1). CERO scraping, sin LLM.

Fuentes mapeadas (verificadas 2026-05-30):
  park4night.hauteur_limite       → max_height_m   (límite de barrera, m)
  caramaps.maxHeight/Length/Width → max_*_m        (dimensiones declaradas)
  promobil.caravan8Meters=false   → max_length_m=8 (>8 m no permitido)
  campingcarpark.prohibitions.vehicleMore9m=true → max_length_m=9
  campy.camperSize                → max_length_m   (longitud máx, m)

Agregación: por spot, el límite MÁS RESTRICTIVO (mínimo) de cada dimensión gana — un
límite físico es un hecho duro y la seguridad pide conservadurismo. Procedencia en `evidence`.

Idempotente: re-correr recomputa y reescribe los campos Tier-1 (tabla regenerable).

Uso:
  python -m jobs.ingest_vehicle_access
  python -m jobs.ingest_vehicle_access --country ES
  python -m jobs.ingest_vehicle_access --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import os

import asyncpg
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jobs.ingest_spot_facts import _dsn  # reutiliza la lógica de conexión existente

# Confianza por fuente (= source_credibility.base_score, snapshot 2026-05-30).
CONF = {
    "park4night": 0.92, "campingcarpark": 0.90, "promobil": 0.84,
    "caramaps": 0.82, "campy": 0.82,
}

# Rangos plausibles (descartan ruido: 0, valores absurdos).
H_MIN, H_MAX = 1.5, 4.5      # altura de barrera razonable
L_MIN, L_MAX = 3.0, 15.0     # longitud
W_MIN, W_MAX = 1.7, 3.5      # anchura


def _country_clause(country: str | None, alias: str = "s") -> tuple[str, list]:
    if country:
        return f"JOIN spots {alias} ON {alias}.id = sr.spot_id AND {alias}.country_iso = $1", [country.upper()]
    return f"JOIN spots {alias} ON {alias}.id = sr.spot_id", []


# Cada extractor devuelve filas (spot_id, dimension, value_float|None, value_bool|None, source).
EXTRACTORS: list[tuple[str, str]] = [
    # (etiqueta, SQL). El SQL debe seleccionar: spot_id, dim, valf, valb, source
    ("park4night.hauteur_limite", """
        SELECT sr.spot_id, 'max_height_m' dim, (sr.raw_data->>'hauteur_limite')::real valf,
               NULL::bool valb, 'park4night' source
        FROM source_records sr {country_join}
        WHERE sr.source='park4night' AND sr.spot_id IS NOT NULL
          AND sr.raw_data->>'hauteur_limite' ~ '^[0-9]+(\\.[0-9]+)?$'
          AND (sr.raw_data->>'hauteur_limite')::real BETWEEN {hmin} AND {hmax}
    """),
    ("caramaps.maxHeight", """
        SELECT sr.spot_id, 'max_height_m' dim, (sr.raw_data->>'maxHeight')::real valf, NULL::bool valb, 'caramaps' source
        FROM source_records sr {country_join}
        WHERE sr.source='caramaps' AND sr.spot_id IS NOT NULL
          AND sr.raw_data->>'maxHeight' ~ '^[0-9]+(\\.[0-9]+)?$'
          AND (sr.raw_data->>'maxHeight')::real BETWEEN {hmin} AND {hmax}
    """),
    ("caramaps.maxLength", """
        SELECT sr.spot_id, 'max_length_m' dim, (sr.raw_data->>'maxLength')::real valf, NULL::bool valb, 'caramaps' source
        FROM source_records sr {country_join}
        WHERE sr.source='caramaps' AND sr.spot_id IS NOT NULL
          AND sr.raw_data->>'maxLength' ~ '^[0-9]+(\\.[0-9]+)?$'
          AND (sr.raw_data->>'maxLength')::real BETWEEN {lmin} AND {lmax}
    """),
    ("caramaps.maxWidth", """
        SELECT sr.spot_id, 'max_width_m' dim, (sr.raw_data->>'maxWidth')::real valf, NULL::bool valb, 'caramaps' source
        FROM source_records sr {country_join}
        WHERE sr.source='caramaps' AND sr.spot_id IS NOT NULL
          AND sr.raw_data->>'maxWidth' ~ '^[0-9]+(\\.[0-9]+)?$'
          AND (sr.raw_data->>'maxWidth')::real BETWEEN {wmin} AND {wmax}
    """),
    ("promobil.caravan8Meters", """
        SELECT sr.spot_id, 'max_length_m' dim, 8.0::real valf, NULL::bool valb, 'promobil' source
        FROM source_records sr {country_join}
        WHERE sr.source='promobil' AND sr.spot_id IS NOT NULL
          AND sr.raw_data->>'caravan8Meters' = 'false'
    """),
    ("campingcarpark.vehicleMore9m", """
        SELECT sr.spot_id, 'max_length_m' dim, 9.0::real valf, NULL::bool valb, 'campingcarpark' source
        FROM source_records sr {country_join}
        WHERE sr.source='campingcarpark' AND sr.spot_id IS NOT NULL
          AND (sr.raw_data->'prohibitions'->>'vehicleMore9m')::bool IS TRUE
    """),
    ("campy.camperSize", """
        SELECT sr.spot_id, 'max_length_m' dim, (sr.raw_data->>'camperSize')::real valf, NULL::bool valb, 'campy' source
        FROM source_records sr {country_join}
        WHERE sr.source='campy' AND sr.spot_id IS NOT NULL
          AND sr.raw_data->>'camperSize' ~ '^[0-9]+(\\.[0-9]+)?$'
          AND (sr.raw_data->>'camperSize')::real BETWEEN {lmin} AND {lmax}
    """),
]


async def collect(conn, country: str | None) -> dict[int, dict]:
    """Agrega contribuciones por spot. Para cada dimensión numérica, el MÍNIMO gana."""
    join, args = _country_clause(country)
    acc: dict[int, dict] = {}
    for label, sql in EXTRACTORS:
        q = sql.format(country_join=join, hmin=H_MIN, hmax=H_MAX,
                       lmin=L_MIN, lmax=L_MAX, wmin=W_MIN, wmax=W_MAX)
        rows = await conn.fetch(q, *args)
        logger.info(f"[{label}] {len(rows)} contribuciones")
        for r in rows:
            sid, dim, valf, source = r["spot_id"], r["dim"], r["valf"], r["source"]
            if valf is None:
                continue
            entry = acc.setdefault(sid, {})
            prev = entry.get(dim)
            # más restrictivo (mínimo) gana; guarda procedencia + confianza
            if prev is None or valf < prev["value"]:
                entry[dim] = {"value": round(float(valf), 2), "src": f"{source}.{dim}",
                              "conf": CONF.get(source, 0.7)}
    return acc


async def upsert(conn, acc: dict[int, dict], dry_run: bool) -> int:
    written = 0
    rows = []
    for sid, dims in acc.items():
        evidence = {d: {"value": v["value"], "src": v["src"]} for d, v in dims.items()}
        field_conf = {d: v["conf"] for d, v in dims.items()}
        conf = max((v["conf"] for v in dims.values()), default=0.0)
        rows.append((
            sid,
            dims.get("max_length_m", {}).get("value"),
            dims.get("max_height_m", {}).get("value"),
            dims.get("max_width_m", {}).get("value"),
            conf, json.dumps(field_conf), json.dumps(evidence),
        ))
    if dry_run:
        logger.info(f"[dry-run] {len(rows)} spots se escribirían. Muestra: {rows[:3]}")
        return 0
    # Upsert por lotes. Merge de evidence/field_confidence con lo existente (OSM/reseñas futuras).
    await conn.executemany("""
        INSERT INTO spot_vehicle_access
          (spot_id, max_length_m, max_height_m, max_width_m, confidence, field_confidence, evidence, computed_at)
        VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb, NOW())
        ON CONFLICT (spot_id) DO UPDATE SET
          max_length_m = COALESCE(EXCLUDED.max_length_m, spot_vehicle_access.max_length_m),
          max_height_m = COALESCE(EXCLUDED.max_height_m, spot_vehicle_access.max_height_m),
          max_width_m  = COALESCE(EXCLUDED.max_width_m,  spot_vehicle_access.max_width_m),
          confidence   = GREATEST(EXCLUDED.confidence, spot_vehicle_access.confidence),
          field_confidence = spot_vehicle_access.field_confidence || EXCLUDED.field_confidence,
          evidence     = spot_vehicle_access.evidence || EXCLUDED.evidence,
          computed_at  = NOW()
    """, rows)
    written = len(rows)
    return written


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default=None, help="ISO-2, p. ej. ES")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            acc = await collect(conn, args.country)
            logger.info(f"Spots con al menos una restricción Tier-1: {len(acc)}")
            n = await upsert(conn, acc, args.dry_run)
            logger.success(f"spot_vehicle_access poblado: {n} spots"
                           + (" (dry-run, nada escrito)" if args.dry_run else ""))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
