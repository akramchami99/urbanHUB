from urbanhub_common import lake_bucket
import io

import pandas as pd
import plotly.express as px
import streamlit as st
from urbanhub_common import s3_client

from common import explain, query

st.set_page_config(page_title="Mobilité · UrbanHub", page_icon="🚲", layout="wide")
st.title("🚲 Partie 2 : mobilité urbaine (flux streaming CityBikes)")
st.caption("Indicateur de stock : la part de vélos disponibles en station, et non le nombre de trajets. "
           "Quand elle baisse, davantage de vélos sont en circulation.")

CITY_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]


@st.cache_data(ttl=600, show_spinner="Lecture des analyses gold (S3)…")
def latest_gold() -> tuple[str | None, dict[str, pd.DataFrame]]:
    s3, bucket = s3_client(), lake_bucket()
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="gold/citybikes/")
    keys = [o["Key"] for p in pages for o in p.get("Contents", []) if o["Key"].endswith(".csv")]
    if not keys:
        return None, {}
    run = max(k.split("/")[2] for k in keys)
    return run, {
        k.rsplit("/", 1)[1][:-4]: pd.read_csv(io.BytesIO(s3.get_object(Bucket=bucket, Key=k)["Body"].read()))
        for k in keys if f"/{run}/" in k
    }


try:
    stations = query("""SELECT city, network_id, station_id, station_name, latitude, longitude,
                               bikes_available, free_slots, collected_at,
                               bikes_available::float / nullif(bikes_available + free_slots, 0) AS availability
                        FROM mobility.stations""")
except Exception as exc:
    st.error(f"Connexion à MinIO impossible ou données du lake indisponibles : {exc}")
    st.stop()
if stations.empty:
    st.warning("Aucune donnée : lancer `docker compose --profile streaming up -d`.")
    st.stop()

cities = sorted(stations["city"].unique())
c1, c2 = st.columns([3, 1])
sel_cities = c1.multiselect("Villes (max 4 pour les courbes)", cities,
                            default=[c for c in ["Paris", "Lyon", "Bordeaux", "Toulouse"] if c in cities][:4] or cities[:4],
                            max_selections=4)
map_city = c2.selectbox("Ville de la carte", cities, index=cities.index("Paris") if "Paris" in cities else 0)

k1, k2, k3, k4 = st.columns(4)
k1.metric("Stations suivies", f"{len(stations):,}".replace(",", " "))
k2.metric("Réseaux", stations["network_id"].nunique())
k3.metric("Vélos disponibles (dernier état)", f"{int(stations['bikes_available'].sum()):,}".replace(",", " "))
k4.metric("Dernière collecte (UTC)", stations["collected_at"].max().strftime("%d/%m %H:%M"))

tab_map, tab_history, tab_hour, tab_gold, tab_flow = st.tabs(
    ["État actuel · Q2/Q4", "Historique · archives", "Q3 · Pics journaliers", "Analyses gold · Q1-Q5", "Qualité du flux"])

with tab_map:
    s = stations[(stations["city"] == map_city) & stations["availability"].notna()]
    fig = px.scatter_map(
        s, lat="latitude", lon="longitude", color="availability", hover_name="station_name",
        hover_data={"bikes_available": True, "free_slots": True, "availability": ":.0%",
                    "latitude": False, "longitude": False},
        color_continuous_scale="Blues", range_color=(0, 1), zoom=11.5, map_style="open-street-map", height=560,
        labels={"availability": "Part de vélos", "bikes_available": "Vélos", "free_slots": "Places libres"},
        title=f"{map_city} : disponibilité des vélos par station (dernière collecte)",
    )
    fig.update_coloraxes(colorbar_tickformat=".0%")
    st.plotly_chart(fig, use_container_width=True)
    explain(
        "Chaque point est une station, dans son dernier état collecté (toutes les 3 min). La couleur donne la "
        "**part de vélos** parmi les places de la station : bleu foncé pour une station pleine de vélos, clair "
        "pour une station vide. Survoler un point affiche le nombre de vélos et de places libres.",
        "**Q2 et Q4** : des zones claires groupées révèlent une **offre insuffisante**, le plus souvent en "
        "hauteur, en périphérie, ou dans les quartiers d'où l'on part le matin. Des zones foncées montrent "
        "les vélos qui s'accumulent (centres, gares, bas des pentes). Ce **déséquilibre géographique** suit "
        "l'heure : les quartiers résidentiels se vident le matin, les quartiers d'emploi le soir. Sur septembre "
        "2026 (archives), la part moyenne de stations à 10 % de vélos ou moins va de 9 % à Strasbourg à 30 % à "
        "Toulouse et Lyon.")
    crit = s[s["availability"] <= 0.10].sort_values("bikes_available")
    st.markdown(f"**{len(crit)} stations à ≤ 10 % de vélos en ce moment** (candidates au rééquilibrage, Q5)")
    st.dataframe(crit[["station_name", "bikes_available", "free_slots", "availability"]],
                 hide_index=True, use_container_width=True)
    explain("Liste des stations de la ville choisie qui ont, à l'instant présent, 10 % de vélos ou moins.",
            "**Q5** : ce sont les candidates prioritaires au **rééquilibrage** (camions qui déplacent des vélos). "
            "Une station est vraiment critique si elle reste sous ce seuil plusieurs collectes de suite : voir "
            "l'onglet « Analyses gold », table Q5.")


