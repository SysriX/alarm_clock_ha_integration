"""Tablet-Wächter: sorgt dafür, dass der Wecker auch weckt, wenn das Dashboard auf dem Tablet hängt.

Das Raum-Panel meldet sich regelmäßig (WS wecker/alive) und sagt beim Klingeln, ob der Ton im Browser
wirklich läuft. Ist für eine Bettseite ein Fully-Kiosk-Tablet eingestellt (seiten.<seite>.tablet =
media_player der Fully-Kiosk-Integration), dann

* lädt der Wächter das Dashboard neu (Fully „Start-URL laden“), wenn sich das Tablet kurz vor einer
  Weckzeit oder sehr lange nicht gemeldet hat,
* weckt er selbst („Notfall-Weckung“), wenn das Panel 20 s nach Klingelbeginn keinen laufenden Ton
  meldet: Bildschirm an + heller, Fully in den Vordergrund, Dashboard neu laden und den Weckton über
  den Fully-Mediaplayer (natives Android, unabhängig vom Browser) in Schleife mit Lautstärke-Anstieg.
  Sobald das Panel wieder selbst spielt, verstummt die Notfall-Weckung.
"""

from __future__ import annotations

import logging
import os
import wave
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import SOUND_DIR, SOUND_URL

if TYPE_CHECKING:
    from .manager import WeckerManager

_LOGGER = logging.getLogger(__package__)

ALIVE_OK_S = 15            # während des Klingelns: so frisch muss die Meldung „Ton läuft“ sein
NOTFALL_NACH_S = 20        # so lange darf das Panel nach Klingelbeginn brauchen
NOTFALL_MAX_S = 60 * 60    # Notfall-Ton höchstens 60 min nach Klingelbeginn
VOR_WECKZEIT_S = 15 * 60   # Prüfung vor der Weckzeit
STUMM_VOR_S = 120          # … Tablet gilt als hängend, wenn es sich so lange nicht gemeldet hat
STUMM_LANG_S = 20 * 60     # sonst: neu laden, wenn so lange keine Meldung
RELOAD_PAUSE_VOR_S = 10 * 60
RELOAD_PAUSE_LANG_S = 60 * 60
HELL_MIN = 60              # Fully-Helligkeit 0–255: darunter beim Notfall auf HELL_NOTFALL
HELL_NOTFALL = 160

# Fully-Kiosk-Entitäten am selben Gerät (Endung der unique_id)
FK = {"player": "-mediaplayer", "reload": "-loadStartUrl", "screen": "-screenOn", "hell": "-screenBrightness", "vorne": "-toForeground"}


def _ton_dauer(path: str) -> float:
    try:
        with wave.open(path, "rb") as w:
            return max(1.0, w.getnframes() / float(w.getframerate() or 1))
    except Exception:  # noqa: BLE001 - kein WAV oder kaputt → Schätzwert
        return 12.0


