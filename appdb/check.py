"""
Exit 0 when the application database answers, 1 otherwise.

The container entrypoint polls this until Oracle is up (``python -m appdb.check``)
— the app-side half of the wait, alongside compose's ``service_healthy``.
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from appdb.engine import get_engine


def ping() -> bool:
    engine = get_engine()
    probe = "SELECT 1 FROM DUAL" if engine.dialect.name == "oracle" else "SELECT 1"
    try:
        with engine.connect() as conn:
            conn.execute(text(probe))
        return True
    except Exception as exc:  # noqa: BLE001 — any failure means "not ready yet"
        print(f"  database not ready: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    sys.exit(0 if ping() else 1)
