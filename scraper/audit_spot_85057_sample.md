# Auditoría de Datos: Spot ID 85057 (Grau Roig)

Este reporte detalla el flujo de datos completo para un spot real antes, durante y después de la fase de enriquecimiento semántico con LLM.

## 1. Datos Crudos en BD (Antes del Enriquecimiento)
Estos son los atributos físicos y metadatos estructurados consolidados en la tabla `spots`:
```json
{
  "id": 85057,
  "canonical_name": "Grau Roig",
  "lat": 42.53200521,
  "lon": 1.69753624,
  "geog": "0101000020E61000006932A9C21B29FB3F352029BF18444540",
  "geohash7": "WZQlyorW",
  "country_iso": "ad",
  "region": "Encamp",
  "tipo": "area_ac",
  "gratuito": true,
  "precio_info": "min: 0 / max: 0",
  "agua_potable": false,
  "vaciado_negras": false,
  "vaciado_grises": false,
  "electricidad": false,
  "ducha": false,
  "wifi": true,
  "wc_publico": true,
  "perros": true,
  "num_plazas": 20,
  "iluminacion": true,
  "seguridad": false,
  "master_rating": 3.0899999141693115,
  "total_reviews": 23,
  "fuentes": [
    "campercontact"
  ],
  "num_fuentes": 1,
  "descripcion_es": "Aparcamiento mixto a 2110 m de altitud, cerca del telesilla. No se trata de un alojamiento oficial, por lo que pernoctar es bajo su propia responsabilidad. Centro de Encamp, a 19 km.",
  "descripcion_en": "Mixed parking at an altitude of 2110m - near the ski lift - this is not an official overnight stay and therefore staying overnight is at your own risk - Encamp center 19km\n",
  "descripcion_fr": "Parking mixte \u00e0 2110 m d'altitude - \u00e0 proximit\u00e9 des remont\u00e9es m\u00e9caniques - ce n'est pas un lieu d'h\u00e9bergement officiel et vous y passerez donc la nuit \u00e0 vos risques et p\u00e9rils - Centre d'Encamp \u00e0 19 km",
  "descripcion_de": "Gemischter Parkplatz auf 2110 m H\u00f6he \u2013 in der N\u00e4he des Skilifts \u2013 Dies ist keine offizielle \u00dcbernachtungsm\u00f6glichkeit, daher erfolgt die \u00dcbernachtung auf eigene Gefahr \u2013 Zentrum Encamp 19 km\n",
  "descripcion_it": "Parcheggio misto a 2110 m di altitudine, vicino all'impianto di risalita. Questo non \u00e8 un luogo di pernottamento ufficiale e pertanto il pernottamento \u00e8 a vostro rischio e pericolo. Centro di Encamp a 19 km.",
  "descripcion_nl": "mix-parking op 2110m hoogte - bij skilift - dit is geen offici\u00eble overnachtingsplaats en overnachten is dus op eigen risico - centrum Encamp 19km\n",
  "web": "http://www.grandvalira.com/en?e=invierno",
  "telefono": "+367801060",
  "email": "info@grandvalira.com",
  "fotos_urls": "[\"https://images.campercontact.com/media/photos/4222124650708394.jpg\", \"https://images.campercontact.com/media/photos/4222124650718103.jpg\", \"https://images.campercontact.com/media/photos/4222124650708396.jpg\", \"https://images.campercontact.com/media/photos/4222124650708395.jpg\", \"https://images.campercontact.com/media/photos/4222124650708393.jpeg\"]",
  "activo": true,
  "verificado": false,
  "conflictos": "[]",
  "confidence": 0.5,
  "created_at": "2026-05-22T09:07:30.536510+00:00",
  "updated_at": "2026-05-28T03:29:18.876486+00:00",
  "continent": "Europe",
  "subregion": "Southern Europe",
  "servicios_extras": "{\"terrain\": [\"illuminated\", \"hardened\", \"onMixParking\", \"wildCamping\"], \"amenity_pricing\": {\"toilet\": \"unknown\", \"internet\": \"unknown\", \"dogsAllowed\": \"unknown\", \"campingBehaviour\": \"unknown\"}}",
  "popularity_score": 0.5049999952316284,
  "reliability_score": 0.7599999904632568
}
```

