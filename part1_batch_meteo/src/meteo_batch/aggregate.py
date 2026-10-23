"""Agrégats calculés à partir du tableau horaire nettoyé (NOAA ou Open-Meteo)."""

import numpy as np
import pandas as pd

from .config import DISTANCE_MAX_KM, MIN_OBS_JOUR


def distance_km(lat1, lon1, lat2, lon2):
    # distance entre deux points GPS (formule de "haversine") - reprise du notebook
    p = np.pi / 180
    a = (0.5 - np.cos((lat2 - lat1) * p) / 2
         + np.cos(lat1 * p) * np.cos(lat2 * p) * (1 - np.cos((lon2 - lon1) * p)) / 2)
    return 12742 * np.arcsin(np.sqrt(a))


def rattacher_villes(stations: pd.DataFrame, villes: list[tuple[str, float, float]]) -> pd.DataFrame:
    """Ajoute city / distance_km : ville de ref.cities la plus proche si elle est à moins de 30 km."""
    st = stations.copy()
    dist = pd.DataFrame({nom: distance_km(st["latitude"], st["longitude"], la, lo) for nom, la, lo in villes},
                        index=st.index)
    st["city"] = dist.idxmin(axis=1)
    st["distance_km"] = dist.min(axis=1).round(1)
    st.loc[st["distance_km"] > DISTANCE_MAX_KM, "city"] = None
    return st


def journalier(horaire: pd.DataFrame, min_obs: int = MIN_OBS_JOUR) -> pd.DataFrame:
    """Résumé par station et par jour (heure de Paris) - même règle que le notebook :
    la pluie du jour ne cumule que les mesures sur 1 h pour ne pas compter deux fois la même pluie."""
    temp = horaire.assign(
        date=horaire["timestamp"].dt.tz_convert("Europe/Paris").dt.date,
        pluie_1h=horaire["precipitation"].where(horaire["precip_period_h"] == 1),
    )
    daily = (temp.groupby(["station_id", "date"], observed=True)
             .agg(temp_mean=("temperature", "mean"),
                  temp_min=("temperature", "min"),
                  temp_max=("temperature", "max"),
                  wind_max=("wind_speed", "max"),
                  pressure_mean=("pressure", "mean"),
                  visibility_mean=("visibility", "mean"),
                  precipitation=("pluie_1h", "sum"),
                  n_pluie=("pluie_1h", "count"),
                  nb_obs=("temperature", "size"))
             .reset_index())
    daily.loc[daily["n_pluie"] == 0, "precipitation"] = np.nan     # pas de mesure de pluie ce jour-là
    daily = daily.drop(columns="n_pluie")
    return daily[daily["nb_obs"] >= min_obs].reset_index(drop=True)


def horaire_par_ville(horaire: pd.DataFrame, station_ville: pd.Series, source: str) -> pd.DataFrame:
    """Moyenne horaire des stations rattachées à chaque ville (1 ligne = ville x heure)."""
    df = horaire.assign(city=horaire["station_id"].map(station_ville)).dropna(subset=["city"])
    if df.empty:
        return pd.DataFrame()
    rad = np.deg2rad(df["wind_direction"].astype(float))
    df = df.assign(
        pluie_1h=df["precipitation"].where(df["precip_period_h"] == 1),
        u=np.sin(rad), v=np.cos(rad),   # direction du vent : moyenne vectorielle (350° et 10° -> 0°, pas 180°)
    )
    out = (df.groupby(["city", "timestamp"], observed=True)
           .agg(temperature=("temperature", "mean"), wind_speed=("wind_speed", "mean"),
                u=("u", "mean"), v=("v", "mean"), pressure=("pressure", "mean"),
                precipitation=("pluie_1h", "mean"), visibility=("visibility", "mean"),
                n_stations=("station_id", "nunique"))
           .reset_index().rename(columns={"timestamp": "hour_utc"}))
    out["wind_direction"] = (np.rad2deg(np.arctan2(out["u"], out["v"])) % 360).round(0)
    out["source"] = source
    return out[["city", "hour_utc", "source", "temperature", "wind_speed", "wind_direction",
                "pressure", "precipitation", "visibility", "n_stations"]]
