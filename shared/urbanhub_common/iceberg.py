"""Apache Iceberg v2 tables in MinIO, managed by the Nessie REST catalog."""
import argparse
import os
import re
import time
from functools import lru_cache
from threading import RLock, Lock

import pandas as pd
import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.exceptions import NoSuchTableError, CommitFailedException
from pyiceberg.io.pyarrow import PyArrowFileIO, schema_to_pyarrow
from pyiceberg.partitioning import PartitionSpec, PartitionField
from pyiceberg.schema import Schema
from pyiceberg.transforms import DayTransform
from pyiceberg.types import NestedField, StringType, LongType, DoubleType, BooleanType, DateType, TimestamptzType

KEYS = {
 ("silver","citybikes"): ["network_id","station_id","collected_at"],
 ("silver","citybikes-archive/city_hourly"): ["city","hour_utc"],
 ("silver","meteo/city_hourly"): ["city","hour_utc","source"],
 ("silver","meteo/stations"): ["station_id"],
 ("silver","noaa/global-hourly"): ["station_id","timestamp"],
 ("silver","open-meteo/weather"): ["station_id","timestamp"],
 ("silver","openaq/sensors"): ["sensor_id"],
 ("silver","openaq/measurements"): ["sensor_id","measured_at"],
 ("gold","meteo/daily"): ["station_id","date"],
 ("gold","analytics/city_hourly"): ["city","hour_utc"],
 ("metadata","ingestion_runs"): ["run_id"],
}
TIME_COLUMNS = {
 ("silver","citybikes"): "collected_at", ("silver","citybikes-archive/city_hourly"): "hour_utc",
 ("silver","meteo/city_hourly"): "hour_utc", ("silver","meteo/stations"): "loaded_at",
 ("silver","noaa/global-hourly"): "timestamp", ("silver","open-meteo/weather"): "timestamp",
 ("silver","openaq/sensors"): "discovered_at", ("silver","openaq/measurements"): "measured_at",
 ("gold","meteo/daily"): "date", ("gold","analytics/city_hourly"): "hour_utc",
 ("metadata","ingestion_runs"): "started_at",
}
TIMESTAMPS = {"hour_utc","timestamp","collected_at","station_ts","measured_at","datetime_first","datetime_last",
              "loaded_at","discovered_at","started_at","finished_at","computed_at","_ingested_at"}
STRINGS = {"station_id","network_id","city","station_name","name","source","pollutant","unit","unit_raw",
           "unit_source","location_name","provider","run_id","part","mode","status","raw_path","error",
           "meteo_source","mobility_source","_lake_object"}
MERGE_OVERWRITE_THRESHOLD = 5000
INTEGERS = {"sensor_id","location_id","n_stations","nb_obs","n_changes","api_calls","rows_received",
            "rows_inserted","rows_rejected","observations","critical_events","hour_france"}


def io_properties():
    return {"s3.endpoint":os.environ["S3_ENDPOINT_URL"], "s3.access-key-id":os.environ["S3_ACCESS_KEY_ID"],
            "s3.secret-access-key":os.environ["S3_SECRET_ACCESS_KEY"], "s3.region":os.getenv("S3_REGION","us-east-1"),
            "py-io-impl":"pyiceberg.io.pyarrow.PyArrowFileIO"}


@lru_cache(maxsize=1)
def catalog():
    return load_catalog("urbanhub",type="rest",uri=os.getenv("ICEBERG_CATALOG_URI","http://localhost:19120/iceberg"),
                        **io_properties())


def identifier(zone,dataset):
    return (zone,re.sub(r"[^a-z0-9_]","_",dataset.lower()))


def load_table(zone,dataset):
    table=catalog().load_table(identifier(zone,dataset))
    # Respect the caller's endpoint both inside Docker and from Windows.
    table.io=PyArrowFileIO(io_properties())
    return table


