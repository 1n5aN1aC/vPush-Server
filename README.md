# vPush-Server

A tiny self-hosted [Web Push](https://developer.mozilla.org/docs/Web/API/Push_API) server for your own web apps and PWAs, including iPhone Home Screen apps.

- **Multiple apps:** each has its own VAPID keys, subscriber list, allowed origins and rules.
- **Scheduled rules:** a built-in `daily` rule, or a small Python script for anything else.
- **Marks:** the app tells the server "done for today", so it doesn't nag.
- **Manual sends:** a protected endpoint for test or event-driven pushes.
- **Tiny:** Python standard library plus `pywebpush`, one SQLite file, and one path behind your reverse proxy.

```
app (browser/phone) ──subscribe/mark──▶ vPush-Server ──Web Push──▶ Apple/Google/Mozilla ──▶ device
```

The app only talks to the server occasionally: when enabling reminders, changing prefs, or posting a mark. Delivery goes from the server through the browser vendor's push service. Messages are end-to-end encrypted, so the push service can't read them.

---

## Deploy

The image is built by GitHub Actions on every push to `main` (the tests and an image smoke test must pass first). It's published for amd64 and arm64:

```
ghcr.io/1n5an1ac/vpush-server:latest      # newest main
ghcr.io/1n5an1ac/vpush-server:<sha>       # a specific commit
```

On your server:

```
vpush/
  docker-compose.yml      ← from this repo; set VPUSH_CONTACT
  update.sh               ← from this repo
  data/                   ← the one volume
    apps/vcounter.toml    ← your app files (+ rule scripts)
    vpush.db              ← created automatically
```

```bash
sudo chown -R 909:909 data  # the container runs as UID/GID 909 and must own data/
docker compose up -d        # first start: generates each app's keys into its .toml
./update.sh                 # later: pull the newest image and restart
```

### Security

The image runs as an unprivileged user, **UID/GID 909**, never as root.
- **No setuid/setgid programs:** every one is removed from the image, so a non-root process has no ordinary way to gain privileges.
- **Locks itself at startup:** the server sets Linux's `no_new_privs` flag on its own process and logs one line of status, e.g. `security: uid=909 gid=909 no_new_privs=True capabilities=none`. It warns if it's running as root or still holds capabilities.
- **Runtime protections:** Docker only accepts `cap_drop`, `read_only` and `no-new-privileges` at run time, not from an image. The provided `docker-compose.yml` turns all three on, and the server never writes outside `/data`.
- **Different UID:** add `user: "UID:GID"` in compose and `chown` `data/` to it.

CI builds the real image and checks all of this (user, setuid files, `no_new_privs`, capabilities) before anything is published.

Back up `data/`: `data/apps/` holds every app's keys, and `vpush.db` the subscribers. **Never change an app's VAPID keys:** every existing subscription is tied to them.

### Reverse proxy: one path

Everything lives under one path (`VPUSH_BASE_PATH`, default `/vpush`). Forward that path to the container. It doesn't matter whether your proxy strips the prefix.

**Caddy**
```
example.com {
    handle /vpush/* {
        reverse_proxy 127.0.0.1:8080
    }
    # ... the rest of your site
}
```

**nginx**
```nginx
location /vpush/ {
    proxy_pass http://127.0.0.1:8080;   # no trailing slash: path passed through unchanged
    proxy_set_header Host $host;
}
```

Check it: `https://example.com/vpush/healthz` → `{"ok": true, ...}`.

### Settings (environment)

| Variable | Default | |
|---|---|---|
| `VPUSH_BASE_PATH` | `/vpush` | The single path the server lives under |
| `VPUSH_CONTACT` | `mailto:admin@example.com` | VAPID contact (`mailto:` or `https:`); push services may reject placeholders |
| `VPUSH_PORT` | `8080` | |
| `VPUSH_CONFIG` / `VPUSH_DATA` | `/data` / `/data` | Where `apps/` and `vpush.db` live; set them apart to split config from the database |
| `VPUSH_PUSH_HOSTS` | | Extra push-service hosts to accept, comma separated |
| `VPUSH_LOG` | `INFO` | |

---

## Apps

One file per app: `data/apps/<app-id>.toml`. The file name is the app id (lowercase letters, digits, `-`, `_`). Adding, editing or removing a file is picked up within a minute, with no restart. See [`examples/apps/vcounter.toml`](examples/apps/vcounter.toml).

```toml
name = "vCounter"
origins = ["https://counter.example.com"]   # pages allowed to call this app's endpoints
# max_subscribers = 1000

[[rules]]
id = "daily"
type = "daily"
default_time = "20:00"
skip_if_mark = "done"
once_per = "day"
payload = { kind = "daily-reminder", title = "vCounter", body = "Don't forget your dailies" }
```

On first start the server **appends** a `[generated]` section with the app's admin token and VAPID key pair. Nothing else in the file is touched. To regenerate, delete the whole section. That breaks existing subscriptions.

### Rules

Every rule answers one question, about a minute at a time, per subscriber: *send now?* The server only asks if this rule hasn't already sent to that subscriber in the current `once_per` period (`"day"` or `"hour"`, in the subscriber's own time zone). Rules never have to track repeats themselves.

