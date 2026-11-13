"""Sensoren je Bettseite: Wecker-Zustand, nächste Weckzeit, Gewicht, Belegung, Heizdecke."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfMass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .bett import SIGNAL_BETT
from .manager import SIGNAL_UPDATE, WeckerManager


async def async_setup_platform(hass: HomeAssistant, _config, async_add_entities: AddEntitiesCallback, _discovery=None) -> None:
    mgr: WeckerManager = hass.data[DOMAIN]
    ents: list[SensorEntity] = []
    for key, s in mgr.seiten().items():
        n = s.get("name") or key
        ents += [StateSensor(mgr, key, n), NextSensor(mgr, key, n), WeightSensor(mgr, key, n), OccupancySensor(mgr, key, n), HeatSensor(mgr, key, n)]
    async_add_entities(ents)


class _Base(SensorEntity):
    _attr_should_poll = False

    def __init__(self, mgr: WeckerManager, seite: str) -> None:
        self.mgr = mgr
        self.seite = seite

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


class StateSensor(_Base):
    """sensor.wecker_<seite>: bereit | klingelt | schlummert | aus"""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["bereit", "klingelt", "schlummert", "aus"]

    def __init__(self, mgr: WeckerManager, seite: str, name: str) -> None:
        super().__init__(mgr, seite)
        self._attr_name = f"Wecker {name}"
        self._attr_unique_id = f"{DOMAIN}_{seite}_zustand"
        self.entity_id = f"sensor.wecker_{seite}"

    @property
    def native_value(self) -> str:
        return self.mgr.seite_state(self.seite)

    @property
    def icon(self) -> str:
        return {"klingelt": "mdi:alarm-light", "schlummert": "mdi:alarm-snooze", "bereit": "mdi:alarm", "aus": "mdi:alarm-off"}[self.native_value]

    @property
    def extra_state_attributes(self) -> dict:
        nx = self.mgr.next_for(self.seite) or {}
        ring = next(({"id": aid, **r} for aid, r in self.mgr.rings.items() if r.get("seite") == self.seite), None)
        return {
            "naechster": nx.get("at"), "naechster_name": nx.get("name"), "naechster_id": nx.get("id"),
            "wecker_id": ring and ring["id"], "wecker_name": ring and ring.get("name"),
            "schlummert_bis": ring and ring.get("until"), "geschlummert": ring and ring.get("count"),
            "anzahl_wecker": sum(1 for a in self.mgr.data["alarms"].values() if a.get("seite") == self.seite),
        }


class NextSensor(_Base):
    """sensor.wecker_<seite>_naechster: Zeitstempel der nächsten Weckzeit (für Lichtwecker u. Ä.)"""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:alarm-check"

    def __init__(self, mgr: WeckerManager, seite: str, name: str) -> None:
        super().__init__(mgr, seite)
        self._attr_name = f"Wecker {name} nächster"
        self._attr_unique_id = f"{DOMAIN}_{seite}_naechster"
        self.entity_id = f"sensor.wecker_{seite}_naechster"

    @property
    def native_value(self):
        nx = self.mgr.next_for(self.seite)
        return dt_util.parse_datetime(nx["at"]) if nx else None

    @property
    def extra_state_attributes(self) -> dict:
        nx = self.mgr.next_for(self.seite) or {}
        return {"name": nx.get("name"), "wecker_id": nx.get("id"), "uhrzeit": nx.get("time")}


class _BettBase(_Base):
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_BETT, self._update))

    def _live(self) -> dict:
        return self.mgr.bett.live.get(self.seite) or {}


class WeightSensor(_BettBase):
    """sensor.bett_<seite>_gewicht: Gewicht auf dieser Bettseite (kg, ohne Leergewicht)"""

    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfMass.KILOGRAMS
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:scale"

    def __init__(self, mgr: WeckerManager, seite: str, name: str) -> None:
        super().__init__(mgr, seite)
        self._attr_name = f"Bett {name} Gewicht"
        self._attr_unique_id = f"{DOMAIN}_bett_{seite}_gewicht"
        self.entity_id = f"sensor.bett_{seite}_gewicht"

    @property
    def available(self) -> bool:
        return self._live().get("gewicht") is not None

    @property
    def native_value(self):
        return self._live().get("gewicht")

    @property
    def extra_state_attributes(self) -> dict:
        lv, c = self._live(), self.mgr.bett.side(self.seite)
        return {"roh": lv.get("roh"), "tara": c.get("tara"), "zellen": lv.get("zellen"), "belegung": lv.get("belegung")}


class OccupancySensor(_BettBase):
    """sensor.bett_<seite>_belegung: frei | sitzt | liegt | belegt (Haltung unbekannt) | unbekannt"""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["frei", "sitzt", "liegt", "belegt", "unbekannt"]

    def __init__(self, mgr: WeckerManager, seite: str, name: str) -> None:
        super().__init__(mgr, seite)
        self._attr_name = f"Bett {name} Belegung"
        self._attr_unique_id = f"{DOMAIN}_bett_{seite}_belegung"
        self.entity_id = f"sensor.bett_{seite}_belegung"

    @property
    def native_value(self) -> str:
        return self._live().get("belegung") or "unbekannt"

    @property
    def icon(self) -> str:
        return {"frei": "mdi:bed-empty", "sitzt": "mdi:seat-recline-normal", "liegt": "mdi:bed", "belegt": "mdi:bed"}.get(self.native_value, "mdi:bed-outline")


class HeatSensor(_BettBase):
    """sensor.bett_<seite>_heizdecke: aus | heizt | haelt | schutz | fehler"""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["aus", "heizt", "haelt", "schutz", "fehler"]

    def __init__(self, mgr: WeckerManager, seite: str, name: str) -> None:
        super().__init__(mgr, seite)
        self._attr_name = f"Bett {name} Heizdecke"
        self._attr_unique_id = f"{DOMAIN}_bett_{seite}_heizdecke"
        self.entity_id = f"sensor.bett_{seite}_heizdecke"

    @property
    def native_value(self) -> str:
        return self.mgr.bett.status(self.seite)

    @property
    def icon(self) -> str:
        return {"heizt": "mdi:heating-coil", "haelt": "mdi:thermometer-check", "schutz": "mdi:thermometer-alert", "fehler": "mdi:alert"}.get(self.native_value, "mdi:radiator-off")

    @property
    def extra_state_attributes(self) -> dict:
        h = self.mgr.bett.heat.get(self.seite) or {}
        return {"ziel": h.get("target"), "bis": h.get("until"), "dauerhaft": h.get("dauerhaft"), "schalter_an": h.get("relay"),
                "matratze": self._live().get("temperatur"), "heizdecke": self.mgr.bett.side(self.seite).get("heizung")}
