#!/usr/bin/env python3
"""
OceanFrame Web — launch server and open browser.
"""
import os
import threading
import time
import webbrowser

import uvicorn

HOST = os.getenv("APP_HOST", "127.0.0.1")
PORT = int(os.getenv("APP_PORT", "80"))
OPEN_BROWSER = os.getenv("OPEN_BROWSER", "1").lower() not in {"0", "false", "no", "off"}
URL  = f"http://{HOST}:{PORT}"

# How long an idle connection stays open. uvicorn's default is 5 s, but the
# Cloud Workstations gateway (like any Google front end) holds its connection
# to the app for up to ~10 minutes and reuses it; if the app closes an idle
# connection just as the gateway sends a save down it, that save fails. Staying
# open longer than the proxy means the proxy always closes first — the cause
# and fix CAT found for its intermittent failed saves.
KEEPALIVE = int(os.getenv("APP_KEEPALIVE", "650"))


def _open_browser():
    time.sleep(1.2)
    webbrowser.open(URL)


if __name__ == "__main__":
    print(f"\n  OceanFrame Web\n  Running at {URL}\n  Press Ctrl+C to stop\n")
    if OPEN_BROWSER:
        threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(
        "main:app", host=HOST, port=PORT, reload=False,
        timeout_keep_alive=KEEPALIVE,
        # Behind the workstation gateway: trust its X-Forwarded-* so request
        # URLs and the client address are the real ones.
        proxy_headers=True, forwarded_allow_ips="*",
    )
