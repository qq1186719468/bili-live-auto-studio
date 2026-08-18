from __future__ import annotations

import tomllib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RoomConfig:
    id: int
    name: str = ""
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class AppSettings:
    poll_seconds: int = 30
    error_retry_seconds: int = 60
    work_dir: Path = Path("data")
    log_level: str = "INFO"
    theme: str = "dark"


@dataclass(frozen=True, slots=True)
class RecordingSettings:
    backend: str = "bililiverecorder"
    executable: str = "yt-dlp"
    format: str = "best"
    extension: str = "mp4"
    min_file_size_mb: float = 1.0
    cookie_file: Path | None = None
    watch_dir: Path = Path("~/Videos/bilibili")
    watch_extensions: tuple[str, ...] = ("mp4", "flv", "mkv", "ts")
    stable_seconds: int = 15
    stable_timeout_seconds: int = 300
    local_scan_seconds: float = 2.0
    manual_stop_stable_seconds: float = 30.0
    interrupt_finalize_timeout_seconds: int = 60
    retention_hours: int = 168
    retention_scan_minutes: int = 60
    delete_only_uploaded: bool = True
    extra_args: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class UploadSettings:
    enabled: bool = False
    executable: str = "biliup"
    cookie_file: Path = Path("secrets/cookies.json")
    # The web endpoint matches Bilibili's current creator center.  Older
    # biliupR builds accepted this option but ignored it; those builds are
    # rejected at runtime with an APP-interface error (for example 21566).
    submit: str = "web"
    line: str = "bda2"
    limit: int = 3
    copyright: int = 2
    source: str = "https://live.bilibili.com/{room_id}"
    tid: int = 171
    tags: tuple[str, ...] = ("直播录像", "录播")
    title: str = "{streamer}直播录像 {start_time}"
    description: str = "直播间：{room_url}\n原直播标题：{room_title}"
    # Empty means publish immediately. Otherwise use Beijing local time in
    # YYYY-MM-DD HH:MM (seconds are accepted as well).
    publish_at: str = ""
    dynamic: str = ""
    is_only_self: bool = False
    no_reprint: bool = False
    charging_pay: bool = False
    extra_args: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ClipAISettings:
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    protocol: str = "auto"
    api_key_file: Path = Path("secrets/clip-ai-key.txt")
    timeout_seconds: int = 90
    chunk_minutes: int = 30
    auto_after_live_upload: bool = False


@dataclass(frozen=True, slots=True)
class Config:
    app: AppSettings
    recording: RecordingSettings
    upload: UploadSettings
    clip_upload: UploadSettings
    clip_ai: ClipAISettings
    rooms: tuple[RoomConfig, ...]
    source_path: Path


def _path(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    result = Path(os.path.expandvars(value)).expanduser()
    return result if result.is_absolute() else (base / result).resolve()


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] 必须是 TOML 表")
    return value


