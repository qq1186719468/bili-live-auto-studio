from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from .config import RecordingSettings
from .models import LiveRoom
from .recorder import RecordingError

LOGGER = logging.getLogger(__name__)

Fingerprint = tuple[int, int]


class DirectoryRecorder:
    """Observe video files produced by BililiveRecorder or another recorder."""

    def __init__(self, settings: RecordingSettings) -> None:
        self.settings = settings

    def snapshot(self, room_id: int | None = None) -> dict[Path, Fingerprint]:
        directory = self.settings.watch_dir
        if not directory.is_dir():
            raise RecordingError(f"录播姬工作目录不存在：{directory}")
        extensions = {f".{value}" for value in self.settings.watch_extensions}
        result: dict[Path, Fingerprint] = {}
        for path in directory.rglob("*"):
            try:
                if path.is_file() and path.suffix.lower() in extensions:
                    relative_text = str(path.relative_to(directory))
                    if room_id is not None and str(room_id) not in relative_text:
                        continue
                    stat = path.stat()
                    result[path.resolve()] = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                continue
        return result

    def _wait_until_stable(
        self,
        baseline: dict[Path, Fingerprint],
        timeout_seconds: float,
        stable_seconds: float,
        cancel_event: threading.Event | None = None,
        room_id: int | None = None,
    ) -> list[Path]:
        deadline = time.monotonic() + timeout_seconds
        previous: dict[Path, Fingerprint] = {}
        stable_since: float | None = None
        while not (cancel_event and cancel_event.is_set()) and time.monotonic() < deadline:
            current = self.changed_since(baseline, room_id)
            if current and current == previous:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= stable_seconds:
                    return sorted(current, key=lambda value: current[value][1])
            else:
                stable_since = None
                previous = current
            if cancel_event:
                cancel_event.wait(min(1, self.settings.local_scan_seconds))
            else:
                time.sleep(min(1, self.settings.local_scan_seconds))
        raise RecordingError("等待录播姬完成录像写入超时")

    def changed_since(
        self, baseline: dict[Path, Fingerprint], room_id: int | None = None
    ) -> dict[Path, Fingerprint]:
        snapshot = self.snapshot(room_id)
        minimum = int(self.settings.min_file_size_mb * 1024 * 1024)
        return {
            path: fingerprint
            for path, fingerprint in snapshot.items()
            if baseline.get(path) != fingerprint and fingerprint[0] >= minimum
        }

    def record(
        self,
        room: LiveRoom,
        is_live: Callable[[], bool],
        stop_event: threading.Event,
        poll_seconds: int,
    ) -> list[Path]:
        baseline = self.snapshot(room.room_id)
        LOGGER.info("录播姬模式：正在监听工作目录 %s", self.settings.watch_dir)
        LOGGER.info("已启用本地文件检测：下播或录像停止写入后将收集成片")

        previous: dict[Path, Fingerprint] = {}
        stable_since: float | None = None
        next_live_check = time.monotonic() + poll_seconds
        went_offline = False
        while not stop_event.is_set():
            current = self.changed_since(baseline, room.room_id)
            if current and current == previous:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= self.settings.manual_stop_stable_seconds:
                    LOGGER.info("检测到本地录像已停止写入，按手动中断录播处理")
                    return sorted(current, key=lambda value: current[value][1])
            else:
                previous = current
                stable_since = None

            if time.monotonic() >= next_live_check:
                try:
                    if not is_live():
                        went_offline = True
                        break
                except Exception as exc:
                    LOGGER.warning("暂时无法查询下播状态，将继续等待：%s", exc)
                next_live_check = time.monotonic() + poll_seconds
            stop_event.wait(self.settings.local_scan_seconds)

        if stop_event.is_set():
            LOGGER.info("收到停止请求，正在检查本场本地录像文件")
            if not self.changed_since(baseline, room.room_id):
                raise RecordingError("监控已停止，本轮没有发现新增或变化的本地录像")
            try:
                stable = self._wait_until_stable(
                    baseline,
                    self.settings.interrupt_finalize_timeout_seconds,
                    self.settings.stable_seconds,
                    room_id=room.room_id,
                )
            except RecordingError as exc:
                raise RecordingError("监控已停止，未发现已经停止写入的本场录像，因此不会强行上传") from exc
            LOGGER.info("停止监控前发现 %d 个已完成录像文件，将继续进入投稿队列", len(stable))
            return stable

        assert went_offline
        if not self.changed_since(baseline, room.room_id):
            LOGGER.info("直播已经结束，但本轮没有发现新增本地录像")
            return []
        stable = self._wait_until_stable(
            baseline,
            self.settings.stable_timeout_seconds,
            self.settings.stable_seconds,
            stop_event,
            room.room_id,
        )
        LOGGER.info("发现 %d 个录播姬录像文件", len(stable))
        return stable


# 兼容 0.1.0 开发阶段的内部名称。
LivehimeRecorder = DirectoryRecorder
