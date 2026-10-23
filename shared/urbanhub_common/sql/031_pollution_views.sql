-- =====================================================================
-- Partie 3 - Flux IoT : vues analytiques (une vue par question métier)
-- Recréées à chaque migration -> on les supprime d'abord (ordre de dépendance géré par CASCADE)
-- =====================================================================






-- Vue "contrat" : exactement les variables demandées par l'énoncé
CREATE VIEW pollution.v_measurements AS
SELECT m.sensor_id,
       m.pollutant,
       m.measured_at AS "timestamp",
       m.value,
       m.unit,
       m.latitude,
       m.longitude,
       m.city,
       s.location_id,
       s.location_name
FROM pollution.measurements m
JOIN pollution.sensors s USING (sensor_id);

-- Moyenne horaire par ville et polluant (base des autres vues et de la partie 4)
CREATE VIEW pollution.v_city_hourly AS
SELECT city,
       pollutant,
       date_trunc('hour', measured_at)  AS hour_utc,
       avg(value)                       AS value,
       count(DISTINCT sensor_id)        AS n_sensors
FROM pollution.measurements
GROUP BY city, pollutant, date_trunc('hour', measured_at);

-- Moyenne journalière (jour civil heure de Paris)
CREATE VIEW pollution.v_city_daily AS
SELECT city,
       pollutant,
       (hour_utc AT TIME ZONE 'Europe/Paris')::date AS day,
       avg(value)   AS mean_value,
       max(value)   AS max_hourly,
       count(*)     AS n_hours
FROM pollution.v_city_hourly
GROUP BY city, pollutant, (hour_utc AT TIME ZONE 'Europe/Paris')::date;

-- Fraîcheur des données (monitoring du flux IoT)
CREATE VIEW pollution.v_freshness AS
SELECT s.city,
       s.pollutant,
       count(DISTINCT s.sensor_id)  AS n_sensors,
       max(m.measured_at)           AS last_measure,
       count(m.sensor_id)                   AS n_measures
FROM pollution.sensors s
LEFT JOIN pollution.measurements m USING (sensor_id)
WHERE s.active
GROUP BY s.city, s.pollutant;

-- ---------------------------------------------------------------------
-- Q1. Quelles zones présentent les niveaux de pollution les plus élevés ?
-- who_ratio = moyenne / ligne directrice OMS 24h  (>1 : au-dessus de la recommandation)
-- ---------------------------------------------------------------------
CREATE VIEW pollution.v_zone_levels AS
SELECT s.city,
       s.location_id,
       s.location_name,
       s.latitude,
       s.longitude,
       m.pollutant,
       count(*)                                               AS n_hours,
       avg(m.value)                                           AS mean_value,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY m.value)  AS p95_value,
       max(m.value)                                           AS max_value,
       avg(m.value) / t.who_24h                               AS who_ratio
FROM pollution.measurements m
JOIN pollution.sensors s USING (sensor_id)
JOIN pollution.thresholds t ON t.pollutant = m.pollutant
GROUP BY s.city, s.location_id, s.location_name, s.latitude, s.longitude, m.pollutant, t.who_24h;

-- Indice synthétique par zone = pire ratio OMS parmi les polluants mesurés
CREATE VIEW pollution.v_zone_index AS
SELECT DISTINCT ON (location_id)
       city, location_id, location_name, latitude, longitude,
       pollutant AS worst_pollutant,
       who_ratio AS zone_index
FROM pollution.v_zone_levels
WHERE n_hours >= 24
ORDER BY location_id, who_ratio DESC;

CREATE VIEW pollution.v_city_levels AS
SELECT d.city,
       d.pollutant,
       avg(d.mean_value)                                              AS mean_value,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY d.mean_value)     AS p95_daily,
       max(d.max_hourly)                                              AS max_hourly,
       avg(d.mean_value) / t.who_24h                                  AS who_ratio,
       avg((d.mean_value > t.who_24h)::int)::float                    AS share_days_above_who,
       count(*)                                                       AS n_days
FROM pollution.v_city_daily d
JOIN pollution.thresholds t ON t.pollutant = d.pollutant
GROUP BY d.city, d.pollutant, t.who_24h;

-- ---------------------------------------------------------------------
-- Q2. Existe-t-il une variation journalière de la pollution ?
-- Profil moyen par heure locale, semaine vs week-end
-- ---------------------------------------------------------------------
CREATE VIEW pollution.v_hourly_profile AS
SELECT city,
       pollutant,
       extract(hour FROM hour_utc AT TIME ZONE 'Europe/Paris')::int AS hour_local,
       CASE WHEN extract(isodow FROM hour_utc AT TIME ZONE 'Europe/Paris') IN (6, 7)
            THEN 'week-end' ELSE 'semaine' END                       AS day_type,
       avg(value)          AS mean_value,
       stddev_samp(value)  AS std_value,
       count(*)            AS n
FROM pollution.v_city_hourly
GROUP BY 1, 2, 3, 4;

