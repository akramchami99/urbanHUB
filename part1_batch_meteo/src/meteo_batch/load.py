"""Persist weather datasets in MinIO and read watermarks from object files."""
import pandas as pd
from urbanhub_common import read_dataset, write_parquet
from urbanhub_common.reference import cities

def villes():
    return list(cities().sort_values("city")[["city","latitude","longitude"]].itertuples(index=False,name=None))

def dernieres_heures():
    frame = read_dataset("meteo/city_hourly")
    if frame.empty:
        return {}
    frame["hour_utc"] = pd.to_datetime(frame.hour_utc, utc=True)
    return frame.groupby("city").hour_utc.max().to_dict()

def stations(frame):
    return write_parquet(frame.assign(loaded_at=pd.Timestamp.now(tz="UTC")), "silver", "meteo/stations", "loaded_at")

def silver_parquet(frame):
    return write_parquet(frame, "silver", "noaa/global-hourly", "timestamp")

def recalculer_journalier_openmeteo():
    frame = read_dataset("meteo/city_hourly")
    if frame.empty:
        return 0
    frame = frame[frame.source == "open-meteo"].sort_values("_lake_object").drop_duplicates(["city","hour_utc"],keep="last")
    if frame.empty:
        return 0
    frame["date"] = pd.to_datetime(frame.hour_utc,utc=True).dt.tz_convert("Europe/Paris").dt.date
    out = frame.groupby(["city","date"]).agg(temp_mean=("temperature","mean"),temp_min=("temperature","min"),
        temp_max=("temperature","max"),wind_max=("wind_speed","max"),pressure_mean=("pressure","mean"),
        visibility_mean=("visibility","mean"),precipitation=("precipitation",lambda s:s.sum(min_count=1)),
        nb_obs=("hour_utc","size")).reset_index()
    out["station_id"] = "OPENMETEO-" + out.pop("city")
    out = out[out.nb_obs >= 8]
    write_parquet(out, "gold", "meteo/daily", "date")
    return len(out)
