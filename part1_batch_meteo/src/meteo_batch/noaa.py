"""Téléchargement parallèle et nettoyage NOAA Global Hourly.

Code repris du notebook UrbanHub_Partie1_Flux_Batch.ipynb (étapes 1 à 3), sans changement de logique.
"""

import io
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests
from urbanhub_common import get_logger

from .config import ANNEES, DOSSIER_RAW, NB_THREADS, URL_FICHIER, URL_LISTE_STATIONS, URL_SECOURS

log = get_logger("noaa")


# ---------------------------------------------------------------------------
# Étape 1 - stations françaises
# ---------------------------------------------------------------------------
def charger_stations_france():
    # 1) télécharger la liste de toutes les stations du monde
    reponse = requests.get(URL_LISTE_STATIONS, timeout=60)
    reponse.raise_for_status()
    stations = pd.read_csv(io.StringIO(reponse.text), dtype=str)

    # 2) garder seulement la France
    stations = stations[stations["CTRY"] == "FR"].copy()

    # 3) fabriquer le nom du fichier : USAF (6 chiffres) + WBAN (5 chiffres)
    stations["station_id"] = stations["USAF"].str.zfill(6) + stations["WBAN"].str.zfill(5)

    # 4) dates de début et de fin d'activité (pour éviter de demander des années inutiles)
    stations["debut"] = pd.to_datetime(stations["BEGIN"], format="%Y%m%d", errors="coerce")
    stations["fin"] = pd.to_datetime(stations["END"], format="%Y%m%d", errors="coerce")

    return stations[["station_id", "STATION NAME", "LAT", "LON", "debut", "fin"]]


# ---------------------------------------------------------------------------
# Étape 2 - téléchargement en parallèle
# ---------------------------------------------------------------------------
def construire_liste_taches(stations):
    taches = []
    for _, st in stations.iterrows():
        for annee in ANNEES:
            if pd.notna(st["debut"]) and st["debut"].year > annee:
                continue     # la station n'existait pas encore
            if pd.notna(st["fin"]) and st["fin"].year < annee:
                continue     # la station était déjà fermée
            taches.append((st["station_id"], annee))
    return taches


def est_un_vrai_csv(contenu):
    # contenu = les octets reçus. Une page web commence par <html ou <!doctype
    debut = contenu[:200].lower()
    return len(debut) > 0 and b"<html" not in debut and b"<!doctype" not in debut


def telecharger_un_fichier(station_id, annee):
    dossier = DOSSIER_RAW / str(annee)
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / f"{station_id}.csv"

    if chemin.exists() and chemin.stat().st_size > 0:
        return "deja"

    probleme_reseau = False
    for modele in (URL_FICHIER, URL_SECOURS):          # 1) NOAA, 2) adresse de secours
        url = modele.format(annee=annee, station=station_id)
        for _ in range(3):
            try:
                reponse = requests.get(url, timeout=60)
            except requests.RequestException:
                probleme_reseau = True
                continue                                # on réessaie
            probleme_reseau = False
            if reponse.status_code == 200 and est_un_vrai_csv(reponse.content):
                chemin.write_bytes(reponse.content)
                return "ok"
            if reponse.status_code >= 500:              # le serveur a un souci : on réessaie
                probleme_reseau = True
                continue
            break                                       # le fichier n'existe pas ici : adresse suivante
    return "erreur" if probleme_reseau else "absent"


def telecharger_tout(taches):
    # nettoyage des faux fichiers d'une exécution précédente
    for f in DOSSIER_RAW.glob("*/*.csv"):
        with open(f, "rb") as fichier:
            if not est_un_vrai_csv(fichier.read(200)):
                f.unlink()

    bilan = {"ok": 0, "deja": 0, "absent": 0, "erreur": 0}
    with ThreadPoolExecutor(max_workers=NB_THREADS) as equipe:
        travaux = [equipe.submit(telecharger_un_fichier, s, a) for s, a in taches]
        for i, travail in enumerate(as_completed(travaux), start=1):
            bilan[travail.result()] += 1
            if i % 100 == 0 or i == len(travaux):
                log.info("téléchargement : %d/%d traités", i, len(travaux))
    log.info("bilan téléchargement : %s", bilan)
    return bilan


