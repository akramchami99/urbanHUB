import pandas as pd
import plotly.express as px
import streamlit as st

from common import explain, query

st.set_page_config(page_title="Analyse croisée · UrbanHub", page_icon="🔗", layout="wide")
st.title("🔗 Partie 4 : analyse croisée météo × pollution × mobilité")
st.caption("Vue `analytics.v_city_hourly_all` : jointure ville × heure des trois flux. Usage des vélos = vélos "
           "retirés par station et par heure (archives CityBikes), comparé à l'usage attendu pour la même ville, "
           "la même heure et le même type de jour.")

POLL_COLORS = {"pm25": "#2a78d6", "pm10": "#eb6834", "no2": "#1baf7a", "o3": "#eda100"}
DIVERGING = ["#2a78d6", "#f0efec", "#e34948"]
T_MIN, T_MAX, VENT_MAX = 12, 28, 25   # règle de confort vélo (voir notebook 04)

try:
    df = query("SELECT * FROM analytics.v_city_hourly_all WHERE hour_utc >= '2026-08-31'")
except Exception as exc:
    st.error(f"Connexion à MinIO impossible ou données du lake indisponibles : {exc}")
    st.stop()
df["rain"] = df["precipitation"] > 0.1

k1, k2, k3, k4 = st.columns(4)
k1.metric("Heures × ville (météo)", f"{len(df):,}".replace(",", " "))
k2.metric("… avec pollution", f"{df['no2'].notna().sum():,}".replace(",", " "))
k3.metric("… avec usage vélos", f"{df['departures'].notna().sum():,}".replace(",", " "))
k4.metric("… les 3 flux", f"{(df['departures'].notna() & df['no2'].notna()).sum():,}".replace(",", " "))

tab1, tab2, tab3, tab4 = st.tabs(["Q1 · Météo → pollution", "Q2 · Météo → vélos",
                                  "Q3 · Conditions favorables", "Q4 · Interactions fortes"])

with tab1:
    d1 = df.dropna(subset=["no2", "temperature"]).copy()
    cols = ["pm25", "pm10", "no2", "o3", "temperature", "wind_speed", "precipitation", "pressure"]
    anom = d1[cols] - d1.groupby(["city", "hour_local", "is_weekend"])[cols].transform("mean")
    corr = anom.corr(method="spearman").loc[["pm25", "pm10", "no2", "o3"],
                                            ["temperature", "wind_speed", "precipitation", "pressure"]]
    c1, c2 = st.columns(2)
    c1.plotly_chart(px.imshow(corr.round(2), text_auto=True, zmin=-0.6, zmax=0.6, aspect="auto",
                              color_continuous_scale=DIVERGING, height=380,
                              title="Corrélation des anomalies (cycle journalier retiré)"), use_container_width=True)
    d1["vent"] = pd.cut(d1["wind_speed"], [0, 5, 10, 15, 20, 30, 100],
                        labels=["0-5", "5-10", "10-15", "15-20", "20-30", ">30"], include_lowest=True)
    vent = (d1.groupby("vent", observed=True)[["no2", "pm25", "pm10", "o3"]].mean().reset_index()
            .melt("vent", var_name="polluant", value_name="µg/m³"))
    fig = px.line(vent, x="vent", y="µg/m³", color="polluant", markers=True, color_discrete_map=POLL_COLORS,
                  height=380, title="Concentration moyenne selon le vent (km/h)")
    fig.update_traces(line_width=2)
    c2.plotly_chart(fig, use_container_width=True)
    st.dataframe(d1.groupby("rain")[["no2", "pm25", "pm10", "o3"]].mean().round(1)
                 .rename(index={False: "temps sec", True: "pluie > 0,1 mm/h"}), use_container_width=True)
    explain(
        "**À gauche** : corrélation de Spearman entre polluants et météo, calculée sur les **anomalies**. Pour chaque "
        "ville, on retire d'abord la moyenne de la même heure et du même type de jour : sinon on mesurerait "
        "seulement qu'il fait plus chaud l'après-midi, quand l'ozone est au plus haut. Rouge = les deux montent "
        "ensemble, bleu = l'un monte quand l'autre baisse. **À droite** : concentration moyenne par classe de vent. "
        "**Tableau** : temps sec ou pluvieux.",
        "**Q1 : oui, nettement.** Le **vent disperse** : NO2 de 25 µg/m³ par vent calme à 9 µg/m³ au-delà de 30 km/h "
        "(corrélation -0,51). Le modèle multivarié du notebook donne -39 % de NO2 et -25 % de PM2.5 par +10 km/h. "
        "**L'ozone suit la chaleur** (+0,49, soit +31 % par +5 °C), car il se forme au soleil. **La pluie lave "
        "l'air** : -10 % de NO2 et -11 % de PM10. La pression joue peu. Les situations à risque sont donc l'**air "
        "calme** pour NO2 et particules, et les **journées chaudes** pour l'ozone.")

