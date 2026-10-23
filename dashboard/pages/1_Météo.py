import pandas as pd
import plotly.express as px
import streamlit as st

from common import explain, query

st.set_page_config(page_title="Météo · UrbanHub", page_icon="🌦️", layout="wide")
st.title("🌦️ Partie 1 : météo urbaine (flux batch NOAA + Open-Meteo)")
st.caption("NOAA Global Hourly (stations françaises, 2022 → août 2025), complété par Open-Meteo "
           "(un point par ville, de la fin de NOAA à aujourd'hui).")

CITY_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]

try:
    daily = query("""SELECT d.date, d.station_id, s.city, s.source, d.temp_mean, d.temp_min, d.temp_max,
                            d.wind_max, d.pressure_mean, d.visibility_mean, d.precipitation
                     FROM meteo.daily d JOIN meteo.stations s USING (station_id)""")
except Exception as exc:
    st.error(f"Connexion à MinIO impossible ou données du lake indisponibles : {exc}")
    st.stop()
if daily.empty:
    st.warning("Aucune donnée : lancer `docker compose --profile batch run --rm batch-meteo`.")
    st.stop()

daily["date"] = pd.to_datetime(daily["date"])
cities = sorted(daily["city"].dropna().unique())
sel_cities = st.multiselect("Villes (max 4 pour les courbes)", cities,
                            default=[c for c in ["Paris", "Marseille", "Lille", "Strasbourg"] if c in cities][:4],
                            max_selections=4)

k1, k2, k3, k4 = st.columns(4)
k1.metric("Stations NOAA", daily.loc[daily["source"] == "noaa", "station_id"].nunique())
k2.metric("Jours × stations", f"{len(daily):,}".replace(",", " "))
k3.metric("Période", f"{daily['date'].min():%m/%Y} → {daily['date'].max():%m/%Y}")
last_noaa = daily.loc[daily["source"] == "noaa", "date"].max()
k4.metric("Fin des données NOAA", last_noaa.strftime("%d/%m/%Y") if pd.notna(last_noaa) else "Non chargé")

tab1, tab2, tab3, tab4, tab5 = st.tabs(["Q1 · Périodes anormales", "Q2 · Météo et visibilité",
                                        "Q3 · Saisonnalité", "Q4 · Jours extrêmes", "Sources"])

# ---- Q1 : z-score de la température France par rapport à la normale du mois (méthode du notebook)
with tab1:
    serie = st.radio("Série de référence", ["11 villes (série homogène)", "Toutes les stations (méthode du notebook)"],
                     horizontal=True)
    if serie.startswith("11"):
        france = (daily.dropna(subset=["city"]).groupby(["date", "city"])["temp_mean"].mean()
                  .groupby("date").mean())
    else:
        france = daily.groupby("date")["temp_mean"].mean()
    mois = france.index.month
    z = (france - france.groupby(mois).transform("mean")) / france.groupby(mois).transform("std")
    anormaux = france[z.abs() > 2]
    fig = px.line(x=france.index, y=france.values, height=380,
                  labels={"x": "", "y": "Température moyenne France (°C)"},
                  title=f"Température moyenne France : {len(anormaux)} jours anormaux (|z| > 2 vs normale du mois)")
    fig.update_traces(line_width=1.5, line_color="#52514e", name="moyenne France", showlegend=True)
    fig.add_scatter(x=anormaux.index, y=anormaux.values, mode="markers", name="jour anormal",
                    marker=dict(color="#e34948", size=7))
    fig.update_layout(hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)
    groupe = (anormaux.index.to_series().diff().dt.days != 1).cumsum()
    periodes = (pd.DataFrame({"t": anormaux, "z": z[anormaux.index]}).groupby(groupe.values)
                .agg(debut=("t", lambda s: s.index.min().date()), fin=("t", lambda s: s.index.max().date()),
                     nb_jours=("t", "size"), temperature_moyenne=("t", "mean"), z_moyen=("z", "mean")))
    periodes["type"] = periodes["z_moyen"].map(lambda v: "chaud" if v > 0 else "froid")
    st.markdown("**Périodes anormales (jours consécutifs)**")
    st.dataframe(periodes.sort_values("nb_jours", ascending=False).round(1).head(15),
                 hide_index=True, use_container_width=True)
    explain(
        "La courbe grise montre la température moyenne de chaque jour. Un point rouge marque un jour **anormal** : "
        "sa température s'écarte de plus de 2 écarts-types (|z| > 2) de la moyenne habituelle de son mois. Dans une "
        "distribution normale, cela n'arrive que 5 % du temps. Le tableau regroupe les jours anormaux consécutifs en "
        "**périodes** : z positif pour une période chaude, négatif pour une période froide.",
        "Oui, on identifie des périodes anormales : environ 68 jours sur 1 751 (4 %), surtout des vagues de chaleur "
        "(fin mai et fin juin 2026, juillet 2026) et des coups de froid (début avril 2022, fin février 2023). "
        "⚠️ Choisissez la **série homogène** : avec « toutes les stations », 2026 n'est représenté que par les "
        "11 villes, plus chaudes de 0,7 °C en moyenne que les stations rurales et de montagne, et l'année paraît "
        "artificiellement chaude (30 jours « chauds », contre 21 avec la série homogène). Le modèle Open-Meteo est "
        "aussi 0,3 °C plus chaud que NOAA (vérifié sur 2024). Les vagues de chaleur de 2026, à z ≈ 2,7-2,9, restent "
        "donc réelles, mais le nombre de jours chauds de 2026 est un peu surestimé.")

