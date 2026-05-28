"""Job mensual — listar y/o promover tags fuera de vocabulario (T1.5/T2.4).

Lee `unknown_tags`, ordena por frecuencia, emite reporte markdown para revisión
humana. Opcionalmente promueve un tag manualmente a `canonical_tags`.

Uso:
    # Solo listar top 20 unknowns sin promover
    docker-compose exec enrichment python -m jobs.review_unknown_tags --top 20

    # Promover un tag al vocabulario (crea canonical_id == tag, categoría opcional)
    docker-compose exec enrichment python -m jobs.review_unknown_tags --promote 'lakefront' --category location

    # Promover un tag como alias de un canonical existente
    docker-compose exec enrichment python -m jobs.review_unknown_tags --promote 'bouwput' --as-alias-of construction

    # Markar como reviewed=TRUE sin promover (ruido, errores tipográficos, etc.)
    docker-compose exec enrichment python -m jobs.review_unknown_tags --dismiss 'asdf123'

El comando devuelve exit 0 incluso sin cambios — está pensado para correr en
schedule. La salida de --top X siempre va a stdout (consumible desde cron + mail).
"""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg
from loguru import logger

from enrichment.tag_canonicalizer import (
    list_top_unknown,
    promote_unknown_to_canonical,
    invalidate_canonical_index,
)


async def _connect() -> asyncpg.Connection:
    dsn = os.environ.get("DATABASE_URL") or (
        f"postgresql://{os.environ.get('POSTGRES_USER','geospots')}:"
        f"{os.environ.get('POSTGRES_PASSWORD','geospots')}@"
        f"{os.environ.get('POSTGRES_HOST','db')}:"
        f"{os.environ.get('POSTGRES_PORT','5432')}/"
        f"{os.environ.get('POSTGRES_DB','geospots')}"
    )
    return await asyncpg.connect(dsn=dsn)


def _fmt_markdown(rows: list[dict]) -> str:
    if not rows:
        return "_No hay unknown_tags pendientes._\n"
    out = ["| tag | count | first_seen | last_seen |", "|---|---|---|---|"]
    for r in rows:
        out.append(
            f"| `{r['tag']}` | {r['occurrence_count']} | "
            f"{r['first_seen'].strftime('%Y-%m-%d')} | "
            f"{r['last_seen'].strftime('%Y-%m-%d')} |"
        )
    return "\n".join(out) + "\n"


async def run(args) -> int:
    conn = await _connect()
    try:
        if args.promote:
            await promote_unknown_to_canonical(
                conn, args.promote,
                canonical_id=args.as_alias_of,
                category=args.category,
            )
            logger.info(
                f"[unknown_tags] promovido '{args.promote}' "
                f"→ canonical='{args.as_alias_of or args.promote}' "
                f"category={args.category!r}"
            )
            invalidate_canonical_index()

        if args.dismiss:
            res = await conn.execute(
                "UPDATE unknown_tags SET reviewed = TRUE WHERE tag = $1",
                args.dismiss,
            )
            logger.info(f"[unknown_tags] descartado '{args.dismiss}' → {res}")

        rows = await list_top_unknown(conn, limit=args.top, reviewed=False)
        print(f"# Top {args.top} unknown tags (reviewed=FALSE)\n")
        print(_fmt_markdown(rows))
        return 0
    finally:
        await conn.close()


def main():
    p = argparse.ArgumentParser(description="Review unknown_tags (T1.5/T2.4).")
    p.add_argument("--top", type=int, default=20, help="Cuántos listar.")
    p.add_argument("--promote", type=str, default=None,
                   help="Tag a promover desde unknown_tags al vocabulario canónico.")
    p.add_argument("--as-alias-of", type=str, default=None,
                   help="Si se promueve, usar este canonical_id existente como destino (el tag se añade a aliases).")
    p.add_argument("--category", type=str, default=None,
                   help="Categoría opcional al crear un canonical nuevo.")
    p.add_argument("--dismiss", type=str, default=None,
                   help="Marca el tag como reviewed=TRUE sin promover (ruido).")
    args = p.parse_args()
    exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
