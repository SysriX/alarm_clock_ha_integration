"""Wecker & Bett – beliebig viele Wecker je Bettseite (Uhrzeit, Wochentage, Weckton, Schlummern,
ansteigende Lautstärke, gesperrte Aus-Taste). Ausgabe auf dem Tablet (Raum-Panel-Modul „wecker“)
und/oder einem media_player je Bettseite. Dazu je Bettseite Gewicht/Belegung und die
Heizdecke (Regelung + Übertemperatur-Schutz, siehe bett.py).

Alle Wecker, Einstellungen und das Protokoll liegen in MariaDB (lokale Kopie in .storage als
Rückfall, falls die Datenbank nicht erreichbar ist). Konfiguration (configuration.yaml):

    wecker:
      db_url: !secret wecker_db_url
      seiten: [links, rechts]      # optional, Standard: links + rechts
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .const import CONF_DB_URL, CONF_SEITEN, DOMAIN
from .manager import WeckerManager

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({
    vol.Required(CONF_DB_URL): cv.string,
    vol.Optional(CONF_SEITEN): vol.All(cv.ensure_list, [cv.slug]),
})}, extra=vol.ALLOW_EXTRA)

TARGET = vol.Schema({vol.Optional("alarm_id"): cv.string, vol.Optional("seite"): cv.string})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    mgr = WeckerManager(hass, config[DOMAIN])
    hass.data[DOMAIN] = mgr
    websocket.async_register(hass)
    await mgr.async_start()

    async def snooze(call: ServiceCall) -> None:
        await mgr.async_snooze(call.data.get("alarm_id"), call.data.get("seite"))

    async def stop(call: ServiceCall) -> None:
        await mgr.async_stop(call.data.get("alarm_id"), call.data.get("seite"))

    async def test(call: ServiceCall) -> None:
        try:
            await mgr.async_test(call.data["alarm_id"])
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def rescan(_call: ServiceCall) -> None:
        await mgr.async_scan_sounds()
        mgr._push()

    hass.services.async_register(DOMAIN, "snooze", snooze, TARGET)
    hass.services.async_register(DOMAIN, "stop", stop, TARGET)
    hass.services.async_register(DOMAIN, "test", test, vol.Schema({vol.Required("alarm_id"): cv.string}))
    hass.services.async_register(DOMAIN, "reload_sounds", rescan)

    async def heat_on(call: ServiceCall) -> None:
        try:
            await mgr.bett.async_heat_on(call.data["seite"], call.data.get("temperatur"), call.data.get("dauer_min"), call.data.get("dauerhaft", False))
        except ValueError as err:   # z. B. Übertemperatur-Sperre → verständliche Meldung statt Fehler 500
            raise ServiceValidationError(str(err)) from err

    async def heat_off(call: ServiceCall) -> None:
        await mgr.bett.async_heat_off(call.data["seite"], "Dienst wecker.heizdecke_aus")

    hass.services.async_register(DOMAIN, "heizdecke_an", heat_on, vol.Schema({
        vol.Required("seite"): cv.string, vol.Optional("temperatur"): vol.Coerce(float),
        vol.Optional("dauer_min"): vol.Coerce(int), vol.Optional("dauerhaft", default=False): cv.boolean}))
    hass.services.async_register(DOMAIN, "heizdecke_aus", heat_off, vol.Schema({vol.Required("seite"): cv.string}))
    hass.async_create_task(async_load_platform(hass, "sensor", DOMAIN, {}, config))
    return True