with tab2:
    d2 = df.dropna(subset=["departures_per_station", "temperature"]).copy()
    d2["attendu"] = d2.groupby(["city", "hour_local", "is_weekend"])["departures_per_station"].transform("mean")
    d2 = d2[d2["hour_local"].between(7, 22) & (d2["attendu"] > 0.05)]
    d2["usage_index"] = d2["departures_per_station"] / d2["attendu"]
    d2["pluie"] = pd.cut(d2["precipitation"], [-0.01, 0.1, 1, 100], labels=["sec", "faible", "forte (> 1 mm)"])
    d2["temp"] = pd.cut(d2["temperature"], [-10, 10, 14, 18, 22, 26, 40],
                        labels=["<10", "10-14", "14-18", "18-22", "22-26", ">26"])
    d2["vent"] = pd.cut(d2["wind_speed"], [0, 10, 20, 30, 100], labels=["0-10", "10-20", "20-30", ">30"],
                        include_lowest=True)
    cs = st.columns(3)
    for c, (col, titre) in zip(cs, [("pluie", "Pluie"), ("temp", "Température (°C)"), ("vent", "Vent (km/h)")]):
        t = d2.groupby(col, observed=True)["usage_index"].mean().reset_index()
        t["écart"] = (t["usage_index"] - 1)
        fig = px.bar(t, x=col, y="écart", height=320, title=f"{titre} : écart d'usage vs attendu",
                     color_discrete_sequence=["#2a78d6"], labels={"écart": "", col: ""})
        fig.update_yaxes(tickformat="+.0%")
        c.plotly_chart(fig, use_container_width=True)
    explain(
        "Chaque barre compare l'usage réel (vélos retirés) à l'**usage attendu** pour une heure comparable : même "
        "ville, même heure, semaine ou week-end. -20 % signifie 20 % de vélos retirés en moins que d'habitude dans "
        "ces conditions. Période : septembre 2026, de 7 h à 22 h.",
        "**Q2 : oui, surtout la pluie** : environ -22 % d'usage dès qu'il pleut (-37 % à Strasbourg, -31 % à Paris). "
        "Le vent a un petit effet (-6 % entre 20 et 30 km/h). La **température n'a pas d'effet** entre 10 et 30 °C, "
        "la plage de septembre. Les classes « < 10 °C » et « > 30 km/h » ne comptent que 11 et 2 heures : ne pas les "
        "interpréter. Au fond, l'usage du vélo suit d'abord la **routine** (domicile-travail) ; la météo ne la "
        "module qu'à la marge, sauf la pluie.")

with tab3:
    d2["favorable"] = ((d2["precipitation"] <= 0.1) & d2["temperature"].between(T_MIN, T_MAX)
                       & (d2["wind_speed"] < VENT_MAX))
    heat = d2.pivot_table(index="temp", columns="pluie", values="usage_index", aggfunc="mean", observed=True)
    c1, c2 = st.columns([1, 2])
    c1.plotly_chart(px.imshow(heat.round(2), text_auto=True, zmin=0.6, zmax=1.4, aspect="auto", height=380,
                              color_continuous_scale=["#e34948", "#f0efec", "#1baf7a"],
                              title="Usage observé / attendu"), use_container_width=True)
    hist = query(f"""SELECT city, extract(month FROM hour_utc AT TIME ZONE 'Europe/Paris')::int AS mois,
                            avg((precipitation <= 0.1 AND temperature BETWEEN {T_MIN} AND {T_MAX}
                                 AND wind_speed < {VENT_MAX})::int) AS part
                     FROM analytics.v_meteo_city_hourly
                     WHERE extract(hour FROM hour_utc AT TIME ZONE 'Europe/Paris') BETWEEN 7 AND 21
                     GROUP BY 1, 2""")
    piv = hist.pivot(index="city", columns="mois", values="part").astype(float) * 100
    c2.plotly_chart(px.imshow(piv.round(0), text_auto=True, aspect="auto", color_continuous_scale="Greens",
                              height=380, labels={"color": "% heures", "x": "mois", "y": ""},
                              title=f"Heures favorables au vélo (sec, {T_MIN}-{T_MAX} °C, vent < {VENT_MAX} km/h), "
                                    "7 h-21 h, 2022-2026"), use_container_width=True)
    st.metric("Usage moyen pendant les heures favorables vs défavorables",
              f"{d2.loc[d2.favorable, 'usage_index'].mean():.2f} vs {d2.loc[~d2.favorable, 'usage_index'].mean():.2f}")
    explain(
        "**À gauche** : usage observé / attendu selon la température et la pluie. Vert = plus de vélos que "
        "d'habitude, rouge = moins. **À droite** : la règle de confort tirée de ce tableau (sec, 12-28 °C, vent < 25 "
        "km/h) est appliquée à **4,5 ans de météo**. Chaque case donne la part des heures de 7 h à 21 h où elle est "
        "respectée, par ville et par mois.",
        "**Q3 : oui.** La condition décisive est **l'absence de pluie** : par temps sec, l'usage reste normal de "
        "10 à 30 °C, et il tombe à 0,72-0,82 sous la pluie. Les meilleurs mois sont **septembre (73 % d'heures "
        "favorables), octobre (70 %) et mai (67 %)**. Juillet-août sont pénalisés par la chaleur dans le Sud "
        "(Marseille : 23 % en juillet). Les villes les plus favorables sont **Nice (43 %), Montpellier, Bordeaux**. "
        "En ajoutant la qualité de l'air (notebook 04), **Nantes, Rennes et Bordeaux** offrent le meilleur "
        "compromis, et Marseille le moins bon (ozone).")

