#!/usr/bin/env bash
# =============================================================================
# OceanFrame — installer for Google Cloud Workstations (and plain Debian hosts)
# =============================================================================
# Installs Docker (and, for the GPU variant, the NVIDIA Container Toolkit),
# builds OceanFrame, and runs it with Oracle Free for users and curation — the
# same shape as CAT's install_cat.sh, which this follows step by step.
#
#   bash deploy/install_oceanframe.sh            regular: CPU, CLIP search
#   bash deploy/install_oceanframe_gpu.sh        GPU: CUDA torch, SAM 3 on the GPU
#
# Run it as your normal user (it uses sudo where it must). It works from a bare
# base image: on a Cloud Workstation everything outside $HOME — /etc, /usr,
# /var, so Docker itself, its images and its config — is reset on every
# stop/start. So:
#
#   * all data lives under $HOME (~/oceanframe_data), as bind mounts;
#   * Docker keeps its images under $HOME too (~/.oceanframe-docker), so the
#     multi-GB image is not rebuilt after every restart;
#   * a boot hook in ~/.customize_environment.d/ reinstalls Docker (and the
#     NVIDIA toolkit), restores Docker's config and starts the stack again.
#
# Nothing in ~/oceanframe_data is ever deleted. To start the database over,
# re-run with OCEANFRAME_RESET_DATA=yes; a backup tarball is taken first.
#
# Settings (environment variables, all optional):
#   OCEANFRAME_INSTALL_VARIANT   base | gpu                 (default: base)
#   OCEANFRAME_REPO_URL          git URL to clone when not run from a checkout
#   OCEANFRAME_BRANCH            branch to clone             (default: main)
#   OCEANFRAME_HOST_PORT         port the app is served on   (default: 80)
#   OCEANFRAME_DOCKER_ROOT       Docker data-root, or "system" to leave Docker's
#                                default alone               (default: ~/.oceanframe-docker)
#   OCEANFRAME_ADMIN_USERNAME    first admin's username      (default: admin)
#   OCEANFRAME_SEED_NOAA         yes | no — pull the NOAA Pacific T3 starter data
#                                (regional project, Hugging Face dataset and model) (default: yes)
#   OCEANFRAME_RESET_DATA=yes    back up, then wipe the Oracle data
# =============================================================================
set -uo pipefail

SCRIPT_VERSION="2026.10.01"
VARIANT="${OCEANFRAME_INSTALL_VARIANT:-base}"
REPO_URL="${OCEANFRAME_REPO_URL:-https://github.com/akridge/oceanframe.git}"
BRANCH="${OCEANFRAME_BRANCH:-main}"
HOST_PORT="${OCEANFRAME_HOST_PORT:-80}"
PORT_NOTE=""

case "$VARIANT" in
    base) COMPOSE_FILES=(-f docker-compose.stack.yml) ;;
    gpu)  COMPOSE_FILES=(-f docker-compose.stack.yml -f docker-compose.stack.gpu.yml -f docker-compose.evaluation.gpu.yml) ;;
    *)    echo "ERROR: OCEANFRAME_INSTALL_VARIANT must be 'base' or 'gpu' (got: $VARIANT)"; exit 1 ;;
esac

# The real user, even when invoked through sudo.
ACTUAL_USER="${SUDO_USER:-$USER}"
ACTUAL_HOME="$(getent passwd "$ACTUAL_USER" | cut -d: -f6)"
ACTUAL_UID="$(id -u "$ACTUAL_USER")"
ACTUAL_GID="$(id -g "$ACTUAL_USER")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

INSTALL_DIR="$ACTUAL_HOME/oceanframe_deployment"
ENV_FILE="$INSTALL_DIR/.env"
DATA_DIR="$ACTUAL_HOME/oceanframe_data"
DOCKER_ROOT="${OCEANFRAME_DOCKER_ROOT:-$ACTUAL_HOME/.oceanframe-docker}"
HOOK_DIR="$ACTUAL_HOME/.customize_environment.d"
BOOT_LOG="/var/log/oceanframe-bootstrap.log"

