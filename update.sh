#!/bin/sh
# Pull the latest published image and restart. Run next to docker-compose.yml.
set -e
cd "$(dirname "$0")"
docker compose pull
docker compose up -d
docker image prune -f >/dev/null
docker compose ps
