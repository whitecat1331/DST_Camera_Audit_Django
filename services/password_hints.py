"""Remember which password NUMBER worked per device, so the next run tries it first.

A small JSON file per device kind (git-ignored). Values are password numbers
(1-based positions in the configured list) — never the passwords themselves.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _setting(name: str, default):
    try:
        from django.conf import settings

        return getattr(settings, name, default)
    except Exception:  # noqa: BLE001 — usable outside Django too
        return default


class HintStore:
    def __init__(self, filename: str, setting: str) -> None:
        self.filename = filename
        self.setting = setting  # settings name that overrides the path (tests)

    def path(self) -> Path | None:
        override = _setting(self.setting, None)
        if override:
            return Path(override)
        base = _setting("BASE_DIR", None)
        return Path(base) / self.filename if base else None

    def _load(self, path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str):
        path = self.path()
        return self._load(path).get(key) if path is not None else None

    def set(self, key: str, value) -> None:
        self.set_many({key: value})

    def set_many(self, values: dict) -> None:
        """Best effort: a hint that can't be saved only costs speed, never a capture."""
        path = self.path()
        if path is None:
            return
        with _lock:
            data = self._load(path)
            data.update(values)
            tmp = path.with_suffix(".tmp")
            for attempt in range(5):
                try:
                    tmp.write_text(json.dumps(data, indent=0, sort_keys=True), encoding="utf-8")
                    tmp.replace(path)  # Windows: fails while a scanner briefly holds the file
                    return
                except OSError as exc:
                    last = exc
                    time.sleep(0.1 * (attempt + 1))
            logger.warning("[HINTS] could not save %s: %s", path.name, last)


def first_then_rest(count: int, *preferred: int | None) -> list[int]:
    """Password numbers 1..count, the preferred (valid) ones first, no repeats."""
    first = [p for p in preferred if isinstance(p, int) and 1 <= p <= count]
    return list(dict.fromkeys([*first, *range(1, count + 1)]))
