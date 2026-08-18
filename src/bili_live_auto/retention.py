from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .config import RecordingSettings
from .upload_history import UploadHistoryStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupReport:
    deleted_files: int = 0
    deleted_bytes: int = 0
    retained_unuploaded: int = 0
    errors: int = 0


class RetentionManager:
    def __init__(self, settings: RecordingSettings, history: UploadHistoryStore) -> None:
        self.settings = settings
        self.history = history

    def cleanup(self, room_ids: set[int]) -> CleanupReport:
        root = self.settings.watch_dir.resolve()
        if not root.is_dir() or not room_ids:
            return CleanupReport()
        cutoff = time.time() - self.settings.retention_hours * 3600
        extensions = {f".{value}" for value in self.settings.watch_extensions}
        deleted_files = deleted_bytes = retained_unuploaded = errors = 0
        for path in root.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file() or path.suffix.lower() not in extensions:
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    continue
                relative = resolved.relative_to(root)
                lowered_parts = {part.casefold() for part in relative.parts}
                if lowered_parts.intersection({"data", "src", "tests", "secrets"}):
                    continue
                if not any(str(room_id) in str(relative) for room_id in room_ids):
                    continue
                stat = resolved.stat()
                if stat.st_mtime >= cutoff:
                    continue
                uploaded = self.history.file_was_uploaded(str(resolved))
                if self.settings.delete_only_uploaded and not uploaded:
                    retained_unuploaded += 1
                    continue
                size = stat.st_size
                resolved.unlink()
                self.history.mark_file_deleted(str(resolved))
                deleted_files += 1
                deleted_bytes += size
                LOGGER.info("已清理过期录像：%s（%.1f MiB）", resolved, size / 1024 / 1024)
            except OSError as exc:
                errors += 1
                LOGGER.warning("清理录像失败：%s：%s", path, exc)
        return CleanupReport(deleted_files, deleted_bytes, retained_unuploaded, errors)