-- ---------------------------------------------------------------------
-- Q3. Quels polluants sont les plus dominants dans la ville étudiée ?
-- Les concentrations brutes ne sont pas comparables entre polluants : on compare
-- des sous-indices (valeur / seuil OMS). Le polluant "dominant" d'une heure est
-- celui qui a le plus grand sous-indice ; on compte la part d'heures dominées.
-- ---------------------------------------------------------------------
CREATE VIEW pollution.v_dominant_pollutant AS
WITH idx AS (
    SELECT h.city, h.hour_utc, h.pollutant, h.value, h.value / t.who_24h AS sub_index
    FROM pollution.v_city_hourly h
    JOIN pollution.thresholds t ON t.pollutant = h.pollutant
),
dom AS (
    SELECT DISTINCT ON (city, hour_utc) city, hour_utc, pollutant
    FROM idx
    ORDER BY city, hour_utc, sub_index DESC
),
share AS (
    SELECT city, pollutant,
           count(*) AS hours_dominant,
           count(*)::float / sum(count(*)) OVER (PARTITION BY city) AS share_dominant
    FROM dom
    GROUP BY city, pollutant
),
lvl AS (
    SELECT city, pollutant, avg(value) AS mean_value, avg(sub_index) AS mean_sub_index
    FROM idx
    GROUP BY city, pollutant
)
SELECT l.city,
       l.pollutant,
       l.mean_value,
       l.mean_sub_index,
       coalesce(s.hours_dominant, 0)  AS hours_dominant,
       coalesce(s.share_dominant, 0)  AS share_dominant,
       rank() OVER (PARTITION BY l.city ORDER BY coalesce(s.share_dominant, 0) DESC) AS dominance_rank
FROM lvl l
LEFT JOIN share s USING (city, pollutant);

-- ---------------------------------------------------------------------
-- Q4. Peut-on identifier des épisodes de pollution anormale ?
-- Deux approches complémentaires :
--   a) statistique : z-score de chaque mesure vs les 7 jours précédents du même capteur
--   b) réglementaire : dépassement des seuils d'information / d'alerte / OMS
-- ---------------------------------------------------------------------
CREATE VIEW pollution.v_anomalies AS
WITH w AS (
    SELECT m.sensor_id, m.city, m.pollutant, m.measured_at, m.value,
           avg(m.value)         OVER win AS baseline_mean,
           stddev_samp(m.value) OVER win AS baseline_std,
           count(*)             OVER win AS baseline_n
    FROM pollution.measurements m
    WINDOW win AS (PARTITION BY m.sensor_id ORDER BY m.measured_at
                   RANGE BETWEEN INTERVAL '7 days' PRECEDING AND INTERVAL '1 second' PRECEDING)
)
SELECT w.*,
       (w.value - w.baseline_mean) / nullif(w.baseline_std, 0)                       AS z_score,
       coalesce(w.baseline_n >= 48
                AND (w.value - w.baseline_mean) / nullif(w.baseline_std, 0) > 3, false) AS is_statistical_anomaly,
       coalesce(t.basis = 'hourly' AND w.value > t.info_threshold, false)             AS exceeds_info_hourly,
       coalesce(t.basis = 'hourly' AND w.value > t.alert_threshold, false)            AS exceeds_alert_hourly
FROM w
JOIN pollution.thresholds t ON t.pollutant = w.pollutant;

-- Épisodes au niveau ville x jour x polluant (seulement les jours "anormaux")
CREATE VIEW pollution.v_pollution_episodes AS
WITH a AS (
    SELECT city, pollutant,
           (measured_at AT TIME ZONE 'Europe/Paris')::date       AS day,
           count(*) FILTER (WHERE is_statistical_anomaly)        AS n_anomalous_hours,
           max(z_score)                                          AS max_z_score,
           bool_or(exceeds_info_hourly)                          AS info_hourly,
           bool_or(exceeds_alert_hourly)                         AS alert_hourly
    FROM pollution.v_anomalies
    GROUP BY 1, 2, 3
),
e AS (
    SELECT d.city, d.pollutant, d.day,
           d.mean_value                AS daily_mean,
           d.max_hourly,
           d.mean_value / t.who_24h    AS who_ratio,
           coalesce(a.n_anomalous_hours, 0) AS n_anomalous_hours,
           a.max_z_score,
           CASE
               WHEN a.alert_hourly OR (t.basis = 'daily' AND d.mean_value > t.alert_threshold) THEN 'alerte'
               WHEN a.info_hourly  OR (t.basis = 'daily' AND d.mean_value > t.info_threshold)  THEN 'information'
               WHEN d.mean_value > t.who_24h                                                   THEN 'depassement_OMS'
               WHEN a.n_anomalous_hours >= 3                                                   THEN 'anomalie_statistique'
           END AS episode_level
    FROM pollution.v_city_daily d
    JOIN pollution.thresholds t ON t.pollutant = d.pollutant
    LEFT JOIN a ON a.city = d.city AND a.pollutant = d.pollutant AND a.day = d.day
)
SELECT * FROM e WHERE episode_level IS NOT NULL;
