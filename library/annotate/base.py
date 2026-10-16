"""Annotator interface — anything that turns an image into labelled regions."""
from __future__ import annotations

from abc import ABC, abstractmethod

from PIL import Image

from library.models import AnnotatorStatus, Detection


class Annotator(ABC):
    """
    One model pass over a batch of images.

    Implementations load their weights lazily so that importing the library
    never pulls in torch, and report *why* they are unavailable rather than
    raising at import time — the UI shows that message next to the disabled
    button.
    """

    name: str = "base"
    needs_prompts: bool = False

    # Set when inference itself failed, as opposed to genuinely finding nothing.
    # A run that swallows every error and reports "0 detections" is
    # indistinguishable from a model that worked and found no objects, which is
    # how a broken SAM 3 looked like a working one for an entire session.
    runtime_error: str = ""

    @abstractmethod
    def status(self) -> AnnotatorStatus:
        """Is this usable, and with which labels? Loads the weights if needed."""

    @abstractmethod
    def loaded(self) -> bool:
        """True once loading has finished, successfully or not. Never loads."""

    def peek(self) -> AnnotatorStatus:
        """
        Status without loading anything, for pages that must answer at once.

        The first load can mean a download (Ultralytics fetches yolo11n.pt from
        GitHub), and the status page doing that inline froze the whole app.
        """
        if self.loaded():
            return self.status()
        return AnnotatorStatus(self.name, False, "Loading the model in the background…", loading=True)

    @abstractmethod
    def annotate(
        self, images: list[Image.Image], prompts: list[str] | None = None
    ) -> list[list[Detection]]:
        """One list of detections per input image, in the same order."""


def xyxy_to_norm(x1: float, y1: float, x2: float, y2: float, w: int, h: int) -> tuple[float, float, float, float]:
    """Pixel corner box -> normalised (cx, cy, w, h), the YOLO label convention."""
    w = max(w, 1)
    h = max(h, 1)
    cx = ((x1 + x2) / 2.0) / w
    cy = ((y1 + y2) / 2.0) / h
    bw = abs(x2 - x1) / w
    bh = abs(y2 - y1) / h
    clamp = lambda v: float(min(max(v, 0.0), 1.0))  # noqa: E731
    return clamp(cx), clamp(cy), clamp(bw), clamp(bh)
