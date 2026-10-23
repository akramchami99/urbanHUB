import gzip
import io
import json
from unittest.mock import Mock

import pandas as pd
import pytest
from moto import mock_aws

from urbanhub_common import lake
from urbanhub_common.__main__ import init_bucket
from urbanhub_common.gold import build_city_hourly, publish
from urbanhub_common.raw_storage import RawWriter


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("LAKE_TABLE_FORMAT","parquet")
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://s3.amazonaws.com")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("S3_BUCKET", "urbanhub-test")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("RAW_STORAGE", "minio")
    lake.s3_client.cache_clear()
    with mock_aws():
        init_bucket()
        yield lake.s3_client()
    lake.s3_client.cache_clear()


def keys(s3, prefix):
    return [o["Key"] for o in s3.list_objects_v2(Bucket=lake.lake_bucket(), Prefix=prefix).get("Contents", [])]


def test_init_is_repeatable_and_versioned(s3):
    init_bucket()
    assert s3.get_bucket_versioning(Bucket=lake.lake_bucket())["Status"] == "Enabled"
    assert s3.meta.config.s3["addressing_style"] == "path"


def test_bronze_round_trip_and_no_collisions(s3):
    for _ in range(2):
        writer = RawWriter("openaq", "pollution", "stream")
        writer.add({"city": "Paris"}, {"results": [{"value": 12}]})
        assert writer.flush().startswith("s3://urbanhub-test/bronze/")
    objects = keys(s3, "bronze/")
    assert len(objects) == 2
    body = s3.get_object(Bucket=lake.lake_bucket(), Key=objects[0])["Body"].read()
    assert json.loads(gzip.decompress(body))["payload"]["results"][0]["value"] == 12


def test_failed_bronze_keeps_buffer_and_raises(s3, monkeypatch):
    writer = RawWriter("test", "data", "run")
    writer.add({}, {"important": True})
    failed = Mock()
    failed.put_object.side_effect = RuntimeError("MinIO unavailable")
    monkeypatch.setattr("urbanhub_common.raw_storage.s3_client", lambda: failed)
    with pytest.raises(RuntimeError, match="MinIO unavailable"):
        writer.flush()
    assert writer._lines
    monkeypatch.setattr("urbanhub_common.raw_storage.s3_client", lambda: s3)
    assert writer.flush()
    assert not writer._lines


def test_partition_dates_use_utc_and_empty_noop(s3):
    frame = pd.DataFrame({"hour_utc": ["2026-10-01T00:30:00+02:00", "2026-10-01T02:30:00+02:00"], "value": [1, 2]})
    paths = lake.write_parquet(frame, "silver", "test", "hour_utc")
    assert len(paths) == 2
    assert any("date=2026-09-30" in p for p in paths)
    assert any("date=2026-10-01" in p for p in paths)
    assert lake.write_parquet(pd.DataFrame(), "silver", "test", "hour_utc") == []
    with pytest.raises(ValueError, match="null"):
        lake.write_parquet(pd.DataFrame({"hour_utc": [None]}), "silver", "test", "hour_utc")