say()  { echo; echo "[$1] $2"; }
# Read one key from a .env file. Not `source`: .env carries UID/GID for compose,
# and bash refuses to assign its read-only $UID.
env_get() { { grep -E "^$1=" "$2" 2>/dev/null || true; } | tail -1 | cut -d= -f2-; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠️  $*"; }
die()  { echo "  ✗ ERROR: $*"; exit 1; }
as_user() { sudo -u "$ACTUAL_USER" "$@"; }
# Write a file owned by the user (CAT's `sudo -u user cat > file` made them
# root-owned: the redirect runs as the caller, not as the sudo target).
write_user_file() { as_user tee "$1" >/dev/null; }

echo "=============================================="
echo " OceanFrame installer v${SCRIPT_VERSION}"
echo " Variant: ${VARIANT}   User: ${ACTUAL_USER}   Home: ${ACTUAL_HOME}"
echo "=============================================="

# =============================================================================
# The Docker host setup, as a standalone script. Run now, and again on every
# workstation boot from ~/.customize_environment.d/10-docker-root.sh, because a
# restart wipes all of it. Kept tool-neutral and early (10-) so that any other
# tool's boot hook (CAT's is 50-start-cat.sh) finds Docker ready and already on
# its persistent data-root, rather than starting containers that a later
# data-root change would strand.
# =============================================================================
DOCKER_SETUP_SCRIPT="$(cat <<'SETUP'
#!/usr/bin/env bash
# Installs Docker CE if missing, keeps its data-root under $HOME so images
# survive a workstation restart, and (GPU) configures the NVIDIA runtime.
# Written by OceanFrame's installer; safe to run repeatedly. Runs as root.
set -uo pipefail
DOCKER_ROOT="__DOCKER_ROOT__"
WANT_GPU="__WANT_GPU__"
DOCKER_USER="__DOCKER_USER__"
log() { echo "[docker-setup $(date '+%F %T')] $*"; }
export DEBIAN_FRONTEND=noninteractive

if ! command -v docker >/dev/null 2>&1; then
    log "installing Docker CE"
    apt-get update || log "apt-get update reported errors (often third-party repos); continuing"
    apt-get install -y ca-certificates curl gnupg lsb-release || exit 1
    install -m 0755 -d /etc/apt/keyrings
    . /etc/os-release
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update || true
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || exit 1
fi
usermod -aG docker "$DOCKER_USER" 2>/dev/null || true

changed=0
if [ "$DOCKER_ROOT" != "system" ]; then
    mkdir -p "$DOCKER_ROOT" /etc/docker
    # Merge, never overwrite: nvidia-ctk (and admins) keep other keys here.
    if command -v python3 >/dev/null 2>&1; then
        out=$(python3 - "$DOCKER_ROOT" <<'PY'
import json, os, sys
path, root = "/etc/docker/daemon.json", sys.argv[1]
try:
    with open(path) as fh:
        config = json.load(fh)
except (FileNotFoundError, ValueError):
    config = {}
if config.get("data-root") != root:
    config["data-root"] = root
    with open(path, "w") as fh:
        json.dump(config, fh, indent=2)
    print("changed")
PY
)
        [ "$out" = "changed" ] && changed=1
    elif [ ! -s /etc/docker/daemon.json ]; then
        printf '{\n  "data-root": "%s"\n}\n' "$DOCKER_ROOT" > /etc/docker/daemon.json
        changed=1
    else
        log "python3 missing and /etc/docker/daemon.json exists; leaving data-root unchanged"
    fi
fi

if [ "$WANT_GPU" = "yes" ]; then
    if ! command -v nvidia-ctk >/dev/null 2>&1; then
        log "installing NVIDIA Container Toolkit"
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
            | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
        curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
            | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
            > /etc/apt/sources.list.d/nvidia-container-toolkit.list
        apt-get update || true
        apt-get install -y nvidia-container-toolkit || exit 1
    fi
    # Re-applied every boot: it lives in /etc/docker/daemon.json, which a
    # restart wipes. (CAT's boot hook skips this, so its GPU installs lose the
    # GPU after the first stop/start.)
    if ! grep -q '"nvidia"' /etc/docker/daemon.json 2>/dev/null; then
        nvidia-ctk runtime configure --runtime=docker && changed=1
    fi
fi

if [ "$changed" = 1 ] || ! docker info >/dev/null 2>&1; then
    log "(re)starting Docker"
    systemctl restart docker 2>/dev/null || service docker restart 2>/dev/null || true
fi
for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
docker info >/dev/null 2>&1 && log "Docker ready (root: $(docker info -f '{{.DockerRootDir}}'))" || { log "Docker did not start"; exit 1; }
SETUP
)"

