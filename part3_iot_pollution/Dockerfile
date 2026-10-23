# Contexte de build = racine du projet (pour embarquer shared/)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY shared /opt/shared
RUN pip install --no-cache-dir /opt/shared

COPY part3_iot_pollution/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY part3_iot_pollution/src ./src
COPY part3_iot_pollution/tests ./tests
ENV PYTHONPATH=/app/src

ENTRYPOINT ["python", "-m", "pollution_iot"]
CMD ["run"]
