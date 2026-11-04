import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import tomllib
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from vpush import config as cfg
from vpush.db import DB
from vpush.rules import Sub, daily_rule
from vpush.scheduler import Registry, run_once
from vpush.server import make_server, strip_base, push_host_allowed
from vpush.settings import Settings

APPLE = "https://web.push.apple.com/QGuQyavXutnMHT4u8nKkTAfQ"
SUB = {"endpoint": APPLE, "keys": {"p256dh": "BOrB4Hm0-p256dh-key-value", "auth": "auth-secret-123"}}
ORIGIN = "https://counter.example.com"

VCOUNTER = """# vCounter
name = "vCounter"
origins = ["https://counter.example.com/"]   # trailing slash is ignored

[[rules]]
id = "daily"
type = "daily"
default_time = "20:00"
skip_if_mark = "done"
payload = { kind = "daily-reminder" }
"""


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("UTC"))


class FakeSender:
    def __init__(self, status=201):
        self.status = status
        self.calls = []

    def __call__(self, app, row, payload, contact, ttl=0, urgency="normal"):
        self.calls.append((app.id, row["id"], payload))
        return self.status(row) if callable(self.status) else self.status


class Env:
    """A temp config + data dir with one app."""

    def __init__(self, toml=VCOUNTER, app_id="vcounter"):
        self.dir = tempfile.mkdtemp(prefix="vpush-test-")
        os.makedirs(os.path.join(self.dir, "config", "apps"))
        self.app_path = os.path.join(self.dir, "config", "apps", f"{app_id}.toml")
        with open(self.app_path, "w", encoding="utf-8") as f:
            f.write(toml)
        self.settings = Settings(config_dir=os.path.join(self.dir, "config"),
                                 data_dir=os.path.join(self.dir, "data"),
                                 base_path="/vpush", host="127.0.0.1", port=0,
                                 contact="mailto:test@example.com", extra_push_hosts=[])
        self.db = DB(os.path.join(self.dir, "data", "vpush.db"))
        self.registry = Registry(self.settings)
        self.registry.reload_if_changed(force=True)

    def close(self):
        self.db.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class PathTests(unittest.TestCase):
    def test_base_path_optional(self):
        for p in ("/vpush/v1/apps/x/key", "/v1/apps/x/key", "/vpush/v1/apps/x/key/", "/v1/apps/x/key?a=1"):
            self.assertEqual(strip_base(p, "/vpush"), "/v1/apps/x/key")
        self.assertEqual(strip_base("/vpushy/v1", "/vpush"), "/vpushy/v1")
        self.assertEqual(strip_base("/vpush", "/vpush"), "/")
        self.assertEqual(strip_base("/v1/x", ""), "/v1/x")

    def test_push_hosts(self):
        self.assertTrue(push_host_allowed(APPLE))
        self.assertTrue(push_host_allowed("https://fcm.googleapis.com/fcm/send/abc"))
        self.assertFalse(push_host_allowed("http://web.push.apple.com/x"))
        self.assertFalse(push_host_allowed("https://evil.com/web.push.apple.com"))
        self.assertFalse(push_host_allowed("https://notpush.apple.com.evil.com/x"))
        self.assertTrue(push_host_allowed("https://push.local/x", ("push.local",)))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_generated_once_and_comments_kept(self):
        with open(self.env.app_path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("# vCounter", text)
        self.assertEqual(text.count("[generated]"), 1)
        gen = tomllib.loads(text)["generated"]
        self.assertEqual(len(cfg.b64url_decode(gen["vapid_public_key"])), 65)
        self.assertEqual(len(cfg.b64url_decode(gen["vapid_private_key"])), 32)
        # Loading again must not append a second section or change the keys.
        app = cfg.load_app(self.env.app_path)
        with open(self.env.app_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)
        self.assertEqual(app.admin_token, gen["admin_token"])
        self.assertEqual(app.origins, [ORIGIN])

    def test_broken_file_is_skipped(self):
        with open(os.path.join(self.env.settings.config_dir, "apps", "broken.toml"), "w") as f:
            f.write("this is = = not toml")
        apps = cfg.load_apps(self.env.settings.config_dir)
        self.assertEqual(sorted(apps), ["vcounter"])

    def test_shipped_example_loads(self):
        src = os.path.join(os.path.dirname(__file__), "..", "examples", "apps", "vcounter.toml")
        dst = os.path.join(self.env.settings.config_dir, "apps", "example.toml")
        shutil.copy(src, dst)
        app = cfg.load_app(dst)
        self.assertEqual([r.type for r in app.rules], ["daily"])
        self.assertEqual(app.rules[0].options["skip_if_mark"], "done")

    def test_bad_rule_type_rejected(self):
        p = os.path.join(self.env.settings.config_dir, "apps", "bad.toml")
        with open(p, "w") as f:
            f.write('[[rules]]\ntype = "weekly"\n')
        with self.assertRaises(cfg.ConfigError):
            cfg.load_app(p)


class DailyRuleTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.rule = daily_rule({"default_time": "20:00", "skip_if_mark": "done", "payload": {"k": 1}})
        sid, _ = self.env.db.create_subscriber("vcounter", SUB, "America/Chicago", {})
        # Pretend they subscribed long ago.
        self.env.db.conn.execute("UPDATE subscribers SET created=? WHERE id=?", (utc(2026, 1, 1, 0).timestamp(), sid))
        self.sid = sid

    def tearDown(self):
        self.env.close()

    def sub(self, now):
        return Sub(self.env.db.get_subscriber(self.sid), self.env.db, now=now)

    def test_before_and_after_time(self):
        # 20:00 Chicago (CST, UTC-6) on Nov 3 = 02:00 UTC Nov 4.
        self.assertIsNone(self.rule(self.sub(utc(2026, 11, 4, 1, 59))))
        self.assertEqual(self.rule(self.sub(utc(2026, 11, 4, 2, 0))), {"k": 1})

    def test_done_mark_skips(self):
        self.env.db.add_mark(self.sid, "done", "2026-11-03")
        self.assertIsNone(self.rule(self.sub(utc(2026, 11, 4, 3))))
        # Next local day isn't affected by yesterday's mark.
        self.assertEqual(self.rule(self.sub(utc(2026, 11, 5, 3))), {"k": 1})

    def test_pref_time(self):
        self.env.db.update_subscriber(self.sid, SUB, "America/Chicago", {"time": "07:30"})
        self.assertIsNone(self.rule(self.sub(utc(2026, 11, 3, 13, 29))))      # 07:29 local
        self.assertIsNotNone(self.rule(self.sub(utc(2026, 11, 3, 13, 30))))   # 07:30 local

    def test_not_sent_on_day_subscribed_after_time(self):
        self.env.db.conn.execute("UPDATE subscribers SET created=? WHERE id=?",
                                 (utc(2026, 11, 4, 4).timestamp(), self.sid))  # 22:00 local Nov 3
        self.assertIsNone(self.rule(self.sub(utc(2026, 11, 4, 5))))
        self.assertIsNotNone(self.rule(self.sub(utc(2026, 11, 5, 3))))        # next evening


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.sid, _ = self.env.db.create_subscriber("vcounter", SUB, "UTC", {})
        self.env.db.conn.execute("UPDATE subscribers SET created=? WHERE id=?", (utc(2026, 1, 1, 0).timestamp(), self.sid))

    def tearDown(self):
        self.env.close()

    def test_once_per_day(self):
        send = FakeSender()
        run_once(self.env.registry, self.env.db, send, now=utc(2026, 11, 3, 20, 0))
        run_once(self.env.registry, self.env.db, send, now=utc(2026, 11, 3, 20, 1))
        run_once(self.env.registry, self.env.db, send, now=utc(2026, 11, 3, 23, 0))
        self.assertEqual(len(send.calls), 1)
        self.assertEqual(send.calls[0][2], {"kind": "daily-reminder"})
        run_once(self.env.registry, self.env.db, send, now=utc(2026, 11, 4, 20, 5))
        self.assertEqual(len(send.calls), 2)

    def test_gone_subscription_removed(self):
        stats = run_once(self.env.registry, self.env.db, FakeSender(410), now=utc(2026, 11, 3, 21))
        self.assertEqual(stats["removed"], 1)
        self.assertIsNone(self.env.db.get_subscriber(self.sid))

    def test_network_error_retries_but_4xx_does_not(self):
        send = FakeSender(0)
        run_once(self.env.registry, self.env.db, send, now=utc(2026, 11, 3, 21))
        run_once(self.env.registry, self.env.db, send, now=utc(2026, 11, 3, 21, 1))
        self.assertEqual(len(send.calls), 2)
        send = FakeSender(403)
        run_once(self.env.registry, self.env.db, send, now=utc(2026, 11, 4, 21))
        run_once(self.env.registry, self.env.db, send, now=utc(2026, 11, 4, 21, 1))
        self.assertEqual(len(send.calls), 1)

    def add_rule(self, block):
        # Appending another [[rules]] entry, even after [generated], is valid TOML.
        with open(self.env.app_path, "a", encoding="utf-8") as f:
            f.write("\n" + block)

    def test_script_rule_and_hot_reload(self):
        self.add_rule('[[rules]]\nid = "custom"\ntype = "script"\nonce_per = "hour"\n')
        script = os.path.join(self.env.settings.config_dir, "apps", "vcounter.py")
        with open(script, "w") as f:
            f.write("def should_send(sub):\n    return {'v': 1, 'who': sub.id} if sub.local_now().minute == 0 else None\n")
        self.assertTrue(self.env.registry.reload_if_changed())
        send = FakeSender()
        run_once(self.env.registry, self.env.db, send, now=utc(2026, 11, 3, 10, 0))
        self.assertEqual([c[2].get("v") for c in send.calls], [1])
        # Edit the script; the next tick picks it up without a restart.
        time.sleep(0.05)
        with open(script, "w") as f:
            f.write("def should_send(sub):\n    return {'v': 2}\n")
        os.utime(script, None)
        self.env.registry.reload_if_changed()
        run_once(self.env.registry, self.env.db, send, now=utc(2026, 11, 3, 11, 30))
        self.assertIn({"v": 2}, [c[2] for c in send.calls])

    def test_rule_exception_is_contained(self):
        script = os.path.join(self.env.settings.config_dir, "apps", "vcounter.py")
        with open(script, "w") as f:
            f.write("def should_send(sub):\n    raise RuntimeError('boom')\n")
        self.add_rule('[[rules]]\nid = "x"\ntype = "script"\n')
        self.env.registry.reload_if_changed()
        send = FakeSender()
        stats = run_once(self.env.registry, self.env.db, send, now=utc(2026, 11, 3, 21))
        self.assertEqual(stats["sent"], 1)   # the daily rule still ran


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.send = FakeSender()
        self.httpd = make_server(self.env.settings, self.env.registry, self.env.db, sender=self.send)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.app = self.env.registry.get("vcounter")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.close()

    def req(self, method, path, body=None, origin=ORIGIN, token=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if origin:
            headers["Origin"] = origin
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        raw = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, (json.loads(raw) if raw else None), hdrs

    def subscribe(self, **kw):
        body = {"subscription": SUB, "tz": "America/Chicago", "prefs": {"time": "20:00"}}
        body.update(kw)
        return self.req("POST", "/vpush/v1/apps/vcounter/subscribe", body)

    def test_healthz_with_and_without_prefix(self):
        for p in ("/healthz", "/vpush/healthz"):
            s, b, _ = self.req("GET", p, origin=None)
            self.assertEqual((s, b["ok"]), (200, True))

    def test_key_and_cors(self):
        s, b, h = self.req("GET", "/v1/apps/vcounter/key")
        self.assertEqual(s, 200)
        self.assertEqual(b["publicKey"], self.app.vapid_public_key)
        self.assertEqual(h["access-control-allow-origin"], ORIGIN)
        s, _, h = self.req("OPTIONS", "/vpush/v1/apps/vcounter/subscribe")
        self.assertEqual(s, 204)
        self.assertIn("Authorization", h["access-control-allow-headers"])

    def test_origin_enforced(self):
        self.assertEqual(self.req("GET", "/v1/apps/vcounter/key", origin="https://evil.com")[0], 403)
        self.assertEqual(self.req("GET", "/v1/apps/vcounter/key", origin=None)[0], 403)
        _, _, h = self.req("GET", "/v1/apps/vcounter/key", origin="https://evil.com")
        self.assertNotIn("access-control-allow-origin", h)

    def test_unknown_app_and_route(self):
        self.assertEqual(self.req("GET", "/v1/apps/nope/key")[0], 404)
        self.assertEqual(self.req("GET", "/v1/whatever")[0], 404)
        self.assertEqual(self.req("GET", "/v1/apps/vcounter/subscribe")[0], 405)

    def test_subscribe_update_mark_unsubscribe(self):
        s, b, _ = self.subscribe()
        self.assertEqual(s, 200)
        sid, token = b["id"], b["token"]
        # Update with the token: same id, no new token, new prefs.
        s, b, _ = self.req("POST", "/v1/apps/vcounter/subscribe",
                           {"subscription": SUB, "tz": "America/Chicago", "prefs": {"time": "07:00"}}, token=token)
        self.assertEqual((s, b), (200, {"id": sid}))
        self.assertEqual(json.loads(self.env.db.get_subscriber(sid)["prefs"]), {"time": "07:00"})
        # Mark today (subscriber's local date).
        today = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
        s, _, _ = self.req("POST", "/v1/apps/vcounter/mark", {"name": "done", "date": today}, token=token)
        self.assertEqual(s, 200)
        self.assertTrue(self.env.db.has_mark(sid, "done", today))
        # Bad date / far date / bad token.
        self.assertEqual(self.req("POST", "/v1/apps/vcounter/mark", {"name": "done", "date": "nope"}, token=token)[0], 400)
        self.assertEqual(self.req("POST", "/v1/apps/vcounter/mark", {"name": "done", "date": "2020-01-01"}, token=token)[0], 400)
        self.assertEqual(self.req("POST", "/v1/apps/vcounter/mark", {"name": "done", "date": today}, token="wrong")[0], 401)
        # Unsubscribe.
        self.assertEqual(self.req("POST", "/v1/apps/vcounter/unsubscribe", {}, token=token)[0], 200)
        self.assertIsNone(self.env.db.get_subscriber(sid))
        self.assertEqual(self.req("POST", "/v1/apps/vcounter/unsubscribe", {}, token=token)[0], 401)

    def test_resubscribe_same_endpoint_replaces(self):
        _, a, _ = self.subscribe()
        _, b, _ = self.subscribe()
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(self.env.db.count("vcounter"), 1)

    def test_rejects_bad_input(self):
        bad_host = {"endpoint": "https://evil.com/x", "keys": SUB["keys"]}
        self.assertEqual(self.subscribe(subscription=bad_host)[0], 400)
        self.assertEqual(self.subscribe(tz="Mars/Olympus")[0], 400)
        self.assertEqual(self.subscribe(prefs="x")[0], 400)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/v1/apps/vcounter/subscribe", body=b"x" * 20000,
                  headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 413)
        c.close()

    def test_subscriber_limit(self):
        self.app.max_subscribers = 1
        self.assertEqual(self.subscribe()[0], 200)
        other = {"endpoint": APPLE + "other", "keys": SUB["keys"]}
        self.assertEqual(self.subscribe(subscription=other)[0], 429)

    def test_admin_send(self):
        _, b, _ = self.subscribe()
        self.assertEqual(self.req("POST", "/v1/apps/vcounter/send", {"payload": {"t": 1}})[0], 401)
        s, r, _ = self.req("POST", "/v1/apps/vcounter/send", {"payload": {"t": 1}}, origin=None,
                           token=self.app.admin_token)
        self.assertEqual((s, r["sent"]), (200, 1))
        s, r, _ = self.req("POST", "/v1/apps/vcounter/send", {"to": b["id"], "payload": {"t": 2}},
                           origin=None, token=self.app.admin_token)
        self.assertEqual(r["sent"], 1)
        self.assertEqual([c[2] for c in self.send.calls], [{"t": 1}, {"t": 2}])

    def test_keep_alive_after_rejected_request_with_body(self):
        """Browsers reuse connections: a request rejected before its body was read
        must not leave bytes behind that corrupt the next request."""
        _, b, _ = self.subscribe()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdr = {"Origin": ORIGIN, "Content-Type": "application/json", "Authorization": "Bearer wrong"}
        c.request("POST", "/vpush/v1/apps/vcounter/mark", body=b'{"name":"done","date":"2026-11-03"}', headers=hdr)
        r = c.getresponse(); r.read()
        self.assertEqual(r.status, 401)
        hdr["Authorization"] = "Bearer " + b["token"]
        c.request("POST", "/vpush/v1/apps/vcounter/unsubscribe", body=b"{}", headers=hdr)   # same connection
        r = c.getresponse(); r.read()
        self.assertEqual(r.status, 200)
        c.close()

    def test_odd_method_cannot_reach_actions(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("send", "/v1/apps/vcounter/send", body=b"{}", headers={"Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 501)
        c.close()


if __name__ == "__main__":
    unittest.main()