render_docker_setup() {
    local want_gpu=no
    [ "$VARIANT" = "gpu" ] && want_gpu=yes
    printf '%s\n' "$DOCKER_SETUP_SCRIPT" \
        | sed -e "s#__DOCKER_ROOT__#${DOCKER_ROOT}#" -e "s#__WANT_GPU__#${want_gpu}#" -e "s#__DOCKER_USER__#${ACTUAL_USER}#"
}

detect_workstation_url() {
    # https://<port>-<hostname>.<cluster>.cloudworkstations.dev, from (1) DNS
    # search domains, (2) the metadata server's attributes, (3) its proxy-url.
    local port="$1" host domain=""
    host="$(hostname 2>/dev/null)"
    domain="$(grep -E '^(search|domain)' /etc/resolv.conf 2>/dev/null | grep -oE '[a-z0-9.-]+\.cloudworkstations\.dev' | head -1)"
    if [ -z "$domain" ]; then
        local attr val
        for attr in cluster-hostname workstation-cluster-domain workstation-cluster proxy-url; do
            val="$(curl -sf -m 2 -H 'Metadata-Flavor: Google' \
                "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$attr" 2>/dev/null)"
            domain="$(echo "$val" | grep -oE '[a-z0-9.-]+\.cloudworkstations\.dev' | head -1)"
            [ -n "$domain" ] && break
        done
    fi
    [ -n "$domain" ] && [ -n "$host" ] && echo "https://${port}-${host}.${domain}"
}

wait_healthy() {
    local name="$1" tries="$2" every="$3" status
    for ((i = 1; i <= tries; i++)); do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$name" 2>/dev/null || echo missing)"
        case "$status" in
            healthy) ok "$name is healthy"; return 0 ;;
            unhealthy|exited|dead) echo "  $name is $status" ;;
        esac
        sleep "$every"
    done
    return 1
}

oc() { (cd "$INSTALL_DIR" && sudo docker compose "${COMPOSE_FILES[@]}" "$@"); }

# =============================================================================
say "1/10" "Prerequisites"
sudo apt-get update || warn "apt-get update failed (often a third-party repo); continuing"
sudo apt-get install -y ca-certificates curl gnupg lsb-release git rsync python3 openssl \
    || die "could not install prerequisites"
ok "prerequisites installed"

# =============================================================================
say "2/10" "Docker$( [ "$VARIANT" = gpu ] && echo ' and the NVIDIA Container Toolkit')"
# Moving Docker's data-root restarts Docker on a new, empty store: any other
# tool's running containers (CAT's, say) would go down until rebuilt. So when
# something else is running, keep Docker's root as it is unless asked.
if [ -z "${OCEANFRAME_DOCKER_ROOT:-}" ] && command -v docker >/dev/null 2>&1; then
    CURRENT_ROOT="$(sudo docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)"
    OTHERS="$(sudo docker ps --format '{{.Names}}' 2>/dev/null | grep -vc '^oceanframe' || true)"
    if [ -n "$CURRENT_ROOT" ] && [ "$CURRENT_ROOT" != "$DOCKER_ROOT" ] && [ "${OTHERS:-0}" -gt 0 ]; then
        warn "Other containers are running ($(sudo docker ps --format '{{.Names}}' | grep -v '^oceanframe' | tr '\n' ' '))."
        warn "Leaving Docker's data-root at $CURRENT_ROOT so they keep running. Images there are"
        warn "lost on each workstation restart; to move them under \$HOME (and restart those tools),"
        warn "re-run with OCEANFRAME_DOCKER_ROOT=$DOCKER_ROOT."
        DOCKER_ROOT="system"
    fi
