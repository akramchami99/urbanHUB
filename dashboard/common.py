import streamlit as st
from threading import RLock
from urbanhub_common import get_connection


@st.cache_resource(ttl=60, show_spinner="Lecture des fichiers du lake…")
def lake_snapshot():
    # A short-lived in-memory reader shared by the page's charts. No database file.
    return get_connection(), RLock()


@st.cache_data(ttl=60, show_spinner="Calcul des indicateurs…")
def query(sql: str, params: tuple | None = None):
    conn, lock = lake_snapshot()
    with lock:
        return conn.execute(sql.replace("%s", "?"), params).fetchdf()


def placeholder(part: str, team: str) -> None:
    st.info(f"🚧 {part} : en cours de réalisation par {team}. "
            "Ajouter les graphiques dans ce fichier une fois les fichiers ingérés.")


def explain(lecture: str, interpretation: str | None = None, expanded: bool = False) -> None:
    """Encadré repliable sous un graphique : comment le lire, puis quoi en conclure."""
    with st.expander("📖 Comment lire et interpréter ce graphique", expanded=expanded):
        st.markdown(f"**Lecture.** {lecture}")
        if interpretation:
            st.markdown(f"**Interprétation.** {interpretation}")
