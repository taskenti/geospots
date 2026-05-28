import os
import glob

files = glob.glob('c:/geospots/scraper/sources/*.py')
for f in files:
    with open(f, 'r', encoding='utf-8') as file:
        content = file.read()
    
    new_content = content.replace('def download_reviews(self, pool, config) -> dict:', 'def download_reviews(self, pool, config, job_id: int = None) -> dict:')
    
    if new_content != content:
        with open(f, 'w', encoding='utf-8') as file:
            file.write(new_content)
        print(f"Updated {f}")