fi
render_docker_setup | sudo tee /tmp/oceanframe-docker-setup.sh >/dev/null
sudo bash /tmp/oceanframe-docker-setup.sh || die "Docker setup failed"
ok "Docker ready: $(sudo docker --version)"
if [ "$VARIANT" = "gpu" ]; then
    echo "  Checking that containers can see the GPU..."
    sudo docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 \
        nvidia-smi --query-gpu=name --format=csv,noheader \
        || die "Docker cannot reach an NVIDIA GPU. Was this workstation created with a GPU?"
    ok "GPU visible to Docker"
fi

# =============================================================================
say "3/10" "Directories"
as_user mkdir -p "$INSTALL_DIR" "$DATA_DIR"/{library,models,uploads,backups} \
    "$DATA_DIR"/library/curation_eval/worker/{inputs,outputs}
sudo mkdir -p "$DATA_DIR/oracle-data"
# The Oracle image runs as uid 54321; the bind mount must be writable by it.
sudo chown 54321:54321 "$DATA_DIR/oracle-data"
sudo chmod 750 "$DATA_DIR/oracle-data"
ok "install: $INSTALL_DIR"
ok "data:    $DATA_DIR  (oracle-data owned by uid 54321)"

# =============================================================================
say "4/10" "Source"
if [ -f "$SOURCE_DIR/docker-compose.stack.yml" ] && [ "$SOURCE_DIR" != "$INSTALL_DIR" ]; then
    echo "  Copying the checkout at $SOURCE_DIR"
    as_user rsync -a --delete \
        --exclude .git --exclude examples --exclude library_data --exclude .venv \
        --exclude __pycache__ --exclude '*.pyc' --exclude .env --exclude backups \
        --exclude 'models/*.pt' \
        "$SOURCE_DIR/" "$INSTALL_DIR/" || die "copy failed"
elif [ -d "$INSTALL_DIR/.git" ]; then
    as_user git -C "$INSTALL_DIR" fetch origin "$BRANCH" && as_user git -C "$INSTALL_DIR" checkout "$BRANCH" \
        && as_user git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" || die "git update failed"
elif [ -f "$INSTALL_DIR/docker-compose.stack.yml" ]; then
    echo "  Using the existing files in $INSTALL_DIR"
else
    as_user git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR.tmp" || die "git clone failed"
    as_user rsync -a "$INSTALL_DIR.tmp/" "$INSTALL_DIR/" && rm -rf "$INSTALL_DIR.tmp"
fi
[ -f "$INSTALL_DIR/docker-compose.stack.yml" ] || die "docker-compose.stack.yml not found in $INSTALL_DIR"
ok "source ready"

# =============================================================================
say "5/10" "Configuration (.env)"
# shellcheck source=lib/make_env.sh
. "$INSTALL_DIR/deploy/lib/make_env.sh" || die "deploy/lib/make_env.sh missing from $INSTALL_DIR"
ACCOUNTS_NOTE=""
# Port 80 is CAT's default too. Taken now (any container publishing it) or at the next boot (CAT installed,
# even if stopped, starts on 80 from its own boot hook): use 8080 instead, unless a port was asked for.
port_80_claimed() {
    sudo docker ps --format '{{.Ports}}' 2>/dev/null | grep -qE '(^|[ ,:])80->' && return 0
    [ -f "$HOOK_DIR/50-start-cat.sh" ] || [ -f /etc/workstation-startup.d/50-start-cat ] && return 0
    if [ -f "$ACTUAL_HOME/cat_deployment/.env" ]; then
        local cat_port; cat_port="$(env_get CAT_HOST_PORT "$ACTUAL_HOME/cat_deployment/.env")"
        [ "${cat_port:-80}" = 80 ] && return 0
    fi
    return 1
}
if [ ! -f "$ENV_FILE" ] && [ -z "${OCEANFRAME_HOST_PORT:-}" ] && [ "$HOST_PORT" = 80 ] && port_80_claimed; then
    HOST_PORT=8080
    PORT_NOTE="Port 80 belongs to CAT here, so OceanFrame uses 8080 (set OCEANFRAME_HOST_PORT to choose another)."
    warn "$PORT_NOTE"
