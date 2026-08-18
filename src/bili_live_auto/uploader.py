from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import UploadSettings
from .models import Recording

LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[float | None, str], None]


class UploadError(RuntimeError):
    pass


def scheduled_publish_timestamp(value: str, *, now: float | None = None) -> int | None:
    """Convert Beijing local publish time to biliup's 10-digit ``dtime``."""
    text = value.strip()
    if not text:
        return None
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise UploadError("定时发布时间格式应为 YYYY-MM-DD HH:MM（也可带秒）")
    # Use an explicit UTC+8 offset so the packaged Windows client does not
    # depend on an optional system tzdata installation.
    timestamp = int(parsed.replace(tzinfo=timezone(timedelta(hours=8))).timestamp())
    current = time.time() if now is None else now
    if timestamp < int(current) + 4 * 3600:
        raise UploadError("定时发布时间必须至少在当前时间 4 小时之后")
    return timestamp


class _TemplateValues(dict[str, object]):
    def __missing__(self, key: str) -> str:
        raise UploadError(f"投稿模板使用了未知字段：{key}")


def render(template: str, recording: Recording) -> str:
    try:
        return template.format_map(_TemplateValues(recording.template_context()))
    except (KeyError, ValueError) as exc:
        raise UploadError(f"投稿模板格式错误：{exc}") from exc