def test_gold_from_silver_replay_priority_and_missing_sources(s3, monkeypatch):
    monkeypatch.setattr("urbanhub_common.db.get_connection", Mock(side_effect=AssertionError("SQL must not be used")))
    time = pd.Timestamp("2026-10-01T00:00Z")
    for source, temperature in [("noaa", 10), ("open-meteo", 99)]:
        lake.write_parquet(pd.DataFrame([dict(city="Paris", hour_utc=time, source=source,
            temperature=temperature, precipitation=0, wind_speed=2)]), "silver", "meteo/city_hourly", "hour_utc")
    # A later replay corrects NOAA without double-counting it.
    lake.write_parquet(pd.DataFrame([dict(city="Paris", hour_utc=time, source="noaa",
        temperature=11, precipitation=0, wind_speed=2)]), "silver", "meteo/city_hourly", "hour_utc")
    measurements = pd.DataFrame([
        dict(city="Paris", sensor_id=sensor, measured_at=time + pd.Timedelta(minutes=minute),
             pollutant="pm25", unit="ug/m3", value=value)
        for sensor, minute, value in [(1, 0, 10), (1, 15, 10), (2, 0, 30)]])
    lake.write_parquet(measurements, "silver", "openaq/measurements", "measured_at")
    lake.write_parquet(measurements, "silver", "openaq/measurements", "measured_at")
    frame, paths = publish("2026-10-01", "2026-10-02")
    assert len(frame) == 1
    assert frame.iloc[0].temperature == 11
    assert frame.iloc[0]["pollution_pm25_ug/m3"] == 20
    assert frame.iloc[0].meteo_source == "noaa"
    assert len(paths) == 1 and "/gold/analytics/city_hourly/date=2026-10-01/" in paths[0]
    stored = s3.get_object(Bucket=lake.lake_bucket(), Key=keys(s3, "gold/")[0])["Body"].read()
    assert len(pd.read_parquet(io.BytesIO(stored))) == 1
    with pytest.raises(RuntimeError, match="No Silver"):
        build_city_hourly("2020-01-01", "2020-01-02")


def test_citybikes_writes_only_minio_and_is_queryable(s3, monkeypatch):
    import main as citybikes
    from urbanhub_common import read_sql
    record = dict(station_id="a", longitude=2.3, latitude=48.8, free_slots=6,
                  station_name="Test", timestamp="2026-10-01T00:00Z", bikes_available=4,
                  network_id="velib", city="Paris", collected_at="2026-10-01T00:00:00Z")
    monkeypatch.setattr(citybikes, "snapshot", lambda *a: ({"network": "raw"}, [record]))
    assert citybikes.collect_one(s3, "velib", "Paris", record["collected_at"]) == 1
    assert len(keys(s3,"bronze/")) == 1 and len(keys(s3,"silver/")) == 1
    assert not hasattr(citybikes,"db_sink")
    gold = build_city_hourly("2026-10-01", "2026-10-02")
    assert gold.iloc[0].bikes_available == 4
    station = read_sql("SELECT * FROM mobility.stations").iloc[0]
    assert station.bikes_available == 4
    snap = read_sql("SELECT * FROM mobility.network_snapshots").iloc[0]
    assert snap.n_stations == 1 and snap.bikes_available == 4


def test_citybikes_storage_failure_does_not_publish_silver(s3, monkeypatch):
    import main as citybikes
    monkeypatch.setattr(citybikes, "snapshot", lambda *a: ({"raw": True}, []))
    client = Mock()
    client.put_object.side_effect = RuntimeError("storage failed")
    assert citybikes.collect_one(client, "velib", "Paris", "2026-10-01T00:00:00Z") == 0
    assert len(keys(s3,"silver/")) == 0


def test_pollution_files_supply_sensors_and_watermarks(s3):
    from pollution_iot import repository
    from urbanhub_common import read_sql
    sensor = dict(sensor_id=1,location_id=12,location_name="Test",city="Paris",pollutant="pm25",
                  unit_source="µg/m³",latitude=48.8,longitude=2.3,provider="Test",
                  datetime_first=None,datetime_last=None,active=True)
    assert repository.upsert_sensors([sensor]) == 1
    assert repository.active_sensors()[0]["sensor_id"] == 1
    time = pd.Timestamp("2026-10-01T00:00Z")
    record = dict(sensor_id=1,measured_at=time,pollutant="pm25",value=10,unit="µg/m³",
                  value_raw=10,unit_raw="µg/m³",latitude=48.8,longitude=2.3,city="Paris",source="stream")
    assert repository.insert_measurements([record]) == 1
    assert repository.insert_measurements([record]) == 1
    assert repository.last_timestamps()[1] == time
    # Replayed objects do not double-count analytical measurements.
    assert read_sql("SELECT count(*) AS n FROM pollution.measurements").iloc[0].n == 1
    assert read_sql("SELECT * FROM pollution.v_city_levels").iloc[0].mean_value == 10
    assert read_sql("SELECT * FROM pollution.v_zone_levels WHERE pollutant=%s",("pm25",)).iloc[0].mean_value == 10


