"""Busca un spot con servicios variados Y reviews para validar v3 con SERVICIOS."""
import asyncio
import os
import asyncpg


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
    conn = await asyncpg.connect(dsn=_dsn())
    try:
        rows = await conn.fetch(
            """
            SELECT id, canonical_name, country_iso, tipo, total_reviews,
                   agua_potable, electricidad, vaciado_grises, vaciado_negras,
                   ducha, wifi, wc_publico, gratuito, num_plazas, altura_max_m
            FROM spots
            WHERE country_iso IN ('es','it','fr','pt')
              AND total_reviews BETWEEN 5 AND 25
              AND agua_potable = TRUE
              AND electricidad = TRUE
              AND (vaciado_grises = TRUE OR vaciado_negras = TRUE)
              AND gratuito = FALSE
              AND activo = TRUE
            ORDER BY total_reviews DESC
            LIMIT 5
            """
        )
        for r in rows:
            print(f"id={r['id']:>6}  reviews={r['total_reviews']:>2}  [{r['country_iso']}] "
                  f"{r['tipo']:<12} agua/elec/grises/negras/ducha/wifi/wc = "
                  f"{r['agua_potable']}/{r['electricidad']}/{r['vaciado_grises']}/{r['vaciado_negras']}/"
                  f"{r['ducha']}/{r['wifi']}/{r['wc_publico']}  plazas={r['num_plazas']} h={r['altura_max_m']} "
                  f"  {r['canonical_name'][:50]}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
