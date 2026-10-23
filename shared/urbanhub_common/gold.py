"""Build city/hour indicators directly from Silver objects, without a SQL connection."""
from functools import reduce
import pandas as pd

from .lake import read_parquet_dataset, write_parquet

KEYS = ["city", "hour_utc"]


def window(frame, column, start, end):
    if frame.empty:
        return frame
    frame = frame.copy()
    frame[column] = pd.to_datetime(frame[column], utc=True)
    return frame[(frame[column] >= start) & (frame[column] < end)]


def latest(frame, keys):
    # Immutable files overlap on replay; use latest ingestion per business key.
    if "_lake_object" in frame:
        frame = frame.sort_values("_lake_object", kind="stable")
    return frame.drop_duplicates(keys, keep="last")


def build_city_hourly(start, end):
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    if end <= start:
        raise ValueError("End must be after start")
    frames = []
    meteo = window(read_parquet_dataset("meteo/city_hourly", start, end), "hour_utc", start, end)
    if not meteo.empty:
        meteo = latest(meteo, KEYS + ["source"])
        meteo["_priority"] = meteo.source.eq("noaa").astype(int)
        meteo = meteo.sort_values("_priority", kind="stable").drop_duplicates(KEYS, keep="last")
        frames.append(meteo[KEYS + ["source", "temperature", "precipitation", "wind_speed"]]
                      .rename(columns={"source": "meteo_source"}))

    bikes = window(read_parquet_dataset("citybikes", start, end), "collected_at", start, end)
    mobility = pd.DataFrame()
    if not bikes.empty:
        bikes = latest(bikes, ["network_id", "station_id", "collected_at"])
        bikes = bikes.dropna(subset=["bikes_available", "free_slots"])
        # Sum stocks across stations at one collection time before hourly averaging.
        snapshots = bikes.groupby(["city", "collected_at"])[["bikes_available", "free_slots"]].sum().reset_index()
        snapshots["hour_utc"] = snapshots.collected_at.dt.floor("h")
        mobility = snapshots.groupby(KEYS)[["bikes_available", "free_slots"]].mean().reset_index()
        mobility["mobility_source"] = "citybikes-snapshots"
    history = window(read_parquet_dataset("citybikes-archive/city_hourly", start, end), "hour_utc", start, end)
    if not history.empty:
        history = latest(history, KEYS)
        history["mobility_source"] = "citybikes-archive"
        cols = KEYS + ["bikes_available", "free_slots", "departures", "returns", "mobility_source"]
        mobility = pd.concat([mobility, history[cols]], ignore_index=True).drop_duplicates(KEYS, keep="last")
    if not mobility.empty:
        frames.append(mobility)

    pollution = window(read_parquet_dataset("openaq/measurements", start, end), "measured_at", start, end)
    if not pollution.empty:
        pollution = latest(pollution, ["sensor_id", "measured_at"])
        pollution["hour_utc"] = pollution.measured_at.dt.floor("h")
        # First average each sensor within an hour, then sensors within a city.
        # Include unit in the column name: values with different units are never mixed.
        sensor = pollution.groupby(KEYS + ["sensor_id", "pollutant", "unit"], dropna=False).value.mean().reset_index()
        sensor["indicator"] = "pollution_" + sensor.pollutant.astype(str) + "_" + sensor.unit.astype(str)
        wide = sensor.pivot_table(index=KEYS, columns="indicator", values="value", aggfunc="mean").reset_index()
        wide.columns.name = None
        frames.append(wide)
    if not frames:
        raise RuntimeError("No Silver data in this window. Run ingestion or import existing data first.")
    result = reduce(lambda left, right: left.merge(right, on=KEYS, how="outer", validate="one_to_one"), frames)
    return result.sort_values(KEYS).reset_index(drop=True)


def publish(start, end):
    frame = build_city_hourly(start, end)
    paths = write_parquet(frame, "gold", "analytics/city_hourly", "hour_utc")
    return frame, paths
