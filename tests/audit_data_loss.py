"""Audita campos raw vs campos llegados a spots para detectar pérdida de datos.

Para spots reales, compara:
  - Lo que el scraper recoge en source_records.raw_data
  - Lo que llega a spots (columnas reconciliadas)
  - Lo que llega al prompt v4 (SERVICES block)
"""
import asyncio, json, os, sys
import asyncpg


def _ld():
    if not os.path.exists('.env'): return
    for line in open('.env'):
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k,v=line.split('=',1); os.environ.setdefault(k,v.strip().strip(chr(34)).strip(chr(39)))


def _dsn():
    _ld()
    return f"postgresql://{os.environ.get('POSTGRES_USER','geospots')}:{os.environ.get('POSTGRES_PASSWORD','geospots')}@{os.environ.get('DB_HOST','localhost')}:{os.environ.get('DB_PORT','25433')}/{os.environ.get('POSTGRES_DB','geospots')}"


# Campos de spots que SÍ pasamos al prompt v4 (subconjunto)
PROMPT_FIELDS = {
    "gratuito", "precio_aprox", "precio_info",
    "agua_potable", "vaciado_negras", "vaciado_grises", "electricidad",
    "ducha", "wifi", "wc_publico",
    "acceso_grandes", "num_plazas", "altura_max_m", "temporada_apertura",
}

# Campos de spots con servicios (todos los que tenemos en schema)
ALL_SPOT_SERVICE_FIELDS = PROMPT_FIELDS | {
    "perros", "reserva_req", "iluminacion", "seguridad",
    "web", "telefono", "email",
}


async def main():
    sys.stdout = open(sys.argv[1] if len(sys.argv) > 1 else
                      "docs/validation/data_loss_audit.txt",
                      "w", encoding="utf-8")
    conn = await asyncpg.connect(dsn=_dsn())
    try:
        # 1. Para cada scraper principal, sacar 1 spot rico (camping/area_ac)
        # y comparar raw_data vs spot reconciliado vs prompt
        for source in ("park4night", "campercontact", "campingcarpark", "agricamper",
                       "caramaps", "campy"):
            print(f"\n{'=' * 80}")
            print(f"FUENTE: {source}")
            print('=' * 80)

            # Coger un spot con muchos campos en raw_data
            row = await conn.fetchrow(
                """
                SELECT sr.spot_id, sr.source_id, sr.raw_data,
                       s.canonical_name, s.country_iso, s.tipo, s.total_reviews,
                       s.gratuito, s.precio_aprox, s.precio_info,
                       s.agua_potable, s.vaciado_negras, s.vaciado_grises,
                       s.electricidad, s.ducha, s.wifi, s.wc_publico,
                       s.acceso_grandes, s.num_plazas, s.altura_max_m,
                       s.temporada_apertura, s.perros, s.reserva_req,
                       s.iluminacion, s.seguridad, s.web, s.telefono, s.email
                FROM source_records sr
                JOIN spots s ON s.id = sr.spot_id
                WHERE sr.source = $1
                  AND sr.raw_data IS NOT NULL
                  AND s.tipo IN ('camping', 'area_ac')
                  AND s.activo = TRUE
                ORDER BY jsonb_array_length(
                    CASE WHEN jsonb_typeof(sr.raw_data) = 'object'
                         THEN (SELECT jsonb_agg(k) FROM jsonb_object_keys(sr.raw_data) k)
                         ELSE '[]'::jsonb END
                ) DESC NULLS LAST
                LIMIT 1
                """,
                source,
            )
            if not row:
                print(f"  (sin datos en {source})")
                continue

            raw = row["raw_data"]
            if isinstance(raw, str):
                raw = json.loads(raw)

            print(f"  spot_id={row['spot_id']} | {row['canonical_name']!r} [{row['country_iso']}] tipo={row['tipo']}")
            print(f"  total_reviews={row['total_reviews']}")
            print()

            # KEYS en raw_data
            keys = sorted(raw.keys()) if isinstance(raw, dict) else []
            non_null_keys = [k for k in keys
                             if raw.get(k) not in (None, "", [], {}, "0", 0, False)
                             or (k in ("price", "tariffs", "facilities", "services", "amperage")
                                 and raw.get(k) is not None)]
            print(f"  RAW DATA: {len(keys)} keys totales, {len(non_null_keys)} con valor no-vacío")
            print()

            # Mostrar los campos no-vacíos del raw
            print("  Campos con datos en raw (top 30):")
            for k in non_null_keys[:30]:
                v = raw.get(k)
                if isinstance(v, (dict, list)):
                    preview = json.dumps(v, ensure_ascii=False)[:80]
                else:
                    preview = str(v)[:80]
                print(f"    {k:<30} = {preview}")
            if len(non_null_keys) > 30:
                print(f"    ... ({len(non_null_keys) - 30} más)")

            # Campos del spot reconciliado con valor
            print()
            spot_filled = {}
            for f in ALL_SPOT_SERVICE_FIELDS:
                v = row[f] if f in row else None
                if v is not None and v != '':
                    spot_filled[f] = v
            print(f"  SPOT RECONCILIADO: {len(spot_filled)}/{len(ALL_SPOT_SERVICE_FIELDS)} campos con valor")
            for k, v in sorted(spot_filled.items()):
                marker = " ← en prompt" if k in PROMPT_FIELDS else " ✗ NO en prompt"
                print(f"    {k:<25} = {v}  {marker}")

            print()
            # Estimación de "pérdida"
            in_prompt = sum(1 for k in PROMPT_FIELDS if row[k] is not None and row[k] != '')
            in_spot = len(spot_filled)
            print(f"  📊 GAP:")
            print(f"     - {len(non_null_keys)} campos no-vacíos en raw")
            print(f"     - {in_spot} llegan a spots (de {len(ALL_SPOT_SERVICE_FIELDS)} columnas posibles)")
            print(f"     - {in_prompt} llegan al prompt v4 SERVICES block")
            print(f"     - posible pérdida raw→spot: {len(non_null_keys) - in_spot} campos")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
