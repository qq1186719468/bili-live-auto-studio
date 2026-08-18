from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, BooleanVar, Canvas, DoubleVar, StringVar, Tk, Toplevel, filedialog, messagebox
from tkinter import scrolledtext, ttk

try:
    from PIL import Image, ImageDraw
    import pystray
except ImportError:  # 源码模式未安装 GUI 可选依赖时仍可退化为任务栏最小化。
    Image = None
    ImageDraw = None
    pystray = None

from .api import BilibiliLiveAPI, RoomIdentityMismatchError, streamer_names_match
from .app import Application
from .clipper import (
    ClipAnalysis,
    ClipCandidate,
    ClipperError,
    analyze_video,
    candidate_count_for_duration,
    find_ffmpeg,
    find_model_directory,
    format_timestamp,
    merge_sources,
)
from .clip_renderer import ClipRenderResult, render_candidates
from .clip_ai import (
    ClipAIError,
    enhance_analysis_with_fallback,
    load_current_cc_switch_provider,
    read_api_key,
    save_api_key,
    test_api_connection,
)
from .config import ClipAISettings, ConfigError, load_config
from .recorder_config import RecorderConfigError, validate_recorder_room
from .models import LiveRoom, Recording
from .online_source import OnlineSourceError, download_online_video, resolve_downloader
from .upload_history import UploadHistoryStore
from .uploader import Uploader
from .uploader import render as render_upload_text
from .recording_time import recording_start_time

APP_TITLE = "录播自动上传助手"
LOCAL_VIDEO_EXTENSIONS = {".flv", ".mp4", ".mkv", ".ts"}
MIN_LEDGER_VIDEO_BYTES = 1024 * 1024
ROOM_RECORDING_DIRECTORY = re.compile(r"^\d+(?:[-_].+)?$")
ROOM_RECORDING_IDENTITY = re.compile(r"^(?P<room_id>\d+)(?:[-_](?P<streamer>.+))?$")
RECORDED_FILE_TITLE = re.compile(
    r"^录制-(?P<room_id>\d+)-(?P<date>\d{8})-(?P<time>\d{6})-(?P<sequence>\d+)-(?P<title>.+)$"
)


def recording_identity(path: Path) -> tuple[int | None, str]:
    """Read the room identity embedded in a BililiveRecorder folder name.

    BililiveRecorder normally stores files below ``<room_id>-<主播名>``.  The
    underscore variant is accepted as well because older recorder versions
    and manually renamed folders used it.  Returning an empty name means the
    path did not carry a trustworthy streamer label and callers should use a
    configured/API fallback.
    """

    parent = path.expanduser().parent.name.strip()
    match = ROOM_RECORDING_IDENTITY.fullmatch(parent)
    if not match:
        return None, ""
    try:
        room_id = int(match.group("room_id"))
    except (TypeError, ValueError):
        return None, ""
    return room_id, str(match.group("streamer") or "").strip()


def recording_streamer_name(path: Path, fallback: str = "") -> str:
    """Return the streamer encoded by a recording path, with a safe fallback."""

    _room_id, streamer = recording_identity(path)
    if streamer:
        return streamer
    parent = path.expanduser().parent.name.strip()
    if " _ " in parent:
        return parent.split(" _ ", 1)[0].strip()
    return fallback.strip()


def recording_file_title(path: Path, fallback: str = "") -> str:
    """Extract the original live title from a recorder-generated filename."""

    match = RECORDED_FILE_TITLE.match(path.expanduser().stem)
    if match:
        return str(match.group("title") or "").strip() or fallback.strip()
    return fallback.strip()


def find_local_recordings(work_dir: Path) -> list[Path]:
    """Find the recorder's room videos without walking caches and build trees.

    Recordings smaller than 1 MiB are treated as unusable fragments.  Upload-
    time validation remains responsible for the other eligibility checks.
    """

    root = work_dir.expanduser()
    if not root.is_dir():
        return []
    scan_roots: list[Path] = []
    direct_files: list[Path] = []
    try:
        entries = tuple(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.is_dir() and ROOM_RECORDING_DIRECTORY.fullmatch(entry.name):
                scan_roots.append(entry)
            elif entry.is_file() and entry.suffix.lower() in LOCAL_VIDEO_EXTENSIONS:
                direct_files.append(entry)
        except OSError:
            continue

    found: list[tuple[float, Path]] = []
    candidates = list(direct_files)
    for room_root in scan_roots:
        candidates.extend(room_root.rglob("*"))
    for path in candidates:
        try:
            stat = path.stat()
            if (
                path.is_file()
                and path.suffix.lower() in LOCAL_VIDEO_EXTENSIONS
                and stat.st_size >= MIN_LEDGER_VIDEO_BYTES
            ):
                found.append((stat.st_mtime, path.resolve()))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _mtime, path in found]


def find_videos_in_directory(directory: Path) -> list[Path]:
    """Recursively inventory videos in a user-selected directory."""

    root = directory.expanduser()
    if not root.is_dir():
        return []
    found: list[tuple[float, Path]] = []
    try:
        candidates = root.rglob("*")
        for path in candidates:
            try:
                stat = path.stat()
                if (
                    path.is_file()
                    and path.suffix.lower() in LOCAL_VIDEO_EXTENSIONS
                    and stat.st_size >= MIN_LEDGER_VIDEO_BYTES
                ):
                    found.append((stat.st_mtime, path.resolve()))
            except OSError:
                continue
    except OSError:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _mtime, path in found]


def format_candidate_ranges(candidate: ClipCandidate) -> str:
    ranges = candidate.timeline_ranges
    rendered = " → ".join(
        f"{format_timestamp(start)}-{format_timestamp(end)}" for start, end in ranges
    )
    return rendered if len(ranges) == 1 else f"{len(ranges)} 段｜{rendered}"

PALETTES = {
    "dark": {
        "bg": "#090B0E",
        "surface": "#11151A",
        "panel": "#171C23",
        "input": "#0D1117",
        "text": "#F5F7FA",
        "muted": "#98A2B3",
        "border": "#2A3441",
        "accent": "#00AEEC",
        "accent_hover": "#1BC3FF",
        "pink": "#FB7299",
        "pink_hover": "#FF8BAD",
        "success": "#32D583",
        "danger": "#F04438",
        "warning": "#FDB022",
        "selection": "#164B63",
    },
    "light": {
        "bg": "#F2F5F8",
        "surface": "#FFFFFF",
        "panel": "#F8FAFC",
        "input": "#FFFFFF",
        "text": "#1D2939",
        "muted": "#667085",
        "border": "#D8E0E8",
        "accent": "#00A1D6",
        "accent_hover": "#008FBE",
        "pink": "#FB7299",
        "pink_hover": "#E96088",
        "success": "#12B76A",
        "danger": "#D92D20",
        "warning": "#DC6803",
        "selection": "#B9E9F8",
    },
}


def app_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    return app_directory() / "bili_live_auto.toml"


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _existing_room_id(directory: Path) -> str:
    path = directory / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data["rooms"][0]["RoomId"]
        return str(raw.get("Value", "") if isinstance(raw, dict) else raw)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return ""


