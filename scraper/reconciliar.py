"""Motor de reconciliación multi-fuente para GeoSpots."""

import json
from datetime import datetime, timezone
from loguru import logger

CREDIBILITY = {
    "tipo":               ["campingcarpark", "campercontact", "park4night", "thedyrt", "campendium", "promobil", "areasac", "campingcarinfos", "agricamper", "furgovw", "stayfree", "wtmg", "campspace", "alpacacamping", "vansite", "roadsurfer", "womostell", "searchforsites", "osm", "ioverlander"],
    "precio_info":        ["campingcarpark", "campercontact", "promobil", "thedyrt", "campendium", "alpacacamping", "womostell", "areasac", "campingcarinfos", "agricamper", "park4night", "stayfree", "vansite", "roadsurfer", "searchforsites", "furgovw", "campspace"],
    "precio_aprox":       ["campingcarpark", "campercontact", "promobil", "thedyrt", "campendium", "alpacacamping", "womostell", "areasac", "campingcarinfos", "agricamper", "park4night", "stayfree", "vansite", "roadsurfer", "searchforsites", "furgovw", "campspace"],
    "gratuito":           ["campingcarpark", "campercontact", "promobil", "thedyrt", "campendium", "alpacacamping", "womostell", "areasac", "campingcarinfos", "agricamper", "park4night", "stayfree", "vansite", "roadsurfer", "searchforsites", "furgovw", "wtmg", "campspace", "ioverlander"],
    "agua_potable":       ["campingcarpark", "promobil", "areasac", "campingcarinfos", "agricamper", "campercontact", "park4night", "thedyrt", "campendium", "womostell", "stayfree", "alpacacamping", "wtmg", "vansite", "roadsurfer", "searchforsites", "campspace", "osm", "ioverlander"],
    "electricidad":       ["campingcarpark", "promobil", "areasac", "campingcarinfos", "agricamper", "campercontact", "park4night", "thedyrt", "campendium", "womostell", "stayfree", "alpacacamping", "wtmg", "vansite", "roadsurfer", "searchforsites", "campspace", "osm", "ioverlander"],
    "ducha":              ["campingcarpark", "promobil", "areasac", "campingcarinfos", "agricamper", "campercontact", "park4night", "thedyrt", "campendium", "womostell", "stayfree", "alpacacamping", "wtmg", "vansite", "roadsurfer", "searchforsites", "campspace", "ioverlander"],
    "wifi":               ["campingcarpark", "promobil", "campercontact", "park4night", "thedyrt", "campendium", "womostell", "campingcarinfos", "agricamper", "vansite", "roadsurfer", "alpacacamping", "searchforsites", "campspace", "ioverlander"],
    "wc_publico":         ["campingcarpark", "promobil", "areasac", "campingcarinfos", "agricamper", "campercontact", "park4night", "thedyrt", "campendium", "womostell", "stayfree", "alpacacamping", "wtmg", "vansite", "roadsurfer", "searchforsites", "campspace", "ioverlander"],
    "vaciado_negras":     ["campingcarpark", "promobil", "areasac", "campingcarinfos", "agricamper", "campercontact", "park4night", "thedyrt", "campendium", "womostell", "stayfree", "alpacacamping", "roadsurfer", "searchforsites", "campspace"],
    "vaciado_grises":     ["campingcarpark", "promobil", "areasac", "campingcarinfos", "agricamper", "campercontact", "park4night", "thedyrt", "campendium", "womostell", "stayfree", "alpacacamping", "roadsurfer", "searchforsites", "campspace"],
    "num_plazas":         ["campingcarpark", "promobil", "campercontact", "thedyrt", "campendium", "areasac", "campingcarinfos", "agricamper", "park4night", "womostell", "wtmg", "vansite", "roadsurfer", "campspace"],
    "acceso_grandes":     ["campingcarpark", "promobil", "campercontact", "areasac", "campingcarinfos", "agricamper", "park4night", "thedyrt", "campendium", "womostell", "wtmg", "vansite", "roadsurfer", "alpacacamping", "searchforsites", "campspace"],
    "perros":             ["campingcarpark", "promobil", "campercontact", "park4night", "thedyrt", "campendium", "womostell", "campingcarinfos", "agricamper", "wtmg", "vansite", "roadsurfer", "alpacacamping", "searchforsites", "campspace", "ioverlander"],
    "altura_max_m":       ["park4night", "campercontact"],
    "reserva_req":        ["campspace", "thedyrt", "campendium", "campercontact", "park4night", "womostell"],
    "iluminacion":        ["campercontact", "park4night"],
    "seguridad":          ["campercontact", "park4night"],
    "canonical_name":     ["campingcarpark", "promobil", "campercontact", "park4night", "thedyrt", "campendium", "areasac", "campingcarinfos", "agricamper", "furgovw", "stayfree", "alpacacamping", "womostell", "wtmg", "vansite", "roadsurfer", "searchforsites", "osm", "ioverlander"],
    "temporada_apertura": ["campercontact", "park4night", "areasac", "womostell", "searchforsites"],
    "descripcion_es":     ["furgovw", "stayfree", "park4night", "areasac", "campingcarinfos", "agricamper", "wtmg", "campercontact", "vansite"],
    "descripcion_en":     ["thedyrt", "campendium", "park4night", "stayfree", "campercontact", "agricamper", "wtmg", "vansite", "roadsurfer", "searchforsites", "campspace", "ioverlander"],
    "descripcion_fr":     ["campingcarpark", "park4night", "campingcarinfos", "agricamper", "wtmg", "campercontact", "vansite"],
    "descripcion_de":     ["promobil", "alpacacamping", "womostell", "park4night", "agricamper", "wtmg", "campercontact", "vansite", "roadsurfer"],
    "master_rating":      ["campingcarpark", "promobil", "campercontact", "park4night", "thedyrt", "campendium", "stayfree", "alpacacamping", "womostell", "areasac", "campingcarinfos", "agricamper", "vansite", "roadsurfer", "searchforsites", "furgovw", "wtmg", "campspace"],
}

CONFLICT_FIELDS = ["gratuito", "precio_info", "agua_potable", "electricidad", "num_plazas", "tipo"]

DB_TO_NORM_KEY = {
    "canonical_name": "nombre",
    "master_rating": "rating_promedio",
    "total_reviews": "num_reviews",
}

def _reconciliar_campo(records: dict, campo: str):
    """Devuelve (valor, fuente) más fiable para un campo."""
    norm_key = DB_TO_NORM_KEY.get(campo, campo)
    for fuente in CREDIBILITY.get(campo, []):
        data = records.get(fuente, {})
        val = data.get(norm_key)
        if val is not None:
            return val, fuente
    for fuente, data in records.items():
        val = data.get(norm_key)
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
