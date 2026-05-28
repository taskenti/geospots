SELECT '--- SPOTS TOTALES ---' as "Seccion";
SELECT 
    count(*) as total, 
    count(*) filter(where activo=true) as activos,
    ROUND(count(*) filter(where activo=true) * 100.0 / NULLIF(count(*), 0), 2) as activos_pct
FROM spots;

SELECT '--- COMPLETITUD BÁSICA ---' as "Seccion";
SELECT 
    count(*) filter(where descripcion_es is not null and descripcion_es != '') as desc_es,
    ROUND(count(*) filter(where descripcion_es is not null and descripcion_es != '') * 100.0 / NULLIF(count(*), 0), 2) as desc_es_pct,
    count(*) filter(where descripcion_en is not null and descripcion_en != '') as desc_en,
    ROUND(count(*) filter(where descripcion_en is not null and descripcion_en != '') * 100.0 / NULLIF(count(*), 0), 2) as desc_en_pct,
    count(*) filter(where master_rating is not null) as has_rating,
    ROUND(count(*) filter(where master_rating is not null) * 100.0 / NULLIF(count(*), 0), 2) as has_rating_pct,
    count(*) filter(where num_fuentes > 1) as multi_fuentes,
    ROUND(count(*) filter(where num_fuentes > 1) * 100.0 / NULLIF(count(*), 0), 2) as multi_fuentes_pct,
    sum(total_reviews) as total_reviews_sum
FROM spots;

SELECT '--- SERVICIOS Y AMENITIES (VERIFICADOS TRUE) ---' as "Seccion";
SELECT 
    count(*) filter(where piscina = true) as piscina,
    ROUND(count(*) filter(where piscina = true) * 100.0 / NULLIF(count(*), 0), 2) as piscina_pct,
    count(*) filter(where lavanderia = true) as lavanderia,
    ROUND(count(*) filter(where lavanderia = true) * 100.0 / NULLIF(count(*), 0), 2) as lav_pct,
    count(*) filter(where gas_recharge = true) as gas_recharge,
    ROUND(count(*) filter(where gas_recharge = true) * 100.0 / NULLIF(count(*), 0), 2) as gas_pct,
    count(*) filter(where restaurant = true) as restaurant,
    ROUND(count(*) filter(where restaurant = true) * 100.0 / NULLIF(count(*), 0), 2) as rest_pct,
    count(*) filter(where online_booking = true) as online_booking,
    count(*) filter(where winter_friendly = true) as winter_friendly
FROM spots;

SELECT '--- DATOS ASOCIADOS (OTRAS TABLAS) ---' as "Seccion";
SELECT 'Total Source Records' as desc, count(*) FROM source_records
UNION ALL
SELECT 'Total Reviews Extraídas', count(*) FROM reviews
UNION ALL
SELECT 'Total Spots Enriquecidos (LLM)', count(*) FROM spot_enrichments
UNION ALL
SELECT 'Total Raw Payloads', count(*) FROM raw_payloads;

SELECT '--- ENRICHMENT QUEUE ---' as "Seccion";
SELECT status, count(*) FROM enrichment_queue GROUP BY status;

SELECT '--- SCRAPE QUEUE ---' as "Seccion";
SELECT status, count(*) FROM scrape_queue GROUP BY status;