## 2. Prompt de Entrada Generado para el LLM
Esta es la representación exacta en texto que se envía al LLM en el User Prompt (incluye descripciones multilingües y las reviews seleccionadas bajo presupuesto de tokens y ordenadas por relevancia temporal):
```text
SPOT id=85057
Name: "Grau Roig"
Type: area_ac
Region: Encamp
Country: ad
Coords: 42.5320, 1.6975
Sources: campercontact

SERVICES (structured facts from sources — do not re-infer):
  Free: Yes | Price info: min: 0 / max: 0
  Drinking water: No | Grey water dump: No | Black water dump: No
  Electricity: No | Shower: No | Public WC: Yes | Wifi: Yes
  Dogs allowed: Yes | Night lighting: Yes | On-site security: No
  Pitches: ~20
  Web: http://www.grandvalira.com/en?e=invierno | Phone: +367801060 | Email: info@grandvalira.com

DESCRIPTIONS (in original language):
[ES] Aparcamiento mixto a 2110 m de altitud, cerca del telesilla. No se trata de un alojamiento oficial, por lo que pernoctar es bajo su propia responsabilidad. Centro de Encamp, a 19 km.
[EN] Mixed parking at an altitude of 2110m - near the ski lift - this is not an official overnight stay and therefore staying overnight is at your own risk - Encamp center 19km
[FR] Parking mixte à 2110 m d'altitude - à proximité des remontées mécaniques - ce n'est pas un lieu d'hébergement officiel et vous y passerez donc la nuit à vos risques et périls - Centre d'Encamp à 19 km
[DE] Gemischter Parkplatz auf 2110 m Höhe – in der Nähe des Skilifts – Dies ist keine offizielle Übernachtungsmöglichkeit, daher erfolgt die Übernachtung auf eigene Gefahr – Zentrum Encamp 19 km
[IT] Parcheggio misto a 2110 m di altitudine, vicino all'impianto di risalita. Questo non è un luogo di pernottamento ufficiale e pertanto il pernottamento è a vostro rischio e pericolo. Centro di Encamp a 19 km.
[NL] mix-parking op 2110m hoogte - bij skilift - dit is geen officiële overnachtingsplaats en overnachten is dus op eigen risico - centrum Encamp 19km

REVIEWS (n=3, ordered by temporal relevance):
[review_id=172992] [2026-04] [campercontact] ★★★★★ parfait. endroit trés calme. autorisé par panneau signalitique.vu imprenable .dormir au calme
[review_id=172994] [2025-06] [campercontact] ★★ Niet geweldig, het hele gebied is in aanbouw.
[review_id=172993] [2025-07] [campercontact] ★ Dit deel is op dit moment (juli 2025) één grote bouwput. Ik denk niet eens dat ik er naar toe had kunnen rijden als ik het gewild had.

SUMMARY_RICHNESS: medium
SUMMARY_INSTRUCTION: Generate a 3-5 sentence summary covering services, atmosphere, and any notable considerations.
```

