"""End-to-end check of real push encryption and VAPID signing, offline.

Sends through vpush.push.send (the real pywebpush code path), captures the HTTP
request instead of letting it hit the network, then decrypts it with the
subscriber's private key and verifies the VAPID JWT with the app's public key,
exactly as a browser and a push service would.
"""

import base64
import json
import os
import unittest
from unittest import mock

import http_ece
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from vpush import config as cfg
from vpush import push


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeResponse:
    def __init__(self, status):
        self.status_code = status
        self.text = ""
        self.headers = {}
        self.reason = "Gone" if status == 410 else "Created"


class PushCryptoTests(unittest.TestCase):
    def setUp(self):
        keys = cfg.generate_keys()
        self.app = cfg.App(id="vcounter", name="vCounter", origins=[], rules=[],
                           admin_token=keys["admin_token"],
                           vapid_public_key=keys["vapid_public_key"],
                           vapid_private_key=keys["vapid_private_key"])
        # The browser's side of a subscription.
        self.ua_private = ec.generate_private_key(ec.SECP256R1())
        ua_public = self.ua_private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.auth = os.urandom(16)
        self.row = {
            "id": "sub1",
            "endpoint": "https://web.push.apple.com/QGuQyavXutnMHT4u8nKkTAfQ",
            "p256dh": cfg.b64url(ua_public),
            "auth": cfg.b64url(self.auth),
        }

    def capture(self, status=201):
        captured = {}

        def fake_post(url, data=None, headers=None, timeout=None, **kw):
            captured.update(url=url, data=data, headers=headers, timeout=timeout)
            return FakeResponse(status)

        return captured, mock.patch("requests.post", side_effect=fake_post)

    def test_payload_decrypts_and_vapid_verifies(self):
        captured, patch = self.capture()
        with patch:
            status = push.send(self.app, self.row, {"kind": "daily-reminder", "n": 2},
                               "mailto:test@example.com", ttl=600, urgency="high")
        self.assertEqual(status, 201)
        self.assertEqual(captured["url"], self.row["endpoint"])
        h = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(h["ttl"], "600")
        self.assertEqual(h["urgency"], "high")
        self.assertEqual(h["content-encoding"], "aes128gcm")

        # 1) The browser can decrypt the payload.
        plain = http_ece.decrypt(captured["data"], private_key=self.ua_private,
                                 auth_secret=self.auth, version="aes128gcm")
        self.assertEqual(json.loads(plain), {"kind": "daily-reminder", "n": 2})

        # 2) The push service can verify the VAPID token: "vapid t=<jwt>, k=<our public key>".
        auth = h["authorization"]
        self.assertTrue(auth.startswith("vapid "))
        parts = dict(p.strip().split("=", 1) for p in auth[6:].split(","))
        self.assertEqual(parts["k"], self.app.vapid_public_key)
        header_b64, claims_b64, sig_b64 = parts["t"].split(".")
        claims = json.loads(b64d(claims_b64))
        self.assertEqual(claims["aud"], "https://web.push.apple.com")
        self.assertEqual(claims["sub"], "mailto:test@example.com")
        self.assertIn("exp", claims)
        sig = b64d(sig_b64)
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64d(self.app.vapid_public_key))
        pub.verify(der, f"{header_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if bad

    def test_claims_not_reused_across_push_services(self):
        """aud must follow each endpoint; a shared claims dict would leak Apple's aud to Google."""
        captured, patch = self.capture()
        with patch:
            push.send(self.app, self.row, {"a": 1}, "mailto:x@example.com")
            row2 = dict(self.row, endpoint="https://fcm.googleapis.com/fcm/send/abc")
            push.send(self.app, row2, {"a": 2}, "mailto:x@example.com")
        auth = {k.lower(): v for k, v in captured["headers"].items()}["authorization"]
        token = dict(p.strip().split("=", 1) for p in auth[6:].split(","))["t"]
        self.assertEqual(json.loads(b64d(token.split(".")[1]))["aud"], "https://fcm.googleapis.com")

    def test_gone_status_is_returned(self):
        _, patch = self.capture(status=410)
        with patch:
            self.assertEqual(push.send(self.app, self.row, {"a": 1}, "mailto:x@example.com"), 410)


if __name__ == "__main__":
    unittest.main()
