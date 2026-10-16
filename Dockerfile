# Two build targets:
#
#   runtime  (default) — the app plus the dependency-free hash descriptor.
#                        451 MB. Indexes, scores, dedupes, tags, exports.
#   ml                 — adds torch, open_clip and ultralytics for semantic
#                        text search and model tagging. ~3 GB with the CPU torch
#                        wheels (the default), 6.1 GB if you build with
#                        --build-arg TORCH_INDEX= and get the CUDA build.
#
# They are separate targets rather than one image with a flag so that rebuilding
# the app never re-resolves the model stack, and `docker compose up` does not
# pull 3 GB to look at some thumbnails.
#
#   docker build --target runtime -t oceanframe:core .
#   docker build --target ml      -t oceanframe:ml   .

# ── Base: the Python runtime ──────────────────────────────────────────────────
# Parameterised so a site with an internal mirror or a hardened base can swap it
# without editing this file:  --build-arg BASE_IMAGE=registry.example/python:3.12-slim
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080 \
    OPEN_BROWSER=0 \
    LIB_DATA_DIR=/data/library \
    LIB_MODEL_DIR=/models

# No apt layer here on purpose.  requirements.txt pins opencv-python-headless,
# which drops the GUI bindings and with them the libGL/libglib dependency — the
# app never opens a window.  That keeps the core image small and its CVE surface
# down to the Python base.  PID 1 reaping is handled by compose's `init: true`
# rather than by installing tini.

# Run as a normal user.  UID/GID are build args because /data and /models are
# usually bind-mounted from the host, and a root-owned catalog is a nuisance to
# clean up afterwards.
ARG UID=1000
ARG GID=1000
# Best-effort: the names are a nicety for `docker exec`. The uid/gid may
# already exist (UID=0 is root, and 1000 often exists in the base image), so
# every step tolerates failure and the image switches user *numerically* below —
# `USER 0` works whether or not a passwd entry was created, while
# `USER oceanframe` would break the container outright.
RUN groupadd --gid "$GID" oceanframe 2>/dev/null || true \
    && useradd --uid "$UID" --gid "$GID" --create-home --shell /bin/bash oceanframe 2>/dev/null || true \
    && mkdir -p /app /data/library/curation_eval/worker/inputs /data/library/curation_eval/worker/outputs \
                /models /inputs /outputs \
    && chown -R "$UID:$GID" /app /data /models /inputs /outputs

WORKDIR /app

# ── Core dependencies ─────────────────────────────────────────────────────────
FROM base AS deps

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

# ── Runtime image (default) ───────────────────────────────────────────────────
FROM deps AS runtime

# ARG scope ends at each FROM, so these have to be re-declared to be usable in
# COPY --chown below.  They inherit the values passed on the build command line.
ARG UID=1000
ARG GID=1000

COPY --chown=$UID:$GID . .
RUN mkdir -p /app/uploads/thumbs && chown -R "$UID:$GID" /app/uploads

USER ${UID}:${GID}
EXPOSE 8080
VOLUME ["/data/library"]

# Uses the stdlib rather than curl so the image needs no extra package, and
# hits /healthz, which deliberately touches neither the catalog nor the models.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url='http://127.0.0.1:%s/healthz' % os.getenv('APP_PORT','8080'); \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

CMD ["python", "launch.py"]

# ── ML image ──────────────────────────────────────────────────────────────────
# Installed on top of the core deps layer, so switching between the two images
# re-uses everything up to this point.
FROM deps AS ml-deps

COPY requirements-ml.txt ./
# The CPU wheel index keeps torch to ~1 GB instead of pulling the CUDA build and
# its nvidia-* dependencies.  Override with --build-arg TORCH_INDEX= to get the
# default (GPU) wheels from PyPI.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "$TORCH_INDEX" ]; then \
        pip install --no-cache-dir --extra-index-url "$TORCH_INDEX" -r requirements-ml.txt; \
    else \
        pip install --no-cache-dir -r requirements-ml.txt; \
    fi

# Ultralytics depends on the full opencv-python, which installs over the
# headless build and then needs libGL/libxcb at import time — verified: `import
# cv2` fails with "libxcb.so.1: cannot open shared object file".  Swapping it
# back afterwards keeps cv2 working with no system libraries at all, so this
# image needs no apt layer either.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip uninstall -y opencv-python \
    && pip install --no-cache-dir --force-reinstall opencv-python-headless \
    && python -c "import cv2, ultralytics; print('cv2', cv2.__version__, '| ultralytics', ultralytics.__version__)"

FROM ml-deps AS evaluation-worker

ARG UID=1000
ARG GID=1000
COPY --chown=$UID:$GID curation/evaluation_worker.py /app/curation/evaluation_worker.py
ENV YOLO_CONFIG_DIR=/tmp/ultralytics MPLCONFIGDIR=/tmp/matplotlib HOME=/tmp
USER ${UID}:${GID}
CMD ["python", "-m", "curation.evaluation_worker"]

FROM ml-deps AS ml

ARG UID=1000
ARG GID=1000

# "auto", not "clip": if the model host is unreachable (air-gapped runner,
# egress proxy) the app still starts on the hash descriptor and says why in the
# banner, rather than refusing to boot.
# YOLO_CONFIG_DIR/MPLCONFIGDIR keep Ultralytics and matplotlib out of the
# read-only home of the unprivileged user.
ENV LIB_EMBED_BACKEND=auto \
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    MPLCONFIGDIR=/tmp/matplotlib

COPY --chown=$UID:$GID . .
RUN mkdir -p /app/uploads/thumbs && chown -R "$UID:$GID" /app/uploads

USER ${UID}:${GID}
EXPOSE 8080
VOLUME ["/data/library"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url='http://127.0.0.1:%s/healthz' % os.getenv('APP_PORT','8080'); \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

CMD ["python", "launch.py"]
