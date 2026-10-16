#!/usr/bin/env bash
# =============================================================================
# Schedule deploy/backup_to_gcs.sh with cron (ported from CAT)
# =============================================================================
#   deploy/schedule_backup_cron.sh install -b gs://my-bucket              daily 02:00
#   deploy/schedule_backup_cron.sh install -b gs://my-bucket --cron "0 3 * * 0"
#   deploy/schedule_backup_cron.sh status
#   deploy/schedule_backup_cron.sh uninstall
#
# Touches exactly one crontab line, tagged "# oceanframe-backup-cron"; the rest
# of your crontab is left alone. Unattended runs need the uploader to have
# credentials (gcloud auth, or the workstation's service account) and the
# stack running at the scheduled time; failures land in the log file.
#
# On a Cloud Workstation, cron itself lives outside $HOME and stops with the
# workstation — schedule backups for hours it is usually running, or run
# oceanframe-backup.sh by hand before stopping it.
# =============================================================================
set -euo pipefail

MARKER="# oceanframe-backup-cron"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_SCRIPT="$SCRIPT_DIR/backup_to_gcs.sh"
LOG_FILE="$PROJECT_DIR/backups/cron_backup.log"

action="${1:-}"; shift || true
BUCKET=""; SCHEDULE="0 2 * * *"; DRY_RUN=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        -b|--bucket) BUCKET="$2"; shift 2 ;;
        --cron)      SCHEDULE="$2"; shift 2 ;;
        --dry-run)   DRY_RUN=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

current() { crontab -l 2>/dev/null || true; }

case "$action" in
    install)
        [[ "$BUCKET" == gs://* ]] || { echo "Give the bucket: install -b gs://BUCKET"; exit 1; }
        [[ $(wc -w <<<"$SCHEDULE") -eq 5 ]] || { echo "--cron needs 5 fields, e.g. \"0 2 * * *\""; exit 1; }
        line="$SCHEDULE cd $PROJECT_DIR && bash $BACKUP_SCRIPT -b $BUCKET >> $LOG_FILE 2>&1 $MARKER"
        if $DRY_RUN; then echo "$line"; exit 0; fi
        mkdir -p "$(dirname "$LOG_FILE")"
        { current | grep -vF "$MARKER"; echo "$line"; } | crontab -
        echo "✓ Scheduled: $SCHEDULE → $BUCKET  (log: $LOG_FILE)"
        ;;
    uninstall)
        current | grep -vF "$MARKER" | crontab -
        echo "✓ Removed the OceanFrame backup schedule"
        ;;
    status)
        if current | grep -qF "$MARKER"; then
            echo "Installed:"; current | grep -F "$MARKER"
        else
            echo "Not installed."
        fi
        [[ -f "$LOG_FILE" ]] && { echo; echo "Last log lines:"; tail -n 15 "$LOG_FILE"; }
        ;;
    *)
        sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
