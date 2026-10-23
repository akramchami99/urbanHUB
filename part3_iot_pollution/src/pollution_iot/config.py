import os

from dotenv import load_dotenv

load_dotenv()

OPENAQ_BASE_URL = "https://api.openaq.org/v3"
OPENAQ_API_KEY = os.getenv("OPENAQ_API_KEY", "")
# Quotas du plan gratuit : 60 requêtes / minute et 2000 / heure
OPENAQ_MAX_PER_MINUTE = 55
OPENAQ_MAX_PER_HOUR = 1900
OPENAQ_PAGE_LIMIT = 1000

POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "10"))
BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "30"))
CITY_RADIUS_M = min(int(os.getenv("CITY_RADIUS_M", "25000")), 25000)
COUNTRY_ISO = "FR"

# Un capteur sans mesure depuis ce délai est considéré inactif (non interrogé)
SENSOR_INACTIVE_AFTER_DAYS = 7

# Polluants retenus (noms des paramètres OpenAQ)
POLLUTANTS = ("pm25", "pm10", "o3", "no2", "co")

# Masses molaires (g/mol) pour convertir les ppm/ppb en µg/m³ (conditions 25 °C, 1 atm)
MOLAR_MASS = {"no2": 46.01, "o3": 48.00, "co": 28.01}
MOLAR_VOLUME_L = 24.45

# Bornes physiquement plausibles en µg/m³ : au-delà = erreur capteur, la mesure est rejetée
PLAUSIBLE_MAX = {"pm25": 1000, "pm10": 2000, "no2": 2000, "o3": 1000, "co": 100_000}
