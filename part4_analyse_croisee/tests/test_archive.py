import pandas as pd

from cross_analysis.citybikes_archive import hourly_from_events


def test_usage_and_stock_from_events():
    ev = pd.DataFrame({
        "id":        ["a", "a", "a", "a", "b"],
        "bikes":     [5, 3, 4, 15, 2],     # -2 (retraits), +1 (retour), +11 (camion de rééquilibrage)
        "free":      [5, 7, 6, 0, 8],
        "timestamp": pd.to_datetime(["2026-09-01 00:10", "2026-09-01 00:40", "2026-09-01 01:05",
                                     "2026-09-01 02:30", "2026-09-01 00:20"]),
    })
    h = hourly_from_events(ev, "Paris", "202609").set_index("hour_utc")
    t0, t1, t2 = (pd.Timestamp(f"2026-09-01 0{i}:00", tz="UTC") for i in range(3))
    assert len(h) == 30 * 24
    assert h.loc[t0, "departures"] == 2 and h.loc[t0, "returns"] == 0
    assert h.loc[t1, "returns"] == 1
    assert h.loc[t2, "returns"] == 0                       # +11 d'un coup = rééquilibrage, ignoré
    assert h.loc[t0, "bikes_available"] == 3 + 2           # état de fin d'heure : a=3, b=2
    assert h.loc[t1, "bikes_available"] == 4 + 2           # b inchangé : dernier état propagé
    assert h.loc[t2, "share_full"] == 0.5                  # a pleine (free = 0)
    t5 = pd.Timestamp("2026-09-01 05:00", tz="UTC")
    assert h.loc[t5, "n_changes"] == 0 and pd.isna(h.loc[t5, "departures"])   # aucun changement = panne, pas 0