fi
if [ ! -f "$ENV_FILE" ]; then
    OCEANFRAME_UID="$ACTUAL_UID" OCEANFRAME_GID="$ACTUAL_GID" oceanframe_env "$HOST_PORT" "$DATA_DIR" \
        | write_user_file "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ACCOUNTS_NOTE="$(oceanframe_accounts "$ENV_FILE")"
    ok "created $ENV_FILE with generated passwords (mode 600)"
else
    ok "keeping existing $ENV_FILE"
    # An older install that chose "no starter data" wrote CURATION_SEED= (empty). Keep that choice:
    # the compose default for CURATION_STARTER_DATA would otherwise switch the data on.
    if grep -q '^CURATION_SEED=$' "$ENV_FILE" && ! grep -q '^CURATION_STARTER_DATA=' "$ENV_FILE"; then
        echo "CURATION_STARTER_DATA=off" >> "$ENV_FILE"
        ok "kept your earlier choice of no starter data (CURATION_STARTER_DATA=off)"
    fi
    HOST_PORT="$(env_get OCEANFRAME_HOST_PORT "$ENV_FILE")"; HOST_PORT="${HOST_PORT:-80}"
fi

# A wrapper with the variant's compose files baked in.
write_user_file "$INSTALL_DIR/oceanframe-compose.sh" <<COMPOSE
#!/usr/bin/env bash
cd "\$(dirname "\$0")"
exec sudo docker compose ${COMPOSE_FILES[*]} "\$@"
COMPOSE
chmod +x "$INSTALL_DIR/oceanframe-compose.sh"

# =============================================================================
say "6/10" "Building the image (first build downloads torch: allow 10–20 minutes)"
oc build || die "image build failed"
ok "image built"

# =============================================================================
say "7/10" "Management scripts"
write_user_file "$INSTALL_DIR/oceanframe-start.sh" <<'EOS'
#!/usr/bin/env bash
cd "$(dirname "$0")"
PORT="$(grep -E '^OCEANFRAME_HOST_PORT=' .env | cut -d= -f2)"; PORT="${PORT:-80}"
./oceanframe-compose.sh up -d && ./oceanframe-compose.sh ps
echo "OceanFrame: http://localhost:${PORT}"
EOS
write_user_file "$INSTALL_DIR/oceanframe-stop.sh" <<'EOS'
#!/usr/bin/env bash
cd "$(dirname "$0")" && ./oceanframe-compose.sh down
EOS
write_user_file "$INSTALL_DIR/oceanframe-restart.sh" <<'EOS'
#!/usr/bin/env bash
cd "$(dirname "$0")" && ./oceanframe-compose.sh up -d && ./oceanframe-compose.sh restart && ./oceanframe-compose.sh ps
EOS
write_user_file "$INSTALL_DIR/oceanframe-logs.sh" <<'EOS'
#!/usr/bin/env bash
cd "$(dirname "$0")" && ./oceanframe-compose.sh logs -f --tail=200 "$@"
EOS
write_user_file "$INSTALL_DIR/oceanframe-status.sh" <<'EOS'
#!/usr/bin/env bash
cd "$(dirname "$0")"
PORT="$(grep -E '^OCEANFRAME_HOST_PORT=' .env | cut -d= -f2)"; PORT="${PORT:-80}"
./oceanframe-compose.sh ps
code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/healthz" || echo 000)
[[ "$code" == 2* ]] && echo "  ✓ app responding (HTTP $code)" || echo "  ✗ app not responding (HTTP $code)"
EOS
write_user_file "$INSTALL_DIR/oceanframe-diagnostics.sh" <<EOS
#!/usr/bin/env bash
# Everything worth knowing when OceanFrame is not reachable or misbehaving.
cd "\$(dirname "\$0")"
PORT="\$(grep -E '^OCEANFRAME_HOST_PORT=' .env | cut -d= -f2)"; PORT="\${PORT:-80}"
echo "=== OceanFrame diagnostics (${VARIANT}) ==="
echo; echo "--- Containers ---"; ./oceanframe-compose.sh ps
echo; echo "--- Health ---"
echo "  Oracle: \$(sudo docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' oceanframe-oracle 2>/dev/null || echo 'not running')"
code=\$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:\${PORT}/healthz" || echo 000)
echo "  App:    HTTP \$code on http://localhost:\${PORT}"
echo; echo "--- Cloud Workstation URL ---"
host=\$(hostname); domain=\$(grep -E '^(search|domain)' /etc/resolv.conf 2>/dev/null | grep -oE '[a-z0-9.-]+\.cloudworkstations\.dev' | head -1)
[ -n "\$domain" ] && echo "  https://\${PORT}-\${host}.\${domain}" || echo "  (not detected — open port \${PORT} from the Cloud Workstations console)"
echo; echo "--- Docker ---"
echo "  data-root: \$(sudo docker info -f '{{.DockerRootDir}}' 2>/dev/null)   (expected ${DOCKER_ROOT})"
echo "  runtimes:  \$(sudo docker info -f '{{range \$k, \$v := .Runtimes}}{{\$k}} {{end}}' 2>/dev/null)"
echo; echo "--- Disk (\$HOME holds the database, the catalog and Docker's images) ---"
df -h "\$HOME" | tail -1 | awk '{print "  "\$4" free of "\$2" ("\$5" used)"}'
avail=\$(df -Pk "\$HOME" | tail -1 | awk '{print \$4}')
[ "\${avail:-0}" -lt 10485760 ] && echo "  ⚠️  under 10 GB free: image rebuilds and the database may fail"
echo; echo "--- Oracle data ---"
echo "  ${DATA_DIR}/oracle-data owner: \$(stat -c '%u' "${DATA_DIR}/oracle-data" 2>/dev/null) (should be 54321)"
if [ "${VARIANT}" = gpu ]; then
    echo; echo "--- GPU inside the app ---"
    sudo docker exec oceanframe-app python -c "import torch; print('  cuda available:', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')" 2>&1 | tail -1