def test_ops_journal_is_persisted_in_metadata_and_queryable(s3):
    from urbanhub_common.ops import start_run,finish_run
    from urbanhub_common import read_sql
    run = start_run("meteo","test")
    finish_run(run,"success",rows_received=4,rows_inserted=3)
    assert len(keys(s3,"metadata/ingestion_runs/")) == 2
    frame = read_sql("SELECT * FROM ops.ingestion_runs")
    assert len(frame) == 1
    assert frame.iloc[0].status == "success" and frame.iloc[0].rows_inserted == 3


def test_empty_lake_all_dashboard_views_bind(s3):
    from urbanhub_common import get_connection
    with get_connection() as conn:
        views = conn.execute("SELECT schema_name,view_name FROM duckdb_views() WHERE schema_name IN ('pollution','analytics')").fetchall()
        assert len(views) >= 17
        for schema,name in views:
            assert len(conn.execute(f'SELECT * FROM "{schema}"."{name}"').fetchdf()) == 0 if name != "thresholds" else True
        assert conn.execute("SELECT count(*) FROM ref.cities").fetchone()[0] == 11


def test_weather_pipeline_load_writes_files_and_cursor(s3):
    from meteo_batch import load
    from urbanhub_common import read_sql
    times = pd.date_range("2026-10-01",periods=8,freq="h",tz="UTC")
    frame = pd.DataFrame(dict(city="Paris",hour_utc=times,source="open-meteo",temperature=10,
                             wind_speed=2,wind_direction=180,pressure=1013,precipitation=0,visibility=10000,n_stations=1))
    lake.write_parquet(frame,"silver","meteo/city_hourly","hour_utc")
    load.stations(pd.DataFrame([dict(station_id="OPENMETEO-Paris",city="Paris",source="open-meteo")]))
    assert load.dernieres_heures()["Paris"] == times[-1]
    assert load.recalculer_journalier_openmeteo() == 1
    daily = read_sql("SELECT d.*,s.city FROM meteo.daily d JOIN meteo.stations s USING(station_id)")
    assert len(daily) == 1 and daily.iloc[0].temp_mean == 10
    crossed = read_sql("SELECT * FROM analytics.v_city_hourly_all")
    assert len(crossed) == 8 and crossed.iloc[0].hour_local == 2


def test_openmeteo_ingestion_runs_without_any_database(s3, monkeypatch):
    from meteo_batch import __main__ as pipeline
    from urbanhub_common import read_sql
    monkeypatch.setattr(pipeline.load,"villes",lambda:[("Paris",48.8,2.3)])
    times=pd.date_range("2026-10-01",periods=8,freq="h",tz="UTC")
    def fetch(city,lat,lon,raw):
        raw.add({"city":city},{"hourly":{"time":[str(t) for t in times]}})
        raw.flush()
        return pd.DataFrame(dict(station_id="OPENMETEO-Paris",timestamp=times,temperature=10,
             wind_speed=2,wind_direction=180,pressure=1013,precipitation=0,precip_period_h=1,visibility=10000))
    monkeypatch.setattr(pipeline.openmeteo,"fetch_live",fetch)
    pipeline.run_openmeteo("live")
    assert len(keys(s3,"bronze/open-meteo/"))==1
    assert len(keys(s3,"silver/meteo/city_hourly/"))==1
    assert len(read_sql("SELECT * FROM meteo.daily"))==1
    assert read_sql("SELECT * FROM ops.ingestion_runs").iloc[0].status=="success"