def write_config(values: dict[str, object], path: Path) -> None:
    tags = [item.strip() for item in str(values["tags"]).replace("，", ",").split(",") if item.strip()]
    tag_text = ", ".join(_toml_string(item) for item in tags)
    clip_tags = [
        item.strip()
        for item in str(values.get("clip_tags", "直播切片,高能切片")).replace("，", ",").split(",")
        if item.strip()
    ]
    clip_tag_text = ", ".join(_toml_string(item) for item in clip_tags)
    text = f'''[app]
poll_seconds = 30
error_retry_seconds = 60
work_dir = {_toml_string(str(values["work_dir"]))}
log_level = "INFO"
theme = {_toml_string(str(values.get("theme", "dark")))}

[recording]
backend = "bililiverecorder"
watch_dir = {_toml_string(str(values["work_dir"]))}
watch_extensions = ["mp4", "flv", "mkv", "ts"]
stable_seconds = 15
stable_timeout_seconds = 300
local_scan_seconds = 2
manual_stop_stable_seconds = 30
interrupt_finalize_timeout_seconds = 60
retention_hours = {int(values.get("retention_hours", 168))}
retention_scan_minutes = 60
delete_only_uploaded = {str(bool(values.get("delete_only_uploaded", True))).lower()}
min_file_size_mb = 1
executable = "yt-dlp"
format = "best"
extension = "mp4"
cookie_file = ""
extra_args = []

[upload]
enabled = {str(bool(values["upload_enabled"])).lower()}
executable = {_toml_string(str(values["biliup_executable"]))}
cookie_file = {_toml_string(str(values["cookie_file"]))}
line = "bda2"
limit = 3
copyright = {int(values["copyright"])}
submit = "web"
source = "https://live.bilibili.com/{{room_id}}"
tid = {int(values["tid"])}
tags = [{tag_text}]
title = {_toml_string(str(values["title"]))}
publish_at = {_toml_string(str(values.get("publish_at", "")))}
dynamic = {_toml_string(str(values.get("dynamic", "")))}
is_only_self = {str(bool(values.get("is_only_self", False))).lower()}
no_reprint = {str(bool(values.get("no_reprint", False))).lower()}
charging_pay = {str(bool(values.get("charging_pay", False))).lower()}
description = {_toml_string(str(values["description"]))}
extra_args = []

[clip_upload]
enabled = true
executable = {_toml_string(str(values.get("clip_biliup_executable", values["biliup_executable"])))}
cookie_file = {_toml_string(str(values.get("clip_cookie_file", values["cookie_file"])))}
line = "bda2"
limit = 3
copyright = {int(values.get("clip_copyright", values["copyright"]))}
submit = "web"
source = "https://live.bilibili.com/{{room_id}}"
tid = {int(values.get("clip_tid", values["tid"]))}
tags = [{clip_tag_text}]
title = "{{streamer}}直播切片"
publish_at = {_toml_string(str(values.get("clip_publish_at", "")))}
dynamic = ""
is_only_self = false
no_reprint = false
charging_pay = false
description = {_toml_string(str(values.get("clip_description", values["description"])))}
extra_args = []

[clip_ai]
enabled = {str(bool(values.get("clip_ai_enabled", False))).lower()}
base_url = {_toml_string(str(values.get("clip_ai_base_url", "")))}
model = {_toml_string(str(values.get("clip_ai_model", "")))}
protocol = {_toml_string(str(values.get("clip_ai_protocol", "auto")))}
api_key_file = {_toml_string(str(values.get("clip_ai_key_file", "./secrets/clip-ai-key.txt")))}
timeout_seconds = {int(values.get("clip_ai_timeout_seconds", 90))}
chunk_minutes = {int(values.get("clip_ai_chunk_minutes", 30))}

[[rooms]]
id = {int(values["room_id"])}
name = {_toml_string(str(values["streamer"]))}
enabled = true
'''
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def find_recorder_executable() -> Path | None:
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "BililiveRecorder"
    candidates = list(local.glob("app-*/BililiveRecorder.WPF.exe"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def clip_runtime_status(base: Path | None = None) -> tuple[bool, str]:
    root = base or app_directory()
    python = root / ".clip-venv-standalone" / "Scripts" / "python.exe"
    model = find_model_directory(root)
    if not python.is_file():
        return False, "切片运行环境未安装；现有切片仍可一键投稿"
    if model is None:
        return False, "Faster-Whisper small 模型未找到；首次分析前需要准备模型"
    if find_ffmpeg() is None:
        return False, "未找到支持字幕渲染的 FFmpeg；只能查看已有切片"
    return True, "Faster-Whisper small 与字幕渲染环境已就绪（自动 GPU / CPU 多线程，保持原识别设置）"


def clip_upload_title(title: str) -> str:
    clean = " ".join(title.split()).strip()
    # Intelligent-clip files are named like
    # ``a04-01-006172-真正的标题-a8f27444.mp4``.  Keep only the human title
    # when a generated filename is used as the upload title.
    clean = re.sub(r"^[A-Za-z]+\d*(?:-\d+){2,3}-", "", clean)
    clean = re.sub(r"-[0-9A-Fa-f]{8}$", "", clean)
    clean = clean.strip(" -_") or "直播切片"
    return clean[:80]


def find_generated_clip_cover(video: Path) -> Path | None:
    """Find the cover emitted next to an intelligent-clip MP4."""
    video = video.expanduser()
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = video.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


def is_recorder_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq BililiveRecorder.WPF.exe", "/NH"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False
    return "BililiveRecorder.WPF.exe" in result.stdout


class QueueLogHandler(logging.Handler):
    def __init__(self, messages: queue.Queue[tuple[str, object]]) -> None:
        super().__init__()
        self.messages = messages
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.put(("log", self.format(record)))


class DesktopClient:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1040x760")
        self.root.minsize(900, 650)
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.application: Application | None = None
        self.worker: threading.Thread | None = None
        self.tray_icon = None
        self.tray_thread: threading.Thread | None = None
        self.exiting = False
        self.last_recorder_running: bool | None = None
        self.style = ttk.Style(self.root)
        self.style.theme_use("clam")

        base = app_directory()
        self.room_id = StringVar(value=_existing_room_id(base))
        self.streamer = StringVar(value="")
        self.work_dir = StringVar(value=str(base))
        self.upload_enabled = BooleanVar(value=False)
        self.biliup_executable = StringVar(value="biliup")
        self.cookie_file = StringVar(value=str(base / "secrets" / "cookies.json"))
        self.tid = StringVar(value="171")
        self.copyright = StringVar(value="2")
        self.tags = StringVar(value="直播录像,录播")
        self.title_template = StringVar(value="{streamer}直播录像 {start_time}")
        self.publish_at = StringVar(value="")
        self.upload_dynamic = StringVar(value="")
        self.upload_private = BooleanVar(value=False)
        self.upload_no_reprint = BooleanVar(value=False)
        self.upload_charging = BooleanVar(value=False)
        self.clip_biliup_executable = StringVar(value="biliup")
        self.clip_cookie_file = StringVar(value=str(base / "secrets" / "clip-cookies.json"))
        self.clip_tid = StringVar(value="138")
        self.clip_copyright = StringVar(value="2")
        self.clip_tags = StringVar(value="直播切片,高能切片")
        self.clip_publish_at = StringVar(value="")
        self.clip_publish_summary = StringVar(value="立即发布")
        self.clip_ai_enabled = BooleanVar(value=False)
        self.clip_ai_base_url = StringVar(value="")
        self.clip_ai_model = StringVar(value="")
        self.clip_ai_protocol = StringVar(value="auto")
        self.clip_ai_key_input = StringVar(value="")
        self.clip_ai_key_file = StringVar(value=str(base / "secrets" / "clip-ai-key.txt"))
        self.clip_ai_timeout_seconds = StringVar(value="90")
        self.clip_ai_chunk_minutes = StringVar(value="30")
        self.clip_ai_status = StringVar(value="API 增强未启用；当前使用本地分析")
        self.status_text = StringVar(value="尚未启动")
        self.recorder_text = StringVar(value="正在检测录播姬……")
        self.theme_mode = StringVar(value="dark")
        self.history_summary = StringVar(value="尚无投稿记录")
        self.history_scan_status = StringVar(value="可扫描录播目录，或选择其他本地录像目录")
        self.manual_append_bvid = StringVar(value="")
        self.retention_hours = StringVar(value="168")
        self.delete_only_uploaded = BooleanVar(value=True)
        self.clip_source = StringVar(value="")
        self.clip_source_summary = StringVar(value="未选择原始录像")
        self.clip_online_url = StringVar(value="")
        self.clip_sources: tuple[Path, ...] = ()
        self.clip_analysis_source: Path | None = None
        self.clip_video = StringVar(value="")
        self.clip_cover = StringVar(value="")
        self.clip_title = StringVar(value="")
        self.clip_cover_title = StringVar(value="")
        self.clip_status = StringVar(value="请选择原始录像和已生成的切片成片")
        self.clip_model_text = StringVar(value="正在检测本地切片模型……")
        self.clip_analysis_text = StringVar(value="选择原始录像后，可按时长自动生成可审核的候选切片")
        self.clip_analysis_thread: threading.Thread | None = None
        self.clip_download_thread: threading.Thread | None = None
        self.clip_render_thread: threading.Thread | None = None
        self.clip_auto_render_requested = False
        self.clip_candidates: dict[str, ClipCandidate] = {}
        self.clip_candidate_outputs: dict[str, ClipRenderResult] = {}
        self.clip_analysis_cache: Path | None = None
        self.clip_work_progress = DoubleVar(value=0.0)
        self.clip_work_progress_text = StringVar(value="当前没有切片分析或生成任务")
        self.clip_work_progress_indeterminate = False
        self.upload_progress = DoubleVar(value=0.0)
        self.upload_progress_text = StringVar(value="当前没有投稿任务")
        self.upload_progress_indeterminate = False

        self._configure_theme()
        self._build_ui()
        self._load_existing_config()
        self._configure_theme()
        self._install_logging()
        self._recover_interrupted_uploads()
        self._scan_local_history(silent=True)
        self._refresh_recorder_status()
        self._refresh_clip_runtime_status()
        self.root.after(100, self._drain_messages)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill=BOTH, expand=True)

        header = ttk.Frame(outer, style="Header.TFrame")
        header.pack(fill=X, pady=(0, 16))
        heading = ttk.Frame(header, style="Header.TFrame")
        heading.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(heading, text="BILI  REC", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(heading, text=APP_TITLE, style="Title.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Label(heading, text="录播姬成片检测 · 身份防错 · 自动投稿", style="Muted.TLabel").pack(
            anchor="w", pady=(3, 0)
        )
        self.theme_button = ttk.Button(header, command=self._toggle_theme, style="Outline.TButton")
        self.theme_button.pack(side=RIGHT, anchor="n")

        upload_state = ttk.Frame(outer, style="Header.TFrame")
        upload_state.pack(fill=X, pady=(0, 12))
        ttk.Label(upload_state, text="投稿进度", style="Muted.TLabel").pack(side=LEFT, padx=(0, 10))
        self.upload_progress_bar = ttk.Progressbar(
            upload_state,
            variable=self.upload_progress,
            maximum=100,
            mode="determinate",
            style="Upload.Horizontal.TProgressbar",
        )
        self.upload_progress_bar.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(upload_state, textvariable=self.upload_progress_text, style="Muted.TLabel", width=34).pack(
            side=LEFT, padx=(10, 0)
        )

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill=BOTH, expand=True)
        monitor = ttk.Frame(self.notebook, padding=16)
        upload = ttk.Frame(self.notebook, padding=16)
        clip_upload = ttk.Frame(self.notebook, padding=16)
        clip_ai = ttk.Frame(self.notebook, padding=16)
        manual = ttk.Frame(self.notebook, padding=16)
        clips_tab = ttk.Frame(self.notebook)
        self.notebook.add(monitor, text="  监控与日志  ")
        self.notebook.add(upload, text="  直播录像投稿  ")
        self.notebook.add(clip_upload, text="  切片投稿设置  ")
        self.notebook.add(clip_ai, text="  AI 分析设置  ")
        self.notebook.add(manual, text="  投稿台账  ")
        self.notebook.add(clips_tab, text="  智能切片  ")

        self.clip_canvas = Canvas(clips_tab, highlightthickness=0, borderwidth=0)
        clip_scroll = ttk.Scrollbar(clips_tab, orient="vertical", command=self.clip_canvas.yview)
        self.clip_canvas.configure(yscrollcommand=clip_scroll.set)
        self.clip_canvas.pack(side=LEFT, fill=BOTH, expand=True)
        clip_scroll.pack(side=RIGHT, fill="y")
        clips = ttk.Frame(self.clip_canvas, padding=16)
        clip_window = self.clip_canvas.create_window((0, 0), window=clips, anchor="nw")

        def update_clip_scroll(_event=None) -> None:
            self.clip_canvas.configure(scrollregion=self.clip_canvas.bbox("all"))

        def fit_clip_width(event) -> None:
            self.clip_canvas.itemconfigure(clip_window, width=event.width)

        clips.bind("<Configure>", update_clip_scroll)
        self.clip_canvas.bind("<Configure>", fit_clip_width)

        info = ttk.LabelFrame(monitor, text="  运行状态  ", padding=14, style="Card.TLabelframe")
        info.pack(fill=X)
        status_line = ttk.Frame(info, style="Card.TFrame")
        status_line.pack(fill=X)
        ttk.Label(status_line, text="●", style="Online.TLabel").pack(side=LEFT, padx=(0, 7))
        ttk.Label(status_line, textvariable=self.recorder_text, style="Status.TLabel").pack(side=LEFT)
        ttk.Label(info, textvariable=self.status_text, style="MutedCard.TLabel").pack(anchor="w", pady=(7, 0))

        room = ttk.LabelFrame(monitor, text="  直播间与目录  ", padding=14, style="Card.TLabelframe")
        room.pack(fill=X, pady=12)
        self._row(room, 0, "房间号", self.room_id, card=True)
        self._row(room, 1, "主播名称", self.streamer, card=True)
        ttk.Label(room, text="录播姬工作目录", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(room, textvariable=self.work_dir).grid(row=2, column=1, sticky="ew", padx=8, pady=5)
        ttk.Button(room, text="选择…", command=self._choose_work_dir).grid(row=2, column=2, pady=5)
        ttk.Label(room, text="本地录像清理", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=5)
        retention_row = ttk.Frame(room, style="Card.TFrame")
        retention_row.grid(row=3, column=1, columnspan=2, sticky="w", padx=8, pady=5)
        ttk.Entry(retention_row, textvariable=self.retention_hours, width=8).pack(side=LEFT)
        ttk.Label(retention_row, text="小时后自动删除", style="Card.TLabel").pack(side=LEFT, padx=(7, 16))
        ttk.Checkbutton(
            retention_row,
            text="仅删除已投稿成功的录像",
            variable=self.delete_only_uploaded,
            style="Card.TCheckbutton",
        ).pack(side=LEFT)
        room.columnconfigure(1, weight=1)

        buttons = ttk.Frame(monitor)
        buttons.pack(fill=X, pady=(0, 10))
        self.start_button = ttk.Button(buttons, text="▶  启动自动监控", command=self._start, style="Primary.TButton")
        self.start_button.pack(side=LEFT)
        self.stop_button = ttk.Button(buttons, text="■  停止", command=self._stop, state="disabled", style="Danger.TButton")
        self.stop_button.pack(side=LEFT, padx=8)
        ttk.Button(buttons, text="核验房间", command=self._check_room, style="Accent.TButton").pack(side=LEFT)
        ttk.Button(buttons, text="最小化到托盘", command=self._minimize_to_tray, style="Outline.TButton").pack(side=LEFT, padx=8)
        ttk.Button(buttons, text="打开录播姬", command=self._open_recorder).pack(side=LEFT, padx=8)
        ttk.Button(buttons, text="退出客户端", command=self._request_exit, style="Danger.TButton").pack(side=LEFT)
        ttk.Button(buttons, text="打开工作目录", command=self._open_work_dir).pack(side=RIGHT)

        ttk.Label(monitor, text="运行日志", style="Section.TLabel").pack(anchor="w", pady=(5, 7))
        self.log_box = scrolledtext.ScrolledText(
            monitor, height=18, state="disabled", font=("Cascadia Mono", 9), relief="flat", borderwidth=1, padx=12, pady=10
        )
        self.log_box.pack(fill=BOTH, expand=True)

        upload.columnconfigure(1, weight=1)
        ttk.Checkbutton(upload, text="启用下播或手动停止后的自动投稿", variable=self.upload_enabled).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )
        self._row(upload, 1, "biliup 程序", self.biliup_executable, button=("选择…", self._choose_biliup))
        self._row(upload, 2, "登录凭据", self.cookie_file, button=("选择…", self._choose_cookie))
        self._row(upload, 3, "投稿分区 tid", self.tid)
        upload_tid_help = ttk.Frame(upload)
        upload_tid_help.grid(row=3, column=2, sticky="w")
        ttk.Label(upload_tid_help, text="数字分区 ID", style="Muted.TLabel").pack(side=LEFT)
        ttk.Button(
            upload_tid_help,
            text="填写说明",
            command=self._show_upload_field_help,
            style="Outline.TButton",
        ).pack(side=LEFT, padx=(6, 0))
        ttk.Label(upload, text="版权类型").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Combobox(
            upload,
            textvariable=self.copyright,
            values=("1", "2"),
            state="readonly",
            width=12,
        ).grid(row=4, column=1, sticky="w", padx=8, pady=5)
        ttk.Label(upload, text="1 = 自制；2 = 转载", style="Muted.TLabel").grid(
            row=4, column=2, sticky="w", pady=5
        )
        self._row(upload, 5, "标签（逗号分隔）", self.tags)
        self._row(upload, 6, "标题模板", self.title_template)
        self._row(upload, 7, "定时发布时间", self.publish_at)
        ttk.Label(
            upload,
            text="留空立即发布；北京时间 YYYY-MM-DD HH:MM，须提前至少 4 小时",
            style="Muted.TLabel",
        ).grid(row=8, column=1, columnspan=2, sticky="w", padx=8, pady=(0, 5))
        self._row(upload, 9, "投稿后动态", self.upload_dynamic)
        options = ttk.Frame(upload)
        options.grid(row=10, column=1, columnspan=2, sticky="w", padx=8, pady=5)
        ttk.Checkbutton(options, text="仅自己可见", variable=self.upload_private).pack(side=LEFT, padx=(0, 14))
        ttk.Checkbutton(options, text="禁止转载", variable=self.upload_no_reprint).pack(side=LEFT, padx=(0, 14))
        ttk.Checkbutton(options, text="开启充电", variable=self.upload_charging).pack(side=LEFT)
        ttk.Label(upload, text="简介模板").grid(row=11, column=0, sticky="nw", pady=5)
        self.description = scrolledtext.ScrolledText(upload, height=7)
        self.description.grid(row=11, column=1, columnspan=2, sticky="nsew", padx=8, pady=5)
        self.description.insert("1.0", "直播间：{room_url}\n原直播标题：{room_title}\n录制开始：{start_time}")
        upload.rowconfigure(11, weight=1)
        actions = ttk.Frame(upload)
        actions.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="保存设置", command=self._save, style="Primary.TButton").pack(side=LEFT)
        ttk.Button(actions, text="扫码登录 biliup", command=self._login, style="Accent.TButton").pack(side=LEFT, padx=8)
        ttk.Label(actions, text="确认版权、分区和标签后再启用自动投稿。", style="Warning.TLabel").pack(side=RIGHT)

        clip_upload.columnconfigure(1, weight=1)
        ttk.Label(
            clip_upload,
            text="这里只配置短视频切片，不会影响整场直播录像的自动投稿。",
            style="Warning.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self._row(
            clip_upload,
            1,
            "切片 biliup 程序",
            self.clip_biliup_executable,
            button=("选择…", self._choose_clip_biliup),
        )
        self._row(
            clip_upload,
            2,
            "切片登录凭据",
            self.clip_cookie_file,
            button=("选择…", self._choose_clip_cookie),
        )
        self._row(clip_upload, 3, "切片投稿分区 tid", self.clip_tid)
        clip_tid_help = ttk.Frame(clip_upload)
        clip_tid_help.grid(row=3, column=2, sticky="w")
        ttk.Label(clip_tid_help, text="数字分区 ID", style="Muted.TLabel").pack(side=LEFT)
        ttk.Button(
            clip_tid_help,
            text="填写说明",
            command=self._show_upload_field_help,
            style="Outline.TButton",
        ).pack(side=LEFT, padx=(6, 0))
        ttk.Label(clip_upload, text="切片版权类型").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Combobox(
            clip_upload,
            textvariable=self.clip_copyright,
            values=("1", "2"),
            state="readonly",
            width=12,
        ).grid(row=4, column=1, sticky="w", padx=8, pady=5)
        ttk.Label(clip_upload, text="1 = 自制；2 = 转载", style="Muted.TLabel").grid(
            row=4, column=2, sticky="w", pady=5
        )
        self._row(clip_upload, 5, "切片标签（逗号分隔）", self.clip_tags)
        ttk.Label(clip_upload, text="切片定时发布").grid(row=6, column=0, sticky="w", pady=5)
        clip_schedule = ttk.Frame(clip_upload)
        clip_schedule.grid(row=6, column=1, columnspan=2, sticky="w", padx=8, pady=5)
        ttk.Label(clip_schedule, textvariable=self.clip_publish_summary, style="Muted.TLabel").pack(side=LEFT)
        ttk.Button(clip_schedule, text="选择时间…", command=self._choose_clip_publish_time).pack(side=LEFT, padx=10)
        ttk.Button(clip_schedule, text="清除", command=self._clear_clip_publish_time, style="Outline.TButton").pack(side=LEFT)
        ttk.Label(
            clip_upload,
            text="留空立即发布；选择时间后将按北京时间定时发布（需提前至少 4 小时）",
            style="Muted.TLabel",
        ).grid(row=7, column=1, columnspan=2, sticky="w", padx=8, pady=(0, 5))
        ttk.Label(clip_upload, text="切片简介模板").grid(row=8, column=0, sticky="nw", pady=5)
        self.clip_description = scrolledtext.ScrolledText(clip_upload, height=8)
        self.clip_description.grid(row=8, column=1, columnspan=2, sticky="nsew", padx=8, pady=5)
        self.clip_description.insert(
            "1.0",
            "直播间：{room_url}\n切片来源时间：{start_time}\n本视频为直播内容精选切片。",
        )
        clip_upload.rowconfigure(8, weight=1)
        clip_upload_actions = ttk.Frame(clip_upload)
        clip_upload_actions.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Button(
            clip_upload_actions,
            text="保存切片投稿设置",
            command=self._save,
            style="Primary.TButton",
        ).pack(side=LEFT)
        ttk.Button(
            clip_upload_actions,
            text="扫码登录切片账号",
            command=self._login_clip,
            style="Accent.TButton",
        ).pack(side=LEFT, padx=8)
        ttk.Label(
            clip_upload_actions,
            text="可以与直播录像使用同一账号，也可以选择独立凭据。",
            style="Muted.TLabel",
        ).pack(side=RIGHT)

        clip_ai.columnconfigure(1, weight=1)
        ttk.Label(
            clip_ai,
            text=(
                "启用后严格使用 API 生成选段、视频标题和黄色封面标题；只发送带时间戳的分段字幕，"
                "不发送录像文件、本地路径或主播身份。API 不足时宁缺毋滥，失败时不生成本地候选。"
            ),
            style="Warning.TLabel",
            wraplength=880,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))
        ttk.Checkbutton(
            clip_ai,
            text="启用 OpenAI 兼容 API 语义增强",
            variable=self.clip_ai_enabled,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._row(clip_ai, 2, "API 根地址 / 完整地址", self.clip_ai_base_url)
        self._row(clip_ai, 3, "模型", self.clip_ai_model)
        ttk.Label(clip_ai, text="协议").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Combobox(
            clip_ai,
            textvariable=self.clip_ai_protocol,
            values=("responses", "chat_completions", "auto"),
            state="readonly",
        ).grid(row=4, column=1, sticky="ew", padx=8, pady=5)
        ttk.Label(clip_ai, text="API Key").grid(row=5, column=0, sticky="w", pady=5)
        ttk.Entry(clip_ai, textvariable=self.clip_ai_key_input, show="●").grid(
            row=5, column=1, sticky="ew", padx=8, pady=5
        )
        ttk.Label(clip_ai, text="留空会保留已保存密钥", style="Muted.TLabel").grid(
            row=5, column=2, sticky="w", pady=5
        )
        self._row(clip_ai, 6, "单次超时（秒）", self.clip_ai_timeout_seconds)
        self._row(clip_ai, 7, "字幕分块（分钟）", self.clip_ai_chunk_minutes)
        ttk.Label(
            clip_ai,
            textvariable=self.clip_ai_status,
            style="Muted.TLabel",
            wraplength=880,
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 6))
        clip_ai_actions = ttk.Frame(clip_ai)
        clip_ai_actions.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(
            clip_ai_actions,
            text="从 CC Switch 导入当前 Codex 供应商",
            command=self._import_cc_switch_clip_ai,
            style="Accent.TButton",
        ).pack(side=LEFT)
        ttk.Button(
            clip_ai_actions,
            text="保存 AI 设置",
            command=self._save,
            style="Primary.TButton",
        ).pack(side=LEFT, padx=8)
        self.clip_ai_test_button = ttk.Button(
            clip_ai_actions,
            text="测试 API 连接",
            command=self._test_clip_ai_connection,
            style="Outline.TButton",
        )
        self.clip_ai_test_button.pack(side=LEFT)
        ttk.Label(
            clip_ai,
            text="CC Switch 的 Codex/Responses 供应商可直接导入。密钥仅写入 secrets/clip-ai-key.txt，不写入 TOML 或日志。",
            style="Muted.TLabel",
            wraplength=880,
        ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(18, 0))

        manual_top = ttk.Frame(manual)
        manual_top.pack(fill=X, pady=(0, 10))
        ttk.Label(manual_top, text="投稿台账", style="Section.TLabel").pack(side=LEFT)
        ttk.Label(manual_top, textvariable=self.history_summary, style="Muted.TLabel").pack(side=LEFT, padx=12)
        ttk.Button(
            manual_top,
            text="选择目录扫描",
            command=self._choose_and_scan_local_history,
            style="Outline.TButton",
        ).pack(side=RIGHT)
        ttk.Button(
            manual_top,
            text="扫描切片成片",
            command=self._scan_clip_history,
            style="Accent.TButton",
        ).pack(side=RIGHT, padx=(0, 8))
        ttk.Button(
            manual_top,
            text="扫描录播目录",
            command=self._scan_local_history,
            style="Outline.TButton",
        ).pack(side=RIGHT, padx=(0, 8))
        ttk.Label(manual, textvariable=self.history_scan_status, style="Muted.TLabel").pack(
            anchor="w", pady=(0, 10)
        )

        append_row = ttk.Frame(manual)
        append_row.pack(fill=X, pady=(0, 10))
        ttk.Label(append_row, text="追加到 BV（可选）").pack(side=LEFT)
        ttk.Entry(append_row, textvariable=self.manual_append_bvid, width=28).pack(side=LEFT, padx=8)
        ttk.Label(append_row, text="留空则创建新稿件；填写后把选中录像追加为新分P。", style="Muted.TLabel").pack(side=LEFT)

        table_frame = ttk.Frame(manual)
        table_frame.pack(fill=BOTH, expand=True)
        columns = ("status", "source", "file", "title", "updated", "message")
        self.history_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "status": "状态",
            "source": "来源",
            "file": "录像文件",
            "title": "投稿标题",
            "updated": "更新时间",
            "message": "结果 / BV号",
        }
        widths = {"status": 78, "source": 72, "file": 210, "title": 220, "updated": 135, "message": 220}
        for key in columns:
            self.history_tree.heading(key, text=headings[key])
            self.history_tree.column(
                key,
                width=widths[key],
                minwidth=60,
                stretch=key in {"file", "title", "message"},
            )
        history_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=history_scroll.set)
        self.history_tree.pack(side=LEFT, fill=BOTH, expand=True)
        history_scroll.pack(side=RIGHT, fill="y")
        self.history_tree.bind("<Double-1>", self._edit_history_selected)

        manual_actions = ttk.Frame(manual)
        manual_actions.pack(fill=X, pady=(12, 0))
        ttk.Button(
            manual_actions,
            text="选择录像并投稿",
            command=self._manual_choose_upload,
            style="Primary.TButton",
        ).pack(side=LEFT)
        ttk.Button(
            manual_actions,
            text="投稿选中记录",
            command=self._manual_upload_selected,
            style="Accent.TButton",
        ).pack(side=LEFT, padx=8)
        ttk.Button(
            manual_actions,
            text="重新投稿",
            command=self._reupload_history_selected,
            style="Danger.TButton",
        ).pack(side=LEFT)
        ttk.Button(
            manual_actions,
            text="编辑记录",
            command=self._edit_history_selected,
            style="Outline.TButton",
        ).pack(side=LEFT, padx=8)
        ttk.Button(
            manual_actions,
            text="批量编辑",
            command=self._batch_edit_history_selected,
            style="Outline.TButton",
        ).pack(side=LEFT, padx=(0, 8))
        ttk.Button(
            manual_actions,
            text="删除记录",
            command=self._delete_history_selected,
            style="Outline.TButton",
        ).pack(side=LEFT)
        ttk.Button(manual_actions, text="刷新", command=self._refresh_history).pack(side=RIGHT)
        ttk.Label(
            manual,
            text="双击可编辑；删除只移除台账记录，不删除录像。“重新投稿”会保留原记录并创建新任务。",
            style="Warning.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        clips.columnconfigure(1, weight=1)
        clips.rowconfigure(5, weight=1)
        runtime = ttk.LabelFrame(clips, text="  本地切片运行环境  ", padding=14, style="Card.TLabelframe")
        runtime.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        ttk.Label(runtime, text="●", style="Online.TLabel").pack(side=LEFT, padx=(0, 7))
        ttk.Label(runtime, textvariable=self.clip_model_text, style="MutedCard.TLabel").pack(side=LEFT)
        online_row = ttk.Frame(runtime)
        online_row.pack(fill=X, pady=(10, 0))
        ttk.Label(online_row, text="在线视频链接", style="MutedCard.TLabel").pack(side=LEFT, padx=(0, 8))
        ttk.Entry(online_row, textvariable=self.clip_online_url).pack(side=LEFT, fill=X, expand=True)
        self.clip_online_download_button = ttk.Button(
            online_row,
            text="下载并分析",
            command=self._download_and_analyze_online_source,
            style="Accent.TButton",
        )
        self.clip_online_download_button.pack(side=LEFT, padx=(8, 0))
        ttk.Label(
            runtime,
            text="支持 B站视频等 http/https 链接；先下载到本地，再使用现有智能切片流程。请确保内容来源已获授权。",
            style="MutedCard.TLabel",
            wraplength=900,
        ).pack(anchor="w", pady=(6, 0))

        self._row(
            clips,
            1,
            "原始直播录像",
            self.clip_source,
            button=("选择…", self._choose_clip_source),
        )
        analysis_actions = ttk.Frame(clips)
        analysis_actions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 4))
        ttk.Label(
            analysis_actions,
            textvariable=self.clip_source_summary,
            style="Muted.TLabel",
        ).pack(side=LEFT, padx=(0, 12))
        self.clip_auto_generate_button = ttk.Button(
            analysis_actions,
            text="智能分析（出片前审批标题）",
            command=self._analyze_and_render_clip_source,
            style="Primary.TButton",
        )
        self.clip_auto_generate_button.pack(side=LEFT)
        self.clip_analyze_button = ttk.Button(
            analysis_actions,
            text="仅分析候选",
            command=self._analyze_clip_source,
            style="Accent.TButton",
        )
        self.clip_analyze_button.pack(side=LEFT, padx=8)
        self.clip_use_candidate_button = ttk.Button(
            analysis_actions,
            text="应用标题修改",
            command=self._use_selected_candidate,
            style="Outline.TButton",
            state="disabled",
        )
        self.clip_use_candidate_button.pack(side=LEFT)
        self.clip_generate_selected_button = ttk.Button(
            analysis_actions,
            text="生成选中成片",
            command=self._render_selected_candidate,
            style="Primary.TButton",
            state="disabled",
        )
        self.clip_generate_selected_button.pack(side=LEFT)
        self.clip_generate_all_button = ttk.Button(
            analysis_actions,
            text="审批后批量生成",
            command=self._render_all_candidates,
            style="Accent.TButton",
            state="disabled",
        )
        self.clip_generate_all_button.pack(side=LEFT, padx=8)
        ttk.Button(
            analysis_actions,
            text="打开分析缓存",
            command=self._open_clip_analysis_cache,
            style="Outline.TButton",
        ).pack(side=LEFT)
        clip_work = ttk.Frame(clips)
        clip_work.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(3, 4))
        ttk.Label(clip_work, text="分析 / 生成进度", style="Muted.TLabel").pack(side=LEFT, padx=(0, 8))
        self.clip_work_progress_bar = ttk.Progressbar(
            clip_work,
            variable=self.clip_work_progress,
            maximum=100,
            mode="determinate",
            style="Upload.Horizontal.TProgressbar",
        )
        self.clip_work_progress_bar.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(
            clip_work,
            textvariable=self.clip_work_progress_text,
            style="Muted.TLabel",
            width=38,
        ).pack(side=LEFT, padx=(8, 0))
        ttk.Label(
            clips,
            textvariable=self.clip_analysis_text,
            style="Muted.TLabel",
            wraplength=900,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 6))
        candidate_frame = ttk.LabelFrame(clips, text="  自动分析候选（先审核，可生成选中项或批量成片）  ", padding=8, style="Card.TLabelframe")
        candidate_frame.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(0, 12))
        candidate_columns = ("status", "range", "duration", "score", "topics", "signals", "title", "cover_title", "evidence")
        self.clip_candidates_tree = ttk.Treeview(
            candidate_frame,
            columns=candidate_columns,
            show="headings",
            height=5,
            selectmode="browse",
        )
        candidate_headings = {
            "status": "状态",
            "range": "时间范围",
            "duration": "时长",
            "score": "评分",
            "topics": "真实命中话题",
            "signals": "高能信号",
            "title": "建议视频标题",
            "cover_title": "黄色封面标题",
            "evidence": "字幕依据",
        }
        candidate_widths = {
            "status": 90,
            "range": 300,
            "duration": 72,
            "score": 58,
            "topics": 180,
            "signals": 160,
            "title": 420,
            "cover_title": 300,
            "evidence": 520,
        }
        for key in candidate_columns:
            self.clip_candidates_tree.heading(key, text=candidate_headings[key])
            self.clip_candidates_tree.column(
                key,
                width=candidate_widths[key],
                minwidth=45,
                stretch=key in {"title", "cover_title", "evidence"},
            )
        candidate_frame.columnconfigure(0, weight=1)
        candidate_frame.rowconfigure(0, weight=1)
        candidate_scroll = ttk.Scrollbar(candidate_frame, orient="vertical", command=self.clip_candidates_tree.yview)
        candidate_xscroll = ttk.Scrollbar(candidate_frame, orient="horizontal", command=self.clip_candidates_tree.xview)
        self.clip_candidates_tree.configure(
            yscrollcommand=candidate_scroll.set,
            xscrollcommand=candidate_xscroll.set,
        )
        self.clip_candidates_tree.grid(row=0, column=0, sticky="nsew")
        candidate_scroll.grid(row=0, column=1, sticky="ns")
        candidate_xscroll.grid(row=1, column=0, sticky="ew")
        detail_frame = ttk.LabelFrame(candidate_frame, text="  选中候选详情  ", padding=6, style="Card.TLabelframe")
        detail_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        self.clip_candidate_detail = scrolledtext.ScrolledText(
            detail_frame,
            height=5,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=6,
        )
        self.clip_candidate_detail.pack(fill=BOTH, expand=True)
        self.clip_candidate_detail.configure(state="disabled")
        self.clip_candidates_tree.bind("<<TreeviewSelect>>", self._on_clip_candidate_selected)
        ttk.Label(clips, text="审核后生成 / 投稿", style="Section.TLabel").grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 2))
        self._row(
            clips,
            7,
            "切片成片",
            self.clip_video,
            button=("选择…", self._choose_clip_video),
        )
        self._row(
            clips,
            8,
            "投稿封面（可选）",
            self.clip_cover,
            button=("选择…", self._choose_clip_cover),
        )
        self._row(clips, 9, "视频投稿标题", self.clip_title)
        self._row(clips, 10, "封面黄色标题", self.clip_cover_title)
        ttk.Label(
            clips,
            text="先在候选表选中一条，再修改视频标题和封面黄色标题；点击生成即视为审批。标题只使用字幕真实依据，不添加时间戳。",
            style="Muted.TLabel",
        ).grid(row=11, column=0, columnspan=3, sticky="w", pady=(8, 4))
        ttk.Label(clips, textvariable=self.clip_status, style="Warning.TLabel").grid(
            row=12, column=0, columnspan=3, sticky="w", pady=(4, 12)
        )
        clip_actions = ttk.Frame(clips)
        clip_actions.grid(row=13, column=0, columnspan=3, sticky="ew")
        self.clip_upload_button = ttk.Button(
            clip_actions,
            text="一键投稿这个切片",
            command=self._upload_clip,
            style="Primary.TButton",
        )
        self.clip_upload_button.pack(side=LEFT)
        ttk.Button(
            clip_actions,
            text="打开切片目录",
            command=self._open_clip_dir,
            style="Outline.TButton",
        ).pack(side=LEFT, padx=8)
        ttk.Label(
            clips,
            text="智能分析会在内部完成转写、语义选段和字幕核验，先停在标题审批；内容完整优先，通常 3–5 分钟，可自然浮动到约 2.5–6 分钟。",
            style="Muted.TLabel",
        ).grid(row=14, column=0, columnspan=3, sticky="w", pady=(16, 0))

    def _configure_theme(self) -> None:
        palette = PALETTES[self.theme_mode.get()]
        self.root.configure(background=palette["bg"])
        font = ("Microsoft YaHei UI", 10)
        self.style.configure(".", font=font, background=palette["bg"], foreground=palette["text"])
        self.style.configure("TFrame", background=palette["bg"])
        self.style.configure("Header.TFrame", background=palette["bg"])
        self.style.configure("Card.TFrame", background=palette["surface"])
        self.style.configure("TLabel", background=palette["bg"], foreground=palette["text"])
        self.style.configure("Brand.TLabel", background=palette["bg"], foreground=palette["accent"], font=("Segoe UI", 9, "bold"))
        self.style.configure("Title.TLabel", background=palette["bg"], foreground=palette["text"], font=("Microsoft YaHei UI", 20, "bold"))
        self.style.configure("Muted.TLabel", background=palette["bg"], foreground=palette["muted"])
        self.style.configure("MutedCard.TLabel", background=palette["surface"], foreground=palette["muted"])
        self.style.configure("Card.TLabel", background=palette["surface"], foreground=palette["text"])
        self.style.configure("Status.TLabel", background=palette["surface"], foreground=palette["text"], font=("Microsoft YaHei UI", 11, "bold"))
        self.style.configure("Online.TLabel", background=palette["surface"], foreground=palette["success"], font=("Segoe UI", 12, "bold"))
        self.style.configure("Section.TLabel", background=palette["bg"], foreground=palette["text"], font=("Microsoft YaHei UI", 10, "bold"))
        self.style.configure("Warning.TLabel", background=palette["bg"], foreground=palette["warning"])
        self.style.configure(
            "Card.TLabelframe",
            background=palette["surface"],
            foreground=palette["accent"],
            bordercolor=palette["border"],
            lightcolor=palette["border"],
            darkcolor=palette["border"],
            relief="solid",
            borderwidth=1,
        )
        self.style.configure("Card.TLabelframe.Label", background=palette["surface"], foreground=palette["accent"], font=("Microsoft YaHei UI", 10, "bold"))
        self.style.configure("TEntry", fieldbackground=palette["input"], foreground=palette["text"], bordercolor=palette["border"], lightcolor=palette["border"], darkcolor=palette["border"], insertcolor=palette["text"], padding=8)
        self.style.map("TEntry", bordercolor=[("focus", palette["accent"])], lightcolor=[("focus", palette["accent"])], darkcolor=[("focus", palette["accent"])])
        self.style.configure("TButton", background=palette["panel"], foreground=palette["text"], borderwidth=0, padding=(13, 8), relief="flat")
        self.style.map("TButton", background=[("active", palette["border"]), ("disabled", palette["surface"])], foreground=[("disabled", palette["muted"])])
        self.style.configure("Primary.TButton", background=palette["pink"], foreground="#FFFFFF", font=("Microsoft YaHei UI", 10, "bold"))
        self.style.map("Primary.TButton", background=[("active", palette["pink_hover"]), ("disabled", palette["border"])])
        self.style.configure("Accent.TButton", background=palette["accent"], foreground="#FFFFFF", font=("Microsoft YaHei UI", 10, "bold"))
        self.style.map("Accent.TButton", background=[("active", palette["accent_hover"])])
        self.style.configure("Outline.TButton", background=palette["bg"], foreground=palette["accent"], bordercolor=palette["accent"], lightcolor=palette["accent"], darkcolor=palette["accent"], borderwidth=1)
        self.style.map("Outline.TButton", background=[("active", palette["panel"])])
        self.style.configure("Danger.TButton", background=palette["panel"], foreground=palette["danger"])
        self.style.map("Danger.TButton", background=[("active", palette["border"])])
        self.style.configure("Dialog.TFrame", background=palette["surface"])
        self.style.configure(
            "DialogTitle.TLabel",
            background=palette["surface"],
            foreground=palette["text"],
            font=("Microsoft YaHei UI", 17, "bold"),
        )
        self.style.configure("DialogExit.TButton", background=palette["border"], foreground=palette["text"])
        self.style.map("DialogExit.TButton", background=[("active", palette["panel"])])
        self.style.configure("DialogCancel.TButton", background=palette["accent"], foreground="#FFFFFF")
        self.style.map("DialogCancel.TButton", background=[("active", palette["accent_hover"])])
        self.style.configure(
            "Upload.Horizontal.TProgressbar",
            background=palette["accent"],
            troughcolor=palette["surface"],
            bordercolor=palette["border"],
            lightcolor=palette["accent"],
            darkcolor=palette["accent"],
        )
        self.style.configure("TNotebook", background=palette["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        self.style.configure("TNotebook.Tab", background=palette["surface"], foreground=palette["muted"], padding=(18, 10), borderwidth=0)
        self.style.map("TNotebook.Tab", background=[("selected", palette["panel"]), ("active", palette["panel"])], foreground=[("selected", palette["accent"]), ("active", palette["text"])])
        self.style.configure("TCheckbutton", background=palette["bg"], foreground=palette["text"], padding=6)
        self.style.map("TCheckbutton", background=[("active", palette["bg"])], indicatorcolor=[("selected", palette["accent"])])
        self.style.configure("Card.TCheckbutton", background=palette["surface"], foreground=palette["text"], padding=6)
        self.style.map(
            "Card.TCheckbutton",
            background=[("active", palette["surface"])],
            indicatorcolor=[("selected", palette["accent"])],
        )
        self.style.configure(
            "Treeview",
            background=palette["input"],
            fieldbackground=palette["input"],
            foreground=palette["text"],
            bordercolor=palette["border"],
            rowheight=31,
        )
        self.style.map("Treeview", background=[("selected", palette["selection"])], foreground=[("selected", palette["text"])])
        self.style.configure(
            "Treeview.Heading",
            background=palette["panel"],
            foreground=palette["accent"],
            relief="flat",
            padding=(8, 7),
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        if hasattr(self, "log_box"):
            self.clip_canvas.configure(background=palette["bg"])
            self.log_box.configure(
                background=palette["input"], foreground=palette["text"], insertbackground=palette["text"],
                selectbackground=palette["selection"], highlightbackground=palette["border"], highlightcolor=palette["accent"],
            )
            self.description.configure(
                background=palette["input"], foreground=palette["text"], insertbackground=palette["text"],
                selectbackground=palette["selection"], highlightbackground=palette["border"], highlightcolor=palette["accent"], relief="flat", borderwidth=1,
            )
            self.clip_description.configure(
                background=palette["input"], foreground=palette["text"], insertbackground=palette["text"],
                selectbackground=palette["selection"], highlightbackground=palette["border"], highlightcolor=palette["accent"], relief="flat", borderwidth=1,
            )
            if hasattr(self, "clip_candidate_detail"):
                self.clip_candidate_detail.configure(
                    background=palette["input"], foreground=palette["text"], insertbackground=palette["text"],
                    selectbackground=palette["selection"], highlightbackground=palette["border"], highlightcolor=palette["accent"],
                )
            self.theme_button.configure(text="切换日间模式" if self.theme_mode.get() == "dark" else "切换夜间模式")

    def _toggle_theme(self) -> None:
        self.theme_mode.set("light" if self.theme_mode.get() == "dark" else "dark")
        self._configure_theme()
        try:
            write_config(self._values(), config_path())
        except (ValueError, OSError):
            pass

    def _show_upload_field_help(self) -> None:
        messagebox.showinfo(
            "投稿字段填写说明",
            "版权类型\n"
            "• 1 = 自制：你拥有该视频内容的原创著作权。\n"
            "• 2 = 转载：录播或切片来自他人直播时通常选择 2，并确保已经获得发布授权。\n\n"
            "投稿分区 tid\n"
            "• tid 是 B 站投稿分区的数字 ID，不是直播间房间号。\n"
            "• 常见示例：21=日常、138=搞笑、17=单机游戏、65=网络游戏、171=电子竞技。\n"
            "• 分区名称和可用范围可能调整，应以 B 站创作中心当前显示的分类为准。\n"
            "• 观点闲聊切片可按实际内容选择日常或对应知识/娱乐分区；游戏内容选择对应游戏分区。\n\n"
            "其他字段\n"
            "• 标签使用逗号分隔，填写与视频实际内容相关的词。\n"
            "• 切片投稿设置与整场直播录像投稿相互独立。",
            parent=self.root,
        )

    def _row(self, parent, row: int, label: str, variable: StringVar, button=None, card: bool = False) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel" if card else "TLabel").grid(
            row=row, column=0, sticky="w", pady=5
        )
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=5)
        if button:
            ttk.Button(parent, text=button[0], command=button[1]).grid(row=row, column=2, pady=5)

    def _load_existing_config(self) -> None:
        path = config_path()
        if not path.is_file():
            return
        try:
            config = load_config(path)
            room = next((item for item in config.rooms if item.enabled), None)
            if room:
                self.room_id.set(str(room.id))
                self.streamer.set(room.name)
            self.work_dir.set(str(config.recording.watch_dir))
            self.upload_enabled.set(config.upload.enabled)
            self.biliup_executable.set(config.upload.executable)
            self.cookie_file.set(str(config.upload.cookie_file))
            self.tid.set(str(config.upload.tid))
            self.copyright.set(str(config.upload.copyright))
            self.tags.set(",".join(config.upload.tags))
            self.title_template.set(config.upload.title)
            self.publish_at.set(config.upload.publish_at)
            self.upload_dynamic.set(config.upload.dynamic)
            self.upload_private.set(config.upload.is_only_self)
            self.upload_no_reprint.set(config.upload.no_reprint)
            self.upload_charging.set(config.upload.charging_pay)
            self.clip_biliup_executable.set(config.clip_upload.executable)
            self.clip_cookie_file.set(str(config.clip_upload.cookie_file))
            self.clip_tid.set(str(config.clip_upload.tid))
            self.clip_copyright.set(str(config.clip_upload.copyright))
            self.clip_tags.set(",".join(config.clip_upload.tags))
            self._set_clip_publish_time(config.clip_upload.publish_at)
            self.clip_ai_enabled.set(config.clip_ai.enabled)
            self.clip_ai_base_url.set(config.clip_ai.base_url)
            self.clip_ai_model.set(config.clip_ai.model)
            self.clip_ai_protocol.set(config.clip_ai.protocol)
            self.clip_ai_key_file.set(str(config.clip_ai.api_key_file))
            self.clip_ai_timeout_seconds.set(str(config.clip_ai.timeout_seconds))
            self.clip_ai_chunk_minutes.set(str(config.clip_ai.chunk_minutes))
            if config.clip_ai.api_key_file.is_file():
                self.clip_ai_status.set(
                    f"已保存 API Key；{'API 增强已启用' if config.clip_ai.enabled else 'API 增强未启用'}"
                )
            else:
                self.clip_ai_status.set(
                    "API 增强已启用但尚未保存密钥" if config.clip_ai.enabled else "API 增强未启用；当前使用本地分析"
                )
            self.theme_mode.set(config.app.theme)
            self.retention_hours.set(str(config.recording.retention_hours))
            self.delete_only_uploaded.set(config.recording.delete_only_uploaded)
            self.description.delete("1.0", END)
            self.description.insert("1.0", config.upload.description)
            self.clip_description.delete("1.0", END)
            self.clip_description.insert("1.0", config.clip_upload.description)
            self._configure_theme()
        except ConfigError as exc:
            messagebox.showwarning(APP_TITLE, f"已有客户端配置无法读取：\n{exc}")

    def _values(self) -> dict[str, object]:
        room_id = self.room_id.get().strip()
        if not room_id.isdigit() or int(room_id) <= 0:
            raise ValueError("请输入正确的数字房间号")
        work_dir = Path(self.work_dir.get().strip()).expanduser()
        if not work_dir.is_dir():
            raise ValueError(f"录播姬工作目录不存在：{work_dir}")
        if not self.tid.get().strip().isdigit():
            raise ValueError("投稿分区 tid 必须是数字")
        if self.copyright.get().strip() not in {"1", "2"}:
            raise ValueError("版权类型只能填写 1（自制）或 2（转载）")
        if not self.clip_tid.get().strip().isdigit():
            raise ValueError("切片投稿分区 tid 必须是数字")
        if self.clip_copyright.get().strip() not in {"1", "2"}:
            raise ValueError("切片版权类型只能填写 1（自制）或 2（转载）")
        if not self.retention_hours.get().strip().isdigit() or int(self.retention_hours.get()) < 1:
            raise ValueError("录像保留小时数必须是大于 0 的整数")
        clip_ai_protocol = self.clip_ai_protocol.get().strip()
        if clip_ai_protocol not in {"auto", "responses", "chat_completions"}:
            raise ValueError("AI 协议必须是 responses、chat_completions 或 auto")
        if not self.clip_ai_timeout_seconds.get().strip().isdigit():
            raise ValueError("AI 单次超时必须是整数秒")
        clip_ai_timeout = int(self.clip_ai_timeout_seconds.get())
        if not 10 <= clip_ai_timeout <= 300:
            raise ValueError("AI 单次超时必须在 10 到 300 秒之间")
        if not self.clip_ai_chunk_minutes.get().strip().isdigit():
            raise ValueError("AI 字幕分块分钟数必须是整数")
        clip_ai_chunk_minutes = int(self.clip_ai_chunk_minutes.get())
        if not 5 <= clip_ai_chunk_minutes <= 60:
            raise ValueError("AI 字幕分块必须在 5 到 60 分钟之间")
        clip_ai_base_url = self.clip_ai_base_url.get().strip()
        clip_ai_model = self.clip_ai_model.get().strip()
        if self.clip_ai_enabled.get() and (not clip_ai_base_url or not clip_ai_model):
            raise ValueError("启用 API 语义增强前，请填写 API 请求地址和模型")
        return {
            "room_id": int(room_id),
            "streamer": self.streamer.get().strip(),
            "work_dir": str(work_dir.resolve()),
            "upload_enabled": self.upload_enabled.get(),
            "biliup_executable": self.biliup_executable.get().strip() or "biliup",
            "cookie_file": self.cookie_file.get().strip(),
            "tid": int(self.tid.get()),
            "copyright": int(self.copyright.get()),
            "tags": self.tags.get(),
            "title": self.title_template.get(),
            "publish_at": self.publish_at.get().strip(),
            "dynamic": self.upload_dynamic.get().strip(),
            "is_only_self": self.upload_private.get(),
            "no_reprint": self.upload_no_reprint.get(),
            "charging_pay": self.upload_charging.get(),
            "description": self.description.get("1.0", "end-1c"),
            "clip_biliup_executable": self.clip_biliup_executable.get().strip() or "biliup",
            "clip_cookie_file": self.clip_cookie_file.get().strip(),
            "clip_tid": int(self.clip_tid.get()),
            "clip_copyright": int(self.clip_copyright.get()),
            "clip_tags": self.clip_tags.get(),
            "clip_publish_at": self.clip_publish_at.get().strip(),
            "clip_description": self.clip_description.get("1.0", "end-1c"),
            "clip_ai_enabled": self.clip_ai_enabled.get(),
            "clip_ai_base_url": clip_ai_base_url,
            "clip_ai_model": clip_ai_model,
            "clip_ai_protocol": clip_ai_protocol,
            "clip_ai_key_file": self.clip_ai_key_file.get().strip(),
            "clip_ai_timeout_seconds": clip_ai_timeout,
            "clip_ai_chunk_minutes": clip_ai_chunk_minutes,
            "theme": self.theme_mode.get(),
            "retention_hours": int(self.retention_hours.get()),
            "delete_only_uploaded": self.delete_only_uploaded.get(),
        }

    def _save(self, quiet: bool = False) -> bool:
        try:
            values = self._values()
            write_config(values, config_path())
            entered_key = self.clip_ai_key_input.get().strip()
            key_file = Path(str(values["clip_ai_key_file"])).expanduser()
            if entered_key:
                save_api_key(key_file, entered_key)
                self.clip_ai_key_input.set("")
                self.clip_ai_status.set(
                    f"API Key 已单独保存；{'API 增强已启用' if self.clip_ai_enabled.get() else 'API 增强未启用'}"
                )
            elif self.clip_ai_enabled.get() and not key_file.is_file():
                self.clip_ai_status.set("设置已保存，但缺少 API Key；启用 API 时将无法生成候选")
        except (ValueError, OSError, ClipAIError) as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return False
        if not quiet:
            messagebox.showinfo(APP_TITLE, f"设置已保存到：\n{config_path()}")
        return True

    def _current_clip_ai_settings(self, enabled: bool | None = None) -> ClipAISettings:
        try:
            timeout_seconds = int(self.clip_ai_timeout_seconds.get().strip())
            chunk_minutes = int(self.clip_ai_chunk_minutes.get().strip())
        except ValueError as exc:
            raise ClipAIError("AI 超时和字幕分块必须填写整数") from exc
        base_url = self.clip_ai_base_url.get().strip()
        model = self.clip_ai_model.get().strip()
        protocol = self.clip_ai_protocol.get().strip()
        if not base_url or not model:
            raise ClipAIError("请先填写或从 CC Switch 导入 API 请求地址和模型")
        if protocol not in {"auto", "responses", "chat_completions"}:
            raise ClipAIError("不支持的 API 协议")
        if not 10 <= timeout_seconds <= 300 or not 5 <= chunk_minutes <= 60:
            raise ClipAIError("AI 超时或字幕分块超出允许范围")
        return ClipAISettings(
            enabled=self.clip_ai_enabled.get() if enabled is None else enabled,
            base_url=base_url,
            model=model,
            protocol=protocol,
            api_key_file=Path(self.clip_ai_key_file.get().strip()).expanduser(),
            timeout_seconds=timeout_seconds,
            chunk_minutes=chunk_minutes,
        )

    def _current_clip_ai_key(self, settings: ClipAISettings) -> str:
        entered = self.clip_ai_key_input.get().strip()
        return entered if entered else read_api_key(settings.api_key_file)

    def _import_cc_switch_clip_ai(self) -> None:
        try:
            provider = load_current_cc_switch_provider()
        except ClipAIError as exc:
            messagebox.showerror(APP_TITLE, f"CC Switch 导入失败：\n{exc}")
            return
        self.clip_ai_enabled.set(True)
        self.clip_ai_base_url.set(provider.base_url)
        self.clip_ai_model.set(provider.model)
        self.clip_ai_protocol.set(provider.protocol)
        self.clip_ai_key_input.set(provider.api_key)
        self.clip_ai_status.set(
            f"已从 CC Switch 读取“{provider.name}”：{provider.protocol} / {provider.model}；点击“保存 AI 设置”写入独立密钥文件"
        )
        logging.info(
            "已从 CC Switch 导入当前 Codex 供应商：%s，协议 %s，模型 %s（API Key 未写入日志）",
            provider.name,
            provider.protocol,
            provider.model,
        )

    def _test_clip_ai_connection(self) -> None:
        try:
            settings = self._current_clip_ai_settings(enabled=True)
            api_key = self._current_clip_ai_key(settings)
        except ClipAIError as exc:
            messagebox.showerror(APP_TITLE, f"无法测试 API：\n{exc}")
            return
        self.clip_ai_test_button.configure(state="disabled")
        self.clip_ai_status.set("正在发送最小连接测试；不会上传字幕或录像……")

        def work() -> None:
            try:
                result = test_api_connection(settings, api_key)
                self.messages.put(("clip_ai_test_result", (True, result)))
            except Exception as exc:
                self.messages.put(("clip_ai_test_result", (False, str(exc))))

        threading.Thread(target=work, daemon=True, name="clip-ai-test").start()

    def _install_logging(self) -> None:
        handler = QueueLogHandler(self.messages)
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

    def _upload_progress_callback(self, label: str):
        def callback(value: float | None, message: str) -> None:
            self.messages.put(("upload_progress", (value, f"{label}：{message}")))

        return callback

    def _show_upload_progress(self, value: float | None, message: str) -> None:
        if value is None:
            if not self.upload_progress_indeterminate:
                self.upload_progress_bar.configure(mode="indeterminate")
                self.upload_progress_bar.start(12)
                self.upload_progress_indeterminate = True
        else:
            if self.upload_progress_indeterminate:
                self.upload_progress_bar.stop()
                self.upload_progress_bar.configure(mode="determinate")
                self.upload_progress_indeterminate = False
            self.upload_progress.set(max(0.0, min(100.0, float(value))))
        # Keep the fixed-height progress area compact; the complete biliup
        # diagnostic is still shown in the error dialog and written to the log.
        compact = message if len(message) <= 180 else message[:180].rstrip() + "…（详情见日志）"
        self.upload_progress_text.set(compact)

    def _show_clip_work_progress(self, value: float | None, message: str) -> None:
        if value is None:
            if not self.clip_work_progress_indeterminate:
                self.clip_work_progress_bar.configure(mode="indeterminate")
                self.clip_work_progress_bar.start(12)
                self.clip_work_progress_indeterminate = True
        else:
            if self.clip_work_progress_indeterminate:
                self.clip_work_progress_bar.stop()
                self.clip_work_progress_bar.configure(mode="determinate")
                self.clip_work_progress_indeterminate = False
            self.clip_work_progress.set(max(0.0, min(100.0, float(value))))
        self.clip_work_progress_text.set(message)

    def _refresh_recorder_status(self) -> None:
        running = is_recorder_running()
        self.recorder_text.set(f"B站录播姬：{'正在运行' if running else '未运行'}")
        if running != self.last_recorder_running:
            if running:
                logging.info("B站录播姬当前正在运行")
            else:
                logging.warning("B站录播姬当前未运行")
            self.last_recorder_running = running
        self.root.after(5000, self._refresh_recorder_status)

    def _refresh_clip_runtime_status(self) -> None:
        ready, text = clip_runtime_status()
        self.clip_model_text.set(("本地模型：" if ready else "本地模型提示：") + text)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self._save(quiet=True):
            return
        try:
            config = load_config(config_path())
        except ConfigError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.start_button.configure(state="disabled")
        self.status_text.set("正在核验房间号、主播身份和录播姬配置……")

        def validate() -> None:
            try:
                room_config = next(room for room in config.rooms if room.enabled)
                validate_recorder_room(config.recording.watch_dir, room_config.id)
                room = BilibiliLiveAPI().get_room(room_config.id, room_config.name)
                self.messages.put(("start_ready", (config, room.streamer)))
            except RoomIdentityMismatchError as exc:
                self.messages.put(("identity_mismatch", exc))
            except Exception as exc:
                self.messages.put(("start_error", str(exc)))

        threading.Thread(target=validate, daemon=True, name="identity-check").start()

    def _begin_monitor(self, config) -> None:
        self.application = Application(config, self._upload_progress_callback("直播录像"))
        self.worker = threading.Thread(target=self._run_worker, daemon=True, name="monitor")
        self.stop_button.configure(state="normal")
        self.status_text.set("自动监控运行中")
        self.worker.start()

    def _run_worker(self) -> None:
        try:
            assert self.application is not None
            self.application.run()
        except Exception:
            logging.exception("自动监控异常退出")
        finally:
            self.messages.put(("stopped", None))

    def _stop(self) -> None:
        if self.application:
            self.application.stop_event.set()
        self.status_text.set("正在停止……")

    def _check_room(self) -> None:
        try:
            room_id = int(self.room_id.get())
        except ValueError:
            messagebox.showerror(APP_TITLE, "请先填写正确的房间号")
            return
        configured_name = self.streamer.get().strip()
        work_dir = Path(self.work_dir.get().strip()).expanduser()

        def work() -> None:
            try:
                room = BilibiliLiveAPI().get_room(room_id)
                state = "直播中" if room.is_live else "未开播"
                recorder_warning = ""
                try:
                    validate_recorder_room(work_dir, room_id)
                except RecorderConfigError as exc:
                    recorder_warning = str(exc)
                self.messages.put(
                    (
                        "room",
                        (
                            f"{state}｜{room.streamer}｜{room.title}｜真实房间号 {room.room_id}",
                            room.streamer,
                            configured_name,
                            recorder_warning,
                        ),
                    )
                )
            except Exception as exc:
                self.messages.put(("error", f"查询失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _open_recorder(self) -> None:
        if is_recorder_running():
            logging.info("B站录播姬已在后台运行，不重复启动")
            self.recorder_text.set("B站录播姬：正在运行")
            self.status_text.set("录播姬已经运行，无需重复打开")
            return
        executable = find_recorder_executable()
        if not executable:
            logging.error("未找到 B站录播姬安装目录")
            messagebox.showerror(APP_TITLE, "未找到 B站录播姬安装目录")
            return
        os.startfile(executable)
        logging.info("已请求启动 B站录播姬：%s", executable)
        self.status_text.set("正在启动 B站录播姬……")
        self.root.after(2000, self._refresh_recorder_status)

    def _history_store(self) -> UploadHistoryStore:
        work_dir = Path(self.work_dir.get().strip()).expanduser()
        return UploadHistoryStore(work_dir / "data" / "upload_history.json")

    def _recover_interrupted_uploads(self) -> None:
        try:
            recovered = self._history_store().recover_interrupted_uploads()
        except OSError:
            return
        if recovered:
            logging.warning("发现 %d 条因上次客户端退出而未确认结果的投稿记录，已标记为待确认", recovered)

    def _local_room_videos(self) -> list[Path]:
        work_dir = Path(self.work_dir.get().strip()).expanduser()
        return find_local_recordings(work_dir)

    def _choose_and_scan_local_history(self) -> None:
        source = Path(self.clip_source.get().strip()).expanduser() if self.clip_source.get().strip() else None
        if source is not None and source.is_file():
            initial = source.parent
        else:
            initial = Path(self.work_dir.get().strip()).expanduser()
        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择需要加入投稿台账的本地录像目录",
            initialdir=str(initial if initial.is_dir() else app_directory()),
        )
        if selected:
            self._scan_local_history(scan_root=Path(selected))

    def _scan_clip_history(self) -> None:
        clip_root = Path(self.work_dir.get().strip()).expanduser() / "智能切片成片"
        if not clip_root.is_dir():
            self.history_scan_status.set(f"切片成片目录不存在：{clip_root}")
            messagebox.showinfo(APP_TITLE, f"还没有找到切片成片目录：\n{clip_root}")
            return
        self._scan_local_history(scan_root=clip_root, history_source="clip_scan")

    def _scan_local_history(
        self,
        silent: bool = False,
        scan_root: Path | None = None,
        history_source: str = "scan",
    ) -> None:
        try:
            store = self._history_store()
            if scan_root is None:
                recordings = self._local_room_videos()
                scanned_name = f"录播目录 {self.work_dir.get().strip()}"
            else:
                recordings = find_videos_in_directory(scan_root)
                scanned_name = f"所选目录 {scan_root}"
            added = store.discover_files(recordings, source=history_source)
            self._refresh_history()
            if added:
                logging.info(
                    "本地录像扫描完成：找到 %d 个录像，新增 %d 个台账条目，已有 %d 个",
                    len(recordings),
                    added,
                    len(recordings) - added,
                )
            elif not silent:
                logging.info("本地录像扫描完成：找到 %d 个录像，全部已在台账中", len(recordings))
            if not silent:
                result_text = (
                    f"{scanned_name}：找到 {len(recordings)} 个，新增 {added} 个，"
                    f"已有 {len(recordings) - added} 个"
                )
                self.status_text.set(f"本地录像扫描完成：{result_text}")
                self.history_scan_status.set(result_text)
        except OSError as exc:
            if not silent:
                self.history_scan_status.set(f"扫描失败：{exc}")
                messagebox.showerror(APP_TITLE, f"扫描本地录像失败：{exc}")

    def _refresh_history(self) -> None:
        if not hasattr(self, "history_tree"):
            return
        for item_id in self.history_tree.get_children():
            self.history_tree.delete(item_id)
        status_names = {
            "untracked": "未记录",
            "pending": "待投稿",
            "uploading": "投稿中",
            "interrupted": "待确认",
            "success": "已投稿",
            "failed": "失败",
            "merged": "已合并",
            "deleted": "已清理",
        }
        source_names = {
            "scan": "直播-本地扫描",
            "clip_scan": "切片-扫描",
            "manual": "直播-手动",
            "auto": "直播-自动",
            "clip": "切片-生成",
        }
        items = self._history_store().items()
        counts: dict[str, int] = {}
        for item in items:
            status = str(item.get("status", "untracked"))
            counts[status] = counts.get(status, 0) + 1
            files = item.get("files", [])
            file_text = Path(files[0]).name if files else ""
            if len(files) > 1:
                file_text += f" 等 {len(files)} 个分P"
            result = str(item.get("bvid") or item.get("message") or "")
            self.history_tree.insert(
                "",
                "end",
                iid=str(item["id"]),
                values=(
                    status_names.get(status, status),
                    source_names.get(str(item.get("source", "")), str(item.get("source", ""))),
                    file_text,
                    str(item.get("title", "")),
                    str(item.get("updated_at", "")).replace("T", " "),
                    result,
                ),
            )
        self.history_summary.set(
            f"共 {len(items)} 条｜已投稿 {counts.get('success', 0)}｜待确认 {counts.get('interrupted', 0)}｜失败 {counts.get('failed', 0)}｜未记录 {counts.get('untracked', 0)}"
        )

    def _edit_history_selected(self, event=None) -> None:
        if event is not None:
            row = self.history_tree.identify_row(event.y)
            if row:
                self.history_tree.selection_set(row)
                self.history_tree.focus(row)
        selected = self.history_tree.selection()
        if len(selected) != 1:
            messagebox.showinfo(APP_TITLE, "请选择一条需要编辑的台账记录")
            return
        store = self._history_store()
        item = store.get(str(selected[0]))
        if not item:
            messagebox.showerror(APP_TITLE, "选中的台账记录已不存在，请刷新后重试")
            return
        if item.get("status") == "uploading":
            messagebox.showinfo(APP_TITLE, "正在投稿的记录不能编辑，请等待投稿结束")
            return

        status_choices = {
            "未记录": "untracked",
            "待投稿": "pending",
            "待确认": "interrupted",
            "已投稿": "success",
            "失败": "failed",
            "已合并": "merged",
            "已清理": "deleted",
        }
        source_choices = {
            "本地扫描": "scan",
            "切片扫描": "clip_scan",
            "手动投稿": "manual",
            "自动投稿": "auto",
            "切片投稿": "clip",
        }
        status_names = {value: key for key, value in status_choices.items()}
        source_names = {value: key for key, value in source_choices.items()}
        status_var = StringVar(value=status_names.get(str(item.get("status", "untracked")), "未记录"))
        source_var = StringVar(value=source_names.get(str(item.get("source", "scan")), "本地扫描"))
        title_var = StringVar(value=str(item.get("title", "")))
        bvid_var = StringVar(value=str(item.get("bvid", "")))

        dialog = Toplevel(self.root)
        dialog.title("编辑投稿台账记录")
        dialog.transient(self.root)
        dialog.minsize(660, 520)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill=BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text=f"记录 ID：{str(item.get('id', ''))[:12]}", style="Muted.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        ttk.Label(body, text="标题").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=title_var).grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=5)
        ttk.Label(body, text="状态").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Combobox(
            body,
            textvariable=status_var,
            values=tuple(status_choices),
            state="readonly",
            width=18,
        ).grid(row=2, column=1, sticky="w", padx=(12, 0), pady=5)
        ttk.Label(body, text="来源").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Combobox(
            body,
            textvariable=source_var,
            values=tuple(source_choices),
            state="readonly",
            width=18,
        ).grid(row=3, column=1, sticky="w", padx=(12, 0), pady=5)
        ttk.Label(body, text="BV 号").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=bvid_var).grid(row=4, column=1, sticky="ew", padx=(12, 0), pady=5)
        ttk.Label(body, text="录像文件\n（一行一个）").grid(row=5, column=0, sticky="nw", pady=5)
        files_text = scrolledtext.ScrolledText(body, height=6, wrap="none")
        files_text.grid(row=5, column=1, sticky="nsew", padx=(12, 0), pady=5)
        files_text.insert("1.0", "\n".join(map(str, item.get("files", []))))
        ttk.Label(body, text="结果 / 备注").grid(row=6, column=0, sticky="nw", pady=5)
        message_text = scrolledtext.ScrolledText(body, height=5, wrap="word")
        message_text.grid(row=6, column=1, sticky="nsew", padx=(12, 0), pady=5)
        message_text.insert("1.0", str(item.get("message", "")))
        body.rowconfigure(5, weight=1)
        body.rowconfigure(6, weight=1)

        def save() -> None:
            title = " ".join(title_var.get().split()).strip()
            files = tuple(line.strip() for line in files_text.get("1.0", "end-1c").splitlines() if line.strip())
            bvid = bvid_var.get().strip()
            if not title:
                messagebox.showerror(APP_TITLE, "台账标题不能为空", parent=dialog)
                return
            if not files:
                messagebox.showerror(APP_TITLE, "至少保留一个录像文件路径", parent=dialog)
                return
            if bvid and not re.fullmatch(r"BV[0-9A-Za-z]+", bvid):
                messagebox.showerror(APP_TITLE, "BV 号格式不正确", parent=dialog)
                return
            new_status = status_choices[status_var.get()]
            if new_status == "success" and item.get("status") != "success":
                if not messagebox.askyesno(
                    "确认标记为已投稿",
                    "请确认该录像确实已经投稿成功。\n\n"
                    "标记为“已投稿”后，如果启用了“仅删除已投稿成功的录像”，"
                    "文件超过保留期限时将允许自动清理。",
                    parent=dialog,
                ):
                    return
            store.edit(
                str(item["id"]),
                files=files,
                title=title[:80],
                source=source_choices[source_var.get()],
                status=new_status,
                message=message_text.get("1.0", "end-1c").strip(),
                bvid=bvid,
            )
            logging.info("已手动编辑投稿台账记录：%s", str(item["id"])[:12])
            dialog.grab_release()
            dialog.destroy()
            self._refresh_history()

        actions = ttk.Frame(body)
        actions.grid(row=7, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(actions, text="取消", command=dialog.destroy, style="Outline.TButton").pack(side=LEFT, padx=6)
        ttk.Button(actions, text="保存修改", command=save, style="Primary.TButton").pack(side=LEFT)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.update_idletasks()
        width, height = 720, 590
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.grab_set()
        dialog.lift()

    def _delete_history_selected(self) -> None:
        selected = tuple(map(str, self.history_tree.selection()))
        if not selected:
            messagebox.showinfo(APP_TITLE, "请先选择需要删除的台账记录")
            return
        store = self._history_store()
        items = [store.get(item_id) for item_id in selected]
        items = [item for item in items if item]
        if any(item.get("status") == "uploading" for item in items):
            messagebox.showinfo(APP_TITLE, "正在投稿的记录不能删除，请等待投稿结束")
            return
        if not messagebox.askyesno(
            "删除台账记录",
            f"确定删除选中的 {len(items)} 条台账记录吗？\n\n"
            "只会删除台账记录，不会删除本地录像文件。之后重新扫描目录时，这些文件可能再次出现。",
            parent=self.root,
        ):
            return
        removed = store.delete([str(item["id"]) for item in items])
        self._refresh_history()
        self.history_scan_status.set(f"已删除 {removed} 条台账记录；本地录像文件未删除")
        logging.info("已删除 %d 条投稿台账记录，本地文件未删除", removed)

    def _batch_edit_history_selected(self) -> None:
        selected = tuple(map(str, self.history_tree.selection()))
        if len(selected) < 2:
            messagebox.showinfo(APP_TITLE, "请至少选择两条台账记录，再使用批量编辑")
            return
        store = self._history_store()
        items = [store.get(item_id) for item_id in selected]
        items = [item for item in items if item]
        if any(item.get("status") == "uploading" for item in items):
            messagebox.showinfo(APP_TITLE, "选中记录中有正在投稿的条目，不能批量编辑")
            return

        status_choices = {
            "不修改": "",
            "待投稿": "pending",
            "待确认": "interrupted",
            "已投稿": "success",
            "失败": "failed",
            "未记录": "untracked",
            "已合并": "merged",
            "已清理": "deleted",
        }
        source_choices = {
            "不修改": "",
            "本地扫描": "scan",
            "切片扫描": "clip_scan",
            "手动投稿": "manual",
            "自动投稿": "auto",
            "切片投稿": "clip",
        }
        status_var = StringVar(value="不修改")
        source_var = StringVar(value="不修改")
        title_var = StringVar(value="")
        bvid_var = StringVar(value="")
        clear_bvid = BooleanVar(value=False)

        dialog = Toplevel(self.root)
        dialog.title("批量编辑投稿台账")
        dialog.transient(self.root)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill=BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(
            body,
            text=f"已选择 {len(items)} 条记录；留空或选择“不修改”的字段会保留各条原值。",
            style="Muted.TLabel",
            wraplength=620,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
        ttk.Label(body, text="统一状态").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(
            body,
            textvariable=status_var,
            values=tuple(status_choices),
            state="readonly",
            width=18,
        ).grid(row=1, column=1, sticky="w", padx=(12, 0), pady=5)
        ttk.Label(body, text="统一来源").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Combobox(
            body,
            textvariable=source_var,
            values=tuple(source_choices),
            state="readonly",
            width=18,
        ).grid(row=2, column=1, sticky="w", padx=(12, 0), pady=5)
        ttk.Label(body, text="统一标题").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=title_var).grid(row=3, column=1, sticky="ew", padx=(12, 0), pady=5)
        ttk.Label(body, text="统一 BV 号").grid(row=4, column=0, sticky="w", pady=5)
        bvid_row = ttk.Frame(body)
        bvid_row.grid(row=4, column=1, sticky="ew", padx=(12, 0), pady=5)
        bvid_row.columnconfigure(0, weight=1)
        ttk.Entry(bvid_row, textvariable=bvid_var).grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(bvid_row, text="清空原 BV 号", variable=clear_bvid).grid(row=0, column=1, padx=(10, 0))
        ttk.Label(body, text="统一备注").grid(row=5, column=0, sticky="nw", pady=5)
        message_text = scrolledtext.ScrolledText(body, height=5, wrap="word")
        message_text.grid(row=5, column=1, sticky="nsew", padx=(12, 0), pady=5)
        body.rowconfigure(5, weight=1)

        def save() -> None:
            title = " ".join(title_var.get().split()).strip()
            bvid = bvid_var.get().strip()
            if title and len(title) > 80:
                title = title[:80]
            if bvid and not re.fullmatch(r"BV[0-9A-Za-z]+", bvid):
                messagebox.showerror(APP_TITLE, "BV 号格式不正确", parent=dialog)
                return
            new_status = status_choices[status_var.get()]
            if new_status == "success" and any(item.get("status") != "success" for item in items):
                if not messagebox.askyesno(
                    "确认批量标记为已投稿",
                    "这些记录标记为“已投稿”后，超过保留期限时可能允许自动清理录像。\n\n确定继续吗？",
                    parent=dialog,
                ):
                    return
            note = message_text.get("1.0", "end-1c").strip()
            for item in items:
                old_bvid = str(item.get("bvid", ""))
                store.edit(
                    str(item["id"]),
                    files=list(map(str, item.get("files", []))),
                    title=title or str(item.get("title", "")),
                    source=source_choices[source_var.get()] or str(item.get("source", "scan")),
                    status=new_status or str(item.get("status", "untracked")),
                    message=note or str(item.get("message", "")),
                    bvid="" if clear_bvid else (bvid or old_bvid),
                )
            logging.info("已批量编辑 %d 条投稿台账记录", len(items))
            dialog.grab_release()
            dialog.destroy()
            self._refresh_history()

        actions = ttk.Frame(body)
        actions.grid(row=6, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(actions, text="取消", command=dialog.destroy, style="Outline.TButton").pack(side=LEFT, padx=6)
        ttk.Button(actions, text="保存批量修改", command=save, style="Primary.TButton").pack(side=LEFT)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.update_idletasks()
        width, height = 680, 470
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.grab_set()
        dialog.lift()

    def _reupload_history_selected(self) -> None:
        selected = tuple(map(str, self.history_tree.selection()))
        if not selected:
            messagebox.showinfo(APP_TITLE, "请先选择需要重新投稿的台账记录")
            return
        store = self._history_store()
        items = [store.get(item_id) for item_id in selected]
        items = [item for item in items if item]
        if any(item.get("status") == "uploading" for item in items):
            messagebox.showinfo(APP_TITLE, "选中记录中有正在投稿的任务，不能重复提交")
            return
        selected_sources = {str(item.get("source", "")) for item in items}
        clip_sources = {"clip", "clip_scan"}
        has_clip = bool(selected_sources & clip_sources)
        has_recording = bool(selected_sources - clip_sources)
        if has_clip and has_recording:
            messagebox.showinfo(APP_TITLE, "切片成片与整场录像不能混合重新投稿，请分开选择")
            return
        files = tuple(value for item in items for value in map(str, item.get("files", [])))
        missing = [value for value in files if not Path(value).is_file()]
        if missing:
            preview = "\n".join(missing[:3])
            suffix = f"\n……另有 {len(missing) - 3} 个" if len(missing) > 3 else ""
            messagebox.showerror(APP_TITLE, f"以下录像文件已不存在，无法重新投稿：\n\n{preview}{suffix}")
            return
        success_count = sum(item.get("status") == "success" for item in items)
        interrupted_count = sum(item.get("status") == "interrupted" for item in items)
        warning = (
            f"将把选中的 {len(items)} 条记录重新投稿，并新建一条台账记录。\n"
            "原记录会保留，本次操作可能在 B 站产生重复稿件。"
        )
        if success_count:
            warning += f"\n\n其中 {success_count} 条已经标记为“已投稿”。"
        if interrupted_count:
            warning += f"\n其中 {interrupted_count} 条结果待确认，请先检查 B 站创作中心。"
        if not messagebox.askyesno("确认重新投稿", warning, parent=self.root):
            return
        upload_source = "clip" if has_clip else "manual"
        # A live recording title must be rebuilt from the selected file's
        # room identity.  Reusing the ledger title here used to preserve a
        # stale主播 name when the ledger entry had been created under another
        # enabled room.  Clip titles are intentionally preserved because they
        # are user-approved titles rather than live-room metadata.
        title_override = str(items[0].get("title", "")).strip() if has_clip else ""
        self._manual_upload_files(
            files,
            source=upload_source,
            title_override=title_override,
            timestamp_files=files,
        )

    def _manual_choose_upload(self) -> None:
        files = filedialog.askopenfilenames(
            initialdir=self.work_dir.get() or str(app_directory()),
            filetypes=[("录像文件", "*.flv *.mp4 *.mkv *.ts"), ("所有文件", "*.*")],
        )
        if files:
            self._manual_upload_files(tuple(files))

    def _manual_upload_selected(self) -> None:
        selected = self.history_tree.selection()
        if not selected:
            messagebox.showinfo(APP_TITLE, "请先在投稿台账中选择一条录像记录")
            return
        items = [self._history_store().get(item_id) for item_id in selected]
        items = [item for item in items if item]
        if any(item.get("status") == "uploading" for item in items):
            messagebox.showinfo(APP_TITLE, "选中记录中有正在投稿的条目，请勿重复提交")
            return
        if any(item.get("status") == "interrupted" for item in items):
            messagebox.showinfo(
                APP_TITLE,
                "选中记录的上一次投稿因客户端退出而结果待确认。\n\n"
                "请先在 B 站创作中心确认没有生成稿件，再重新选择文件进行手动投稿，避免重复稿件。",
            )
            return
        if any(item.get("status") == "success" for item in items):
            messagebox.showinfo(APP_TITLE, "已投稿记录不能再次创建稿件；如需追加，请只选择未记录/失败条目并填写 BV 号")
            return
        selected_sources = {str(item.get("source", "")) for item in items}
        clip_sources = {"clip", "clip_scan"}
        has_clip = bool(selected_sources & clip_sources)
        has_recording = bool(selected_sources - clip_sources)
        if len(items) > 1:
            merge = messagebox.askyesno(
                "批量投稿方式",
                f"已选择 {len(items)} 条记录。\n\n"
                "选择“是”：合并为一个稿件，录像作为多个分P。\n"
                "选择“否”：每条记录分别创建一个稿件。",
                parent=self.root,
            )
        else:
            merge = True
        if merge:
            if has_clip and has_recording:
                messagebox.showinfo(APP_TITLE, "切片成片与整场录像不能合并到同一稿件，请选择分别投稿")
                return
            upload_source = "clip" if has_clip else "manual"
            files = tuple(value for item in items for value in map(str, item.get("files", [])))
            if upload_source != "clip":
                identities = {
                    (room_id, streamer)
                    for value in files
                    for room_id, streamer in (recording_identity(Path(value)),)
                    if room_id is not None or streamer
                }
                if len(identities) > 1:
                    messagebox.showinfo(
                        APP_TITLE,
                        "选中的直播录像来自不同房间或主播，不能合并为同一稿件；"
                        "请在批量投稿方式中选择“否”分别投稿。",
                    )
                    return
            item_id = str(items[0]["id"])
            if len(items) > 1:
                store = self._history_store()
                store.replace_files(item_id, files)
                for merged in items[1:]:
                    store.update(str(merged["id"]), "merged", f"已合并到同一手动投稿记录 {item_id[:8]}")
            title_override = Path(files[0]).stem if has_clip and files else ""
            self._manual_upload_files(
                files,
                item_id=item_id,
                source=upload_source,
                title_override=title_override,
            )
            return

        # Separate mode preserves one ledger item per稿件 and starts one
        # upload worker for each selected recording.
        for item in items:
            item_files = tuple(map(str, item.get("files", [])))
            if not item_files:
                continue
            item_source = "clip" if str(item.get("source", "")) in clip_sources else "manual"
            title_override = Path(item_files[0]).stem if item_source == "clip" else ""
            self._manual_upload_files(
                item_files,
                item_id=str(item["id"]),
                source=item_source,
                title_override=title_override,
            )

    def _manual_upload_files(
        self,
        files: tuple[str, ...],
        item_id: str | None = None,
        *,
        source: str = "manual",
        title_override: str = "",
        cover_path: str = "",
        timestamp_files: tuple[str, ...] | None = None,
    ) -> None:
        existing = [path for path in files if Path(path).is_file()]
        if not existing:
            messagebox.showerror(APP_TITLE, "选中的录像文件不存在")
            return
        if not self._save(quiet=True):
            return
        config = load_config(config_path())
        append_bvid = "" if source == "clip" else self.manual_append_bvid.get().strip()
        if append_bvid and not re.fullmatch(r"BV[0-9A-Za-z]+", append_bvid):
            messagebox.showerror(APP_TITLE, "追加目标 BV 号格式不正确")
            return
        store = self._history_store()
        if item_id is None:
            item_id = store.create(
                existing,
                title_override or Path(existing[0]).stem,
                source,
                status="pending",
            )
        else:
            store.set_source(item_id, source)
            store.replace_files(item_id, existing)
        store.update(item_id, "uploading", "正在调用 biliup")
        self._refresh_history()
        if source == "clip":
            self.clip_status.set("正在投稿切片，请勿重复点击……")
            self.clip_upload_button.configure(state="disabled")
            self._show_upload_progress(None, "切片：正在准备投稿")
        else:
            self.status_text.set("手动投稿进行中……")
            self._show_upload_progress(None, "手动投稿：正在准备")

        time_sources = tuple(
            value for value in (timestamp_files or tuple(existing)) if Path(value).is_file()
        ) or tuple(existing)
        selected_cover = Path(cover_path).expanduser() if cover_path else None
        # A clip can be submitted either from the dedicated clip panel or
        # from the upload ledger.  In both cases prefer the cover generated
        # beside the MP4 when the caller did not explicitly choose one.
        if source == "clip" and selected_cover is None and existing:
            generated_cover = find_generated_clip_cover(Path(existing[0]))
            if generated_cover is not None:
                selected_cover = generated_cover
        configured_streamer = self.streamer.get().strip()

        def work() -> None:
            try:
                room_config = next(room for room in config.rooms if room.enabled)
                manual_start = recording_start_time(time_sources, fallback=datetime.now())
                if source == "clip":
                    room = LiveRoom(
                        room_id=room_config.id,
                        short_id=0,
                        title=title_override or Path(existing[0]).stem,
                        streamer=room_config.name or configured_streamer or "直播切片",
                        live_status=0,
                        live_time=manual_start.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    api_room = room
                else:
                    # Resolve the room from the selected recording itself. A
                    # manual ledger upload may contain a room different from
                    # the currently enabled monitoring room. Previously this
                    # always queried ``room_config`` and produced a wrong
                    # streamer, title, and source URL in the投稿 form.
                    path_room_id, path_streamer = recording_identity(Path(existing[0]))
                    lookup_room_id = path_room_id or room_config.id
                    try:
                        # Do not pass the current configured name: it can be
                        # for another room and would correctly trigger the
                        # identity mismatch guard during a manual upload.
                        api_room = BilibiliLiveAPI().get_room(lookup_room_id)
                    except Exception as exc:
                        # Posting a completed local file should remain
                        # possible when the live API is temporarily offline.
                        # Keep the path-derived identity and filename title;
                        # the uploader still receives the correct room URL.
                        logging.warning(
                            "无法查询本地录像所属房间 %s，使用文件路径元数据：%s",
                            lookup_room_id,
                            exc,
                        )
                        api_room = LiveRoom(
                            room_id=lookup_room_id,
                            short_id=0,
                            title=recording_file_title(Path(existing[0]), Path(existing[0]).stem)
                            or "未命名直播",
                            streamer=path_streamer or room_config.name or configured_streamer or "未知主播",
                            live_status=0,
                            live_time="",
                        )
                    room = replace(
                        api_room,
                        streamer=path_streamer or api_room.streamer,
                        title=api_room.title
                        or recording_file_title(Path(existing[0]), Path(existing[0]).stem),
                        live_time=manual_start.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                recording = Recording.from_room(
                    room,
                    existing,
                    datetime.now(),
                    event_key=f"manual:{uuid.uuid4().hex}",
                )
                session_key = ""
                try:
                    live_started = datetime.fromisoformat(api_room.live_time)
                    if api_room.is_live and all(
                        datetime.fromtimestamp(Path(path).stat().st_mtime) >= live_started
                        for path in existing
                    ):
                        session_key = api_room.event_key
                except (ValueError, OSError):
                    pass
                store.set_session(item_id, session_key, recording.event_key)
                base_settings = config.clip_upload if source == "clip" else config.upload
                settings = replace(base_settings, enabled=True)
                if title_override:
                    final_literal = (
                        clip_upload_title(title_override)
                        if source == "clip"
                        else " ".join(title_override.split()).strip()[:80]
                    )
                    settings = replace(
                        settings,
                        title=final_literal.replace("{", "{{").replace("}", "}}"),
                    )
                elif "{start_time}" not in settings.title:
                    settings = replace(settings, title=f"{settings.title.rstrip()} {{start_time}}")
                final_title = render_upload_text(settings.title, recording)[:80]
                store.set_title(item_id, final_title)
                progress_label = "切片" if source == "clip" else "手动投稿"
                uploader = Uploader(settings, self._upload_progress_callback(progress_label))
                if append_bvid:
                    uploader.append(recording, append_bvid)
                    bvid = append_bvid
                    message = f"已追加到 {append_bvid}"
                else:
                    bvid = uploader.upload(recording, cover=selected_cover)
                    message = bvid or "投稿成功，未解析到 BV 号"
                store.update(item_id, "success", message, bvid=bvid or "")
                result_kind = "clip_result" if source == "clip" else "manual_result"
                self.messages.put((result_kind, (True, message)))
            except Exception as exc:
                store.update(item_id, "failed", str(exc))
                result_kind = "clip_result" if source == "clip" else "manual_result"
                self.messages.put((result_kind, (False, str(exc))))

        thread_name = "clip-upload" if source == "clip" else "manual-upload"
        threading.Thread(target=work, daemon=True, name=thread_name).start()

    def _clear_clip_candidates(self) -> None:
        self.clip_candidates.clear()
        self.clip_candidate_outputs.clear()
        self.clip_analysis_cache = None
        self.clip_analysis_source = None
        self.clip_video.set("")
        self.clip_cover.set("")
        self._set_clip_candidate_detail("")
        if hasattr(self, "clip_candidates_tree"):
            for item_id in self.clip_candidates_tree.get_children():
                self.clip_candidates_tree.delete(item_id)
        if hasattr(self, "clip_use_candidate_button"):
            self.clip_use_candidate_button.configure(state="disabled")
            self.clip_generate_selected_button.configure(state="disabled")
            self.clip_generate_all_button.configure(state="disabled")

    def _clip_streamer_name(self, source: Path) -> str:
        parent = source.parent.name.strip()
        room_folder = re.match(r"^\d+[-_](.+)$", parent)
        if room_folder:
            return room_folder.group(1).strip()
        if " _ " in parent:
            return parent.split(" _ ", 1)[0].strip()
        return self.streamer.get().strip()

    def _selected_clip_sources(self) -> tuple[Path, ...]:
        """Return the current multi-select, or the manually typed single path."""
        current = Path(self.clip_source.get().strip()).expanduser() if self.clip_source.get().strip() else None
        selected = tuple(path for path in self.clip_sources if path.is_file())
        if selected and current is not None:
            try:
                if current.resolve() == selected[0].resolve():
                    return selected
            except OSError:
                pass
        return (current,) if current is not None else ()

    def _analyze_clip_source(self) -> None:
        self._start_clip_analysis(auto_render=False)

    def _analyze_and_render_clip_source(self) -> None:
        self._start_clip_analysis(auto_render=True)

    def _start_clip_analysis(self, auto_render: bool) -> None:
        sources = self._selected_clip_sources()
        if not sources or any(not source.is_file() for source in sources):
            messagebox.showerror(APP_TITLE, "请先选择已经完成录制、可以正常播放的原始录像")
            return
        if self.clip_analysis_thread and self.clip_analysis_thread.is_alive():
            return
        ready, reason = clip_runtime_status()
        if not ready:
            messagebox.showerror(APP_TITLE, f"无法分析录像：\n{reason}")
            return
        python = app_directory() / ".clip-venv-standalone" / "Scripts" / "python.exe"
        model = find_model_directory(app_directory())
        if model is None:
            messagebox.showerror(APP_TITLE, "未找到本地 Faster-Whisper small 模型")
            return
        try:
            cache_root = Path(self.work_dir.get().strip()).expanduser().resolve() / "data" / "clip_cache"
        except OSError:
            messagebox.showerror(APP_TITLE, "切片缓存目录不可用")
            return
        self._clear_clip_candidates()
        self.clip_auto_render_requested = auto_render
        self.clip_auto_generate_button.configure(state="disabled")
        self.clip_analyze_button.configure(state="disabled")
        self.clip_analysis_text.set("正在准备分析……")
        if auto_render:
            self.clip_status.set("正在完成转写和语义选段；出片前会停在标题审批")
            self._show_clip_work_progress(None, "智能剪辑正在分析原录像")
        else:
            self.clip_status.set("正在分析原录像；只生成候选，不会自动生成视频或投稿")
            self._show_clip_work_progress(None, "正在分析原录像")
        streamer = self._clip_streamer_name(sources[0])
        ai_settings: ClipAISettings | None = None
        ai_key = ""
        ai_setup_error = ""
        ai_enabled = self.clip_ai_enabled.get()
        if ai_enabled:
            try:
                ai_settings = self._current_clip_ai_settings(enabled=True)
                ai_key = self._current_clip_ai_key(ai_settings)
            except ClipAIError as exc:
                ai_setup_error = str(exc)

        def progress(message: str) -> None:
            self.messages.put(("clip_analysis_progress", message))

        def work() -> None:
            try:
                source = merge_sources(sources, cache_root, progress)
                self.messages.put(("clip_analysis_progress", f"正在分析 {len(sources)} 个分P的统一时间轴"))
                result = analyze_video(source, cache_root, python, model, progress, streamer=streamer)
                self.messages.put(("clip_transcription_complete", result))
                if ai_enabled:
                    if ai_setup_error or ai_settings is None:
                        reason = ai_setup_error or "API 设置不可用"
                        progress(f"API 设置不可用，本次不生成候选：{reason}")
                        result = replace(
                            result,
                            candidates=(),
                            candidate_source="api_failed",
                            candidate_note=f"API 设置不可用，本次未生成候选：{reason}",
                        )
                    else:
                        result = enhance_analysis_with_fallback(
                            result,
                            ai_settings,
                            ai_key,
                            progress,
                            streamer=streamer,
                        )
                self.messages.put(("clip_analysis_result", result))
            except Exception as exc:
                self.messages.put(("clip_analysis_failed", str(exc)))

        self.clip_analysis_thread = threading.Thread(target=work, daemon=True, name="clip-analysis")
        self.clip_analysis_thread.start()

    def _show_clip_analysis(self, result: ClipAnalysis) -> None:
        self._clear_clip_candidates()
        self.clip_analysis_cache = result.cache_dir
        self.clip_analysis_source = result.source
        for candidate in result.candidates:
            self.clip_candidates[candidate.id] = candidate
            topics = "、".join(candidate.topics) if candidate.topics else "未归类"
            signals = "、".join(candidate.signals) if candidate.signals else "信息完整"
            self.clip_candidates_tree.insert(
                "",
                END,
                iid=candidate.id,
                values=(
                    "待生成·API" if candidate.origin == "api" else "待生成·本地",
                    format_candidate_ranges(candidate),
                    f"{candidate.duration:.0f} 秒",
                    f"{candidate.score:.0f}",
                    topics,
                    signals,
                    candidate.title,
                    candidate.cover_title or candidate.title,
                    candidate.evidence,
                ),
            )
        cache_note = "复用本地转写缓存" if result.transcript_from_cache else "已完成新的本地转写"
        target = candidate_count_for_duration(result.duration)
        self.clip_analysis_text.set(
            f"录像 {format_timestamp(result.duration)}，按时长建议 {target} 条；{cache_note}，"
            f"生成 {len(result.candidates)} 个候选。{result.candidate_note}"
        )
        auto_render = self.clip_auto_render_requested
        self.clip_auto_render_requested = False
        if result.candidate_source == "api_failed":
            self.clip_status.set("API 不可用，本次未生成候选；请检查 API 设置或连接后重试")
        elif auto_render:
            self.clip_status.set("分析与核验已完成：请逐条审批视频标题和封面黄色标题，再生成成片")
        else:
            self.clip_status.set("请先在候选表中核对时间范围、字幕依据和标题；确认后再制作成片、上传")
        self.clip_generate_all_button.configure(state="normal" if result.candidates else "disabled")
        self._show_clip_work_progress(100.0, f"分析完成，共 {len(result.candidates)} 条候选")
        logging.info(
            "智能切片分析完成：%s，生成 %d 个候选；候选来源：%s；%s",
            result.source,
            len(result.candidates),
            result.candidate_source,
            result.candidate_note,
        )
        self.clip_auto_generate_button.configure(state="normal")
        self.clip_analyze_button.configure(state="normal")
        if auto_render and result.candidates:
            first = self.clip_candidates_tree.get_children()[0]
            self.clip_candidates_tree.selection_set(first)
            self.clip_candidates_tree.focus(first)
            self._on_clip_candidate_selected()

    def _on_clip_candidate_selected(self, _event=None) -> None:
        selected = self.clip_candidates_tree.selection()
        self.clip_use_candidate_button.configure(state="normal" if selected else "disabled")
        self.clip_generate_selected_button.configure(state="normal" if selected else "disabled")
        if selected:
            candidate = self.clip_candidates.get(selected[0])
            if candidate is not None:
                self.clip_title.set(candidate.title)
                self.clip_cover_title.set(candidate.cover_title or candidate.title)
                output = self.clip_candidate_outputs.get(candidate.id)
                if output is None:
                    # Never leave the previous candidate's files visible after
                    # a selection change; this was only a display bug and did
                    # not affect the actual render output.
                    self.clip_video.set("")
                    self.clip_cover.set("")
                    self.clip_status.set(
                        f"已选中 {candidate.id}（{format_candidate_ranges(candidate)}），尚未生成成片"
                    )
                else:
                    self.clip_video.set(str(output.video))
                    self.clip_cover.set(str(output.cover))
                    self.clip_status.set(
                        f"已选中 {candidate.id}，已加载对应成片和封面"
                    )
                self._set_clip_candidate_detail(
                    "\n".join(
                        (
                            f"视频标题：{candidate.title}",
                            f"封面标题：{candidate.cover_title or candidate.title}",
                            f"剪辑范围：{format_candidate_ranges(candidate)}",
                            f"时长：{candidate.duration:.1f} 秒    评分：{candidate.score:.0f}",
                            f"真实命中话题：{'、'.join(candidate.topics) if candidate.topics else '未归类'}",
                            f"高能信号：{'、'.join(candidate.signals) if candidate.signals else '信息完整'}",
                            f"字幕依据：{candidate.evidence}",
                        )
                    )
                )
        else:
            self.clip_video.set("")
            self.clip_cover.set("")
            self._set_clip_candidate_detail("")

    def _set_clip_candidate_detail(self, text: str) -> None:
        widget = getattr(self, "clip_candidate_detail", None)
        if widget is None:
            return
        widget.configure(state="normal")
        widget.delete("1.0", END)
        if text:
            widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _use_selected_candidate(self, silent: bool = False) -> ClipCandidate | None:
        selected = self.clip_candidates_tree.selection()
        if not selected:
            if not silent:
                messagebox.showinfo(APP_TITLE, "请先选中一条候选切片")
            return None
        candidate = self.clip_candidates.get(selected[0])
        if candidate is None:
            return None
        title = " ".join(self.clip_title.get().split()).strip()
        cover_title = " ".join(self.clip_cover_title.get().split()).strip()
        if not title or not cover_title:
            if not silent:
                messagebox.showerror(APP_TITLE, "视频标题和封面黄色标题都不能为空")
            return None
        candidate = replace(candidate, title=title[:80], cover_title=cover_title[:40])
        self.clip_candidates[candidate.id] = candidate
        self.clip_candidate_outputs.pop(candidate.id, None)
        if self.clip_candidates_tree.exists(candidate.id):
            values = list(self.clip_candidates_tree.item(candidate.id, "values"))
            values[6] = candidate.title
            values[7] = candidate.cover_title
            values[0] = "标题已审批"
            self.clip_candidates_tree.item(candidate.id, values=values)
        output = self.clip_candidate_outputs.get(candidate.id)
        if output is not None:
            self.clip_video.set(str(output.video))
            self.clip_cover.set(str(output.cover))
            self.clip_status.set(
                f"已载入候选成片（{format_candidate_ranges(candidate)}），可以继续审核或稍后投稿。"
            )
        else:
            self.clip_video.set("")
            self.clip_cover.set("")
            self.clip_status.set(
                f"标题修改已应用（{format_candidate_ranges(candidate)}）。点击生成即确认审批。"
            )
        self._on_clip_candidate_selected()
        logging.info("切片候选标题已审批 %s：视频=%s；封面=%s", candidate.id, candidate.title, candidate.cover_title)
        return candidate

    def _render_selected_candidate(self) -> None:
        selected = self.clip_candidates_tree.selection()
        if not selected:
            messagebox.showinfo(APP_TITLE, "请先选中一条候选切片")
            return
        candidate = self._use_selected_candidate(silent=False)
        if candidate is not None:
            self._start_render_candidates((candidate,))

    def _render_all_candidates(self) -> None:
        if self.clip_candidates_tree.selection():
            if self._use_selected_candidate(silent=False) is None:
                return
        if not messagebox.askyesno(
            "标题审批确认",
            "请确认已经逐条核对视频标题和封面黄色标题。\n\n确认后将批量生成成片和封面，是否继续？",
            parent=self.root,
        ):
            return
        candidates = tuple(sorted(self.clip_candidates.values(), key=lambda item: item.start))
        self._start_render_candidates(candidates)

    def _start_render_candidates(self, candidates: tuple[ClipCandidate, ...]) -> None:
        if self.clip_render_thread and self.clip_render_thread.is_alive():
            return
        source = self.clip_analysis_source or (
            Path(self.clip_source.get().strip()).expanduser()
            if self.clip_source.get().strip()
            else None
        )
        if source is None or not source.is_file() or self.clip_analysis_cache is None:
            messagebox.showerror(APP_TITLE, "请先完成原录像分析，再生成候选成片")
            return
        if not candidates:
            messagebox.showinfo(APP_TITLE, "当前没有可生成的候选切片")
            return
        output_root = Path(self.work_dir.get().strip()).expanduser() / "智能切片成片"
        cache_dir = self.clip_analysis_cache
        self.clip_analyze_button.configure(state="disabled")
        self.clip_auto_generate_button.configure(state="disabled")
        self.clip_generate_selected_button.configure(state="disabled")
        self.clip_generate_all_button.configure(state="disabled")
        self.clip_status.set(f"正在生成 {len(candidates)} 条候选成片；不会自动投稿")
        self._show_clip_work_progress(0.0, "正在准备生成成片")

        def progress(value: float | None, message: str) -> None:
            self.messages.put(("clip_render_progress", (value, message)))

        def work() -> None:
            try:
                results = render_candidates(source, candidates, cache_dir, output_root, progress)
                self.messages.put(("clip_render_result", results))
            except Exception as exc:
                self.messages.put(("clip_render_failed", str(exc)))

        self.clip_render_thread = threading.Thread(target=work, daemon=True, name="clip-render")
        self.clip_render_thread.start()

    def _set_clip_publish_time(self, value: str) -> None:
        self.clip_publish_at.set(value)
        self.clip_publish_summary.set(f"{value.replace('-', '年', 1).replace('-', '月', 1).replace(' ', '日 ')}" if value else "立即发布")

    def _clear_clip_publish_time(self) -> None:
        self._set_clip_publish_time("")

    def _choose_clip_publish_time(self) -> None:
        earliest = datetime.now().replace(second=0, microsecond=0) + timedelta(hours=4, minutes=5)
        minute = ((earliest.minute + 4) // 5) * 5
        if minute >= 60:
            earliest = earliest.replace(minute=0) + timedelta(hours=1)
        else:
            earliest = earliest.replace(minute=minute)
        current = self.clip_publish_at.get().strip()
        try:
            initial = datetime.strptime(current, "%Y-%m-%d %H:%M") if current else earliest
        except ValueError:
            initial = earliest

        dialog = Toplevel(self.root)
        dialog.title("选择切片发布时间")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill=BOTH, expand=True)
        ttk.Label(body, text="选择北京时间（至少提前 4 小时）", style="Section.TLabel").grid(
            row=0, column=0, columnspan=6, sticky="w", pady=(0, 12)
        )
        year = StringVar(value=str(initial.year))
        month = StringVar(value=f"{initial.month:02d}")
        day = StringVar(value=f"{initial.day:02d}")
        hour = StringVar(value=f"{initial.hour:02d}")
        minute_var = StringVar(value=f"{initial.minute:02d}")
        fields = (
            (year, [str(value) for value in range(datetime.now().year, datetime.now().year + 4)], "年"),
            (month, [f"{value:02d}" for value in range(1, 13)], "月"),
            (day, [f"{value:02d}" for value in range(1, 32)], "日"),
            (hour, [f"{value:02d}" for value in range(24)], "时"),
            (minute_var, [f"{value:02d}" for value in range(0, 60, 5)], "分"),
        )
        for column, (variable, values, label) in enumerate(fields):
            ttk.Combobox(body, textvariable=variable, values=values, state="readonly", width=6).grid(
                row=1, column=column * 2, sticky="w"
            )
            ttk.Label(body, text=label).grid(row=1, column=column * 2 + 1, sticky="w", padx=(2, 8))

        def confirm() -> None:
            try:
                selected = datetime(
                    int(year.get()), int(month.get()), int(day.get()), int(hour.get()), int(minute_var.get())
                )
            except ValueError:
                messagebox.showerror(APP_TITLE, "请选择有效的日期和时间", parent=dialog)
                return
            if selected < datetime.now() + timedelta(hours=4):
                messagebox.showerror(APP_TITLE, "定时发布时间必须至少在当前时间 4 小时之后", parent=dialog)
                return
            self._set_clip_publish_time(selected.strftime("%Y-%m-%d %H:%M"))
            dialog.grab_release()
            dialog.destroy()

        actions = ttk.Frame(body)
        actions.grid(row=2, column=0, columnspan=10, sticky="e", pady=(16, 0))
        ttk.Button(actions, text="取消", command=dialog.destroy, style="Outline.TButton").pack(side=LEFT, padx=6)
        ttk.Button(actions, text="确定", command=confirm, style="Primary.TButton").pack(side=LEFT)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        dialog.focus_set()

    def _finish_clip_render(self, results: tuple[ClipRenderResult, ...]) -> None:
        for result in results:
            self.clip_candidate_outputs[result.candidate.id] = result
            if self.clip_candidates_tree.exists(result.candidate.id):
                values = list(self.clip_candidates_tree.item(result.candidate.id, "values"))
                if values:
                    values[0] = "已生成"
                    self.clip_candidates_tree.item(result.candidate.id, values=values)
        chosen = results[0]
        selected = self.clip_candidates_tree.selection()
        if selected and selected[0] in self.clip_candidate_outputs:
            chosen = self.clip_candidate_outputs[selected[0]]
        self.clip_video.set(str(chosen.video))
        self.clip_cover.set(str(chosen.cover))
        self.clip_title.set(chosen.candidate.title)
        self.clip_cover_title.set(chosen.candidate.cover_title or chosen.candidate.title)
        self.clip_analyze_button.configure(state="normal")
        self.clip_auto_generate_button.configure(state="normal")
        self.clip_generate_all_button.configure(state="normal")
        self.clip_generate_selected_button.configure(state="normal" if selected else "disabled")
        self._show_clip_work_progress(100.0, f"已生成 {len(results)} 条候选成片")
        self.clip_status.set(
            f"成片已生成到：{chosen.video.parent}。已回填当前候选的视频、黄色标题封面和标题；不会自动投稿。"
        )
        logging.info("智能切片成片生成完成：%d 条，目录 %s", len(results), chosen.video.parent)

    def _open_clip_analysis_cache(self) -> None:
        path = self.clip_analysis_cache
        if path is None and self.clip_source.get().strip():
            path = Path(self.work_dir.get().strip()).expanduser() / "data" / "clip_cache"
        if path and path.is_dir():
            os.startfile(path)
        else:
            messagebox.showinfo(APP_TITLE, "尚未生成分析缓存")

    def _download_and_analyze_online_source(self) -> None:
        url = self.clip_online_url.get().strip()
        if not url:
            messagebox.showinfo(APP_TITLE, "请先粘贴 B 站视频或其他支持的在线视频链接")
            return
        if self.clip_download_thread and self.clip_download_thread.is_alive():
            return
        if self.clip_analysis_thread and self.clip_analysis_thread.is_alive():
            messagebox.showinfo(APP_TITLE, "正在分析其他录像，请等待当前分析完成")
            return
        try:
            config = load_config(config_path())
            preferred = config.recording.executable
        except Exception:
            preferred = "yt-dlp"
        try:
            work_dir = Path(self.work_dir.get().strip()).expanduser().resolve()
            executable = resolve_downloader(
                preferred,
                (
                    app_directory(),
                    app_directory() / ".clip-venv-standalone" / "Scripts",
                    work_dir,
                ),
            )
            output_dir = work_dir / "智能切片来源"
        except (OSError, OnlineSourceError) as exc:
            messagebox.showerror(APP_TITLE, f"无法准备在线来源下载：\n{exc}")
            return
        self.clip_online_download_button.configure(state="disabled")
        self.clip_analysis_text.set("正在准备下载在线来源……")
        self.clip_status.set("正在下载在线来源；下载完成后会自动进入智能分析")
        self._show_clip_work_progress(None, "正在下载在线来源")

        def progress(value: float | None, message: str) -> None:
            self.messages.put(("clip_download_progress", (value, message)))

        def work() -> None:
            try:
                path = download_online_video(url, executable, output_dir, progress)
                self.messages.put(("clip_download_result", str(path)))
            except Exception as exc:
                self.messages.put(("clip_download_failed", str(exc)))

        self.clip_download_thread = threading.Thread(target=work, daemon=True, name="clip-online-download")
        self.clip_download_thread.start()

    def _choose_clip_source(self) -> None:
        if self.clip_render_thread and self.clip_render_thread.is_alive():
            messagebox.showinfo(APP_TITLE, "正在生成候选成片，请完成后再更换原始录像")
            return
        values = filedialog.askopenfilenames(
            initialdir=self.work_dir.get() or str(app_directory()),
            title="选择一个或多个直播录像分P（可按 Ctrl/Shift 多选）",
            filetypes=[("直播录像", "*.flv *.mp4 *.mkv *.ts"), ("所有文件", "*.*")],
        )
        if values:
            sources = tuple(Path(value).expanduser().resolve() for value in values)
            self.clip_sources = sources
            self.clip_source.set(str(sources[0]))
            self._clear_clip_candidates()
            if len(sources) == 1:
                self.clip_source_summary.set(f"已选择 1 个录像：{sources[0].name}")
            else:
                self.clip_source_summary.set(
                    f"已选择 {len(sources)} 个分P，将按选择顺序合并分析、一次批量生成"
                )
            self.clip_analysis_text.set(
                "原始录像已选择；智能分析完成后会先停在视频标题和封面标题审批。"
                + (f"本次共 {len(sources)} 个分P。" if len(sources) > 1 else "")
            )
            if not self.clip_title.get().strip():
                self.clip_title.set(sources[0].stem)

    def _choose_clip_video(self) -> None:
        initial = Path(self.clip_source.get()).parent if self.clip_source.get() else Path(self.work_dir.get())
        value = filedialog.askopenfilename(
            initialdir=str(initial),
            filetypes=[("切片视频", "*.mp4 *.mkv *.flv *.ts"), ("所有文件", "*.*")],
        )
        if not value:
            return
        path = Path(value)
        self.clip_video.set(value)
        current_title = self.clip_title.get().strip()
        if (
            not current_title
            or re.match(r"^[A-Za-z]+\d*(?:-\d+){2,3}-", current_title)
            or re.search(r"-[0-9A-Fa-f]{8}$", current_title)
        ):
            self.clip_title.set(clip_upload_title(path.stem))
        if not self.clip_cover_title.get().strip():
            self.clip_cover_title.set(self.clip_title.get().strip() or path.stem)
        generated_cover = find_generated_clip_cover(path)
        if generated_cover is not None:
            self.clip_cover.set(str(generated_cover))
        self.clip_status.set("切片成片已选择，可以一键投稿")

    def _choose_clip_cover(self) -> None:
        initial = Path(self.clip_video.get()).parent if self.clip_video.get() else Path(self.work_dir.get())
        value = filedialog.askopenfilename(
            initialdir=str(initial),
            filetypes=[("封面图片", "*.jpg *.jpeg *.png *.webp"), ("所有文件", "*.*")],
        )
        if value:
            self.clip_cover.set(value)

    def _upload_clip(self) -> None:
        video = Path(self.clip_video.get().strip()).expanduser()
        source = Path(self.clip_source.get().strip()).expanduser()
        cover_text = self.clip_cover.get().strip()
        title = self.clip_title.get().strip()
        if not source.is_file():
            messagebox.showerror(APP_TITLE, "请先选择原始直播录像，用于读取正确的直播时间")
            return
        if not video.is_file():
            messagebox.showerror(APP_TITLE, "请选择已经生成完成的切片视频")
            return
        if not cover_text:
            generated_cover = find_generated_clip_cover(video)
            if generated_cover is not None:
                cover_text = str(generated_cover)
                self.clip_cover.set(cover_text)
        if not title:
            messagebox.showerror(APP_TITLE, "请填写切片标题")
            return
        if cover_text and not Path(cover_text).expanduser().is_file():
            messagebox.showerror(APP_TITLE, "选择的切片封面不存在")
            return
        self._manual_upload_files(
            (str(video),),
            source="clip",
            title_override=title,
            cover_path=cover_text,
            timestamp_files=(str(source),),
        )

    def _open_clip_dir(self) -> None:
        selected = self.clip_video.get().strip() or self.clip_source.get().strip()
        path = Path(selected).expanduser().parent if selected else Path(self.work_dir.get()).expanduser() / "clip_demo"
        if path.is_dir():
            os.startfile(path)

    def _open_work_dir(self) -> None:
        path = Path(self.work_dir.get()).expanduser()
        if path.is_dir():
            os.startfile(path)

    def _choose_work_dir(self) -> None:
        value = filedialog.askdirectory(initialdir=self.work_dir.get() or str(app_directory()))
        if value:
            self.work_dir.set(value)

    def _choose_biliup(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("程序", "*.exe"), ("所有文件", "*.*")])
        if value:
            self.biliup_executable.set(value)

    def _choose_cookie(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if value:
            self.cookie_file.set(value)

    def _choose_clip_biliup(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("程序", "*.exe"), ("所有文件", "*.*")])
        if value:
            self.clip_biliup_executable.set(value)

    def _choose_clip_cookie(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if value:
            self.clip_cookie_file.set(value)

    def _login(self) -> None:
        self._login_with(
            self.biliup_executable.get().strip() or "biliup",
            self.cookie_file.get().strip(),
            "直播录像",
        )

    def _login_clip(self) -> None:
        self._login_with(
            self.clip_biliup_executable.get().strip() or "biliup",
            self.clip_cookie_file.get().strip(),
            "切片",
        )

    def _login_with(self, executable: str, cookie_text: str, label: str) -> None:
        resolved = shutil.which(executable) or (executable if Path(executable).is_file() else None)
        if not resolved:
            messagebox.showerror(APP_TITLE, f"找不到{label}投稿使用的 biliup，请先选择 biliup.exe")
            return
        cookie = Path(cookie_text).expanduser()
        cookie.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [str(resolved), "--user-cookie", str(cookie), "login"],
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )

    def _create_tray_image(self):
        assert Image is not None and ImageDraw is not None
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 8, 60, 56), radius=13, fill=(251, 114, 153, 255))
        draw.rectangle((16, 22, 48, 44), fill=(255, 255, 255, 255))
        draw.polygon(((48, 26), (58, 20), (58, 46), (48, 40)), fill=(255, 255, 255, 255))
        draw.ellipse((25, 29, 31, 35), fill=(251, 114, 153, 255))
        draw.ellipse((36, 29, 42, 35), fill=(251, 114, 153, 255))
        return image

    def _minimize_to_tray(self) -> None:
        if pystray is None or Image is None:
            self.root.iconify()
            self.messages.put(("log", "托盘组件不可用，已最小化到任务栏"))
            return
        if self.tray_icon is not None:
            self.root.withdraw()
            return

        def show_window(_icon=None, _item=None) -> None:
            self.root.after(0, self._restore_from_tray)

        def exit_client(_icon=None, _item=None) -> None:
            self.root.after(0, self._exit_from_tray)

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", show_window, default=True),
            pystray.MenuItem("处理本场录像后退出", exit_client),
        )
        self.tray_icon = pystray.Icon("bili_live_auto", self._create_tray_image(), APP_TITLE, menu)
        self.root.withdraw()
        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True, name="system-tray")
        self.tray_thread.start()
        self.messages.put(("log", "已最小化到系统托盘，自动监控继续运行"))

    def _restore_from_tray(self) -> None:
        self._stop_tray_icon()
        self.root.deiconify()
        self.root.state("normal")
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()
        self.status_text.set("已从系统托盘恢复到前台")
        logging.info("托盘图标被激活，客户端窗口已置于最前方")
        self.root.after(500, self._release_temporary_topmost)

    def _release_temporary_topmost(self) -> None:
        try:
            self.root.attributes("-topmost", False)
        except Exception:
            pass

    def _exit_from_tray(self) -> None:
        self._restore_from_tray()
        self.root.after(80, self._request_exit)

    def _stop_tray_icon(self) -> None:
        icon = self.tray_icon
        tray_thread = self.tray_thread
        self.tray_icon = None
        self.tray_thread = None
        if icon is not None:
            # Windows 偶尔会在进程退出后留下“幽灵”通知区图标。先直接发送
            # NIM_DELETE，再修改 pystray 的可见状态，确保不依赖析构时机。
            try:
                hide = getattr(icon, "_hide", None)
                if callable(hide) and getattr(icon, "_hwnd", None):
                    hide()
            except Exception:
                logging.debug("显式注销 Windows 托盘图标失败", exc_info=True)
            try:
                icon.visible = False
            except Exception:
                pass
            try:
                icon.stop()
            except Exception:
                pass
        if (
            tray_thread is not None
            and tray_thread.is_alive()
            and tray_thread is not threading.current_thread()
        ):
            tray_thread.join(timeout=5)
            if tray_thread.is_alive():
                try:
                    icon.stop()
                except Exception:
                    pass
                tray_thread.join(timeout=2)
            if tray_thread.is_alive():
                logging.warning("托盘线程未在退出时及时结束，Windows 可能暂时显示缓存图标")
            else:
                try:
                    release_icon = getattr(icon, "_release_icon", None)
                    if callable(release_icon):
                        release_icon()
                except Exception:
                    logging.debug("释放托盘图标资源失败", exc_info=True)

    def _finish_exit(self) -> None:
        self._stop_tray_icon()
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        self.root.destroy()

    def _confirm_exit(self) -> bool:
        if self.exiting:
            return False
        palette = PALETTES[self.theme_mode.get()]
        result = {"confirmed": False}
        dialog = Toplevel(self.root)
        dialog.title("退出确认")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(background=palette["border"])
        dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))

        content = ttk.Frame(dialog, padding=(24, 28, 24, 22), style="Dialog.TFrame")
        content.pack(fill=BOTH, expand=True, padx=1, pady=1)
        ttk.Label(content, text="确定要退出吗？", style="DialogTitle.TLabel").pack(pady=(4, 9))
        detail = "退出前会先检查本场录像，已完成的有效文件仍会进入投稿队列。"
        ttk.Label(content, text=detail, style="MutedCard.TLabel", wraplength=340, justify="center").pack()
        buttons = ttk.Frame(content, style="Dialog.TFrame")
        buttons.pack(fill=X, pady=(24, 0))

        previous_alpha = self.root.attributes("-alpha")

        def close(confirmed: bool) -> None:
            result["confirmed"] = confirmed
            try:
                dialog.grab_release()
            except Exception:
                pass
            try:
                self.root.attributes("-alpha", previous_alpha)
            except Exception:
                pass
            dialog.destroy()

        ttk.Button(
            buttons,
            text="退出",
            command=lambda: close(True),
            style="DialogExit.TButton",
            width=15,
        ).pack(side=LEFT, expand=True, fill=X, padx=(0, 5))
        cancel = ttk.Button(
            buttons,
            text="取消",
            command=lambda: close(False),
            style="DialogCancel.TButton",
            width=15,
        )
        cancel.pack(side=LEFT, expand=True, fill=X, padx=(5, 0))

        self.root.update_idletasks()
        width, height = 430, 220
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        try:
            self.root.attributes("-alpha", 0.72)
        except Exception:
            pass
        dialog.grab_set()
        dialog.lift()
        cancel.focus_set()
        dialog.bind("<Escape>", lambda _event: close(False))
        dialog.bind("<Return>", lambda _event: close(False))
        self.root.wait_window(dialog)
        return bool(result["confirmed"])

    def _request_exit(self) -> None:
        if self.exiting:
            return
        if not self._confirm_exit():
            logging.info("已取消退出，客户端继续运行")
            return
        self.exiting = True
        if self.application and self.worker and self.worker.is_alive():
            self.application.stop_event.set()
            self.status_text.set("正在检查本场录像并安全退出……")
            self.messages.put(("log", "退出前正在等待本地录像完成写入；有效文件会继续进入投稿队列"))
            self._wait_for_safe_exit()
        else:
            self._finish_exit()

    def _wait_for_safe_exit(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(250, self._wait_for_safe_exit)
            return
        self._finish_exit()

    def _drain_messages(self) -> None:
        while True:
            try:
                kind, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log_box.configure(state="normal")
                self.log_box.insert(END, f"{value}\n")
                self.log_box.see(END)
                self.log_box.configure(state="disabled")
            elif kind == "upload_progress":
                progress, message = value
                self._show_upload_progress(progress, str(message))
            elif kind == "stopped":
                self.status_text.set("已停止")
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
            elif kind == "room":
                status, streamer, configured_name, recorder_warning = value
                self.status_text.set(str(status))
                if configured_name and not streamer_names_match(configured_name, streamer):
                    replace = messagebox.askyesno(
                        "主播身份不一致",
                        f"房间号 {self.room_id.get()} 的真实主播是：\n\n{streamer}\n\n"
                        f"当前填写的是：\n\n{configured_name}\n\n是否替换为真实主播名？",
                    )
                    if replace:
                        self.streamer.set(str(streamer))
                elif not configured_name:
                    self.streamer.set(str(streamer))
                if recorder_warning:
                    messagebox.showwarning("录播姬房间未配置", recorder_warning)
            elif kind == "start_ready":
                config, streamer = value
                if not self.streamer.get().strip():
                    self.streamer.set(str(streamer))
                    if not self._save(quiet=True):
                        self.start_button.configure(state="normal")
                        continue
                    config = load_config(config_path())
                self._begin_monitor(config)
            elif kind == "identity_mismatch":
                exc = value
                replace = messagebox.askyesno(
                    "房间号与主播名不一致",
                    f"房间号 {exc.room_id} 的真实主播是：\n\n{exc.actual_name}\n\n"
                    f"当前填写的是：\n\n{exc.configured_name}\n\n"
                    "为防止录错和投错，监控尚未启动。是否立即替换并重新校验？",
                )
                self.start_button.configure(state="normal")
                self.status_text.set("身份校验未通过")
                if replace:
                    self.streamer.set(exc.actual_name)
                    if self._save(quiet=True):
                        self.root.after(0, self._start)
            elif kind == "start_error":
                self.start_button.configure(state="normal")
                self.status_text.set("启动校验失败")
                messagebox.showerror(APP_TITLE, str(value))
            elif kind == "manual_result":
                success, message = value
                self._refresh_history()
                self.status_text.set("手动投稿成功" if success else "手动投稿失败")
                if success:
                    self.manual_append_bvid.set("")
                    self._show_upload_progress(100.0, f"手动投稿：{message}")
                    messagebox.showinfo(APP_TITLE, f"手动投稿成功：\n{message}")
                else:
                    self._show_upload_progress(0.0, f"手动投稿失败：{message}")
                    messagebox.showerror(APP_TITLE, f"手动投稿失败：\n{message}")
            elif kind == "clip_result":
                success, message = value
                self._refresh_history()
                self.clip_upload_button.configure(state="normal")
                if success:
                    self.clip_status.set(f"切片投稿成功：{message}")
                    self._show_upload_progress(100.0, f"切片：{message}")
                    messagebox.showinfo(APP_TITLE, f"切片投稿成功：\n{message}")
                else:
                    self.clip_status.set(f"切片投稿失败：{message}")
                    self._show_upload_progress(0.0, f"切片投稿失败：{message}")
                    messagebox.showerror(APP_TITLE, f"切片投稿失败：\n{message}")
            elif kind == "clip_ai_test_result":
                success, message = value
                self.clip_ai_test_button.configure(state="normal")
                if success:
                    self.clip_ai_status.set(str(message))
                    logging.info("切片 AI API 连接测试成功：%s", message)
                    messagebox.showinfo(APP_TITLE, f"API 测试成功：\n{message}")
                else:
                    self.clip_ai_status.set(f"连接测试失败：{message}")
                    logging.warning("切片 AI API 连接测试失败：%s", message)
                    messagebox.showerror(APP_TITLE, f"API 测试失败：\n{message}")
            elif kind == "clip_download_progress":
                progress, message = value
                self.clip_analysis_text.set(str(message))
                self._show_clip_work_progress(progress, str(message))
            elif kind == "clip_download_result":
                self.clip_online_download_button.configure(state="normal")
                self.clip_download_thread = None
                path = Path(value).expanduser().resolve()
                self.clip_sources = (path,)
                self.clip_source.set(str(path))
                self.clip_source_summary.set(f"在线来源已下载：{path.name}")
                self.clip_status.set("在线来源下载完成，正在进入智能转写和候选分析")
                logging.info("在线切片来源下载完成：%s", path)
                self._start_clip_analysis(auto_render=True)
            elif kind == "clip_download_failed":
                self.clip_online_download_button.configure(state="normal")
                self.clip_download_thread = None
                self._show_clip_work_progress(0.0, "在线来源下载失败")
                self.clip_status.set("在线来源下载失败")
                self.clip_analysis_text.set(f"下载失败：{value}")
                messagebox.showerror(APP_TITLE, f"在线来源下载失败：\n{value}")
            elif kind == "clip_analysis_progress":
                self.clip_analysis_text.set(str(value))
                self._show_clip_work_progress(None, str(value))
            elif kind == "clip_transcription_complete":
                analysis = value
                source_count = max(1, len(self._selected_clip_sources()))
                self.clip_status.set(
                    f"本地转写已完成（{source_count} 个分P），正在生成候选；请稍候"
                )
                self.clip_analysis_text.set(
                    f"字幕转写已完成，正在进行语义分析（本地已生成 {len(analysis.candidates)} 个基础候选）"
                )
                logging.info("本地字幕转写完成：%s", analysis.source)
                try:
                    self.root.bell()
                except Exception:
                    pass
                if self.tray_icon is not None:
                    try:
                        self.tray_icon.notify("本地转写已完成，正在生成候选", APP_TITLE)
                    except Exception:
                        pass
            elif kind == "clip_analysis_result":
                self._show_clip_analysis(value)
            elif kind == "clip_analysis_failed":
                self.clip_auto_render_requested = False
                self.clip_auto_generate_button.configure(state="normal")
                self.clip_analyze_button.configure(state="normal")
                self.clip_analysis_text.set(f"分析失败：{value}")
                self.clip_status.set("未生成候选切片")
                self._show_clip_work_progress(0.0, "分析失败")
                messagebox.showerror(APP_TITLE, f"智能切片分析失败：\n{value}")
            elif kind == "clip_render_progress":
                progress, message = value
                self._show_clip_work_progress(progress, str(message))
            elif kind == "clip_render_result":
                self._finish_clip_render(value)
            elif kind == "clip_render_failed":
                self.clip_auto_generate_button.configure(state="normal")
                self.clip_analyze_button.configure(state="normal")
                self.clip_generate_all_button.configure(state="normal" if self.clip_candidates else "disabled")
                self.clip_generate_selected_button.configure(
                    state="normal" if self.clip_candidates_tree.selection() else "disabled"
                )
                self._show_clip_work_progress(0.0, "生成失败")
                self.clip_status.set(f"候选成片生成失败：{value}")
                messagebox.showerror(APP_TITLE, f"候选成片生成失败：\n{value}")
            elif kind == "error":
                messagebox.showerror(APP_TITLE, str(value))
        self.root.after(100, self._drain_messages)

    def _on_close(self) -> None:
        self._request_exit()


def main() -> int:
    root = Tk()
    DesktopClient(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
