"""Konstanten für den Wecker."""

DOMAIN = "wecker"
VERSION = "1.4.0"

CONF_DB_URL = "db_url"
CONF_SEITEN = "seiten"

CACHE_STORE_KEY = "wecker.cache"
CACHE_STORE_VERSION = 1

# Wecktöne: Dateien in /config/www/wecker/toene (wav, mp3, ogg, m4a) → URL /local/wecker/toene/<datei>
SOUND_DIR = "www/wecker/toene"
SOUND_URL = "/local/wecker/toene/"
SOUND_EXT = (".wav", ".mp3", ".ogg", ".m4a", ".flac")

SYNC_INTERVAL_S = 60          # DB neu verbinden / Fälligkeiten nachprüfen
CATCH_UP_S = 15 * 60          # verpasste Weckzeit (HA war aus) bis 15 min später noch nachholen
EVENTS_KEEP_DAYS = 730

# Ereignisse auf dem HA-Bus (für Automationen: Bettlicht, Lautsprecher, …)
EVENT_RING = "wecker_klingelt"
EVENT_SNOOZE = "wecker_schlummert"
EVENT_STOP = "wecker_aus"
EVENT_HEAT = "wecker_heizdecke"        # Heizdecke an/aus/Schutz

DEFAULT_SEITEN = {"links": "Links", "rechts": "Rechts"}

# Klangregler (Soundsystem) und Vibrationsmuster (ESP32 „Bett Vibration“, Option des select)
EQ_BANDS = ("bass", "tiefen", "mitten", "hoehen")
VIB_MUSTER = ("puls", "welle", "herzschlag", "sos", "steigend", "dauer")

DEFAULT_ALARM = {
    "name": "Wecker",
    "seite": "links",
    "enabled": True,
    "time": "07:00",
    "days": [0, 1, 2, 3, 4],       # 0 = Montag … 6 = Sonntag · leer = einmalig
    "sound": "",                   # Dateiname in SOUND_DIR, leer = erster vorhandener
    "snooze": True,
    "snooze_min": 9,
    "snooze_max": 0,               # 0 = beliebig oft
    "ramp": True,                  # Lautstärke ansteigend
    "vol_min": 10,                 # %
    "vol_max": 80,                 # %
    "ramp_s": 120,                 # Sekunden von min bis max
    "lock": "rechnen",             # Sperre der Aus-Taste: rechnen | halten | keine
    "auto_stop_min": 30,           # nach so vielen Minuten Klingeln von selbst aus
    "skip_date": None,             # "YYYY-MM-DD": an diesem Tag einmal auslassen
    "sort": 50,
    # ab 1.3.0 (Spalte extra): zufälliger Ton, Vibrationsalarm, Klang des Soundsystems
    "zufall": False,              # bei jedem Klingeln einen anderen Ton aus dem Ordner
    "vib": False,                 # Vibrationsalarm (Gerät: Einstellungen → Wecker → Vibration)
    "vib_muster": "puls",         # puls | welle | herzschlag | sos | steigend | dauer
    "vib_staerke": 70,            # %
    "vib_verz_s": 0,              # Vibration erst nach so vielen Sekunden
    "eq": {"bass": None, "tiefen": None, "mitten": None, "hoehen": None},   # % des Reglerbereichs, None = unverändert
}

# Bett je Seite: Gewicht (Summe aus Wägezellen oder ein Gesamt-Sensor), Heizdecke + Matratzentemperatur
BETT_ZELLEN = ("kopf_aussen", "kopf_innen", "fuss_aussen", "fuss_innen")
DEFAULT_BETT_SEITE = {
    "gesamt": "",                                  # Sensor mit dem Gesamtgewicht (kg) – oder Zellen:
    "zellen": {k: "" for k in BETT_ZELLEN},         # Wägezellen-Sensoren (kg), Summe = Gewicht
    "tara": 0.0,                                   # Leergewicht (kg), wird abgezogen
    "heizung": "",                                 # Heizdecke: switch/light/fan/input_boolean/climate
    "temperatur": "",                              # Matratzentemperatur (°C)
}
DEFAULT_BETT = {
    "max_kg": 200,              # Ende der Gewichtsanzeige
    "belegt_kg": 20,            # ab diesem Gewicht gilt die Seite als belegt
    "temp_min": 15,             # Anfang der Temperaturanzeige
    "temp_max": 45,             # Ende der Temperaturanzeige
    "max_temp": 38,             # höchste einstellbare Zieltemperatur
    "schutz_temp": 45,          # Übertemperatur: sofort aus + gesperrt bis 5 °C darunter
    "max_dauer_min": 600,       # längste Laufzeit (auch "dauerhaft")
    "standard_temp": 30,
    "standard_dauer_min": 60,
    "hysterese": 1.0,           # °C: ein unter Ziel − h/2, aus über Ziel + h/2
    **{k: {**DEFAULT_BETT_SEITE, "zellen": dict(DEFAULT_BETT_SEITE["zellen"])} for k in DEFAULT_SEITEN},
}

# Hintergrund der Bett-Tablets je Seite (Einstellungen → Allgemein)
#   art: aurora | himmel (echter Sonnen-/Mondstand + Mondphase) | beides | farbe | bild
#   bild: media-source-ID aus /media/wallpaper, leer = alle Bilder als Diashow
HINTERGRUND_ARTEN = ("aurora", "himmel", "beides", "farbe", "bild")
DEFAULT_HINTERGRUND = {"art": "aurora", "farbe": "#101a2c", "bild": "", "diashow_min": 10, "abdunkeln": 25, "blick": 180, "glas_blur": 10, "glas_deckkraft": 50, "batterie_meldungen": True, "schrift": "standard"}
SCHRIFTEN = ("standard", "charlie")

DEFAULT_SETTINGS = {
    # je Bettseite: Anzeige-Name, Ausgabe im Browser (Tablet) und optional ein media_player
    "seiten": {k: {"name": v, "browser": True, "player": ""} for k, v in DEFAULT_SEITEN.items()},
    "uhr": {"zeitzone": "tablet"},              # tablet = Zeitzone des Tablets · server = Zeitzone von HA
    "bett": DEFAULT_BETT,
    "hintergrund": {k: dict(DEFAULT_HINTERGRUND) for k in DEFAULT_SEITEN},
    # ab 1.3.0: Soundsystem beim Wecken (Lautstärke-/Klangregler, Einschalter, Quelle) und Vibrationsgerät je Seite
    "ausgabe": {k: {"laut": "", "ein": "", "quelle": "", "quelle_opt": "", "eq": {b: "" for b in ("bass", "tiefen", "mitten", "hoehen")}, "zurueck": True} for k in DEFAULT_SEITEN},
    "vibration": {k: {"schalter": "", "staerke": "", "muster": ""} for k in DEFAULT_SEITEN},
}
