"""WebSocket-API des Weckers (für das Raum-Panel-Modul)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .manager import WeckerManager


def _mgr(hass: HomeAssistant) -> WeckerManager:
    return hass.data[DOMAIN]


@callback
def async_register(hass: HomeAssistant) -> None:
    for cmd in (ws_subscribe, ws_save, ws_delete, ws_snooze, ws_stop, ws_test, ws_settings, ws_events, ws_sounds, ws_heat, ws_tara, ws_vib_test, ws_alive, ws_tablets):
        websocket_api.async_register_command(hass, cmd)


async def _run(connection: websocket_api.ActiveConnection, msg: dict, fn: Callable[[], Awaitable[Any]]) -> None:
    try:
        res = await fn()
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(msg["id"], res if isinstance(res, dict) else {"result": res})


@websocket_api.websocket_command({vol.Required("type"): "wecker/subscribe"})
@websocket_api.async_response
async def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    mgr = _mgr(hass)
    await mgr.async_scan_sounds()

    @callback
    def forward(snap: dict) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], snap))

    connection.subscriptions[msg["id"]] = mgr.async_subscribe(forward)
    connection.send_result(msg["id"])
    forward(mgr.snapshot())


@websocket_api.websocket_command({vol.Required("type"): "wecker/alarm/save", vol.Required("alarm"): dict})
@websocket_api.async_response
async def ws_save(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, lambda: _mgr(hass).async_save_alarm(msg["alarm"]))


@websocket_api.websocket_command({vol.Required("type"): "wecker/alarm/delete", vol.Required("alarm_id"): str})
@websocket_api.async_response
async def ws_delete(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, lambda: _mgr(hass).async_delete_alarm(msg["alarm_id"]))


TARGET = {vol.Optional("alarm_id"): vol.Any(str, None), vol.Optional("seite"): vol.Any(str, None)}


@websocket_api.websocket_command({vol.Required("type"): "wecker/snooze", **TARGET})
@websocket_api.async_response
async def ws_snooze(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, lambda: _mgr(hass).async_snooze(msg.get("alarm_id"), msg.get("seite")))


@websocket_api.websocket_command({vol.Required("type"): "wecker/stop", **TARGET})
@websocket_api.async_response
async def ws_stop(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, lambda: _mgr(hass).async_stop(msg.get("alarm_id"), msg.get("seite")))


@websocket_api.websocket_command({vol.Required("type"): "wecker/test", vol.Required("alarm_id"): str})
@websocket_api.async_response
async def ws_test(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, lambda: _mgr(hass).async_test(msg["alarm_id"]))


@websocket_api.websocket_command({vol.Required("type"): "wecker/settings/save", vol.Required("settings"): dict})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_settings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, lambda: _mgr(hass).async_save_settings(msg["settings"]))


@websocket_api.websocket_command({
    vol.Required("type"): "wecker/events",
    vol.Optional("limit", default=100): vol.All(int, vol.Range(min=1, max=1000)),
    vol.Optional("seite"): vol.Any(str, None),
})
@websocket_api.async_response
async def ws_events(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    ev = await _mgr(hass).async_events(msg["limit"], msg.get("seite"))
    connection.send_result(msg["id"], {"events": ev})


@websocket_api.websocket_command({vol.Required("type"): "wecker/sounds"})
@websocket_api.async_response
async def ws_sounds(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"sounds": await _mgr(hass).async_scan_sounds()})


@websocket_api.websocket_command({
    vol.Required("type"): "wecker/bett/heat",
    vol.Required("seite"): str,
    vol.Required("on"): bool,
    vol.Optional("temperatur"): vol.Any(vol.Coerce(float), None),
    vol.Optional("dauer_min"): vol.Any(vol.Coerce(int), None),
    vol.Optional("dauerhaft", default=False): bool,
})
@websocket_api.async_response
async def ws_heat(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    b = _mgr(hass).bett
    if msg["on"]:
        await _run(connection, msg, lambda: b.async_heat_on(msg["seite"], msg.get("temperatur"), msg.get("dauer_min"), msg["dauerhaft"]))
    else:
        await _run(connection, msg, lambda: b.async_heat_off(msg["seite"], "manuell (Tablet)"))


@websocket_api.websocket_command({vol.Required("type"): "wecker/bett/tara", vol.Required("seite"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_tara(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, lambda: _mgr(hass).bett.async_tara(msg["seite"]))



@websocket_api.websocket_command({
    vol.Required("type"): "wecker/vib_test",
    vol.Required("seite"): str,
    vol.Optional("muster", default="puls"): str,
    vol.Optional("staerke", default=70): vol.All(vol.Coerce(int), vol.Range(min=5, max=100)),
    vol.Optional("dauer_s", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
})
@websocket_api.async_response
async def ws_vib_test(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Vibrationsgerät der Seite kurz mit Muster/Stärke laufen lassen (Einstellungen → Testen)."""
    await _run(connection, msg, lambda: _mgr(hass).async_vib_test(msg["seite"], msg["muster"], msg["staerke"], msg["dauer_s"]))


@websocket_api.websocket_command({
    vol.Required("type"): "wecker/alive",
    vol.Required("seite"): str,
    vol.Optional("playing", default=False): bool,
    vol.Optional("ring"): vol.Any(str, None),
    vol.Optional("visible", default=True): bool,
    vol.Optional("fully", default=False): bool,
    vol.Optional("ua"): str,
})
@callback
def ws_alive(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Lebenszeichen des Panels (alle 30 s, beim Klingeln alle 4 s). Antwort: aktueller Zähler rev –
    weicht er vom zuletzt empfangenen Snapshot ab, ist das Abo des Panels tot und es meldet sich neu an.
    Für den Tablet-Wächter zählen nur Meldungen aus Fully Kiosk (sonst würde z. B. ein PC das Tablet „vertreten“)."""
    mgr = _mgr(hass)
    mgr.waechter.seen[f'{msg["seite"]}|{"fully" if msg["fully"] else "browser"}|{(msg.get("ua") or "")[:120]}'] = dt_util.utcnow().isoformat()
    if msg["fully"] and msg["seite"] in mgr.seiten():
        mgr.waechter.async_alive(msg["seite"], msg["playing"], msg.get("ring"), msg["visible"])
    connection.send_result(msg["id"], {"rev": mgr.rev, "now": dt_util.utcnow().isoformat()})


@websocket_api.websocket_command({vol.Required("type"): "wecker/tablets"})
@callback
def ws_tablets(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Zustand des Tablet-Wächters (letzte Meldung je Bettseite, Notfall, letztes Neuladen, Fully-Entitäten)."""
    w = _mgr(hass).waechter
    out = w.status()
    out["_gesehen"] = dict(w.seen)
    for s, v in out.items():
        if s.startswith("_"):
            continue
        a = w.alive.get(s) or {}
        v.update(playing=a.get("playing"), ring=a.get("ring"), visible=a.get("visible"), entitaeten=w.ents(s))
    connection.send_result(msg["id"], out)
