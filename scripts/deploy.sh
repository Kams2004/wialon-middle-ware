#!/usr/bin/env bash
# Pull the latest code, rebuild and restart the container, then wait until it is healthy.
source "$(dirname "$0")/_common.sh"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — fill in the values (nano .env) and run this again."
  exit 1
fi
grep -q '^API_KEY=..*' .env || echo "WARNING: API_KEY is empty in .env; the API is unprotected." >&2

git pull --ff-only
$COMPOSE up -d --build --remove-orphans
docker image prune -f >/dev/null

echo -n "Waiting for the service to become healthy"
for _ in $(seq 1 30); do
  state=$(docker inspect -f '{{.State.Health.Status}}' wialon-middleware 2>/dev/null || echo starting)
  if [ "$state" = "healthy" ]; then echo " ✔"; $COMPOSE ps; exit 0; fi
  echo -n "."; sleep 3
done
echo; echo "Service is not healthy yet ($state). Recent logs:" >&2
$COMPOSE logs --tail 50 middleware
exit 1
