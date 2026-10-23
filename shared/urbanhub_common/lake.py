"""MinIO object lake. All durable storage is in MinIO."""
import io
import os
from datetime import datetime, timezone
from functools import lru_cache
from uuid import uuid4

import boto3
from botocore.config import Config


def lake_bucket():
    return os.getenv("S3_BUCKET", "urbanhub")


@lru_cache(maxsize=1)
def s3_client():
    required = ("S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError("Missing MinIO configuration: " + ", ".join(missing))
    return boto3.client(
        "s3", endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.getenv("S3_REGION", "us-east-1"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      retries={"max_attempts": 3, "mode": "standard"}),
    )


def write_legacy_parquet(frame, zone, dataset, time_column):
    """Append immutable, UTC-date-partitioned Parquet before publishing to serving.

    Replays may overlap: consumers deduplicate on dataset business keys.
    Empty frames do not create objects. Storage errors propagate to the job.
    """
    import pandas as pd
    if zone not in ("silver", "gold", "metadata"):
        raise ValueError("Parquet zone must be silver, gold or metadata")
    if frame.empty:
        return []
    dates = pd.to_datetime(frame[time_column], utc=True, errors="raise")
    if dates.isna().any():
        raise ValueError("Lake partition timestamp cannot be null")
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex
    paths = []
    for day, part in frame.groupby(dates.dt.strftime("%Y-%m-%d"), sort=True):
        key = f"{zone}/{dataset}/date={day}/{run}.parquet"
        data = io.BytesIO()
        part.to_parquet(data, index=False, engine="pyarrow", compression="zstd")
        s3_client().put_object(Bucket=lake_bucket(), Key=key, Body=data.getvalue(),
                               ContentType="application/octet-stream")
        paths.append(f"s3://{lake_bucket()}/{key}")
    return paths


def read_legacy_parquet_dataset(dataset, start, end):
    """Read silver for [start, end), pruning daily partitions before downloading.

    Intended for the teaching project's data volumes, not a distributed query engine.
    """
    import pandas as pd
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    frames = []
    client = s3_client()
    for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=lake_bucket(), Prefix=f"silver/{dataset}/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".parquet") or "/date=" not in key:
                continue
            day = pd.Timestamp(key.split("/date=")[1][:10], tz="UTC")
            if day >= end or day + pd.Timedelta(days=1) <= start:
                continue
            response = client.get_object(Bucket=lake_bucket(), Key=key)
            body = response["Body"]
            try:
                frame = pd.read_parquet(io.BytesIO(body.read()))
            finally:
                body.close()
            # Listing order follows object key, not ingestion order across partitions.
            frame["_lake_object"] = key.rsplit("/", 1)[-1]
            frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def read_legacy_dataset(dataset, zone="silver"):
    """Read a complete object dataset for TP-scale queries and metadata lookups."""
    import pandas as pd
    frames = []
    client = s3_client()
    from concurrent.futures import ThreadPoolExecutor
    keys=[]
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=lake_bucket(),Prefix=f"{zone}/{dataset}/"):
        keys.extend(obj["Key"] for obj in page.get("Contents",[]) if obj["Key"].endswith(".parquet"))
    def read(key):
        body=client.get_object(Bucket=lake_bucket(),Key=key)["Body"]
        try: frame=pd.read_parquet(io.BytesIO(body.read()))
        finally: body.close()
        frame["_lake_object"]=key.rsplit("/",1)[-1]
        return frame
    with ThreadPoolExecutor(max_workers=8) as pool:
        frames=list(pool.map(read,keys))
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()


def write_parquet(frame,zone,dataset,time_column):
    """Compatibility API: structured data is now committed to Iceberg v2 tables."""
    if os.getenv("LAKE_TABLE_FORMAT","iceberg")=="parquet":
        return write_legacy_parquet(frame,zone,dataset,time_column)
    from .iceberg import write_table
    return write_table(frame,zone,dataset,time_column)


def read_dataset(dataset,zone="silver"):
    if os.getenv("LAKE_TABLE_FORMAT","iceberg")=="parquet":
        return read_legacy_dataset(dataset,zone)
    from .iceberg import scan_table
    return scan_table(dataset,zone)


def read_parquet_dataset(dataset,start,end):
    if os.getenv("LAKE_TABLE_FORMAT","iceberg")=="parquet":
        return read_legacy_parquet_dataset(dataset,start,end)
    from .iceberg import scan_table
    return scan_table(dataset,"silver",start,end)
