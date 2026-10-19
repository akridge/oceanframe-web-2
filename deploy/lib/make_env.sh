# shellcheck shell=bash
# =============================================================================
# The .env for docker-compose.stack.yml, shared by deploy/install_oceanframe.sh
# (Cloud Workstations) and deploy/stack_up.sh (any Docker host). Sourced, not run.
#
#   oceanframe_env <port> [data-dir]  > .env
#
# With a data directory, every volume is a bind mount under it (a Cloud
# Workstation keeps only $HOME); without one, the stack's named volumes are used.
# Prints generated passwords into the file only; callers show them once.
# Needs openssl. UID/GID come from OCEANFRAME_UID/OCEANFRAME_GID, else id -u/-g.
# =============================================================================

# A letter then hex: safe in a URL and in username:role:password lists, and
# Oracle rejects passwords that start with a digit.
oceanframe_password() { printf 'P%s' "$(openssl rand -hex 12)"; }

oceanframe_env() {
    local port="$1" data_dir="${2:-}"
    local uid="${OCEANFRAME_UID:-$(id -u)}" gid="${OCEANFRAME_GID:-$(id -g)}"
    # Docker Desktop on Windows reports the Windows uid; the image wants a normal one.
    [ "$uid" -ge 1000 ] 2>/dev/null && [ "$uid" -lt 60000 ] || uid=1000
    [ "$gid" -ge 1000 ] 2>/dev/null && [ "$gid" -lt 60000 ] || gid=1000
    local starter
    starter=$( [ "${OCEANFRAME_SEED_NOAA:-yes}" = "no" ] && echo "off" || echo "noaa-t3" )
    cat <<ENVFILE
# Written by OceanFrame's deploy scripts on $(date -u '+%F %T UTC'). Keep private.

# ── Build ───────────────────────────────────────────────────────────────────
UID=${uid}
GID=${gid}

# ── Oracle Free ─────────────────────────────────────────────────────────────
ORACLE_PASSWORD=$(oceanframe_password)
APP_SCHEMA_NAME=oceanframe
APP_SCHEMA_PASSWORD=$(oceanframe_password)
DB_SERVICE_NAME=FREEPDB1
ORACLE_HOST_PORT=1522
ENVFILE
    if [ -n "$data_dir" ]; then
        cat <<ENVFILE

# ── Where data lives (only \$HOME survives a workstation restart) ───────────
OCEANFRAME_ORACLE_DATA=${data_dir}/oracle-data
OCEANFRAME_LIBRARY_DATA=${data_dir}/library
OCEANFRAME_MODELS=${data_dir}/models
OCEANFRAME_UPLOADS=${data_dir}/uploads
OCEANFRAME_EVAL_INPUTS=${data_dir}/library/curation_eval/worker/inputs
OCEANFRAME_EVAL_OUTPUTS=${data_dir}/library/curation_eval/worker/outputs
ENVFILE
    fi
    cat <<ENVFILE

# ── Serving ─────────────────────────────────────────────────────────────────
OCEANFRAME_HOST_PORT=${port}
APP_KEEPALIVE=650

# ── Sign-in ─────────────────────────────────────────────────────────────────
OCEANFRAME_AUTH=on
OCEANFRAME_ADMIN_USERNAME=${OCEANFRAME_ADMIN_USERNAME:-admin}
OCEANFRAME_ADMIN_PASSWORD=$(oceanframe_password)
# Made on first start, never changed afterwards: username:role:password, comma
# separated. Roles: viewer, curator, lead, admin. Change passwords under Account.
OCEANFRAME_STARTER_USERS=test:lead:$(oceanframe_password),user1:curator:$(oceanframe_password)
# Set to true when the app is reached only over https (a Cloud Workstation URL is).
OCEANFRAME_SESSION_COOKIE_SECURE=false
# If every save fails with "Cross-site request refused", the proxy in front
# rewrote the Host header; set this to off.
OCEANFRAME_ORIGIN_CHECK=on

# ── Curation ────────────────────────────────────────────────────────────────
# Starter data: the NOAA Pacific T3 regional project (~160 MB from the public
# bucket), the Hugging Face T3 dataset as a project, and the T3 classifier, which
# is then scored once on the dataset's train, val and test splits so the confusion matrix
# has a baseline. A project you delete stays deleted. noaa-t3 | regional | hub | off
CURATION_STARTER_DATA=${starter}
# Classifier checkpoints load only in the sandboxed evaluation worker; only these
# Hugging Face owners may be registered (comma separated; blank allows any).
CURATION_TRUSTED_CHECKPOINTS=on
CURATION_HF_ALLOWED_ORGS=NMFS-OSI
# Evaluation worker limits.
EVALUATION_CPUS=${OCEANFRAME_EVAL_CPUS:-2}
EVALUATION_MEMORY=${OCEANFRAME_EVAL_MEMORY:-8g}
# Also copy each published version's manifest and change log to a bucket you
# can write to:  CURATION_PUBLISH_URI=gs://your-bucket/oceanframe-datasets
# CURATION_PUBLISH_URI=

# ── Optional ────────────────────────────────────────────────────────────────
# LIB_SOURCE=gs://your-bucket/path
# CURATION_IMAGE_BUCKETS=your-bucket
# HTTP_PROXY= / HTTPS_PROXY= / NO_PROXY=   outbound proxy, if the network needs one
ENVFILE
}

# Prints "user / password (role)" for each account the .env creates on first start.
oceanframe_accounts() {
    local file="$1" admin admin_pw starters
    admin="$(grep -E '^OCEANFRAME_ADMIN_USERNAME=' "$file" | tail -1 | cut -d= -f2-)"
    admin_pw="$(grep -E '^OCEANFRAME_ADMIN_PASSWORD=' "$file" | tail -1 | cut -d= -f2-)"
    [ -n "$admin" ] && echo "    ${admin} / ${admin_pw}   (admin)"
    starters="$(grep -E '^OCEANFRAME_STARTER_USERS=' "$file" | tail -1 | cut -d= -f2-)"
    local IFS=','
    for entry in $starters; do
        local name="${entry%%:*}" rest="${entry#*:}"
        echo "    ${name} / ${rest#*:}   (${rest%%:*})"
    done
}
