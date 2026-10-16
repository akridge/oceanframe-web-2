#!/usr/bin/env bash
# =============================================================================
# Kept so older instructions still work: hands over to deploy/install_oceanframe.sh,
# which installs Docker, writes .env with generated passwords and accounts, starts
# Oracle, the app and the evaluation worker, and brings them back on boot.
#
#   sudo bash cloud_bootstrap.sh
#   curl -SL https://raw.githubusercontent.com/akridge/oceanframe/main/cloud_bootstrap.sh | sudo bash
#
# REPO_URL and BRANCH choose what to clone when not run from a checkout; every
# OCEANFRAME_* setting of the installer passes through (see its header).
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/akridge/oceanframe}"
BRANCH="${BRANCH:-main}"

here="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd || true)"
if [ -n "$here" ] && [ -f "$here/deploy/install_oceanframe.sh" ]; then
    source_dir="$here"
else
    command -v git >/dev/null 2>&1 || { sudo apt-get update -qq && sudo apt-get install -y -qq git; }
    source_dir="$(mktemp -d)/oceanframe"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$source_dir"
fi

# The installer runs as the normal user and uses sudo where it must.
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    chown -R "$SUDO_USER" "$source_dir"
    exec sudo -u "$SUDO_USER" --preserve-env bash "$source_dir/deploy/install_oceanframe.sh"
fi
exec bash "$source_dir/deploy/install_oceanframe.sh"
