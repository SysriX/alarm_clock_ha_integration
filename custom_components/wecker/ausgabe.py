"""Ausgabe beim Klingeln: Soundsystem (Lautstärke, Klang, Einschalten) und Vibrationsalarm je Bettseite.

Einstellungen (settings):
  ausgabe.<seite>   = {laut, ein, quelle, quelle_opt, eq{bass,tiefen,mitten,hoehen}, zurueck}
                      laut/eq = number- oder input_number-Entitäten (z. B. Z906 Main/Sub), ein = switch,
                      quelle = select/input_select + gewünschte Option. zurueck = nach dem Wecken alte Werte wiederherstellen.
  vibration.<seite> = {schalter, staerke, muster}   (z. B. ESP32 „Bett Vibration“: switch, number 0–100 %, select)
Wecker (alarm):     vol_min/vol_max/ramp (Lautstärke), eq{…} in % (None = unverändert), vib, vib_muster, vib_staerke, vib_verz_s.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import EQ_BANDS, VIB_MUSTER

_LOGGER = logging.getLogger(__package__)


def _num_domain(ent: str) -> str | None:
    d = ent.split(".", 1)[0] if ent and "." in ent else ""
    return d if d in ("number", "input_number") else None


class Ausgabe:
    """Soundsystem + Vibration einer Bettseite steuern (ohne eigenen Zustand außer den gemerkten Altwerten)."""

    def __init__(self, mgr: Any) -> None:
        self.mgr = mgr
        self.hass: HomeAssistant = mgr.hass
        self.saved: dict[str, dict[str, Any]] = {}     # seite → {entity: alter Zustand}
        self.vib_on: set[str] = set()

    # ------------------------------------------------------------ Einstellungen
    def cfg(self, seite: str | None) -> dict:
        return ((self.mgr.data["settings"].get("ausgabe") or {}).get(seite or "") or {})

    def vcfg(self, seite: str | None) -> dict:
        return ((self.mgr.data["settings"].get("vibration") or {}).get(seite or "") or {})

    # ------------------------------------------------------------ Hilfen
    async def _call(self, domain: str, service: str, data: dict) -> None:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Wecker: %s.%s für %s fehlgeschlagen (%s)", domain, service, data.get("entity_id"), err)

    def _remember(self, seite: str, ent: str) -> None:
        st = self.hass.states.get(ent)
        if st is None:
            return
        self.saved.setdefault(seite, {}).setdefault(ent, st.state)

    async def set_pct(self, ent: str, pct: float) -> None:
        """number/input_number auf pct % ihres Bereichs setzen (Schrittweite beachten)."""
        dom = _num_domain(ent)
        st = self.hass.states.get(ent) if dom else None
        if not st:
            return
        a = st.attributes
        lo, hi, step = float(a.get("min", 0)), float(a.get("max", 100)), float(a.get("step", 1) or 1)
        v = lo + (hi - lo) * max(0.0, min(100.0, pct)) / 100
        v = round(round((v - lo) / step) * step + lo, 4)
        await self._call(dom, "set_value", {"entity_id": ent, "value": v})

    # ------------------------------------------------------------ Klingeln
    async def async_start(self, a: dict, seite: str, vol_pct: float) -> None:
        c = self.cfg(seite)
        if c:
            ein = c.get("ein") or ""
            if ein:
                self._remember(seite, ein)
                dom = ein.split(".", 1)[0]
                if dom in ("switch", "input_boolean", "light", "media_player"):
                    await self._call(dom, "turn_on", {"entity_id": ein})
            q, opt = c.get("quelle") or "", c.get("quelle_opt") or ""
            if q and opt and q.split(".", 1)[0] in ("select", "input_select"):
                self._remember(seite, q)
                await self._call(q.split(".", 1)[0], "select_option", {"entity_id": q, "option": opt})
            laut = c.get("laut") or ""
            if laut:
                self._remember(seite, laut)
                await self.set_pct(laut, vol_pct)
            eq_vals = a.get("eq") or {}
            for band in EQ_BANDS:
                ent, v = (c.get("eq") or {}).get(band) or "", eq_vals.get(band)
                if ent and v is not None:
                    self._remember(seite, ent)
                    await self.set_pct(ent, float(v))
        if a.get("vib"):
            await self.async_vib_on(a, seite)

    async def async_volume(self, seite: str, vol_pct: float) -> None:
        laut = self.cfg(seite).get("laut") or ""
        if laut:
            await self.set_pct(laut, vol_pct)

    async def async_vib_on(self, a: dict, seite: str) -> None:
        v = self.vcfg(seite)
        sw = v.get("schalter") or ""
        if not sw:
            return
        if v.get("staerke"):
            await self.set_pct(v["staerke"], float(a.get("vib_staerke") or 70))
        sel = v.get("muster") or ""
        st = self.hass.states.get(sel) if sel else None
        if st is not None:
            want = str(a.get("vib_muster") or "puls").lower()
            opts = st.attributes.get("options") or []
            opt = next((o for o in opts if str(o).lower() == want), None) or next((o for o in opts if str(o).lower().startswith(want[:4])), None)
            if opt:
                await self._call(sel.split(".", 1)[0], "select_option", {"entity_id": sel, "option": opt})
        await self._call(sw.split(".", 1)[0], "turn_on", {"entity_id": sw})
        self.vib_on.add(seite)

    async def async_vib_off(self, seite: str) -> None:
        sw = self.vcfg(seite).get("schalter") or ""
        if sw and (seite in self.vib_on or (self.hass.states.get(sw) and self.hass.states.get(sw).state == "on")):
            await self._call(sw.split(".", 1)[0], "turn_off", {"entity_id": sw})
        self.vib_on.discard(seite)

    # ------------------------------------------------------------ Schlummern / Aus
    async def async_pause(self, seite: str) -> None:
        """Schlummern: nur Vibration aus (Soundsystem bleibt eingestellt)."""
        await self.async_vib_off(seite)

    async def async_finish(self, seite: str) -> None:
        """Aus: Vibration aus, gemerkte Werte zurück (wenn eingestellt)."""
        await self.async_vib_off(seite)
        old = self.saved.pop(seite, {})
        if not self.cfg(seite).get("zurueck", True):
            return
        for ent, val in old.items():
            dom = ent.split(".", 1)[0]
            try:
                if dom in ("number", "input_number"):
                    await self._call(dom, "set_value", {"entity_id": ent, "value": float(val)})
                elif dom in ("select", "input_select"):
                    await self._call(dom, "select_option", {"entity_id": ent, "option": val})
                elif dom in ("switch", "input_boolean", "light", "media_player") and val in ("on", "off"):
                    await self._call(dom, "turn_on" if val == "on" else "turn_off", {"entity_id": ent})
            except (TypeError, ValueError):
                continue


def clean_alarm_extra(out: dict) -> None:
    """Neue Wecker-Felder (ab 1.3.0) prüfen."""
    out["zufall"] = bool(out.get("zufall"))
    out["vib"] = bool(out.get("vib"))
    m = str(out.get("vib_muster") or "puls").lower()
    out["vib_muster"] = m if m in VIB_MUSTER else "puls"
    out["vib_staerke"] = max(5, min(100, int(out.get("vib_staerke") or 70)))
    out["vib_verz_s"] = max(0, min(600, int(out.get("vib_verz_s") or 0)))
    eq = out.get("eq") if isinstance(out.get("eq"), dict) else {}
    out["eq"] = {b: (None if eq.get(b) in (None, "") else max(0, min(100, int(float(eq[b]))))) for b in EQ_BANDS}


def merge_ausgabe(settings: dict, patch: dict, seiten: dict) -> None:
    au = settings.setdefault("ausgabe", {})
    for seite in seiten:
        sp = patch.get(seite)
        if not isinstance(sp, dict):
            continue
        cur = au.setdefault(seite, default_ausgabe())
        for k in ("laut", "ein", "quelle", "quelle_opt"):
            if k in sp:
                cur[k] = str(sp[k] or "")[:255]
        if "zurueck" in sp:
            cur["zurueck"] = bool(sp["zurueck"])
        if isinstance(sp.get("eq"), dict):
            for b in EQ_BANDS:
                if b in sp["eq"]:
                    cur.setdefault("eq", {})[b] = str(sp["eq"][b] or "")[:255]


def merge_vibration(settings: dict, patch: dict, seiten: dict) -> None:
    vb = settings.setdefault("vibration", {})
    for seite in seiten:
        sp = patch.get(seite)
        if not isinstance(sp, dict):
            continue
        cur = vb.setdefault(seite, default_vibration())
        for k in ("schalter", "staerke", "muster"):
            if k in sp:
                cur[k] = str(sp[k] or "")[:255]


def default_ausgabe() -> dict:
    return {"laut": "", "ein": "", "quelle": "", "quelle_opt": "", "eq": {b: "" for b in EQ_BANDS}, "zurueck": True}


def default_vibration() -> dict:
    return {"schalter": "", "staerke": "", "muster": ""}
