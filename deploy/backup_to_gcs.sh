#!/usr/bin/env bash
# =============================================================================
# OceanFrame backup — the Oracle schema, the library catalog, the config
# =============================================================================
# Ported from CAT's scripts/backup_to_gcs.sh. Makes a timestamped folder with:
#
#   oracle-export/oceanframe.dmp   Data Pump export of the app schema: users,
#                                  curation projects, every decision and its
#                                  audit trail. Consistent while running.
#   library/catalog.sqlite         the library catalog (collections and their
#                                  hand-entered metadata), via SQLite's online
#                                  backup, plus the vector index files
#   config/                        compose files and a manifest (.env only with
#                                  --include-env: it holds the passwords)
#   oracle-data.tgz                raw datafiles, only with --cold (stops the
#                                  stack while copying — a live copy of Oracle's
#                                  files is not a backup you can trust)
#
# and uploads it to gs://BUCKET/PREFIX/<timestamp>/.
#
#   deploy/backup_to_gcs.sh -b gs://my-bucket                  back up and upload
#   deploy/backup_to_gcs.sh --local-only                       keep it on disk
#   deploy/backup_to_gcs.sh -b gs://my-bucket --cold           also the raw datafiles
#   deploy/backup_to_gcs.sh -b gs://my-bucket --dry-run        show the plan
#
# Restore the schema into a running stack:
#   docker cp ./oceanframe.dmp oceanframe-oracle:/tmp/of_backup/
#   docker exec oceanframe-oracle bash -c 'impdp "system/$ORACLE_PASSWORD@FREEPDB1" \
#       schemas=OCEANFRAME directory=OF_BACKUP_DIR dumpfile=oceanframe.dmp \
#       table_exists_action=replace logfile=oceanframe_import.log'
# (use the schema name from APP_SCHEMA_NAME in .env, upper-cased). Into an
# existing stack it reports one error, ORA-31684 "user already exists": that is
# expected and harmless — the tables and rows are replaced. Verified: a deleted
# project comes back, with users and sessions intact.
# =============================================================================
set -euo pipefail

ORACLE_CONTAINER="oceanframe-oracle"
APP_CONTAINER="oceanframe-app"
DB_SERVICE="FREEPDB1"

GCS_BUCKET=""
GCS_PREFIX="oceanframe-backups"
BACKUP_ROOT=""
LOCAL_ONLY=false
COLD=false
INCLUDE_ENV=false
KEEP_LOCAL=false
DRY_RUN=false

step() { echo; echo "━━━ $* ━━━"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠️  $*"; }
die()  { echo "  ✗ $*"; exit 1; }

usage() {
    sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'
    cat <<USAGE

Options:
  -b, --bucket gs://BUCKET   where to upload (required unless --local-only)
  -p, --prefix PREFIX        folder in the bucket (default: oceanframe-backups)
  --backup-root DIR          local staging folder (default: <data dir>/backups)
  --local-only               do not upload
  --cold                     also copy Oracle's raw datafiles (stops the stack meanwhile)
  --include-env              also copy .env (contains passwords — mind the bucket's ACL)
  --keep-local               keep the local copy after uploading
  --dry-run                  print the plan and stop
USAGE
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -b|--bucket)   GCS_BUCKET="${2%/}"; shift 2 ;;
        -p|--prefix)   GCS_PREFIX="${2%/}"; shift 2 ;;
        --backup-root) BACKUP_ROOT="$2"; shift 2 ;;
        --local-only)  LOCAL_ONLY=true; shift ;;
        --cold)        COLD=true; shift ;;
        --include-env) INCLUDE_ENV=true; shift ;;
        --keep-local)  KEEP_LOCAL=true; shift ;;
        --dry-run)     DRY_RUN=true; shift ;;
        -h|--help)     usage ;;
        *) die "Unknown option: $1 (see -h)" ;;
    esac
done

# The deployment directory: where .env and the compose files are.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
[[ -f "$ENV_FILE" ]] || die "No .env in $PROJECT_DIR — run this from the OceanFrame deployment"
env_get() { { grep -E "^$1=" "$ENV_FILE" || true; } | tail -1 | cut -d= -f2-; }  # a missing key is not an error

ORACLE_PASSWORD="$(env_get ORACLE_PASSWORD)"
SCHEMA="$(env_get APP_SCHEMA_NAME)"; SCHEMA="${SCHEMA:-oceanframe}"; SCHEMA="${SCHEMA^^}"
ORACLE_DATA="$(env_get OCEANFRAME_ORACLE_DATA)"
LIBRARY_DATA="$(env_get OCEANFRAME_LIBRARY_DATA)"
[[ -n "$ORACLE_PASSWORD" ]] || die "ORACLE_PASSWORD is not set in .env"

