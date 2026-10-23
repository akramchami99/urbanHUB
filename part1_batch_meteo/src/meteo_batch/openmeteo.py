"""Complément Open-Meteo : météo horaire des villes de ref.cities après la fin des données NOAA (août 2025).

API gratuite, sans clé. Mêmes unités que le nettoyage NOAA (°C, km/h, degrés, hPa, mm, m).
  - historique : historical-forecast-api (archives des modèles, visibilité incluse)
  - temps réel : api.open-meteo.com/v1/forecast avec past_days
"""

import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
from urbanhub_common import RawWriter, get_logger

from .config import OPENMETEO_HISTORY_URL, OPENMETEO_LIVE_URL, OPENMETEO_VARS

log = get_logger("openmeteo")


def station_id(city: str) -> str:
    return f"OPENMETEO-{city}"


def _get(url: str, params: dict, raw: RawWriter | None) -> dict:
    for attempt in range(1, 5):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            payload = r.json()
            if raw is not None:
                raw.add({"url": url, "params": params}, payload)
                raw.flush()
            return payload
        except requests.RequestException as exc:
            log.warning("Open-Meteo : %s (tentative %d)", exc, attempt)
            time.sleep(5 * attempt)
    raise RuntimeError(f"Open-Meteo injoignable : {url}")


def _to_frame(payload: dict, city: str) -> pd.DataFrame:
    """Réponse Open-Meteo -> même format que le tableau horaire NOAA nettoyé."""
    h = payload.get("hourly") or {}
    if not h.get("time"):
        return pd.DataFrame()
    df = pd.DataFrame({col: h.get(var) for var, col in OPENMETEO_VARS.items()})
    df.insert(0, "timestamp", pd.to_datetime(h["time"], utc=True))   # timezone=GMT dans la requête
    df.insert(0, "station_id", station_id(city))
    df["precip_period_h"] = 1.0                                       # cumul sur l'heure
    now = pd.Timestamp.now(tz="UTC").floor("h")
    return df[df["timestamp"] <= now].dropna(subset=list(OPENMETEO_VARS.values()), how="all")


def fetch_history(city: str, lat: float, lon: float, start: date, end: date, raw: RawWriter | None):
    frames, cur = [], start
    while cur <= end:   # par tranches de 92 jours pour garder des réponses légères
        stop = min(cur + timedelta(days=91), end)
        payload = _get(OPENMETEO_HISTORY_URL, {
            "latitude": lat, "longitude": lon, "timezone": "GMT",
            "start_date": cur.isoformat(), "end_date": stop.isoformat(),
            "hourly": ",".join(OPENMETEO_VARS),
        }, raw)
        frames.append(_to_frame(payload, city))
        cur = stop + timedelta(days=1)
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_live(city: str, lat: float, lon: float, raw: RawWriter | None, past_days: int = 2):
    payload = _get(OPENMETEO_LIVE_URL, {
        "latitude": lat, "longitude": lon, "timezone": "GMT",
        "past_days": past_days, "forecast_days": 1, "hourly": ",".join(OPENMETEO_VARS),
    }, raw)
    return _to_frame(payload, city)


def today_utc() -> date:
    return datetime.now(timezone.utc).date()
