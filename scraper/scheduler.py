"""GeoSpots Scheduler — Orquestador de scrapers."""

import asyncio
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
}


def _load_source(key: str):
    """Carga dinámicamente una clase de fuente."""
    module_path, class_name = SOURCES[key].rsplit(":", 1)
    import importlib
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)()


async def run_source(source_key: str):
    """Ejecuta un scraper individual."""
    source = _load_source(source_key)
    logger.info(f"=== Iniciando {source.name} ===")
    async with pool.acquire() as conn:
        log_id = await init_scraper_log(conn, source.name)
    try:
        stats = await source.run(pool, config, log_id)
        logger.info(f"{source.name} completado: {stats}")
        return stats
    except Exception as e:
        logger.error(f"{source.name} falló: {e}")
        from db import finish_scraper_log
        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, {
                "errores": 1, "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0
            })

async def run_source_reviews(source_key: str):
    """Ejecuta la descarga desacoplada de reviews para una fuente."""
    source = _load_source(source_key)
    logger.info(f"=== Iniciando descarga de reviews para {source.name} ===")
    async with pool.acquire() as conn:
        log_id = await init_scraper_log(conn, f"{source.name}_reviews")
    try:
        stats = await source.download_reviews(pool, config)
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

            if source_key in SOURCES:
                logger.info(f"Modo: solo {source_key}")
                await run_source(source_key)
                return

            logger.error(f"Fuente desconocida: {source_key}")
            logger.info(f"Fuentes disponibles: {', '.join(SOURCES.keys())}")
            return

    # Modo por defecto: todas las fuentes
    logger.info("Modo: pipeline completo")
    await run_all_sources()


if __name__ == "__main__":
    asyncio.run(main())
