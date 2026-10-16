#!/usr/bin/env bash
#
# Build a real library from NOAA's public PIFSC open-data bucket.
#
#   ./scripts/noaa_quickstart.sh            # ~2,800 images, a few minutes
#   FULL=1 ./scripts/noaa_quickstart.sh     # no limits (large: hours, ~TBs read)
#
# The bucket is world-readable, so this needs no GCP project and no gcloud
# login — the library falls back to an anonymous GCS client.
#
#   https://console.cloud.google.com/storage/browser/nmfs_odp_pifsc
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
[[ -x "$PY" ]] || PY="python3"

export LIB_DATA_DIR="${LIB_DATA_DIR:-$PWD/library_data}"
BUCKET="gs://nmfs_odp_pifsc"
AI="$BUCKET/PIFSC/ESD/ARP/pifsc-ai-data-repository"

if [[ "${FULL:-0}" == "1" ]]; then
  LIM_CLASS=0; LIM_FISH=0; LIM_PHOTO=0
else
  LIM_CLASS=1800; LIM_FISH=900; LIM_PHOTO=120
fi

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

step "What is installed"
$PY -m library.cli doctor

# Each collection gets its own path rule: one bucket, three conventions.
step "1/3  Coral bleaching classifier — 224px crops in train|val|test / CORAL|CORAL_BL"
$PY -m library.cli index "$AI/class/noaa-esd-coral-bleaching-classifierv1/dataset" \
  --label "ESD coral bleaching classifier v1" \
  --limit "$LIM_CLASS" \
  --tag-pattern '(?P<split>train|val|test)/(?P<class>[A-Z_]+)/(?P<island>[A-Z]{3})-(?P<station>[A-Z0-9]+)_(?P<year>\d{4})'

step "2/3  MOUSS fish detection 2016 — deep-water stereo camera stills"
$PY -m library.cli index "$AI/fish-detection/MOUSS_fish_detection_v1/datasets/large_2016_dataset/images" \
  --label "MOUSS fish detection 2016" \
  --limit "$LIM_FISH" \
  --tag-pattern '(?P<date>\d{8})\.(?P<time>\d{6})'

step "3/3  CRCP photogrammetry — 6000x4000 DSLR frames, ~13 MB each"
$PY -m library.cli index "$BUCKET/PIFSC/ESD/ARP/Photogrammetric Imagery/CRCP_Projects" \
  --label "CRCP photogrammetry projects" \
  --limit "$LIM_PHOTO" \
  --tag-pattern '(?P<project>[^/]+)/(?P<site>[^/]+)/'

# NOAA publishes its own detector next to the imagery.  Use it: a COCO-trained
# checkpoint labels reef fish "airplane" and coral "skateboard" — verified, not
# hypothetical.
ICRA="$AI/models/yolo11-esa-icra-detector.pt"
if $PY -c "import ultralytics" 2>/dev/null; then
  step "Tagging coral imagery with NOAA's ESA/ICRA detector"
  $PY -m library.cli annotate --annotator yolo --model "$ICRA" \
    --query '{"source_id": 3}' || true
else
  printf '\n[!] ultralytics is not installed; skipping detection.\n'
  printf '    pip install -r requirements-ml.txt, then:\n'
  printf '    %s -m library.cli annotate --annotator yolo --model %s --query %s\n' \
    "$PY" "$ICRA" "'{\"source_id\": 3}'"
fi

step "Catalog"
$PY -m library.cli stats

TRY_THIS='
Things to try:
  * filter to one Collection in the left rail, then drill the folder tree
  * click a coral crop and hit "Find similar"
  * tick "Collapse near-duplicates" on the photogrammetry collection
  * select images, tag them, and build a dataset with a by-folder split'

# Same catalog either way, but "run launch.py" is useless advice inside a
# container, and "docker compose up" is useless advice outside one.
if [[ -f /.dockerenv ]]; then
	cat <<DONE

Now browse it:

    docker compose up -d
    open http://localhost:${PORT:-8080}/library

To tag with NOAA's coral detector, add this to .env and restart:

    LIB_YOLO_MODEL=${ICRA}
${TRY_THIS}
DONE
else
	cat <<DONE

Now browse it:

    LIB_DATA_DIR="${LIB_DATA_DIR}" \\
    LIB_YOLO_MODEL="${ICRA}" \\
    ${PY} launch.py

then open http://localhost/library
${TRY_THIS}
DONE
fi