with tab_history:
    history = query("""SELECT city, hour_utc, n_stations, bikes_available, free_slots,
                               departures, returns, n_changes
                        FROM mobility.city_hourly_history ORDER BY city, hour_utc""")
    if history.empty:
        st.info("Aucune archive chargée. Importer un mois avec : "
                "docker compose --profile analysis run --rm cross-analysis archive --months AAAAMM")
    else:
        history["hour_utc"] = pd.to_datetime(history["hour_utc"], utc=True)
        st.caption("Archives mensuelles CityBikes conservées dans MinIO Bronze, puis transformées en "
                   "Parquet Silver horaires. Les retraits et retours sont estimés à partir des variations "
                   "de stock ; les grands mouvements de rééquilibrage sont exclus. "
                   "Les heures de panne restent inconnues et apparaissent comme des trous dans les courbes.")
        coverage = history.groupby("city").agg(
            debut=("hour_utc", "min"), fin=("hour_utc", "max"), heures=("hour_utc", "size"),
            heures_sans_changement=("n_changes", lambda col: int((col == 0).sum()))).reset_index()
        st.dataframe(coverage, hide_index=True, use_container_width=True)
        first_day = history.hour_utc.min().date()
        last_day = history.hour_utc.max().date()
        period = st.date_input("Période historique (UTC)", value=(first_day, last_day),
                               min_value=first_day, max_value=last_day, key="citybikes_history_period")
        if len(period) == 2:
            start = pd.Timestamp(period[0], tz="UTC")
            end = pd.Timestamp(period[1], tz="UTC") + pd.Timedelta(days=1)
            selected = history[history.city.isin(sel_cities) & history.hour_utc.ge(start) & history.hour_utc.lt(end)]
            if selected.empty:
                st.info("Aucune observation pour les villes et la période sélectionnées.")
            else:
                flows = selected.melt(id_vars=["city", "hour_utc"], value_vars=["departures", "returns"],
                                      var_name="mouvement", value_name="velos")
                flows["mouvement"] = flows.mouvement.map({"departures": "retraits estimés", "returns": "retours estimés"})
                st.plotly_chart(px.line(flows, x="hour_utc", y="velos", color="city", line_dash="mouvement",
                    color_discrete_sequence=CITY_COLORS, height=380,
                    labels={"hour_utc": "Heure UTC", "velos": "Vélos / heure", "city": "Ville", "mouvement": "Mouvement"},
                    title="Historique horaire : retraits et retours estimés"), use_container_width=True)
                st.plotly_chart(px.line(selected, x="hour_utc", y="bikes_available", color="city",
                    color_discrete_sequence=CITY_COLORS, height=340,
                    labels={"hour_utc": "Heure UTC", "bikes_available": "Vélos disponibles", "city": "Ville"},
                    title="Historique du stock en station"), use_container_width=True)
                st.dataframe(selected, hide_index=True, use_container_width=True)
        else:
            st.info("Choisir aussi la date de fin de la période.")

