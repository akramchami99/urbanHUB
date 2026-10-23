import pandas as pd
import pytest
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.io.fsspec import FsspecFileIO
from urbanhub_common import iceberg

@pytest.fixture
def local_catalog(tmp_path,monkeypatch):
    # Isolated SQLite test catalog; production uses persistent Nessie REST/RocksDB.
    cat=SqlCatalog("test",uri="sqlite:///"+str(tmp_path/"catalog.db"),warehouse=(tmp_path/"warehouse").as_uri(), **{"py-io-impl":"pyiceberg.io.fsspec.FsspecFileIO"})
    monkeypatch.setattr(iceberg,"catalog",lambda:cat)
    monkeypatch.setattr(iceberg,"PyArrowFileIO",FsspecFileIO)
    monkeypatch.setattr(iceberg,"io_properties",lambda:{"py-io-impl":"pyiceberg.io.fsspec.FsspecFileIO"})
    for name,value in {"S3_ENDPOINT_URL":"http://localhost:9002","S3_ACCESS_KEY_ID":"test",
                       "S3_SECRET_ACCESS_KEY":"test","S3_REGION":"us-east-1"}.items():
        monkeypatch.setenv(name,value)
    return cat

def weather(value=10.0):
    return pd.DataFrame([dict(city="Paris",hour_utc=pd.Timestamp("2026-09-01T00:00Z"),source="noaa",temperature=value)])

def test_iceberg_upsert_partitioning_and_old_committed_version(local_catalog):
    old=iceberg.write_table(weather(),"silver","meteo/city_hourly","hour_utc")[0]
    iceberg.write_table(weather(20.0),"silver","meteo/city_hourly","hour_utc")
    table=iceberg.load_table("silver","meteo/city_hourly")
    assert table.metadata.format_version==2
    assert str(table.spec().fields[0].transform)=="day"
    current=iceberg.scan_table("meteo/city_hourly")
    assert len(current)==1 and current.iloc[0].temperature==20
    previous=iceberg.scan_table("meteo/city_hourly",metadata_location=old)
    assert len(previous)==1 and previous.iloc[0].temperature==10

def test_iceberg_additive_schema_and_timestamp_filter(local_catalog):
    iceberg.write_table(weather(),"silver","meteo/city_hourly","hour_utc")
    changed=weather(15.0).assign(quality="checked")
    iceberg.write_table(changed,"silver","meteo/city_hourly","hour_utc")
    assert "quality" in iceberg.scan_table("meteo/city_hourly")
    assert len(iceberg.scan_table("meteo/city_hourly",start="2026-09-01",end="2026-09-02"))==1
    assert iceberg.scan_table("meteo/city_hourly",start="2026-09-02",end="2026-09-03").empty

def test_iceberg_rejects_null_keys_and_partition_time(local_catalog):
    with pytest.raises(ValueError,match="partition timestamp"):
        iceberg.write_table(weather().assign(hour_utc=None),"silver","meteo/city_hourly","hour_utc")
    with pytest.raises(ValueError,match="business keys"):
        iceberg.write_table(weather().assign(city=None),"silver","meteo/city_hourly","hour_utc")
    assert iceberg.write_table(pd.DataFrame(),"silver","meteo/city_hourly","hour_utc")==[]


def test_large_merge_preserves_rows_outside_batch(local_catalog,monkeypatch):
    monkeypatch.setattr(iceberg,"MERGE_OVERWRITE_THRESHOLD",2)
    first=pd.concat([weather().assign(city="Paris"),weather().assign(city="Lyon")],ignore_index=True)
    old=iceberg.write_table(first,"silver","meteo/city_hourly","hour_utc")[0]
    batch=pd.concat([weather(20),weather(30).assign(city="Lille")],ignore_index=True)
    iceberg.write_table(batch,"silver","meteo/city_hourly","hour_utc")
    current=iceberg.scan_table("meteo/city_hourly").set_index("city")
    assert current.temperature.to_dict()=={"Paris":20,"Lyon":10,"Lille":30}
    assert len(iceberg.scan_table("meteo/city_hourly",metadata_location=old))==2
