# UrbanHub : Apache Iceberg sur MinIO

## Composants

- **MinIO** : sources Bronze ; Parquet, manifestes Avro et métadonnées JSON des tables Iceberg.
- **Apache Iceberg v2 / PyIceberg 0.10.0** : schémas à identifiants de champs, partitionnement caché par jour, commits atomiques par table, upserts, évolution additive du schéma et lectures de versions conservées.
- **Nessie 0.106.0** : API standard Iceberg REST ; état persistant dans RocksDB sur le volume `nessie-data`. Aucun serveur SQL n'est utilisé.
- **DuckDB en mémoire** : requêtes analytiques sur les Arrow DataFrames lus depuis les snapshots Iceberg.

## Tables

Les namespaces `silver`, `gold` et `metadata` remplacent les scans de préfixes Parquet. Exemples : `silver.citybikes`, `silver.citybikes_archive_city_hourly`, `silver.meteo_city_hourly`, `silver.openaq_measurements`, `gold.analytics_city_hourly`, `metadata.ingestion_runs`.

Les clés métier des datasets servent aux upserts. Une nouvelle collecte ne double pas une observation déjà présente. Les versions précédentes restent référencées dans l'historique du catalogue et par leurs métadonnées Iceberg conservées. Les écritures concurrentes utilisent les vérifications de commit du catalogue et sont retentées en cas de conflit.

Le découpage en jours s'appuie sur la date des observations en UTC, pas sur le nom des fichiers. Les filtres temporels sont transmis au scan Iceberg ; les lecteurs ne listent pas tous les Parquet sous un préfixe et ignorent les fichiers non référencés par le snapshot.

## Migration

Les anciens fichiers sont préservés. Le job initial les lit, conserve la dernière observation de chaque clé, crée les tables, puis compare les comptes de lignes. Après validation, les services repartent en mode Iceberg. Bronze garde les sources originales. Les exports CSV/PNG de présentation restent des artefacts, tandis que leurs résultats structurés sont aussi publiés en tables Iceberg.

Une relance de la migration préserve toute table Iceberg qui possède déjà un snapshot, pour éviter de remplacer des collectes plus récentes par les anciens fichiers.

## Vérification

La page **Data Lake Iceberg** montre le format v2, le schéma, les partitions, le snapshot courant, les nombres de lignes/fichiers et l'URI du metadata JSON. Elle peut relire une ancienne version à partir de son URI de métadonnées committée.

Nessie expose le snapshot de la révision consultée et non l'ensemble des snapshots de toutes ses révisions dans une seule réponse. Pour une lecture historique, utiliser une révision du catalogue ou les métadonnées committées de cette version. Ne pas présenter un simple listing de fichiers comme un historique de commits.

Le projet n'exécute aucune expiration de snapshots ni suppression de fichiers. Les politiques de rétention/compaction restent à configurer pour une exploitation longue durée. La configuration locale est limitée à localhost ; elle est destinée au module de cours.

Sources : [API Apache PyIceberg](https://py.iceberg.apache.org/api/), [configuration Nessie Iceberg REST](https://projectnessie.org/guides/iceberg-rest/), [REST Catalog Apache Iceberg](https://iceberg.apache.org/docs/latest/rest-catalog/).
