import plotly.express as px
import streamlit as st

from common import explain, query

st.set_page_config(page_title="Pollution · UrbanHub", page_icon="🌫️", layout="wide")
st.title("🌫️ Partie 3 : pollution de l'air (flux IoT OpenAQ)")

# Couleur fixe par polluant : elle suit l'entité, jamais le rang
POLLUTANT_COLORS = {"pm25": "#2a78d6", "pm10": "#eb6834", "no2": "#1baf7a", "o3": "#eda100", "co": "#e87ba4"}
CITY_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
LABELS = {"pm25": "PM2.5", "pm10": "PM10", "no2": "NO2", "o3": "O3", "co": "CO"}
PCT = dict(tickformat=".0%")

try:
    fresh = query("SELECT * FROM pollution.v_freshness")
except Exception as exc:
    st.error(f"Connexion à MinIO impossible ou données du lake indisponibles : {exc}")
    st.stop()
if fresh.empty or fresh["n_measures"].sum() == 0:
    st.warning("Aucune mesure : lancer `docker compose --profile iot up -d`.")
    st.stop()

# ---- filtres (une seule rangée) ---------------------------------------------
cities = sorted(fresh["city"].unique())
c1, c2 = st.columns([3, 1])
sel_cities = c1.multiselect("Villes (max 4 pour les courbes)", cities, default=cities[:4], max_selections=4)
pollutant = c2.selectbox("Polluant", list(LABELS), format_func=LABELS.get)

# ---- KPIs -------------------------------------------------------------------
k1, k2, k3, k4 = st.columns(4)
k1.metric("Capteurs actifs", int(fresh["n_sensors"].sum()))
k2.metric("Mesures stockées", f"{int(fresh['n_measures'].sum()):,}".replace(",", " "))
k3.metric("Villes couvertes", len(cities))
last = fresh["last_measure"].max()
k4.metric("Dernière mesure (UTC)", last.strftime("%d/%m %H:%M") if last is not None else "-")

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "Q1 · Zones les plus polluées", "Q2 · Variation journalière", "Q3 · Polluants dominants",
    "Q4 · Épisodes anormaux", "Qualité du flux",
])

# ---- Q1 ---------------------------------------------------------------------
with tab1:
    st.caption("Ratio OMS = concentration moyenne / ligne directrice OMS 2021 sur 24 h. "
               "Au-dessus de 1, la zone dépasse la recommandation.")
    zones = query("SELECT * FROM pollution.v_zone_levels WHERE pollutant = %s AND n_hours >= 24", (pollutant,))
    if zones.empty:
        st.info("Pas encore assez de données pour ce polluant.")
    else:
        fig = px.scatter_map(
            zones, lat="latitude", lon="longitude", color="who_ratio", size="mean_value",
            hover_name="location_name",
            hover_data={"city": True, "mean_value": ":.1f", "p95_value": ":.1f", "who_ratio": ":.2f",
                        "latitude": False, "longitude": False},
            color_continuous_scale="Oranges", zoom=4.6, center={"lat": 46.6, "lon": 2.4},
            map_style="open-street-map", height=520,
            labels={"who_ratio": "Ratio OMS", "mean_value": "Moyenne µg/m³", "p95_value": "P95 µg/m³"},
        )
        st.plotly_chart(fig, use_container_width=True)
        st.markdown(f"**Top 10 des zones ({LABELS[pollutant]})**")
        st.dataframe(
            zones.sort_values("mean_value", ascending=False).head(10)[
                ["city", "location_name", "mean_value", "p95_value", "max_value", "who_ratio", "n_hours"]],
            hide_index=True, use_container_width=True,
        )
        explain(
            "Chaque cercle est un site de mesure. Sa **taille** donne la concentration moyenne et sa **couleur** le "
            "**ratio OMS** (moyenne / ligne directrice OMS 2021 sur 24 h). Au-delà de 1, la recommandation sanitaire "
            "est dépassée en moyenne. Le tableau classe les 10 sites les plus chargés pour le polluant choisi "
            "(P95 = valeur dépassée 5 % du temps).",
            "**Q1** : les zones les plus polluées sont les **sites de trafic**, toutes pour le NO2 : A1 à Saint-Denis "
            "(ratio 2,0), Marseille Plombières (1,9), périphérique de Lyon (1,8), Montpellier Liberté, RN20, "
            "boulevard périphérique Est à Paris, A35 à Strasbourg. Les sites de fond urbain restent sous le seuil. La "
            "pollution est donc **très localisée** près des grands axes.")

    lvl = query("SELECT * FROM pollution.v_city_levels")
    fig = px.bar(
        lvl.sort_values("who_ratio"), x="who_ratio", y="city", color="pollutant", barmode="group",
        orientation="h", color_discrete_map=POLLUTANT_COLORS, height=560,
        labels={"who_ratio": "Concentration moyenne / seuil OMS", "city": "", "pollutant": "Polluant"},
        title="Niveau moyen par ville, rapporté au seuil OMS",
    )
    fig.add_vline(x=1, line_dash="dot", line_color="#52514e", annotation_text="seuil OMS")
    st.plotly_chart(fig, use_container_width=True)
    explain("Une barre par ville et par polluant : la concentration moyenne divisée par le seuil OMS. La ligne "
            "pointillée (= 1) marque le seuil.",
            "Le NO2 est le polluant le plus proche du seuil : **Marseille (1,07)** le dépasse en moyenne, suivie de "
            "Strasbourg (0,98), Paris (0,93) et Montpellier (0,87). Les particules restent nettement en dessous "
            "(ratio 0,25 à 0,58 sur septembre-octobre) : l'automne est une saison peu chargée en PM, alors que "
            "l'hiver (chauffage au bois) l'est davantage.")

