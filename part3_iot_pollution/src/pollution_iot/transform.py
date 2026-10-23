"""Nettoyage et harmonisation des données OpenAQ (fonctions pures, testées dans tests/).

Règles de nettoyage :
  - on ne garde que les 5 polluants de l'énoncé (pm25, pm10, o3, no2, co)
  - valeurs manquantes, non numériques, négatives ou sentinelles (-999) : rejetées
  - unités harmonisées en µg/m³ (ppm/ppb -> µg/m³ via la masse molaire, mg/m³ -> µg/m³)
  - valeurs physiquement impossibles (> PLAUSIBLE_MAX) : rejetées
  - timestamp : ISO 8601 -> datetime UTC (fin de la période de mesure)
  - coordonnées manquantes : celles du site de mesure
  - doublons (même capteur, même instant) : supprimés
"""

import math
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import config

TARGET_UNIT = "µg/m³"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _norm_unit(unit: str | None) -> str:
    return (unit or "").strip().lower().replace("μ", "µ").replace("ug", "µg").replace("m3", "m³")


def to_ugm3(pollutant: str, value: float, unit: str | None) -> float | None:
    """Convertit une concentration en µg/m³. None si l'unité est inconnue."""
    u = _norm_unit(unit)
    if u in ("µg/m³", ""):
        return value
    if u == "mg/m³":
        return value * 1000
    if u in ("ppm", "ppb"):
        molar = config.MOLAR_MASS.get(pollutant)
        if molar is None:
            return None
        ppb = value * 1000 if u == "ppm" else value
        return ppb * molar / config.MOLAR_VOLUME_L
    return None


def clean_value(pollutant: str, value, unit: str | None) -> tuple[float | None, str | None]:
    """Renvoie (valeur en µg/m³, None) ou (None, motif de rejet)."""
    if pollutant not in config.POLLUTANTS:
        return None, "polluant_hors_perimetre"
    if value is None:
        return None, "valeur_manquante"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None, "valeur_non_numerique"
    if math.isnan(value) or value < 0:
        return None, "valeur_negative_ou_nan"
    converted = to_ugm3(pollutant, value, unit)
    if converted is None:
        return None, "unite_inconnue"
    if converted > config.PLAUSIBLE_MAX[pollutant]:
        return None, "valeur_aberrante"
    return round(converted, 3), None


# ---------------------------------------------------------------------------
# Découverte des capteurs
# ---------------------------------------------------------------------------
def sensors_from_location(location: dict, city: str, now: datetime | None = None) -> list[dict]:
    """Extrait les capteurs utiles d'un objet 'location' OpenAQ."""
    now = now or datetime.now(timezone.utc)
    if location.get("isMobile"):
        return []
    country = (location.get("country") or {}).get("code")
    if country and country != config.COUNTRY_ISO:
        return []

    coords = location.get("coordinates") or {}
    last = parse_ts((location.get("datetimeLast") or {}).get("utc"))
    first = parse_ts((location.get("datetimeFirst") or {}).get("utc"))
    active = last is not None and last >= now - timedelta(days=config.SENSOR_INACTIVE_AFTER_DAYS)

    sensors = []
    for s in location.get("sensors") or []:
        param = s.get("parameter") or {}
        if param.get("name") not in config.POLLUTANTS:
            continue
        sensors.append(
            {
                "sensor_id": s["id"],
                "location_id": location["id"],
                "location_name": location.get("name"),
                "city": city,
                "pollutant": param["name"],
                "unit_source": param.get("units"),
                "latitude": coords.get("latitude"),
                "longitude": coords.get("longitude"),
                "provider": (location.get("provider") or {}).get("name"),
                "datetime_first": first,
                "datetime_last": last,
                "active": active,
            }
        )
    return sensors


# ---------------------------------------------------------------------------
# Mesures
# ---------------------------------------------------------------------------
def _record(sensor: dict, value, unit, ts: datetime | None, coords: dict | None, source: str):
    if ts is None:
        return None, "timestamp_invalide"
    now = datetime.now(timezone.utc)
    if ts > now + timedelta(hours=1):
        return None, "timestamp_futur"
    # /latest renvoie aussi la dernière valeur de capteurs arrêtés depuis des années
    if source == "stream" and ts < now - timedelta(days=config.SENSOR_INACTIVE_AFTER_DAYS):
        return None, "mesure_perimee"
    clean, reason = clean_value(sensor["pollutant"], value, unit)
    if reason:
        return None, reason
    coords = coords or {}
    return {
        "sensor_id": sensor["sensor_id"],
        "measured_at": ts,
        "pollutant": sensor["pollutant"],
        "value": clean,
        "unit": TARGET_UNIT,
        "value_raw": float(value),
        "unit_raw": unit,
        "latitude": coords.get("latitude") or sensor.get("latitude"),
        "longitude": coords.get("longitude") or sensor.get("longitude"),
        "city": sensor["city"],
        "source": source,
    }, None


def records_from_measurements(sensor: dict, results: list[dict], source: str = "backfill"):
    """Résultats de /sensors/{id}/measurements -> (enregistrements propres, compteur de rejets)."""
    records, rejected = [], Counter()
    for r in results:
        period = r.get("period") or {}
        ts = parse_ts((period.get("datetimeTo") or {}).get("utc"))
        unit = (r.get("parameter") or {}).get("units") or sensor.get("unit_source")
        rec, reason = _record(sensor, r.get("value"), unit, ts, r.get("coordinates"), source)
        if rec:
            records.append(rec)
        else:
            rejected[reason] += 1
    return dedupe(records), rejected


def records_from_latest(sensors_by_id: dict[int, dict], results: list[dict], source: str = "stream"):
    """Résultats de /locations/{id}/latest -> (enregistrements propres, compteur de rejets)."""
    records, rejected = [], Counter()
    for r in results:
        sensor = sensors_by_id.get(r.get("sensorsId"))
        if sensor is None:  # capteur d'un polluant non retenu
            continue
        ts = parse_ts((r.get("datetime") or {}).get("utc"))
        rec, reason = _record(sensor, r.get("value"), sensor.get("unit_source"), ts, r.get("coordinates"), source)
        if rec:
            records.append(rec)
        else:
            rejected[reason] += 1
    return dedupe(records), rejected


def dedupe(records: list[dict]) -> list[dict]:
    seen = {}
    for r in records:
        seen[(r["sensor_id"], r["measured_at"])] = r
    return list(seen.values())
