#!/usr/bin/env bash
# =============================================================================
# OceanFrame container start-up for the Oracle stack (docker-compose.stack.yml)
# =============================================================================
#   1. Wait until the application database answers.
#   2. Bring its schema up to date (idempotent) and seed the first admin.
#   3. Start the app.
#
# Compose already waits for Oracle's own healthcheck; this is the app-side
# half, because "Oracle is healthy" and "the app user can log in to the PDB"
# are not quite the same moment. Invoked as `bash docker-entrypoint.sh`, so the
# file needs no exec bit (it is written on Windows as often as not).
# =============================================================================
set -euo pipefail

if [[ -n "${OCEANFRAME_DB_USER:-}" && -z "${OCEANFRAME_DB_PASSWORD:-}" ]]; then
    echo "✗ OCEANFRAME_DB_PASSWORD is empty: set APP_SCHEMA_PASSWORD (and ORACLE_PASSWORD) in .env, or run make env"
    exit 1
fi

RETRIES="${OCEANFRAME_DB_WAIT_RETRIES:-60}"
INTERVAL="${OCEANFRAME_DB_WAIT_INTERVAL:-5}"

echo "OceanFrame: waiting for the application database..."
for ((i = 1; i <= RETRIES; i++)); do
    if python -m appdb.check 2>/dev/null; then
        echo "  ✓ database is answering"
        break
    fi
    if [[ "$i" -eq "$RETRIES" ]]; then
        echo "  ✗ database did not answer after $((RETRIES * INTERVAL))s — last error:"
        python -m appdb.check || true
        exit 1
    fi
    echo "  attempt $i/$RETRIES — not ready, retrying in ${INTERVAL}s"
    sleep "$INTERVAL"
done

python -m appdb.bootstrap

exec python launch.py
