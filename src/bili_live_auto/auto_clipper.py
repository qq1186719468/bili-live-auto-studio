from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from .clip_ai import enhance_analysis_with_fallback, read_api_key
from .clip_renderer import ClipRenderResult, render_candidates
from .clipper import (
    ClipperError,
    analyze_video,
    find_model_directory,
    merge_sources,
)
from .config import Config
from .models import Recording

LOGGER = logging.getLogger(__name__)
AutoClipProgress = Callable[[str], None]


def _progress(callback: AutoClipProgress | None, message: str) -> None:
    if callback is not None:
        callback(message)
    LOGGER.info("自动切片：%s", message)


def generate_after_live_upload(
    recording: Recording,
    config: Config,
    progress: AutoClipProgress | None = None,
) -> tuple[ClipRenderResult, ...]:
    """Analyze and render a finished live session without submitting clips.

    This is intentionally separate from ``Uploader``: the live upload must
    finish first, while generated clips are only placed in the ledger as
    pending manual submissions.
    """

    if not config.clip_ai.auto_after_live_upload:
        return ()
    sources = tuple(Path(value).expanduser().resolve() for value in recording.files)
    missing = [path for path in sources if not path.is_file()]
    if missing:
        raise ClipperError(f"自动切片找不到录像文件：{missing[0]}")
    base = config.source_path.parent
    python = base / ".clip-venv-standalone" / "Scripts" / "python.exe"
    model = find_model_directory(base)
    if not python.is_file():
        raise ClipperError(f"自动切片找不到本地 Python 环境：{python}")
    if model is None:
        raise ClipperError("自动切片找不到 Faster-Whisper small 模型")

    cache_root = config.app.work_dir / "data" / "clip_cache"
    output_root = config.app.work_dir / "智能切片成片"
    _progress(progress, f"准备分析本场直播（{len(sources)} 个分P）")
    source = merge_sources(sources, cache_root, lambda message: _progress(progress, message))
    analysis = analyze_video(
        source,
        cache_root,
        python,
        model,
        lambda message: _progress(progress, message),
        streamer=recording.streamer,
    )
    if config.clip_ai.enabled:
        api_key = read_api_key(config.clip_ai.api_key_file)
        analysis = enhance_analysis_with_fallback(
            analysis,
            config.clip_ai,
            api_key,
            lambda message: _progress(progress, message),
            streamer=recording.streamer,
        )
    if not analysis.candidates:
        raise ClipperError("自动切片没有生成通过核验的候选；请检查 API 或转写设置")
    _progress(progress, f"候选核验完成，共 {len(analysis.candidates)} 条，开始生成成片")
    return render_candidates(
        source,
        analysis.candidates,
        analysis.cache_dir,
        output_root,
        lambda value, message: _progress(
            progress,
            f"{message}{f'（{value:.0f}%）' if value is not None else ''}",
        ),
    )