# ---------------------------------------------------------------------------
# Étape 3 - lecture et nettoyage
# ---------------------------------------------------------------------------
COLONNES = ["STATION", "DATE", "WND", "VIS", "TMP", "SLP", "AA1"]
MAUVAISE_QUALITE = ["2", "3", "6", "7"]      # codes NOAA : mesure douteuse ou fausse

# Valeurs physiquement possibles. En dehors = erreur de capteur.
LIMITES = {
    "temperature":    (-50, 55),      # °C
    "wind_speed":     (0, 300),       # km/h
    "wind_direction": (0, 360),       # degrés
    "pressure":       (850, 1090),    # hPa
    "visibility":     (0, 100000),    # mètres
    "precipitation":  (0, 300),       # mm
}
MESURES = list(LIMITES)


def decouper(serie, nb):
    # Coupe '180,1,N,0050,1' en colonnes : ['180', '1', 'N', '0050', '1']
    morceaux = serie.fillna("").astype(str).str.split(",", expand=True)
    return morceaux.reindex(columns=range(nb))


def en_nombre(morceaux, i_val, i_qual, manquant, echelle):
    # Prend une valeur, enlève 'manquant' et les mauvaises qualités, puis change l'échelle
    val = pd.to_numeric(morceaux[i_val], errors="coerce")
    val = val.where(val != manquant)                          # 9999 -> vide
    val = val.where(~morceaux[i_qual].isin(MAUVAISE_QUALITE)) # mesure douteuse -> vide
    return val / echelle                                      # ex : 123 -> 12.3


def lire_et_nettoyer(chemin):
    df = pd.read_csv(chemin, usecols=lambda c: c in COLONNES, dtype=str,
                     on_bad_lines="skip").reindex(columns=COLONNES)

    wnd = decouper(df["WND"], 5)   # vent : direction, qualité, type, vitesse, qualité
    vis = decouper(df["VIS"], 4)   # visibilité
    tmp = decouper(df["TMP"], 2)   # température
    slp = decouper(df["SLP"], 2)   # pression
    aa1 = decouper(df["AA1"], 4)   # pluie : durée (h), quantité, condition, qualité

    out = pd.DataFrame({
        "station_id":      df["STATION"],
        "timestamp":       pd.to_datetime(df["DATE"], utc=True, errors="coerce"),
        "wind_speed":      en_nombre(wnd, 3, 4, 9999, 10) * 3.6,   # dixièmes de m/s -> km/h
        "pressure":        en_nombre(slp, 0, 1, 99999, 10),         # dixièmes de hPa -> hPa
        "wind_direction":  en_nombre(wnd, 0, 1, 999, 1),            # degrés
        "precipitation":   en_nombre(aa1, 1, 3, 9999, 10),          # dixièmes de mm -> mm
        "precip_period_h": en_nombre(aa1, 0, 3, 99, 1),             # durée de la mesure de pluie
        "temperature":     en_nombre(tmp, 0, 1, 9999, 10),          # dixièmes de °C -> °C
        "visibility":      en_nombre(vis, 0, 1, 999999, 1),         # mètres
    })

    for col, (mini, maxi) in LIMITES.items():                       # valeurs impossibles -> vide
        out[col] = out[col].where(out[col].between(mini, maxi))

    out = out.dropna(subset=["timestamp"])
    out["timestamp"] = out["timestamp"].dt.floor("h")               # arrondi à l'heure
    return out.drop_duplicates(["station_id", "timestamp"])         # 1 mesure / station / heure


def nettoyer_tout(fichiers):
    """Nettoie tous les fichiers -> tableau horaire unique (float32 pour limiter la mémoire)."""
    morceaux = []
    for i, f in enumerate(fichiers, start=1):
        df = lire_et_nettoyer(f)
        num = df.columns.drop(["station_id", "timestamp"])
        df[num] = df[num].astype(np.float32)
        morceaux.append(df)
        if i % 100 == 0 or i == len(fichiers):
            log.info("nettoyage : %d/%d fichiers", i, len(fichiers))
    meteo = pd.concat(morceaux, ignore_index=True)
    meteo = meteo.dropna(subset=MESURES, how="all")                  # lignes sans aucune mesure
    meteo = meteo.drop_duplicates(["station_id", "timestamp"])
    meteo["station_id"] = meteo["station_id"].astype("category")
    return meteo.sort_values(["station_id", "timestamp"]).reset_index(drop=True)
