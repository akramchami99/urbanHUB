"""Point d'entrée : python -m pollution_iot <commande>

  discover   découvrir les capteurs des villes de ref.cities
  backfill   charger l'historique (--days, défaut BACKFILL_DAYS)
  poll       un seul tick du flux IoT
  run        mode service (Docker) : découverte + rattrapage au démarrage, puis
             poll toutes les POLL_INTERVAL_MINUTES, rattrapage toutes les 6 h,
             redécouverte des capteurs chaque nuit
"""

import argparse
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from urbanhub_common import get_logger

from . import config, pipeline

log = get_logger("pollution_iot")


def _safe(fn, *args):
    """Une erreur ponctuelle (réseau, API) ne doit pas arrêter le service."""
    def wrapper():
        try:
            fn(*args)
        except Exception:
            log.exception("échec de %s", fn.__name__)
    wrapper.__name__ = fn.__name__
    return wrapper


def run_service() -> None:
    if pipeline.discover() == 0:
        raise SystemExit("aucun capteur actif trouvé : vérifier la clé OpenAQ et ref.cities")
    _safe(pipeline.backfill)()

    sched = BlockingScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})
    sched.add_job(_safe(pipeline.poll), "interval", minutes=config.POLL_INTERVAL_MINUTES,
                  next_run_time=datetime.now(timezone.utc), id="poll")
    # le endpoint /latest ne donne que la dernière valeur : si un tick est manqué, ce job comble le trou
    sched.add_job(_safe(pipeline.backfill), "interval", hours=6, id="catch_up")
    sched.add_job(_safe(pipeline.discover), "cron", hour=3, minute=0, id="discover")
    log.info("service démarré : poll toutes les %d min", config.POLL_INTERVAL_MINUTES)
    sched.start()


def main() -> None:
    parser = argparse.ArgumentParser(prog="pollution_iot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover")
    bf = sub.add_parser("backfill")
    bf.add_argument("--days", type=int, default=config.BACKFILL_DAYS)
    bf.add_argument("--full", action="store_true", help="recharger toute la fenêtre (étendre l'historique)")
    sub.add_parser("poll")
    sub.add_parser("run")
    args = parser.parse_args()

    if args.cmd == "discover":
        pipeline.discover()
    elif args.cmd == "backfill":
        pipeline.backfill(args.days, args.full)
    elif args.cmd == "poll":
        pipeline.poll()
    else:
        run_service()


if __name__ == "__main__":
    main()
