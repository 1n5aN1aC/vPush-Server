FROM python:3.15-alpine

WORKDIR /app

# Dependencies first so code-only changes rebuild in seconds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vpush ./vpush

ARG VPUSH_BUILD=dev
# One volume: /data/apps/*.toml (+ rule scripts) and /data/vpush.db.
ENV VPUSH_CONFIG=/data \
    VPUSH_DATA=/data \
    VPUSH_PORT=8080 \
    VPUSH_BUILD=${VPUSH_BUILD} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD wget -qO- "http://127.0.0.1:${VPUSH_PORT}/healthz" >/dev/null || exit 1

CMD ["python", "-m", "vpush"]
