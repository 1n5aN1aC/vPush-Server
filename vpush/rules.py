"""Rules decide, per subscriber, whether to send right now.

Every rule is a should_send(sub) that returns a payload dict or None. The
scheduler only calls it when the rule hasn't already sent to that subscriber
in the current once_per period, so rules never have to worry about repeats.

Built-in:  type = "daily"   (see daily_rule below; it's also the example)
Custom:    type = "script"  -> should_send(sub) in <config>/apps/<app>.py
"""

import importlib.util
import json
import logging
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

log = logging.getLogger("vpush.rules")


class Sub:
    """What a rule gets to look at. Read-only view of one subscriber."""

    def __init__(self, row, db, now=None):
        self._row = row
        self._db = db
        self._now = now  # injectable UTC "now" for tests
        self.id = row["id"]
        self.app = row["app"]
        self.tz = ZoneInfo(row["tz"])
        self.prefs = json.loads(row["prefs"] or "{}")
        self.created = datetime.fromtimestamp(row["created"], self.tz)

    def local_now(self) -> datetime:
        now = self._now or datetime.now(tz=ZoneInfo("UTC"))
        return now.astimezone(self.tz)

    def today(self) -> str:
        return self.local_now().date().isoformat()

    def has_mark(self, name: str, date: str = None) -> bool:
        return self._db.has_mark(self.id, name, date or self.today())

    def period(self, once_per: str) -> str:
        now = self.local_now()
        return now.strftime("%Y-%m-%d") if once_per == "day" else now.strftime("%Y-%m-%dT%H")


def parse_hhmm(value, fallback=None):
    try:
        h, m = str(value).split(":")
        return dtime(int(h), int(m))
    except (ValueError, TypeError):
        return fallback


def daily_rule(options: dict):
    """Once a day at the subscriber's chosen time (prefs.time, "HH:MM"), falling
    back to default_time. Skipped if the subscriber has today's skip_if_mark.
    Not sent on the day someone subscribes after that day's time has passed."""
    default = parse_hhmm(options.get("default_time", "20:00"), dtime(20, 0))
    pref_key = options.get("pref", "time")
    skip_mark = options.get("skip_if_mark")
    payload = dict(options.get("payload") or {"title": "Reminder"})

    def should_send(sub: Sub):
        at = parse_hhmm(sub.prefs.get(pref_key), default)
        now = sub.local_now()
        due = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        if now < due:
            return None
        if sub.created > due:
            return None  # subscribed after today's reminder time
        if skip_mark and sub.has_mark(skip_mark):
            return None
        return payload

    return should_send


class ScriptRule:
    """should_send(sub) from <config>/apps/<app>.py, reloaded when the file changes."""

    def __init__(self, path: str, func: str = "should_send"):
        self.path = path
        self.func = func
        self._mtime = None
        self._fn = None

    def _load(self):
        mtime = os.stat(self.path).st_mtime_ns
        if self._fn is not None and mtime == self._mtime:
            return self._fn
        spec = importlib.util.spec_from_file_location(f"vpush_rule_{abs(hash(self.path))}", self.path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = getattr(module, self.func, None)
        if not callable(fn):
            raise AttributeError(f"{self.path} has no function {self.func}(sub)")
        self._fn, self._mtime = fn, mtime
        log.info("loaded rule script %s", os.path.basename(self.path))
        return fn

    def __call__(self, sub: Sub):
        return self._load()(sub)


def build(app, rule):
    """The should_send callable for one configured rule."""
    if rule.type == "daily":
        return daily_rule(rule.options)
    path = rule.options.get("file")
    path = os.path.join(os.path.dirname(app.path), path) if path else app.script_path
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"{app.id}: rule {rule.id} needs {app.id}.py (or file = ...)")
    return ScriptRule(path, rule.options.get("function", "should_send"))
