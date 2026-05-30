"""Limpieza de spots "shell" (fantasma) generados por deriva de dedup.

CONTEXTO (ver docs y commit "dedup sticky")
--------------------------------------------------------------------------
Antes del fix de dedup sticky, cada re-scrape re-evaluaba find_spot_cercano.
Si las coordenadas de un marker se movían o aparecía un spot más cercano, el
source_record MIGRABA a otro spot y dejaba el original como duplicado.

Un subconjunto de esos originales quedó como **shell**: spot ACTIVO con reviews
varadas pero SIN NINGÚN source_record (el husk vacío del que se fue todo). Son
ghost spots que aún aparecen en el mapa.

Este job SOLO toca shells (0 source_records). Los spots multi-fuente con reviews
huérfanas NO se tocan: tienen SRs de otras fuentes, son lugares reales y el
análisis mostró que sus vecinos NO son duplicados claros (fusionarlos
corrompería datos buenos).

ACCIÓN por shell (solo si hay un canónico seguro):
  - Canónico = spot activo más cercano CON source_records.
  - Umbral seguro (mismos que el dedup de producción):
        dist < 25m   (error GPS típico → mismo sitio), O
        dist < 60m Y similarity(nombre) >= 0.4
  - Mover reviews del shell al canónico (evitando colisión por
    (source, source_review_id)), unir fuentes[], recomputar total_reviews,
    y desactivar el shell (activo=FALSE, advertencia con el destino).

Shells sin canónico seguro se REPORTAN y se dejan intactos.

Uso:
    python -m jobs.cleanup_dedup_shells              # DRY-RUN (no escribe)
    python -m jobs.cleanup_dedup_shells --execute    # aplica los cambios
"""

import argparse
import asyncio

from loguru import logger

from config import Config
from db import create_pool

NEAR_DIST_M = 25.0      # match incondicional por cercanía (error GPS)
NAME_DIST_M = 60.0      # match con confirmación de nombre
NAME_SIM_MIN = 0.4      # umbral de similarity para el tramo 25-60m

# Grupos de tipos mutuamente excluyentes (mismos que find_spot_cercano): nunca
# fusionar un camping con un parking/wild/naturaleza, etc. — aunque las coords
# coincidan (dist=0), suelen ser POIs distintos co-ubicados.
EXCLUSION_GROUPS = {
    "camping": {"wild", "naturaleza", "parking_publico", "parking", "picnic", "area_descanso"},
    "wild": {"camping", "parking_privado", "area_ac", "gasolinera", "marina", "naturaleza"},
    "naturaleza": {"camping", "parking_privado", "area_ac", "gasolinera", "marina", "wild"},
    "parking_publico": {"camping", "wild", "naturaleza"},
    "parking": {"camping", "wild", "naturaleza"},
}


def _tipos_excluyentes(t1: str, t2: str) -> bool:
    a = (t1 or "otro").lower().strip()
    b = (t2 or "otro").lower().strip()
    if a in EXCLUSION_GROUPS and b in EXCLUSION_GROUPS[a]:
        return True
    if b in EXCLUSION_GROUPS and a in EXCLUSION_GROUPS[b]:
        return True
    return False


SHELL_CANDIDATES_SQL = """
WITH shells AS (
    SELECT DISTINCT rv.spot_id
    FROM reviews rv
    WHERE NOT EXISTS (
        SELECT 1 FROM source_records sr
        WHERE sr.source = rv.source AND sr.spot_id = rv.spot_id
    )
      AND NOT EXISTS (
        SELECT 1 FROM source_records sr2 WHERE sr2.spot_id = rv.spot_id
    )
)
SELECT
    s.id              AS shell_id,
    s.canonical_name  AS shell_name,
    s.tipo            AS shell_tipo,
    s.fuentes         AS shell_fuentes,
    (SELECT COUNT(*) FROM reviews r WHERE r.spot_id = s.id) AS n_reviews,
    nb.id             AS canon_id,
    nb.canonical_name AS canon_name,
    nb.tipo           AS canon_tipo,
    nb.dist,
    nb.sim
FROM shells sh
JOIN spots s ON s.id = sh.spot_id AND s.activo
LEFT JOIN LATERAL (
    SELECT s2.id, s2.canonical_name, s2.tipo,
           ST_Distance(s2.geog, s.geog) AS dist,
           similarity(s2.canonical_name, s.canonical_name) AS sim
    FROM spots s2
    WHERE s2.activo AND s2.id <> s.id
      AND EXISTS (SELECT 1 FROM source_records sr WHERE sr.spot_id = s2.id)
      AND ST_DWithin(s2.geog, s.geog, 150)
    ORDER BY ST_Distance(s2.geog, s.geog)
    LIMIT 1
) nb ON TRUE
ORDER BY n_reviews DESC;
"""


NAME_OVERRIDE_SIM = 0.6  # con nombre tan fuerte, el match gana a la exclusión de tipos


def _is_safe(dist, sim, shell_tipo=None, canon_tipo=None) -> bool:
    if dist is None:
        return False
    s = sim or 0.0
    # Proximidad/nombre suficientes
    proximo = dist < NEAR_DIST_M or (dist < NAME_DIST_M and s >= NAME_SIM_MIN)
    if not proximo:
        return False
    # Salvaguarda de tipos: veta camping↔parking/wild/etc. SOLO cuando el nombre
    # es débil. Si el nombre coincide fuerte (sim>=0.6) es el mismo sitio aunque
    # cada fuente lo clasifique distinto (parking vs area_ac es discrepancia
    # típica entre fuentes), así que el nombre manda. El caso peligroso real es
    # dist=0 + nombre distinto + tipo incompatible (POIs co-ubicados distintos).
    if _tipos_excluyentes(shell_tipo, canon_tipo) and s < NAME_OVERRIDE_SIM:
        return False
    return True


