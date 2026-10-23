# Contexte de build = racine du projet (pour embarquer shared/)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY shared /opt/shared
RUN pip install --no-cache-dir /opt/shared

COPY part1_batch_meteo/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY part1_batch_meteo/src ./src
COPY part1_batch_meteo/tests ./tests
ENV PYTHONPATH=/app/src

ENTRYPOINT ["python", "-m", "meteo_batch"]
CMD ["run"]
