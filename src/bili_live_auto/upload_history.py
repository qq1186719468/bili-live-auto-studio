from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Recording

_PROCESS_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class UploadHistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = _PROCESS_LOCK
        self._items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        self._items = []
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        items = data.get("items", []) if isinstance(data, dict) else []
        if isinstance(items, list):
            self._items = [item for item in items if isinstance(item, dict) and item.get("id")]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "items": self._items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    @staticmethod
    def _file_key(value: str) -> str:
        try:
            return str(Path(value).resolve()).casefold()
        except OSError:
            return str(value).casefold()

    def items(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            self._load()
            return tuple(dict(item) for item in reversed(self._items))

    def get(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._load()
            item = next((item for item in self._items if item.get("id") == item_id), None)
            return dict(item) if item else None

    def create(
        self,
        files: tuple[str, ...] | list[str],
        title: str,
        source: str,
        event_key: str = "",
        session_key: str = "",
        status: str = "pending",
        message: str = "",
    ) -> str:
        with self._lock:
            self._load()
            item_id = uuid.uuid4().hex
            now = _now()
            self._items.append(
                {
                    "id": item_id,
                    "event_key": event_key,
                    "session_key": session_key,
                    "source": source,
                    "files": list(files),
                    "title": title,
                    "status": status,
                    "message": message,
                    "bvid": "",
                    "created_at": now,
                    "updated_at": now,
                }
            )
            self._save()
            return item_id

    def ensure_recording(self, recording: Recording, source: str, session_key: str = "") -> str:
        with self._lock:
            self._load()
            existing = next(
                (item for item in self._items if item.get("event_key") == recording.event_key),
                None,
            )
            if existing:
                return str(existing["id"])
            recording_keys = {self._file_key(value) for value in recording.files}
            untracked = next(
                (
                    item
                    for item in self._items
                    if item.get("status") == "untracked"
                    and {self._file_key(value) for value in item.get("files", [])} == recording_keys
                ),
                None,
            )
            if untracked:
                untracked["event_key"] = recording.event_key
                untracked["session_key"] = session_key
                untracked["source"] = source
                untracked["title"] = recording.title or Path(recording.path).stem
                untracked["status"] = "pending"
                untracked["updated_at"] = _now()
                self._save()
                return str(untracked["id"])
        return self.create(
            recording.files,
            recording.title or Path(recording.path).stem,
            source,
            event_key=recording.event_key,
            session_key=session_key,
        )

    def update(self, item_id: str, status: str, message: str = "", bvid: str = "") -> None:
        with self._lock:
            self._load()
            item = next((item for item in self._items if item.get("id") == item_id), None)
            if not item:
                return
            item["status"] = status
            item["message"] = message
            if bvid:
                item["bvid"] = bvid
            item["updated_at"] = _now()
            self._save()

    def recover_interrupted_uploads(self) -> int:
        """Mark work left in `uploading` by an earlier client process as unknown.

        A forced exit can happen after biliup has submitted the video but before
        this client receives its final output.  Calling such entries "failed"
        would invite a duplicate upload, so they deliberately require a manual
        check in Bilibili's creator center.
        """
        with self._lock:
            self._load()
            changed = 0
            for item in self._items:
                if item.get("status") != "uploading":
                    continue
                previous = str(item.get("message") or "正在调用 biliup")
                item["status"] = "interrupted"
                item["message"] = (
                    "客户端在等待 biliup 结果时退出，投稿结果待确认；"
                    "请先在创作中心确认是否已生成稿件，确认未投稿后再手动重试。"
                    f"（退出前状态：{previous}）"
                )
                item["updated_at"] = _now()
                changed += 1
            if changed:
                self._save()
            return changed

    def replace_files(self, item_id: str, files: tuple[str, ...] | list[str]) -> None:
        with self._lock:
            self._load()
            item = next((item for item in self._items if item.get("id") == item_id), None)
            if not item:
                return
            item["files"] = list(files)
            item["updated_at"] = _now()
            self._save()

    def set_source(self, item_id: str, source: str) -> None:
        with self._lock:
            self._load()
            item = next((item for item in self._items if item.get("id") == item_id), None)
            if not item:
                return
            item["source"] = source
            item["updated_at"] = _now()
            self._save()

    def set_session(self, item_id: str, session_key: str, event_key: str = "") -> None:
        with self._lock:
            self._load()
            item = next((item for item in self._items if item.get("id") == item_id), None)
            if not item:
                return
            item["session_key"] = session_key
            if event_key:
                item["event_key"] = event_key
            item["updated_at"] = _now()
            self._save()

    def set_title(self, item_id: str, title: str) -> None:
        with self._lock:
            self._load()
            item = next((item for item in self._items if item.get("id") == item_id), None)
            if not item:
                return
            item["title"] = title
            item["updated_at"] = _now()
            self._save()

    def edit(
        self,
        item_id: str,
        *,
        files: tuple[str, ...] | list[str],
        title: str,
        source: str,
        status: str,
        message: str,
        bvid: str,
    ) -> bool:
        """Edit a ledger entry without touching any local recording file."""

        with self._lock:
            self._load()
            item = next((item for item in self._items if item.get("id") == item_id), None)
            if not item:
                return False
            item["files"] = list(files)
            item["title"] = title
            item["source"] = source
            item["status"] = status
            item["message"] = message
            item["bvid"] = bvid
            item["updated_at"] = _now()
            self._save()
            return True

    def delete(self, item_ids: tuple[str, ...] | list[str]) -> int:
        """Delete ledger entries only; referenced video files are preserved."""

        wanted = {str(item_id) for item_id in item_ids}
        if not wanted:
            return 0
        with self._lock:
            self._load()
            before = len(self._items)
            self._items = [item for item in self._items if str(item.get("id")) not in wanted]
            removed = before - len(self._items)
            if removed:
                self._save()
            return removed

    def discover_files(self, files: list[Path], source: str = "scan") -> int:
        with self._lock:
            self._load()
            known = {
                self._file_key(value)
                for item in self._items
                for value in item.get("files", [])
            }
            added = 0
            for path in files:
                key = self._file_key(str(path))
                if key in known:
                    continue
                now = _now()
                self._items.append(
                    {
                        "id": uuid.uuid4().hex,
                        "event_key": "",
                        "session_key": "",
                        "source": source,
                        "files": [str(path.resolve())],
                        "title": path.stem,
                        "status": "untracked",
                        "message": (
                            "本地切片成片，尚无本客户端投稿记录"
                            if source == "clip_scan"
                            else "历史文件，尚无本客户端投稿记录"
                        ),
                        "bvid": "",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                known.add(key)
                added += 1
            if added:
                self._save()
            return added

    def files_are_claimed(self, files: tuple[str, ...] | list[str]) -> bool:
        wanted = {self._file_key(value) for value in files}
        if not wanted:
            return False
        with self._lock:
            self._load()
            claimed = {
                self._file_key(value)
                for item in self._items
                if item.get("status") in {"pending", "uploading", "interrupted", "success"}
                for value in item.get("files", [])
            }
        return wanted.issubset(claimed)

    def session_bvid(self, session_key: str) -> str | None:
        if not session_key:
            return None
        with self._lock:
            self._load()
            for item in reversed(self._items):
                if item.get("session_key") == session_key and item.get("status") == "success" and item.get("bvid"):
                    return str(item["bvid"])
        return None

    def session_waiting_for_first(self, session_key: str, exclude_id: str) -> bool:
        if not session_key:
            return False
        with self._lock:
            self._load()
            return any(
                item.get("id") != exclude_id
                and item.get("session_key") == session_key
                and item.get("status") in {"pending", "uploading", "interrupted", "failed"}
                and not item.get("bvid")
                for item in self._items
            )

    def file_was_uploaded(self, file_path: str) -> bool:
        wanted = self._file_key(file_path)
        with self._lock:
            self._load()
            return any(
                item.get("status") == "success"
                and any(self._file_key(value) == wanted for value in item.get("files", []))
                for item in self._items
            )

    def mark_file_deleted(self, file_path: str) -> None:
        wanted = self._file_key(file_path)
        with self._lock:
            self._load()
            changed = False
            for item in self._items:
                if any(self._file_key(value) == wanted for value in item.get("files", [])):
                    suffix = "本地录像已按保留策略删除"
                    message = str(item.get("message") or "")
                    if suffix not in message:
                        item["message"] = f"{message}；{suffix}".strip("；")
                    if item.get("status") == "untracked":
                        item["status"] = "deleted"
                    item["updated_at"] = _now()
                    changed = True
            if changed:
                self._save()
