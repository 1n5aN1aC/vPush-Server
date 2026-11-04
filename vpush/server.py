"""The HTTP API (standard library only).

Every route works with or without the base path prefix, so it doesn't matter
whether the reverse proxy strips /vpush before forwarding.

  GET  /healthz
  GET  /v1/apps/{app}/key           -> {"publicKey"}        (origin-checked)
  POST /v1/apps/{app}/subscribe     -> {"id", "token"}      (origin-checked; Bearer token = update)
  POST /v1/apps/{app}/mark          {name, date}            (Bearer subscriber token)
  POST /v1/apps/{app}/unsubscribe                           (Bearer subscriber token)
  POST /v1/apps/{app}/send          {to, payload, ttl?}     (Bearer admin token)
"""

import hmac
import json
import logging
import re
from datetime import date as ddate, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from . import push as pushmod

log = logging.getLogger("vpush.http")

MAX_BODY = 16 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
ROUTE_RE = re.compile(r"^/v1/apps/([a-z0-9][a-z0-9_-]{0,63})/(key|subscribe|mark|unsubscribe|send)$")

# Push services we'll accept subscriptions for (suffix match on the host).
PUSH_HOSTS = (
    "push.apple.com",                       # Safari / iOS: web.push.apple.com
    "fcm.googleapis.com",                   # Chrome, Edge (Android/desktop)
    "push.services.mozilla.com",            # Firefox
    "notify.windows.com",                   # legacy Edge / Windows
)


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def strip_base(path: str, base: str) -> str:
    path = urlsplit(path).path or "/"
    if base and (path == base or path.startswith(base + "/")):
        path = path[len(base):] or "/"
    return path.rstrip("/") or "/"


def push_host_allowed(endpoint: str, extra=()) -> bool:
    try:
        u = urlsplit(endpoint)
    except ValueError:
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    host = u.hostname.lower()
    return any(host == h or host.endswith("." + h) for h in (*PUSH_HOSTS, *extra))


def valid_subscription(sub, extra_hosts=()):
    if not isinstance(sub, dict):
        raise HttpError(400, "subscription must be an object")
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    if not isinstance(endpoint, str) or len(endpoint) > 1024 or not push_host_allowed(endpoint, extra_hosts):
        raise HttpError(400, "subscription endpoint is not a known push service")
    for k in ("p256dh", "auth"):
        v = keys.get(k)
        if not isinstance(v, str) or not 8 <= len(v) <= 256:
            raise HttpError(400, f"subscription keys.{k} is missing or invalid")
    return {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}


