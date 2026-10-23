import streamlit as st

from common import query

st.set_page_config(page_title="UrbanHub", page_icon="🏙️", layout="wide")

st.title("🏙️ UrbanHub : jumeau numérique urbain")
st.markdown(
    """
Plateforme d'ingestion et d'analyse des données d'une Smart City, alimentée par trois flux :

| Flux | Source | Page |
|---|---|---|
| **Batch** : météo historique | NOAA Global Hourly | 1 · Météo |
| **Streaming** : mobilité douce | CityBikes API | 2 · Mobilité |
| **IoT** : pollution de l'air | OpenAQ v3 | 3 · Pollution |
| **Croisement** des trois flux | - | 4 · Analyse croisée |
"""
)

st.info("Data lake : MinIO pour Bronze et les fichiers Iceberg Silver / Gold. "
        "Catalogue REST Nessie ; DuckDB interroge les snapshots des tables en mémoire.")

st.subheader("État des ingestions")
try:
    runs = query(
        """SELECT part, mode, status, started_at, finished_at, api_calls, rows_inserted, rows_rejected, error
           FROM ops.ingestion_runs ORDER BY started_at DESC LIMIT 20"""
    )
    st.dataframe(runs, use_container_width=True, hide_index=True)
except Exception as exc:  # données du lake indisponibles ou .env absent
    st.error(f"Connexion à MinIO impossible : {exc}")
