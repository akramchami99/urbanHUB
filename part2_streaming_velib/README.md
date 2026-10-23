# Partie 2 : CityBikes → MinIO

Collecte toutes les 3 minutes : réponse JSON gzip dans Bronze, stations nettoyées dans la table Iceberg `silver.citybikes`. Nessie conserve les références des tables ; leurs données sont dans MinIO. Les vues du dashboard recalculent l'état courant et les agrégats réseau depuis Silver.

```bash
docker compose --profile streaming run --rm streaming-velib --check-s3
docker compose --profile streaming run --rm streaming-velib --once
docker compose --profile streaming up -d --build
docker compose --profile streaming run --rm streaming-velib --analyze --days 2
```

L'analyse Q1–Q5 lit les snapshots Silver et publie cinq tables Iceberg Gold. Les exports CSV/graphiques sont conservés dans `gold/citybikes/run=…/`. Les snapshots mesurent un stock ; les archives de la partie 4 permettent d'estimer les retraits et retours.

[Architecture](../docs/ARCHITECTURE_ICEBERG.md)

## Historique antérieur au démarrage du polling

Le polling conserve les observations à partir du démarrage. Les archives mensuelles publiques CityBikes permettent de remonter avant cette date :

```bash
docker compose --profile analysis run --rm cross-analysis archive --months 202609
docker compose --profile analysis run --rm lake-gold python -m urbanhub_common gold --start 2026-09-01 --end 2026-10-01
```

Dans Mobilité, l'onglet **Historique · archives** montre les périodes disponibles, les stocks horaires et les retraits/retours estimés, avec un choix de dates. Les heures de panne restent inconnues. Plusieurs mois peuvent être passés à `--months` ; seuls les mois publiés par la source sont disponibles.