def normalize(frame,time_column):
    frame=frame.drop(columns=["_lake_object"],errors="ignore").copy()
    frame["_ingested_at"]=pd.Timestamp.now(tz="UTC")
    arrays=[]
    names=[]
    for name in frame.columns:
        values=frame[name]
        if name in TIMESTAMPS or name==time_column and name!="date":
            values=pd.to_datetime(values,utc=True,errors="raise")
            dtype=pa.timestamp("us",tz="UTC")
        elif name=="date":
            values=pd.to_datetime(values,errors="raise").dt.date
            dtype=pa.date32()
        elif name in STRINGS:
            values=values.astype("string")
            dtype=pa.string()
        elif name in INTEGERS:
            values=pd.to_numeric(values,errors="raise")
            dtype=pa.int64()
        else:
            inferred=pa.array(values,from_pandas=True).type
            if pa.types.is_null(inferred):
                dtype=pa.float64() if name.startswith("pollution_") else pa.string()
            elif pa.types.is_floating(inferred):
                dtype=pa.float64()
            elif pa.types.is_integer(inferred):
                dtype=pa.int64()
            elif pa.types.is_large_string(inferred):
                dtype=pa.string()
            else:
                dtype=inferred
        arrays.append(pa.array(values,type=dtype,from_pandas=True))
        names.append(name)
    return pa.Table.from_arrays(arrays,names=names)


def iceberg_schema(arrow_schema):
    fields=[]
    for i,field in enumerate(arrow_schema,1):
        t=field.type
        if pa.types.is_timestamp(t): kind=TimestamptzType()
        elif pa.types.is_date(t): kind=DateType()
        elif pa.types.is_string(t): kind=StringType()
        elif pa.types.is_boolean(t): kind=BooleanType()
        elif pa.types.is_integer(t): kind=LongType()
        elif pa.types.is_floating(t): kind=DoubleType()
        else: raise TypeError(f"Unsupported Iceberg field: {field}")
        fields.append(NestedField(i,field.name,kind,required=False))
    return Schema(*fields)


def _commit_table(frame,zone,dataset,time_column):
    if frame.empty:
        return []
    dates=pd.to_datetime(frame[time_column],utc=True,errors="raise")
    if dates.isna().any():
        raise ValueError("Iceberg partition timestamp cannot be null")
    keys=KEYS.get((zone,dataset))
    if keys:
        if frame[keys].isna().any().any():
            raise ValueError("Iceberg business keys cannot be null")
        frame=frame.drop_duplicates(keys,keep="last")
    arrow=normalize(frame,time_column)
    schema=iceberg_schema(arrow.schema)
    spec=PartitionSpec(PartitionField(source_id=schema.find_field(time_column).field_id,
        field_id=1000,transform=DayTransform(),name=time_column+"_day"))
    cat=catalog()
    cat.create_namespace_if_not_exists(zone)
    for attempt in range(6):
        try:
            table=cat.create_table_if_not_exists(identifier(zone,dataset),schema=schema,partition_spec=spec,
                properties={"format-version":"2","urbanhub.dataset":dataset,"urbanhub.zone":zone,
                            "urbanhub.time-column":time_column,"write.parquet.compression-codec":"zstd"})
            table.io=PyArrowFileIO(io_properties())
            changed_schema = bool(set(arrow.column_names)-set(table.schema().column_names))
            if changed_schema:
                with table.update_schema() as update:
                    update.union_by_name(arrow.schema)
                table=load_table(zone,dataset)
            target=schema_to_pyarrow(table.schema(),include_field_ids=False)
            # Columns absent in a new batch remain null; evolution is additive.
            prepared=pa.Table.from_arrays([
                arrow[field.name].cast(field.type) if field.name in arrow.column_names else pa.nulls(len(arrow),type=field.type)
                for field in target],schema=target)
            snapshot = table.current_snapshot()
            needs_rewrite = snapshot is not None and (changed_schema or snapshot.schema_id != table.schema().schema_id
                or (keys and len(prepared) >= MERGE_OVERWRITE_THRESHOLD))
            if needs_rewrite:
                # PyIceberg 0.10 upsert can scan the previous snapshot schema after
                # additive evolution. Large batches also produce expensive per-key predicates.
                # Align and merge in memory for the course-sized datasets, then commit atomically.
                existing = table.scan().to_arrow()
                existing = pa.Table.from_arrays([
                    existing[field.name].cast(field.type) if field.name in existing.column_names else pa.nulls(len(existing),type=field.type)
                    for field in target],schema=target)
                merged = pd.concat([existing.to_pandas(),prepared.to_pandas()],ignore_index=True)
                if keys:
                    merged = merged.drop_duplicates(keys,keep="last")
                table.overwrite(pa.Table.from_pandas(merged,schema=target,preserve_index=False))
            elif keys:
                table.upsert(prepared,join_cols=keys)
            else:
                table.append(prepared)
            return [load_table(zone,dataset).metadata_location]
        except CommitFailedException:
            if attempt==5: raise
            time.sleep(0.1*(2**attempt))


