from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import Recording


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"recorded_events": [], "pending_uploads": [], "uploaded_events": []}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, dict):
            for key in self._data:
                if isinstance(loaded.get(key), list):
                    self._data[key] = loaded[key]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def has_recorded(self, event_key: str) -> bool:
        with self._lock:
            return event_key in self._data["recorded_events"]

    def add_pending(self, recording: Recording) -> None:
        with self._lock:
            if recording.event_key not in self._data["recorded_events"]:
                self._data["recorded_events"].append(recording.event_key)
            if not any(item.get("event_key") == recording.event_key for item in self._data["pending_uploads"]):
                self._data["pending_uploads"].append(asdict(recording))
            self._save()

    def pending(self) -> tuple[Recording, ...]:
        with self._lock:
            result: list[Recording] = []
            for item in self._data["pending_uploads"]:
                try:
                    if isinstance(item.get("parts"), list):
                        item = {**item, "parts": tuple(item["parts"])}
                    result.append(Recording(**item))
                except (TypeError, KeyError):
                    continue
            return tuple(result)

    def mark_uploaded(self, event_key: str) -> None:
        with self._lock:
            self._data["pending_uploads"] = [
                item for item in self._data["pending_uploads"] if item.get("event_key") != event_key
            ]
            if event_key not in self._data["uploaded_events"]:
                self._data["uploaded_events"].append(event_key)
            self._save()