# ---- Q2 : corrélation de Spearman avec la visibilité
with tab2:
    cols = ["visibility_mean", "temp_mean", "wind_max", "pressure_mean", "precipitation"]
    corr = daily[cols].corr(method="spearman")
    fig = px.imshow(corr.round(2), text_auto=True, zmin=-1, zmax=1, height=460,
                    color_continuous_scale=["#2a78d6", "#f0efec", "#e34948"],
                    title="Corrélation de Spearman entre variables météo (journalières)")
    st.plotly_chart(fig, use_container_width=True)
    st.dataframe(corr["visibility_mean"].drop("visibility_mean").sort_values().round(2).rename("corrélation")
                 .to_frame(), use_container_width=True)
    st.caption("Une corrélation ne prouve pas une causalité. La pluie et la visibilité sont souvent manquantes.")
    explain(
        "Chaque case donne la corrélation de **Spearman** entre deux variables journalières. Elle va de -1 (quand "
        "l'une monte, l'autre baisse) à +1 (elles montent ensemble). Le bleu indique une corrélation négative, le "
        "rouge une corrélation positive, le blanc une absence de lien. Spearman travaille sur les rangs : il est "
        "robuste aux valeurs extrêmes et aux relations non linéaires, fréquentes avec la visibilité (plafonnée par "
        "les instruments).",
        "Les liens existent mais sont **faibles** : la pluie réduit la visibilité (-0,16), la pression aussi "
        "légèrement (-0,10, car l'air stable des anticyclones piège brume et particules), et la chaleur l'améliore "
        "(+0,20, car l'air chaud est plus sec, avec moins de brouillard). À l'échelle de la journée, la visibilité "
        "dépend surtout de phénomènes ponctuels (brouillard matinal, averses), que la moyenne journalière lisse.")

# ---- Q3 : cycle saisonnier France + villes
with tab3:
    daily["mois"] = daily["date"].dt.month
    fr = daily.groupby("mois")["temp_mean"].mean().reset_index().assign(city="France")
    vm = (daily[daily["city"].isin(sel_cities)].groupby(["city", "mois"])["temp_mean"].mean().reset_index())
    data = pd.concat([fr, vm])
    fig = px.line(data, x="mois", y="temp_mean", color="city", markers=True, height=420,
                  color_discrete_map={"France": "#0b0b0b", **dict(zip(sel_cities, CITY_COLORS))},
                  category_orders={"city": ["France"] + sel_cities},
                  labels={"mois": "Mois", "temp_mean": "Température moyenne (°C)", "city": ""},
                  title="Cycle saisonnier de la température")
    fig.update_traces(line_width=2)
    fig.update_xaxes(dtick=1)
    st.plotly_chart(fig, use_container_width=True)
    st.dataframe(daily.dropna(subset=["city"]).pivot_table(index="mois", columns="city", values="temp_mean")
                 .round(1), use_container_width=True)
    explain(
        "Chaque courbe donne la température moyenne de chaque mois, toutes années confondues (2022-2026). La "
        "courbe noire représente la France entière, les couleurs les villes choisies. On compare la **forme** (le "
        "cycle) et le **niveau** (l'écart entre les villes).",
        "Le cycle est très marqué : environ 6 °C en janvier, environ 22 °C en juillet-août pour la France. Le "
        "**Sud** (Marseille, Nice, Montpellier : 8-10 °C en janvier, 26-27 °C en juillet) est plus chaud que le "
        "**Nord-Est** (Strasbourg : 3 °C en janvier ; Lille : 4 °C). L'amplitude est plus forte à l'intérieur "
        "des terres (Strasbourg, Lyon : environ 19 °C entre janvier et juillet) que dans l'Ouest océanique "
        "(Rennes, Nantes : environ 15 °C), car l'océan amortit les variations.")

