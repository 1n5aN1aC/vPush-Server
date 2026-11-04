"""Sending one push. The only place that talks to pywebpush."""

import json
import logging

log = logging.getLogger("vpush.push")

# Statuses that mean the subscription is gone for good.
GONE = (404, 410)


def send(app, row, payload: dict, contact: str, ttl: int = 43200, urgency: str = "normal") -> int:
    """Deliver payload to one subscriber. Returns the push service's HTTP status
    (201 is success), or 0 if the request never got a response."""
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush

    # from_raw takes the base64url text of the raw key (as bytes) and decodes it itself.
    vapid = Vapid.from_raw(app.vapid_private_key.encode())
    try:
        resp = webpush(
            subscription_info={"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}},
            data=json.dumps(payload, separators=(",", ":")),
            vapid_private_key=vapid,
            # A fresh dict every time: pywebpush writes aud/exp into it, and
            # aud differs per push service.
            vapid_claims={"sub": contact},
            ttl=ttl,
            headers={"Urgency": urgency},
            timeout=15,
        )
        return getattr(resp, "status_code", 201)
    except WebPushException as e:
        status = getattr(e.response, "status_code", 0) if e.response is not None else 0
        body = ""
        try:
            body = e.response.text[:200] if e.response is not None else ""
        except Exception:
            pass
        log.warning("push to %s failed: %s %s", row["id"], status, body or e)
        return status
    except Exception as e:  # network errors, timeouts
        log.warning("push to %s failed: %s", row["id"], e)
        return 0
