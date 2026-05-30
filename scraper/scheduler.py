"""GeoSpots Scheduler — Orquestador de scrapers."""

import asyncio
import json
import sys
from datetime import datetime
from loguru import logger

from config import Config
from db import create_pool, init_scraper_log

pool = None
config = None

# ═══════════════════════════════════════════════════════════════
# SOURCE REGISTRY
# ═══════════════════════════════════════════════════════════════

SOURCES = {
    "campercontact": "sources.campercontact:CamperContactSource",
    "park4night": "sources.park4night:Park4NightSource",
    "ioverlander": "sources.ioverlander:IOverlanderSource",
    "amigosac": "sources.amigosac:AmigosACSource",
    "freecampsites": "sources.freecampsites:FreeCampsitesSource",
    "furgovw": "sources.furgovw:FurgovwSource",
    "areasac": "sources.areasac:AreasACSource",
    "osm": "sources.osm:OSMSource",
    "searchforsites": "sources.searchforsites:SearchForSitesSource",
    "wtmg": "sources.wtmg:WelcomeToMyGardenSource",
    "nomady": "sources.nomady:NomadySource",
    "campspace": "sources.campspace:CampspaceSource",
    "roadsurfer": "sources.roadsurfer:RoadsurferSource",
    "vansite": "sources.vansite:VansiteSource",
    "portugaleasycamp": "sources.portugaleasycamp:PortugalEasyCampSource",
    "caramaps": "sources.caramaps:CaramapsSource",
    "stayfree": "sources.stayfree:StayFreeSource",
    "promobil": "sources.promobil:PromobilSource",
    "camperstop": "sources.camperstop:CamperstopSource",
    "alpacacamping": "sources.alpacacamping:AlpacaCampingSource",
    "womostell": "sources.womostell:WomoStellplatzSource",
    "thedyrt": "sources.thedyrt:TheDyrtSource",
    "campingcarinfos": "sources.campingcarinfos:CampingcarInfosSource",
    "agricamper": "sources.agricamper:AgricamperSource",
    "campendium": "sources.campendium:CampendiumSource",
    "campingcarpark": "sources.campingcarpark:CampingCarParkSource",
    "campy": "sources.campy:CampySource",
    "bobilguiden": "sources.bobilguiden:BobilguidenSource",
    "google_maps": "sources.google_maps:GoogleMapsSource",
    "google_maps_api": "sources.google_maps_api:GoogleMapsAPISource",
}


def _load_source(key: str):
    """Carga dinámicamente una clase de fuente."""
    module_path, class_name = SOURCES[key].rsplit(":", 1)
    import importlib
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)()


async def run_source(source_key: str, job_id: int = None):
    """Ejecuta un scraper individual."""
    source = _load_source(source_key)
    logger.info(f"=== Iniciando {source.name} ===")
    async with pool.acquire() as conn:
        log_id = await init_scraper_log(conn, source.name)
    try:
        stats = await source.run(pool, config, log_id, job_id=job_id)
        logger.info(f"{source.name} completado: {stats}")
        return stats
    except Exception as e:
        logger.error(f"{source.name} falló: {e}")
        from db import finish_scraper_log
        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, {
                "errores": 1, "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0
            })

async def run_source_reviews(source_key: str, job_id: int = None):
    """Ejecuta la descarga desacoplada de reviews para una fuente."""
    source = _load_source(source_key)
    logger.info(f"=== Iniciando descarga de reviews para {source.name} ===")
    async with pool.acquire() as conn:
        log_id = await init_scraper_log(conn, f"{source.name}_reviews")
    try:
        stats = await source.download_reviews(pool, config, job_id=job_id)
        logger.info(f"Descarga de reviews para {source.name} completada: {stats}")
        from db import finish_scraper_log
        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
        return stats
    except Exception as e:
        logger.error(f"Descarga de reviews para {source.name} falló: {e}")
        from db import finish_scraper_log
        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, {
                "errores": 1, "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0
            })


