"""Motor de reconciliación multi-fuente para GeoSpots."""

import json
from datetime import datetime, timezone
from loguru import logger

CREDIBILITY = {
    "precio_info":        ["campercontact", "areasac", "park4night", "furgovw"],
    "gratuito":           ["campercontact", "areasac", "park4night", "furgovw", "ioverlander"],
    "agua_potable":       ["areasac", "campercontact", "park4night", "osm", "ioverlander"],
    "electricidad":       ["areasac", "campercontact", "park4night", "osm", "ioverlander"],
    "ducha":              ["areasac", "campercontact", "park4night", "ioverlander"],
    "wifi":               ["campercontact", "park4night", "ioverlander"],
    "wc_publico":         ["areasac", "campercontact", "park4night", "ioverlander"],
    "vaciado_negras":     ["areasac", "campercontact", "park4night"],
    "vaciado_grises":     ["areasac", "campercontact", "park4night"],
    "num_plazas":         ["campercontact", "areasac", "park4night"],
    "acceso_grandes":     ["campercontact", "areasac", "park4night"],
    "canonical_name":     ["campercontact", "park4night", "areasac", "furgovw", "osm", "ioverlander"],
    "temporada_apertura": ["campercontact", "park4night", "areasac"],
    "descripcion_es":     ["furgovw", "park4night", "areasac", "campercontact"],
    "descripcion_en":     ["park4night", "campercontact", "ioverlander"],
    "descripcion_fr":     ["park4night", "campercontact"],
    "descripcion_de":     ["park4night", "campercontact"],
}

CONFLICT_FIELDS = ["gratuito", "precio_info", "agua_potable", "electricidad", "num_plazas"]


def _reconciliar_campo(records: dict, campo: str):
    """Devuelve (valor, fuente) más fiable para un campo."""
    for fuente in CREDIBILITY.get(campo, []):
        data = records.get(fuente, {})
        val = data.get(campo)
        if val is not None:
            return val, fuente
    for fuente, data in records.items():
        val = data.get(campo)
        if val is not None:
            return val, fuente
    return None, None


def _detectar_conflictos(records: dict) -> list[dict]:
    conflictos = []
    for campo in CONFLICT_FIELDS:
        valores = {}
        for fuente, data in records.items():
            v = data.get(campo)
            if v is not None:
                valores[fuente] = v
        if len(valores) >= 2 and len(set(str(v) for v in valores.values())) > 1:
            conflictos.append({
                "campo": campo,
                "fuentes": valores,
                "detectado_en": datetime.now(timezone.utc).isoformat(),
            })
    return conflictos


async def job_reconciliar(pool) -> dict:
    """Recorre spots multi-fuente y aplica jerarquía de credibilidad."""
    inicio = datetime.now()
    stats = {"procesados": 0, "actualizados": 0, "conflictos_total": 0, "errores": 0}

    async with pool.acquire() as conn:
        spot_ids = await conn.fetch("""
            SELECT id FROM spots
            WHERE activo = TRUE AND array_length(fuentes, 1) > 1
        """)

    logger.info(f"Reconciliación: {len(spot_ids)} spots multi-fuente")

    for row in spot_ids:
        spot_id = row["id"]
        try:
            async with pool.acquire() as conn:
                records_raw = await conn.fetch(
                    "SELECT source, normalized_data FROM source_records WHERE spot_id = $1",
                    spot_id
                )
                if len(records_raw) < 2:
                    continue

                records = {}
                for r in records_raw:
                    nd = r["normalized_data"]
                    if isinstance(nd, str):
                        nd = json.loads(nd)
                    records[r["source"]] = nd

                updates = {}
                for campo in CREDIBILITY:
                    val, _ = _reconciliar_campo(records, campo)
                    if val is not None:
                        updates[campo] = val

                conflictos = _detectar_conflictos(records)

                if not updates and not conflictos:
                    continue

                sets, vals = [], []
                idx = 1
                for campo, valor in updates.items():
                    sets.append(f"{campo} = ${idx}")
                    vals.append(valor)
                    idx += 1

                sets.append(f"conflictos = ${idx}::jsonb")
                vals.append(json.dumps(conflictos))
                idx += 1
                vals.append(spot_id)

                query = f"""
                    UPDATE spots SET {', '.join(sets)}, updated_at = NOW()
                    WHERE id = ${idx}
                """
                await conn.execute(query, *vals)
                stats["actualizados"] += 1
                stats["conflictos_total"] += len(conflictos)

            stats["procesados"] += 1
        except Exception as e:
            logger.error(f"Error reconciliando spot {spot_id}: {e}")
            stats["errores"] += 1

    dur = (datetime.now() - inicio).seconds
    logger.info(f"Reconciliación completada en {dur}s: {stats}")
    return stats
