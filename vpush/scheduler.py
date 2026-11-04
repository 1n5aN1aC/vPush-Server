"""The once-a-minute loop that runs every app's rules."""

import logging
import threading
import time

from . import config as cfg
from . import push as pushmod
from . import rules as rulesmod
from .rules import Sub

log = logging.getLogger("vpush.scheduler")


class Registry:
    """The current apps and their compiled rules. Swapped atomically on reload,
    so request threads always see a consistent set."""

    def __init__(self, settings):
        self.settings = settings
        self.apps = {}
        self.compiled = {}   # app_id -> [(Rule, should_send)]
        self._sig = None
        self.lock = threading.Lock()

    def reload_if_changed(self, force=False) -> bool:
        sig = cfg.signature(self.settings.config_dir)
        if not force and sig == self._sig:
            return False
        apps = cfg.load_apps(self.settings.config_dir)
        compiled = {}
        for app in apps.values():
            compiled[app.id] = []
            for rule in app.rules:
                try:
                    compiled[app.id].append((rule, rulesmod.build(app, rule)))
                except Exception as e:
                    log.error("%s: rule %s disabled: %s", app.id, rule.id, e)
        with self.lock:
            self.apps, self.compiled = apps, compiled
        # Re-read: loading may have appended [generated] and changed the mtime.
        self._sig = cfg.signature(self.settings.config_dir)
        log.info("loaded %d app(s): %s", len(apps), ", ".join(sorted(apps)) or "none")
        return True

    def get(self, app_id):
        with self.lock:
            return self.apps.get(app_id)

    def snapshot(self):
        with self.lock:
            return dict(self.apps), dict(self.compiled)


def run_once(registry, db, sender=pushmod.send, now=None) -> dict:
    """One pass over every app/rule/subscriber. Returns counts (for tests/logs)."""
    stats = {"checked": 0, "sent": 0, "failed": 0, "removed": 0}
    apps, compiled = registry.snapshot()
    contact = registry.settings.contact
    for app_id, rules in compiled.items():
        app = apps[app_id]
        for row in db.subscribers(app_id):
            try:
                sub = Sub(row, db, now=now)
            except Exception as e:
                log.warning("%s: subscriber %s unreadable: %s", app_id, row["id"], e)
                continue
            for rule, should_send in rules:
                period = sub.period(rule.once_per)
                if db.was_sent(sub.id, rule.id, period):
                    continue
                stats["checked"] += 1
                try:
                    payload = should_send(sub)
                except Exception as e:
                    log.error("%s: rule %s raised for %s: %s", app_id, rule.id, sub.id, e)
                    continue
                if not payload:
                    continue
                status = sender(app, row, payload, contact, ttl=rule.ttl, urgency=rule.urgency)
                if status in pushmod.GONE:
                    db.delete_subscriber(sub.id)
                    stats["removed"] += 1
                    break  # subscriber is gone; skip its remaining rules
                if 200 <= status < 300:
                    db.record_send(sub.id, rule.id, period, status)
                    stats["sent"] += 1
                else:
                    stats["failed"] += 1
                    # A 4xx won't fix itself within the period: record it so we
                    # don't retry every minute. Network errors / 5xx retry next tick.
                    if 400 <= status < 500:
                        db.record_send(sub.id, rule.id, period, status)
    return stats


class Scheduler(threading.Thread):
    def __init__(self, registry, db, sender=pushmod.send):
        super().__init__(name="vpush-scheduler", daemon=True)
        self.registry, self.db, self.sender = registry, db, sender
        self.stop_event = threading.Event()
        self._last_prune = 0

    def run(self):
        tick = self.registry.settings.tick_seconds
        while not self.stop_event.is_set():
            try:
                self.registry.reload_if_changed()
                stats = run_once(self.registry, self.db, self.sender)
                if stats["sent"] or stats["failed"] or stats["removed"]:
                    log.info("tick: %s", stats)
                if time.time() - self._last_prune > 86400:
                    self.db.prune()
                    self._last_prune = time.time()
            except Exception:
                log.exception("scheduler tick failed")
            # Wake just after the start of each minute (or every `tick` seconds).
            delay = tick - (time.time() % tick) + 1 if tick >= 60 else tick
            self.stop_event.wait(delay)
