"""Regresion Sprint 6 -- BUG-12 tag canonicalization + BUG-34 total_records.

BUG-12: Tags almacenados en formato bruto del LLM ("dog friendly", "crowded",
        "dump station") deben normalizarse al canonical correcto.
BUG-34: sync_db.py debe incluir la actualizacion de total_records en
        source_credibility.

No toca DB: ejercita normalize_raw_tag y canonicalize_tag directamente
contra el indice construido en memoria con datos de prueba.

Ejecutar:  python -m tests.test_tags_sprint6
"""

from enrichment.tag_canonicalizer import normalize_raw_tag, canonicalize_tag


# Indice minimo que replica los casos problematicos reales de la DB
_TEST_INDEX = {
    # canonicals directos
    "dog-friendly": "dog-friendly",
    "no-services": "no-services",
    "dump-station": "dump-station",
    "busy": "busy",
    "exposed": "exposed",
    "free": "free",
    "quiet": "quiet",
    # aliases reales del vocabulario canonico
    "crowded": "busy",          # alias de busy
    "packed": "busy",
    "windy": "exposed",         # alias de exposed
    "wind-exposed": "exposed",
    "dogs-allowed": "dog-friendly",
    "pet-friendly": "dog-friendly",
    "no-amenities": "no-services",
    "grey-water": "dump-station",
    "vidange": "dump-station",
    "gratis": "free",
    "gratuit": "free",
    "peaceful": "quiet",
    "tranquilo": "quiet",
}


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # ── normalize_raw_tag: espacios/guiones bajos -> kebab-case ──────────────
    check(normalize_raw_tag("dog friendly") == "dog-friendly",
          "espacio debe convertirse a guion: 'dog friendly' -> 'dog-friendly'")
    check(normalize_raw_tag("dog-friendly") == "dog-friendly",
          "ya kebab debe permanecer igual: 'dog-friendly'")
    check(normalize_raw_tag("dump_station") == "dump-station",
          "guion bajo debe convertirse: 'dump_station' -> 'dump-station'")
    check(normalize_raw_tag("dump station") == "dump-station",
          "espacio debe convertirse: 'dump station' -> 'dump-station'")
    check(normalize_raw_tag("no services") == "no-services",
          "'no services' -> 'no-services'")
    check(normalize_raw_tag("DOG FRIENDLY") == "dog-friendly",
          "mayusculas deben normalizarse a kebab lowercase")
    check(normalize_raw_tag("  quiet  ") == "quiet",
          "espacios en bordes se eliminan")
    check(normalize_raw_tag("") == "",
          "cadena vacia devuelve cadena vacia")
    check(normalize_raw_tag(None) == "",
          "None devuelve cadena vacia")

    # ── canonicalize_tag: resolucion en indice ────────────────────────────────
    # variantes con espacio del LLM -> canonical correcto
    check(canonicalize_tag("dog friendly", _TEST_INDEX) == "dog-friendly",
          "'dog friendly' deberia resolver a 'dog-friendly' (BUG-12)")
    check(canonicalize_tag("dump station", _TEST_INDEX) == "dump-station",
          "'dump station' deberia resolver a 'dump-station' (BUG-12)")
    check(canonicalize_tag("no services", _TEST_INDEX) == "no-services",
          "'no services' deberia resolver a 'no-services' (BUG-12)")

    # aliases deben resolverse al canonical
    check(canonicalize_tag("crowded", _TEST_INDEX) == "busy",
          "alias 'crowded' debe resolver a canonical 'busy'")
    check(canonicalize_tag("windy", _TEST_INDEX) == "exposed",
          "alias 'windy' debe resolver a canonical 'exposed'")
    check(canonicalize_tag("pet-friendly", _TEST_INDEX) == "dog-friendly",
          "alias 'pet-friendly' debe resolver a 'dog-friendly'")
    check(canonicalize_tag("gratis", _TEST_INDEX) == "free",
          "alias 'gratis' debe resolver a 'free'")
    check(canonicalize_tag("tranquilo", _TEST_INDEX) == "quiet",
          "alias 'tranquilo' debe resolver a 'quiet'")

    # canonical directo
    check(canonicalize_tag("free", _TEST_INDEX) == "free",
          "canonical directo debe resolverse a si mismo")

    # tag fuera de vocabulario -> None (se descarta, no se inventa)
    check(canonicalize_tag("large pitches", _TEST_INDEX) is None,
          "tag fuera de vocabulario debe devolver None (no inventar canonical)")
    check(canonicalize_tag("friendly staff", _TEST_INDEX) is None,
          "'friendly staff' fuera de vocabulario debe devolver None")

    # ── BUG-34: sync_db.py incluye bloque total_records ──────────────────────
    # sync_db.py pertenece al contenedor scraper (no importable directamente
    # porque depende de 'config' que solo existe en ese entorno). Verificamos
    # el codigo fuente en bruto para confirmar que el bloque esta presente.
    import pathlib
    sync_db_src = pathlib.Path(__file__).parent.parent / "scraper" / "sync_db.py"
    src = sync_db_src.read_text(encoding="utf-8")
    check("total_records" in src,
          "sync_db.py debe actualizar total_records en source_credibility (BUG-34)")
    check("UPDATE source_credibility" in src,
          "sync_db.py debe tener UPDATE source_credibility con total_records")

    if failures:
        print(f"FALLOS ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK -- todos los casos de tags/sync de Sprint 6 pasan")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
