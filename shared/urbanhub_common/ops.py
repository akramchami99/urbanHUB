"""Ingestion journal stored as immutable Parquet events in MinIO metadata/."""
from uuid import uuid4
import pandas as pd
from .lake import write_parquet

_runs = {}

def start_run(part, mode):
    run_id = uuid4().hex
    record = dict(run_id=run_id,part=part,mode=mode,status="running",
                  started_at=pd.Timestamp.now(tz="UTC"),finished_at=None,
                  api_calls=0,rows_received=0,rows_inserted=0,rows_rejected=0,raw_path=None,error=None)
    write_parquet(pd.DataFrame([record]), "metadata", "ingestion_runs", "started_at")
    _runs[run_id] = record
    return run_id

def finish_run(run_id, status, **stats):
    record = dict(_runs[run_id], status=status, finished_at=pd.Timestamp.now(tz="UTC"))
    for name in ("api_calls","rows_received","rows_inserted","rows_rejected","raw_path","error"):
        if name in stats:
            record[name] = stats[name]
    write_parquet(pd.DataFrame([record]), "metadata", "ingestion_runs", "started_at")
    _runs.pop(run_id, None)