async def _merge_shell(conn, shell_id: int, canon_id: int) -> int:
    """Mueve reviews shell→canónico, une fuentes, recomputa, desactiva shell.
    Devuelve nº de reviews movidas. Debe ejecutarse dentro de una transacción.
    """
    # 1. Borrar reviews del shell que YA existan en el canónico (misma
    #    (source, source_review_id)) para no violar el índice único al mover.
    await conn.execute("""
        DELETE FROM reviews r_shell
        USING reviews r_canon
        WHERE r_shell.spot_id = $1
          AND r_canon.spot_id = $2
          AND r_shell.source = r_canon.source
          AND r_shell.source_review_id = r_canon.source_review_id
    """, shell_id, canon_id)

    # 2. Mover el resto.
    moved = await conn.fetchval("""
        WITH upd AS (
            UPDATE reviews SET spot_id = $2 WHERE spot_id = $1 RETURNING 1
        )
        SELECT COUNT(*) FROM upd
    """, shell_id, canon_id)

    # 3. Unir fuentes[] del shell en el canónico (dedup).
    await conn.execute("""
        UPDATE spots c
        SET fuentes = ARRAY(SELECT DISTINCT unnest(
                COALESCE(c.fuentes, '{}') || COALESCE(sh.fuentes, '{}')))
        FROM spots sh
        WHERE c.id = $2 AND sh.id = $1
    """, shell_id, canon_id)

    # 4. Mover el estado semántico de las reviews al canónico. Un shell NO tiene
    #    source_records → todos sus extracted_claims están anclados a reviews
    #    (review_id NOT NULL), nunca scraped_facts; y el canónico nunca tuvo esas
    #    reviews → mover spot_id es libre de colisiones con el índice único de
    #    claims. Las observaciones van por claim_id (también limpio).
    await conn.execute(
        "UPDATE extracted_claims SET spot_id = $2 WHERE spot_id = $1", shell_id, canon_id)
    await conn.execute(
        "UPDATE normalized_observations SET spot_id = $2 WHERE spot_id = $1", shell_id, canon_id)
    # Estado agregado del shell: stale y regenerable → borrar (el del canónico se
    # recomputará en el próximo full_recompute/nightly con las nuevas obs).
    await conn.execute("DELETE FROM spot_semantic_state WHERE spot_id = $1", shell_id)
    await conn.execute("DELETE FROM spot_embeddings WHERE spot_id = $1", shell_id)

    # 5. Recomputar total_reviews del canónico.
    await conn.execute("""
        UPDATE spots SET total_reviews = (
            SELECT COUNT(*) FROM reviews WHERE spot_id = $1
        ) WHERE id = $1
    """, canon_id)

    # 6. Desactivar el shell con traza del destino.
    await conn.execute("""
        UPDATE spots
        SET activo = FALSE,
            advertencia = CONCAT_WS(' | ', advertencia, $2::text),
            updated_at = NOW()
        WHERE id = $1
    """, shell_id, f"shell dedup fusionado en spot {canon_id}")

    return int(moved or 0)


async def main(execute: bool):
    pool = await create_pool(Config.from_env())
    modo = "EXECUTE" if execute else "DRY-RUN"
    logger.info(f"[cleanup_shells] Modo: {modo}")

    async with pool.acquire() as conn:
        rows = await conn.fetch(SHELL_CANDIDATES_SQL)

    def safe_row(r):
        return _is_safe(r["dist"], r["sim"], r["shell_tipo"], r["canon_tipo"])

    safe = [r for r in rows if safe_row(r)]
    no_canon = [r for r in rows if r["dist"] is None]
    unsafe = [r for r in rows if r["dist"] is not None and not safe_row(r)]

    rev_safe = sum(r["n_reviews"] for r in safe)
    logger.info(f"[cleanup_shells] shells totales: {len(rows)}")
    logger.info(f"  fusionables (seguros): {len(safe)}  ({rev_safe} reviews a mover)")
    logger.info(f"  sin canónico <150m:    {len(no_canon)}  (se dejan intactos)")
    logger.info(f"  con canónico pero NO seguro: {len(unsafe)}  (se dejan intactos)")

    # Muestra de los seguros
    for r in safe[:10]:
        logger.info(
            f"    shell {r['shell_id']} '{(r['shell_name'] or '')[:30]}' "
            f"({r['n_reviews']} rev) -> canon {r['canon_id']} "
            f"'{(r['canon_name'] or '')[:30]}' dist={r['dist']:.1f}m sim={r['sim'] or 0:.2f}"
        )

    if not execute:
        logger.info("[cleanup_shells] DRY-RUN: no se ha escrito nada. "
                    "Reejecuta con --execute para aplicar.")
        await pool.close()
        return

    total_moved = 0
    fusionados = 0
    for r in safe:
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    moved = await _merge_shell(conn, r["shell_id"], r["canon_id"])
            total_moved += moved
            fusionados += 1
            if fusionados % 25 == 0:
                logger.info(f"  ... {fusionados}/{len(safe)} shells fusionados")
        except Exception as e:
            logger.error(f"  shell {r['shell_id']} -> {r['canon_id']}: ERROR {e}")

    logger.info(f"[cleanup_shells] HECHO: {fusionados} shells fusionados, "
                f"{total_moved} reviews movidas, {len(safe)-fusionados} con error.")
    await pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="Aplica los cambios (por defecto: dry-run, no escribe)")
    args = ap.parse_args()
    asyncio.run(main(execute=args.execute))
