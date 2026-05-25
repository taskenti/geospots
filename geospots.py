#!/usr/bin/env python
"""GeoSpots — CLI Helper & Developer Utility Tool

This script simplifies running commands locally by automatically parsing the .env file,
setting appropriate environment variable mappings, starting services, and executing scripts.
"""

import os
import sys
import subprocess
import asyncio

def load_env():
    """Loads environment variables from .env file and sets up host DB mappings."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ[k.strip()] = v.strip()

    # Map database credentials to host-visible ports
    os.environ['DB_HOST'] = os.environ.get('DB_HOST', 'localhost')
    os.environ['DB_PORT'] = os.environ.get('DB_PORT', '25433') # host-mapped port
    os.environ['DB_NAME'] = os.environ.get('POSTGRES_DB', 'geospots')
    os.environ['DB_USER'] = os.environ.get('POSTGRES_USER', 'geospots')
    os.environ['DB_PASSWORD'] = os.environ.get('POSTGRES_PASSWORD', 'camperbot_local_dev_2026')
    default_kmz = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'ioverlander.kmz')
    os.environ['IOV_KMZ_PATH'] = os.environ.get('IOV_KMZ_PATH', default_kmz)


def run_command(cmd, cwd=None):
    """Utility to run shell command and stream output."""
    try:
        process = subprocess.Popen(cmd, shell=True, env=os.environ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cwd)
        while True:
            output = process.stdout.readline()
            if output == '' and process.poll() is not None:
                break
            if output:
                print(output.strip())
        rc = process.poll()
        return rc
    except KeyboardInterrupt:
        print("\nProceso interrumpido por el usuario.")
        return 1

def start_db():
    print("Iniciando contenedor de base de datos PostgreSQL (PostGIS + pgvector)...")
    return run_command("docker-compose up -d db")

def stop_db():
    print("Deteniendo contenedores...")
    return run_command("docker-compose down")

async def init_schema():
    load_env()
    print("Inicializando base de datos local...")
    import asyncpg
    
    dsn = f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'db', 'schema.sql')
    
    if not os.path.exists(schema_path):
        print(f"Error: No se encontró el esquema en {schema_path}")
        return 1
        
    with open(schema_path, 'r', encoding='utf-8') as f:
        schema_sql = f.read()
        
    try:
        conn = await asyncpg.connect(dsn=dsn)
        print("Conectado a la base de datos. Aplicando esquema...")
        # Ejecutar por bloques separados por punto y coma (o directo si no hay bloques conflictivos)
        await conn.execute(schema_sql)
        await conn.close()
        print("Esquema aplicado con éxito.")
        return 0
    except Exception as e:
        print(f"Error inicializando esquema: {e}")
        return 1

def sync_sources():
    load_env()
    print("Sincronizando configuración de fuentes...")
    scraper_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scraper')
    import sys
    return run_command(f'"{sys.executable}" sync_db.py', cwd=scraper_dir)

def import_ioverlander():
    load_env()
    print("Iniciando importación offline de iOverlander KMZ...")
    scraper_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scraper')
    import sys
    return run_command(f'"{sys.executable}" scheduler.py --ioverlander', cwd=scraper_dir)

def run_scraper(name):
    load_env()
    print(f"Iniciando scraper: {name}...")
    scraper_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scraper')
    import sys
    return run_command(f'"{sys.executable}" scheduler.py --{name}', cwd=scraper_dir)

def run_scraper_reviews(name):
    load_env()
    print(f"Iniciando descarga de reviews para: {name}...")
    scraper_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scraper')
    import sys
    return run_command(f'"{sys.executable}" scheduler.py --reviews {name}', cwd=scraper_dir)

def status():
    load_env()
    print("Comprobando estado de la base de datos...")
    scraper_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scraper')
    import sys
    return run_command(f'"{sys.executable}" diagnostico.py', cwd=scraper_dir)

def print_help():
    print("""
GeoSpots Developer CLI Tool

Uso:
  python geospots.py <comando>

Comandos disponibles:
  db-start            Inicia el contenedor Docker de PostgreSQL (PostGIS + pgvector).
  db-stop             Detiene los contenedores Docker de GeoSpots.
  db-init             Conecta a la DB local y aplica el esquema db/schema.sql.
  db-sync             Sincroniza y registra las fuentes en fuentes_config.
  db-status           Muestra el diagnóstico de spots, reviews e historial en la DB.
  import-ioverlander  Ejecuta la importación offline desde data/ioverlander.kmz.
  run-scraper <name>  Ejecuta un scraper específico (ej: park4night, furgovw).
  run-reviews <name>  Ejecuta la descarga desacoplada de reviews para una fuente (ej: park4night, stayfree).
  help                Muestra esta ayuda.
""")

def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)

    cmd = sys.argv[1].lower()
    
    if cmd == 'db-start':
        sys.exit(start_db())
    elif cmd == 'db-stop':
        sys.exit(stop_db())
    elif cmd == 'db-init':
        sys.exit(asyncio.run(init_schema()))
    elif cmd == 'db-sync':
        sys.exit(sync_sources())
    elif cmd == 'import-ioverlander':
        sys.exit(import_ioverlander())
    elif cmd == 'run-scraper':
        if len(sys.argv) < 3:
            print("Error: Debes especificar el nombre del scraper. Ej: python geospots.py run-scraper furgovw")
            sys.exit(1)
        sys.exit(run_scraper(sys.argv[2]))
    elif cmd == 'run-reviews':
        if len(sys.argv) < 3:
            print("Error: Debes especificar la fuente. Ej: python geospots.py run-reviews park4night")
            sys.exit(1)
        sys.exit(run_scraper_reviews(sys.argv[2]))
    elif cmd == 'db-status':
        sys.exit(status())
    elif cmd in ('help', '--help', '-h'):
        print_help()
    else:
        print(f"Comando desconocido: {cmd}")
        print_help()
        sys.exit(1)

if __name__ == '__main__':
    main()
