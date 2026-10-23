# Partie 3 : IoT pollution OpenAQ

Découverte des capteurs, backfill et polling toutes les 10 minutes. Les réponses brutes sont dans `bronze/openaq/`, les capteurs dans la table Iceberg `silver.openaq_sensors`, les mesures harmonisées dans `silver.openaq_measurements`. Les points de reprise sont calculés depuis ces snapshots, les journaux sont dans `metadata.ingestion_runs`.

```bash
docker compose --profile iot run --rm iot-pollution discover
docker compose --profile iot run --rm iot-pollution backfill --days 30
docker compose --profile iot up -d --build
```

Les vues analytiques du dashboard et du notebook sont des requêtes DuckDB en mémoire sur les scans Iceberg. Les upserts utilisent la clé `(sensor_id, measured_at)`. Les compteurs d'ingestion indiquent les lignes soumises, pas uniquement les nouvelles clés.

## Nettoyage (fichier `src/pollution_iot/transform.py`)

- On ne garde que les 5 polluants de l'énoncé et uniquement les capteurs fixes situés en France.
- Les valeurs manquantes, non numériques, négatives ou sentinelles (-999) sont rejetées.
- Les unités sont harmonisées en **µg/m³** : ppm et ppb sont convertis via la masse molaire (25 °C), mg/m³ est multiplié par 1000.
  La valeur et l'unité d'origine sont conservées (`value_raw`, `unit_raw`).
- Les valeurs physiquement impossibles sont rejetées (ex. PM2.5 > 1000 µg/m³).
- Timestamp : fin de la période de mesure, converti en UTC.
- Les coordonnées manquantes sont remplacées par celles du site de mesure.
- Les doublons sont supprimés.
- Le nombre de rejets est tracé dans `ops.ingestion_runs.rows_rejected`.

Tests : `docker compose run --rm --entrypoint pytest iot-pollution tests -q`

## Questions métier : où sont les réponses ?

| Question | Vue SQL | Dashboard / notebook |
|---|---|---|
| Q1 zones les plus polluées | `v_zone_levels`, `v_zone_index`, `v_city_levels` | onglet Q1, carte + classement |
| Q2 variation journalière | `v_hourly_profile` | onglet Q2 + test de Kruskal-Wallis |
| Q3 polluants dominants | `v_dominant_pollutant` (sous-indice = valeur / seuil OMS) | onglet Q3 |
| Q4 épisodes anormaux | `v_anomalies` (z-score sur 7 j glissants), `v_pollution_episodes` (seuils réglementaires et OMS) | onglet Q4 |
