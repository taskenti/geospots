"""Analiza reviews de 2 spots por categoria para detectar contenido inutil.

Objetivo: encontrar patrones que podemos eliminar antes de enviar al LLM:
  - Agradecimientos al municipio / propietario
  - Saludos y firmas
  - Repeticiones entre reviews
  - Frases banales sin contenido informativo
  - Datos que ya estan en SERVICIOS (precio, plazas, gratuito)
  - Spam de booking/marketing
"""
import asyncio
import os
import asyncpg
import sys
from collections import Counter


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
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "25433")
    name = os.environ.get("DB_NAME") or os.environ.get("POSTGRES_DB", "geospots")
    user = os.environ.get("DB_USER") or os.environ.get("POSTGRES_USER", "geospots")
    pw = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD", "geospots")
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


async def main():
    _load_dotenv()
    out_path = sys.argv[1] if len(sys.argv) > 1 else "docs/validation/review_waste_analysis.txt"
    sys.stdout = open(out_path, "w", encoding="utf-8")

    conn = await asyncpg.connect(dsn=_dsn())
    try:
        # 1. Distribucion de tipos
        print("=== DISTRIBUCION DE TIPOS ===\n")
        tipos = await conn.fetch("""
            SELECT tipo, COUNT(*) as n_spots, SUM(total_reviews) as n_reviews
            FROM spots WHERE activo=TRUE AND total_reviews >= 3
            GROUP BY tipo ORDER BY n_spots DESC
        """)
        for t in tipos:
            print(f"  {t['tipo']:<20} {t['n_spots']:>6} spots  {t['n_reviews']:>8} reviews")

        # 2. Para cada categoria, sacar 2 spots con reviews variadas (paises mezclados)
        # Elegimos spots con 10-30 reviews para tener volumen pero no caos
        print("\n\n=== MUESTRA: 2 SPOTS POR CATEGORIA ===")
        for t in tipos:
            tipo = t["tipo"]
            if not tipo:
                continue
            print(f"\n\n{'#'*80}\n# TIPO: {tipo}\n{'#'*80}")

            spots = await conn.fetch("""
                SELECT id, canonical_name, country_iso, total_reviews,
                       agua_potable, electricidad, vaciado_grises, vaciado_negras,
                       gratuito, num_plazas
                FROM spots
                WHERE tipo = $1 AND activo = TRUE
                  AND total_reviews BETWEEN 8 AND 25
                ORDER BY total_reviews DESC, random()
                LIMIT 2
            """, tipo)

            for s in spots:
                print(f"\n--- spot_id={s['id']} [{s['country_iso']}] '{s['canonical_name']}' "
                      f"({s['total_reviews']} reviews) "
                      f"gratuito={s['gratuito']} agua={s['agua_potable']} elec={s['electricidad']} ---")
                reviews = await conn.fetch("""
                    SELECT id, source, fecha, rating, idioma,
                           COALESCE(texto_limpio, texto, texto_original) AS texto
                    FROM reviews
                    WHERE spot_id = $1 AND COALESCE(texto_limpio, texto, texto_original) IS NOT NULL
                    ORDER BY fecha DESC NULLS LAST
                    LIMIT 12
                """, s["id"])
                for r in reviews:
                    fecha_str = r["fecha"].strftime("%Y-%m") if r["fecha"] else "?"
                    txt = (r["texto"] or "").replace("\n", " ").strip()
                    idioma = r.get("idioma") or "?"
                    print(f"  [{fecha_str}] [{r['source']}] [{idioma}] [{r['rating'] or '?'}/5] {txt}")

        # 3. Heuristicas globales: frases mas frecuentes en reviews
        # (Tomar muestra grande, normalizar, contar bigramas/frases)
        print("\n\n=== FRASES MAS COMUNES (top 30 ngrams) ===")
        sample = await conn.fetch("""
            SELECT COALESCE(texto_limpio, texto, texto_original) AS texto
            FROM reviews
            WHERE COALESCE(texto_limpio, texto, texto_original) IS NOT NULL
              AND length(COALESCE(texto_limpio, texto, texto_original)) BETWEEN 30 AND 500
            ORDER BY random() LIMIT 5000
        """)
        # Contar palabras frecuentes y frases cortas comunes
        word_counter = Counter()
        phrase_counter = Counter()
        for r in sample:
            txt = (r["texto"] or "").lower()
            # Palabras sueltas (>3 chars)
            for w in txt.split():
                w = w.strip(".,;:!?¡¿\"'()[]{}«»").lower()
                if 3 <= len(w) <= 20:
                    word_counter[w] += 1
            # Bigramas
            tokens = [w.strip(".,;:!?¡¿\"'()[]{}«»").lower() for w in txt.split() if len(w) > 2]
            for i in range(len(tokens) - 1):
                phrase_counter[(tokens[i], tokens[i+1])] += 1

        print("\nTop 40 palabras (excl. stopwords muy obvias):")
        STOP = {"the","and","this","that","with","very","for","but","not","you","are",
                "was","have","has","had","were","they","them","there","their",
                "para","con","muy","una","como","pero","esta","este","por","los","las",
                "des","les","une","des","est","pas","aux","dans","pour","sur","sans",
                "auf","mit","sehr","ist","der","die","das","von","ein","nicht",
                "che","una","del","della","con","per","sono","molto"}
        for w, n in word_counter.most_common(150):
            if w in STOP: continue
            print(f"  {n:>5} {w}")

        print("\nTop 50 bigramas (potenciales boilerplate):")
        for (a, b), n in phrase_counter.most_common(80):
            if a in STOP or b in STOP: continue
            if n < 5: break
            print(f"  {n:>5} '{a} {b}'")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
