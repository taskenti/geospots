"""Regresion -- toda fuente debe aceptar job_id en run() y download_reviews().

Blindaje de Sprint 2. El scheduler invoca SIEMPRE:
    source.run(pool, config, log_id, job_id=job_id)
    source.download_reviews(pool, config, job_id=job_id)

Si una fuente sobreescribe cualquiera de los dos metodos sin el parametro
job_id, el job revienta con TypeError y termina con errores:1 sin scrapear
nada (este fue el bug masivo que afecto a 23 fuentes). Este test introspecciona
las firmas de TODAS las fuentes del registro SOURCES y falla si alguna no acepta
job_id, evitando que el bug reincida al anadir o editar una fuente.

NO toca DB ni red: solo carga las clases e inspecciona firmas.

Ejecutar:  python -m tests.test_source_signatures   (desde la raiz del repo)
"""

import inspect
import os
import sys

# scraper/ debe estar en sys.path porque las fuentes hacen
# `from sources.base import AbstractSource`.
_SCRAPER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scraper")
if _SCRAPER_DIR not in sys.path:
    sys.path.insert(0, _SCRAPER_DIR)

from scheduler import SOURCES, _load_source  # noqa: E402


def _accepts_job_id(method) -> bool:
    """True si la firma acepta el kwarg job_id (explicito o via **kwargs)."""
    sig = inspect.signature(method)
    params = sig.parameters
    if "job_id" in params:
        return True
    # **kwargs tambien lo absorberia
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def main() -> int:
    failures: list[str] = []

    for key in SOURCES:
        try:
            source = _load_source(key)
        except Exception as e:  # pragma: no cover - import roto = fallo real
            failures.append(f"{key}: no se pudo cargar la clase ({e})")
            continue

        for method_name in ("run", "download_reviews"):
            method = getattr(source, method_name)
            if not _accepts_job_id(method):
                sig = inspect.signature(method)
                failures.append(
                    f"{key}.{method_name}{sig} NO acepta job_id "
                    f"(el scheduler lo pasa como kwarg -> TypeError)"
                )

    if failures:
        print(f"FALLOS ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK -- las {len(SOURCES)} fuentes aceptan job_id en run() y download_reviews()")
    return 0


if __name__ == "__main__":
    sys.exit(main())