## 3. Estado Semántico Consolidado (Después del Enriquecimiento)
Esta es la fila resultante guardada en la tabla `spot_semantic_state` que consume la API y los buscadores vectoriales:
```json
{
  "spot_id": 85057,
  "quietness_score": 0.8999999761581421,
  "safety_score": null,
  "police_risk_score": null,
  "beauty_score": 0.800000011920929,
  "crowd_level_score": null,
  "overnight_safe": true,
  "stealth_score": null,
  "signals_data": "{\"beauty\": {\"score\": 0.8, \"confidence\": 0.139899, \"n_observations\": 1, \"weight_support\": 0.699494}, \"quietness\": {\"score\": 0.9, \"confidence\": 0.276824, \"n_observations\": 2, \"weight_support\": 1.384118}, \"dog_friendly\": {\"score\": true, \"confidence\": 1.0, \"n_observations\": 1, \"weight_support\": 0.84997}, \"water_working\": {\"score\": false, \"confidence\": 1.0, \"n_observations\": 1, \"weight_support\": 0.89902}, \"overnight_safe\": {\"score\": true, \"confidence\": 1.0, \"n_observations\": 2, \"weight_support\": 1.113756}, \"wild_camping_legal\": {\"score\": true, \"confidence\": 1.0, \"n_observations\": 1, \"weight_support\": 0.675123}, \"electricity_working\": {\"score\": false, \"confidence\": 1.0, \"n_observations\": 1, \"weight_support\": 0.89902}, \"dump_station_working\": {\"score\": false, \"confidence\": 1.0, \"n_observations\": 1, \"weight_support\": 0.89902}}",
  "semantic_dsl": "quiet:+0.9 beauty:+0.8 overnight:T",
  "summary_es": null,
  "summary_en": "Grau Roig is a free mixed parking area at 2110m altitude near a ski lift in Andorra. It is not an official overnight spot, but a sign authorizes camping and it is described as very quiet with great views. However, recent reviews (2025) indicate the area is under heavy construction, making access difficult. Services are minimal: no water, electricity, or dump station, but dogs are allowed and there is public WC and WiFi.",
  "tags": [
    "free",
    "mountain",
    "ski area",
    "construction",
    "quiet",
    "dog-friendly",
    "no services"
  ],
  "best_for": [
    "winter sports",
    "overlanding"
  ],
  "best_season": "winter",
  "total_observations": 10,
  "consensus_confidence": 0.370976060628891,
  "weight_support": 7.419521331787109,
  "last_aggregated_at": "2026-05-28T02:36:44.580651+00:00",
  "last_snapshot_data": "{\"beauty\": {\"score\": 0.8, \"confidence\": 0.139899, \"n_observations\": 1, \"weight_support\": 0.699494}, \"quietness\": {\"score\": 0.9, \"confidence\": 0.276824, \"n_observations\": 2, \"weight_support\": 1.384118}, \"dog_friendly\": {\"score\": true, \"confidence\": 1.0, \"n_observations\": 1, \"weight_support\": 0.84997}, \"water_working\": {\"score\": false, \"confidence\": 1.0, \"n_observations\": 1, \"weight_support\": 0.89902}, \"overnight_safe\": {\"score\": true, \"confidence\": 1.0, \"n_observations\": 2, \"weight_support\": 1.113756}, \"wild_camping_legal\": {\"score\": true, \"confidence\": 1.0, \"n_observations\": 1, \"weight_support\": 0.675123}, \"electricity_working\": {\"score\": false, \"confidence\": 1.0, \"n_observations\": 1, \"weight_support\": 0.89902}, \"dump_station_working\": {\"score\": false, \"confidence\": 1.0, \"n_observations\": 1, \"weight_support\": 0.89902}}",
  "stale": false,
  "created_at": "2026-05-27T04:49:53.045772+00:00",
  "updated_at": "2026-05-28T02:36:44.580651+00:00",
  "enrichment_version": 4,
  "llm_model": "deepseek-chat",
  "last_observation_at": "2026-04-20T00:00:00+00:00",
  "noise_sources": null,
  "parking_capacity": null,
  "cell_coverage": null,
  "wild_camping_legal": true,
  "avoid_season": "summer"
}
```