if [[ -z "$BACKUP_ROOT" ]]; then
    if [[ -n "$LIBRARY_DATA" ]]; then BACKUP_ROOT="$(dirname "$LIBRARY_DATA")/backups"; else BACKUP_ROOT="$PROJECT_DIR/backups"; fi
fi
$LOCAL_ONLY || [[ "$GCS_BUCKET" == gs://* ]] || die "Give a bucket with -b gs://... (or use --local-only)"

STAMP="$(date +%Y-%m-%d_%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"

step "Plan"
echo "  schema:      $SCHEMA  (container $ORACLE_CONTAINER)"
echo "  local:       $BACKUP_DIR"
$LOCAL_ONLY || echo "  upload to:   $GCS_BUCKET/$GCS_PREFIX/$STAMP/"
$COLD && echo "  raw files:   $ORACLE_DATA (the stack is stopped while copying)"
$INCLUDE_ENV && warn ".env (passwords) will be included"
$DRY_RUN && { echo; echo "Dry run — nothing done."; exit 0; }

step "Pre-flight"
$DOCKER inspect "$ORACLE_CONTAINER" >/dev/null 2>&1 || die "$ORACLE_CONTAINER is not running — start the stack first"
ok "Oracle container found ($($DOCKER inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$ORACLE_CONTAINER"))"
mkdir -p "$BACKUP_DIR"/{oracle-export,library,config}

step "1/4 Oracle schema (Data Pump)"
# The password goes in through the environment, not the command line, so it
# does not show in `ps` on the host.
$DOCKER exec -e OF_PW="$ORACLE_PASSWORD" "$ORACLE_CONTAINER" bash -c "
    mkdir -p /tmp/of_backup && rm -f /tmp/of_backup/oceanframe.*
    sqlplus -s \"system/\${OF_PW}@${DB_SERVICE}\" >/dev/null <<SQL
WHENEVER SQLERROR CONTINUE
CREATE OR REPLACE DIRECTORY of_backup_dir AS '/tmp/of_backup';
EXIT;
SQL
    expdp \"system/\${OF_PW}@${DB_SERVICE}\" schemas=${SCHEMA} directory=OF_BACKUP_DIR \
        dumpfile=oceanframe.dmp logfile=oceanframe.log reuse_dumpfiles=yes" \
    > "$BACKUP_DIR/oracle-export/expdp_output.txt" 2>&1 || warn "expdp reported errors — see oracle-export/expdp_output.txt"
$DOCKER cp "$ORACLE_CONTAINER:/tmp/of_backup/oceanframe.dmp" "$BACKUP_DIR/oracle-export/" 2>/dev/null \
    && ok "schema exported ($(du -h "$BACKUP_DIR/oracle-export/oceanframe.dmp" | cut -f1))" \
    || die "no dump produced — see $BACKUP_DIR/oracle-export/expdp_output.txt"
$DOCKER cp "$ORACLE_CONTAINER:/tmp/of_backup/oceanframe.log" "$BACKUP_DIR/oracle-export/" 2>/dev/null || true

step "2/4 Library catalog"
if $DOCKER inspect "$APP_CONTAINER" >/dev/null 2>&1; then
    # SQLite's backup API takes a consistent copy while the app keeps running.
    $DOCKER exec "$APP_CONTAINER" python -c "
import sqlite3
src = sqlite3.connect('/data/library/catalog.sqlite')
dst = sqlite3.connect('/tmp/catalog.backup.sqlite')
src.backup(dst); dst.close(); src.close()" \
        && $DOCKER cp "$APP_CONTAINER:/tmp/catalog.backup.sqlite" "$BACKUP_DIR/library/catalog.sqlite" \
        && ok "catalog.sqlite" || warn "catalog backup failed"
    for f in vectors.f32 vectors.json; do
        $DOCKER cp "$APP_CONTAINER:/data/library/$f" "$BACKUP_DIR/library/" 2>/dev/null && ok "$f" || true
    done
    # Published versions' manifests and change logs (not their image folders,
    # which can be rebuilt from the manifest).
    if $DOCKER exec "$APP_CONTAINER" test -d /data/library/curation_versions; then
        $DOCKER exec "$APP_CONTAINER" tar -czf /tmp/curation_versions.tgz --exclude='*/images' \
            -C /data/library curation_versions \
            && $DOCKER cp "$APP_CONTAINER:/tmp/curation_versions.tgz" "$BACKUP_DIR/library/" \
            && ok "curation_versions.tgz (published manifests and change logs)" || warn "versions backup failed"
    fi