fi
echo; echo "--- Evaluation worker (scores the confusion matrix baseline) ---"
echo "  state: \$(sudo docker inspect -f '{{.State.Status}}' oceanframe-evaluation 2>/dev/null || echo 'not created')"
sudo docker exec oceanframe-evaluation python -c "import torch; print('  cuda available:', torch.cuda.is_available())" 2>&1 | tail -1
./oceanframe-compose.sh logs --tail=10 oceanframe-evaluation 2>/dev/null
echo; echo "--- Refused cross-site writes (if this lists your saves, set OCEANFRAME_ORIGIN_CHECK=off) ---"
./oceanframe-compose.sh logs --tail=2000 oceanframe-app 2>/dev/null | grep 'Cross-site request refused' | tail -5 || true
echo; echo "--- Last boot hook run ---"
sudo tail -n 15 ${BOOT_LOG} 2>/dev/null || echo "  (no boot log yet)"
echo; echo "--- Recent app logs ---"
./oceanframe-compose.sh logs --tail=25 oceanframe-app 2>/dev/null
EOS
write_user_file "$INSTALL_DIR/oceanframe-backup.sh" <<'EOS'
#!/usr/bin/env bash
# Back up to GCS (or locally with --local-only). See deploy/backup_to_gcs.sh -h.
cd "$(dirname "$0")" && exec bash deploy/backup_to_gcs.sh "$@"
EOS
chmod +x "$INSTALL_DIR"/oceanframe-*.sh
ok "oceanframe-{start,stop,restart,status,logs,diagnostics,backup}.sh in $INSTALL_DIR"

# =============================================================================
say "8/10" "Start on boot"
AUTO_START=""
START_HOOK="$(cat <<HOOK
#!/usr/bin/env bash
# Starts OceanFrame on boot (after 10-docker-root.sh has Docker ready).
# Written by OceanFrame's installer. Runs as root.
set -uo pipefail
echo "=== OceanFrame boot \$(date '+%F %T %Z') ==="
for _ in \$(seq 1 24); do
    [ -f "${INSTALL_DIR}/docker-compose.stack.yml" ] && [ -S /var/run/docker.sock ] && break
    sleep 5