_write_locks={}
_write_locks_guard=Lock()

def write_table(frame,zone,dataset,time_column):
    # HTTP collection stays parallel. Serialize commits to the same table within a process
    # so network workers do not repeatedly invalidate one another's snapshot requirements.
    with _write_locks_guard:
        lock=_write_locks.setdefault((zone,dataset),RLock())
    with lock:
        return _commit_table(frame,zone,dataset,time_column)


def scan_table(dataset,zone="silver",start=None,end=None,snapshot_id=None,metadata_location=None):
    from pyiceberg.expressions import And,GreaterThanOrEqual,LessThan
    from pyiceberg.table import StaticTable
    try:
        table=(StaticTable.from_metadata(metadata_location,properties=io_properties()) if metadata_location else load_table(zone,dataset))
    except NoSuchTableError:
        return pd.DataFrame()
    time_column=table.properties.get("urbanhub.time-column",TIME_COLUMNS.get((zone,dataset)))
    kwargs={}
    if start is not None and end is not None:
        def utc(value):
            value=pd.Timestamp(value)
            return value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")
        lower,upper=utc(start),utc(end)
        if upper<=lower:
            raise ValueError("End must be after start")
        if time_column=="date": lower,upper=lower.date(),upper.date()
        else: lower,upper=lower.to_pydatetime(),upper.to_pydatetime()
        kwargs["row_filter"]=And(GreaterThanOrEqual(time_column,lower),LessThan(time_column,upper))
    if snapshot_id is not None:
        kwargs["snapshot_id"]=snapshot_id
    frame=table.scan(**kwargs).to_pandas()
    if not frame.empty:
        frame["_lake_object"]=pd.to_datetime(frame["_ingested_at"],utc=True).astype(str)
    return frame


def inventory():
    result=[]
    cat=catalog()
    for ns in cat.list_namespaces():
        if ns[0] not in ("silver","gold","metadata"): continue
        for ident in cat.list_tables(ns):
            table=cat.load_table(ident)
            snap=table.current_snapshot()
            summary=snap.summary.model_dump() if snap else {}
            result.append(dict(table=".".join(ident),format_version=table.metadata.format_version,
                snapshot_id=str(snap.snapshot_id) if snap else None,rows=int(summary.get("total-records",0)),
                data_files=int(summary.get("total-data-files",0)),partition=str(table.spec()),metadata=table.metadata_location))
    return result


def migrate():
    from .lake import read_legacy_dataset
    for (zone,dataset),time_column in TIME_COLUMNS.items():
        try:
            existing=load_table(zone,dataset)
            if existing.current_snapshot() is not None:
                print(f"SKIP {zone}.{dataset}: existing committed Iceberg table (preserved)",flush=True)
                continue
        except NoSuchTableError:
            pass
        frame=read_legacy_dataset(dataset,zone)
        if frame.empty:
            print(f"SKIP {zone}.{dataset}: no legacy data",flush=True)
            continue
        frame=frame.sort_values("_lake_object",kind="stable")
        keys=KEYS.get((zone,dataset))
        if keys: frame=frame.drop_duplicates(keys,keep="last")
        if time_column not in frame:
            frame[time_column]=pd.Timestamp.now(tz="UTC")
        paths=write_table(frame,zone,dataset,time_column)
        count=len(scan_table(dataset,zone))
        if count!=len(frame):
            raise RuntimeError(f"Migration count mismatch for {zone}/{dataset}: {count} != {len(frame)}")
        print(f"MIGRATED {zone}.{dataset}: {count} rows -> {paths[0]}",flush=True)


def main():
    from dotenv import load_dotenv
    load_dotenv()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=["migrate","list"])
    args=parser.parse_args()
    if args.command=="migrate": migrate()
    else:
        import json
        print(json.dumps(inventory(),ensure_ascii=False,indent=2))

if __name__=="__main__": main()