async def write_worker_heartbeat():
    """Escribe el heartbeat del daemon en scraper_jobs_meta.

    El panel admin lee este timestamp para saber si el worker está vivo.
    Si no se actualiza en >90s, asume que el worker está muerto.
    """
    async with pool.acquire() as conn:
        # Crea tabla si no existe (idempotente, no rompe si la creó la API).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scraper_jobs_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await conn.execute("""
            INSERT INTO scraper_jobs_meta(key, value) VALUES ('worker_heartbeat', NOW()::text)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """)


async def _heartbeat_task(period_s: int = 20):
    """Task de fondo: escribe heartbeat cada N segundos, independiente del
    trabajo. Crítico — antes el heartbeat estaba dentro del bucle principal
    y se 'congelaba' mientras un scrape largo bloqueaba run_pending_jobs(),
    haciendo creer al panel que el worker estaba muerto."""
    while True:
        try:
            await write_worker_heartbeat()
        except Exception as e:
            logger.error(f"[heartbeat] {e}")
        await asyncio.sleep(period_s)


async def daemon_loop(interval_s: int = 30):
    """Bucle principal del daemon. Lanza heartbeat en background y procesa
    la cola en primer plano. El heartbeat sigue beat-eando incluso durante
    scrapes que tardan horas.
    """
    logger.info(f"[daemon] Arrancando worker (poll cada {interval_s}s, heartbeat cada 20s)")
    # Primer heartbeat inmediato para que el panel lo vea en <1s
    await write_worker_heartbeat()
    # Background task — nunca termina (no se await-ea explícitamente)
    asyncio.create_task(_heartbeat_task(period_s=20))

    # Limpieza inmediata de zombies por reinicio: todo lo que diga 'running' al 
    # arrancar el daemon es seguro que está muerto.
    async with pool.acquire() as conn:
        await conn.execute("UPDATE scraper_log SET estado = 'zombie', terminado_en = NOW() WHERE estado = 'running'")
        await conn.execute("UPDATE scraper_jobs SET status = 'error', finished_at = NOW(), result = '{\"error\": \"daemon restarted\"}'::jsonb WHERE status = 'running'")
        logger.info("[daemon] Limpiados posibles zombies por reinicio del daemon")

    while True:
        try:
            await run_pending_jobs()
        except Exception as e:
            logger.error(f"[daemon] Error en ciclo: {e}")
        await asyncio.sleep(interval_s)


async def cleanup_zombie_runs(max_hours: int = 4) -> dict:
    """Marca como zombie scraper_log/scraper_jobs colgados >max_hours en running."""
    async with pool.acquire() as conn:
        log_res = await conn.execute(
            f"UPDATE scraper_log SET estado = 'zombie', terminado_en = NOW() "
            f"WHERE estado = 'running' AND iniciado_en < NOW() - INTERVAL '{max_hours} hours'"
        )
        job_res = await conn.execute(
            f"UPDATE scraper_jobs SET status = 'error', finished_at = NOW(), "
            f"result = COALESCE(result,'{{}}'::jsonb) || jsonb_build_object('error', 'timeout: zombie tras {max_hours}h') "
            f"WHERE status IN ('pending','running') AND created_at < NOW() - INTERVAL '{max_hours} hours'"
        )
    return {
        "scraper_log_updated": int(log_res.split()[-1]) if log_res else 0,
        "scraper_jobs_updated": int(job_res.split()[-1]) if job_res else 0,
    }


async def run_pending_jobs():
    """Ejecuta los jobs de la cola scraper_jobs de forma concurrente."""
    cleanup = await cleanup_zombie_runs(max_hours=4)
    if cleanup["scraper_log_updated"] or cleanup["scraper_jobs_updated"]:
        logger.info(f"[queue] Limpieza zombies previa: {cleanup}")

    async with pool.acquire() as conn:
        running_count = await conn.fetchval("SELECT COUNT(*) FROM scraper_jobs WHERE status='running'")
        
        limit = config.max_workers - running_count
        if limit <= 0:
            return

        jobs = await conn.fetch(
            "UPDATE scraper_jobs SET status='running', started_at=NOW() "
            f"WHERE id IN (SELECT id FROM scraper_jobs WHERE status='pending' ORDER BY created_at LIMIT {limit}) "
            "RETURNING id, source, job_type"
        )

    if not jobs:
        return

    async def _run_one_job(job):
        job_id, source_key, job_type = job["id"], job["source"], job["job_type"]
        logger.info(f"[queue] Ejecutando job {job_id}: {source_key} ({job_type})")

        # Job de mantenimiento especial: reconciliación multi-fuente.
        # No es una "fuente" del registro SOURCES; lo ejecutamos aparte para
        # que el panel admin pueda lanzarlo con un botón como el resto.
        if source_key == "reconciliar":
            try:
                from reconciliar import job_reconciliar
                stats = await job_reconciliar(pool)
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE scraper_jobs SET status='done', finished_at=NOW(), result=$1::jsonb WHERE id=$2",
                        json.dumps(stats or {}, default=str), job_id,
                    )
                logger.info(f"[queue] Job {job_id} (reconciliar) completado: {stats}")
            except Exception as e:
                logger.error(f"[queue] Job {job_id} (reconciliar) falló: {e}")
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE scraper_jobs SET status='error', finished_at=NOW(), result=$1::jsonb WHERE id=$2",
                        json.dumps({"error": str(e)}), job_id,
                    )
            return

        if source_key not in SOURCES:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE scraper_jobs SET status='error', finished_at=NOW(), result=$1::jsonb WHERE id=$2",
                    json.dumps({"error": f"Fuente desconocida: {source_key}"}), job_id,
                )
            return

        try:
            if job_type == "reviews":
                stats = await run_source_reviews(source_key, job_id=job_id)
            else:
                stats = await run_source(source_key, job_id=job_id)
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE scraper_jobs SET status='done', finished_at=NOW(), result=$1::jsonb WHERE id=$2",
                    json.dumps(stats or {}, default=str), job_id,
                )
            logger.info(f"[queue] Job {job_id} completado: {stats}")
        except Exception as e:
            logger.error(f"[queue] Job {job_id} falló: {e}")
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE scraper_jobs SET status='error', finished_at=NOW(), result=$1::jsonb WHERE id=$2",
                    json.dumps({"error": str(e)}), job_id,
                )

    # Lanzar los jobs en background sin bloquear el loop principal
    for job in jobs:
        asyncio.create_task(_run_one_job(job))



async def run_all_sources():
    """Ejecuta todos los scrapers activos secuencialmente."""
    for key in SOURCES:
        try:
            await run_source(key)
        except Exception as e:
            logger.error(f"Error en {key}: {e}")


async def main():
    global pool, config

    logger.info("GeoSpots Scraper arrancando...")

    config = Config.from_env()
    pool = await create_pool(config)
    logger.info("Conexión a DB establecida")

    args = sys.argv[1:]

    # Modo: ejecutar una fuente específica
    if args:
        first_arg = args[0]
        if first_arg == "--reviews":
            if len(args) < 2:
                logger.error("Debes especificar la fuente. Ej: --reviews park4night")
                return
            source_key = args[1].lower()
            if source_key in SOURCES:
                logger.info(f"Modo: solo reviews de {source_key}")
                await run_source_reviews(source_key)
            else:
                logger.error(f"Fuente desconocida: {source_key}")
            return

        if first_arg.startswith("--"):
            source_key = first_arg.lstrip("-")

            if source_key == "all":
                logger.info("Modo: todas las fuentes")
                await run_all_sources()
                return

            if source_key == "reconciliar":
                logger.info("Modo: reconciliación")
                from reconciliar import job_reconciliar
                stats = await job_reconciliar(pool)
                logger.info(f"Reconciliación completada: {stats}")
                return

            if source_key == "run-pending":
                logger.info("Modo: ejecutar jobs pendientes de la cola")
                await run_pending_jobs()
                return

            if source_key == "daemon":
                logger.info("Modo: daemon (worker continuo de la cola)")
                await daemon_loop(interval_s=30)
                return  # nunca llega — daemon_loop es infinito

            if source_key == "cleanup-zombies":
                logger.info("Modo: limpiar runs zombie (>12h en 'running')")
                res = await cleanup_zombie_runs(max_hours=12)
                logger.info(f"Limpieza completada: {res}")
                return

            if source_key == "run-all":
                logger.info("Modo: ejecutar todos los scrapers (one-shot)")
                await run_all_sources()
                return

            if source_key in SOURCES:
                logger.info(f"Modo: solo {source_key}")
                await run_source(source_key)
                return

            logger.error(f"Fuente desconocida: {source_key}")
            logger.info(f"Fuentes disponibles: {', '.join(SOURCES.keys())}")
            return

    # Modo por defecto: daemon. ANTES era run_all_sources() que disparaba
    # los 21 scrapers en cadena al arrancar el contenedor — disruptivo y
    # dejaba runs en estado 'running' colgado tras cualquier restart.
    # Si quieres el comportamiento antiguo: python scheduler.py --run-all
    logger.info("Modo: daemon (default). Usa --run-all para ejecutar todo, "
                "o --<fuente> para una sola.")
    await daemon_loop(interval_s=30)


if __name__ == "__main__":
    asyncio.run(main())
