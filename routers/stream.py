"""
GET /api/stream/{session_id}?mode=all  — SSE analysis stream.
POST /api/cancel/{session_id}          — cancel running analysis.
"""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from analysis import run_analysis, run_image_analysis
from session import get_session, clear_thumbnails

router   = APIRouter()
_executor = ThreadPoolExecutor(max_workers=4)


@router.get("/stream/{session_id}")
async def stream_analysis(session_id: str, mode: str = "all"):
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    session.frames.clear()
    clear_thumbnails(session)
    session.cancel_flag = False

    loop  = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def post_event(event: dict):
        loop.call_soon_threadsafe(queue.put_nowait, event)

    async def generator():
        yield ": connected\n\n"

        if session.source_type == "images":
            future = loop.run_in_executor(_executor, run_image_analysis, session, post_event)
        else:
            future = loop.run_in_executor(_executor, run_analysis, session, post_event, mode)

        try:
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=60.0)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("complete", "error", "cancelled"):
                    break
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Analysis timed out'})}\n\n"
        finally:
            await future

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cancel/{session_id}")
def cancel_analysis(session_id: str):
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    session.cancel_flag = True
    return JSONResponse({"ok": True})