## 4. Claims Extraídos por el LLM (Historial)
Estos son los hechos individuales extraídos por el LLM en la tabla `extracted_claims` (citan la review de origen y el fragmento textual literal original):
```json
[
  {
    "id": 386178,
    "review_id": null,
    "spot_id": 85057,
    "signal_type": "dump_station_working",
    "raw_value": "False",
    "extraction_confidence": 0.8999999761581421,
    "extractor_name": "deepseek_spot_v2",
    "extractor_version": "v4",
    "pipeline_run_id": "10194392-74ae-44d2-a696-9e8e3408f9c4",
    "excerpt": "Grey water dump: No",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 386177,
    "review_id": null,
    "spot_id": 85057,
    "signal_type": "electricity_working",
    "raw_value": "False",
    "extraction_confidence": 0.8999999761581421,
    "extractor_name": "deepseek_spot_v2",
    "extractor_version": "v4",
    "pipeline_run_id": "10194392-74ae-44d2-a696-9e8e3408f9c4",
    "excerpt": "Electricity: No",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 386176,
    "review_id": null,
    "spot_id": 85057,
    "signal_type": "water_working",
    "raw_value": "False",
    "extraction_confidence": 0.8999999761581421,
    "extractor_name": "deepseek_spot_v2",
    "extractor_version": "v4",
    "pipeline_run_id": "10194392-74ae-44d2-a696-9e8e3408f9c4",
    "excerpt": "Drinking water: No",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 386175,
    "review_id": null,
    "spot_id": 85057,
    "signal_type": "dog_friendly",
    "raw_value": "True",
    "extraction_confidence": 0.8500000238418579,
    "extractor_name": "deepseek_spot_v2",
    "extractor_version": "v4",
    "pipeline_run_id": "10194392-74ae-44d2-a696-9e8e3408f9c4",
    "excerpt": "Dogs allowed: Yes",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 386174,
    "review_id": 172992,
    "spot_id": 85057,
    "signal_type": "overnight_safe",
    "raw_value": "True",
    "extraction_confidence": 0.699999988079071,
    "extractor_name": "deepseek_spot_v2",
    "extractor_version": "v4",
    "pipeline_run_id": "10194392-74ae-44d2-a696-9e8e3408f9c4",
    "excerpt": "dormir au calme",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 386173,
    "review_id": 172992,
    "spot_id": 85057,
    "signal_type": "beauty",
    "raw_value": "0.8",
    "extraction_confidence": 0.699999988079071,
    "extractor_name": "deepseek_spot_v2",
    "extractor_version": "v4",
    "pipeline_run_id": "10194392-74ae-44d2-a696-9e8e3408f9c4",
    "excerpt": "vu imprenable",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 386172,
    "review_id": 172992,
    "spot_id": 85057,
    "signal_type": "wild_camping_legal",
    "raw_value": "True",
    "extraction_confidence": 0.699999988079071,
    "extractor_name": "deepseek_spot_v2",
    "extractor_version": "v4",
    "pipeline_run_id": "10194392-74ae-44d2-a696-9e8e3408f9c4",
    "excerpt": "autoris\u00e9 par panneau signalitique",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 386171,
    "review_id": 172992,
    "spot_id": 85057,
    "signal_type": "quietness",
    "raw_value": "0.9",
    "extraction_confidence": 0.800000011920929,
    "extractor_name": "deepseek_spot_v2",
    "extractor_version": "v4",
    "pipeline_run_id": "10194392-74ae-44d2-a696-9e8e3408f9c4",
    "excerpt": "endroit tr\u00e9s calme",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 46342,
    "review_id": 172992,
    "spot_id": 85057,
    "signal_type": "overnight_safe",
    "raw_value": "true",
    "extraction_confidence": 0.8600000143051147,
    "extractor_name": "regex_v1",
    "extractor_version": "phase3-2026-05-23",
    "pipeline_run_id": "1c8057e8-bbe7-4fa4-80c3-41d538a7fea9",
    "excerpt": "par panneau signalitique.vu imprenable .dormir au calme",
    "created_at": "2026-05-27T04:49:53.045772+00:00"
  },
  {
    "id": 46341,
    "review_id": 172992,
    "spot_id": 85057,
    "signal_type": "quietness",
    "raw_value": "0.9",
    "extraction_confidence": 0.8600000143051147,
    "extractor_name": "regex_v1",
    "extractor_version": "phase3-2026-05-23",
    "pipeline_run_id": "1c8057e8-bbe7-4fa4-80c3-41d538a7fea9",
    "excerpt": "parfait. endroit tr\u00e9s calme. autoris\u00e9 par panneau signalitique.vu",
    "created_at": "2026-05-27T04:49:53.045772+00:00"
  }
]
```

