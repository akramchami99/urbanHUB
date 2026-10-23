-- =====================================================================
-- Partie 4 - Analyse croisée (À COMPLÉTER)
-- Principe : chaque flux expose une vue "ville x heure" dans le schéma analytics,
-- puis on les joint sur (city, hour_utc).
-- =====================================================================

-- Pollution : format large, 1 ligne = 1 ville x 1 heure (prêt pour la jointure)

CREATE VIEW analytics.v_pollution_city_hourly AS
SELECT city,
       hour_utc,
       avg(value) FILTER (WHERE pollutant = 'pm25') AS pm25,
       avg(value) FILTER (WHERE pollutant = 'pm10') AS pm10,
       avg(value) FILTER (WHERE pollutant = 'no2')  AS no2,
       avg(value) FILTER (WHERE pollutant = 'o3')   AS o3,
       avg(value) FILTER (WHERE pollutant = 'co')   AS co
FROM pollution.v_city_hourly
GROUP BY city, hour_utc;

-- Météo : 1 ligne = 1 ville x 1 heure (NOAA jusqu'en août 2025, puis Open-Meteo)

CREATE VIEW analytics.v_meteo_city_hourly AS
SELECT city, hour_utc, temperature, wind_speed, wind_direction, pressure, precipitation, visibility, source
FROM meteo.city_hourly;
-- Mobilité : 1 ligne = 1 ville x 1 heure
--   archives CityBikes (usage réel : departures / returns) + instantanés temps réel (stock seulement)

CREATE VIEW analytics.v_mobility_city_hourly AS
WITH snap AS (   -- une collecte = même collected_at pour tous les réseaux d'une ville
    SELECT city, collected_at,
           sum(n_stations) AS n_stations, sum(bikes_available) AS bikes_available,
           sum(free_slots) AS free_slots, sum(n_low_supply) AS n_low_supply,
           sum(n_empty) AS n_empty, sum(n_full) AS n_full
    FROM mobility.network_snapshots
    GROUP BY city, collected_at
),
live AS (
    SELECT city,
           date_trunc('hour', collected_at)                                         AS hour_utc,
           avg(n_stations)                                                          AS n_stations,
           avg(bikes_available)                                                     AS bikes_available,
           avg(free_slots)                                                          AS free_slots,
           -- part des places occupées par un vélo garé : plus elle baisse, plus les vélos sont en circulation
           avg(bikes_available::float / nullif(bikes_available + free_slots, 0))    AS availability_ratio,
           avg(n_low_supply::float / nullif(n_stations, 0))                         AS share_low_supply,
           avg(n_empty::float / nullif(n_stations, 0))                              AS share_empty,
           avg(n_full::float / nullif(n_stations, 0))                               AS share_full
    FROM snap
    GROUP BY city, date_trunc('hour', collected_at)
)
SELECT city, hour_utc, n_stations, bikes_available, free_slots, availability_ratio,
       share_low_supply, share_empty, share_full, departures, returns, 'archive' AS source
FROM mobility.city_hourly_history
UNION ALL
SELECT l.city, l.hour_utc, l.n_stations, l.bikes_available, l.free_slots, l.availability_ratio,
       l.share_low_supply, l.share_empty, l.share_full, NULL::integer, NULL::integer, 'live'
FROM live l
WHERE NOT EXISTS (SELECT 1 FROM mobility.city_hourly_history h
                  WHERE h.city = l.city AND h.hour_utc = l.hour_utc);

-- ---------------------------------------------------------------------
-- Partie 4 : table de croisement ville x heure (météo = base, pollution et vélos en LEFT JOIN)
-- ---------------------------------------------------------------------

CREATE VIEW analytics.v_city_hourly_all AS
SELECT w.city,
       w.hour_utc,
       (w.hour_utc AT TIME ZONE 'Europe/Paris')::date                        AS date_local,
       extract(hour FROM w.hour_utc AT TIME ZONE 'Europe/Paris')::int        AS hour_local,
       extract(isodow FROM w.hour_utc AT TIME ZONE 'Europe/Paris') IN (6, 7) AS is_weekend,
       w.temperature, w.wind_speed, w.wind_direction, w.pressure, w.precipitation, w.visibility,
       w.source AS meteo_source,
       p.pm25, p.pm10, p.no2, p.o3, p.co,
       m.n_stations, m.availability_ratio, m.share_empty, m.share_full,
       m.departures, m.returns,
       m.departures::float / nullif(m.n_stations, 0)                          AS departures_per_station,
       m.source AS mobility_source
FROM analytics.v_meteo_city_hourly w
LEFT JOIN analytics.v_pollution_city_hourly p USING (city, hour_utc)
LEFT JOIN analytics.v_mobility_city_hourly m USING (city, hour_utc);
