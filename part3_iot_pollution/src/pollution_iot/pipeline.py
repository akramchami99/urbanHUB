"""Les 3 étapes du flux IoT pollution.

  discover : trouve les capteurs OpenAQ autour des villes de ref.cities -> pollution.sensors
  backfill : rattrape l'historique (BACKFILL_DAYS) ou comble les trous depuis la dernière mesure
  poll     : simule le flux IoT ; à chaque tick, chaque site renvoie la dernière mesure de ses capteurs
"""

from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from urbanhub_common import RawWriter, get_logger

from . import config, repository, transform
from .openaq_client import OpenAQClient

log = get_logger("pipeline")


@contextmanager
def _run(mode: str):
    """Ouvre connexion + client + zone raw, et journalise l'exécution dans ops.ingestion_runs."""
    raw = RawWriter("openaq", "pollution", mode)
    client = OpenAQClient(raw=raw)
    stats = Counter()
    run_id = repository.start_run(mode)
    try:
        yield client, stats
    except Exception as exc:
        repository.finish_run(run_id, "error", api_calls=client.calls, raw_path=raw.flush(),
                              error=repr(exc)[:2000], **stats)
        raise
    repository.finish_run(run_id, "success", api_calls=client.calls, raw_path=raw.flush(), **stats)
    log.info("[%s] terminé : %d appels API, %s", mode, client.calls, dict(stats))


def discover() -> int:
    with _run("discover") as (client, stats):
        sensors: dict[int, dict] = {}
        for city, lat, lon in repository.load_cities():
            found = 0
            for location in client.locations_near(lat, lon, config.CITY_RADIUS_M):
                for s in transform.sensors_from_location(location, city):
                    sensors.setdefault(s["sensor_id"], s)
                    found += 1
            log.info("%-10s : %d capteurs trouvés", city, found)
        stats["rows_received"] = len(sensors)
        stats["rows_inserted"] = repository.upsert_sensors(list(sensors.values()))
        active = sum(s["active"] for s in sensors.values())
        log.info("découverte : %d capteurs dont %d actifs", len(sensors), active)
        return active


def backfill(days: int = config.BACKFILL_DAYS, full: bool = False) -> int:
    """Idempotent : pour chaque capteur, repart de sa dernière mesure connue (ou de J-days).
    full=True : recharge toute la fenêtre J-days, pour étendre l'historique vers le passé (doublons ignorés)."""
    with _run("backfill") as (client, stats):
        now = datetime.now(timezone.utc)
        horizon = now - timedelta(days=days)
        last = repository.last_timestamps()
        sensors = repository.active_sensors()
        for i, sensor in enumerate(sensors, 1):
            start = horizon if full else max(last.get(sensor["sensor_id"], horizon), horizon)
            if now - start < timedelta(hours=1):
                continue
            results = client.sensor_measurements(sensor["sensor_id"], start, now)
            records, rejected = transform.records_from_measurements(sensor, results, "backfill")
            stats["rows_received"] += len(results)
            stats["rows_rejected"] += sum(rejected.values())
            stats["rows_inserted"] += repository.insert_measurements(records)
            if i % 20 == 0 or i == len(sensors):
                log.info("backfill : %d/%d capteurs, %d lignes insérées", i, len(sensors), stats["rows_inserted"])
        return stats["rows_inserted"]


def poll() -> int:
    """Un 'tick' du flux IoT : on interroge chaque site de mesure comme s'il émettait ses valeurs."""
    with _run("stream") as (client, stats):
        by_location: dict[int, dict[int, dict]] = defaultdict(dict)
        for s in repository.active_sensors():
            by_location[s["location_id"]][s["sensor_id"]] = s
        if not by_location:
            log.warning("aucun capteur actif : lancer d'abord 'discover'")
            return 0

        rejected_total = Counter()
        for location_id, sensors in by_location.items():
            results = client.location_latest(location_id)
            records, rejected = transform.records_from_latest(sensors, results, "stream")
            rejected_total += rejected
            stats["rows_received"] += len(records) + sum(rejected.values())
            stats["rows_inserted"] += repository.insert_measurements(records)
            for r in records:
                log.debug("IoT msg %s", {k: r[k] for k in ("sensor_id", "pollutant", "measured_at", "value", "unit")})
        stats["rows_rejected"] = sum(rejected_total.values())
        if rejected_total:
            log.info("rejets : %s", dict(rejected_total))
        return stats["rows_inserted"]
