"""Client HTTP OpenAQ v3 : authentification, respect des quotas, reprise sur erreur, pagination."""

import time
from collections import deque
from datetime import datetime
from typing import Iterator

import requests
from urbanhub_common import RawWriter, get_logger

from . import config

log = get_logger("openaq")


class OpenAQClient:
    # partagé entre toutes les instances : le quota OpenAQ est par clé, pas par exécution
    _recent: deque[float] = deque()

    def __init__(self, api_key: str = config.OPENAQ_API_KEY, raw: RawWriter | None = None):
        if not api_key:
            raise RuntimeError("OPENAQ_API_KEY manquant dans le .env")
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key, "Accept": "application/json"})
        self.raw = raw
        self.calls = 0

    # -- quotas ---------------------------------------------------------
    def _throttle(self) -> None:
        now = time.monotonic()
        while self._recent and now - self._recent[0] > 3600:
            self._recent.popleft()
        last_minute = sum(1 for t in self._recent if now - t < 60)
        if last_minute >= config.OPENAQ_MAX_PER_MINUTE:
            oldest_in_minute = next(t for t in self._recent if now - t < 60)
            time.sleep(60 - (now - oldest_in_minute) + 0.5)
        if len(self._recent) >= config.OPENAQ_MAX_PER_HOUR:
            wait = 3600 - (now - self._recent[0]) + 1
            log.warning("quota horaire OpenAQ presque atteint, pause de %.0f s", wait)
            time.sleep(wait)
        self._recent.append(time.monotonic())

    def get(self, path: str, params: dict | None = None, retries: int = 5) -> dict:
        url = f"{config.OPENAQ_BASE_URL}{path}"
        for attempt in range(1, retries + 1):
            self._throttle()
            self.calls += 1
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                log.warning("erreur réseau %s (tentative %d) : %s", path, attempt, exc)
                time.sleep(2**attempt)
                continue

            if resp.status_code == 429:
                reset = resp.headers.get("x-ratelimit-reset", "60")
                wait = int(reset) if reset.isdigit() and int(reset) <= 3600 else 60
                log.warning("429 Too Many Requests, pause de %d s", wait)
                time.sleep(wait + 1)
                continue
            if resp.status_code >= 500:
                log.warning("HTTP %d sur %s (tentative %d)", resp.status_code, path, attempt)
                time.sleep(2**attempt)
                continue
            if resp.status_code == 404:
                return {"results": []}
            resp.raise_for_status()

            payload = resp.json()
            if self.raw is not None:
                self.raw.add({"path": path, "params": params}, payload)
                self.raw.flush()
            return payload
        raise RuntimeError(f"OpenAQ : échec après {retries} tentatives sur {path}")

    def paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        params = dict(params or {}, limit=config.OPENAQ_PAGE_LIMIT, page=1)
        while True:
            results = self.get(path, params).get("results", [])
            yield from results
            if len(results) < config.OPENAQ_PAGE_LIMIT:
                return
            params["page"] += 1

    # -- endpoints ------------------------------------------------------
    def locations_near(self, lat: float, lon: float, radius_m: int) -> Iterator[dict]:
        return self.paginate(
            "/locations", {"coordinates": f"{lat:.4f},{lon:.4f}", "radius": radius_m}
        )

    def location_latest(self, location_id: int) -> list[dict]:
        return list(self.paginate(f"/locations/{location_id}/latest"))

    def sensor_measurements(self, sensor_id: int, dt_from: datetime, dt_to: datetime) -> list[dict]:
        return list(
            self.paginate(
                f"/sensors/{sensor_id}/measurements",
                {
                    "datetime_from": dt_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "datetime_to": dt_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )
        )
