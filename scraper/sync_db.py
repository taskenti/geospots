import asyncio
from config import Config
from db import create_pool

async def main():
    print("Iniciando sincronización de fuentes_config...")
    config = Config.from_env()
    pool = await create_pool(config)
    
    async with pool.acquire() as conn:
        print("Conectado a la base de datos interna.")
        
        # 1. Insertar fuentes faltantes
        print("Registrando nuevas fuentes en fuentes_config...")
        await conn.execute("""
            INSERT INTO fuentes_config (nombre, activa, spots_totales)
            VALUES 
              ('portugaleasycamp', true, 0),
              ('campspace', true, 0),
              ('wtmg', true, 0),
              ('roadsurfer', true, 0),
              ('vansite', true, 0),
              ('caramaps', true, 0),
              ('stayfree', true, 0),
              ('promobil', true, 0),
              ('womostell', true, 0),
              ('thedyrt', true, 0),
              ('campingcarinfos', true, 0),
              ('agricamper', true, 0),
              ('campendium', true, 0),
              ('campingcarpark', true, 0),
              ('campy', true, 0),
              ('google_maps', true, 0),
              ('bobilguiden', true, 0),
              ('amigosac', true, 0),
              ('freecampsites', true, 0)
            ON CONFLICT (nombre) DO NOTHING;
        """)
        
        # 1.1 Registrar credibilidad base para fuentes nuevas
        print("Registrando credibilidad en source_credibility...")
        await conn.execute("""
            INSERT INTO source_credibility (source, display_name, base_score, review_quality, coverage_region)
            VALUES 
              ('agricamper', 'Agricamper Italia', 0.80, 0.70, ARRAY['IT']),
              ('campingcarinfos', 'Campingcar-infos', 0.82, 0.70, ARRAY['EU']),
              ('campendium', 'Campendium', 0.85, 0.75, ARRAY['US', 'CA']),
              ('campingcarpark', 'CampingCar Park', 0.90, 0.85, ARRAY['EU']),
              ('campspace', 'Campspace', 0.74, 0.76, ARRAY['EU']),
              ('campy', 'Campy', 0.82, 0.82, ARRAY['DE', 'AT', 'CH']),
              ('google_maps', 'Google Maps', 0.90, 0.95, ARRAY['GL']),
              ('bobilguiden', 'Bobilguiden', 0.85, 0.80, ARRAY['NO', 'SE', 'DK']),
              ('amigosac', 'AmigosAC España/Portugal', 0.85, 0.70, ARRAY['ES', 'PT']),
              ('freecampsites', 'FreeCampsites.net', 0.78, 0.80, ARRAY['US', 'CA', 'AU'])
            ON CONFLICT (source) DO NOTHING;
        """)
        
        # 2. Sincronizar spots_totales reales
        print("Sincronizando contadores de spots_totales...")
        await conn.execute("""
            UPDATE fuentes_config fc
            SET spots_totales = (SELECT COUNT(*) FROM source_records sr WHERE sr.source = fc.nombre)
            WHERE fc.nombre IN ('ioverlander', 'park4night', 'portugaleasycamp', 'campspace', 'caramaps', 'stayfree', 'promobil', 'alpacacamping', 'womostell', 'thedyrt', 'campingcarinfos', 'agricamper', 'campendium', 'campingcarpark', 'campy', 'bobilguiden', 'google_maps', 'amigosac', 'freecampsites');
        """)

        # 3. Sincronizar total_records en source_credibility (BUG-34).
        # Este campo estaba siempre en 0 porque sync_db nunca lo actualizaba.
        # Cualquier código que lo lea (ej. reporting, futuros scorers) obtenía 0.
        print("Sincronizando total_records en source_credibility...")
        await conn.execute("""
            UPDATE source_credibility sc
            SET total_records = (
                SELECT COUNT(*) FROM source_records sr WHERE sr.source = sc.source
            )
        """)

        # 4. Marcar qué fuentes implementan download_reviews() (override real,
        #    no el no-op de AbstractSource). El PWA lo usa para mostrar el botón
        #    "Reviews" aunque la fuente aún tenga 0 reviews descargadas.
        print("Detectando soporte de reviews por fuente...")
        from scheduler import SOURCES, _load_source
        from sources.base import AbstractSource
        con_reviews = []
        for key in SOURCES.keys():
            try:
                src = _load_source(key)
                if type(src).download_reviews is not AbstractSource.download_reviews:
                    con_reviews.append(key)
            except Exception as e:
                print(f"  ⚠ no se pudo inspeccionar {key}: {e}")
        await conn.execute(
            "UPDATE fuentes_config SET has_reviews_support = (nombre = ANY($1::text[]))",
            con_reviews,
        )
        print(f"  {len(con_reviews)} fuentes con download_reviews: {sorted(con_reviews)}")

        print("Sincronización completada con éxito!")

    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
