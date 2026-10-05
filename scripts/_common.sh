# shared helpers for the scripts in this folder
set -euo pipefail
cd "$(dirname "$0")/.."

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "Docker Compose is not installed (run scripts/setup-vps.sh)" >&2; exit 1
fi

# run the python program given on stdin inside the container (no python needed on the host);
# it gets get(path, method) for calling the API with the configured key, and $ARG as env ARG
in_container() {
  { cat <<'PY'
import json, os, urllib.request
def get(path, method="GET"):
    req = urllib.request.Request("http://127.0.0.1:8000" + path, method=method,
                                 headers={"X-API-Key": os.environ.get("API_KEY", "")})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)
PY
    cat; } | $COMPOSE exec -T -e ARG="${ARG:-}" middleware python -
}
