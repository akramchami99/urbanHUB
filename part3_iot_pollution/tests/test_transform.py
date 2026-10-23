from datetime import datetime, timedelta, timezone

import pytest

from pollution_iot import transform

SENSOR = {"sensor_id": 1, "pollutant": "no2", "unit_source": "µg/m³", "city": "Paris",
          "latitude": 48.85, "longitude": 2.35}


@pytest.mark.parametrize("unit", ["µg/m³", "μg/m³", "ug/m3", "UG/M3"])
def test_ugm3_variants_unchanged(unit):
    assert transform.to_ugm3("no2", 40.0, unit) == 40.0


def test_ppb_and_ppm_conversion():
    assert transform.to_ugm3("no2", 10, "ppb") == pytest.approx(10 * 46.01 / 24.45)
    assert transform.to_ugm3("o3", 0.05, "ppm") == pytest.approx(50 * 48.0 / 24.45)
    assert transform.to_ugm3("co", 1, "mg/m³") == 1000
    assert transform.to_ugm3("pm25", 10, "ppm") is None  # pas de masse molaire pour des particules


@pytest.mark.parametrize("value,reason", [
    (None, "valeur_manquante"), ("abc", "valeur_non_numerique"),
    (-999, "valeur_negative_ou_nan"), (float("nan"), "valeur_negative_ou_nan"),
    (5000, "valeur_aberrante"),
])
def test_clean_value_rejections(value, reason):
    assert transform.clean_value("no2", value, "µg/m³") == (None, reason)


def test_clean_value_out_of_scope_pollutant():
    assert transform.clean_value("so2", 3, "µg/m³") == (None, "polluant_hors_perimetre")


def test_parse_ts_to_utc():
    assert transform.parse_ts("2026-10-01T12:00:00+02:00") == datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    assert transform.parse_ts("2026-10-01T10:00:00Z") == datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    assert transform.parse_ts("pas une date") is None


def test_sensors_from_location_filters():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    location = {
        "id": 10, "name": "Paris 13e", "isMobile": False,
        "country": {"code": "FR"}, "coordinates": {"latitude": 48.83, "longitude": 2.36},
        "provider": {"name": "EEA"},
        "datetimeFirst": {"utc": "2020-01-01T00:00:00Z"}, "datetimeLast": {"utc": "2026-10-20T23:00:00Z"},
        "sensors": [
            {"id": 100, "parameter": {"name": "no2", "units": "µg/m³"}},
            {"id": 101, "parameter": {"name": "so2", "units": "µg/m³"}},
            {"id": 102, "parameter": {"name": "pm25", "units": "µg/m³"}},
        ],
    }
    sensors = transform.sensors_from_location(location, "Paris", now)
    assert [s["sensor_id"] for s in sensors] == [100, 102]
    assert all(s["active"] and s["city"] == "Paris" for s in sensors)

    assert transform.sensors_from_location({**location, "isMobile": True}, "Paris", now) == []
    assert transform.sensors_from_location({**location, "country": {"code": "BE"}}, "Lille", now) == []


def test_records_from_measurements_cleans_and_dedupes():
    results = [
        {"value": 30, "parameter": {"units": "µg/m³"}, "coordinates": None,
         "period": {"datetimeTo": {"utc": "2026-10-20T10:00:00Z"}}},
        {"value": 30, "parameter": {"units": "µg/m³"}, "coordinates": None,
         "period": {"datetimeTo": {"utc": "2026-10-20T10:00:00Z"}}},  # doublon
        {"value": -1, "parameter": {"units": "µg/m³"},
         "period": {"datetimeTo": {"utc": "2026-10-20T11:00:00Z"}}},
        {"value": 12, "period": {}},
    ]
    records, rejected = transform.records_from_measurements(SENSOR, results)
    assert len(records) == 1
    assert records[0]["latitude"] == 48.85 and records[0]["unit"] == "µg/m³"
    assert rejected == {"valeur_negative_ou_nan": 1, "timestamp_invalide": 1}


def _iso(delta_hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).strftime("%Y-%m-%dT%H:00:00Z")


def test_records_from_latest_ignores_unknown_sensors():
    results = [
        {"sensorsId": 1, "value": 20, "datetime": {"utc": _iso(-2)},
         "coordinates": {"latitude": 48.9, "longitude": 2.4}},
        {"sensorsId": 999, "value": 5, "datetime": {"utc": _iso(-2)}},
    ]
    records, rejected = transform.records_from_latest({1: SENSOR}, results)
    assert len(records) == 1 and records[0]["source"] == "stream"
    assert records[0]["latitude"] == 48.9
    assert not rejected


def test_records_from_latest_rejects_stale_and_future():
    sensor2 = {**SENSOR, "sensor_id": 2}
    results = [
        {"sensorsId": 1, "value": 20, "datetime": {"utc": "2018-01-18T10:00:00Z"}},  # capteur arrêté
        {"sensorsId": 2, "value": 20, "datetime": {"utc": _iso(5)}},
    ]
    records, rejected = transform.records_from_latest({1: SENSOR, 2: sensor2}, results)
    assert records == []
    assert rejected == {"mesure_perimee": 1, "timestamp_futur": 1}
