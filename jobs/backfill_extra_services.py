"""Rescate de servicios extra desde source_records.raw_data.

Cada scraper recoge 30-50 campos en raw_data, pero solo ~14 llegaban a `spots`.
Esta job lee `source_records.raw_data` (nada de re-scrapear), aplica un mapper
por fuente y rellena las nuevas columnas v4c + JSONB servicios_extras.

Idempotente: solo escribe valores no-NULL nuevos, nunca pisa datos ya existentes.

Uso:
  python -m jobs.backfill_extra_services                # todos
  python -m jobs.backfill_extra_services --country ES   # solo ES
  python -m jobs.backfill_extra_services --limit 100    # solo 100 spots
  python -m jobs.backfill_extra_services --source park4night  # solo records de esa fuente
  python -m jobs.backfill_extra_services --dry-run      # mostrar sin escribir
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import asyncpg
from loguru import logger


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
    # Docker compose exposes DB_USER / DB_PASSWORD / DB_HOST / DB_PORT.
    # POSTGRES_* are the host-side defaults used in .env.example.
    # DB_* take priority so the job works inside the container unchanged.
    user     = os.environ.get("DB_USER")     or os.environ.get("POSTGRES_USER",     "geospots")
    password = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD", "geospots")
    host     = os.environ.get("DB_HOST",  "localhost")
    port     = os.environ.get("DB_PORT",  "25433")
    dbname   = os.environ.get("DB_NAME")     or os.environ.get("POSTGRES_DB",       "geospots")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


# ───────────────────────────────────────────────────────────────────
# Extractors: importados desde scraper/sources/_normalize_helpers.py
#
# El backfill DELEGA toda la lógica de mapeo raw_data → columnas v4c/v4d
# en el módulo único compartido con los scrapers. Antes de PR 8e teníamos
# dos copias divergentes; ahora hay una sola.
# ───────────────────────────────────────────────────────────────────

import os as _os
import sys as _sys
_HELPERS_DIR = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "scraper"))
if _HELPERS_DIR not in _sys.path:
    _sys.path.insert(0, _HELPERS_DIR)

from sources._normalize_helpers import (  # noqa: E402
    extract_agricamper,
    extract_bobilguiden,
    extract_campercontact,
    extract_campercontact_detail,
    extract_campendium,
    extract_campingcarpark,
    extract_campspace,
    extract_campy,
    extract_camperstop,
    extract_caramaps,
    extract_furgovw,
    extract_nomady,
    extract_osm,
    extract_park4night,
    extract_promobil,
    extract_searchforsites,
    extract_stayfree,
    extract_thedyrt,
    extract_womostell,
    extract_wtmg,
)


EXTRACTORS = {
    "park4night":     extract_park4night,
    "campingcarpark": extract_campingcarpark,
    "agricamper":     extract_agricamper,
    "caramaps":       extract_caramaps,
    "campy":          extract_campy,
    "campercontact":  extract_campercontact,
    "camperstop":     extract_camperstop,
    "womostell":      extract_womostell,
    "stayfree":       extract_stayfree,
    "promobil":       extract_promobil,
    "searchforsites": extract_searchforsites,
    "bobilguiden":    extract_bobilguiden,
    "campendium":     extract_campendium,
    "osm":            extract_osm,
    "furgovw":        extract_furgovw,
    "thedyrt":        extract_thedyrt,
    "wtmg":           extract_wtmg,
    "nomady":         extract_nomady,
    "campspace":      extract_campspace,
}


# Coercers locales sólo para soporte legacy si algún test los importa.
# (No los usa el backfill — los importa quien quiera la lógica vieja.)


def _bool(v: Any) -> bool | None:
    """Acepta los formatos que vemos en raw_data: '0', '1', '', 'None',
    'true', 'false', True, False, 0, 1, None.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if v == 0:
            return False
        if v == 1:
            return True
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("", "none", "null", "nc"):
            return None
        if s in ("1", "true", "yes", "si", "sí"):
            return True
        if s in ("0", "false", "no"):
            return False
    return None


def _bool_any(*values) -> bool | None:
    """OR sobre múltiples valores. True si CUALQUIERA es True; False si TODOS son False; None si no info."""
    result = None
    for v in values:
        b = _bool(v)
        if b is True:
            return True
        if b is False:
            result = False
    return result


def _int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        i = int(v)
        return i if i > 0 else None  # 0 → None (sin info real)
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in ("none", "null"):
            return None
        try:
            i = int(float(s))
            return i if i > 0 else None
        except ValueError:
            return None
    return None


