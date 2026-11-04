"""SQLite storage: subscribers, marks, and the send log used for once_per."""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    id          TEXT PRIMARY KEY,
    app         TEXT NOT NULL,
    token_hash  TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    tz          TEXT NOT NULL,
    prefs       TEXT NOT NULL DEFAULT '{}',
    created     REAL NOT NULL,
    updated     REAL NOT NULL,
    UNIQUE (app, endpoint)
);
CREATE TABLE IF NOT EXISTS marks (
    sub_id  TEXT NOT NULL,
    name    TEXT NOT NULL,
    date    TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (sub_id, name, date)
);
CREATE TABLE IF NOT EXISTS sends (
    sub_id  TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    period  TEXT NOT NULL,
    status  INTEGER NOT NULL,
    sent_at REAL NOT NULL,
    PRIMARY KEY (sub_id, rule_id, period)
);
"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def _x(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).rowcount

    # ---- subscribers -------------------------------------------------------

    def create_subscriber(self, app, sub, tz, prefs):
        """New subscriber; replaces any existing one with the same endpoint
        (e.g. the app was reinstalled and lost its token). Returns (id, token)."""
        sid = secrets.token_urlsafe(9)
        token = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            old = self.conn.execute(
                "SELECT id FROM subscribers WHERE app=? AND endpoint=?", (app, sub["endpoint"])
            ).fetchone()
            if old:
                self._delete_locked(old["id"])
            self.conn.execute(
                "INSERT INTO subscribers (id, app, token_hash, endpoint, p256dh, auth, tz, prefs, created, updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, app, hash_token(token), sub["endpoint"], sub["keys"]["p256dh"],
                 sub["keys"]["auth"], tz, json.dumps(prefs), now, now),
            )
        return sid, token

    def update_subscriber(self, sid, sub, tz, prefs):
        with self.lock:
            # A rotated endpoint may already belong to a stale record; drop that one.
            clash = self.conn.execute(
                "SELECT id FROM subscribers WHERE endpoint=? AND id<>? AND app=(SELECT app FROM subscribers WHERE id=?)",
                (sub["endpoint"], sid, sid),
            ).fetchone()
            if clash:
                self._delete_locked(clash["id"])
            self.conn.execute(
                "UPDATE subscribers SET endpoint=?, p256dh=?, auth=?, tz=?, prefs=?, updated=? WHERE id=?",
                (sub["endpoint"], sub["keys"]["p256dh"], sub["keys"]["auth"], tz,
                 json.dumps(prefs), time.time(), sid),
            )

    def authenticate(self, app, token):
        """The subscriber row for this app + token, or None."""
        if not token:
            return None
        h = hash_token(token)
        rows = self._q("SELECT * FROM subscribers WHERE app=? AND token_hash=?", (app, h))
        for r in rows:
            if hmac.compare_digest(r["token_hash"], h):
                return r
        return None

    def get_subscriber(self, sid):
        rows = self._q("SELECT * FROM subscribers WHERE id=?", (sid,))
        return rows[0] if rows else None

    def subscribers(self, app):
        return self._q("SELECT * FROM subscribers WHERE app=? ORDER BY created", (app,))

    def count(self, app):
        return self._q("SELECT COUNT(*) AS n FROM subscribers WHERE app=?", (app,))[0]["n"]

    def _delete_locked(self, sid):
        for table, col in (("marks", "sub_id"), ("sends", "sub_id"), ("subscribers", "id")):
            self.conn.execute(f"DELETE FROM {table} WHERE {col}=?", (sid,))

    def delete_subscriber(self, sid):
        with self.lock:
            self._delete_locked(sid)

    # ---- marks -------------------------------------------------------------

    def add_mark(self, sid, name, date):
        self._x("INSERT OR IGNORE INTO marks (sub_id, name, date, created) VALUES (?,?,?,?)",
                (sid, name, date, time.time()))

    def has_mark(self, sid, name, date):
        return bool(self._q("SELECT 1 FROM marks WHERE sub_id=? AND name=? AND date=?", (sid, name, date)))

    # ---- send log ----------------------------------------------------------

    def was_sent(self, sid, rule_id, period):
        return bool(self._q("SELECT 1 FROM sends WHERE sub_id=? AND rule_id=? AND period=?",
                            (sid, rule_id, period)))

    def record_send(self, sid, rule_id, period, status):
        self._x("INSERT OR REPLACE INTO sends (sub_id, rule_id, period, status, sent_at) VALUES (?,?,?,?,?)",
                (sid, rule_id, period, status, time.time()))

    # ---- housekeeping ------------------------------------------------------

    def prune(self, days=30):
        cutoff = time.time() - days * 86400
        self._x("DELETE FROM marks WHERE created < ?", (cutoff,))
        self._x("DELETE FROM sends WHERE sent_at < ?", (cutoff,))