# ---- Q4 : jours extrêmes (moyenne France par jour, top 5 par catégorie)
with tab4:
    jours = daily.groupby("date").agg(temp_max=("temp_max", "mean"), temp_min=("temp_min", "mean"),
                                      vent_max=("wind_max", "mean"), pluie=("precipitation", "mean"))
    c = st.columns(4)
    for col, (titre, serie) in zip(c, [("🔥 Plus chauds (°C)", jours["temp_max"].nlargest(5)),
                                       ("🥶 Plus froids (°C)", jours["temp_min"].nsmallest(5)),
                                       ("💨 Plus venteux (km/h)", jours["vent_max"].nlargest(5)),
                                       ("🌧️ Plus pluvieux (mm)", jours["pluie"].nlargest(5))]):
        col.markdown(f"**{titre}**")
        col.dataframe(serie.round(1).rename_axis("jour").reset_index().assign(
            jour=lambda d: d["jour"].dt.date), hide_index=True, use_container_width=True)
    explain(
        "Pour chaque jour, on calcule la moyenne nationale du maximum de température, du minimum, de la rafale "
        "maximale et du cumul de pluie, puis on garde les 5 jours les plus extrêmes dans chaque catégorie. Une "
        "moyenne nationale élevée signifie que l'événement a touché **beaucoup de stations en même temps**.",
        "Ces jours correspondent à des événements connus : **canicule fin juin 2026** (max moyen d'environ 37 °C), "
        "**froid de janvier 2025** (min moyen de -4,6 °C), **tempête Ciaran du 2 novembre 2023** (vent max moyen "
        "de 52 km/h), et fortes pluies de septembre 2025 et octobre 2026. Ces dates servent de points de contrôle "
        "pour croiser avec la pollution et les vélos (partie 4).")

with tab5:
    cov = query("""SELECT date_trunc('month', hour_utc)::date AS mois, source, count(*) AS heures
                   FROM meteo.city_hourly GROUP BY 1, 2 ORDER BY 1""")
    fig = px.bar(cov, x="mois", y="heures", color="source", height=360,
                 color_discrete_map={"noaa": "#2a78d6", "open-meteo": "#eb6834"},
                 labels={"mois": "", "heures": "Heures ville disponibles", "source": "Source"},
                 title="Couverture de meteo.city_hourly par source (jointure partie 4)")
    st.plotly_chart(fig, use_container_width=True)
    explain("Nombre d'heures × ville disponibles chaque mois, selon la source. NOAA (mesures de stations) couvre "
            "jusqu'au 24/08/2025, Open-Meteo (modèle météo) prend le relais ensuite.",
            "La série est continue de 2022 à aujourd'hui, ce qui permet de croiser la météo avec la pollution et les "
            "vélos de 2026. Changer de source introduit un léger biais (Open-Meteo est 0,3 °C plus chaud que NOAA en "
            "moyenne, avec une corrélation horaire ≥ 0,97).")
    runs = query("""SELECT mode, status, started_at, finished_at, rows_received, rows_inserted, raw_path, error
                    FROM ops.ingestion_runs WHERE part = 'meteo' ORDER BY started_at DESC LIMIT 20""")
    st.dataframe(runs, hide_index=True, use_container_width=True)