# ---- Q2 ---------------------------------------------------------------------
with tab2:
    prof = query("SELECT * FROM pollution.v_hourly_profile WHERE pollutant = %s", (pollutant,))
    prof = prof[prof["city"].isin(sel_cities)]
    if prof.empty:
        st.info("Pas de données pour cette sélection.")
    else:
        fig = px.line(
            prof.sort_values("hour_local"), x="hour_local", y="mean_value", color="city",
            facet_col="day_type", markers=True, color_discrete_sequence=CITY_COLORS,
            category_orders={"city": sel_cities, "day_type": ["semaine", "week-end"]},
            labels={"hour_local": "Heure locale", "mean_value": f"{LABELS[pollutant]} moyen (µg/m³)",
                    "city": "Ville", "day_type": ""},
            title=f"Profil journalier moyen de {LABELS[pollutant]}", height=450,
        )
        fig.update_traces(line_width=2)
        fig.update_xaxes(dtick=3)
        st.plotly_chart(fig, use_container_width=True)
        amp = prof.groupby(["city", "day_type"])["mean_value"].agg(["min", "max", "idxmax"]).reset_index()
        amp["heure_du_pic"] = prof.loc[amp["idxmax"], "hour_local"].values
        amp["amplitude"] = amp["max"] - amp["min"]
        st.dataframe(amp.drop(columns="idxmax"), hide_index=True, use_container_width=True)
        explain(
            "Concentration moyenne pour chaque heure de la journée (heure locale), en semaine et le week-end. "
            "Le tableau donne, par ville, l'heure du pic et l'amplitude (écart entre l'heure la plus polluée et la "
            "plus propre).",
            "**Q2 : oui, la variation journalière est forte et significative** (test de Kruskal-Wallis, p < 0,001 "
            "pour toutes les villes et tous les polluants, voir le notebook). Le **NO2** culmine vers **10 h** en "
            "semaine (31 µg/m³ en moyenne, contre 16 le week-end), soit la signature du **trafic du matin**, "
            "renforcée par une atmosphère encore peu mélangée. Il remonte le soir (22-23 h), quand la couche d'air "
            "proche du sol s'abaisse et piège les émissions. L'**O3** fait l'inverse : minimum vers 9-10 h (34 µg/m³), "
            "**maximum vers 17 h** (85 µg/m³), car il se forme au soleil et se trouve détruit par le NO2 le matin.")

# ---- Q3 ---------------------------------------------------------------------
with tab3:
    st.caption("Les concentrations brutes ne se comparent pas d'un polluant à l'autre. Pour chaque heure, "
               "le polluant dominant est celui qui a le plus grand sous-indice (valeur / seuil OMS).")
    dom = query("SELECT * FROM pollution.v_dominant_pollutant")
    fig = px.bar(
        dom, x="share_dominant", y="city", color="pollutant", orientation="h",
        color_discrete_map=POLLUTANT_COLORS, category_orders={"pollutant": list(LABELS)},
        labels={"share_dominant": "Part des heures où le polluant domine", "city": "", "pollutant": "Polluant"},
        title="Polluant dominant par ville", height=480,
    )
    fig.update_traces(marker_line_width=2, marker_line_color="white")
    fig.update_xaxes(**PCT)
    st.plotly_chart(fig, use_container_width=True)
    st.dataframe(dom.sort_values(["city", "dominance_rank"]), hide_index=True, use_container_width=True)
    explain("Pour chaque ville, une barre empilée donne la part des heures où chaque polluant est **dominant**, "
            "c'est-à-dire le plus élevé **par rapport à son seuil OMS**. On ne compare pas les µg/m³ bruts : l'O3 "
            "ou le CO auraient toujours les plus gros chiffres.",
            "**Q3** : deux familles de villes. **NO2 dominant** (trafic) dans les grandes villes et les villes denses : "
            "Strasbourg (68 % des heures), Marseille (63 %), Paris (57 %), Montpellier, Lyon. **O3 dominant** sur la "
            "façade atlantique et en Méditerranée ensoleillée : Bordeaux (57 %), Nice, Rennes, Lille, Nantes. Pour "
            "une ville étudiée comme Paris, la priorité est donc la réduction du trafic routier.")

