#!/usr/bin/env bash
#
# Set up the OceanFrame image library on a Google Cloud Workstation.
#
#   curl -SL https://raw.githubusercontent.com/akridge/oceanframe/main/deploy/workstation_setup.sh | bash
#
# or, from a clone:
#
#   ./deploy/workstation_setup.sh
#
# What it does:
#   1. installs the Python dependencies into a venv (optionally the ML stack)
#   2. verifies the workstation's service account can read the bucket
#   3. writes ~/.oceanframe.env with your settings
#   4. registers a systemd --user service so the app survives a workstation restart
#   5. prints the URL to open
#
# Everything is overridable by environment variable:
#   REPO_URL, INSTALL_DIR, BRANCH, PORT, LIB_SOURCE, WITH_ML, LIB_PATH_TAG_PATTERN
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/akridge/oceanframe}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/oceanframe}"
BRANCH="${BRANCH:-main}"
PORT="${PORT:-8080}"
WITH_ML="${WITH_ML:-0}"
LIB_SOURCE="${LIB_SOURCE:-}"
LIB_DATA_DIR="${LIB_DATA_DIR:-$HOME/oceanframe-data}"
SERVICE_NAME="${SERVICE_NAME:-oceanframe-library}"

info()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. Code ───────────────────────────────────────────────────────────────────

if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Updating $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch origin "$BRANCH" && git -C "$INSTALL_DIR" checkout "$BRANCH" && git -C "$INSTALL_DIR" pull origin "$BRANCH"
elif [[ -f "$PWD/main.py" && -d "$PWD/library" ]]; then
  info "Using the checkout in $PWD"
  INSTALL_DIR="$PWD"
else
  info "Cloning $REPO_URL into $INSTALL_DIR"
  command -v git >/dev/null || die "git is not installed"
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# ── 2. Python ─────────────────────────────────────────────────────────────────

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || die "python3 is not installed"

info "Creating the virtualenv"
"$PYTHON" -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

if [[ "$WITH_ML" == "1" ]]; then
  info "Installing the model stack (torch, open_clip, ultralytics) — this takes a while"
  ./.venv/bin/pip install --quiet -r requirements-ml.txt
else
  info "Skipping the model stack (WITH_ML=1 to include it)"
fi

# ── 3. Credentials + bucket check ─────────────────────────────────────────────

if [[ -n "$LIB_SOURCE" && "$LIB_SOURCE" == gs://* ]]; then
  info "Checking read access to $LIB_SOURCE"
  if command -v gcloud >/dev/null; then
    gcloud storage ls "$LIB_SOURCE" --limit=1 >/dev/null 2>&1 \
      || warn "Could not list $LIB_SOURCE. A workstation uses its attached service account: grant it roles/storage.objectViewer on the bucket, or run 'gcloud auth application-default login'."
  else
    warn "gcloud not found; skipping the bucket check."
  fi
fi

# ── 4. Environment file ───────────────────────────────────────────────────────

ENV_FILE="$HOME/.oceanframe.env"
info "Writing $ENV_FILE"
cat > "$ENV_FILE" <<ENV
# OceanFrame image library — edit and restart with:
#   systemctl --user restart ${SERVICE_NAME}
APP_HOST=0.0.0.0
APP_PORT=${PORT}
OPEN_BROWSER=0

LIB_DATA_DIR=${LIB_DATA_DIR}
LIB_SOURCE=${LIB_SOURCE}
LIB_EMBED_BACKEND=${LIB_EMBED_BACKEND:-auto}
LIB_YOLO_MODEL=${LIB_YOLO_MODEL:-yolo11n.pt}
LIB_SAM3_MODEL=${LIB_SAM3_MODEL:-${INSTALL_DIR}/models/sam3.pt}
LIB_PATH_TAG_PATTERN=${LIB_PATH_TAG_PATTERN:-}
LIB_CRAWL_WORKERS=${LIB_CRAWL_WORKERS:-8}
ENV
mkdir -p "$LIB_DATA_DIR" "$INSTALL_DIR/models"

# ── 5. systemd --user service ─────────────────────────────────────────────────

if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  info "Installing the ${SERVICE_NAME} user service"
  cat > "$UNIT_DIR/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=OceanFrame image library
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${INSTALL_DIR}/.venv/bin/python launch.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now "${SERVICE_NAME}.service"
  # A workstation container stops when you disconnect unless lingering is on.
  loginctl enable-linger "$USER" >/dev/null 2>&1 || true
  sleep 2
  systemctl --user --no-pager --lines=5 status "${SERVICE_NAME}.service" || true
else
  warn "systemd --user is unavailable; start it manually instead:"
  warn "  cd ${INSTALL_DIR} && set -a && . ${ENV_FILE} && set +a && ./.venv/bin/python launch.py"
fi

# ── 6. How to reach it ────────────────────────────────────────────────────────

cat <<DONE

OceanFrame library is set up in ${INSTALL_DIR}.

  Open in the workstation browser : http://localhost:${PORT}/library
  From your laptop                : use the workstation's web preview on port ${PORT},
                                    or tunnel it:

    gcloud workstations start-tcp-tunnel \\
      --project=PROJECT --region=REGION --cluster=CLUSTER --config=CONFIG \\
      WORKSTATION ${PORT} --local-host-port=localhost:${PORT}

  First crawl (a large bucket belongs in a terminal, not a browser tab):

    cd ${INSTALL_DIR} && set -a && . ${ENV_FILE} && set +a
    ./.venv/bin/python -m library.cli doctor
    ./.venv/bin/python -m library.cli index ${LIB_SOURCE:-gs://your-bucket/prefix}

DONE
