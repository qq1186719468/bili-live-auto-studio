from __future__ import annotations

import logging
import hashlib
import threading
import time
from datetime import datetime
from pathlib import Path

from .api import BilibiliLiveAPI, RoomIdentityMismatchError
from .auto_clipper import generate_after_live_upload
from .config import Config, RoomConfig
from .models import Recording
from .directory_recorder import DirectoryRecorder
from .recorder import Recorder, RecordingError
from .recorder_config import validate_recorder_room
from .state import StateStore
from .uploader import ProgressCallback, Uploader
from .upload_history import UploadHistoryStore
from .retention import RetentionManager

LOGGER = logging.getLogger(__name__)


class Application:
    def __init__(self, config: Config, upload_progress_callback: ProgressCallback | None = None) -> None:
        self.config = config
        self.upload_progress_callback = upload_progress_callback
        self.api = BilibiliLiveAPI()
        self.recorder = Recorder(config.recording, config.app.work_dir / "recordings")
        self.directory_recorder = DirectoryRecorder(config.recording)
        self.uploader = Uploader(config.upload, upload_progress_callback)
        self.state = StateStore(config.app.work_dir / "state.json")
        self.history = UploadHistoryStore(config.app.work_dir / "data" / "upload_history.json")
        self.retention = RetentionManager(config.recording, self.history)
        self.stop_event = threading.Event()
        self._auto_clip_lock = threading.Lock()
        self._auto_clip_sessions: set[str] = set()

    @staticmethod
    def _session_key(recording: Recording) -> str:
        return recording.event_key.split(":segment:", 1)[0]

    def _upload(self, recording: Recording) -> bool:
        session_key = self._session_key(recording)
        history_id = self.history.ensure_recording(recording, source="auto", session_key=session_key)
        if not self.config.upload.enabled:
            self.history.update(history_id, "pending", "自动投稿未启用")
            LOGGER.warning("投稿未启用，文件已加入待投稿队列：%s", recording.path)
            if self.upload_progress_callback:
                self.upload_progress_callback(0.0, "自动投稿未启用")
            return False
        existing_bvid = self.history.session_bvid(session_key)
        if not existing_bvid and self.history.session_waiting_for_first(session_key, history_id):
            self.history.update(history_id, "pending", "等待本场直播首个分段投稿成功后再追加")
            LOGGER.warning("本场直播的首个稿件尚未成功，当前分段保留在待投稿队列：%s", recording.path)
            return False
        self.history.update(history_id, "uploading", "正在调用 biliup")
        try:
            if existing_bvid:
                self.uploader.append(recording, existing_bvid)
                bvid = existing_bvid
                success_message = f"已追加到同场稿件 {existing_bvid}"
            else:
                bvid = self.uploader.upload(recording)
                success_message = "本场直播首个稿件投稿成功"
        except Exception as exc:
            self.history.update(history_id, "failed", str(exc))
            if self.upload_progress_callback:
                self.upload_progress_callback(0.0, f"投稿失败：{exc}")
            LOGGER.exception("投稿失败，稍后会从队列重试：%s", recording.path)
            return False
        self.history.update(history_id, "success", success_message, bvid=bvid or "")
        self.state.mark_uploaded(recording.event_key)
        self._schedule_auto_clips(recording)
        return True

    def _schedule_auto_clips(self, recording: Recording) -> None:
        """Start one post-upload clip job per live session, without auto-upload."""

        if not self.config.clip_ai.auto_after_live_upload:
            return
        session_key = self._session_key(recording)
        with self._auto_clip_lock:
            if session_key in self._auto_clip_sessions:
                LOGGER.info("本场直播已启动自动切片，跳过重复任务：%s", session_key)
                return
            self._auto_clip_sessions.add(session_key)

        def work() -> None:
            try:
                results = generate_after_live_upload(
                    recording,
                    self.config,
                    lambda message: LOGGER.info("%s", message),
                )
                added = 0
                for result in results:
                    file_path = str(result.video.resolve())
                    if self.history.files_are_claimed([file_path]):
                        continue
                    self.history.create(
                        (file_path,),
                        result.candidate.title,
                        "clip",
                        event_key=f"auto-clip:{session_key}:{result.candidate.id}",
                        status="pending",
                        message="下播后自动生成，等待手动投稿",
                    )
                    added += 1
                LOGGER.info("本场直播自动切片完成：生成 %d 条，新增 %d 条待手动投稿台账", len(results), added)
            except Exception:
                LOGGER.exception("本场直播投稿成功，但下播后自动切片失败：%s", recording.path)

        threading.Thread(target=work, daemon=True, name="auto-clip-after-live").start()

    def retry_pending(self) -> None:
        pending = self.state.pending()
        if pending:
            LOGGER.info("发现 %d 个待投稿文件", len(pending))
        for recording in pending:
            if self.stop_event.is_set():
                return
            self._upload(recording)

    @staticmethod
    def _segment_event_key(room, paths: list[str]) -> str:
        parts: list[str] = []
        for value in paths:
            path = Path(value)
            try:
                stat = path.stat()
                parts.append(f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}")
            except OSError:
                parts.append(str(path.resolve()))
        digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
        return f"{room.event_key}:segment:{digest}"

    def process_room_once(self, room_config: RoomConfig) -> str:
        room = self.api.get_room(room_config.id, room_config.name)
        if not room.is_live:
            LOGGER.info("未开播：%s（房间 %s）", room.streamer, room.room_id)
            return "idle"
        directory_backend = self.config.recording.backend in {"bililiverecorder", "livehime"}
        if not directory_backend and self.state.has_recorded(room.event_key):
            LOGGER.info("本场远程直播已录制，B站开播时间：%s", room.live_time)
            return "skipped"
        LOGGER.info("检测到开播：%s - %s", room.streamer, room.title)
        LOGGER.info("B站返回的本场开播时间：%s", room.live_time or "未知")
        if directory_backend:
            outputs = self.directory_recorder.record(
                room,
                lambda: self.api.get_room(room_config.id, room_config.name).is_live,
                self.stop_event,
                self.config.app.poll_seconds,
            )
            output_values = [str(path) for path in outputs]
        else:
            output_values = [str(self.recorder.record(room))]
        if not output_values:
            return "idle"
        if self.history.files_are_claimed(output_values):
            LOGGER.info("本地录像已经在投稿台账中，跳过重复处理：%s", output_values[0])
            return "skipped"
        event_key = self._segment_event_key(room, output_values) if directory_backend else room.event_key
        if self.state.has_recorded(event_key):
            LOGGER.info("本地录像分段已经进入过投稿队列，跳过：%s", output_values[0])
            return "skipped"
        recording = Recording.from_room(room, output_values, datetime.now(), event_key=event_key)
        self.state.add_pending(recording)
        self._upload(recording)
        return "handled"

    def _room_worker(self, room: RoomConfig) -> None:
        while not self.stop_event.is_set():
            delay = self.config.app.poll_seconds
            try:
                outcome = self.process_room_once(room)
                if outcome == "handled":
                    delay = 1
            except RoomIdentityMismatchError as exc:
                LOGGER.error("主播身份校验失败：%s", exc)
                break
            except RecordingError as exc:
                if self.stop_event.is_set():
                    LOGGER.info("房间 %s 的监控已停止", room.id)
                    break
                delay = self.config.app.error_retry_seconds
                LOGGER.error("处理直播间 %s 失败：%s", room.id, exc)
            except Exception:
                delay = self.config.app.error_retry_seconds
                LOGGER.exception("处理直播间 %s 时发生错误", room.id)
            self.stop_event.wait(delay)

    def run(self, once: bool = False) -> None:
        rooms = [room for room in self.config.rooms if room.enabled]
        if not rooms:
            raise RuntimeError("没有启用的直播间，请编辑 [[rooms]]")
        if self.config.recording.backend in {"bililiverecorder", "livehime"}:
            for room in rooms:
                validate_recorder_room(self.config.recording.watch_dir, room.id)
        for room in rooms:
            checked = self.api.get_room(room.id, room.name)
            LOGGER.info(
                "房间身份校验通过：%s → %s（B站本场开播时间：%s）",
                room.id,
                checked.streamer,
                checked.live_time or "未开播",
            )
        self.retry_pending()
        room_ids = {room.id for room in rooms}
        report = self.retention.cleanup(room_ids)
        LOGGER.info(
            "录像保留检查完成：删除 %d 个文件，释放 %.1f MiB，保留未投稿文件 %d 个",
            report.deleted_files,
            report.deleted_bytes / 1024 / 1024,
            report.retained_unuploaded,
        )
        if once:
            for room in rooms:
                self.process_room_once(room)
            return

        threads = [
            threading.Thread(target=self._room_worker, args=(room,), name=f"room-{room.id}", daemon=True)
            for room in rooms
        ]
        for thread in threads:
            thread.start()
        LOGGER.info("监控已启动，共 %d 个直播间；按 Ctrl+C 停止", len(threads))
        next_cleanup = time.monotonic() + self.config.recording.retention_scan_minutes * 60
        try:
            while any(thread.is_alive() for thread in threads):
                time.sleep(0.5)
                if time.monotonic() >= next_cleanup:
                    report = self.retention.cleanup(room_ids)
                    if report.deleted_files or report.retained_unuploaded or report.errors:
                        LOGGER.info(
                            "定时清理完成：删除 %d 个，释放 %.1f MiB，保留未投稿 %d 个，失败 %d 个",
                            report.deleted_files,
                            report.deleted_bytes / 1024 / 1024,
                            report.retained_unuploaded,
                            report.errors,
                        )
                    next_cleanup = time.monotonic() + self.config.recording.retention_scan_minutes * 60
        except KeyboardInterrupt:
            LOGGER.info("正在停止……")
            self.stop_event.set()
        for thread in threads:
            thread.join(timeout=5)