done
[ -S /var/run/docker.sock ] || { echo "docker socket never appeared"; exit 0; }
cd "${INSTALL_DIR}" && docker compose ${COMPOSE_FILES[*]} up -d && echo "OceanFrame started"
HOOK
)"
if [ -d /etc/workstation-startup.d ]; then
    # This session: the image's own startup directory (wiped on restart).
    { render_docker_setup; echo; echo "$START_HOOK" | sed '1d'; } \
        | sudo tee /etc/workstation-startup.d/50-start-oceanframe >/dev/null
    sudo chmod +x /etc/workstation-startup.d/50-start-oceanframe

    # Every later boot: a dispatcher in $HOME that runs each drop-in as root.
    # The marker line is the same as CAT's, so whichever tool installs it, both
    # tools' drop-ins run and neither overwrites the other's.
    as_user mkdir -p "$HOOK_DIR" "$ACTUAL_HOME/.workstation"
    install_dispatcher() {
        local hook="$1"
        if [ -f "$hook" ] && ! grep -q 'customize_environment.d dispatcher' "$hook"; then
            as_user cp "$hook" "$HOOK_DIR/00-original-$(basename "$hook").sh"
        fi
        write_user_file "$hook" <<'DISPATCH'
#!/bin/bash
# customize_environment.d dispatcher - runs as the user once per workstation start.
# Executes each drop-in in ~/.customize_environment.d/ as ROOT via sudo so multiple
# tools can register persistent privileged boot actions without overwriting each other.
set -uo pipefail
HOOK_DIR="${HOME}/.customize_environment.d"
[ -d "$HOOK_DIR" ] || exit 0
for _f in "$HOOK_DIR"/*; do
    [ -f "$_f" ] || continue
    if command -v sudo >/dev/null 2>&1; then
        sudo -n bash "$_f" || true
    else
        bash "$_f" || true
    fi
done
DISPATCH
        chmod +x "$hook"
    }
    install_dispatcher "$ACTUAL_HOME/.workstation/customize_environment"
    install_dispatcher "$ACTUAL_HOME/.customize_environment"
    { render_docker_setup | sed "2a exec >> ${BOOT_LOG} 2>&1"; } | write_user_file "$HOOK_DIR/10-docker-root.sh"
    { echo "$START_HOOK" | sed "2a exec >> ${BOOT_LOG} 2>&1"; } | write_user_file "$HOOK_DIR/50-start-oceanframe.sh"
    chmod +x "$HOOK_DIR"/*.sh
    AUTO_START="Cloud Workstation boot hooks: $HOOK_DIR/10-docker-root.sh, 50-start-oceanframe.sh (log: $BOOT_LOG)"
elif [ -d /run/systemd/system ]; then
    sudo tee /etc/systemd/system/oceanframe.service >/dev/null <<UNIT
[Unit]
Description=OceanFrame (Oracle stack)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/docker compose ${COMPOSE_FILES[*]} up -d
ExecStop=/usr/bin/docker compose ${COMPOSE_FILES[*]} down

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload && sudo systemctl enable oceanframe.service
    AUTO_START="systemd: oceanframe.service"
else
    BASHRC="$ACTUAL_HOME/.bashrc"
    as_user sed -i '/# oceanframe-autostart-begin/,/# oceanframe-autostart-end/d' "$BASHRC" 2>/dev/null || true
    # Unquoted heredoc on purpose: INSTALL_DIR is expanded now; \$ defers the rest.
    as_user tee -a "$BASHRC" >/dev/null <<BASHRC_BLOCK
# oceanframe-autostart-begin
if [[ \$- == *i* ]] && ! docker inspect -f '{{.State.Running}}' oceanframe-app 2>/dev/null | grep -q true; then
    (nohup "${INSTALL_DIR}/oceanframe-start.sh" >> "${INSTALL_DIR}/autostart.log" 2>&1 &)
fi
# oceanframe-autostart-end
BASHRC_BLOCK
    AUTO_START=".bashrc (starts on your first interactive login)"
fi
ok "$AUTO_START"

# =============================================================================
say "9/10" "First start"
if [ "${OCEANFRAME_RESET_DATA:-}" = "yes" ] && [ -n "$(sudo ls -A "$DATA_DIR/oracle-data" 2>/dev/null)" ]; then
    BACKUP="$DATA_DIR/backups/oracle-data-$(date +%Y%m%d-%H%M%S).tgz"
    warn "OCEANFRAME_RESET_DATA=yes — backing up to $BACKUP, then wiping the database"
    oc down || true
    sudo tar -czf "$BACKUP" -C "$DATA_DIR" oracle-data || die "backup failed; nothing was deleted"
    sudo find "$DATA_DIR/oracle-data" -mindepth 1 -delete
    ok "database reset (backup kept)"
elif [ -n "$(sudo ls -A "$DATA_DIR/oracle-data" 2>/dev/null)" ]; then
    ok "existing database found — reusing it (nothing is deleted)"
fi

oc up -d oceanframe-oracle || die "could not start Oracle"
echo "  Waiting for Oracle (first start creates the database: several minutes)..."
wait_healthy oceanframe-oracle 90 5 || { oc logs --tail=80 oceanframe-oracle; die "Oracle did not become healthy"; }
oc up -d oceanframe-app oceanframe-evaluation || die "could not start the app"
echo "  Waiting for the app..."
wait_healthy oceanframe-app 60 5 || { oc logs --tail=80 oceanframe-app; die "the app did not become healthy"; }
# The evaluation worker scores the starter baseline: without it the confusion matrix never fills.
for _ in $(seq 1 12); do
    [ "$(sudo docker inspect -f '{{.State.Status}}' oceanframe-evaluation 2>/dev/null)" = running ] && break
    sleep 5
done
if [ "$(sudo docker inspect -f '{{.State.Status}}' oceanframe-evaluation 2>/dev/null)" = running ]; then
    ok "evaluation worker running"
    if [ "$VARIANT" = "gpu" ]; then
        if sudo docker exec oceanframe-evaluation python -c "import sys, torch; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
            ok "evaluation worker sees the GPU"
        else
            warn "the evaluation worker cannot see the GPU: the baseline will score on CPU (much slower). Run oceanframe-diagnostics.sh"
        fi
    fi
else
    oc logs --tail=40 oceanframe-evaluation || true
    warn "the evaluation worker is not running: the app works, but the confusion matrix baseline will wait for it"
fi

# =============================================================================
say "10/10" "Done"
URL="$(detect_workstation_url "$HOST_PORT")"
cat <<SUMMARY

==============================================
 OceanFrame is running (${VARIANT})
==============================================
  Local:        http://localhost:${HOST_PORT}
  Workstation:  ${URL:-run oceanframe-diagnostics.sh to find it}

  Install:      ${INSTALL_DIR}
  Data:         ${DATA_DIR}   (Oracle, catalog, models, uploads, backups)
  Config:       ${INSTALL_DIR}/.env
  On boot:      ${AUTO_START}

  Manage:       ${INSTALL_DIR}/oceanframe-{start,stop,restart,status,logs,diagnostics,backup}.sh
SUMMARY
if grep -q '^CURATION_STARTER_DATA=noaa-t3' "$ENV_FILE"; then
    echo "
  Curation:     The install is done; the NOAA Pacific T3 starter data keeps loading in
                the background and the app is usable meanwhile. Measured with a GPU:
                regional import ~25 min, Hugging Face import ~10 min, linking the
                patches to their source images ~15 min, publishing v1, downloading the
                test+val patches ~15 min, then scoring ~2 min: the confusion matrix
                fills about 70 minutes from now (scoring takes much longer on CPU).
                Progress: Curation → Dataset status, or oceanframe-logs.sh."
fi
[ -n "$PORT_NOTE" ] && echo "
  Note:         $PORT_NOTE"
if [ -n "$ACCOUNTS_NOTE" ]; then
    cat <<ADMIN

  Accounts (shown once; also in .env):
${ACCOUNTS_NOTE}
  Sign in, then add your team under Users. Change these passwords under Account.
ADMIN
fi
[ "$VARIANT" = "gpu" ] && echo "
  SAM 3 weights are licence-gated: put sam3.pt in ${DATA_DIR}/models/ and restart."
echo
echo "  Back up regularly:  ${INSTALL_DIR}/oceanframe-backup.sh -b gs://YOUR-BUCKET"
echo "=============================================="
