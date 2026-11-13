"""Datenbank-Schicht des Weckers (synchron, läuft im Executor).

Ohne Home-Assistant-Importe, damit sie auch außerhalb von HA testbar ist (SQLite).
wk_alarm  = alle Wecker (eine Zeile je Wecker)
wk_setting = Einstellungen (JSON je Schlüssel, z. B. die Bettseiten)
wk_event  = Protokoll: klingelt, schlummert, aus, automatisch aus, verpasst, ausgelassen
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine, make_url

SCHEMA_VERSION = 1

TABLE_KW = {
    "mysql_engine": "InnoDB", "mysql_charset": "utf8mb4",
    "mariadb_engine": "InnoDB", "mariadb_charset": "utf8mb4",
}

md = MetaData()

t_meta = Table("wk_meta", md, Column("key", String(64), primary_key=True), Column("value", Text), **TABLE_KW)

t_setting = Table("wk_setting", md, Column("key", String(64), primary_key=True), Column("value", Text), **TABLE_KW)

t_alarm = Table(
    "wk_alarm", md,
    Column("alarm_id", String(36), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("seite", String(32), nullable=False, index=True),
    Column("enabled", Boolean, nullable=False),
    Column("time", String(5), nullable=False),        # HH:MM
    Column("days", String(32)),                       # JSON-Liste 0..6 (Mo..So), leer = einmalig
    Column("sound", String(255)),
    Column("snooze", Boolean),
    Column("snooze_min", Integer),
    Column("snooze_max", Integer),
    Column("ramp", Boolean),
    Column("vol_min", Integer),
    Column("vol_max", Integer),
    Column("ramp_s", Integer),
    Column("lock", String(16)),                       # rechnen | halten | keine
    Column("auto_stop_min", Integer),
    Column("skip_date", String(10)),
    Column("sort", Integer),
    Column("extra", Text),                           # JSON: Felder ab 1.3.0 (zufall, vib…, eq)
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
    **TABLE_KW,
)

t_event = Table(
    "wk_event", md,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("alarm_id", String(36), index=True),
    Column("name", String(255)),
    Column("seite", String(32)),
    Column("kind", String(16), nullable=False),   # ring | snooze | stop | auto_stop | missed | skip | test
    Column("detail", Text),
    **TABLE_KW,
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def to_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _l(v: Any, default: Any) -> Any:
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _s(v: Any, n: int) -> str | None:
    return None if v in (None, "") else str(v)[:n]


EXTRA_KEYS = ("zufall", "vib", "vib_muster", "vib_staerke", "vib_verz_s", "eq")


def alarm_row(a: dict) -> dict:
    return {
        "extra": _j({k: a[k] for k in EXTRA_KEYS if k in a}),
        "name": _s(a.get("name"), 255) or "Wecker", "seite": _s(a.get("seite"), 32) or "links",
        "enabled": bool(a.get("enabled")), "time": str(a.get("time") or "07:00")[:5],
        "days": _j(sorted({int(d) for d in a.get("days") or []})), "sound": _s(a.get("sound"), 255),
        "snooze": bool(a.get("snooze")), "snooze_min": int(a.get("snooze_min") or 9), "snooze_max": int(a.get("snooze_max") or 0),
        "ramp": bool(a.get("ramp")), "vol_min": int(a.get("vol_min") or 0), "vol_max": int(a.get("vol_max") or 0),
        "ramp_s": int(a.get("ramp_s") or 0), "lock": _s(a.get("lock"), 16) or "rechnen",
        "auto_stop_min": int(a.get("auto_stop_min") or 0), "skip_date": _s(a.get("skip_date"), 10), "sort": int(a.get("sort") or 50),
        "created_at": from_iso(a.get("created_at")) or now_utc(), "updated_at": now_utc(),
    }


class WeckerDB:
    """Zugriff auf die Datenbank (MariaDB, zum Testen auch SQLite)."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine: Engine | None = None

    def connect(self) -> int:
        self.close()
        u = make_url(self.url)
        kw: dict[str, Any] = {"pool_pre_ping": True, "future": True}
        if u.get_backend_name() in ("mysql", "mariadb"):
            kw.update(pool_recycle=1800, pool_size=2, max_overflow=3, connect_args={"connect_timeout": 10})
        self.engine = create_engine(u, **kw)
        md.create_all(self.engine)
        # Spalte extra (1.3.0) in bestehenden Tabellen nachrüsten
        if "extra" not in {c["name"] for c in inspect(self.engine).get_columns("wk_alarm")}:
            with self.engine.begin() as c:
                c.execute(text("ALTER TABLE wk_alarm ADD COLUMN extra TEXT"))
        with self.engine.begin() as c:
            v = c.execute(select(t_meta.c.value).where(t_meta.c.key == "schema_version")).scalar()
            if v is None:
                c.execute(insert(t_meta).values(key="schema_version", value=str(SCHEMA_VERSION)))
                return SCHEMA_VERSION
            return int(v)

    def close(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None

    def is_initialized(self) -> bool:
        with self.engine.connect() as c:
            return c.execute(select(t_meta.c.value).where(t_meta.c.key == "initialized")).scalar() is not None

    # ------------------------------------------------------------ Lesen
    def load_all(self) -> dict:
        out: dict[str, Any] = {"settings": {}, "alarms": {}}
        with self.engine.connect() as c:
            for r in c.execute(select(t_setting)):
                out["settings"][r.key] = _l(r.value, None)
            for r in c.execute(select(t_alarm)).mappings():
                out["alarms"][r["alarm_id"]] = {
                    "id": r["alarm_id"], "name": r["name"], "seite": r["seite"], "enabled": bool(r["enabled"]),
                    "time": r["time"], "days": _l(r["days"], []), "sound": r["sound"] or "",
                    "snooze": bool(r["snooze"]), "snooze_min": r["snooze_min"] or 9, "snooze_max": r["snooze_max"] or 0,
                    "ramp": bool(r["ramp"]), "vol_min": r["vol_min"] or 0, "vol_max": r["vol_max"] or 0, "ramp_s": r["ramp_s"] or 0,
                    "lock": r["lock"] or "rechnen", "auto_stop_min": r["auto_stop_min"] or 0, "skip_date": r["skip_date"],
                    "sort": r["sort"] or 50, "created_at": to_iso(r["created_at"]),
                    **{k: v for k, v in (_l(r.get("extra"), {}) or {}).items() if k in EXTRA_KEYS},
                }
        return out

    def list_events(self, limit: int = 200, seite: str | None = None) -> list[dict]:
        q = select(t_event).order_by(t_event.c.id.desc()).limit(limit)
        if seite:
            q = q.where(t_event.c.seite == seite)
        with self.engine.connect() as c:
            return [
                {"id": r["id"], "ts": to_iso(r["ts"]), "alarm": r["alarm_id"], "name": r["name"], "seite": r["seite"],
                 "kind": r["kind"], "detail": r["detail"]}
                for r in c.execute(q).mappings()
            ]

    # ------------------------------------------------------------ Schreiben
    def save_all(self, data: dict) -> None:
        """Alles ersetzen (nach Offline-Betrieb oder bei leerer DB)."""
        with self.engine.begin() as c:
            c.execute(delete(t_setting))
            rows = [{"key": k[:64], "value": _j(v)} for k, v in (data.get("settings") or {}).items()]
            if rows:
                c.execute(insert(t_setting), rows)
            c.execute(delete(t_alarm))
            rows = [{"alarm_id": aid[:36], **alarm_row(a)} for aid, a in (data.get("alarms") or {}).items()]
            if rows:
                c.execute(insert(t_alarm), rows)
            c.execute(delete(t_meta).where(t_meta.c.key == "initialized"))
            c.execute(insert(t_meta).values(key="initialized", value=to_iso(now_utc())))

    def save_alarm(self, a: dict) -> None:
        with self.engine.begin() as c:
            c.execute(delete(t_alarm).where(t_alarm.c.alarm_id == a["id"]))
            c.execute(insert(t_alarm).values(alarm_id=a["id"][:36], **alarm_row(a)))

    def delete_alarm(self, aid: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(t_alarm).where(t_alarm.c.alarm_id == aid))

    def save_settings(self, settings: dict) -> None:
        with self.engine.begin() as c:
            c.execute(delete(t_setting))
            rows = [{"key": k[:64], "value": _j(v)} for k, v in settings.items()]
            if rows:
                c.execute(insert(t_setting), rows)

    def insert_events(self, events: list[dict]) -> None:
        if not events:
            return
        rows = [{
            "ts": from_iso(e.get("ts")) or now_utc(), "alarm_id": _s(e.get("alarm"), 36), "name": _s(e.get("name"), 255),
            "seite": _s(e.get("seite"), 32), "kind": (e.get("kind") or "info")[:16], "detail": e.get("detail"),
        } for e in events]
        with self.engine.begin() as c:
            c.execute(insert(t_event), rows)
            c.execute(delete(t_event).where(t_event.c.ts < now_utc() - timedelta(days=730)))