def _str_nonempty(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in ("none", "null", "nc"):
            return None
        return s
    return None


def _lang_iso(label: str) -> str | None:
    """Mapea "English" / "Italian" / etc → 'en' / 'it'."""
    if not label:
        return None
    LANGS = {
        "english": "en", "italian": "it", "spanish": "es", "french": "fr",
        "german": "de", "dutch": "nl", "portuguese": "pt",
        "polish": "pl", "romanian": "ro", "czech": "cs", "swedish": "sv",
        "norwegian": "no", "finnish": "fi", "danish": "da", "russian": "ru",
        "greek": "el", "hungarian": "hu", "turkish": "tr",
    }
    return LANGS.get(label.strip().lower())


def _has(text_list: list[Any], *needles: str) -> bool | None:
    """¿Alguna entrada de text_list contiene alguno de los needles (case-insensitive)?"""
    if not text_list:
        return None
    lower = [str(t).lower() for t in text_list if t]
    if not lower:
        return None
    for needle in needles:
        n = needle.lower()
        if any(n in t for t in lower):
            return True
    return False


# ───────────────────────────────────────────────────────────────────
# Merge de valores de múltiples fuentes para un mismo spot
# ───────────────────────────────────────────────────────────────────

BOOLEAN_COLS = (
    # v4c
    "piscina", "lavanderia", "gas_recharge", "restaurant", "juegos_ninos",
    "mirador", "zona_protegida", "online_booking", "winter_friendly", "apto_motos",
    "mtb_friendly", "surf_friendly", "fishing", "climbing", "hiking_nearby",
    # v4d (audit capa 1)
    "seguridad", "acceso_dificil", "accesibilidad_reducida", "acepta_caravanas",
)
INT_COLS = ("amperaje", "n_enchufes", "max_noches")
ARRAY_COLS = ("idiomas_hablados", "productos_venta")
# v4d: campos texto/fecha rellenados por primer no-NULL (mismo criterio que ints)
SCALAR_COLS = ("municipio", "email", "temporada_apertura")


def merge_spot_values(values_by_source: list[dict]) -> dict:
    """Une valores de múltiples fuentes para un spot.

    Estrategia:
      - Booleanos: True > False > None (cualquier True confirma)
      - Ints: primer no-NULL (las fuentes con datos suelen estar de acuerdo)
      - Arrays: unión deduplicada
      - JSONB: deep merge (later wins per-key, listas se concatenan dedup)
    """
    merged: dict = {}

    # Booleanos: OR — True si alguna fuente dice True
    for col in BOOLEAN_COLS:
        vals = [v.get(col) for v in values_by_source if v.get(col) is not None]
        if not vals:
            continue
        if any(v is True for v in vals):
            merged[col] = True
        elif all(v is False for v in vals):
            merged[col] = False
        # mixto sin True → no decisión clara

    # Ints: el más informativo (no-NULL, mayor que 0)
    for col in INT_COLS:
        for v in values_by_source:
            iv = v.get(col)
            if iv is not None:
                merged[col] = iv
                break

    # Scalars (texto/fechas): primer no-NULL no vacío
    for col in SCALAR_COLS:
        for v in values_by_source:
            sv = v.get(col)
            if sv is not None and sv != "":
                merged[col] = sv
                break

    # Arrays: unión
    for col in ARRAY_COLS:
        union: set[str] = set()
        for v in values_by_source:
            arr = v.get(col)
            if isinstance(arr, list):
                for item in arr:
                    if item:
                        union.add(str(item))
        if union:
            merged[col] = sorted(union)

    # JSONB: deep merge
    extras_merged: dict = {}
    for v in values_by_source:
        ex = v.get("servicios_extras")
        if not isinstance(ex, dict):
            continue
        for k, val in ex.items():
            if k not in extras_merged:
                extras_merged[k] = val
            elif isinstance(val, dict) and isinstance(extras_merged[k], dict):
                # Merge dicts: later doesn't overwrite earlier per-key
                for sk, sv in val.items():
                    extras_merged[k].setdefault(sk, sv)
            elif isinstance(val, list) and isinstance(extras_merged[k], list):
                # Listas: unión dedup preservando orden
                seen = set()
                merged_list = []
                for item in (extras_merged[k] + val):
                    key = json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
                    if key not in seen:
                        seen.add(key)
                        merged_list.append(item)
                extras_merged[k] = merged_list[:30]  # cap lists
    if extras_merged:
        merged["servicios_extras"] = extras_merged

    return merged


# ───────────────────────────────────────────────────────────────────
# Procesamiento de un spot
# ───────────────────────────────────────────────────────────────────


async def process_spot(conn, spot_id: int, *, dry_run: bool = False) -> tuple[bool, dict]:
    """Procesa un spot: lee sus source_records, extrae, mergea, UPDATE.

    Devuelve (updated, debug_dict).
    """
    rows = await conn.fetch(
        """
        SELECT source, raw_data FROM source_records
        WHERE spot_id = $1 AND raw_data IS NOT NULL
        """,
        spot_id,
    )
    if not rows:
        return False, {}

    extracted_per_source = []
    for r in rows:
        extractor = EXTRACTORS.get(r["source"])
        if not extractor:
            continue
        raw = r["raw_data"] if isinstance(r["raw_data"], dict) else json.loads(r["raw_data"])
        ex = extractor(raw)
        if ex:
            extracted_per_source.append(ex)

    if not extracted_per_source:
        return False, {}

    merged = merge_spot_values(extracted_per_source)
    if not merged:
        return False, {}

    if dry_run:
        return False, merged

    # UPDATE solo donde el campo actual sea NULL (no pisar info ya existente).
    # Para servicios_extras, mergeamos con lo que ya hay.
    set_clauses = []
    params: list = [spot_id]
    pi = 2  # PostgreSQL params start at $1

    for col in BOOLEAN_COLS + INT_COLS + SCALAR_COLS:
        if col in merged:
            set_clauses.append(f"{col} = COALESCE({col}, ${pi})")
            params.append(merged[col])
            pi += 1

    # Arrays: unión dedup en SQL (no pisamos lo existente, sumamos)
    for col in ARRAY_COLS:
        if col in merged:
            set_clauses.append(
                f"{col} = ARRAY(SELECT DISTINCT unnest("
                f"COALESCE({col}, ARRAY[]::text[]) || ${pi}::text[]"
                f"))"
            )
            params.append(merged[col])
            pi += 1

    if "servicios_extras" in merged:
        # Si ya hay datos en servicios_extras, hacemos merge JSONB
        set_clauses.append(
            f"servicios_extras = COALESCE(servicios_extras, '{{}}'::jsonb) || ${pi}::jsonb"
        )
        params.append(json.dumps(merged["servicios_extras"]))
        pi += 1

    if not set_clauses:
        return False, merged

    set_sql = ", ".join(set_clauses)
    await conn.execute(
        f"UPDATE spots SET {set_sql}, updated_at = NOW() WHERE id = $1",
        *params,
    )
    return True, merged


# ───────────────────────────────────────────────────────────────────
# Entry point
# ───────────────────────────────────────────────────────────────────


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill servicios extra desde source_records.raw_data")
    parser.add_argument("--country", help="ISO code (lowercase). Solo procesa spots de ese país.")
    parser.add_argument("--source", help="Solo spots con records de esa fuente.",
                        choices=list(EXTRACTORS.keys()))
    parser.add_argument("--limit", type=int, default=None, help="Max spots a procesar")
    parser.add_argument("--dry-run", action="store_true", help="No escribe, solo muestra estadísticas")
    parser.add_argument("--spot-id", type=int, default=None, help="Solo este spot_id (debug)")
    parser.add_argument("--batch-size", type=int, default=500, help="Spots por commit batch")
    args = parser.parse_args(argv)

    conn = await asyncpg.connect(dsn=_dsn())
    try:
        # Selección de spots
        where = ["s.activo = TRUE"]
        params: list = []
        pi = 1
        if args.spot_id:
            where.append(f"s.id = ${pi}")
            params.append(args.spot_id)
            pi += 1
        if args.country:
            where.append(f"s.country_iso = ${pi}")
            params.append(args.country.lower())
            pi += 1
        if args.source:
            where.append(
                f"EXISTS (SELECT 1 FROM source_records sr WHERE sr.spot_id = s.id AND sr.source = ${pi})"
            )
            params.append(args.source)
            pi += 1

        where_sql = " AND ".join(where)
        sql = f"SELECT s.id FROM spots s WHERE {where_sql} ORDER BY s.id"
        if args.limit:
            sql += f" LIMIT {args.limit}"

        spot_rows = await conn.fetch(sql, *params)
        total = len(spot_rows)
        logger.info(f"[backfill] target spots: {total} (dry_run={args.dry_run})")

        updated = 0
        skipped = 0
        sample_outputs = []
        for i, row in enumerate(spot_rows):
            try:
                u, debug = await process_spot(conn, row["id"], dry_run=args.dry_run)
                if u or (args.dry_run and debug):
                    updated += 1
                    if len(sample_outputs) < 5:
                        sample_outputs.append((row["id"], debug))
                else:
                    skipped += 1
            except Exception as exc:
                logger.error(f"[backfill] spot_id={row['id']} fallo: {exc}")
                skipped += 1
            if (i + 1) % args.batch_size == 0:
                logger.info(f"[backfill] progress {i + 1}/{total} updated={updated} skipped={skipped}")

        logger.info(f"[backfill] DONE total={total} updated={updated} skipped={skipped}")
        if sample_outputs:
            print("\nSAMPLE de outputs (primeros 5):")
            for sid, d in sample_outputs:
                print(f"\n  spot_id={sid}:")
                for k, v in d.items():
                    vs = json.dumps(v, ensure_ascii=False)[:200] if isinstance(v, (dict, list)) else str(v)
                    print(f"    {k:<25} = {vs}")
        return 0
    finally:
        await conn.close()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
