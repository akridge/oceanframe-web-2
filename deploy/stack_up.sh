#!/usr/bin/env bash
# =============================================================================
# OceanFrame on any Docker host: Docker Desktop, a Linux VM, a laptop.
# =============================================================================
# The same stack as the Cloud Workstation installer (Oracle, the app and the
# evaluation worker, with the NOAA Pacific T3 starter data), without its host
# setup: Docker must already be installed. Data stays in named volumes.
#
#   bash deploy/stack_up.sh                 start, writing .env on first run
#   bash deploy/stack_up.sh --fresh         delete OceanFrame's containers and
#                                           volumes (asks first) and start over
#   bash deploy/stack_up.sh --fresh --yes   the same, without asking
#
# Settings (environment variables, all optional):
#   OCEANFRAME_HOST_PORT   port the app is served on            (default: 8080)
#   OCEANFRAME_EVAL_GPU    auto | on | off — run the evaluation worker on an
#                          NVIDIA GPU                           (default: auto)
#   OCEANFRAME_SEED_NOAA   yes | no — pull the T3 starter data  (default: yes)
#
# Only OceanFrame's own resources (the oceanframe-stack project and its
# oceanframe-stack_* volumes) are ever removed; other projects are untouched.
# =============================================================================
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=lib/make_env.sh
. deploy/lib/make_env.sh

FRESH=no ASSUME_YES=no
for arg in "$@"; do
    case "$arg" in
        --fresh) FRESH=yes ;;
        --yes|-y) ASSUME_YES=yes ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg (see --help)"; exit 1 ;;
    esac
done

PROJECT=oceanframe-stack
FILES=(-f docker-compose.stack.yml)
case "${OCEANFRAME_EVAL_GPU:-auto}" in
    on) FILES+=(-f docker-compose.evaluation.gpu.yml) ;;
    auto) command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1 \
              && FILES+=(-f docker-compose.evaluation.gpu.yml) ;;
    off) ;;
    *) echo "OCEANFRAME_EVAL_GPU must be auto, on or off"; exit 1 ;;
esac
compose() { docker compose -p "$PROJECT" "${FILES[@]}" "$@"; }

wait_healthy() {  # container, tries, seconds between
    local state
    for _ in $(seq 1 "$2"); do
        state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null || true)"
        [ "$state" = healthy ] && return 0
        sleep "$3"
    done
    return 1
}

docker info >/dev/null 2>&1 || { echo "Docker is not running."; exit 1; }

if [ "$FRESH" = yes ]; then
    volumes="$(docker volume ls -q --filter "name=^${PROJECT}_" || true)"
    containers="$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT}" || true)"
    echo "This deletes OceanFrame's containers and these volumes (users, curation, the library catalog):"
    echo "${volumes:-  (no volumes)}" | sed 's/^/  /'
    if [ "$ASSUME_YES" != yes ]; then
        read -r -p "Type 'delete' to continue: " answer
        [ "$answer" = delete ] || { echo "Nothing was deleted."; exit 1; }
    fi
    [ -n "$containers" ] && docker rm -f $containers >/dev/null
    [ -n "$volumes" ] && docker volume rm $volumes >/dev/null
    if [ -f .env ]; then
        backup=".env.bak-$(date +%Y%m%d-%H%M%S)"
        mv .env "$backup"
        echo "  Moved the old .env to $backup (its passwords belonged to the deleted database)."
    fi
fi

NEW_ENV=no
if [ ! -f .env ]; then
    oceanframe_env "${OCEANFRAME_HOST_PORT:-8080}" > .env
    chmod 600 .env 2>/dev/null || true
    NEW_ENV=yes
    echo "  Wrote .env with generated passwords."
else
    for key in ORACLE_PASSWORD APP_SCHEMA_PASSWORD OCEANFRAME_ADMIN_PASSWORD; do
        grep -qE "^${key}=.+" .env || { echo "  .env has no ${key}: add it, or start over with --fresh."; exit 1; }
    done
fi
PORT="$(grep -E '^OCEANFRAME_HOST_PORT=' .env | tail -1 | cut -d= -f2-)"; PORT="${PORT:-8080}"

echo "Building images (the first build downloads torch: allow 10-20 minutes)..."
compose build
echo "Starting Oracle (a new database takes a few minutes)..."
compose up -d oceanframe-oracle
wait_healthy oceanframe-oracle 90 5 || { compose logs --tail=80 oceanframe-oracle; echo "Oracle did not become healthy"; exit 1; }
echo "Starting the app and the evaluation worker..."
compose up -d oceanframe-app oceanframe-evaluation
wait_healthy oceanframe-app 60 5 || { compose logs --tail=80 oceanframe-app; echo "The app did not become healthy"; exit 1; }

echo
echo "OceanFrame is running: http://localhost:${PORT}"
echo "  Compose:  docker compose -p ${PROJECT} ${FILES[*]} <command>"
if grep -q '^CURATION_STARTER_DATA=noaa-t3' .env; then
    echo "  Curation: the NOAA Pacific T3 starter projects import in the background; the T3"
    echo "            classifier is then scored once on the test and val splits for the matrix."
fi
if [ "$NEW_ENV" = yes ]; then
    echo "  Accounts (shown once; also in .env):"
    oceanframe_accounts .env
fi
