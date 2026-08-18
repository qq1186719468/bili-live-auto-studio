from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse


class OnlineSourceError(RuntimeError):
    pass


OnlineProgress = Callable[[float | None, str], None]
_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)%")
_PATH_MARKER = "ONLINE_SOURCE:"
_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".flv", ".ts", ".mov"}


def validate_online_url(value: str) -> str:
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OnlineSourceError("在线来源必须是 http:// 或 https:// 视频链接")
    return text


def resolve_downloader(preferred: str, extra_dirs: Iterable[Path] = ()) -> str:
    value = preferred.strip() or "yt-dlp"
    direct = Path(value).expanduser()
    if direct.is_file():
        return str(direct.resolve())
    for directory in extra_dirs:
        candidate = directory.expanduser() / value
        if candidate.is_file():
            return str(candidate.resolve())
        if candidate.suffix.lower() != ".exe":
            candidate = candidate.with_suffix(".exe")
            if candidate.is_file():
                return str(candidate.resolve())
    found = shutil.which(value) or shutil.which(f"{value}.exe")
    if found:
        return found
    raise OnlineSourceError(f"找不到下载程序：{value}。请安装 yt-dlp 或在录制设置中填写完整路径")


def build_download_command(executable: str, url: str, output_dir: Path) -> list[str]:
    url = validate_online_url(url)
    output_dir = output_dir.expanduser().resolve()
    template = output_dir / "%(title).120s [%(id)s].%(ext)s"
    return [
        executable,
        "--no-playlist",
        "--newline",
        "--no-warnings",
        "--windows-filenames",
        "--format",
        "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "--merge-output-format",
        "mp4",
        "--output",
        str(template),
        "--print",
        f"after_move:{_PATH_MARKER}%(filepath)s",
        url,
    ]


def _candidate_files(output_dir: Path) -> tuple[Path, ...]:
    try:
        return tuple(
            path
            for path in output_dir.iterdir()
            if path.is_file() and path.suffix.lower() in _VIDEO_EXTENSIONS
        )
    except OSError:
        return ()


def download_online_video(
    url: str,
    executable: str,
    output_dir: Path,
    progress: OnlineProgress | None = None,
) -> Path:
    """Download one authorized online video and return its local media path."""

    url = validate_online_url(url)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    before = set(_candidate_files(output_dir))
    command = build_download_command(executable, url, output_dir)
    if progress:
        progress(None, "正在下载在线来源；只下载到本地，不会上传原视频")
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise OnlineSourceError(f"无法启动下载程序：{exc}") from exc

    reported: Path | None = None
    output_lines: list[str] = []
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.strip()
        if line:
            output_lines.append(line)
            del output_lines[:-20]
        if _PATH_MARKER in line:
            reported = Path(line.split(_PATH_MARKER, 1)[1].strip()).expanduser()
        match = _PERCENT_RE.search(line)
        if progress and match:
            progress(min(100.0, float(match.group(1))), f"正在下载在线来源：{match.group(1)}%")
    return_code = process.wait()
    if return_code != 0:
        detail = "\n".join(output_lines[-6:]) or f"退出码 {return_code}"
        raise OnlineSourceError(f"在线来源下载失败（退出码 {return_code}）：\n{detail[:1600]}")

    candidates = []
    if reported is not None and reported.is_file():
        candidates.append(reported)
    candidates.extend(path for path in _candidate_files(output_dir) if path not in candidates and path not in before)
    if not candidates:
        candidates.extend(_candidate_files(output_dir))
    candidates = [path for path in candidates if path.is_file() and path.stat().st_size >= 1024 * 1024]
    if not candidates:
        raise OnlineSourceError("下载完成，但没有找到大于 1 MiB 的可用视频文件")
    result = max(candidates, key=lambda path: path.stat().st_mtime)
    if progress:
        progress(100.0, f"在线来源下载完成：{result.name}")
    return result.resolve()

