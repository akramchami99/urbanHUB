"""Historique vélos depuis les archives mensuelles CityBikes (https://data.citybik.es, licence ODbL).

Une archive = un fichier Parquet par réseau et par mois ; chaque ligne est un CHANGEMENT d'état d'une station
(id, bikes, free, timestamp UTC). On en déduit, par ville et par heure :
  - le stock : état de chaque station à la fin de l'heure (dernier changement connu, propagé)
  - l'usage réel : vélos retirés (departures) et rendus (returns) = variations du nombre de vélos.
    Une variation d'au moins REBALANCING_MIN vélos d'un coup est un camion de rééquilibrage : exclue.
  - les pannes de l'archive : une heure sans aucun changement dans le réseau -> valeurs NULL.
"""

import io

import numpy as np
import pandas as pd
import requests
from urbanhub_common import get_logger, s3_client, lake_bucket, write_parquet

log = get_logger("citybikes_archive")

BASE_URL = "https://data.citybik.es/dumps/by-network/{year}/{month}-{network}-stats.parquet"
# réseaux suivis par la partie 2 (main.py) -> ville de ref.cities
NETWORKS = {
    "velib": "Paris", "velov": "Lyon", "le-velo": "Marseille", "velo": "Toulouse",
    "v3-bordeaux": "Bordeaux", "bicloo": "Nantes", "vlille": "Lille", "velhop": "Strasbourg",
    "le-velo-star": "Rennes", "velomagg": "Montpellier",
}
REBALANCING_MIN = 5          # |variation| >= 5 vélos en un seul changement -> rééquilibrage
MIN_STATION_COVERAGE = 0.8   # heures où < 80 % des stations ont un état connu (début de mois) : stock ignoré


def download(network: str, month: str) -> bytes | None:
    url = BASE_URL.format(year=month[:4], month=month, network=network)
    r = requests.get(url, timeout=300)
    if r.status_code == 404:
        log.warning("archive absente : %s", url)
        return None
    r.raise_for_status()
    return r.content


def archive_to_bronze(network: str, month: str, content: bytes) -> str:
    bucket = lake_bucket()
    key = f"bronze/citybikes-archive/month={month}/{network}.parquet"
    s3_client().put_object(Bucket=bucket, Key=key, Body=content, ContentType="application/octet-stream")
    return f"s3://{bucket}/{key}"


def hourly_from_events(events: pd.DataFrame, city: str, month: str) -> pd.DataFrame:
    """Changements d'état des stations -> 1 ligne par heure (stock + usage)."""
    df = events[["id", "bikes", "free", "timestamp"]].dropna(subset=["id", "timestamp"]).copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values(["id", "timestamp"])
    df["hour"] = df["timestamp"].dt.floor("h")

    # --- usage : variations du nombre de vélos entre deux états successifs d'une même station
    delta = df.groupby("id")["bikes"].diff()
    normal = delta.abs() < REBALANCING_MIN
    df["departures"] = (-delta).clip(lower=0).where(normal, 0)
    df["returns"] = delta.clip(lower=0).where(normal, 0)
    usage = df.groupby("hour")[["departures", "returns"]].sum()

    # --- stock : état de chaque station à la fin de chaque heure du mois
    start = pd.Timestamp(f"{month[:4]}-{month[4:]}-01", tz="UTC")
    hours = pd.date_range(start, start + pd.offsets.MonthBegin(1), freq="h", inclusive="left")
    last = df.groupby(["id", "hour"])[["bikes", "free"]].last()
    bikes = last["bikes"].unstack("id").reindex(hours).ffill()
    free = last["free"].unstack("id").reindex(hours).ffill()
    known = bikes.notna() & free.notna()
    cap = (bikes + free).where(known)
    ratio = (bikes / cap).where(cap > 0)

    out = pd.DataFrame(index=hours)
    out["n_stations"] = known.sum(axis=1)
    out["bikes_available"] = bikes.where(known).sum(axis=1)
    out["free_slots"] = free.where(known).sum(axis=1)
    out["availability_ratio"] = out["bikes_available"] / (out["bikes_available"] + out["free_slots"])
    out["share_low_supply"] = (ratio <= 0.10).sum(axis=1) / ratio.notna().sum(axis=1)
    out["share_empty"] = ((bikes == 0) & known).sum(axis=1) / out["n_stations"]
    out["share_full"] = ((free == 0) & known).sum(axis=1) / out["n_stations"]
    partial = out["n_stations"] < MIN_STATION_COVERAGE * out["n_stations"].max()
    stock_cols = ["bikes_available", "free_slots", "availability_ratio", "share_low_supply", "share_empty",
                  "share_full"]
    out.loc[partial, stock_cols] = np.nan
    out[["n_stations", "bikes_available", "free_slots"]] = out[["n_stations", "bikes_available", "free_slots"]].astype("Int64")

    # heure sans AUCUN changement d'état dans tout le réseau = panne de l'archive (ex. Lyon, Nantes, Toulouse
    # du 1er au 10/09/2026), pas une heure sans vélo : usage et stock mis à NULL (inconnus)
    out = out.join(usage).join(df.groupby("hour").size().rename("n_changes"))
    out["n_changes"] = out["n_changes"].fillna(0).astype(int)
    outage = out["n_changes"] == 0
    out.loc[outage, ["departures", "returns"] + stock_cols] = np.nan
    out[["departures", "returns"]] = out[["departures", "returns"]].astype("Int64")
    out = out.rename_axis("hour_utc").reset_index()
    out.insert(0, "city", city)
    return out


def load_month(month: str) -> list[pd.DataFrame]:
    frames = []
    for network, city in NETWORKS.items():
        content = download(network, month)
        if content is None:
            continue
        path = archive_to_bronze(network, month, content)
        events = pd.read_parquet(io.BytesIO(content), columns=["id", "bikes", "free", "timestamp"])
        hourly = hourly_from_events(events, city, month)
        write_parquet(hourly, "silver", "citybikes-archive/city_hourly", "hour_utc")
        log.info("%-11s %s : %8d changements -> %d heures, %d retraits (%s)", city, month, len(events),
                 len(hourly), hourly["departures"].sum(), path)
        frames.append(hourly)
    return frames
