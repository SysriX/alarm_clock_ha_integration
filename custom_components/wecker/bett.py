"""Bett: Gewicht + Belegung (frei/sitzt/liegt) je Seite und Steuerung der Heizdecken.

Sicherheit der Heizdecke (läuft im Server, nicht im Tablet):
- Laufzeit immer begrenzt (auch "dauerhaft" höchstens max_dauer_min)
- Übertemperatur (schutz_temp): sofort aus, neues Einschalten gesperrt bis 5 °C darunter
- Matratzentemperatur fehlt/unplausibel: Heizung aus (Fehler "sensor")
- beim Start/Beenden von Home Assistant: Schalter aus, sofern nicht noch eine gültige Laufzeit besteht
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import BETT_ZELLEN, EVENT_HEAT

if TYPE_CHECKING:
    from .manager import WeckerManager

_LOGGER = logging.getLogger(__package__)
SIGNAL_BETT = "wecker_bett_update"
SWITCHLIKE = ("switch", "light", "fan", "input_boolean")
NA = ("unavailable", "unknown", "none", "")


def _num(hass, ent: str | None) -> float | None:
    if not ent:
        return None
    st = hass.states.get(ent)
    if st is None or str(st.state).lower() in NA:
        return None
    try:
        return float(st.state)
    except (TypeError, ValueError):
        return None


class BettController:
    def __init__(self, mgr: WeckerManager) -> None:
        self.mgr = mgr
        self.hass = mgr.hass
        self.live: dict[str, dict] = {}          # seite → {gewicht, roh, belegung, zellen, temperatur}
        self.heat: dict[str, dict] = {}          # seite → {on, target, until, started, dauerhaft, relay, fault, lock}
        self._track: Callable[[], None] | None = None
        self._timers: dict[str, Callable[[], None]] = {}
        self._tick: Callable[[], None] | None = None

    # ---------------------------------------------------------------- Konfiguration
    def cfg(self) -> dict:
        return self.mgr.data["settings"].get("bett") or {}

    def side(self, seite: str) -> dict:
        return self.cfg().get(seite) or {}

    def seiten(self) -> list[str]:
        return list(self.mgr.seiten())

    def entities(self) -> set[str]:
        out: set[str] = set()
        for s in self.seiten():
            c = self.side(s)
            out |= {c.get("gesamt"), c.get("heizung"), c.get("temperatur"), *(c.get("zellen") or {}).values()}
        return {e for e in out if e}

    # ---------------------------------------------------------------- Lebenszyklus
    async def async_start(self, cache: dict) -> None:
        self.heat = cache or {}
        self.retrack()
        self._tick = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=30), name="wecker_bett")
        now = dt_util.utcnow()
        for s in self.seiten():
            h = self.heat.get(s)
            until = dt_util.parse_datetime(h["until"]) if h and h.get("until") else None
            if h and h.get("on") and until and until > now:
                self._arm(s)                                 # nach Neustart weiterlaufen lassen
                await self._regulate(s)
            else:
                if h and h.get("on"):
                    self._log(s, "heat_off", "Laufzeit während HA aus war abgelaufen")
                self.heat[s] = {**(h or {}), "on": False, "relay": False}
                await self._relay(s, False, force=True)      # sicherer Zustand
        self.recalc()

    async def async_stop(self) -> None:
        if self._track:
            self._track()
        if self._tick:
            self._tick()
        for u in self._timers.values():
            u()
        # beim Beenden Heizdecken aus (Laufzeit bleibt gespeichert und wird beim Start fortgesetzt)
        for s in self.seiten():
            if (self.heat.get(s) or {}).get("relay"):
                await self._relay(s, False, force=True)

    def retrack(self) -> None:
        if self._track:
            self._track()
            self._track = None
        ents = sorted(self.entities())
        if ents:
            self._track = async_track_state_change_event(self.hass, ents, self._changed)
        self.recalc()

    @callback
    def _changed(self, event: Event) -> None:
        ent = event.data.get("entity_id")
        self.recalc()
        for s in self.seiten():
            if ent == self.side(s).get("temperatur") and (self.heat.get(s) or {}).get("on"):
                self.hass.async_create_task(self._regulate(s))
            elif ent == self.side(s).get("temperatur") and (self.heat.get(s) or {}).get("lock"):
                self._unlock_check(s)

    async def _async_tick(self, _now: Any = None) -> None:
        for s in self.seiten():
            if (self.heat.get(s) or {}).get("on"):
                await self._regulate(s)
            elif (self.heat.get(s) or {}).get("lock"):
                self._unlock_check(s)

    # ---------------------------------------------------------------- Gewicht + Belegung
    @callback
    def recalc(self) -> None:
        cfg = self.cfg()
        belegt = float(cfg.get("belegt_kg") or 20)
        changed = False
        for s in self.seiten():
            c = self.side(s)
            z = {k: _num(self.hass, (c.get("zellen") or {}).get(k)) for k in BETT_ZELLEN if (c.get("zellen") or {}).get(k)}
            vals = [v for v in z.values() if v is not None]
            if z and len(vals) == len(z):
                roh = sum(vals)
            elif z:
                roh = None                                   # Zelle fehlt → kein Wert statt falscher Summe
            else:
                roh = _num(self.hass, c.get("gesamt"))
            kg = None if roh is None else round(max(0.0, roh - float(c.get("tara") or 0)), 1)
            if kg is None:
                bel = "unbekannt"
            elif kg < belegt:
                bel = "frei"
            elif len(z) == 4 and kg > 0:
                # Haltung aus der Verteilung: liegend = Gewicht auf Kopf- und Fußende verteilt,
                # sitzend = fast alles an einem Ende (Kopfteil) oder an der Außenkante (Bettkante)
                kopf = (z["kopf_aussen"] + z["kopf_innen"]) / roh if roh else 0
                aussen = (z["kopf_aussen"] + z["fuss_aussen"]) / roh if roh else 0
                bel = "liegt" if 0.25 <= kopf <= 0.75 and 0.15 <= aussen <= 0.85 else "sitzt"
            else:
                bel = "belegt"
            new = {"gewicht": kg, "roh": None if roh is None else round(roh, 2), "belegung": bel,
                   "zellen": {k: (None if v is None else round(v, 2)) for k, v in z.items()}, "temperatur": _num(self.hass, c.get("temperatur"))}
            if self.live.get(s) != new:
                self.live[s] = new
                changed = True
        if changed:
            async_dispatcher_send(self.hass, SIGNAL_BETT)

    async def async_tara(self, seite: str) -> float:
        c = self.side(seite)
        self.recalc()
        roh = (self.live.get(seite) or {}).get("roh")
        if roh is None:
            raise ValueError("Kein Gewichtswert – Sensoren prüfen")
        c["tara"] = round(roh, 2)
        self.mgr.dirty_settings = True
        self.recalc()
        await self.mgr._async_persist()
        self.mgr._push()
        return c["tara"]

    # ---------------------------------------------------------------- Heizdecke
    def status(self, s: str) -> str:
        h = self.heat.get(s) or {}
        if h.get("fault") == "schutz" or h.get("lock"):
            return "schutz"
        if h.get("fault") == "sensor":
            return "fehler"
        if not h.get("on"):
            return "aus"
        return "heizt" if h.get("relay") else "haelt"

    async def async_heat_on(self, seite: str, target: float | None = None, dauer_min: float | None = None, dauerhaft: bool = False) -> None:
        cfg, c = self.cfg(), self.side(seite)
        if seite not in self.seiten():
            raise ValueError(f"Unbekannte Bettseite: {seite}")
        if not c.get("heizung"):
            raise ValueError("Keine Heizdecke eingestellt (Einstellungen → Betttemperatur)")
        h = self.heat.get(seite) or {}
        if h.get("lock"):
            raise ValueError(f"Übertemperatur-Schutz aktiv – erst unter {float(cfg.get('schutz_temp') or 45) - 5:.0f} °C wieder möglich")
        dom = c["heizung"].split(".")[0]
        if dom not in ("climate",) and not c.get("temperatur"):
            raise ValueError("Ohne Matratzentemperatur kann die Heizdecke nicht geregelt werden")
        mx = float(cfg.get("max_temp") or 38)
        t = max(10.0, min(mx, float(target if target is not None else cfg.get("standard_temp") or 30)))
        mxd = int(cfg.get("max_dauer_min") or 600)
        # dauerhaft = bis zur Höchstdauer (im Popup wählbar, nie über max_dauer_min)
        if dauerhaft:
            mins = max(1, min(mxd, int(dauer_min))) if dauer_min else mxd
        else:
            mins = max(1, min(mxd, int(dauer_min or cfg.get("standard_dauer_min") or 60)))
        now = dt_util.utcnow()
        keep_start = h.get("on") and h.get("started")
        self.heat[seite] = {
            "on": True, "target": t, "dauerhaft": bool(dauerhaft), "relay": bool(h.get("relay")), "fault": None, "lock": False,
            "started": h["started"] if keep_start else now.isoformat(), "until": (now + timedelta(minutes=mins)).isoformat(),
        }
        self._arm(seite)
        self._log(seite, "heat_on", f"Ziel {t:.1f} °C, {'dauerhaft (max. ' + str(mins) + ' min)' if dauerhaft else str(mins) + ' min'}")
        await self._regulate(seite)
        await self._changed_state(seite)

    async def async_heat_off(self, seite: str, reason: str = "manuell") -> None:
        h = self.heat.get(seite) or {}
        was = h.get("on")
        self.heat[seite] = {**h, "on": False, "until": None}
        if seite in self._timers:
            self._timers.pop(seite)()
        await self._relay(seite, False, force=True)
        if was:
            self._log(seite, "heat_off", reason)
        await self._changed_state(seite)

    def _arm(self, s: str) -> None:
        if s in self._timers:
            self._timers.pop(s)()
        until = dt_util.parse_datetime((self.heat.get(s) or {}).get("until") or "")
        if until:
            async def done(_now: Any) -> None:
                self._timers.pop(s, None)
                await self.async_heat_off(s, "Laufzeit beendet")
            self._timers[s] = async_track_point_in_utc_time(self.hass, done, until)

    async def _regulate(self, s: str) -> None:
        h, cfg, c = self.heat.get(s) or {}, self.cfg(), self.side(s)
        if not h.get("on"):
            return
        until = dt_util.parse_datetime(h.get("until") or "")
        if not until or until <= dt_util.utcnow():
            await self.async_heat_off(s, "Laufzeit beendet")
            return
        ent = c.get("heizung") or ""
        temp = _num(self.hass, c.get("temperatur"))
        if temp is None and ent.startswith("climate."):
            st = self.hass.states.get(ent)
            temp = st.attributes.get("current_temperature") if st else None
        schutz = float(cfg.get("schutz_temp") or 45)
        if temp is not None and temp >= schutz:
            self.heat[s] = {**h, "lock": True, "fault": "schutz"}
            self._log(s, "protect", f"Übertemperatur {temp:.1f} °C ≥ {schutz:.0f} °C – Heizdecke aus")
            await self.async_heat_off(s, f"Übertemperatur {temp:.1f} °C")
            self.heat[s]["lock"], self.heat[s]["fault"] = True, "schutz"
            await self._changed_state(s)
            return
        if temp is None or temp < -20 or temp > 90:
            self.heat[s] = {**h, "fault": "sensor"}
            await self.async_heat_off(s, "Matratzentemperatur nicht verfügbar")
            self.heat[s]["fault"] = "sensor"
            await self._changed_state(s)
            return
        if ent.startswith("climate."):
            await self._relay(s, True, target=h["target"])
            return
        hy = float(cfg.get("hysterese") or 1.0) / 2
        if temp < h["target"] - hy and not h.get("relay"):
            await self._relay(s, True)
        elif temp >= h["target"] + hy and h.get("relay"):
            await self._relay(s, False)
        elif h.get("relay"):
            await self._relay(s, True)                       # Zustand des Schalters immer wieder bestätigen

    def _unlock_check(self, s: str) -> None:
        h, cfg = self.heat.get(s) or {}, self.cfg()
        temp = _num(self.hass, self.side(s).get("temperatur"))
        if temp is not None and temp < float(cfg.get("schutz_temp") or 45) - 5:
            self.heat[s] = {**h, "lock": False, "fault": None}
            self._log(s, "info", f"Übertemperatur-Schutz aufgehoben ({temp:.1f} °C)")
            self.hass.async_create_task(self._changed_state(s))

    async def _relay(self, s: str, on: bool, target: float | None = None, force: bool = False) -> None:
        ent = self.side(s).get("heizung") or ""
        h = self.heat.setdefault(s, {})
        if not ent:
            h["relay"] = False
            return
        dom = ent.split(".")[0]
        try:
            if dom == "climate":
                if on:
                    await self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": ent, "hvac_mode": "heat"}, blocking=True)
                    await self.hass.services.async_call("climate", "set_temperature", {"entity_id": ent, "temperature": target}, blocking=True)
                else:
                    await self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": ent, "hvac_mode": "off"}, blocking=True)
            elif dom in SWITCHLIKE:
                st = self.hass.states.get(ent)
                if force or st is None or (st.state == "on") != on:
                    await self.hass.services.async_call(dom, "turn_on" if on else "turn_off", {"entity_id": ent}, blocking=True)
            else:
                _LOGGER.warning("Wecker/Bett: %s kann nicht geschaltet werden (Bereich %s)", ent, dom)
                return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Wecker/Bett: Heizdecke %s schalten fehlgeschlagen (%s)", ent, err)
            return
        if h.get("relay") != on:
            h["relay"] = on
            await self._changed_state(s)

    async def _changed_state(self, s: str) -> None:
        h = self.heat.get(s) or {}
        self.hass.bus.async_fire(EVENT_HEAT, {"seite": s, "status": self.status(s), "ziel": h.get("target"), "bis": h.get("until")})
        await self.mgr._async_persist()
        self.mgr._push()
        async_dispatcher_send(self.hass, SIGNAL_BETT)

    def _log(self, s: str, kind: str, detail: str) -> None:
        self.mgr._event(kind, {"id": None, "name": "Heizdecke", "seite": s}, detail)

    def snapshot(self) -> dict:
        out = {}
        for s in self.seiten():
            h = self.heat.get(s) or {}
            out[s] = {**{k: h.get(k) for k in ("on", "target", "until", "started", "dauerhaft", "relay", "fault", "lock")}, "status": self.status(s)}
        return out
