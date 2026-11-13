"""Wecker: Zustand, Zeitplanung, Klingeln/Schlummern/Aus, Datenbank-Abgleich."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import random
from urllib.parse import quote
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time, async_track_time_interval
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CACHE_STORE_KEY,
    CACHE_STORE_VERSION,
    CATCH_UP_S,
    CONF_DB_URL,
    CONF_SEITEN,
    DEFAULT_ALARM,
    DEFAULT_SETTINGS,
    EVENT_RING,
    EVENT_SNOOZE,
    EVENT_STOP,
    SOUND_DIR,
    SOUND_EXT,
    SOUND_URL,
    SYNC_INTERVAL_S,
    VERSION,
)
from .ausgabe import Ausgabe, clean_alarm_extra, default_ausgabe, default_vibration, merge_ausgabe, merge_vibration
from .bett import BettController
from .const import BETT_ZELLEN, DEFAULT_BETT, DEFAULT_BETT_SEITE, DEFAULT_HINTERGRUND, HINTERGRUND_ARTEN, SCHRIFTEN
from .db import WeckerDB
from .waechter import TabletWaechter

_LOGGER = logging.getLogger(__package__)
SIGNAL_UPDATE = "wecker_update"
LOCKS = ("rechnen", "halten", "keine")


def _deep_defaults(dst: dict, src: dict) -> bool:
    """Fehlende Schlüssel (auch verschachtelt) aus src ergänzen; True = etwas ergänzt."""
    changed = False
    for k, v in src.items():
        if k not in dst:
            dst[k] = copy.deepcopy(v)
            changed = True
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            changed = _deep_defaults(dst[k], v) or changed
    return changed


def _f(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def empty_data() -> dict:
    return {"settings": {}, "alarms": {}}


def _iso(dt: datetime | None) -> str | None:
    return dt_util.as_utc(dt).isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return dt_util.parse_datetime(s) if s else None


def _hm(t: str) -> tuple[int, int]:
    try:
        h, m = str(t).split(":")[:2]
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except (TypeError, ValueError):
        return 7, 0


def next_occurrence(a: dict, after: datetime) -> datetime | None:
    """Nächste Weckzeit (lokal) strikt nach `after`; None = aus."""
    if not a.get("enabled"):
        return None
    h, m = _hm(a.get("time"))
    days = {int(d) for d in a.get("days") or []}
    skip = a.get("skip_date")
    loc = dt_util.as_local(after)
    for i in range(0, 9):
        d: date = loc.date() + timedelta(days=i)
        if days and d.weekday() not in days:
            continue
        if skip and d.isoformat() == skip:
            continue
        cand = dt_util.as_local(datetime(d.year, d.month, d.day, h, m, tzinfo=dt_util.DEFAULT_TIME_ZONE))
        if cand > loc:
            return cand
    return None


class WeckerManager:
    def __init__(self, hass: HomeAssistant, conf: dict) -> None:
        self.hass = hass
        self.conf = conf
        self.db = WeckerDB(conf[CONF_DB_URL])
        self.store: Store = Store(hass, CACHE_STORE_VERSION, CACHE_STORE_KEY)
        self.data: dict = empty_data()
        self.rings: dict[str, dict] = {}       # alarm_id → {state, started, since, until, count, due}
        self.next_due: dict[str, str] = {}     # alarm_id → ISO (UTC) der nächsten Weckzeit
        self.sounds: list[dict] = []
        self.db_ok = False
        self.db_error: str | None = None
        self.schema: int | None = None
        self.dirty_all = False
        self.dirty_alarms: set[str] = set()
        self.deleted_alarms: set[str] = set()
        self.dirty_settings = False
        self.pending_events: list[dict] = []
        self.recent_events: list[dict] = []
        self._warned = False
        self._lock = asyncio.Lock()
        self._listeners: set[Callable[[dict], None]] = set()
        self._unsubs: list[Callable[[], None]] = []
        self._timer: Callable[[], None] | None = None
        self._ramp_unsub: Callable[[], None] | None = None
        self.bett = BettController(self)
        self._bett_started = False
        self.ausgabe = Ausgabe(self)                 # Soundsystem + Vibration beim Klingeln
        self._last_sound: dict[str, str] = {}        # seite → zuletzt zufällig gewählter Ton
        self.rev = 0                                 # Zähler je Zustandsänderung (Panel prüft damit sein Abo)
        self.waechter = TabletWaechter(self)         # Tablet hängt? → neu laden / Notfall-Weckung über Fully

    # ================================================================ Lebenszyklus
    async def async_start(self) -> None:
        cache = await self.store.async_load() or {}
        self.data = {**empty_data(), **(cache.get("data") or {})}
        self.rings = cache.get("rings") or {}
        self.next_due = cache.get("next_due") or {}
        self.dirty_all = bool(cache.get("dirty_all"))
        self.dirty_alarms = set(cache.get("dirty_alarms") or [])
        self.deleted_alarms = set(cache.get("deleted_alarms") or [])
        self.dirty_settings = bool(cache.get("dirty_settings"))
        self.pending_events = cache.get("events") or []
        self.recent_events = cache.get("recent") or []
        await self._async_connect()
        self._ensure_defaults()
        await self.async_scan_sounds()
        await self.bett.async_start(cache.get("heat") or {})
        self._bett_started = True
        self._unsubs.append(async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=SYNC_INTERVAL_S), name="wecker"))
        # listen_once meldet sich nach dem Auslösen selbst ab – nicht in _unsubs (sonst Fehler beim Beenden)
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_stop)
        # verpasste Weckzeiten (HA war aus) nachholen, Rest planen
        await self._async_process()

    async def _async_stop(self, _event: Event | None = None) -> None:
        for u in self._unsubs:
            try:
                u()
            except Exception:  # noqa: BLE001
                pass
        self._unsubs.clear()
        if self._timer:
            self._timer()
        if self._ramp_unsub:
            self._ramp_unsub()
        self.waechter.stop()
        await self.bett.async_stop()
        await self._async_save_cache()
        await self.hass.async_add_executor_job(self.db.close)

    def _ensure_defaults(self) -> None:
        s = self.data["settings"]
        if _deep_defaults(s, DEFAULT_SETTINGS):
            self.dirty_settings = True
        # Bettseiten aus configuration.yaml ergänzen (wecker: seiten: [links, rechts])
        for key in self.conf.get(CONF_SEITEN) or []:
            if key not in s["seiten"]:
                s["seiten"][key] = {"name": key.capitalize(), "browser": True, "player": "", "tablet": ""}
                self.dirty_settings = True
            if key not in s["bett"]:
                s["bett"][key] = copy.deepcopy(DEFAULT_BETT_SEITE)
                self.dirty_settings = True
            if key not in s["hintergrund"]:
                s["hintergrund"][key] = dict(DEFAULT_HINTERGRUND)
                self.dirty_settings = True
            if key not in s["ausgabe"]:
                s["ausgabe"][key] = default_ausgabe()
                self.dirty_settings = True
            if key not in s["vibration"]:
                s["vibration"][key] = default_vibration()
                self.dirty_settings = True

    def seiten(self) -> dict:
        return self.data["settings"].get("seiten") or {}

    # ================================================================ Datenbank
    def _connect_sync(self) -> tuple[int, bool]:
        schema = self.db.connect()
        return schema, self.db.is_initialized()

    async def _async_connect(self) -> bool:
        try:
            self.schema, initialized = await self.hass.async_add_executor_job(self._connect_sync)
        except Exception as err:  # noqa: BLE001
            self._set_offline(err)
            return False
        try:
            offline_changes = self.dirty_all or self.dirty_alarms or self.deleted_alarms or self.dirty_settings
            if initialized and not offline_changes:
                loaded = await self.hass.async_add_executor_job(self.db.load_all)
                self.data = {**empty_data(), **loaded}
                self._ensure_defaults()
                if self._bett_started:
                    self.bett.retrack()
            elif not initialized:
                # DB leer → lokaler Stand ist führend
                self.dirty_all = True
            self.db_ok, self.db_error, self._warned = True, None, False
            _LOGGER.info("Wecker: Datenbank verbunden (Schema %s)", self.schema)
            await self._async_flush()
        except Exception as err:  # noqa: BLE001
            self._set_offline(err)
            return False
        self._reschedule()
        self._push()
        return True

    def _set_offline(self, err: Exception) -> None:
        self.db_ok = False
        self.db_error = f"{type(err).__name__}: {err}".splitlines()[0][:300]
        if not self._warned:
            _LOGGER.warning("Wecker: Datenbank nicht erreichbar, arbeite mit lokaler Kopie (%s)", self.db_error)
            self._warned = True

    async def _async_flush(self) -> None:
        if self.db.engine is None:
            return
        job = self.hass.async_add_executor_job
        if self.dirty_all:
            await job(self.db.save_all, self.data)
            self.dirty_all, self.dirty_settings = False, False
            self.dirty_alarms.clear()
            self.deleted_alarms.clear()
        if self.dirty_settings:
            await job(self.db.save_settings, self.data["settings"])
            self.dirty_settings = False
        for aid in list(self.deleted_alarms):
            await job(self.db.delete_alarm, aid)
            self.deleted_alarms.discard(aid)
        for aid in list(self.dirty_alarms):
            a = self.data["alarms"].get(aid)
            if a:
                await job(self.db.save_alarm, a)
            self.dirty_alarms.discard(aid)
        if self.pending_events:
            ev = list(self.pending_events)
            await job(self.db.insert_events, ev)
            self.pending_events = self.pending_events[len(ev):]

    async def _async_persist(self) -> None:
        if self.db_ok:
            try:
                await self._async_flush()
            except Exception as err:  # noqa: BLE001
                self._set_offline(err)
        await self._async_save_cache()

    async def _async_save_cache(self) -> None:
        await self.store.async_save({
            "data": self.data, "rings": self.rings, "next_due": self.next_due,
            "dirty_all": self.dirty_all, "dirty_alarms": sorted(self.dirty_alarms),
            "deleted_alarms": sorted(self.deleted_alarms), "dirty_settings": self.dirty_settings,
            "events": self.pending_events[-2000:], "recent": self.recent_events[-100:],
            "heat": self.bett.heat,
        })

    async def _async_tick(self, _now: Any = None) -> None:
        if not self.db_ok:
            await self._async_connect()
        elif self.pending_events:
            await self._async_persist()
        await self._async_process()
        try:
            await self.waechter.async_tick()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Wecker: Fehler im Tablet-Wächter")

    # ================================================================ Wecktöne
    def _scan_sync(self) -> list[dict]:
        d = self.hass.config.path(SOUND_DIR)
        os.makedirs(d, exist_ok=True)
        out = []
        for f in sorted(os.listdir(d), key=str.lower):
            if f.lower().endswith(SOUND_EXT) and not f.startswith("_"):
                out.append({"file": f, "name": os.path.splitext(f)[0].replace("_", " "), "url": SOUND_URL + quote(f)})
        return out

    async def async_scan_sounds(self) -> list[dict]:
        try:
            self.sounds = await self.hass.async_add_executor_job(self._scan_sync)
        except OSError as err:
            _LOGGER.warning("Wecker: Ordner der Wecktöne nicht lesbar (%s)", err)
        return self.sounds

    def sound_url(self, a: dict, r: dict | None = None) -> str | None:
        if r and r.get("sound_url"):      # beim Klingeln gewählt (z. B. zufällig) – bleibt bis zum Aus gleich
            return r["sound_url"]
        files = {s["file"]: s["url"] for s in self.sounds}
        if a.get("sound") in files:
            return files[a["sound"]]
        return self.sounds[0]["url"] if self.sounds else None

    # ================================================================ Zeitplanung
    def _reschedule(self) -> None:
        """Nächste Weckzeiten neu berechnen (bestehende, noch nicht fällige bleiben) und Timer stellen."""
        now = dt_util.now()
        for aid, a in self.data["alarms"].items():
            cur = _dt(self.next_due.get(aid))
            nd = next_occurrence(a, now)
            # eine gespeicherte Zeit in der Vergangenheit bleibt stehen, bis _async_process sie abarbeitet
            if cur and cur <= now and a.get("enabled"):
                continue
            self.next_due[aid] = _iso(nd) if nd else None
        for aid in list(self.next_due):
            if aid not in self.data["alarms"]:
                self.next_due.pop(aid, None)
        self._arm_timer()

    def _arm_timer(self) -> None:
        if self._timer:
            self._timer()
            self._timer = None
        pts: list[datetime] = [d for d in (_dt(v) for v in self.next_due.values()) if d]
        for r in self.rings.values():
            if r["state"] == "snoozed" and r.get("until"):
                pts.append(_dt(r["until"]))
            elif r["state"] == "ringing" and r.get("stop_at"):
                pts.append(_dt(r["stop_at"]))
        pts = [p for p in pts if p]
        if not pts:
            return
        at = max(min(pts), dt_util.utcnow() + timedelta(milliseconds=200))
        self._timer = async_track_point_in_utc_time(self.hass, self._on_timer, dt_util.as_utc(at))

    async def _on_timer(self, _now: datetime) -> None:
        self._timer = None
        await self._async_process()

    async def _async_process(self) -> None:
        """Fällige Wecker auslösen, Schlummern beenden, automatisch abschalten."""
        async with self._lock:
            now = dt_util.utcnow()
            changed = False
            for aid, iso in list(self.next_due.items()):
                due = _dt(iso)
                a = self.data["alarms"].get(aid)
                if not due or not a or due > now:
                    continue
                late = (now - due).total_seconds()
                self.next_due[aid] = None
                if not a.get("enabled"):
                    continue
                if late <= CATCH_UP_S:
                    self._ring(a, due, late)
                else:
                    self._event("missed", a, f"Weckzeit {dt_util.as_local(due):%d.%m. %H:%M} verpasst (HA war aus)")
                    if not a.get("days"):
                        a["enabled"] = False
                        self.dirty_alarms.add(aid)
                changed = True
            for aid, r in list(self.rings.items()):
                a = self.data["alarms"].get(aid) or {"id": aid, "name": r.get("name"), "seite": r.get("seite")}
                if r["state"] == "snoozed" and _dt(r.get("until")) and _dt(r["until"]) <= now:
                    r.update(state="ringing", since=_iso(now), until=None)
                    r["stop_at"] = self._stop_at(a, now)
                    self._event("ring", a, f"Schlummern vorbei ({r['count']}×)")
                    self._fire(EVENT_RING, a, r)
                    self._player_start(a, r)
                    changed = True
                elif r["state"] == "ringing" and _dt(r.get("stop_at")) and _dt(r["stop_at"]) <= now:
                    self._finish(aid, "auto_stop", "Nach Ablauf automatisch beendet")
                    changed = True
            # abgelaufene "einmal auslassen"-Tage aufräumen
            today = dt_util.now().date().isoformat()
            for aid, a in self.data["alarms"].items():
                if a.get("skip_date") and a["skip_date"] < today:
                    a["skip_date"] = None
                    self.dirty_alarms.add(aid)
                    changed = True
            self._reschedule()
        if changed:
            await self._async_persist()
            self._push()

    def _stop_at(self, a: dict, start: datetime) -> str | None:
        m = int(a.get("auto_stop_min") or 0)
        return _iso(start + timedelta(minutes=m)) if m > 0 else None

    # ================================================================ Klingeln / Schlummern / Aus
    def _ring(self, a: dict, due: datetime, late: float = 0, test: bool = False) -> None:
        now = dt_util.utcnow()
        r = {
            "state": "ringing", "started": _iso(now), "since": _iso(now), "until": None, "count": 0,
            "due": _iso(due), "name": a.get("name"), "seite": a.get("seite"), "test": test,
        }
        r["stop_at"] = self._stop_at(a, now)
        if a.get("zufall") and self.sounds:
            # zufälliger Ton, möglichst nicht derselbe wie beim letzten Mal auf dieser Seite
            pool = [x["url"] for x in self.sounds if x["url"] != self._last_sound.get(a.get("seite") or "")] or [x["url"] for x in self.sounds]
            r["sound_url"] = random.choice(pool)
            self._last_sound[a.get("seite") or ""] = r["sound_url"]
        self.rings[a["id"]] = r
        detail = "Test" if test else (f"{int(late // 60)} min verspätet nachgeholt" if late > 90 else None)
        self._event("test" if test else "ring", a, detail)
        if not a.get("days") and not test:   # einmaliger Wecker: nach dem Auslösen aus
            a["enabled"] = False
            self.dirty_alarms.add(a["id"])
        self._fire(EVENT_RING, a, r)
        self._player_start(a, r)

    def _fire(self, ev: str, a: dict, r: dict | None = None) -> None:
        self.hass.bus.async_fire(ev, {
            "alarm_id": a.get("id"), "name": a.get("name"), "seite": a.get("seite"), "time": a.get("time"),
            "sound": self.sound_url(a, r), "snooze_count": (r or {}).get("count", 0), "test": bool((r or {}).get("test")),
        })

    def _targets(self, alarm_id: str | None, seite: str | None) -> list[str]:
        if alarm_id:
            return [alarm_id] if alarm_id in self.rings else []
        return [aid for aid, r in self.rings.items() if not seite or r.get("seite") == seite]

    async def async_snooze(self, alarm_id: str | None = None, seite: str | None = None) -> int:
        n = 0
        async with self._lock:
            now = dt_util.utcnow()
            for aid in self._targets(alarm_id, seite):
                r, a = self.rings[aid], self.data["alarms"].get(aid) or {}
                if r["state"] != "ringing":
                    continue
                if not a.get("snooze", True):
                    continue
                mx = int(a.get("snooze_max") or 0)
                if mx and r["count"] >= mx:
                    continue
                r["count"] += 1
                r.update(state="snoozed", until=_iso(now + timedelta(minutes=int(a.get("snooze_min") or 9))), stop_at=None)
                self._event("snooze", {**a, "id": aid, "name": r.get("name"), "seite": r.get("seite")}, f"{r['count']}. Mal, {a.get('snooze_min') or 9} min")
                self._fire(EVENT_SNOOZE, {**a, "id": aid}, r)
                self._player_stop(r.get("seite"))
                self.hass.async_create_task(self.ausgabe.async_pause(r.get("seite") or ""))
                n += 1
            self._arm_timer()
        await self._async_persist()
        self._push()
        return n

    async def async_stop(self, alarm_id: str | None = None, seite: str | None = None) -> int:
        async with self._lock:
            ids = self._targets(alarm_id, seite)
            for aid in ids:
                self._finish(aid, "stop", None)
            self._reschedule()
        await self._async_persist()
        self._push()
        return len(ids)

    def _finish(self, aid: str, kind: str, detail: str | None) -> None:
        r = self.rings.pop(aid, None)
        if not r:
            return
        a = self.data["alarms"].get(aid) or {}
        mins = (dt_util.utcnow() - _dt(r["started"])).total_seconds() / 60 if r.get("started") else 0
        info = detail or f"nach {mins:.0f} min, {r['count']}× geschlummert"
        self._event(kind, {**a, "id": aid, "name": r.get("name"), "seite": r.get("seite")}, info)
        self._fire(EVENT_STOP, {**a, "id": aid, "seite": r.get("seite")}, r)
        self._player_stop(r.get("seite"))
        if not any(x.get("seite") == r.get("seite") for x in self.rings.values()):
            self.hass.async_create_task(self.ausgabe.async_finish(r.get("seite") or ""))

    async def async_test(self, alarm_id: str) -> None:
        a = self.data["alarms"].get(alarm_id)
        if not a:
            raise ValueError("Wecker nicht gefunden")
        async with self._lock:
            self._ring(a, dt_util.utcnow(), test=True)
            self._arm_timer()
        await self._async_persist()
        self._push()

    async def async_vib_test(self, seite: str, muster: str, staerke: int, dauer_s: int) -> dict:
        if seite not in self.seiten():
            raise ValueError(f"Unbekannte Bettseite: {seite}")
        if not self.ausgabe.vcfg(seite).get("schalter"):
            raise ValueError("Kein Vibrationsgerät eingestellt")
        await self.ausgabe.async_vib_on({"vib_muster": muster, "vib_staerke": staerke}, seite)

        def off(_now: Any = None) -> None:
            if not any(r.get("seite") == seite and r.get("state") == "ringing" for r in self.rings.values()):
                self.hass.async_create_task(self.ausgabe.async_vib_off(seite))

        async_call_later(self.hass, dauer_s, off)
        return {"ok": True}

    # ================================================================ Ausgabe über media_player (optional je Bettseite)
    def _player(self, seite: str | None) -> str | None:
        p = (self.seiten().get(seite or "") or {}).get("player")
        return p if p and p.startswith("media_player.") else None

    def _abs_url(self, url: str) -> str:
        try:
            return get_url(self.hass, prefer_external=False).rstrip("/") + url
        except NoURLAvailableError:
            return url

    def _player_start(self, a: dict, r: dict) -> None:
        seite = a.get("seite") or ""
        ent, url = self._player(seite), self.sound_url(a, r)
        vol = (a.get("vol_min") if a.get("ramp") else a.get("vol_max")) or 50
        # Soundsystem (Einschalten, Quelle, Lautstärke-/Klangregler) – unabhängig vom media_player
        self.hass.async_create_task(self.ausgabe.async_start({**a, "vib": False}, seite, vol))
        if self.ausgabe.cfg(seite).get("laut"):
            self._arm_ramp()
        if a.get("vib"):
            delay = int(a.get("vib_verz_s") or 0)
            aid = a.get("id")

            def vib_go(_now: Any = None) -> None:
                rr = self.rings.get(aid)
                if rr and rr.get("state") == "ringing":
                    self.hass.async_create_task(self.ausgabe.async_vib_on(a, seite))

            if delay > 0:
                async_call_later(self.hass, delay, vib_go)
            else:
                vib_go()
        if not ent or not url:
            return

        async def go() -> None:
            try:
                await self.hass.services.async_call("media_player", "volume_set", {"entity_id": ent, "volume_level": vol / 100}, blocking=True)
                await self.hass.services.async_call("media_player", "play_media", {
                    "entity_id": ent, "media_content_id": self._abs_url(url), "media_content_type": "music"}, blocking=True)
                try:
                    await self.hass.services.async_call("media_player", "repeat_set", {"entity_id": ent, "repeat": "one"}, blocking=True)
                except Exception:  # noqa: BLE001 - nicht jeder Player kann Wiederholen
                    pass
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Wecker: Abspielen auf %s fehlgeschlagen (%s)", ent, err)

        self.hass.async_create_task(go())
        self._arm_ramp()

    def _arm_ramp(self) -> None:
        if self._ramp_unsub:
            return
        self._ramp_unsub = async_track_time_interval(self.hass, self._ramp_step, timedelta(seconds=5), name="wecker_ramp")

    async def _ramp_step(self, _now: Any = None) -> None:
        active = False
        for aid, r in self.rings.items():
            a = self.data["alarms"].get(aid) or {}
            ent = self._player(r.get("seite"))
            laut = self.ausgabe.cfg(r.get("seite")).get("laut")
            if r["state"] != "ringing" or not (ent or laut) or not a.get("ramp"):
                continue
            active = True
            el = (dt_util.utcnow() - _dt(r["since"])).total_seconds()
            f = min(1.0, el / max(1, int(a.get("ramp_s") or 1)))
            vol = (a.get("vol_min") or 0) + ((a.get("vol_max") or 0) - (a.get("vol_min") or 0)) * f
            if ent:
                await self.hass.services.async_call("media_player", "volume_set", {"entity_id": ent, "volume_level": round(vol) / 100})
            if laut:
                await self.ausgabe.async_volume(r.get("seite") or "", vol)
        if not active and self._ramp_unsub:
            self._ramp_unsub()
            self._ramp_unsub = None

    def _player_stop(self, seite: str | None) -> None:
        ent = self._player(seite)
        if ent:
            self.hass.async_create_task(self.hass.services.async_call("media_player", "media_stop", {"entity_id": ent}))

    # ================================================================ Bearbeiten
    def _clean(self, a: dict) -> dict:
        out = {**copy.deepcopy(DEFAULT_ALARM), **{k: v for k, v in a.items() if k in DEFAULT_ALARM or k in ("id", "created_at")}}
        h, m = _hm(out["time"])
        out["time"] = f"{h:02d}:{m:02d}"
        out["days"] = sorted({int(d) for d in out.get("days") or [] if 0 <= int(d) <= 6})
        out["vol_min"] = max(0, min(100, int(out["vol_min"])))
        out["vol_max"] = max(out["vol_min"], min(100, int(out["vol_max"])))
        out["snooze_min"] = max(1, min(60, int(out["snooze_min"])))
        out["snooze_max"] = max(0, min(20, int(out["snooze_max"])))
        out["ramp_s"] = max(5, min(1800, int(out["ramp_s"])))
        out["auto_stop_min"] = max(0, min(180, int(out["auto_stop_min"])))
        if out["lock"] not in LOCKS:
            out["lock"] = "rechnen"
        out["name"] = str(out.get("name") or "Wecker")[:60]
        clean_alarm_extra(out)
        return out

    async def async_save_alarm(self, patch: dict) -> str:
        aid = patch.get("id") or uuid.uuid4().hex[:12]
        old = self.data["alarms"].get(aid) or {"created_at": _iso(dt_util.utcnow())}
        a = self._clean({**old, **patch, "id": aid})
        if a["seite"] not in self.seiten():
            raise ValueError(f"Unbekannte Bettseite: {a['seite']}")
        self.data["alarms"][aid] = a
        self.dirty_alarms.add(aid)
        self.next_due.pop(aid, None)
        self._reschedule()
        await self._async_persist()
        self._push()
        return aid

    async def async_delete_alarm(self, aid: str) -> None:
        if aid in self.rings:
            await self.async_stop(alarm_id=aid)
        self.data["alarms"].pop(aid, None)
        self.dirty_alarms.discard(aid)
        self.deleted_alarms.add(aid)
        self._reschedule()
        await self._async_persist()
        self._push()

    async def async_save_settings(self, patch: dict) -> None:
        s = self.data["settings"]
        if "seiten" in patch:
            seiten = {}
            for k, v in (patch["seiten"] or {}).items():
                seiten[str(k)[:32]] = {"name": str(v.get("name") or k)[:40], "browser": bool(v.get("browser", True)), "player": str(v.get("player") or ""),
                                       "tablet": str(v.get("tablet") or "")}
            s["seiten"] = seiten or s.get("seiten")
        if "uhr" in patch:
            tz = (patch["uhr"] or {}).get("zeitzone")
            s["uhr"] = {**(s.get("uhr") or {}), "zeitzone": tz if tz in ("tablet", "server") else "tablet"}
        if "hintergrund" in patch:
            self._merge_bg(patch["hintergrund"] or {})
        if "ausgabe" in patch:
            merge_ausgabe(s, patch["ausgabe"] or {}, self.seiten())
        if "vibration" in patch:
            merge_vibration(s, patch["vibration"] or {}, self.seiten())
        if "bett" in patch:
            old = {k: self.bett.side(k).get("heizung") for k in self.seiten()}
            for k, ent in old.items():   # Heizdecke wird gewechselt, während sie läuft → erst die alte ausschalten
                sp = (patch["bett"] or {}).get(k) or {}
                if "heizung" in sp and (sp["heizung"] or "") != (ent or "") and (self.bett.heat.get(k) or {}).get("on"):
                    await self.bett.async_heat_off(k, "Heizdecke in den Einstellungen geändert")
            self._merge_bett(patch["bett"] or {})
        self.dirty_settings = True
        if "bett" in patch:
            self.bett.retrack()
        await self._async_persist()
        self._push()

    def _merge_bg(self, p: dict) -> None:
        hg = self.data["settings"].setdefault("hintergrund", {})
        for seite in self.seiten():
            sp = p.get(seite)
            if not isinstance(sp, dict):
                continue
            cur = hg.setdefault(seite, dict(DEFAULT_HINTERGRUND))
            if sp.get("art") in HINTERGRUND_ARTEN:
                cur["art"] = sp["art"]
            if isinstance(sp.get("farbe"), str) and len(sp["farbe"]) <= 32:
                cur["farbe"] = sp["farbe"]
            if "bild" in sp:
                cur["bild"] = str(sp["bild"] or "")[:500]
            if "diashow_min" in sp:
                cur["diashow_min"] = int(_f(sp["diashow_min"], 1, 1440, 10))
            if "abdunkeln" in sp:
                cur["abdunkeln"] = int(_f(sp["abdunkeln"], 0, 90, 25))
            if "blick" in sp:
                cur["blick"] = int(_f(sp["blick"], 0, 359, 180)) % 360
            if "glas_blur" in sp:
                cur["glas_blur"] = int(_f(sp["glas_blur"], 0, 30, 10))
            if "glas_deckkraft" in sp:
                cur["glas_deckkraft"] = int(_f(sp["glas_deckkraft"], 5, 95, 50))
            if "batterie_meldungen" in sp:      # Batterie-Manager-Meldungen auf dem Tablet dieser Seite zeigen
                cur["batterie_meldungen"] = bool(sp["batterie_meldungen"])
            if sp.get("schrift") in SCHRIFTEN:  # Erscheinungsbild: Schriftart des Panels
                cur["schrift"] = sp["schrift"]
            # Uhr nachts dimmen: an/aus, Zeitraum (HH:MM, auch über Mitternacht), Helligkeit in %
            if "nacht_an" in sp:
                cur["nacht_an"] = bool(sp["nacht_an"])
            for k in ("nacht_von", "nacht_bis"):
                v = str(sp.get(k) or "")
                if k in sp and len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit() and int(v[:2]) < 24 and int(v[3:]) < 60:
                    cur[k] = v
            if "nacht_hell" in sp:
                cur["nacht_hell"] = int(_f(sp["nacht_hell"], 5, 100, 45))
            # Akku des Tablets: Entität für % und für den Ladezustand (leer = automatisch über Browser Mod)
            # Leistung: Aurora bewegt, Rahmen-Weichzeichner, Vollbild vorbereiten, Zoom-Dauer (ms)
            for k in ("perf_aurora", "perf_blur", "perf_vorab", "perf_anzeige", "led_geist"):
                if k in sp:
                    cur[k] = bool(sp[k])
            if "aurora_tempo" in sp:            # Bewegungsdauer der Aurora: 1 = normal, 2 = halb so schnell …
                cur["aurora_tempo"] = round(_f(sp["aurora_tempo"], 0.25, 4, 1), 2)
            for k in ("akku_level", "akku_status", "akku_lader"):
                if k in sp:
                    v = str(sp[k] or "").strip()
                    if not v or (len(v) <= 255 and v.count(".") == 1 and v.replace(".", "").replace("_", "").isalnum() and v == v.lower()):
                        cur[k] = v

    def _merge_bett(self, p: dict) -> None:
        b = self.data["settings"].setdefault("bett", copy.deepcopy(DEFAULT_BETT))
        lim = {"max_kg": (20, 500), "belegt_kg": (1, 150), "temp_min": (0, 30), "temp_max": (25, 70), "max_temp": (20, 50),
               "schutz_temp": (25, 70), "max_dauer_min": (10, 1440), "standard_temp": (15, 50), "standard_dauer_min": (5, 1440), "hysterese": (0.2, 5)}
        for k, (lo, hi) in lim.items():
            if k in p:
                b[k] = _f(p[k], lo, hi, b.get(k, DEFAULT_BETT[k]))
        b["max_temp"] = min(b["max_temp"], b["schutz_temp"] - 2)        # Ziel immer unter dem Schutz
        b["standard_temp"] = min(b["standard_temp"], b["max_temp"])
        for seite in self.seiten():
            if not isinstance(p.get(seite), dict):
                continue
            sp, cur = p[seite], b.setdefault(seite, copy.deepcopy(DEFAULT_BETT_SEITE))
            for k in ("gesamt", "heizung", "temperatur"):
                if k in sp:
                    cur[k] = str(sp[k] or "")[:255]
            if "tara" in sp:
                cur["tara"] = _f(sp["tara"], -50, 300, 0.0)
            for k in BETT_ZELLEN:
                if k in (sp.get("zellen") or {}):
                    cur.setdefault("zellen", {})[k] = str(sp["zellen"][k] or "")[:255]

    async def async_events(self, limit: int, seite: str | None) -> list[dict]:
        if self.db_ok:
            try:
                return await self.hass.async_add_executor_job(self.db.list_events, limit, seite)
            except Exception as err:  # noqa: BLE001
                self._set_offline(err)
        ev = [e for e in reversed(self.recent_events) if not seite or e.get("seite") == seite]
        return ev[:limit]

    # ================================================================ Protokoll / Ausgabe
    def _event(self, kind: str, a: dict, detail: str | None = None) -> None:
        e = {"ts": dt_util.utcnow().isoformat(), "alarm": a.get("id"), "name": a.get("name"), "seite": a.get("seite"), "kind": kind, "detail": detail}
        self.pending_events.append(e)
        self.recent_events = (self.recent_events + [e])[-100:]
        _LOGGER.info("Wecker %s (%s): %s %s", a.get("name"), a.get("seite"), kind, detail or "")

    def next_for(self, seite: str) -> dict | None:
        best = None
        for aid, iso in self.next_due.items():
            a = self.data["alarms"].get(aid)
            if not iso or not a or a.get("seite") != seite:
                continue
            if best is None or iso < best[0]:
                best = (iso, a)
        return {"at": best[0], "id": best[1]["id"], "name": best[1]["name"], "time": best[1]["time"]} if best else None

    def seite_state(self, seite: str) -> str:
        st = [r["state"] for r in self.rings.values() if r.get("seite") == seite]
        if "ringing" in st:
            return "klingelt"
        if "snoozed" in st:
            return "schlummert"
        return "bereit" if self.next_for(seite) else "aus"

    def snapshot(self) -> dict:
        alarms = []
        for a in sorted(self.data["alarms"].values(), key=lambda x: (x.get("seite"), x.get("time"), x.get("name"))):
            alarms.append({**a, "next": self.next_due.get(a["id"]), "sound_url": self.sound_url(a)})
        rings = {}
        for aid, r in self.rings.items():
            a = self.data["alarms"].get(aid) or {}
            mx = int(a.get("snooze_max") or 0)
            rings[aid] = {
                **r, "id": aid, "time": a.get("time"), "sound_url": self.sound_url(a, r), "ramp": a.get("ramp", True),
                "vol_min": a.get("vol_min", 10), "vol_max": a.get("vol_max", 80), "ramp_s": a.get("ramp_s", 120),
                "lock": a.get("lock", "rechnen"), "snooze_min": a.get("snooze_min", 9),
                "can_snooze": bool(a.get("snooze", True)) and (not mx or r["count"] < mx),
                "browser": (self.seiten().get(r.get("seite") or "") or {}).get("browser", True),
            }
        return {
            "version": VERSION,
            "rev": self.rev,
            "now": dt_util.utcnow().isoformat(),
            "tablets": self.waechter.status(),
            "db": {"connected": self.db_ok, "error": self.db_error, "schema": self.schema,
                   "pending": len(self.pending_events) + len(self.dirty_alarms) + len(self.deleted_alarms) + int(self.dirty_settings or self.dirty_all)},
            "seiten": self.seiten(),
            "alarms": alarms,
            "rings": rings,
            "next": {s: self.next_for(s) for s in self.seiten()},
            "sounds": self.sounds,
            "uhr": self.data["settings"].get("uhr") or {"zeitzone": "tablet"},
            "hintergrund": self.data["settings"].get("hintergrund") or {},
            "ausgabe": self.data["settings"].get("ausgabe") or {},
            "vibration": self.data["settings"].get("vibration") or {},
            "bett": {"config": self.data["settings"].get("bett") or {}, "heat": self.bett.snapshot()},
        }

    @callback
    def async_subscribe(self, cb: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.add(cb)

        @callback
        def unsub() -> None:
            self._listeners.discard(cb)

        return unsub

    @callback
    def _push(self) -> None:
        self.rev += 1
        snap = self.snapshot()
        for cb in list(self._listeners):
            try:
                cb(snap)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Wecker: Fehler beim Senden an die Karte")
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        try:
            self.waechter.async_sync()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Wecker: Fehler im Tablet-Wächter")