def extract_upload_percent(text: str) -> float | None:
    matches = re.findall(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%", text)
    if not matches:
        return None
    value = float(matches[-1])
    return max(0.0, min(100.0, value))


def summarize_upload_failure(output: list[str], returncode: int) -> str:
    """Keep the actionable biliup hint instead of showing an opaque exit code.

    The command itself is never written into this text, and common credential
    labels are redacted in case a third-party version of biliup echoes them.
    """
    lines: list[str] = []
    combined = "\n".join(output)
    code_match = re.search(r"(?:code|错误码)\s*[:=：]\s*(-?\d+)", combined, re.IGNORECASE)
    code_hint = ""
    if code_match:
        code = int(code_match.group(1))
        hints = {
            21566: (
                "B站拒绝了 APP 投稿接口请求（错误码 21566）。官网投稿使用 Web 接口；"
                "请使用支持 Web 投稿的 biliupR，并确认命令包含 --submit web。"
            ),
        }
        code_hint = hints.get(code, f"B站返回错误码 {code}。")
    for raw in output[-12:]:
        clean = re.sub(
            r"(?i)\b(cookie|token|authorization|sessdata)\b\s*[:=]\s*\S+",
            r"\1=<已隐藏>",
            raw,
        )
        if clean:
            lines.append(clean[:260])
    if not lines:
        return f"投稿失败，biliup 退出码：{returncode}（未返回具体提示）"
    suffix = f"\n处理建议：{code_hint}" if code_hint else ""
    return f"投稿失败，biliup 退出码：{returncode}\n原始提示：\n" + "\n".join(lines) + suffix


class Uploader:
    def __init__(self, settings: UploadSettings, progress_callback: ProgressCallback | None = None) -> None:
        self.settings = settings
        self.progress_callback = progress_callback

    def _progress(self, value: float | None, message: str) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(value, message)
        except Exception:
            LOGGER.debug("投稿进度回调发生错误", exc_info=True)

    def build_command(self, recording: Recording, cover: Path | None = None) -> list[str]:
        settings = self.settings
        command = [
            settings.executable,
            "--user-cookie",
            str(settings.cookie_file),
            "upload",
            "--submit",
            settings.submit,
            *recording.files,
            "--line",
            settings.line,
            "--limit",
            str(settings.limit),
            "--copyright",
            str(settings.copyright),
            "--source",
            render(settings.source, recording),
            "--tid",
            str(settings.tid),
            "--tag",
            ",".join(settings.tags),
        ]
        dtime = scheduled_publish_timestamp(settings.publish_at)
        if dtime is not None:
            command.extend(("--dtime", str(dtime)))
        if settings.dynamic:
            command.extend(("--dynamic", render(settings.dynamic, recording)))
        if settings.is_only_self:
            command.extend(("--is-only-self", "1"))
        if settings.no_reprint:
            command.extend(("--no-reprint", "1"))
        if settings.charging_pay:
            command.extend(("--charging-pay", "1"))
        if cover is not None:
            command.extend(("--cover", str(cover)))
        command.extend(
            (
                "--title",
                render(settings.title, recording)[:80],
                "--desc",
                render(settings.description, recording),
            )
        )
        command.extend(settings.extra_args)
        return command

    def build_append_command(self, recording: Recording, bvid: str) -> list[str]:
        settings = self.settings
        command = [
            settings.executable,
            "--user-cookie",
            str(settings.cookie_file),
            "append",
            "--submit",
            settings.submit,
            "--vid",
            bvid,
            *recording.files,
            "--line",
            settings.line,
            "--limit",
            str(settings.limit),
            "--copyright",
            str(settings.copyright),
            "--source",
            render(settings.source, recording),
            "--tid",
            str(settings.tid),
            "--tag",
            ",".join(settings.tags),
            "--title",
            render(settings.title, recording)[:80],
            "--desc",
            render(settings.description, recording),
        ]
        command.extend(settings.extra_args)
        return command

    def _run(self, command: list[str]) -> tuple[list[str], int]:
        self._progress(None, "正在连接投稿服务器")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            self._progress(0.0, "找不到投稿程序")
            raise UploadError(f"找不到投稿程序：{self.settings.executable}") from exc
        output: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
            if clean:
                output.append(clean)
                LOGGER.info("[biliup] %s", clean)
                percent = extract_upload_percent(clean)
                if percent is not None:
                    self._progress(percent, f"正在上传 {percent:g}%")
                elif "submit" in clean.casefold() or "提交" in clean:
                    self._progress(None, "正在提交稿件信息")
        returncode = process.wait()
        if returncode != 0:
            self._progress(0.0, f"投稿程序退出码 {returncode}")
        return output, returncode

    def upload(self, recording: Recording, cover: Path | None = None) -> str | None:
        if not self.settings.enabled:
            raise UploadError("自动投稿尚未启用")
        paths = tuple(Path(value) for value in recording.files)
        missing = [path for path in paths if not path.is_file()]
        if missing:
            raise UploadError(f"待投稿文件不存在：{missing[0]}")
        if not self.settings.cookie_file.is_file():
            raise UploadError(f"登录凭据不存在：{self.settings.cookie_file}")
        if cover is not None and not cover.is_file():
            raise UploadError(f"投稿封面不存在：{cover}")
        LOGGER.info("开始投稿：%s（%d 个分P）", paths[0], len(paths))
        self._progress(0.0, f"准备上传 {len(paths)} 个分P")
        output, returncode = self._run(self.build_command(recording, cover=cover))
        if returncode != 0:
            raise UploadError(summarize_upload_failure(output, returncode))
        LOGGER.info("投稿提交成功：%s", paths[0])
        self._progress(100.0, "投稿提交成功")
        combined = "\n".join(output)
        match = re.search(r'"bvid"\s*:\s*String\("(BV[0-9A-Za-z]+)"\)', combined)
        if not match:
            match = re.search(r'"bvid"\s*:\s*"(BV[0-9A-Za-z]+)"', combined)
        return match.group(1) if match else None

    def append(self, recording: Recording, bvid: str) -> None:
        if not self.settings.enabled:
            raise UploadError("自动投稿尚未启用")
        paths = tuple(Path(value) for value in recording.files)
        missing = [path for path in paths if not path.is_file()]
        if missing:
            raise UploadError(f"待追加文件不存在：{missing[0]}")
        if not self.settings.cookie_file.is_file():
            raise UploadError(f"登录凭据不存在：{self.settings.cookie_file}")
        if not re.fullmatch(r"BV[0-9A-Za-z]+", bvid):
            raise UploadError(f"BV 号格式不正确：{bvid}")
        LOGGER.info("开始向同场稿件 %s 追加 %d 个分P", bvid, len(paths))
        self._progress(0.0, f"准备追加 {len(paths)} 个分P")
        _output, returncode = self._run(self.build_append_command(recording, bvid))
        if returncode != 0:
            detail = summarize_upload_failure(_output, returncode)
            raise UploadError(detail.replace("投稿失败", "追加分P失败", 1))
        LOGGER.info("分P追加成功：%s", bvid)
        self._progress(100.0, "分P追加成功")
