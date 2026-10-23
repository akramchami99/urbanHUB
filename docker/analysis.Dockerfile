# Image commune aux services "dashboard" (Streamlit) et "jupyter".
# Contexte de build = racine du projet.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY shared /opt/shared
RUN pip install --no-cache-dir /opt/shared

COPY docker/requirements-analysis.txt .
RUN pip install --no-cache-dir -r requirements-analysis.txt
