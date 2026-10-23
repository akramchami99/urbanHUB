"""Point d'entrée : python -m meteo_batch <commande>

  noaa       batch NOAA : stations FR -> téléchargement parallèle -> nettoyage -> MinIO Silver/Gold
  openmeteo  complète avec Open-Meteo de la fin de NOAA (ou de la dernière heure en base) à aujourd'hui
  run        noaa puis openmeteo (initialisation complète)
  live       service : openmeteo puis mise à jour toutes les METEO_LIVE_INTERVAL_MINUTES (heures récentes)
"""

import argparse
import time
from datetime import timedelta

import pandas as pd
from urbanhub_common import RawWriter, get_logger, write_parquet, s3_client, lake_bucket
from urbanhub_common.ops import finish_run, start_run

from . import aggregate, load, noaa, openmeteo
from .config import ANNEES, DOSSIER_RAW, LIVE_INTERVAL_MINUTES, OPENMETEO_START

log = get_logger("meteo_batch")


def run_noaa() -> None:
    run_id = start_run("meteo", "noaa-batch")
    try:
        # Étapes 1-2 : stations françaises + téléchargement parallèle (bronze local)
        stations = noaa.charger_stations_france()
        log.info("%d stations trouvées en France", len(stations))
        taches = noaa.construire_liste_taches(stations)
        bilan = noaa.telecharger_tout(taches)
        fichiers = sorted(f for a in ANNEES for f in (DOSSIER_RAW / str(a)).glob("*.csv"))
        if not fichiers:
            raise RuntimeError("aucun fichier NOAA valide téléchargé")

        # Local NOAA files are a download cache; durable Bronze is in MinIO.
        for fichier in fichiers:
            key = f"bronze/noaa/global-hourly/year={fichier.parent.name}/{fichier.name}"
            s3_client().upload_file(str(fichier), lake_bucket(), key)

        # Étape 3 : nettoyage -> silver
        horaire = noaa.nettoyer_tout(fichiers)
        log.info("%d lignes horaires | %d stations | %s -> %s", len(horaire), horaire["station_id"].nunique(),
                 horaire["timestamp"].min(), horaire["timestamp"].max())
        silver = load.silver_parquet(horaire)

        # Étape 4 : fichiers de référence et agrégats MinIO
        liste = (stations.rename(columns={"STATION NAME": "name", "LAT": "latitude", "LON": "longitude"})
                 [["station_id", "name", "latitude", "longitude"]])
        liste = liste[liste["station_id"].isin(horaire["station_id"].astype(str).unique())].copy()
        liste[["latitude", "longitude"]] = liste[["latitude", "longitude"]].apply(pd.to_numeric, errors="coerce")
        liste = aggregate.rattacher_villes(liste.dropna(subset=["latitude", "longitude"]), load.villes())
        liste["source"] = "noaa"
        load.stations(liste)

        daily = aggregate.journalier(horaire)
        daily = daily[daily["station_id"].astype(str).isin(liste["station_id"])]
        write_parquet(daily, "gold", "meteo/daily", "date")
        n_daily = len(daily)

        ville = liste.set_index("station_id")["city"].dropna()
        city_hourly = aggregate.horaire_par_ville(horaire.assign(station_id=horaire["station_id"].astype(str)),
                                                  ville, "noaa")
        write_parquet(city_hourly, "silver", "meteo/city_hourly", "hour_utc")
        n_city = len(city_hourly)

        finish_run(run_id, "success", api_calls=bilan["ok"] + bilan["deja"], rows_received=len(horaire),
                   rows_inserted=n_daily + n_city, raw_path=silver[0] if silver else None)
    except Exception as exc:
        finish_run(run_id, "error", error=repr(exc)[:2000])
        raise


def run_openmeteo(mode: str = "backfill") -> None:
    raw = RawWriter("open-meteo", "weather", mode)
    run_id = start_run("meteo", f"open-meteo-{mode}")
    try:
        villes = load.villes()
        derniere = load.dernieres_heures()
        frames = []
        for city, lat, lon in villes:
            last = derniere.get(city)
            if mode == "live":   # 2 derniers jours, réécrits (le modèle se corrige) ; NOAA protégé plus bas
                df = openmeteo.fetch_live(city, lat, lon, raw)
            else:
                start = (last + timedelta(hours=1)).date() if last is not None else OPENMETEO_START
                if start > openmeteo.today_utc():
                    continue
                df = openmeteo.fetch_history(city, lat, lon, start, openmeteo.today_utc(), raw)
                if last is not None and not df.empty:
                    df = df[df["timestamp"] > last]
            log.info("%-11s : %d heures Open-Meteo", city, len(df))
            frames.append(df)

        horaire = pd.concat([f for f in frames if not f.empty], ignore_index=True) if any(
            not f.empty for f in frames) else pd.DataFrame()
        n = 0
        if not horaire.empty:
            write_parquet(horaire, "silver", "open-meteo/weather", "timestamp")
            points = pd.DataFrame([{"station_id": openmeteo.station_id(c), "name": f"Open-Meteo {c}",
                                    "latitude": la, "longitude": lo, "city": c, "distance_km": 0.0,
                                    "source": "open-meteo"} for c, la, lo in villes])
            load.stations(points)
            ville = points.set_index("station_id")["city"]
            city_hourly = aggregate.horaire_par_ville(horaire, ville, "open-meteo")
            write_parquet(city_hourly, "silver", "meteo/city_hourly", "hour_utc")
            # Source priority is resolved when querying Silver files.
            n = len(city_hourly)
            load.recalculer_journalier_openmeteo()
        finish_run(run_id, "success", api_calls=len(villes), rows_received=len(horaire),
                   rows_inserted=n, raw_path=raw.flush())
    except Exception as exc:
        finish_run(run_id, "error", raw_path=raw.flush(), error=repr(exc)[:2000])
        raise


def run_live() -> None:
    run_openmeteo("backfill")   # comble un éventuel trou depuis le dernier arrêt
    while True:
        time.sleep(LIVE_INTERVAL_MINUTES * 60)
        try:
            run_openmeteo("live")
        except Exception:
            log.exception("mise à jour Open-Meteo en échec, nouvel essai au prochain cycle")


def main() -> None:
    parser = argparse.ArgumentParser(prog="meteo_batch", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cmd", choices=["noaa", "openmeteo", "run", "live"])
    args = parser.parse_args()
    if args.cmd in ("noaa", "run"):
        run_noaa()
    if args.cmd in ("openmeteo", "run"):
        run_openmeteo("backfill")
    if args.cmd == "live":
        run_live()


if __name__ == "__main__":
    main()
