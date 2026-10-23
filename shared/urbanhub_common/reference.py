"""Project reference values, versioned with the ingestion code."""
import pandas as pd

def cities():
    return pd.DataFrame([
        ("Paris",48.8566,2.3522,2102650), ("Marseille",43.2965,5.3698,873076),
        ("Lyon",45.7640,4.8357,522250), ("Toulouse",43.6047,1.4442,504078),
        ("Nice",43.7102,7.2620,348085), ("Nantes",47.2184,-1.5536,323204),
        ("Strasbourg",48.5734,7.7521,291313), ("Bordeaux",44.8378,-0.5792,261804),
        ("Lille",50.6292,3.0573,236710), ("Rennes",48.1173,-1.6778,225081),
        ("Montpellier",43.6108,3.8767,302454),
    ], columns=["city","latitude","longitude","population"])

def thresholds():
    return pd.DataFrame([
        ("pm25","PM2.5",None,15,None,None,"daily"),
        ("pm10","PM10",None,45,50,80,"daily"),
        ("no2","NO2",46.01,25,200,400,"hourly"),
        ("o3","O3",48.0,100,180,240,"hourly"),
        ("co","CO",28.01,4000,10000,None,"daily"),
    ], columns=["pollutant","label","molar_mass","who_24h","info_threshold","alert_threshold","basis"])
