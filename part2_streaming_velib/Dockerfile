# Contexte de build = racine du projet (pour embarquer shared/)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY shared /opt/shared
RUN pip install --no-cache-dir /opt/shared

COPY part2_streaming_velib/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY part2_streaming_velib/main.py ./

ENTRYPOINT ["python", "main.py"]
# collecte toutes les 3 min (villes par défaut : DEFAULT_CITIES de main.py)
CMD ["--interval", "180"]