def valid_tz(tz):
    try:
        ZoneInfo(str(tz))
        return str(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HttpError(400, "tz must be an IANA time zone like America/Chicago")


def valid_prefs(prefs):
    if prefs is None:
        return {}
    if not isinstance(prefs, dict) or len(json.dumps(prefs)) > 2048:
        raise HttpError(400, "prefs must be a small object")
    return prefs


class Handler(BaseHTTPRequestHandler):
    server_version = "vPush/" + __version__
    protocol_version = "HTTP/1.1"

    # Set on the class by make_server().
    registry = None
    db = None
    settings = None
    sender = staticmethod(pushmod.send)

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # ---- plumbing ----------------------------------------------------------

    def _origin(self):
        return (self.headers.get("Origin") or "").rstrip("/")

    def _cors(self, app):
        origin = self._origin()
        if app and origin and app.origin_allowed(origin):
            return {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "86400",
                "Vary": "Origin",
            }
        return {"Vary": "Origin"}

    def _reply(self, status, body=None, app=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        for k, v in self._cors(app).items():
            self.send_header(k, v)
        self.send_header("Cache-Control", "no-store")
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _read_body(self):
        """Read the whole request body up front, whatever happens next.

        Connections are kept alive, so a rejected request whose body was never
        read would leave those bytes in the socket and corrupt the browser's
        next request on the same connection."""
        self._raw = b""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            raise HttpError(400, "bad Content-Length")
        if n > MAX_BODY:
            self.close_connection = True   # don't read it; drop the connection after replying
            raise HttpError(413, "request too large")
        if n > 0:
            self._raw = self.rfile.read(n)

    def _json(self):
        if not self._raw:
            return {}
        try:
            body = json.loads(self._raw)
        except ValueError:
            raise HttpError(400, "body must be JSON")
        if not isinstance(body, dict):
            raise HttpError(400, "body must be a JSON object")
        return body

    def _bearer(self):
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth[:7].lower() == "bearer " else ""

    def _route(self):
        path = strip_base(self.path, self.settings.base_path)
        if path == "/healthz":
            return "healthz", None, None
        m = ROUTE_RE.match(path)
        if not m:
            raise HttpError(404, "not found")
        app = self.registry.get(m.group(1))
        if not app:
            raise HttpError(404, "unknown app")
        return m.group(2), app, path

    def _require_origin(self, app):
        # Browsers always send Origin on these requests. This keeps other sites'
        # pages from using an app's endpoints (it's not authentication).
        origin = self._origin()
        if app.origins and not (origin and app.origin_allowed(origin)):
            raise HttpError(403, "origin not allowed for this app")

    def _dispatch(self, method):
        app = None
        body_error = None
        try:
            self._read_body()
        except HttpError as e:
            body_error = e   # still resolve the app below, so the error carries CORS headers
        try:
            action, app, _ = self._route()
            if body_error:
                raise body_error
            if method == "OPTIONS":
                return self._reply(204, None, app)
            if action == "healthz":
                if method != "GET":
                    raise HttpError(405, "method not allowed")
                return self._reply(200, {"ok": True, "version": __version__, "build": self.settings.build})
            want = "GET" if action == "key" else "POST"
            if method != want:
                raise HttpError(405, "method not allowed")
            result = getattr(self, "act_" + action)(app)
            self._reply(200, result, app)
        except HttpError as e:
            self._reply(e.status, {"error": e.message}, app)
        except Exception:
            log.exception("unhandled error on %s %s", method, self.path)
            self._reply(500, {"error": "internal error"}, app)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    # ---- actions -----------------------------------------------------------

    def act_key(self, app):
        self._require_origin(app)
        return {"publicKey": app.vapid_public_key}

    def act_subscribe(self, app):
        self._require_origin(app)
        body = self._json()
        sub = valid_subscription(body.get("subscription"), self.settings.extra_push_hosts)
        tz = valid_tz(body.get("tz", "UTC"))
        prefs = valid_prefs(body.get("prefs"))
        token = self._bearer()
        if token:
            row = self.db.authenticate(app.id, token)
            if not row:
                raise HttpError(401, "unknown subscriber token")
            self.db.update_subscriber(row["id"], sub, tz, prefs)
            return {"id": row["id"]}
        if self.db.count(app.id) >= app.max_subscribers:
            raise HttpError(429, "this app has reached its subscriber limit")
        sid, new_token = self.db.create_subscriber(app.id, sub, tz, prefs)
        log.info("%s: new subscriber %s", app.id, sid)
        return {"id": sid, "token": new_token}

    def _subscriber(self, app):
        row = self.db.authenticate(app.id, self._bearer())
        if not row:
            raise HttpError(401, "missing or unknown subscriber token")
        return row

    def act_mark(self, app):
        self._require_origin(app)
        row = self._subscriber(app)
        body = self._json()
        name = body.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise HttpError(400, "name must be 1-64 letters, digits or _.:-")
        try:
            day = ddate.fromisoformat(str(body.get("date")))
        except ValueError:
            raise HttpError(400, "date must be YYYY-MM-DD")
        local_today = datetime.now(ZoneInfo(row["tz"])).date()
        if abs((day - local_today).days) > 1:
            raise HttpError(400, "date is more than a day from the subscriber's local date")
        self.db.add_mark(row["id"], name, day.isoformat())
        return {"ok": True}

    def act_unsubscribe(self, app):
        self._require_origin(app)
        row = self._subscriber(app)
        self.db.delete_subscriber(row["id"])
        log.info("%s: subscriber %s unsubscribed", app.id, row["id"])
        return {"ok": True}

    def act_send(self, app):
        if not hmac.compare_digest(self._bearer(), app.admin_token):
            raise HttpError(401, "admin token required")
        body = self._json()
        payload = body.get("payload")
        if not isinstance(payload, dict) or len(json.dumps(payload)) > 3000:
            raise HttpError(400, "payload must be an object under 3 KB")
        to = body.get("to", "all")
        if to == "all":
            rows = self.db.subscribers(app.id)
        else:
            row = self.db.get_subscriber(str(to))
            if not row or row["app"] != app.id:
                raise HttpError(404, "unknown subscriber")
            rows = [row]
        ttl = int(body.get("ttl", 3600))
        result = {"sent": 0, "failed": 0, "removed": 0}
        for row in rows:
            status = self.sender(app, row, payload, self.settings.contact, ttl=ttl)
            if status in pushmod.GONE:
                self.db.delete_subscriber(row["id"])
                result["removed"] += 1
            elif 200 <= status < 300:
                result["sent"] += 1
            else:
                result["failed"] += 1
        return result


def make_server(settings, registry, db, sender=None):
    handler = type("BoundHandler", (Handler,), {
        "registry": registry, "db": db, "settings": settings,
        **({"sender": staticmethod(sender)} if sender else {}),
    })
    return ThreadingHTTPServer((settings.host, settings.port), handler)