## 5. Observaciones Normalizadas en la BD
Estos son los valores de señal normalizados en la tabla `normalized_observations` (con cálculo de pesos y asignación de confianza por fuente y extractor):
```json
[
  {
    "id": 377475,
    "claim_id": 386178,
    "spot_id": 85057,
    "signal_type": "dump_station_working",
    "value_num": null,
    "value_bool": false,
    "value_text": null,
    "extraction_confidence": 0.8999999761581421,
    "source_confidence": 1.0,
    "reviewer_confidence": 1.0,
    "observation_weight": 0.8999999761581421,
    "observed_at": "2026-05-28T00:20:57.983684+00:00",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 377474,
    "claim_id": 386177,
    "spot_id": 85057,
    "signal_type": "electricity_working",
    "value_num": null,
    "value_bool": false,
    "value_text": null,
    "extraction_confidence": 0.8999999761581421,
    "source_confidence": 1.0,
    "reviewer_confidence": 1.0,
    "observation_weight": 0.8999999761581421,
    "observed_at": "2026-05-28T00:20:57.982786+00:00",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 377473,
    "claim_id": 386176,
    "spot_id": 85057,
    "signal_type": "water_working",
    "value_num": null,
    "value_bool": false,
    "value_text": null,
    "extraction_confidence": 0.8999999761581421,
    "source_confidence": 1.0,
    "reviewer_confidence": 1.0,
    "observation_weight": 0.8999999761581421,
    "observed_at": "2026-05-28T00:20:57.981881+00:00",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 377472,
    "claim_id": 386175,
    "spot_id": 85057,
    "signal_type": "dog_friendly",
    "value_num": null,
    "value_bool": true,
    "value_text": null,
    "extraction_confidence": 0.8500000238418579,
    "source_confidence": 1.0,
    "reviewer_confidence": 1.0,
    "observation_weight": 0.8500000238418579,
    "observed_at": "2026-05-28T00:20:57.980315+00:00",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 377471,
    "claim_id": 386174,
    "spot_id": 85057,
    "signal_type": "overnight_safe",
    "value_num": null,
    "value_bool": true,
    "value_text": null,
    "extraction_confidence": 0.699999988079071,
    "source_confidence": 1.0,
    "reviewer_confidence": 1.0,
    "observation_weight": 0.699999988079071,
    "observed_at": "2026-04-20T00:00:00+00:00",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 377470,
    "claim_id": 386173,
    "spot_id": 85057,
    "signal_type": "beauty",
    "value_num": 0.800000011920929,
    "value_bool": null,
    "value_text": null,
    "extraction_confidence": 0.699999988079071,
    "source_confidence": 1.0,
    "reviewer_confidence": 1.0,
    "observation_weight": 0.699999988079071,
    "observed_at": "2026-04-20T00:00:00+00:00",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 377469,
    "claim_id": 386172,
    "spot_id": 85057,
    "signal_type": "wild_camping_legal",
    "value_num": null,
    "value_bool": true,
    "value_text": null,
    "extraction_confidence": 0.699999988079071,
    "source_confidence": 1.0,
    "reviewer_confidence": 1.0,
    "observation_weight": 0.699999988079071,
    "observed_at": "2026-04-20T00:00:00+00:00",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 377468,
    "claim_id": 386171,
    "spot_id": 85057,
    "signal_type": "quietness",
    "value_num": 0.8999999761581421,
    "value_bool": null,
    "value_text": null,
    "extraction_confidence": 0.800000011920929,
    "source_confidence": 1.0,
    "reviewer_confidence": 1.0,
    "observation_weight": 0.800000011920929,
    "observed_at": "2026-04-20T00:00:00+00:00",
    "created_at": "2026-05-28T00:20:57.834033+00:00"
  },
  {
    "id": 46337,
    "claim_id": 46342,
    "spot_id": 85057,
    "signal_type": "overnight_safe",
    "value_num": null,
    "value_bool": true,
    "value_text": null,
    "extraction_confidence": 0.8600000143051147,
    "source_confidence": 0.800000011920929,
    "reviewer_confidence": 1.0,
    "observation_weight": 0.6880000233650208,
    "observed_at": "2026-04-20T00:00:00+00:00",
    "created_at": "2026-05-27T04:49:53.045772+00:00"
  },
  {
    "id": 46336,
    "claim_id": 46341,
    "spot_id": 85057,
    "signal_type": "quietness",
    "value_num": 0.8999999761581421,
    "value_bool": null,
    "value_text": null,
    "extraction_confidence": 0.8600000143051147,
    "source_confidence": 0.800000011920929,
    "reviewer_confidence": 1.0,
    "observation_weight": 0.6880000233650208,
    "observed_at": "2026-04-20T00:00:00+00:00",
    "created_at": "2026-05-27T04:49:53.045772+00:00"
  }
]
```
