"""Loading apps from /config/apps/<id>.toml.

One file per app. On first load, any app whose file has no [generated] section
gets one appended (admin token + VAPID key pair). Nothing else in the file is
ever rewritten, so comments and formatting survive.
"""

import base64
import logging
import os
import re
import secrets
import tomllib
from dataclasses import dataclass, field

log = logging.getLogger("vpush.config")

APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ONCE_PER = ("day", "hour")
GENERATED_KEYS = ("admin_token", "vapid_public_key", "vapid_private_key")


class ConfigError(Exception):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class Rule:
    id: str
    type: str                      # "daily" | "script"
    once_per: str = "day"
    ttl: int = 43200               # seconds the push service keeps it if the device is offline
    urgency: str = "normal"
    options: dict = field(default_factory=dict)   # the raw [[rules]] table


@dataclass
class App:
    id: str
    name: str
    origins: list
    rules: list
    admin_token: str
    vapid_public_key: str          # base64url uncompressed P-256 point (applicationServerKey)
    vapid_private_key: str         # base64url raw 32-byte private scalar
    max_subscribers: int = 1000
    path: str = ""                 # the .toml file
    script_path: str = ""          # the .py file, if any

    def origin_allowed(self, origin: str) -> bool:
        return "*" in self.origins or origin in self.origins


def generate_keys() -> dict:
    """A fresh admin token and VAPID (P-256) key pair."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    raw_private = private.private_numbers().private_value.to_bytes(32, "big")
    raw_public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return {
        "admin_token": secrets.token_urlsafe(32),
        "vapid_public_key": b64url(raw_public),
        "vapid_private_key": b64url(raw_private),
    }


def _append_generated(path: str) -> None:
    keys = generate_keys()
    block = (
        "\n\n[generated]\n"
        "# Written by vPush-Server on first start. Keep this file private.\n"
        "# Changing the VAPID keys breaks every existing subscription for this app.\n"
        + "".join(f'{k} = "{keys[k]}"\n' for k in GENERATED_KEYS)
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(block)
    log.info("generated admin token and VAPID keys for %s", os.path.basename(path))


def _parse_rules(app_id: str, raw_rules) -> list:
    if raw_rules is None:
        return []
    if not isinstance(raw_rules, list):
        raise ConfigError(f"{app_id}: [[rules]] must be an array of tables")
    rules = []
    seen = set()
    for i, r in enumerate(raw_rules):
        if not isinstance(r, dict):
            raise ConfigError(f"{app_id}: rule {i} is not a table")
        rtype = r.get("type")
        if rtype not in ("daily", "script"):
            raise ConfigError(f'{app_id}: rule {i} has unknown type {rtype!r} (use "daily" or "script")')
        rid = str(r.get("id") or f"rule{i}")
        if rid in seen:
            raise ConfigError(f"{app_id}: duplicate rule id {rid!r}")
        seen.add(rid)
        once = r.get("once_per", "day")
        if once not in ONCE_PER:
            raise ConfigError(f"{app_id}: rule {rid} once_per must be one of {ONCE_PER}")
        urgency = r.get("urgency", "normal")
        if urgency not in ("very-low", "low", "normal", "high"):
            raise ConfigError(f"{app_id}: rule {rid} urgency is invalid")
        rules.append(Rule(id=rid, type=rtype, once_per=once, ttl=int(r.get("ttl", 43200)),
                          urgency=urgency, options=r))
    return rules


def load_app(path: str) -> App:
    app_id = os.path.splitext(os.path.basename(path))[0]
    if not APP_ID_RE.match(app_id):
        raise ConfigError(f"{path}: file name must be a lowercase app id (a-z, 0-9, - and _)")

    with open(path, "rb") as f:
        data = tomllib.load(f)
    gen = data.get("generated")
    if gen is None:
        _append_generated(path)
        with open(path, "rb") as f:
            data = tomllib.load(f)
        gen = data["generated"]
    missing = [k for k in GENERATED_KEYS if not gen.get(k)]
    if missing:
        raise ConfigError(f"{app_id}: [generated] is missing {', '.join(missing)} "
                          "(delete the whole [generated] section to regenerate)")

    origins = data.get("origins", [])
    if not isinstance(origins, list) or not all(isinstance(o, str) for o in origins):
        raise ConfigError(f"{app_id}: origins must be a list of strings")

    script = os.path.splitext(path)[0] + ".py"
    return App(
        id=app_id,
        name=str(data.get("name", app_id)),
        origins=[o.rstrip("/") for o in origins],
        rules=_parse_rules(app_id, data.get("rules")),
        admin_token=gen["admin_token"],
        vapid_public_key=gen["vapid_public_key"],
        vapid_private_key=gen["vapid_private_key"],
        max_subscribers=int(data.get("max_subscribers", 1000)),
        path=path,
        script_path=script if os.path.exists(script) else "",
    )


def apps_dir(config_dir: str) -> str:
    return os.path.join(config_dir, "apps")


def signature(config_dir: str) -> tuple:
    """Changes whenever an app file is added, removed or edited."""
    d = apps_dir(config_dir)
    if not os.path.isdir(d):
        return ()
    out = []
    for name in sorted(os.listdir(d)):
        if name.endswith((".toml", ".py")):
            st = os.stat(os.path.join(d, name))
            out.append((name, st.st_mtime_ns, st.st_size))
    return tuple(out)


def load_apps(config_dir: str) -> dict:
    """All valid apps. A broken file is logged and skipped, never fatal."""
    d = apps_dir(config_dir)
    os.makedirs(d, exist_ok=True)
    apps = {}
    for name in sorted(os.listdir(d)):
        if not name.endswith(".toml"):
            continue
        try:
            app = load_app(os.path.join(d, name))
            apps[app.id] = app
        except (ConfigError, tomllib.TOMLDecodeError, OSError, ValueError) as e:
            log.error("skipping %s: %s", name, e)
    return apps
