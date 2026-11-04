FROM python:3.15-alpine

WORKDIR /app

# Dependencies first so code-only changes rebuild in seconds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vpush ./vpush

# Run as an unprivileged user (UID/GID 909) by default. Override with
# `user:` in compose if you want a different UID; it only needs write access
# to /data.
#
# Strip setuid/setgid bits from everything in the image: those are the only
# ordinary way a non-root process could gain privileges, so with them gone
# (plus no_new_privs, which the server sets on itself at startup) there are no
# capabilities left to drop. cap_drop/read_only/no-new-privileges are runtime
# settings Docker won't take from an image; see docker-compose.yml for those.
RUN addgroup -S -g 909 vpush \
 && adduser -S -D -H -u 909 -G vpush -s /sbin/nologin vpush \
 && mkdir -p /data \
 && chown 909:909 /data \
 && { find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true; } \
 && test -z "$(find / -xdev -perm /6000 -type f 2>/dev/null)"

ARG VPUSH_BUILD=dev
# One volume: /data/apps/*.toml (+ rule scripts) and /data/vpush.db.
ENV VPUSH_CONFIG=/data \
    VPUSH_DATA=/data \
    VPUSH_PORT=8080 \
    VPUSH_BUILD=${VPUSH_BUILD} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 909:909

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD wget -qO- "http://127.0.0.1:${VPUSH_PORT}/healthz" >/dev/null || exit 1

CMD ["python", "-m", "vpush"]
