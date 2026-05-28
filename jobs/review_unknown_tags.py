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
from datetime import datetime, timezone

import asyncpg
from loguru import logger

from enrichment.tag_canonicalizer import (
    invalidate_canonical_index,
    list_top_unknown,
    load_canonical_index,
    promote_unknown_to_canonical,
    suggest_canonical,
    unknown_tags_stats,
)


async def _connect() -> asyncpg.Connection:
    # Reusa el resolver canónico de credenciales (carga .env + DB_HOST/PORT/...).
    # Antes este job tenía su propio fallback con password 'geospots' que fallaba
    # contra la DB real — bug corregido reusando worker._dsn (T2.4).
    from enrichment.worker import _dsn
    dsn = os.environ.get("DATABASE_URL") or _dsn()
    return await asyncpg.connect(dsn=dsn)


def _fmt_markdown(rows: list[dict], suggestions: dict[str, str] | None = None) -> str:
    if not rows:
        return "_No hay unknown_tags pendientes._\n"
    suggestions = suggestions or {}
    out = [
        "| tag | count | first_seen | last_seen | suggested canonical |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        sug = suggestions.get(r["tag"])
        sug_cell = f"`{sug}`" if sug else "—"
        out.append(
            f"| `{r['tag']}` | {r['occurrence_count']} | "
            f"{r['first_seen'].strftime('%Y-%m-%d')} | "
            f"{r['last_seen'].strftime('%Y-%m-%d')} | {sug_cell} |"
        )
    return "\n".join(out) + "\n"


def _fmt_header(stats: dict, top: int) -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"# Unknown tags — revisión mensual (T2.4)\n\n"
        f"_Generado {generated}_\n\n"
        f"- **Pendientes:** {stats.get('pending', 0)} tags "
        f"({stats.get('pending_occurrences', 0)} ocurrencias)\n"
        f"- **Ya revisados:** {stats.get('reviewed', 0)}\n"
        f"- **Total histórico:** {stats.get('total', 0)}\n\n"
        f"Mostrando top {top} por frecuencia. La columna *suggested canonical* es "
        f"una pista difusa (typos/variantes); verificar antes de promover.\n\n"
        f"Promover:  `python -m jobs.review_unknown_tags --promote '<tag>' "
        f"[--as-alias-of <canonical>] [--category <cat>]`\n"
        f"Descartar: `python -m jobs.review_unknown_tags --dismiss '<tag>'`\n"
    )


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

        stats = await unknown_tags_stats(conn)
        rows = await list_top_unknown(conn, limit=args.top, reviewed=False)

        # Sugerencia difusa de canonical por tag (acelera la revisión humana).
        suggestions: dict[str, str] = {}
        if rows:
            index = await load_canonical_index(conn)
            for r in rows:
                sug = suggest_canonical(r["tag"], index)
                if sug:
                    suggestions[r["tag"]] = sug

        report = _fmt_header(stats, args.top) + "\n" + _fmt_markdown(rows, suggestions)
        print(report)

        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(report)
            logger.info(f"[unknown_tags] reporte escrito en {args.out}")
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
    p.add_argument("--out", type=str, default=None,
                   help="Escribe también el reporte markdown a este fichero (archivo mensual).")
    args = p.parse_args()
    exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
