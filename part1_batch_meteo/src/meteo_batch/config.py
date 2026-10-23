import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------- NOAA (réglages repris du notebook) ----------
ANNEE_FIN = int(os.getenv("NOAA_YEAR_END", date.today().year))
ANNEES = list(range(ANNEE_FIN - 4, ANNEE_FIN + 1))   # les 5 dernières années (NOAA s'arrête en août 2025)
NB_THREADS = int(os.getenv("NOAA_THREADS", "10"))  # téléchargements en même temps
MIN_OBS_JOUR = 8                                    # nombre minimum de mesures pour garder un jour
DISTANCE_MAX_KM = 30                                # rattachement d'une station à une ville

DOSSIER_RAW = Path(os.getenv("NOAA_RAW_DIR", "data/noaa/raw"))   # bronze NOAA (local, ~3 Go)

URL_LISTE_STATIONS = "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv"
URL_FICHIER = "https://www.ncei.noaa.gov/data/global-hourly/access/{annee}/{station}.csv"
# Adresse de secours : exactement les mêmes données, hébergées chez Amazon
URL_SECOURS = "https://noaa-global-hourly-pds.s3.amazonaws.com/{annee}/{station}.csv"

# ---------- Open-Meteo (complément après la fin de NOAA) ----------
OPENMETEO_HISTORY_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
OPENMETEO_LIVE_URL = "https://api.open-meteo.com/v1/forecast"
OPENMETEO_VARS = {   # variable Open-Meteo -> colonne UrbanHub (unités identiques au nettoyage NOAA)
    "temperature_2m": "temperature",          # °C
    "wind_speed_10m": "wind_speed",           # km/h
    "wind_direction_10m": "wind_direction",   # degrés
    "pressure_msl": "pressure",               # hPa
    "precipitation": "precipitation",         # mm
    "visibility": "visibility",               # m
}
OPENMETEO_START = date(2025, 8, 25)   # début par défaut si la ville n'a aucune donnée NOAA
LIVE_INTERVAL_MINUTES = int(os.getenv("METEO_LIVE_INTERVAL_MINUTES", "60"))
