"""Entry point.

  python -m vpush                 run the server (HTTP + scheduler)
  python -m vpush apps            list apps, subscriber counts and rules
  python -m vpush token <app>     print an app's admin token
  python -m vpush key <app>       print an app's VAPID public key
"""

import logging
import os
import signal
import sys

from . import __version__
from .config import load_apps
from .db import DB
from .scheduler import Registry, Scheduler
from .server import make_server
from .settings import Settings


def _db(settings):
    return DB(os.path.join(settings.data_dir, "vpush.db"))


def serve(settings):
    db = _db(settings)
    registry = Registry(settings)
    registry.reload_if_changed(force=True)
    scheduler = Scheduler(registry, db)
    scheduler.start()
    httpd = make_server(settings, registry, db)
    log = logging.getLogger("vpush")
    log.info("vPush %s (%s) on %s:%d, base path %r", __version__, settings.build,
             settings.host, settings.port, settings.base_path or "/")
    if settings.contact == "mailto:admin@example.com":
        log.warning("VPUSH_CONTACT is not set; push services may reject the placeholder address")

    def stop(*_):
        scheduler.stop_event.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        log.info("stopped")


def main(argv):
    logging.basicConfig(level=os.environ.get("VPUSH_LOG", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    cmd = argv[0] if argv else "serve"
    if cmd == "serve":
        return serve(settings)
    if cmd in ("token", "key"):
        if len(argv) != 2:
            print(f"usage: python -m vpush {cmd} <app>", file=sys.stderr)
            return 2
        app = load_apps(settings.config_dir).get(argv[1])
        if not app:
            print(f"unknown app {argv[1]!r}", file=sys.stderr)
            return 1
        print(app.admin_token if cmd == "token" else app.vapid_public_key)
        return 0
    if cmd == "apps":
        db = _db(settings)
        for app in load_apps(settings.config_dir).values():
            rules = ", ".join(f"{r.id}:{r.type}/{r.once_per}" for r in app.rules) or "no rules"
            print(f"{app.id:<16} {db.count(app.id):>5} subscribers   {rules}   origins: {', '.join(app.origins) or '-'}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