else
    warn "$APP_CONTAINER is not running — catalog skipped"
fi

step "3/4 Config"
for f in docker-compose.stack.yml docker-compose.stack.gpu.yml; do
    [[ -f "$PROJECT_DIR/$f" ]] && cp "$PROJECT_DIR/$f" "$BACKUP_DIR/config/" && ok "$f"
done
if $INCLUDE_ENV; then cp "$ENV_FILE" "$BACKUP_DIR/config/.env" && chmod 600 "$BACKUP_DIR/config/.env" && ok ".env"; fi
cat > "$BACKUP_DIR/backup_manifest.json" <<JSON
{
  "timestamp": "$STAMP",
  "created": "$(date -Iseconds)",
  "hostname": "$(hostname)",
  "schema": "$SCHEMA",
  "oracle_image": "$($DOCKER inspect -f '{{.Config.Image}}' "$ORACLE_CONTAINER" 2>/dev/null || echo unknown)",
  "app_image": "$($DOCKER inspect -f '{{.Config.Image}}' "$APP_CONTAINER" 2>/dev/null || echo unknown)",
  "cold_datafiles": $COLD,
  "includes_env": $INCLUDE_ENV
}
JSON
ok "manifest"

if $COLD; then
    [[ -n "$ORACLE_DATA" && -d "$ORACLE_DATA" ]] || die "OCEANFRAME_ORACLE_DATA is not a directory; --cold needs the bind-mounted datafiles"
    step "Raw datafiles (stack stopped)"
    (cd "$PROJECT_DIR" && ./oceanframe-compose.sh stop)
    sudo tar -czf "$BACKUP_DIR/oracle-data.tgz" -C "$(dirname "$ORACLE_DATA")" "$(basename "$ORACLE_DATA")" \
        && ok "oracle-data.tgz ($(du -h "$BACKUP_DIR/oracle-data.tgz" | cut -f1))"
    (cd "$PROJECT_DIR" && ./oceanframe-compose.sh start)
fi

if $LOCAL_ONLY; then
    step "4/4 Upload skipped (--local-only)"
    echo "  Backup kept at $BACKUP_DIR"
    exit 0
fi

step "4/4 Upload to $GCS_BUCKET/$GCS_PREFIX/$STAMP/"
DEST="$GCS_BUCKET/$GCS_PREFIX/$STAMP"
if command -v gcloud >/dev/null 2>&1; then
    gcloud storage cp -r "$BACKUP_DIR/*" "$DEST/" && UPLOADED=true || UPLOADED=false
elif command -v gsutil >/dev/null 2>&1; then
    gsutil -m cp -r "$BACKUP_DIR/*" "$DEST/" && UPLOADED=true || UPLOADED=false
else
    # A bare base image has no Cloud SDK; the app container has the GCS client
    # and, on a workstation, the attached service account's credentials.
    echo "  (no gcloud/gsutil on the host — uploading through $APP_CONTAINER)"
    $DOCKER exec "$APP_CONTAINER" rm -rf /tmp/of_upload >/dev/null 2>&1 || true
    $DOCKER cp "$BACKUP_DIR" "$APP_CONTAINER:/tmp/of_upload"
    $DOCKER exec "$APP_CONTAINER" python - "$DEST" <<'PY' && UPLOADED=true || UPLOADED=false
import pathlib, sys
from google.cloud import storage
bucket_name, _, prefix = sys.argv[1].removeprefix("gs://").partition("/")
bucket = storage.Client().bucket(bucket_name)
root = pathlib.Path("/tmp/of_upload")
for path in sorted(p for p in root.rglob("*") if p.is_file()):
    blob = bucket.blob(f"{prefix}/{path.relative_to(root).as_posix()}")
    blob.upload_from_filename(str(path))
    print("  uploaded", blob.name)
PY
    $DOCKER exec "$APP_CONTAINER" rm -rf /tmp/of_upload >/dev/null 2>&1 || true
fi

if $UPLOADED; then
    ok "uploaded to $DEST/"
    $KEEP_LOCAL || { rm -rf "$BACKUP_DIR"; ok "local copy removed (--keep-local keeps it)"; }
else
    die "upload failed — the backup is still at $BACKUP_DIR"
fi
