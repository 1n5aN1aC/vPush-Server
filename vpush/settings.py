"""Process-wide settings, all from environment variables."""

import os
from dataclasses import dataclass, field


def _clean_base(path: str) -> str:
    path = "/" + path.strip().strip("/")
    return "" if path == "/" else path


@dataclass
class Settings:
    config_dir: str = field(default_factory=lambda: os.environ.get("VPUSH_CONFIG", "/config"))
    data_dir: str = field(default_factory=lambda: os.environ.get("VPUSH_DATA", "/data"))
    base_path: str = field(default_factory=lambda: _clean_base(os.environ.get("VPUSH_BASE_PATH", "/vpush")))
    host: str = field(default_factory=lambda: os.environ.get("VPUSH_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("VPUSH_PORT", "8080")))
    # VAPID "sub" claim: how push services can contact you. Must be mailto: or https:.
    contact: str = field(default_factory=lambda: os.environ.get("VPUSH_CONTACT", "mailto:admin@example.com"))
    # Extra push-service hosts to accept, comma separated (suffix match).
    extra_push_hosts: list = field(default_factory=lambda: [
        h.strip().lower() for h in os.environ.get("VPUSH_PUSH_HOSTS", "").split(",") if h.strip()
    ])
    tick_seconds: int = field(default_factory=lambda: int(os.environ.get("VPUSH_TICK", "60")))
    build: str = field(default_factory=lambda: os.environ.get("VPUSH_BUILD", "dev"))