Common options for any rule:

| | |
|---|---|
| `id` | stable name (used to remember what was sent) |
| `once_per` | `"day"` (default) or `"hour"` |
| `ttl` | seconds the push service holds the message if the device is offline (default 43200) |
| `urgency` | `very-low`, `low`, `normal`, `high` |

**`type = "daily"`** sends `payload` once a day at the subscriber's `prefs.time` (`"HH:MM"`), or `default_time`. It's skipped if the subscriber has today's `skip_if_mark`. It isn't sent on the day someone subscribes after that day's time has passed. `pref = "..."` changes which prefs key holds the time.

**`type = "script"`** calls `should_send(sub)` in `data/apps/<app-id>.py`, or `file = "..."` / `function = "..."`. It returns a payload dict or `None`. Edits are picked up on the next tick, and exceptions are logged and skipped. See [`examples/apps/example-rule.py`](examples/apps/example-rule.py).

```python
def should_send(sub):
    now = sub.local_now()
    if now.weekday() == 6 and now.hour >= 18 and not sub.has_mark("weekly-review"):
        return {"title": "Weekly review", "body": "Look back on your week."}
    return None
```

`sub` gives you `id`, `prefs`, `tz`, `local_now()`, `today()`, `has_mark(name, date=None)` and `created`.

---

## API

All routes are under the base path (e.g. `https://example.com/vpush/v1/...`). Everything is JSON. Subscriber calls need the page's `Origin` to be in the app's `origins`, and get CORS headers for it.

| | | |
|---|---|---|
| `GET /v1/apps/{app}/key` | | `{"publicKey"}`, the VAPID key for `pushManager.subscribe` |
| `POST /v1/apps/{app}/subscribe` | `{subscription, tz, prefs}` | `{"id","token"}`. Send again with `Authorization: Bearer <token>` to update (rotated subscription, new prefs or time zone) |
| `POST /v1/apps/{app}/mark` | `{name, date}` + subscriber token | `date` is the device's local `YYYY-MM-DD` (±1 day of the subscriber's) |
| `POST /v1/apps/{app}/unsubscribe` | subscriber token | |
| `POST /v1/apps/{app}/send` | `{to: "all" \| id, payload, ttl?}` + **admin** token | `{"sent","failed","removed"}` |
| `GET /healthz` | | |

Subscriptions the push service reports as gone (404/410) are deleted automatically.

```bash
# Send a test push to everyone subscribed to an app
curl -X POST https://example.com/vpush/v1/apps/vcounter/send \
  -H "Authorization: Bearer $(docker exec vpush python -m vpush token vcounter)" \
  -H "Content-Type: application/json" \
  -d '{"to":"all","payload":{"title":"Test","body":"Hello from vPush"}}'
```

---

## Client

Copy [`client/vpush-client.js`](client/vpush-client.js) into your app (no dependencies):

```js
const push = VPush({ server: "https://example.com/vpush", app: "vcounter" });

settingsToggle.onclick = () => push.enable({ time: "20:00" });   // must be inside a tap
push.update({ time: "07:30" });                                   // change prefs
push.mark("done", "2026-11-03");                                  // queued and retried if offline
push.sync();                 // on every launch/foreground: re-checks the subscription, flushes marks
push.disable();
```

Your service worker shows the notification. **Every push must show one.** iOS revokes the subscription of apps that receive pushes silently.

```js
self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil(self.registration.showNotification(data.title || "Reminder", { body: data.body, data }));
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow("./"));
});
```

The worker can also build the text from the app's own data (e.g. IndexedDB) instead of using the payload text.

**iPhone/iPad:** push only works for web apps added to the Home Screen (iOS 16.4+), and permission must be requested from a tap. If an installed app ever stops showing the keyboard or notifications while Safari works, restarting the device clears a known iOS bug.

---

## Command line

```bash
docker exec vpush python -m vpush apps            # apps, subscriber counts, rules
docker exec vpush python -m vpush token vcounter  # admin token
docker exec vpush python -m vpush key vcounter    # VAPID public key
```

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # (bin/ on Linux/macOS)
python -m unittest discover -s tests -v
```

The tests include a real end-to-end check: a push is encrypted and signed exactly as it would be sent, then decrypted with the subscriber's key and the VAPID signature verified.

To run locally, point `VPUSH_CONFIG`/`VPUSH_DATA` at a scratch folder (`dev/` is git-ignored) and run `python -m vpush`. [`client/demo/`](client/demo/) is a test page: serve the `client/` folder and add its origin to an app. Desktop Chrome supports push on `http://localhost`.

Releases: bump `__version__` in `vpush/__init__.py`. Pushing a `v1.2.3` tag also publishes `:1.2.3`.
