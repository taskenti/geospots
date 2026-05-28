import os
import psycopg2
from psycopg2.extras import DictCursor

def connect():
    return psycopg2.connect(
        dbname=os.environ.get("DB_NAME", "geospots"),
        user=os.environ.get("DB_USER", "geospots"),
        password=os.environ.get("DB_PASSWORD", "camperbot_local_dev_2026"),
        host=os.environ.get("DB_HOST", "db"),
        port=os.environ.get("DB_PORT", "5432")
    )

def audit():
    conn = connect()
    cur = conn.cursor(cursor_factory=DictCursor)
    
    # Total Spots
    cur.execute("SELECT count(*) as total, count(*) filter(where activo=true) as activos FROM spots")
    total_spots = cur.fetchone()
    total = total_spots['total']
    
    print(f"--- SPOTS TOTALES ---")
    print(f"Total Spots: {total}")
    print(f"Spots Activos: {total_spots['activos']} ({(total_spots['activos']/max(1, total)*100):.2f}%)")
    
    if total == 0:
        return

    # Descripciones y datos básicos
    q_desc = """
    SELECT 
        count(*) filter(where descripcion_es is not null and descripcion_es != '') as desc_es,
        count(*) filter(where descripcion_en is not null and descripcion_en != '') as desc_en,
        count(*) filter(where master_rating is not null) as has_rating,
        count(*) filter(where num_fuentes > 1) as multi_fuentes,
        sum(total_reviews) as total_reviews_sum
    FROM spots
    """
    cur.execute(q_desc)
    desc = cur.fetchone()
    
    print(f"\n--- COMPLETITUD BÁSICA ---")
    print(f"Con Descripción (ES): {desc['desc_es']} ({(desc['desc_es']/total*100):.2f}%)")
    print(f"Con Descripción (EN): {desc['desc_en']} ({(desc['desc_en']/total*100):.2f}%)")
    print(f"Con Master Rating: {desc['has_rating']} ({(desc['has_rating']/total*100):.2f}%)")
    print(f"Con >1 Fuente: {desc['multi_fuentes']} ({(desc['multi_fuentes']/total*100):.2f}%)")
    print(f"Total Reviews acumuladas en spots: {desc['total_reviews_sum']}")

    # Servicios
    q_servicios = """
    SELECT 
        count(*) filter(where piscina = true) as piscina,
        count(*) filter(where lavanderia = true) as lavanderia,
        count(*) filter(where gas_recharge = true) as gas_recharge,
        count(*) filter(where restaurant = true) as restaurant,
        count(*) filter(where online_booking = true) as online_booking,
        count(*) filter(where winter_friendly = true) as winter_friendly
    FROM spots
    """
    cur.execute(q_servicios)
    servicios = cur.fetchone()
    print(f"\n--- SERVICIOS Y AMENITIES (VERIFICADOS TRUE) ---")
    for k, v in dict(servicios).items():
        print(f"{k}: {v} ({(v/total*100):.2f}%)")
        
    # Otras tablas
    print(f"\n--- DATOS ASOCIADOS (OTRAS TABLAS) ---")
    
    cur.execute("SELECT count(*) FROM source_records")
    print(f"Total Source Records: {cur.fetchone()[0]}")
    
    cur.execute("SELECT count(*) FROM reviews")
    print(f"Total Reviews Extraídas: {cur.fetchone()[0]}")
    
    cur.execute("SELECT count(*) FROM spot_enrichments")
    print(f"Total Spots Enriquecidos (LLM): {cur.fetchone()[0]}")
    
    cur.execute("SELECT count(*) FROM raw_payloads")
    print(f"Total Raw Payloads (json original): {cur.fetchone()[0]}")
    
    cur.execute("SELECT status, count(*) FROM enrichment_queue GROUP BY status")
    eq = cur.fetchall()
    print(f"\nEnrichment Queue:")
    for row in eq:
        print(f"- {row['status']}: {row['count']}")
        
    cur.execute("SELECT status, count(*) FROM scrape_queue GROUP BY status")
    sq = cur.fetchall()
    print(f"\nScrape Queue:")
    for row in sq:
        print(f"- {row['status']}: {row['count']}")

if __name__ == '__main__':
    audit()