def load_config(path: str | Path) -> Config:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ConfigError(f"配置文件不存在：{source}")
    try:
        with source.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 格式错误：{exc}") from exc

    base = source.parent
    app_raw = _section(data, "app")
    rec_raw = _section(data, "recording")
    upload_raw = _section(data, "upload")
    clip_ai_raw = _section(data, "clip_ai")
    raw_clip_upload = data.get("clip_upload")
    if raw_clip_upload is None:
        clip_upload_raw = dict(upload_raw)
    elif isinstance(raw_clip_upload, dict):
        clip_upload_raw = raw_clip_upload
    else:
        raise ConfigError("[clip_upload] 必须是 TOML 表")

    app = AppSettings(
        poll_seconds=int(app_raw.get("poll_seconds", 30)),
        error_retry_seconds=int(app_raw.get("error_retry_seconds", 60)),
        work_dir=_path(base, str(app_raw.get("work_dir", "./data"))) or base / "data",
        log_level=str(app_raw.get("log_level", "INFO")).upper(),
        theme=str(app_raw.get("theme", "dark")).lower(),
    )
    recording = RecordingSettings(
        backend=str(rec_raw.get("backend", "bililiverecorder")).lower(),
        executable=str(rec_raw.get("executable", "yt-dlp")),
        format=str(rec_raw.get("format", "best")),
        extension=str(rec_raw.get("extension", "mp4")).lstrip("."),
        min_file_size_mb=float(rec_raw.get("min_file_size_mb", 1)),
        cookie_file=_path(base, str(rec_raw.get("cookie_file", ""))),
        watch_dir=_path(base, str(rec_raw.get("watch_dir", "~/Videos/bilibili"))) or base,
        watch_extensions=tuple(str(value).lower().lstrip(".") for value in rec_raw.get("watch_extensions", ["mp4", "flv", "mkv", "ts"])),
        stable_seconds=int(rec_raw.get("stable_seconds", 15)),
        stable_timeout_seconds=int(rec_raw.get("stable_timeout_seconds", 300)),
        local_scan_seconds=float(rec_raw.get("local_scan_seconds", 2)),
        manual_stop_stable_seconds=float(rec_raw.get("manual_stop_stable_seconds", 30)),
        interrupt_finalize_timeout_seconds=int(rec_raw.get("interrupt_finalize_timeout_seconds", 60)),
        retention_hours=int(rec_raw.get("retention_hours", 168)),
        retention_scan_minutes=int(rec_raw.get("retention_scan_minutes", 60)),
        delete_only_uploaded=bool(rec_raw.get("delete_only_uploaded", True)),
        extra_args=tuple(map(str, rec_raw.get("extra_args", []))),
    )
    upload_cookie = _path(base, str(upload_raw.get("cookie_file", "./secrets/cookies.json")))
    assert upload_cookie is not None
    upload = UploadSettings(
        enabled=bool(upload_raw.get("enabled", False)),
        executable=str(upload_raw.get("executable", "biliup")),
        cookie_file=upload_cookie,
        submit=str(upload_raw.get("submit", "web")).strip().lower(),
        line=str(upload_raw.get("line", "bda2")),
        limit=int(upload_raw.get("limit", 3)),
        copyright=int(upload_raw.get("copyright", 2)),
        source=str(upload_raw.get("source", "https://live.bilibili.com/{room_id}")),
        tid=int(upload_raw.get("tid", 171)),
        tags=tuple(map(str, upload_raw.get("tags", ["直播录像", "录播"]))),
        title=str(upload_raw.get("title", "{streamer}直播录像 {start_time}")),
        description=str(upload_raw.get("description", "直播间：{room_url}")),
        publish_at=str(upload_raw.get("publish_at", "")).strip(),
        dynamic=str(upload_raw.get("dynamic", "")).strip(),
        is_only_self=bool(upload_raw.get("is_only_self", False)),
        no_reprint=bool(upload_raw.get("no_reprint", False)),
        charging_pay=bool(upload_raw.get("charging_pay", False)),
        extra_args=tuple(map(str, upload_raw.get("extra_args", []))),
    )
    clip_cookie = _path(base, str(clip_upload_raw.get("cookie_file", upload.cookie_file)))
    assert clip_cookie is not None
    clip_upload = UploadSettings(
        enabled=bool(clip_upload_raw.get("enabled", True)),
        executable=str(clip_upload_raw.get("executable", upload.executable)),
        cookie_file=clip_cookie,
        submit=str(clip_upload_raw.get("submit", upload.submit)).strip().lower(),
        line=str(clip_upload_raw.get("line", upload.line)),
        limit=int(clip_upload_raw.get("limit", upload.limit)),
        copyright=int(clip_upload_raw.get("copyright", upload.copyright)),
        source=str(clip_upload_raw.get("source", upload.source)),
        tid=int(clip_upload_raw.get("tid", upload.tid)),
        tags=tuple(map(str, clip_upload_raw.get("tags", upload.tags))),
        title=str(clip_upload_raw.get("title", "{streamer}直播切片")),
        description=str(clip_upload_raw.get("description", upload.description)),
        publish_at=str(clip_upload_raw.get("publish_at", "")).strip(),
        dynamic=str(clip_upload_raw.get("dynamic", "")).strip(),
        is_only_self=bool(clip_upload_raw.get("is_only_self", False)),
        no_reprint=bool(clip_upload_raw.get("no_reprint", False)),
        charging_pay=bool(clip_upload_raw.get("charging_pay", False)),
        extra_args=tuple(map(str, clip_upload_raw.get("extra_args", []))),
    )
    clip_ai_key_file = _path(base, str(clip_ai_raw.get("api_key_file", "./secrets/clip-ai-key.txt")))
    assert clip_ai_key_file is not None
    clip_ai = ClipAISettings(
        enabled=bool(clip_ai_raw.get("enabled", False)),
        base_url=str(clip_ai_raw.get("base_url", "")).strip(),
        model=str(clip_ai_raw.get("model", "")).strip(),
        protocol=str(clip_ai_raw.get("protocol", "auto")).strip().lower(),
        api_key_file=clip_ai_key_file,
        timeout_seconds=int(clip_ai_raw.get("timeout_seconds", 90)),
        chunk_minutes=int(clip_ai_raw.get("chunk_minutes", 30)),
        auto_after_live_upload=bool(clip_ai_raw.get("auto_after_live_upload", False)),
    )

    rooms_raw = data.get("rooms", [])
    if not isinstance(rooms_raw, list):
        raise ConfigError("[[rooms]] 必须是数组表")
    rooms: list[RoomConfig] = []
    for index, item in enumerate(rooms_raw, start=1):
        if not isinstance(item, dict) or "id" not in item:
            raise ConfigError(f"第 {index} 个房间缺少 id")
        rooms.append(RoomConfig(int(item["id"]), str(item.get("name", "")), bool(item.get("enabled", True))))

    if app.poll_seconds < 10:
        raise ConfigError("poll_seconds 不应小于 10 秒")
    if app.theme not in {"dark", "light"}:
        raise ConfigError("theme 只能是 dark 或 light")
    if app.error_retry_seconds < 10:
        raise ConfigError("error_retry_seconds 不应小于 10 秒")
    if recording.min_file_size_mb < 0:
        raise ConfigError("min_file_size_mb 不能为负数")
    if recording.backend not in {"bililiverecorder", "livehime", "yt-dlp"}:
        raise ConfigError("recording.backend 只能是 bililiverecorder、livehime 或 yt-dlp")
    if not recording.watch_extensions:
        raise ConfigError("watch_extensions 不能为空")
    if recording.stable_seconds < 1 or recording.stable_timeout_seconds < recording.stable_seconds:
        raise ConfigError("文件稳定等待时间配置无效")
    if recording.local_scan_seconds < 0.2 or recording.manual_stop_stable_seconds < 5:
        raise ConfigError("本地录像中断检测时间配置无效")
    if recording.interrupt_finalize_timeout_seconds < recording.stable_seconds:
        raise ConfigError("中断后的录像等待时间不能小于 stable_seconds")
    if recording.retention_hours < 1 or recording.retention_scan_minutes < 1:
        raise ConfigError("录像保留和清理周期必须大于 0")
    if upload.copyright not in (1, 2):
        raise ConfigError("copyright 只能是 1（自制）或 2（转载）")
    if clip_upload.copyright not in (1, 2):
        raise ConfigError("clip_upload.copyright 只能是 1（自制）或 2（转载）")
    if upload.submit not in {"app", "web", "b-cut-android"}:
        raise ConfigError("submit 只能是 app、web 或 b-cut-android")
    if clip_upload.submit not in {"app", "web", "b-cut-android"}:
        raise ConfigError("clip_upload.submit 只能是 app、web 或 b-cut-android")
    if clip_ai.protocol not in {"auto", "responses", "chat_completions"}:
        raise ConfigError("clip_ai.protocol 只能是 auto、responses 或 chat_completions")
    if clip_ai.enabled and (not clip_ai.base_url or not clip_ai.model):
        raise ConfigError("启用 clip_ai 时必须填写 base_url 和 model")
    if clip_ai.base_url and not clip_ai.base_url.lower().startswith(("http://", "https://")):
        raise ConfigError("clip_ai.base_url 必须以 http:// 或 https:// 开头")
    if not 10 <= clip_ai.timeout_seconds <= 300:
        raise ConfigError("clip_ai.timeout_seconds 必须在 10 到 300 秒之间")
    if not 5 <= clip_ai.chunk_minutes <= 60:
        raise ConfigError("clip_ai.chunk_minutes 必须在 5 到 60 分钟之间")
    if upload.enabled and not any(room.enabled for room in rooms):
        raise ConfigError("已开启投稿，但没有启用任何直播间")
    if recording.backend in {"bililiverecorder", "livehime"} and sum(room.enabled for room in rooms) > 1:
        raise ConfigError("录播姬目录监听模式目前只支持一个启用的直播间")

    return Config(app, recording, upload, clip_upload, clip_ai, tuple(rooms), source)
