"""Pollution datasets, references and cursors from MinIO files only."""
import pandas as pd
from urbanhub_common import read_dataset, write_parquet
from urbanhub_common.reference import cities
from urbanhub_common.ops import start_run as _start, finish_run

SENSOR_COLS = ("sensor_id","location_id","location_name","city","pollutant","unit_source",
               "latitude","longitude","provider","datetime_first","datetime_last","active")
MEASURE_COLS = ("sensor_id","measured_at","pollutant","value","unit","value_raw","unit_raw",
                "latitude","longitude","city","source")

def load_cities():
    return list(cities().sort_values("population", ascending=False)[["city","latitude","longitude"]].itertuples(index=False,name=None))

def upsert_sensors(sensors):
    if not sensors:
        return 0
    frame = pd.DataFrame(sensors).assign(discovered_at=pd.Timestamp.now(tz="UTC"))
    write_parquet(frame, "silver", "openaq/sensors", "discovered_at")
    return len(frame)

def active_sensors():
    frame = read_dataset("openaq/sensors")
    if frame.empty:
        return []
    frame = frame.sort_values("_lake_object").drop_duplicates("sensor_id",keep="last")
    return frame[frame.active].drop(columns="_lake_object").to_dict("records")

def last_timestamps():
    frame = read_dataset("openaq/measurements")
    if frame.empty:
        return {}
    frame["measured_at"] = pd.to_datetime(frame.measured_at,utc=True)
    return frame.groupby("sensor_id").measured_at.max().to_dict()

def insert_measurements(records):
    if not records:
        return 0
    frame = pd.DataFrame(records,columns=MEASURE_COLS).drop_duplicates(["sensor_id","measured_at"],keep="last")
    write_parquet(frame, "silver", "openaq/measurements", "measured_at")
    return len(frame)

def start_run(mode):
    return _start("pollution", mode)