with tab_hour:
    h = query("""SELECT city, extract(hour FROM hour_utc AT TIME ZONE 'Europe/Paris')::int AS hour_local,
                        avg(availability_ratio) AS availability_ratio, avg(share_low_supply) AS share_low_supply,
                        count(*) AS n_hours
                 FROM analytics.v_mobility_city_hourly GROUP BY 1, 2""")
    h = h[h["city"].isin(sel_cities)].sort_values("hour_local")
    if h.empty:
        st.info("Pas encore assez de collectes.")
    else:
        for col, title in [("availability_ratio", "Part de vélos disponibles en station"),
                           ("share_low_supply", "Part des stations à ≤ 10 % de vélos")]:
            fig = px.line(h, x="hour_local", y=col, color="city", markers=True, height=360,
                          color_discrete_sequence=CITY_COLORS, category_orders={"city": sel_cities},
                          labels={"hour_local": "Heure locale", col: "", "city": "Ville"}, title=title)
            fig.update_traces(line_width=2)
            fig.update_yaxes(tickformat=".0%")
            fig.update_xaxes(dtick=3)
            st.plotly_chart(fig, use_container_width=True)
        st.caption(f"Nombre moyen d'heures observées par point : {h['n_hours'].mean():.1f}. "
                   "Le profil n'est fiable qu'avec plusieurs jours de collecte.")
        explain("Profil moyen par heure locale de la part de vélos en station (en haut) et de la part de stations "
                "presque vides (en bas). Il s'agit d'un **stock** : il baisse quand beaucoup de vélos sont en "
                "circulation.",
                "Le creux du stock et le pic de stations vides marquent les heures de forte demande. Pour mesurer "
                "directement l'usage, voir le graphique ci-dessous (vélos retirés).")
    use = query("""SELECT city, hour_local, is_weekend, avg(departures_per_station) AS retraits
                   FROM analytics.v_city_hourly_all WHERE mobility_source = 'archive' GROUP BY 1, 2, 3""")
    use = use[use["city"].isin(sel_cities)].sort_values("hour_local")
    if not use.empty:
        use["jour"] = use["is_weekend"].map({True: "week-end", False: "semaine"})
        fig = px.line(use, x="hour_local", y="retraits", color="city", facet_col="jour", markers=True, height=380,
                      color_discrete_sequence=CITY_COLORS, category_orders={"city": sel_cities,
                                                                           "jour": ["semaine", "week-end"]},
                      labels={"hour_local": "Heure locale", "retraits": "Vélos retirés / station / heure",
                              "city": "Ville", "jour": ""},
                      title="Usage réel : vélos retirés par station et par heure (archives CityBikes, septembre 2026)")
        fig.update_traces(line_width=2)
        fig.update_xaxes(dtick=3)
        st.plotly_chart(fig, use_container_width=True)
        explain("Nombre moyen de vélos retirés par station et par heure, en semaine (à gauche) et le week-end "
                "(à droite). Les données viennent des archives CityBikes de septembre 2026 : chaque baisse du nombre "
                "de vélos d'une station compte comme un retrait, hors camions de rééquilibrage.",
                "**Q3, pics d'utilisation** : en semaine, deux pics nets liés aux **trajets domicile-travail**, "
                "à **8 h** (2,8 retraits par station et par heure en moyenne) et à **17-18 h** (3,3), plus un "
                "petit pic à midi. Le week-end, les pics disparaissent au profit d'un **plateau de loisirs** de "
                "11 h à 19 h, avec plus d'usage la nuit (sorties).")

with tab_gold:
    try:
        run, gold = latest_gold()
    except Exception as exc:
        run, gold = None, {}
        st.error(f"Lecture S3 impossible : {exc}")
    if not gold:
        st.info("Aucune analyse gold : `docker compose --profile streaming run --rm streaming-velib --analyze --days 2`")
    else:
        st.caption(f"Dernier calcul : `{run}` (bucket S3, gold/citybikes/)")
        titles = {
            "q1_top_occupied_stations": "Q1 · Stations au plus fort taux d'occupation",
            "q2_undersupplied_areas": "Q2 · Zones où l'offre est insuffisante (mailles de 0,01°)",
            "q3_hourly_patterns_france_time": "Q3 · Profil horaire",
            "q4_geographic_imbalance": "Q4 · Déséquilibres géographiques",
            "q5_critical_stations": "Q5 · Stations critiques (≤ 10 % sur 3 collectes consécutives)",
        }
        for name, title in titles.items():
            if name in gold:
                st.markdown(f"**{title}** · {len(gold[name])} lignes")
                st.dataframe(gold[name].head(15), hide_index=True, use_container_width=True)
        explain("Tables calculées par le script du binôme partie 2 (`main.py --analyze`) à partir des fichiers "
                "silver (Parquet). Q1 : occupation moyenne par station (1 = station sans vélo). Q2 et Q4 : mailles "
                "géographiques d'environ 1 km. Q5 : stations restées à 10 % de vélos ou moins pendant 3 collectes "
                "consécutives en moins de 10 minutes.",
                "Ces indicateurs mesurent le **stock**, pas les trajets : une station « très utilisée » au sens de "
                "Q1 est une station souvent vide. Ils ne sont fiables qu'après plusieurs jours de collecte : relancer "
                "`docker compose --profile streaming run --rm streaming-velib --analyze --days 7`.")

with tab_flow:
    snaps = query("""SELECT city, network_id, count(*) AS collectes, min(collected_at) AS premiere,
                            max(collected_at) AS derniere, avg(n_stations) AS stations_moy
                     FROM mobility.network_snapshots GROUP BY 1, 2 ORDER BY 1, 2""")
    st.dataframe(snaps, hide_index=True, use_container_width=True)