class TabletWaechter:
    def __init__(self, mgr: "WeckerManager") -> None:
        self.mgr = mgr
        self.hass = mgr.hass
        self.alive: dict[str, dict] = {}        # seite → {at, playing, ring, visible}
        self.reload_at: dict[str, datetime] = {}
        self.nf: dict[str, dict] = {}           # seite → Notfall-Zustand
        self.started = dt_util.utcnow()
        self._loop = None
        self._dauer: dict[str, float] = {}
        self.wach: dict[str, dict] = {}         # seite → {key, hell_alt}: Bildschirm beim Klingeln geweckt
        self.seen: dict[str, str] = {}          # Diagnose: alle Meldungen (seite|fully/browser|ua) → Zeit

    # ---------------------------------------------------------------- Konfiguration
    def tablet(self, seite: str) -> str | None:
        t = (self.mgr.seiten().get(seite) or {}).get("tablet") or ""
        return t if t.startswith("media_player.") else None

    def ents(self, seite: str) -> dict[str, str]:
        """Fully-Entitäten des Tablets dieser Seite (über das Gerät des Mediaplayers)."""
        mp = self.tablet(seite)
        if not mp:
            return {}
        reg = er.async_get(self.hass)
        e = reg.async_get(mp)
        out = {"player": mp}
        if not e or not e.device_id:
            return out
        for x in er.async_entries_for_device(reg, e.device_id):
            if x.disabled_by or x.platform != e.platform:
                continue
            for k, suf in FK.items():
                if str(x.unique_id).endswith(suf):
                    out.setdefault(k, x.entity_id)
        return out

    def _ok(self, ent: str | None) -> bool:
        st = self.hass.states.get(ent) if ent else None
        return bool(st) and st.state not in ("unavailable", "unknown")

    # ---------------------------------------------------------------- Meldungen vom Panel
    @callback
    def async_alive(self, seite: str, playing: bool, ring: str | None, visible: bool) -> None:
        self.alive[seite] = {"at": dt_util.utcnow(), "playing": bool(playing), "ring": ring, "visible": bool(visible)}
        if seite in self.nf:
            self._arm()

    def status(self) -> dict:
        out = {}
        for s in self.mgr.seiten():
            a = self.alive.get(s) or {}
            out[s] = {"tablet": self.tablet(s) or "", "alive": a["at"].isoformat() if a.get("at") else None,
                      "notfall": bool((self.nf.get(s) or {}).get("ton")),
                      "neu_geladen": self.reload_at[s].isoformat() if s in self.reload_at else None}
        return out

    def _seit_meldung(self, seite: str, now: datetime) -> float:
        a = self.alive.get(seite)
        ref = a["at"] if a else self.started
        return (now - ref).total_seconds()

    # ---------------------------------------------------------------- Dienste
    def _call(self, domain: str, service: str, data: dict) -> None:
        async def go() -> None:
            try:
                await self.hass.services.async_call(domain, service, data, blocking=True)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Wecker-Wächter: %s.%s %s fehlgeschlagen (%s)", domain, service, data.get("entity_id"), err)
        self.hass.async_create_task(go())

    def _reload(self, seite: str, grund: str, event: bool = True) -> bool:
        e = self.ents(seite)
        if not self._ok(e.get("reload")):
            return False
        self.reload_at[seite] = dt_util.utcnow()
        _LOGGER.warning("Wecker-Wächter: Dashboard auf Tablet %s neu geladen (%s)", seite, grund)
        if e.get("vorne"):
            self._call("button", "press", {"entity_id": e["vorne"]})
        self._call("button", "press", {"entity_id": e["reload"]})
        if event:
            self.mgr._event("info", {"id": None, "name": "Tablet", "seite": seite}, f"Dashboard neu geladen: {grund}")
        return True

    # ---------------------------------------------------------------- jede Minute (aus dem Manager)
    async def async_tick(self) -> None:
        now = dt_util.utcnow()
        for s in self.mgr.seiten():
            if not self.tablet(s) or s in self.nf:
                continue
            stumm = self._seit_meldung(s, now)
            last = self.reload_at.get(s)
            pause = (now - last).total_seconds() if last else 1e9
            nx = self.mgr.next_for(s)
            bald = False
            if nx:
                at = dt_util.parse_datetime(nx["at"])
                bald = bool(at) and 0 < (at - now).total_seconds() <= VOR_WECKZEIT_S
            if bald and stumm > STUMM_VOR_S and pause > RELOAD_PAUSE_VOR_S:
                self._reload(s, f"vor der Weckzeit {nx['time']} seit {int(stumm // 60)} min keine Meldung")
            elif stumm > STUMM_LANG_S and pause > RELOAD_PAUSE_LANG_S:
                self._reload(s, f"seit {int(stumm // 60)} min keine Meldung", event=False)

    # ---------------------------------------------------------------- Klingeln überwachen
    @callback
    def async_sync(self) -> None:
        """Nach jeder Zustandsänderung des Weckers: Überwachung starten/beenden."""
        ringing = self._ringing()
        for s in list(self.nf):
            if s not in ringing:
                self._nf_end(s)
        if any(self.tablet(s) for s in ringing):
            self._arm()

    def _ringing(self) -> dict[str, tuple[str, dict]]:
        out: dict[str, tuple[str, dict]] = {}
        for aid, r in self.mgr.rings.items():
            if r.get("state") == "ringing" and r.get("seite"):
                out.setdefault(r["seite"], (aid, r))
        return out

    def _arm(self) -> None:
        if not self._loop:
            self._loop = async_track_time_interval(self.hass, self._step, timedelta(seconds=2), name="wecker_waechter")

    def stop(self) -> None:
        if self._loop:
            self._loop()
            self._loop = None
        for s in list(self.nf):
            self._nf_end(s)
        for s in list(self.wach):
            self._schlafen(s)

    @callback
    def _step(self, _now: Any = None) -> None:
        now = dt_util.utcnow()
        ringing = self._ringing()
        active = False
        for s in list(self.wach):
            if s not in ringing:
                self._schlafen(s)
        for s, (aid, r) in ringing.items():
            if not self.tablet(s):
                continue
            since = dt_util.parse_datetime(r.get("since") or "") or now
            age = (now - since).total_seconds()
            key = f"{aid}|{r.get('since')}"
            if (self.wach.get(s) or {}).get("key") != key and age < NOTFALL_MAX_S:
                self._wecken(s, key)
            if (self.mgr.seiten().get(s) or {}).get("browser") is False:
                active = True
                continue
            a = self.alive.get(s) or {}
            spielt = bool(a.get("at")) and (now - a["at"]).total_seconds() <= ALIVE_OK_S and a.get("playing") and a.get("ring") == aid
            nf = self.nf.get(s)
            if age > NOTFALL_MAX_S:
                if nf and nf.get("ton"):
                    self._nf_ton_aus(s)
                continue
            active = True
            if nf is None:
                if age >= NOTFALL_NACH_S and not spielt:
                    self._nf_start(s, aid, r, age)
                continue
            if nf["ring"] != aid or nf["since"] != r.get("since"):     # neues Klingeln (nach Schlummern)
                self._nf_end(s)
                continue
            if spielt:
                if nf.get("ton"):
                    _LOGGER.info("Wecker-Wächter: Panel %s spielt wieder selbst – Notfall-Ton aus", s)
                    self._nf_ton_aus(s)
                nf["ok_seit"] = nf.get("ok_seit") or now
                continue
            nf["ok_seit"] = None
            if not nf.get("ton"):
                nf["ton"] = True
                nf["next"] = now
            if now >= nf["next"]:
                self._nf_play(s, aid, r, age)
            elif now >= nf.get("vol_next", now):
                self._nf_vol(s, aid, age)
        # Notfall-Zustände ohne Klingeln aufräumen
        for s in list(self.nf):
            if s not in ringing:
                self._nf_end(s)
        if not active and not self.nf and not self.wach and self._loop:
            self._loop()
            self._loop = None

    def _vol(self, aid: str, age: float) -> float:
        a = self.mgr.data["alarms"].get(aid) or {}
        lo, hi = int(a.get("vol_min") or 10), int(a.get("vol_max") or 80)
        if not a.get("ramp", True):
            return hi / 100
        f = min(1.0, max(0.0, age / max(1, int(a.get("ramp_s") or 120))))
        return round(lo + (hi - lo) * f) / 100

    def _ton(self, aid: str, r: dict) -> tuple[str | None, float]:
        a = self.mgr.data["alarms"].get(aid) or {"id": aid}
        url = self.mgr.sound_url(a, r)
        if not url:
            return None, 12.0
        if url not in self._dauer:
            f = unquote(url[len(SOUND_URL):]) if url.startswith(SOUND_URL) else ""
            self._dauer[url] = _ton_dauer(os.path.join(self.hass.config.path(SOUND_DIR), f)) if f else 12.0
        return url, self._dauer[url]

    def _nf_start(self, s: str, aid: str, r: dict, age: float) -> None:
        e = self.ents(s)
        a = self.alive.get(s)
        grund = "Panel meldet sich nicht" if not a or (dt_util.utcnow() - a["at"]).total_seconds() > ALIVE_OK_S else "Ton im Browser läuft nicht (Autoplay gesperrt?)"
        _LOGGER.warning("Wecker-Wächter: Notfall-Weckung auf Tablet %s – %s", s, grund)
        self.mgr._event("info", {"id": aid, "name": r.get("name") or "Wecker", "seite": s}, f"Notfall-Weckung über Fully: {grund}")
        nf = {"ring": aid, "since": r.get("since"), "ton": True, "next": dt_util.utcnow() + timedelta(seconds=3), "ok_seit": None}
        self.nf[s] = nf
        # Dashboard neu laden (repariert das Panel meist), der Ton startet 3 s später nativ
        self._reload(s, "Notfall-Weckung", event=False)
        self.mgr._push()

    def _nf_play(self, s: str, aid: str, r: dict, age: float) -> None:
        nf = self.nf[s]
        mp = self.tablet(s)
        url, dauer = self._ton(aid, r)
        nf["next"] = dt_util.utcnow() + timedelta(seconds=dauer + 0.5)
        if not mp or not url or not self._ok(mp):
            return
        self._call("media_player", "volume_set", {"entity_id": mp, "volume_level": self._vol(aid, age)})
        self._call("media_player", "play_media", {"entity_id": mp, "media_content_id": self.mgr._abs_url(url), "media_content_type": "music"})
        nf["vol_next"] = dt_util.utcnow() + timedelta(seconds=5)

    def _nf_vol(self, s: str, aid: str, age: float) -> None:
        nf = self.nf[s]
        nf["vol_next"] = dt_util.utcnow() + timedelta(seconds=5)
        mp = self.tablet(s)
        if mp and self._ok(mp):
            self._call("media_player", "volume_set", {"entity_id": mp, "volume_level": self._vol(aid, age)})

    def _nf_ton_aus(self, s: str) -> None:
        nf = self.nf.get(s)
        if not nf or not nf.get("ton"):
            return
        nf["ton"] = False
        mp = self.tablet(s)
        if mp and self._ok(mp):
            self._call("media_player", "media_stop", {"entity_id": mp})
        self.mgr._push()

    def _nf_end(self, s: str) -> None:
        nf = self.nf.pop(s, None)
        if not nf:
            return
        mp = self.tablet(s)
        if nf.get("ton") and mp and self._ok(mp):
            self._call("media_player", "media_stop", {"entity_id": mp})
        _LOGGER.info("Wecker-Wächter: Notfall-Weckung %s beendet", s)

    # ---------------------------------------------------------------- Bildschirm beim Klingeln
    def _wecken(self, s: str, key: str) -> None:
        """Bildschirm an, Fully nach vorne, nachts dunklen Bildschirm aufhellen (Klingel-Fenster sichtbar)."""
        e = self.ents(s)
        alt = self.wach.get(s) or {}
        w = {"key": key, "hell_alt": alt.get("hell_alt")}
        self.wach[s] = w
        if self._ok(e.get("screen")):
            self._call("switch", "turn_on", {"entity_id": e["screen"]})
        if e.get("vorne") and self._ok(e.get("vorne")):
            self._call("button", "press", {"entity_id": e["vorne"]})
        hs = self.hass.states.get(e.get("hell") or "")
        try:
            hv = float(hs.state) if hs else None
        except ValueError:
            hv = None
        if hv is not None and hv < HELL_MIN:
            if w["hell_alt"] is None:
                w["hell_alt"] = hv
            self._call("number", "set_value", {"entity_id": e["hell"], "value": HELL_NOTFALL})

    def _schlafen(self, s: str) -> None:
        w = self.wach.pop(s, None)
        if not w or w.get("hell_alt") is None:
            return
        e = self.ents(s)
        if self._ok(e.get("hell")):
            self._call("number", "set_value", {"entity_id": e["hell"], "value": w["hell_alt"]})