# ---- Q4 ---------------------------------------------------------------------
with tab4:
    st.caption("Un épisode est un jour anormal pour une ville et un polluant. Il est détecté soit par un seuil "
               "réglementaire (information, alerte, OMS), soit statistiquement : au moins 3 heures avec un "
               "z-score > 3 par rapport aux 7 jours précédents.")
    epi = query("SELECT * FROM pollution.v_pollution_episodes ORDER BY day DESC")
    if epi.empty:
        st.success("Aucun épisode détecté sur la période.")
    else:
        counts = epi.groupby(["city", "episode_level"]).size().reset_index(name="jours")
        fig = px.bar(counts, x="jours", y="city", color="episode_level", orientation="h",
                     category_orders={"episode_level": ["alerte", "information", "depassement_OMS",
                                                        "anomalie_statistique"]},
                     color_discrete_sequence=["#e34948", "#eb6834", "#eda100", "#6250d6"],
                     labels={"jours": "Nombre de jours", "city": "", "episode_level": "Niveau"},
                     title="Jours d'épisode par ville et par niveau", height=420)
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(epi, hide_index=True, use_container_width=True)
        explain("Nombre de jours d'épisode par ville, par gravité : **alerte** et **information** (seuils "
                "réglementaires), **dépassement OMS** (moyenne journalière au-dessus de la ligne directrice), "
                "**anomalie statistique** (au moins 3 heures très au-dessus des 7 jours précédents, z > 3).",
                "**Q4 : oui.** On compte 113 jours de dépassement OMS pour le NO2 et 12 pour les PM2.5, mais "
                "seulement 2 jours d'« alerte » NO2 à Lyon. Ces deux alertes viennent **d'un seul capteur (LYON "
                "Centre, 400-500 µg/m³)** quand le reste de la ville était à environ 25 : c'est plus probablement "
                "un défaut de capteur qu'un vrai épisode. Les **épisodes régionaux** sont plus parlants : le NO2 "
                "a dépassé le seuil OMS dans **7 villes à la fois les 24 et 25 septembre 2026**, ce qui signe une "
                "situation météo stagnante (voir la partie 4).")

    city = st.selectbox("Série temporelle détaillée : ville", cities)
    ts = query("""SELECT hour_utc, value FROM pollution.v_city_hourly
                  WHERE city = %s AND pollutant = %s ORDER BY hour_utc""", (city, pollutant))
    an = query("""SELECT date_trunc('hour', measured_at) AS hour_utc, max(value) AS value, max(z_score) AS z
                  FROM pollution.v_anomalies
                  WHERE city = %s AND pollutant = %s AND (is_statistical_anomaly OR exceeds_info_hourly)
                  GROUP BY 1""", (city, pollutant))
    if not ts.empty:
        fig = px.line(ts, x="hour_utc", y="value", height=380,
                      labels={"hour_utc": "", "value": f"{LABELS[pollutant]} (µg/m³)"},
                      title=f"{LABELS[pollutant]} à {city} : moyenne horaire des capteurs et anomalies",
                      color_discrete_sequence=[POLLUTANT_COLORS[pollutant]])
        fig.update_traces(line_width=2, name="moyenne horaire", showlegend=True)
        if not an.empty:
            fig.add_scatter(x=an["hour_utc"], y=an["value"], mode="markers", name="anomalie (max capteur)",
                            marker=dict(size=9, color="#e34948", symbol="x"))
        fig.update_layout(hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)
        explain("La courbe donne la moyenne horaire des capteurs de la ville. Une croix rouge marque une heure où au "
                "moins un capteur est anormal (z-score > 3, ou dépassement du seuil d'information).",
                "Des croix isolées sur un seul capteur signalent une source locale ou une panne. Des croix groupées "
                "sur plusieurs heures, avec une courbe moyenne qui monte, révèlent un véritable épisode de pollution.")

# ---- monitoring ------------------------------------------------------------
with tab5:
    st.dataframe(fresh.sort_values(["city", "pollutant"]), hide_index=True, use_container_width=True)
    runs = query("""SELECT mode, status, started_at, finished_at, api_calls, rows_received,
                           rows_inserted, rows_rejected, raw_path, error
                    FROM ops.ingestion_runs WHERE part = 'pollution' ORDER BY started_at DESC LIMIT 50""")
    st.dataframe(runs, hide_index=True, use_container_width=True)
