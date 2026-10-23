# Partie 4 : archives et analyse croisée

```bash
docker compose --profile analysis run --rm cross-analysis archive --months 202609
docker compose --profile analysis run --rm lake-gold
```

Les archives mensuelles CityBikes sont conservées dans Bronze, les stocks et variations horaires dans la table Iceberg `silver.citybikes_archive_city_hourly`. Le calcul Gold lit les snapshots Silver et publie `gold.analytics_city_hourly`. Le dashboard interroge les résultats des scans Iceberg avec DuckDB en mémoire.

Les retraits/retours proviennent des variations d'état, avec exclusion des grands mouvements assimilés au rééquilibrage. Les heures sans changements dans tout le réseau restent inconnues, pas des zéros.

[Architecture](../docs/ARCHITECTURE_ICEBERG.md)