with tab4:
    day = (df.dropna(subset=["departures"]).groupby(["city", "date_local"])
           .agg(temperature=("temperature", "mean"), pluie_mm=("precipitation", "sum"),
                vent=("wind_speed", "mean"), retraits=("departures", "sum"), heures_velo=("departures", "count"),
                no2=("no2", "mean"), pm25=("pm25", "mean"), o3=("o3", "mean"),
                weekend=("is_weekend", "first")).reset_index())
    day = day[day["heures_velo"] >= 20]   # jours complets (pannes de l'archive exclues)
    day["usage_index"] = day["retraits"] / day.groupby(["city", "weekend"])["retraits"].transform("mean")
    day = day.dropna(subset=["no2"])
    cols = ["temperature", "pluie_mm", "vent", "usage_index", "no2", "pm25", "o3"]
    if day.empty:
        st.info("Pas encore de jours avec les trois flux.")
    else:
        z = day.groupby("city")[cols].transform(lambda s: (s - s.mean()) / s.std())
        day["n_forts"] = (z.abs() > 1).sum(axis=1)
        c1, c2 = st.columns([1, 1])
        c1.plotly_chart(px.imshow(day[cols].corr(method="spearman").round(2), text_auto=True, zmin=-1, zmax=1,
                                  color_continuous_scale=DIVERGING, height=420,
                                  title="Corrélations journalières (ville × jour)"), use_container_width=True)
        forts = day[day["n_forts"] >= 3]
        c2.plotly_chart(px.scatter(forts, x="date_local", y="city", size="n_forts", color="pluie_mm",
                                   color_continuous_scale="Blues", height=420,
                                   title=f"{len(forts)} jours d'interaction forte (≥ 3 indicateurs à |z| > 1)"),
                        use_container_width=True)
        st.dataframe(forts.sort_values("n_forts", ascending=False)[["city", "date_local"] + cols].round(1),
                     hide_index=True, use_container_width=True)
        explain(
            "**À gauche** : corrélations entre indicateurs **journaliers** (une ligne = une ville × un jour). "
            "**À droite** : les jours d'**interaction forte**, où au moins 3 indicateurs s'écartent nettement de la "
            "normale de la ville (|z| > 1). La taille du point donne le nombre d'indicateurs extrêmes, la couleur "
            "la pluie du jour.",
            "**Q4 : oui, environ 26 % des jours.** Trois mécanismes reviennent : (1) **vent et pluie** : air propre "
            "(vent × NO2 = -0,52) et moins de vélos (pluie × usage = -0,30) ; (2) **air stagnant** : la pollution "
            "monte ; (3) **chaleur sèche** : ozone en hausse (+0,56) et **plus de cyclistes**. Point clé pour les "
            "décideurs : l'usage du vélo est *positivement* lié au NO2 (+0,19). Ce n'est pas le vélo qui pollue : "
            "le **beau temps calme** attire les cyclistes **et** empêche la dispersion. **Les jours où l'on pédale "
            "le plus sont ceux où l'on est le plus exposé.**")
