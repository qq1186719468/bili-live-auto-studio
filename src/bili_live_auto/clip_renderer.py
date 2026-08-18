from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

from .clipper import ClipCandidate, ClipperError, TranscriptSegment, find_ffmpeg
from .zh_simplify import to_simplified


RenderProgressCallback = Callable[[float | None, str], None]


@dataclass(frozen=True)
class ClipRenderResult:
    candidate: ClipCandidate
    video: Path
    cover: Path
    subtitles: Path
    reused: bool = False


def _safe_filename(value: str, fallback: str = "直播切片") -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    clean = re.sub(r"\s+", " ", clean)
    return (clean or fallback)[:72]


def _load_transcript(cache_dir: Path) -> tuple[TranscriptSegment, ...]:
    path = cache_dir / "transcript.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClipperError("找不到候选对应的转写缓存，请重新分析原录像") from exc
    result: list[TranscriptSegment] = []
    for item in data.get("segments", []):
        try:
            start = float(item["start"])
            end = float(item["end"])
            text = str(item["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if text and end > start:
            result.append(TranscriptSegment(start, end, text))
    if not result:
        raise ClipperError("转写缓存中没有可用字幕")
    return tuple(result)


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, int(round(seconds * 100)))
    minutes, centisecond = divmod(centiseconds, 6000)
    hour, minute = divmod(minutes, 60)
    second, centisecond = divmod(centisecond, 100)
    return f"{hour}:{minute:02d}:{second:02d}.{centisecond:02d}"


def _subtitle_text(value: str, width: int = 16) -> str:
    clean = re.sub(r"\s+", "", to_simplified(value)).replace("{", "（").replace("}", "）")
    if len(clean) <= width:
        return clean
    return clean[:width] + r"\N" + clean[width : width * 2]


def write_candidate_subtitles(
    path: Path,
    candidate: ClipCandidate,
    segments: Iterable[TranscriptSegment],
) -> int:
    events: list[str] = []
    output_offset = 0.0
    source_segments = tuple(segments)
    for range_start, range_end in candidate.timeline_ranges:
        range_duration = range_end - range_start
        for segment in source_segments:
            if segment.end <= range_start or segment.start >= range_end:
                continue
            start = output_offset + max(0.0, segment.start - range_start)
            end = output_offset + min(range_duration, segment.end - range_start)
            if end - start < 0.15:
                continue
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Subtitle,,0,0,0,,{_subtitle_text(segment.text)}"
            )
        output_offset += range_duration
    if not events:
        raise ClipperError("候选时间范围内没有可用字幕，无法生成成片")
    header = """[Script Info]
Title: Bili Live Auto Clip
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Subtitle,Microsoft YaHei UI,54,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,3,2,0,2,70,70,145,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")
    return len(events)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in (Path("C:/Windows/Fonts/msyhbd.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _cover_lines(draw: ImageDraw.ImageDraw, title: str, font: ImageFont.ImageFont) -> tuple[str, ...]:
    clean = re.sub(r"\s+", "", title).replace("「", "").replace("」", "")
    max_width = 1140
    lines: list[str] = []
    remaining = clean
    while remaining and len(lines) < 2:
        line = ""
        for character in remaining:
            candidate = line + character
            if line and draw.textbbox((0, 0), candidate, font=font, stroke_width=7)[2] > max_width:
                break
            line = candidate
        if not line:
            line = remaining[:1]
        lines.append(line)
        remaining = remaining[len(line) :]
    if remaining and lines:
        lines[-1] = lines[-1][:-1] + "…"
    return tuple(lines)


def create_yellow_title_cover(frame: Path, output: Path, title: str) -> None:
    with Image.open(frame) as source:
        image = source.convert("RGB").resize((1280, 720), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(image)
    font = _font(54)
    lines = _cover_lines(draw, title, font)
    line_height = 68
    top = 365 - max(0, len(lines) - 1) * line_height // 2
    for index, line in enumerate(lines):
        draw.text(
            (64, top + index * line_height),
            line,
            font=font,
            fill="#FFE12A",
            stroke_width=8,
            stroke_fill="#050505",
        )
    image.save(output, format="JPEG", quality=94, optimize=True)


def choose_sharpest_cover_frame(frames: Iterable[Path]) -> Path:
    candidates = tuple(path for path in frames if path.is_file())
    if not candidates:
        raise ClipperError("没有提取到可用封面画面")

    def score(path: Path) -> float:
        with Image.open(path) as image:
            gray = image.convert("L").resize((320, 180), Image.Resampling.BILINEAR)
            edge_variance = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0]
            brightness = ImageStat.Stat(gray).mean[0]
            exposure_penalty = max(0.0, abs(brightness - 128) - 70) * 2
            return edge_variance - exposure_penalty

    return max(candidates, key=score)


def _parse_ffmpeg_time(value: str) -> float | None:
    match = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", value.strip())
    if not match:
        return None
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def _run_ffmpeg(
    command: list[str],
    cwd: Path,
    duration: float,
    progress: RenderProgressCallback | None,
) -> None:
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise ClipperError(f"无法启动 FFmpeg：{exc}") from exc
    output: list[str] = []
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.strip()
        if not line:
            continue
        output.append(line)
        if len(output) > 40:
            output.pop(0)
        if line.startswith("out_time=") and progress:
            elapsed = _parse_ffmpeg_time(line.partition("=")[2])
            if elapsed is not None and duration > 0:
                progress(min(99.0, elapsed / duration * 100), "正在编码竖屏成片")
    returncode = process.wait()
    if returncode:
        detail = "\n".join(output[-8:]) or f"退出码 {returncode}"
        raise ClipperError(f"FFmpeg 生成切片失败：\n{detail[:1200]}")


def _source_has_audio(ffmpeg: Path, source: Path) -> bool:
    completed = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    details = f"{completed.stdout}\n{completed.stderr}"
    return bool(re.search(r"Stream\s+#\S+.*Audio:", details, flags=re.IGNORECASE))


def _edited_filter_complex(
    candidate: ClipCandidate,
    subtitles_name: str,
    has_audio: bool,
    source_offset: float,
) -> tuple[str, str | None]:
    parts: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []
    for index, (start, end) in enumerate(candidate.timeline_ranges):
        relative_start = max(0.0, start - source_offset)
        relative_end = max(relative_start, end - source_offset)
        video_label = f"ev{index}"
        parts.append(
            f"[0:v:0]trim=start={relative_start:.3f}:end={relative_end:.3f},"
            f"setpts=PTS-STARTPTS[{video_label}]"
        )
        video_labels.append(f"[{video_label}]")
        if has_audio:
            audio_label = f"ea{index}"
            parts.append(
                f"[0:a:0]atrim=start={relative_start:.3f}:end={relative_end:.3f},"
                f"asetpts=PTS-STARTPTS[{audio_label}]"
            )
            audio_labels.append(f"[{audio_label}]")

    if len(video_labels) == 1:
        edited_video = video_labels[0]
    else:
        parts.append("".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0[editedv]")
        edited_video = "[editedv]"
    edited_audio: str | None = None
    if audio_labels:
        if len(audio_labels) == 1:
            edited_audio = audio_labels[0]
        else:
            parts.append("".join(audio_labels) + f"concat=n={len(audio_labels)}:v=0:a=1[editeda]")
            edited_audio = "[editeda]"

    parts.extend(
        [
            f"{edited_video}split=2[bg][fg]",
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=28[bg2]",
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fg2]",
            "[bg2][fg2]overlay=(W-w)/2:(H-h)/2[v0]",
            f"[v0]ass='{subtitles_name}'[v]",
        ]
    )
    return ";".join(parts), edited_audio


def _source_time_at_output_ratio(candidate: ClipCandidate, ratio: float) -> float:
    remaining = candidate.duration * max(0.0, min(1.0, ratio))
    for start, end in candidate.timeline_ranges:
        length = end - start
        if remaining <= length:
            return min(end - 0.05, start + remaining)
        remaining -= length
    return candidate.timeline_ranges[-1][1] - 0.05


def render_candidate(
    source: Path,
    candidate: ClipCandidate,
    cache_dir: Path,
    output_root: Path,
    progress: RenderProgressCallback | None = None,
) -> ClipRenderResult:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ClipperError(f"原始录像不存在：{source}")
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise ClipperError("找不到支持字幕的现代 FFmpeg")
    # Windows silently removes trailing spaces/dots from directory names.
    # If a long source name is truncated exactly after a space, the directory
    # created by mkdir and the later subtitle path no longer refer to the
    # same path (the latter still contains that space).  Trim again after
    # truncation so the .ass file is written where FFmpeg will find it.
    source_folder = _safe_filename(source.stem, "原始录像")[:48].rstrip(" .") or "原始录像"
    output_dir = output_root.expanduser().resolve() / source_folder
    output_dir.mkdir(parents=True, exist_ok=True)
    title_identity = hashlib.sha256(
        (
            f"{candidate.title}\n{candidate.cover_title or candidate.title}\n"
            f"{json.dumps(candidate.timeline_ranges, ensure_ascii=True)}"
        ).encode("utf-8")
    ).hexdigest()[:8]
    stem = f"{candidate.id}-{_safe_filename(candidate.title)}-{title_identity}"
    video = output_dir / f"{stem}.mp4"
    cover = output_dir / f"{stem}.jpg"
    subtitles = output_dir / f"{candidate.id}.ass"
    if video.is_file() and video.stat().st_size >= 1024 * 1024 and cover.is_file() and subtitles.is_file():
        if progress:
            progress(100.0, "已复用生成完成的候选成片")
        return ClipRenderResult(candidate, video, cover, subtitles, reused=True)

    segments = _load_transcript(cache_dir)
    write_candidate_subtitles(subtitles, candidate, segments)
    partial = output_dir / f"{stem}.partial.mp4"
    frames = tuple(output_dir / f"{candidate.id}.cover-frame-{index}.jpg" for index in range(1, 4))
    for temporary in (partial, *frames):
        if temporary.is_file():
            temporary.unlink()

    if progress:
        progress(0.0, "正在准备字幕和竖屏画面")
    source_offset = min(start for start, _end in candidate.timeline_ranges)
    source_end = max(end for _start, end in candidate.timeline_ranges)
    has_audio = _source_has_audio(ffmpeg, source)
    filter_complex, edited_audio = _edited_filter_complex(
        candidate,
        subtitles.name,
        has_audio,
        source_offset,
    )
    command = [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-ss",
        f"{source_offset:.3f}",
        "-t",
        f"{source_end - source_offset:.3f}",
        "-i",
        str(source),
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
    ]
    if edited_audio:
        command.extend(["-map", edited_audio])
    command.append(str(partial))
    try:
        _run_ffmpeg(command, output_dir, candidate.duration, progress)
        if not partial.is_file() or partial.stat().st_size < 1024 * 256:
            raise ClipperError("FFmpeg 未生成有效切片文件")
        partial.replace(video)
        if progress:
            progress(99.0, "正在生成黄色标题封面")
        extracted: list[Path] = []
        for ratio, frame in zip((0.22, 0.5, 0.78), frames):
            frame_at = _source_time_at_output_ratio(candidate, ratio)
            frame_command = [
                str(ffmpeg),
                "-y",
                "-hide_banner",
                "-ss",
                f"{frame_at:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
                str(frame),
            ]
            completed = subprocess.run(
                frame_command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode == 0 and frame.is_file():
                extracted.append(frame)
        best_frame = choose_sharpest_cover_frame(extracted)
        create_yellow_title_cover(best_frame, cover, candidate.cover_title or candidate.title)
    finally:
        if partial.is_file():
            partial.unlink()
        for frame in frames:
            if frame.is_file():
                frame.unlink()
    if progress:
        progress(100.0, "候选成片和黄色标题封面已生成")
    return ClipRenderResult(candidate, video, cover, subtitles)


def render_candidates(
    source: Path,
    candidates: Iterable[ClipCandidate],
    cache_dir: Path,
    output_root: Path,
    progress: RenderProgressCallback | None = None,
) -> tuple[ClipRenderResult, ...]:
    selected = tuple(candidates)
    if not selected:
        raise ClipperError("没有可以生成的候选切片")
    results: list[ClipRenderResult] = []
    total = len(selected)
    for index, candidate in enumerate(selected):
        def item_progress(value: float | None, message: str) -> None:
            if progress is None:
                return
            combined = None if value is None else (index + value / 100) / total * 100
            progress(combined, f"第 {index + 1}/{total} 条：{message}")

        results.append(render_candidate(source, candidate, cache_dir, output_root, item_progress))
    return tuple(results)
