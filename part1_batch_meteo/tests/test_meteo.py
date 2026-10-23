import pandas as pd
import pytest

from meteo_batch import aggregate, noaa

# extrait au format réel NOAA (chaque champ multi-valeurs est entre guillemets)
CSV = """"STATION","DATE","WND","VIS","TMP","SLP","AA1"
"07149099999","2024-01-01T00:00:00","180,1,N,0050,1","010000,1,N,1","+0123,1","10132,1","01,0020,3,1"
"07149099999","2024-01-01T00:30:00","180,1,N,0050,1","010000,1,N,1","+0125,1","10132,1","01,0020,3,1"
"07149099999","2024-01-01T01:00:00","999,9,C,0000,1","999999,9,9,9","+9999,9","99999,9",""
"07149099999","2024-01-01T02:00:00","090,1,N,0030,7","005000,1,N,1","+0800,1","10100,1","06,0050,3,1"
"""


@pytest.fixture
def fichier(tmp_path):
    p = tmp_path / "07149099999.csv"
    p.write_text(CSV)
    return p


def test_nettoyage_noaa(fichier):
    df = noaa.lire_et_nettoyer(fichier).set_index("timestamp")
    h0 = df.loc[pd.Timestamp("2024-01-01T00:00Z")]
    assert len(df) == 3                                      # 00:30 fusionné dans 00:00 (1 mesure / heure)
    assert h0.temperature == pytest.approx(12.3)             # dixièmes de °C
    assert h0.wind_speed == pytest.approx(18.0)              # 5,0 m/s -> 18 km/h
    assert h0.pressure == pytest.approx(1013.2)
    assert h0.precipitation == pytest.approx(2.0) and h0.precip_period_h == 1
    h1 = df.loc[pd.Timestamp("2024-01-01T01:00Z")]
    assert h1[["temperature", "pressure", "visibility"]].isna().all()   # codes 9999 -> NaN
    h2 = df.loc[pd.Timestamp("2024-01-01T02:00Z")]
    assert pd.isna(h2.temperature)                           # 80 °C impossible -> NaN
    assert pd.isna(h2.wind_speed)                            # qualité 7 -> NaN


def test_rattacher_villes():
    st = pd.DataFrame({"station_id": ["orly", "brest"], "latitude": [48.72, 48.45], "longitude": [2.36, -4.42]})
    out = aggregate.rattacher_villes(st, [("Paris", 48.8566, 2.3522), ("Nantes", 47.2184, -1.5536)])
    assert out.loc[0, "city"] == "Paris" and out.loc[0, "distance_km"] < 30
    assert pd.isna(out.loc[1, "city"])                         # Brest : aucune ville à moins de 30 km


def test_horaire_par_ville_direction_vectorielle():
    h = pd.DataFrame({
        "station_id": ["a", "b"], "timestamp": pd.to_datetime(["2024-01-01T00:00Z"] * 2),
        "temperature": [10.0, 12.0], "wind_speed": [10.0, 20.0], "wind_direction": [350.0, 10.0],
        "pressure": [1010.0, 1012.0], "precipitation": [1.0, 3.0], "precip_period_h": [1.0, 6.0],
        "visibility": [10000.0, 8000.0],
    })
    out = aggregate.horaire_par_ville(h, pd.Series({"a": "Paris", "b": "Paris"}), "noaa").iloc[0]
    assert out.temperature == 11.0 and out.n_stations == 2
    assert out.wind_direction in (0.0, 360.0)                # et non 180°
    assert out.precipitation == 1.0                          # seule la mesure sur 1 h compte
