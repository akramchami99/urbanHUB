# Partie 1 : météo batch et horaire

NOAA est téléchargé en parallèle et archivé dans MinIO Bronze. Le nettoyage publie les tables Iceberg Silver horaires ; les stations et la météo par ville sont aussi dans Silver. Les agrégats journaliers sont dans `gold.meteo_daily`. Open-Meteo complète les périodes récentes ; NOAA garde la priorité à la lecture.

```bash
docker compose --profile batch run --rm batch-meteo
docker compose --profile batch run --rm batch-meteo openmeteo
docker compose --profile meteo up -d --build
```

Les dernières heures sont relues depuis MinIO pour la reprise. Le notebook utilise les mêmes snapshots Iceberg via les vues DuckDB en mémoire ; son écriture directe est désactivée par défaut, le pipeline Docker assure l'ingestion complète Bronze/Silver/Gold.

[Architecture](../docs/ARCHITECTURE_ICEBERG.md) · [Démarrage](../README.md)
