FROM python:3.12-slim

WORKDIR /app

# gcc: compilación de deps. libexpat1: requerido por pyosmium (import PBF, Sprint 4).
RUN apt-get update && apt-get install -y gcc libexpat1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "scheduler.py"]
