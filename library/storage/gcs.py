"""
Google Cloud Storage backend.

Auth is Application Default Credentials, so an attached service account on a
Cloud Workstation / GCE VM works with no key file:

    gcloud auth application-default login       # laptop
    (nothing)                                   # workstation with a SA attached

The bucket only needs ``roles/storage.objectViewer``.

Public buckets need no credentials at all.  NOAA's open-data buckets — such as
``gs://nmfs_odp_pifsc`` — are world-readable, and a scientist trying the library
on one should not have to create a GCP project first, so when ADC is absent the
backend falls back to an anonymous client.  ``LIB_GCS_ANONYMOUS`` forces either
behaviour (``auto`` | ``yes`` | ``no``).
"""
from __future__ import annotations

import os
import threading
from typing import Iterator
from urllib.parse import urlparse

from library import settings
from library.models import ObjectRef
from library.storage.base import StorageBackend


class GCSUnavailable(RuntimeError):
    pass


def _size_connection_pool(client) -> None:
    """
    Widen the HTTP connection pool to match the crawl fan-out.

    google-cloud-storage inherits urllib3's default of 10 pooled connections.
    Crawling with more workers than that makes every extra worker open a socket,
    use it once, and have it discarded ("Connection pool is full"), which shows
    up as a stream of warnings and a TLS handshake per object.
    """
    try:  # evaluation staging fetches this many patches at once (curation.settings clamps the same way)
        fetchers = max(1, min(64, int(os.getenv("CURATION_EVAL_FETCH_WORKERS", "32"))))
    except ValueError:
        fetchers = 32
    size = max(settings.CRAWL_WORKERS, settings.INDEX_BATCH, fetchers, 10) + 4
    try:
        import requests.adapters  # noqa: PLC0415

        adapter = requests.adapters.HTTPAdapter(pool_connections=size, pool_maxsize=size)
        client._http.mount("https://", adapter)
    except Exception:
        # Private attribute; if the client internals move, the default pool
        # still works — it is a throughput hint, not a correctness requirement.
        pass


def parse_gs_uri(uri: str) -> tuple[str, str]:
    """'gs://bucket/a/b' -> ('bucket', 'a/b').  Trailing slashes are stripped."""
    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError(f"Not a gs:// URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/").rstrip("/")


class GCSBackend(StorageBackend):
    kind = "gcs"

    def __init__(self, root: str) -> None:
        bucket, prefix = parse_gs_uri(root)
        super().__init__(f"gs://{bucket}" + (f"/{prefix}" if prefix else ""))
        self.bucket_name = bucket
        self.prefix = prefix
        self.anonymous = False
        self._client = None
        self._bucket = None
        self._lock = threading.Lock()

    # ── client ────────────────────────────────────────────────────────────────

    def _ensure_client(self):
        # google-cloud-storage clients are thread-safe once built, but building
        # one twice concurrently wastes a metadata-server round trip.
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                from google.cloud import storage  # noqa: PLC0415
            except ImportError as exc:
                raise GCSUnavailable(
                    "google-cloud-storage is not installed. "
                    "Install it with: pip install google-cloud-storage"
                ) from exc

            self._client, self._bucket = self._build_client(storage)
            _size_connection_pool(self._client)
            return self._client

    def _build_client(self, storage):
        """Credentialed client when ADC is available, anonymous otherwise."""
        mode = settings.GCS_ANONYMOUS.strip().lower()

        if mode != "yes":
            try:
                client = storage.Client()
                self.anonymous = False
                return client, client.bucket(self.bucket_name)
            except Exception as exc:
                if mode == "no":
                    raise GCSUnavailable(
                        "No Google credentials found. Run "
                        "`gcloud auth application-default login`, attach a service "
                        "account to the workstation, or set LIB_GCS_ANONYMOUS=yes "
                        f"for a public bucket. ({exc})"
                    ) from exc

        client = storage.Client.create_anonymous_client()
        self.anonymous = True
        return client, client.bucket(self.bucket_name)

    def exists(self) -> bool:
        try:
            self._ensure_client()
            # A 1-object listing is cheaper and needs weaker permissions than
            # bucket.exists(), which requires storage.buckets.get.
            next(iter(self._client.list_blobs(self.bucket_name, prefix=self.prefix, max_results=1)), None)
            return True
        except Exception:
            return False

    # ── listing ───────────────────────────────────────────────────────────────

    def list_objects(self, extensions: set[str]) -> Iterator[ObjectRef]:
        self._ensure_client()
        lowered = {e.lower() for e in extensions}
        base = f"{self.prefix}/" if self.prefix else ""

        for blob in self._client.list_blobs(self.bucket_name, prefix=self.prefix):
            name = blob.name
            if name.endswith("/"):
                continue                       # directory placeholder object
            dot = name.rfind(".")
            if dot < 0 or name[dot:].lower() not in lowered:
                continue
            key = name[len(base):] if base and name.startswith(base) else name
            yield ObjectRef(
                uri=f"gs://{self.bucket_name}/{name}",
                key=key,
                size=int(blob.size or 0),
                mtime=blob.updated.timestamp() if blob.updated else 0.0,
                # generation changes on every overwrite, so it is the cheapest
                # correct "has this object changed?" token.
                etag=str(blob.generation or blob.etag or ""),
            )

    # ── reads ─────────────────────────────────────────────────────────────────

    def read_bytes(self, uri: str) -> bytes:
        self._ensure_client()
        _, key = parse_gs_uri(uri)
        return self._bucket.blob(key).download_as_bytes()

    def signed_url(self, uri: str, ttl: int) -> str | None:
        try:
            self._ensure_client()
            from datetime import timedelta  # noqa: PLC0415

            _, key = parse_gs_uri(uri)
            return self._bucket.blob(key).generate_signed_url(
                version="v4", expiration=timedelta(seconds=ttl), method="GET"
            )
        except Exception:
            # Signing needs a private key or the IAM Credentials API; when it is
            # not available the caller falls back to proxying the bytes.
            return None

    def describe(self) -> str:
        self._ensure_client()
        return f"{self.root} ({'anonymous' if self.anonymous else 'authenticated'})"

    # ── writes (dataset export only) ──────────────────────────────────────────

    def write_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._ensure_client()
        blob = self._bucket.blob(key)
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{self.bucket_name}/{key}"
