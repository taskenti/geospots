import asyncio
import os
import sys
import json
sys.path.append(os.path.dirname(__file__))
from config import Config
import asyncpg
# Add enrichment folder to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from enrichment.spot_packager import fetch_spot_for_enrichment, fetch_reviews_for_enrichment, select_reviews_for_prompt
from enrichment.prompts import build_spot_user_prompt

async def main():
    cfg = Config.from_env()
    pool = await asyncpg.create_pool(cfg.db_dsn)
    
    spot_id = 85057 # Grau Roig in Andorra (enriched in the last run)
    
    async with pool.acquire() as conn:
        # 1. Fetch raw spot data (BEFORE enrichment)
        spot_raw = await conn.fetchrow("SELECT * FROM spots WHERE id = $1", spot_id)
        spot_dict = dict(spot_raw)
        
        # 2. Fetch reviews (BEFORE enrichment)
        reviews_raw = await conn.fetch("SELECT * FROM reviews WHERE spot_id = $1", spot_id)
        reviews_list = [dict(r) for r in reviews_raw]
        
        # 3. Generate the actual prompt sent to LLM (DURING enrichment)
        selected_reviews = select_reviews_for_prompt(reviews_list, apply_dedup=True)
        prompt_text = build_spot_user_prompt(spot_dict, selected_reviews)
        
        # 4. Fetch the enriched state (AFTER enrichment)
        semantic_state = await conn.fetchrow("SELECT * FROM spot_semantic_state WHERE spot_id = $1", spot_id)
        claims = await conn.fetch("SELECT * FROM extracted_claims WHERE spot_id = $1 ORDER BY id DESC", spot_id)
        observations = await conn.fetch("SELECT * FROM normalized_observations WHERE spot_id = $1 ORDER BY id DESC", spot_id)
        
        # Format dates as strings
        def date_handler(obj):
            if hasattr(obj, 'isoformat'):
                return obj.isoformat()
            raise TypeError(f"Type {type(obj)} not serializable")
            
        # Filter out fields that are None or empty to keep it compact
        spot_filtered = {k: v for k, v in spot_dict.items() if v is not None and v != "" and v != {} and v != []}
        
        md_content = []
        md_content.append(f"# Auditoría de Datos: Spot ID {spot_id} ({spot_dict.get('canonical_name')})")
        md_content.append("\nEste reporte detalla el flujo de datos completo para un spot real antes, durante y después de la fase de enriquecimiento semántico con LLM.\n")
        
        md_content.append("## 1. Datos Crudos en BD (Antes del Enriquecimiento)")
        md_content.append("Estos son los atributos físicos y metadatos estructurados consolidados en la tabla `spots`:")
        md_content.append("```json\n" + json.dumps(spot_filtered, default=date_handler, indent=2) + "\n```\n")
        
        md_content.append("## 2. Prompt de Entrada Generado para el LLM")
        md_content.append("Esta es la representación exacta en texto que se envía al LLM en el User Prompt (incluye descripciones multilingües y las reviews seleccionadas bajo presupuesto de tokens y ordenadas por relevancia temporal):")
        md_content.append("```text\n" + prompt_text + "\n```\n")
        
        md_content.append("## 3. Estado Semántico Consolidado (Después del Enriquecimiento)")
        md_content.append("Esta es la fila resultante guardada en la tabla `spot_semantic_state` que consume la API y los buscadores vectoriales:")
        md_content.append("```json\n" + json.dumps(dict(semantic_state) if semantic_state else {}, default=date_handler, indent=2) + "\n```\n")
        
        md_content.append("## 4. Claims Extraídos por el LLM (Historial)")
        md_content.append("Estos son los hechos individuales extraídos por el LLM en la tabla `extracted_claims` (citan la review de origen y el fragmento textual literal original):")
        claims_list = [dict(c) for c in claims]
        md_content.append("```json\n" + json.dumps(claims_list[:15], default=date_handler, indent=2) + "\n```\n")
        
        md_content.append("## 5. Observaciones Normalizadas en la BD")
        md_content.append("Estos son los valores de señal normalizados en la tabla `normalized_observations` (con cálculo de pesos y asignación de confianza por fuente y extractor):")
        obs_list = [dict(o) for o in observations]
        md_content.append("```json\n" + json.dumps(obs_list[:15], default=date_handler, indent=2) + "\n```\n")
        
        # Write to scraper folder (mounted as /app)
        output_path = os.path.join(os.path.dirname(__file__), 'audit_spot_85057_sample.md')
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(md_content))
        print(f"Audit file generated at: {os.path.abspath(output_path)}")

    await pool.close()

if __name__ == '__main__':
    asyncio.run(main())
