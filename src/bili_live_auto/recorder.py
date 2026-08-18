from __future__ import annotations

import logging
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .config import RecordingSettings
from .models import LiveRoom

LOGGER = logging.getLogger(__name__)


class RecordingError(RuntimeError):
    pass


def safe_filename(value: str, limit: int = 80) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    value = re.sub(r"\s+", " ", value)
    return (value or "live")[:limit]


class Recorder:
    def __init__(self, settings: RecordingSettings, output_dir: Path) -> None:
        self.settings = settings
        self.output_dir = output_dir

    def output_path(self, room: LiveRoom, now: datetime) -> Path:
        stamp = now.strftime("%Y%m%d_%H%M%S")
        name = safe_filename(room.streamer or str(room.room_id), 48)
        return self.output_dir / str(room.room_id) / f"{name}_{stamp}.{self.settings.extension}"

    def build_command(self, room: LiveRoom, output: Path) -> list[str]:
        command = [
            self.settings.executable,
            "--no-playlist",
            "--newline",
            "--no-progress",
            "--hls-use-mpegts",
            "--format",
            self.settings.format,
            "--output",
            str(output),
        ]
        if self.settings.cookie_file:
            command.extend(["--cookies", str(self.settings.cookie_file)])
        command.extend(self.settings.extra_args)
        command.append(room.url)
        return command

    def record(self, room: LiveRoom, now: datetime | None = None) -> Path:
        now = now or datetime.now()
        output = self.output_path(room, now)
        output.parent.mkdir(parents=True, exist_ok=True)
        command = self.build_command(room, output)
        LOGGER.info("开始录制 %s（%s）", room.streamer, room.url)
        try:
            result = subprocess.run(command, check=False)
        except FileNotFoundError as exc:
            raise RecordingError(f"找不到录制程序：{self.settings.executable}") from exc
        except KeyboardInterrupt:
            LOGGER.warning("收到停止信号，正在结束录制")
            raise

        candidates = [output, Path(f"{output}.part")]
        actual = next((path for path in candidates if path.is_file()), None)
        minimum = int(self.settings.min_file_size_mb * 1024 * 1024)
        if actual is None or actual.stat().st_size < minimum:
            raise RecordingError(
                f"录制未生成有效文件（退出码 {result.returncode}，最小 {self.settings.min_file_size_mb:g} MiB）"
            )
        if actual.suffix == ".part":
            completed = actual.with_suffix("")
            shutil.move(str(actual), str(completed))
            actual = completed
        if result.returncode != 0:
            LOGGER.warning("录制程序以 %s 退出，但已保留有效文件：%s", result.returncode, actual)
        else:
            LOGGER.info("录制完成：%s", actual)
        return actual

