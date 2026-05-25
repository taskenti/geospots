# -*- coding: utf-8 -*-
"""
StayFree Spots Scraper v2
- maxResults=100 (limite del servidor)
- Paginacion por offset para superar el limite
- Un spotType a la vez para evitar bloqueos
"""

import sys
import requests
import json
import time
import os
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

# ── CONFIGURACION ─────────────────────────────────────────────────────────────

SPOT_TYPES = [
    "WILD_SPOT",
    "PARKING_FREE",
    "CAMPING",
    "PARKING_CAMPER",
    "PARKING_CAMPER_ACS",
    "CAMPING_ACS",
    "CAMPING_PRIVATE",
    "AGROTOURISM",
]

MAX_RESULTS = 100  # Limite real del servidor
DELAY = 2.0        # Segundos entre peticiones

COUNTRIES = [
    "ES", "FR", "PT", "IT", "DE", "AT", "CH", "BE", "NL", "LU",
    "GB", "IE", "DK", "SE", "NO", "FI", "IS",
    "PL", "CZ", "SK", "HU", "RO", "BG", "HR", "SI", "RS", "BA",
    "GR", "TR", "CY", "MT",
    "EE", "LV", "LT",
    "AL", "MK", "ME",
    "MA", "TN",
]

HEADERS = {
    "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36",
    "accept": "*/*",
    "accept-language": "es-ES,es;q=0.9",
    "referer": "https://www.stayfree.app/es/campspots/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

BASE_URL = "https://www.stayfree.app/api/spots"

# ── FETCH CON PAGINACION ──────────────────────────────────────────────────────

def fetch_all_spots_for(session, country, spot_type):
    """Pagina hasta agotar resultados o alcanzar 10 paginas."""
    all_spots = []
    page = 0

    while True:
        params = {
            "spotType": spot_type,
            "maxResults": MAX_RESULTS,
            "locale": "es",
            "sort": "rating",
            "locationCountry": country,
            "page": page,
        }
        try:
            resp = session.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
            if resp.status_code == 503:
                print(f"    503 en pagina {page}, esperando 5s...")
                time.sleep(5)
                resp = session.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                batch = data
            elif isinstance(data, dict):
                batch = data.get("spots", data.get("data", data.get("results", [])))
            else:
                batch = []

            if not batch:
                break

            all_spots.extend(batch)

            # Si devuelve menos de maxResults, no hay mas paginas
            if len(batch) < MAX_RESULTS:
                break

            page += 1
            if page >= 20:  # Tope de seguridad: 20 paginas x 100 = 2000 spots por tipo/pais
                print(f"    Tope de 20 paginas alcanzado")
                break

            time.sleep(DELAY)

        except requests.HTTPError as e:
            print(f"    ERROR HTTP {e.response.status_code} (p{page})")
            break
        except Exception as e:
            print(f"    ERROR: {e}")
            break

    return all_spots


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("output", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"output/stayfree_spots_{timestamp}.json"

    session = requests.Session()

    # Clave: spot_id para deduplicar spots que aparezcan en varios tipos
    all_spots_by_id = {}
    total_requests = 0

    print(f"Iniciando extraccion: {len(COUNTRIES)} paises x {len(SPOT_TYPES)} tipos\n")

    for country in COUNTRIES:
        country_count_before = len(all_spots_by_id)
        print(f"[{country}]")
        for spot_type in SPOT_TYPES:
            spots = fetch_all_spots_for(session, country, spot_type)
            new = 0
            for s in spots:
                sid = s.get("_id")
                if sid and sid not in all_spots_by_id:
                    all_spots_by_id[sid] = s
                    new += 1
            total_requests += 1
            if spots:
                print(f"  {spot_type}: {len(spots)} spots ({new} nuevos)")
            time.sleep(DELAY)

        added = len(all_spots_by_id) - country_count_before
        print(f"  -> {added} spots unicos nuevos en {country} | Total acumulado: {len(all_spots_by_id)}\n")

        # Guardar progreso tras cada pais
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(list(all_spots_by_id.values()), f, ensure_ascii=False, indent=2)

    print("=" * 50)
    print(f"TOTAL FINAL: {len(all_spots_by_id)} spots unicos")
    print(f"Peticiones realizadas: {total_requests}")
    print(f"Guardado en: {output_file}")


if __name__ == "__main__":
    main()
