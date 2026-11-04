"""Lock the process down at startup (Linux only; a no-op elsewhere).

Docker only takes cap_drop / read_only / no-new-privileges from the runtime
(compose or docker run), not from the image. So the server does what it can
for itself:

  * sets no_new_privs, the same flag `security_opt: no-new-privileges:true`
    sets: nothing this process (or anything it ever executes) runs can gain
    privileges through setuid/setgid binaries or file capabilities;
  * reports its user and effective capabilities, and warns if it's running as
    root or still holds capabilities (the image runs as UID 909, which has none).

Call it before starting any threads: the flag is inherited by threads created
afterwards.
"""

import ctypes
import logging
import os
import sys

log = logging.getLogger("vpush.security")

PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39


def _effective_caps() -> int:
    """CapEff from /proc/self/status as an int (0 = no capabilities), or -1."""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    return int(line.split()[1], 16)
    except (OSError, ValueError):
        pass
    return -1


def harden() -> dict:
    status = {"platform": sys.platform, "no_new_privs": None, "uid": None, "gid": None, "cap_eff": None}
    if not sys.platform.startswith("linux"):
        return status

    status["uid"], status["gid"] = os.geteuid(), os.getegid()
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            log.warning("could not set no_new_privs (errno %d)", ctypes.get_errno())
        status["no_new_privs"] = libc.prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) == 1
    except (OSError, AttributeError) as e:
        log.warning("could not set no_new_privs: %s", e)

    caps = _effective_caps()
    status["cap_eff"] = caps
    log.info("security: uid=%s gid=%s no_new_privs=%s capabilities=%s",
             status["uid"], status["gid"], status["no_new_privs"],
             "none" if caps == 0 else (hex(caps) if caps > 0 else "unknown"))
    if status["uid"] == 0:
        log.warning("running as root; the image defaults to UID 909, so something overrode it "
                    "(compose `user:` or `docker run -u`). Nothing here needs root.")
    elif caps > 0:
        log.warning("process holds capabilities %s; add `cap_drop: [ALL]` to the container", hex(caps))
    return status