def test_pollution_poll_runs_from_sensor_files_only(s3, monkeypatch):
    from pollution_iot import repository,pipeline
    from urbanhub_common import read_sql
    repository.upsert_sensors([dict(sensor_id=1,location_id=2,location_name="Test",city="Paris",pollutant="pm25",
        unit_source="µg/m³",latitude=48.8,longitude=2.3,provider="Test",datetime_first=None,datetime_last=None,active=True)])
    record=dict(sensor_id=1,measured_at=pd.Timestamp("2026-10-01T00:00Z"),pollutant="pm25",value=10,
        unit="µg/m³",value_raw=10,unit_raw="µg/m³",latitude=48.8,longitude=2.3,city="Paris",source="stream")
    class Client:
        def __init__(self,raw):
            self.raw=raw
            self.calls=0
        def location_latest(self,location):
            self.calls+=1
            self.raw.add({"location":location},{"results":[{"value":10}]})
            self.raw.flush()
            return [{"value":10}]
    monkeypatch.setattr(pipeline,"OpenAQClient",Client)
    monkeypatch.setattr(pipeline.transform,"records_from_latest",lambda *args:([record],{}))
    assert pipeline.poll()==1
    assert len(keys(s3,"bronze/openaq/"))==1
    assert read_sql("SELECT * FROM pollution.measurements").iloc[0].value==10
    assert read_sql("SELECT * FROM ops.ingestion_runs").iloc[0].status=="success"


def test_runtime_contains_no_external_database_dependency():
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    config=(root/"docker-compose.yml").read_text(encoding="utf-8").lower()
    assert "db-migrate" not in config
    assert "postgres" not in config
    assert "supabase" not in config
    deps=(root/"shared/pyproject.toml").read_text(encoding="utf-8").lower()
    assert "psycopg" not in deps
    assert "duckdb" in deps
    assert not (root/"supabase").exists()


def test_citybikes_poll_is_visible_in_common_ingestion_journal(s3,monkeypatch):
    import main as citybikes
    from urbanhub_common import read_sql
    monkeypatch.setattr(citybikes,"list_networks",lambda cities:[("velib","Paris")])
    monkeypatch.setattr(citybikes,"collect_one",lambda *args:3372)
    assert citybikes.collect(s3,["Paris"])==3372
    row=read_sql("SELECT * FROM ops.ingestion_runs").iloc[0]
    assert row.part=="mobility" and row["mode"]=="citybikes-poll"
    assert row.status=="success" and row.rows_inserted==3372
    assert pd.notna(row.finished_at)


def test_citybikes_failed_network_is_not_reported_as_success(s3,monkeypatch):
    import main as citybikes
    from urbanhub_common import read_sql
    monkeypatch.setattr(citybikes,"list_networks",lambda cities:[("velib","Paris"),("velov","Lyon")])
    monkeypatch.setattr(citybikes,"collect_one",lambda client,network,*args:100 if network=="velib" else 0)
    assert citybikes.collect(s3,["Paris","Lyon"])==100
    row=read_sql("SELECT * FROM ops.ingestion_runs").iloc[0]
    assert row.status=="partial" and row.rows_inserted==100
    assert "1/2" in row.error


def test_citybikes_rate_limit_is_logged_and_propagated(s3,monkeypatch):
    import main as citybikes
    from urbanhub_common import read_sql
    monkeypatch.setattr(citybikes,"list_networks",lambda cities:[("velib","Paris")])
    def limited(*args):
        raise citybikes.ApiRateLimited("hourly quota")
    monkeypatch.setattr(citybikes,"collect_one",limited)
    with pytest.raises(citybikes.ApiRateLimited):
        citybikes.collect(s3,["Paris"])
    row=read_sql("SELECT * FROM ops.ingestion_runs").iloc[0]
    assert row.status=="error" and "hourly quota" in row.error
