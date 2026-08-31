"""TF CPU VNC screenshot capture."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from PIL import Image
from pyvnc import SyncVNCClient, VNCConfig

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]


def _noop_progress(_: str) -> None:
    return None


def capture_vnc(
    output_dir: str | Path,
    host: str,
    password: str,
    port: int = 5900,
    filename: str = "VNC_Screenshot.png",
    on_progress: ProgressFn | None = None,
) -> Path:
    """Connect to a TF VNC server and save a full-screen PNG."""
    progress = on_progress or _noop_progress
    if not password:
        raise RuntimeError("TF_VNC_PASSWORD is not configured")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / filename

    t0 = time.perf_counter()
    progress(f"VNC connecting {host}:{port}")
    logger.info("[VNC] connecting host=%s port=%s", host, port)
    # Keyword args required: positional 3rd arg is timeout, not password.
    config = VNCConfig(host=host, port=port, password=password, timeout=15.0)
    with SyncVNCClient.connect(config) as vnc:
        logger.info(
            "[VNC] Connected to %s:%s (%sx%s) in %.1fs",
            host,
            port,
            vnc.rect.width,
            vnc.rect.height,
            time.perf_counter() - t0,
        )
        progress(f"VNC capturing {host} ({vnc.rect.width}x{vnc.rect.height})")
        pixels = vnc.capture()
        image = Image.fromarray(pixels, "RGBA")
        image.save(filepath)

    logger.info(
        "[VNC] Saved screenshot path=%s elapsed=%.1fs",
        filepath,
        time.perf_counter() - t0,
    )
    progress(f"VNC saved {filename}")
    return filepath
