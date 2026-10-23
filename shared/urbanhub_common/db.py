"""Read-only SQL over Iceberg snapshots using an in-memory DuckDB engine.

No database server, database URL, relational persistence, migrations or SQL writes.
The historical schema names are query aliases for object datasets only.
"""
from pathlib import Path

import duckdb
from dotenv import load_dotenv

from .lake import read_dataset
from .reference import cities, thresholds

load_dotenv()

# name: (zone, object dataset, business key, typed query columns)
DATASETS = {
 "meteo.stations": ("silver", "meteo/stations", ["station_id"],
    "station_id VARCHAR,name VARCHAR,latitude DOUBLE,longitude DOUBLE,city VARCHAR,distance_km DOUBLE,source VARCHAR"),
 "meteo.daily": ("gold", "meteo/daily", ["station_id","date"],
    "station_id VARCHAR,date DATE,temp_mean DOUBLE,temp_min DOUBLE,temp_max DOUBLE,wind_max DOUBLE,pressure_mean DOUBLE,visibility_mean DOUBLE,precipitation DOUBLE,nb_obs BIGINT"),
 "meteo.city_hourly": ("silver", "meteo/city_hourly", ["city","hour_utc"],
    "city VARCHAR,hour_utc TIMESTAMPTZ,source VARCHAR,temperature DOUBLE,wind_speed DOUBLE,wind_direction DOUBLE,pressure DOUBLE,precipitation DOUBLE,visibility DOUBLE,n_stations BIGINT"),
 "pollution.sensors": ("silver", "openaq/sensors", ["sensor_id"],
    "sensor_id BIGINT,location_id BIGINT,location_name VARCHAR,city VARCHAR,pollutant VARCHAR,unit_source VARCHAR,latitude DOUBLE,longitude DOUBLE,provider VARCHAR,datetime_first TIMESTAMPTZ,datetime_last TIMESTAMPTZ,active BOOLEAN"),
 "pollution.measurements": ("silver", "openaq/measurements", ["sensor_id","measured_at"],
    "sensor_id BIGINT,measured_at TIMESTAMPTZ,pollutant VARCHAR,value DOUBLE,unit VARCHAR,value_raw DOUBLE,unit_raw VARCHAR,latitude DOUBLE,longitude DOUBLE,city VARCHAR,source VARCHAR"),
 "mobility.city_hourly_history": ("silver", "citybikes-archive/city_hourly", ["city","hour_utc"],
    "city VARCHAR,hour_utc TIMESTAMPTZ,n_stations BIGINT,bikes_available DOUBLE,free_slots DOUBLE,availability_ratio DOUBLE,share_low_supply DOUBLE,share_empty DOUBLE,share_full DOUBLE,departures DOUBLE,returns DOUBLE,n_changes BIGINT"),
 "mobility.observations": ("silver", "citybikes", ["network_id","station_id","collected_at"],
    "network_id VARCHAR,station_id VARCHAR,station_name VARCHAR,city VARCHAR,latitude DOUBLE,longitude DOUBLE,bikes_available DOUBLE,free_slots DOUBLE,timestamp TIMESTAMPTZ,collected_at TIMESTAMPTZ"),
 "ops.ingestion_runs": ("metadata", "ingestion_runs", ["run_id"],
    "run_id VARCHAR,part VARCHAR,mode VARCHAR,status VARCHAR,started_at TIMESTAMPTZ,finished_at TIMESTAMPTZ,api_calls BIGINT,rows_received BIGINT,rows_inserted BIGINT,rows_rejected BIGINT,raw_path VARCHAR,error VARCHAR"),
}


def _register(conn, name, frame, columns):
    definition = [column.strip().split() for column in columns.split(",")]
    if frame.empty:
        projection = ", ".join(f'CAST(NULL AS {kind}) AS "{column}"' for column, kind in definition)
        conn.execute(f"CREATE VIEW {name} AS SELECT {projection} WHERE FALSE")
        return
    alias = "_file_" + name.replace(".", "_")
    conn.register(alias, frame)
    projection = ", ".join(
        f'CAST("{column}" AS {kind}) AS "{column}"' if column in frame.columns else
        f'CAST(NULL AS {kind}) AS "{column}"' for column, kind in definition)
    conn.execute(f"CREATE VIEW {name} AS SELECT {projection} FROM {alias}")


def get_connection():
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("SET TimeZone='UTC'")
        for schema in ("ref","ops","meteo","mobility","pollution","analytics"):
            conn.execute(f"CREATE SCHEMA {schema}")
        conn.register("_cities", cities())
        conn.execute("CREATE VIEW ref.cities AS SELECT * FROM _cities")
        conn.register("_thresholds", thresholds())
        conn.execute("CREATE VIEW pollution.thresholds AS SELECT * FROM _thresholds")
        for name, (zone, dataset, key, columns) in DATASETS.items():
            frame = read_dataset(dataset, zone)
            if not frame.empty:
                frame = frame.sort_values("_lake_object", kind="stable")
                if name == "meteo.city_hourly":
                    frame["_priority"] = frame.source.eq("noaa").astype(int)
                    frame = frame.sort_values("_priority", kind="stable")
                frame = frame.drop_duplicates(key, keep="last")
            _register(conn, name, frame, columns)
        conn.execute("""
            CREATE VIEW mobility.stations AS
            SELECT network_id,station_id,station_name,city,latitude,longitude,bikes_available,free_slots,
                   timestamp AS station_ts,collected_at
            FROM mobility.observations
            QUALIFY row_number() OVER (PARTITION BY network_id,station_id ORDER BY collected_at DESC)=1;
            CREATE VIEW mobility.network_snapshots AS
            SELECT network_id,city,collected_at,count(*) AS n_stations,
                   sum(bikes_available) AS bikes_available,sum(free_slots) AS free_slots,
                   count(*) FILTER (WHERE bikes_available/(bikes_available+free_slots)<=0.1) AS n_low_supply,
                   count(*) FILTER (WHERE bikes_available=0) AS n_empty,
                   count(*) FILTER (WHERE free_slots=0) AS n_full
            FROM mobility.observations
            WHERE bikes_available>=0 AND free_slots>=0 AND bikes_available+free_slots>0
            GROUP BY network_id,city,collected_at;
        """)
        for sql_file in sorted((Path(__file__).parent / "sql").glob("*.sql")):
            conn.execute(sql_file.read_text(encoding="utf-8"))
        return conn
    except Exception:
        conn.close()
        raise


def read_sql(query, params=None):
    """Run analytical SQL over file-backed views; %s parameters are accepted for notebooks."""
    with get_connection() as conn:
        if params is not None:
            query = query.replace("%s", "?")
        return conn.execute(query, params).fetchdf()
