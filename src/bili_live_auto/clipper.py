from __future__ import annotations

"""Offline transcript analysis used by the desktop client's clip-candidate page.

The main client deliberately does not import faster-whisper.  Its packaged
executable stays small, while the retained standalone Python environment does
the CPU transcription work in a subprocess.
"""

import hashlib
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable


ProgressCallback = Callable[[str], None]

PREFERRED_MIN_CLIP_SECONDS = 180.0
PREFERRED_MAX_CLIP_SECONDS = 300.0
MIN_CLIP_SECONDS = 150.0
MAX_CLIP_SECONDS = 360.0
TARGET_CLIP_SECONDS = 240.0
BOUNDARY_SEARCH_SECONDS = 24.0


class ClipperError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TopicMatch:
    label: str
    keywords: tuple[str, ...]
    evidence: str
    at_seconds: float


@dataclass(frozen=True)
class ClipCandidate:
    id: str
    start: float
    end: float
    score: float
    title: str
    topics: tuple[str, ...]
    evidence: str
    cover_title: str = ""
    signals: tuple[str, ...] = ()
    origin: str = "local"
    edit_ranges: tuple[tuple[float, float], ...] = ()

    @property
    def timeline_ranges(self) -> tuple[tuple[float, float], ...]:
        return self.edit_ranges or ((self.start, self.end),)

    @property
    def duration(self) -> float:
        return round(sum(max(0.0, end - start) for start, end in self.timeline_ranges), 2)

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["duration"] = round(self.duration, 2)
        return result


@dataclass(frozen=True)
class ClipAnalysis:
    source: Path
    duration: float
    cache_dir: Path
    transcript_from_cache: bool
    candidates: tuple[ClipCandidate, ...]
    candidate_source: str = "local"
    candidate_note: str = "本地规则分析"


@dataclass(frozen=True)
class _TopicRule:
    label: str
    keywords: tuple[str, ...]
    # A sensitive combined label is only used after enough distinct terms
    # appear in the same candidate context.
    min_distinct: int = 1


# These are discovery rules, not a list of claims to add into titles.  A label
# only reaches a title when the original transcript really contains its terms.
TOPIC_RULES = (
    # Specific people and phrases stay ahead of broad categories so a title
    # uses the most concrete topic that is actually present in the transcript.
    _TopicRule("户晨风", ("户晨风",)),
    _TopicRule("峰哥亡命天涯", ("峰哥亡命天涯", "峰哥", "亡命天涯"), 2),
    _TopicRule("擦边主播", ("擦边", "主播"), 2),
    _TopicRule("结婚", ("结婚", "婚姻", "离婚", "彩礼", "领证", "嫁人", "娶老婆", "娶媳妇")),
    _TopicRule("恋爱相亲", ("恋爱", "谈恋爱", "相亲", "对象", "分手", "男朋友", "女朋友", "谈对象")),
    _TopicRule("男女两性", ("男女", "两性", "男生", "女生", "男人", "女人", "男的", "女的", "夫妻"), 2),
    _TopicRule("失业", ("失业", "失业了", "裁员", "找不到工作", "没工作", "待业", "被开除")),
    _TopicRule("躺平", ("躺平", "摆烂", "不想努力", "不想上班")),
    _TopicRule("力工", ("力工", "体力活", "工地干活", "搬砖")),
    _TopicRule("流量", ("流量", "起号", "爆款", "涨粉", "掉粉", "网红", "热度")),
    _TopicRule("工作与职场", ("工作", "上班", "下班", "加班", "职场", "老板", "同事", "面试", "辞职", "招聘", "打工", "找工作", "行业")),
    _TopicRule("收入与赚钱", ("工资", "收入", "月薪", "年薪", "赚钱", "挣钱", "存款", "副业", "生意", "亏钱", "亏损")),
    _TopicRule("消费与房子", ("消费", "价格", "买不起", "房子", "房租", "房价", "买房", "租房", "房贷", "车贷", "省钱")),
    _TopicRule("家庭生活", ("父母", "家庭", "孩子", "生育", "带娃", "亲戚", "养老", "家里人")),
    _TopicRule("学历与教育", ("学历", "大学", "学校", "高考", "考研", "专业", "学生", "毕业", "读书")),
    _TopicRule("直播行业", ("直播", "主播", "弹幕", "粉丝", "礼物", "平台", "工会", "带货", "直播间")),
    _TopicRule("网络争议", ("网暴", "节奏", "争议", "带节奏", "喷子", "舆论", "骂我", "被骂")),
    _TopicRule("游戏体验", ("游戏", "玩家", "排位", "主机", "剧情", "操作", "版本", "匹配机制", "氪金", "通关")),
    _TopicRule("人际关系", ("朋友", "友情", "社交", "人情", "圈子", "关系", "背叛", "绝交")),
    _TopicRule("焦虑压力", ("焦虑", "压力", "内耗", "崩溃", "难受", "迷茫", "抑郁", "睡不着")),
    _TopicRule("社会现实", ("现实", "社会", "普通人", "年轻人", "底层", "阶层", "贫富", "生活成本")),
)

IMPACT_MARKERS = (
    "没想到",
    "竟然",
    "第一次",
    "后悔",
    "根本",
    "为什么",
    "现实",
    "离谱",
    "真相",
    "废物",
    "不可能",
    "看不起",
    "失望",
    "希望",
    "完全",
    "最",
)

STANCE_MARKERS = (
    "我觉得",
    "我认为",
    "我发现",
    "才发现",
    "在我看来",
    "说白了",
    "说实话",
    "其实",
    "根本",
    "必须",
    "应该",
    "不应该",
    "没必要",
    "不能",
    "绝对",
    "肯定",
)

QUESTION_MARKERS = ("为什么", "怎么会", "怎么可能", "凭什么", "难道", "到底", "什么道理", "你们觉得")
CONFLICT_MARKERS = ("但是", "可是", "反而", "结果却", "没想到", "不是", "而是", "看不起", "吵", "骂")
EMOTION_MARKERS = ("离谱", "气死", "破防", "崩溃", "笑死", "后悔", "难受", "害怕", "恶心", "无语", "震惊")
STORY_MARKERS = ("当时", "后来", "结果", "没想到", "第一次", "有一次", "最后", "原来")

_FILLER_ONLY = re.compile(r"^(嗯+|啊+|哦+|对+|是+|行+|好+|哈哈+|呵呵+|那个|这个|然后|就是|就是说)[呀啊吧吗呢的]*[。！？，, ]*$")
_NUMBER_SIGNAL = re.compile(
    r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万两]+)"
    r"(?:多)?(?:块|元|万|年|月|天|小时|分钟|岁|个|次|倍|%|％|的?(?:工资|收入|房租|房价))"
)


def format_timestamp(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def candidate_count_for_duration(duration: float) -> int:
    """Choose a reviewable count: about three candidates per hour, max twelve.

    This keeps a three-hour recording near nine API candidates while allowing
    the requested two-to-four-per-hour range to vary with the actual content.
    """
    if duration <= 0:
        return 1
    minutes = duration / 60
    if minutes < 12:
        return 1
    if minutes < 20:
        return 2
    return min(12, max(3, int(round(duration / 3600 * 3))))


def find_model_directory(base: Path) -> Path | None:
    roots = (
        base / "models" / "faster-whisper-small",
        Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
        / "hub"
        / "models--Systran--faster-whisper-small",
    )
    for root in roots:
        if (root / "model.bin").is_file():
            return root
        snapshots = sorted(root.glob("snapshots/*/model.bin"), key=lambda item: item.stat().st_mtime, reverse=True)
        if snapshots:
            return snapshots[0].parent
    return None


def find_ffmpeg() -> Path | None:
    candidates = (
        Path(sys.executable).resolve().parent / "ffmpeg.exe",
        Path(os.environ.get("APPDATA", ""))
        / "Python"
        / "Python312"
        / "site-packages"
        / "imageio_ffmpeg"
        / "binaries"
        / "ffmpeg-win-x86_64-v7.1.exe",
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            # A managed Windows account may expose APPDATA while denying the
            # packaged client access to one of its subdirectories.
            continue
    from shutil import which

    located = which("ffmpeg")
    return Path(located) if located else None


def probe_duration(source: Path, ffmpeg: Path | None = None) -> float:
    executable = ffmpeg or find_ffmpeg()
    if executable is None:
        raise ClipperError("找不到 FFmpeg，无法读取录像时长")
    try:
        result = subprocess.run(
            [str(executable), "-hide_banner", "-i", str(source)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClipperError(f"读取录像时长失败：{exc}") from exc
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr + "\n" + result.stdout)
    if not match:
        raise ClipperError("无法从录像中读取时长，请确认文件已完成录制且可以播放")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def _source_fingerprint(source: Path) -> str:
    stat = source.stat()
    content = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(content).hexdigest()[:24]


def source_collection_fingerprint(sources: Iterable[Path]) -> str:
    """Stable cache key for an ordered set of split live-recording files."""
    parts: list[str] = []
    for source in sources:
        path = source.expanduser().resolve()
        stat = path.stat()
        parts.append(f"{path}|{stat.st_size}|{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:24]


def merge_sources(
    sources: Iterable[Path],
    cache_root: Path,
    progress: ProgressCallback | None = None,
) -> Path:
    """Create (or reuse) a local joined file for several recording parts.

    BililiveRecorder normally writes parts with compatible streams, so the
    first attempt uses stream-copy and is very fast.  A normalized encode is a
    safe fallback for a part whose codec/container parameters differ.  The
    merged file stays in the cache and is fingerprinted by the ordered source
    list, so repeated analysis does not join the same parts again.
    """
    ordered = tuple(path.expanduser().resolve() for path in sources)
    if not ordered:
        raise ClipperError("至少选择一个原始录像")
    for source in ordered:
        if not source.is_file():
            raise ClipperError(f"原始录像不存在：{source}")
    if len(ordered) == 1:
        return ordered[0]
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise ClipperError("找不到 FFmpeg，无法合并多个直播分P")
    fingerprint = source_collection_fingerprint(ordered)
    directory = cache_root.expanduser().resolve() / "multi_sources" / fingerprint
    directory.mkdir(parents=True, exist_ok=True)
    # Include the collection fingerprint in the filename too: the renderer
    # uses the source stem as its output subdirectory, so different multi-part
    # sessions must never share a generic ``merged`` folder.
    merged = directory / f"multi-{fingerprint}.mp4"
    if merged.is_file() and merged.stat().st_size >= 256 * 1024:
        if progress:
            progress(f"已复用 {len(ordered)} 个分P的合并缓存")
        return merged
    concat_file = directory / "sources.txt"
    lines = []
    for source in ordered:
        escaped = str(source).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    partial = directory / "merged.partial.mp4"
    if partial.is_file():
        partial.unlink()
    if progress:
        progress(f"正在合并 {len(ordered)} 个直播分P（优先无损拼接）")
    copy_command = [
        str(ffmpeg), "-y", "-hide_banner", "-f", "concat", "-safe", "0",
        "-i", str(concat_file), "-c", "copy", "-movflags", "+faststart", str(partial),
    ]
    copied = subprocess.run(
        copy_command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=1800,
    )
    if copied.returncode or not partial.is_file() or partial.stat().st_size < 256 * 1024:
        if partial.is_file():
            partial.unlink()
        if progress:
            progress("分P编码参数不同，正在使用兼容模式合并（只需首次执行）")
        encode_command = [
            str(ffmpeg), "-y", "-hide_banner", "-f", "concat", "-safe", "0",
            "-i", str(concat_file), "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(partial),
        ]
        encoded = subprocess.run(
            encode_command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=3600,
        )
        if encoded.returncode:
            detail = (encoded.stderr or encoded.stdout).strip()[-1200:]
            raise ClipperError(f"多个分P合并失败：{detail}")
    if not partial.is_file() or partial.stat().st_size < 256 * 1024:
        raise ClipperError("多个分P合并后没有生成有效视频")
    partial.replace(merged)
    (directory / "manifest.json").write_text(
        json.dumps({"sources": [str(path) for path in ordered]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return merged


def cache_directory(cache_root: Path, source: Path) -> Path:
    return cache_root / _source_fingerprint(source)


def _segments_from_payload(payload: object) -> tuple[TranscriptSegment, ...]:
    if not isinstance(payload, list):
        return ()
    segments: list[TranscriptSegment] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
            text = str(item["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if text and end > start:
            segments.append(TranscriptSegment(start=start, end=end, text=text))
    return tuple(segments)


def _load_cached_transcript(path: Path, source: Path) -> tuple[TranscriptSegment, ...] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("fingerprint") != _source_fingerprint(source):
        return None
    segments = _segments_from_payload(data.get("segments"))
    return segments or None


_TRANSCRIBE_PROGRAM = r'''
import json
import os
import sys
from faster_whisper import WhisperModel

payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
model_path = payload["model"]

# Keep the accuracy-sensitive decoding settings unchanged, but use available
# hardware more effectively.  A CUDA build is preferred when it is actually
# available; otherwise CTranslate2 gets all but one logical CPU thread.  If a
# packaged CUDA runtime is incomplete, initialization falls back to the same
# CPU/int8 model used by older builds instead of failing the whole analysis.
cpu_threads = max(1, int(payload.get("cpu_threads") or max(1, min(16, (os.cpu_count() or 4) - 1))))
device = "cpu"
compute_type = "int8"
try:
    import ctranslate2
    if ctranslate2.get_cuda_device_count() > 0:
        device = "cuda"
        compute_type = "float16"
except Exception:
    pass
try:
    model = WhisperModel(
        model_path,
        device=device,
        compute_type=compute_type,
        cpu_threads=cpu_threads,
        num_workers=1,
    )
except Exception:
    if device != "cpu":
        device = "cpu"
        compute_type = "int8"
        model = WhisperModel(
            model_path,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            num_workers=1,
        )
    else:
        raise
segments, info = model.transcribe(
    payload["source"],
    beam_size=5,
    vad_filter=True,
    condition_on_previous_text=True,
)
result = []
for segment in segments:
    text = (segment.text or "").strip()
    if text:
        result.append({"start": round(float(segment.start), 3), "end": round(float(segment.end), 3), "text": text})
response = json.dumps({"language": info.language, "segments": result}, ensure_ascii=False)
sys.stdout.buffer.write(response.encode("utf-8"))
'''


def _transcription_request_payload(source: Path, model: Path) -> str:
    # One thread is deliberately left for the GUI/FFmpeg process.  This does
    # not alter the model, beam search, VAD, or context settings, so subtitle
    # accuracy is preserved while CPU inference is substantially less idle.
    cpu_count = os.cpu_count() or 4
    cpu_threads = max(1, min(16, cpu_count - 1))
    return json.dumps(
        {"source": str(source), "model": str(model), "cpu_threads": cpu_threads},
        ensure_ascii=True,
    )


def _transcribe(
    source: Path,
    python: Path,
    model: Path,
    progress: ProgressCallback | None,
) -> tuple[TranscriptSegment, ...]:
    if progress:
        cpu_count = os.cpu_count() or 4
        thread_count = max(1, min(16, cpu_count - 1))
        progress(
            "正在使用本地 Faster-Whisper small 转写（保持 beam_size=5；自动使用 GPU 或 "
            f"CPU {thread_count} 线程），不会降低字幕识别设置"
        )
    # Keep the request itself ASCII-only as an extra guard against Windows
    # console code pages; the child still reads the bytes explicitly as UTF-8.
    payload = _transcription_request_payload(source, model)
    try:
        process = subprocess.run(
            [str(python), "-c", _TRANSCRIBE_PROGRAM],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(source.parent),
            env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise ClipperError(f"无法启动本地切片环境：{exc}") from exc
    if process.returncode:
        detail = (process.stderr or process.stdout).strip().splitlines()
        hint = detail[-1] if detail else f"退出码 {process.returncode}"
        raise ClipperError(f"本地转写失败：{hint[:360]}")
    try:
        output = json.loads(process.stdout)
    except ValueError as exc:
        raise ClipperError("本地转写返回格式异常") from exc
    segments = _segments_from_payload(output.get("segments"))
    if not segments:
        raise ClipperError("没有识别到有效语音，未生成候选切片")
    return segments


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _marker_count(value: str, markers: Iterable[str]) -> int:
    normalized = _normalize_text(value)
    return sum(_normalize_text(marker) in normalized for marker in markers)


def _segment_interest_score(segment: TranscriptSegment) -> float:
    """Estimate whether a subtitle carries a hook, opinion, or story beat.

    This deliberately scores language structure as well as topics.  A strong
    opinion or surprising story can therefore become a candidate even when it
    does not contain one of the configured topic words.
    """
    text = segment.text.strip()
    if not text or _FILLER_ONLY.fullmatch(text):
        return -12.0
    score = min(5.0, len(text) / 8.0)
    score += _marker_count(text, STANCE_MARKERS) * 4.5
    score += _marker_count(text, QUESTION_MARKERS) * 5.0
    score += _marker_count(text, CONFLICT_MARKERS) * 3.5
    score += _marker_count(text, EMOTION_MARKERS) * 4.5
    score += _marker_count(text, STORY_MARKERS) * 3.0
    score += _marker_count(text, IMPACT_MARKERS) * 1.8
    if "？" in text or "?" in text:
        score += 2.5
    if "！" in text or "!" in text:
        score += 1.5
    if _NUMBER_SIGNAL.search(text):
        score += 3.5
    if 12 <= len(text) <= 70:
        score += 2.0
    return score


def _interest_labels(value: str) -> tuple[str, ...]:
    labels: list[str] = []
    if _marker_count(value, STANCE_MARKERS):
        labels.append("明确观点")
    if _marker_count(value, QUESTION_MARKERS) or "？" in value or "?" in value:
        labels.append("反问追问")
    if _marker_count(value, CONFLICT_MARKERS):
        labels.append("冲突转折")
    if _marker_count(value, EMOTION_MARKERS):
        labels.append("强烈情绪")
    if _marker_count(value, STORY_MARKERS):
        labels.append("故事经历")
    if _NUMBER_SIGNAL.search(value):
        labels.append("数字信息")
    return tuple(labels)


def _matched_topics(segments: Iterable[TranscriptSegment]) -> tuple[TopicMatch, ...]:
    source = tuple(segments)
    combined = _normalize_text("".join(segment.text for segment in source))
    result: list[TopicMatch] = []
    for rule in TOPIC_RULES:
        matched = tuple(keyword for keyword in rule.keywords if _normalize_text(keyword) in combined)
        exact_combined = rule.label in {"男女两性", "擦边主播"} and any(
            _normalize_text(phrase) in combined
            for phrase in (("男女", "两性") if rule.label == "男女两性" else ("擦边主播",))
        )
        if len(set(matched)) < rule.min_distinct and not exact_combined:
            continue
        relevant = [
            segment
            for segment in source
            if any(_normalize_text(keyword) in _normalize_text(segment.text) for keyword in matched)
        ]
        if not relevant:
            continue
        evidence_segment = max(
            relevant,
            key=lambda segment: (
                sum(_normalize_text(keyword) in _normalize_text(segment.text) for keyword in matched),
                sum(marker in segment.text for marker in IMPACT_MARKERS),
                len(segment.text),
            ),
        )
        evidence = evidence_segment.text
        if rule.min_distinct > 1 and len(
            {keyword for keyword in matched if _normalize_text(keyword) in _normalize_text(evidence)}
        ) < rule.min_distinct:
            extra = next((item.text for item in relevant if item is not evidence_segment), "")
            if extra:
                evidence = f"{evidence}；{extra}"
        result.append(TopicMatch(rule.label, matched, evidence, min(item.start for item in relevant)))
    return tuple(result)


def _best_evidence(
    segments: tuple[TranscriptSegment, ...], topics: tuple[TopicMatch, ...] = ()
) -> str:
    if not segments:
        return "无可用字幕证据"
    topic_keywords = tuple(keyword for topic in topics for keyword in topic.keywords)

    def evidence_score(item: TranscriptSegment) -> float:
        topic_hits = sum(
            _normalize_text(keyword) in _normalize_text(item.text) for keyword in topic_keywords
        )
        return _segment_interest_score(item) + topic_hits * 6.0

    ranked = sorted(range(len(segments)), key=lambda index: evidence_score(segments[index]), reverse=True)
    best_index = ranked[0]
    chosen = {best_index}
    neighbors: list[tuple[float, int]] = []
    if best_index > 0 and segments[best_index].start - segments[best_index - 1].end <= 18:
        neighbors.append((evidence_score(segments[best_index - 1]), best_index - 1))
    if best_index + 1 < len(segments) and segments[best_index + 1].start - segments[best_index].end <= 18:
        neighbors.append((evidence_score(segments[best_index + 1]), best_index + 1))
    if neighbors:
        neighbor_score, neighbor_index = max(neighbors)
        combined_length = len(segments[best_index].text) + len(segments[neighbor_index].text)
        if neighbor_score >= 4.5 and combined_length <= 160:
            chosen.add(neighbor_index)
    return "；".join(segments[index].text.strip() for index in sorted(chosen))[:180]


def _topic_anchor_hits(segments: tuple[TranscriptSegment, ...]) -> tuple[TopicMatch, ...]:
    result: list[TopicMatch] = []
    seen: set[tuple[str, int]] = set()
    for index, segment in enumerate(segments):
        nearby = [segment]
        if index and segment.start - segments[index - 1].end <= 20:
            nearby.insert(0, segments[index - 1])
        if index + 1 < len(segments) and segments[index + 1].start - segment.end <= 20:
            nearby.append(segments[index + 1])
        for match in _matched_topics(nearby):
            key = (match.label, int(match.at_seconds // 75))
            if key not in seen:
                seen.add(key)
                result.append(match)
    return tuple(result)


def _interest_anchor_hits(segments: tuple[TranscriptSegment, ...]) -> tuple[tuple[float, float], ...]:
    """Return high-information speech anchors, de-duplicated in short buckets."""
    buckets: dict[int, tuple[float, float]] = {}
    for segment in segments:
        score = _segment_interest_score(segment)
        if score < 8.0:
            continue
        bucket = int(segment.start // 45)
        previous = buckets.get(bucket)
        if previous is None or score > previous[1]:
            buckets[bucket] = (segment.start, score)
    return tuple(sorted(buckets.values(), key=lambda item: (-item[1], item[0])))


def _candidate_window(anchor: float, duration: float, desired: float) -> tuple[float, float]:
    if duration <= 0:
        return 0.0, 0.0
    if duration < PREFERRED_MIN_CLIP_SECONDS:
        desired = duration
    else:
        desired = min(PREFERRED_MAX_CLIP_SECONDS, max(PREFERRED_MIN_CLIP_SECONDS, desired))
    start = max(0.0, anchor - desired * 0.34)
    end = min(duration, start + desired)
    start = max(0.0, end - desired)
    return round(start, 2), round(end, 2)


def _refine_window_to_speech_boundaries(
    start: float,
    end: float,
    segments: tuple[TranscriptSegment, ...],
    anchor: float,
    duration: float,
) -> tuple[float, float]:
    """Nudge a coarse window to pauses and completed subtitle sentences.

    The reference editing style keeps a complete discussion instead of cutting
    it into scattered sound bites.  Subtitle pauses are therefore used as a
    safe local boundary signal.  Three to five minutes is preferred, while a
    natural beginning or ending may move the result to 2.5--6 minutes.
    """

    if not segments or duration <= 0:
        return round(start, 2), round(end, 2)
    minimum = min(MIN_CLIP_SECONDS, duration)
    maximum = min(MAX_CLIP_SECONDS, duration)
    start_options: list[tuple[float, float]] = [(start, 0.0)]
    end_options: list[tuple[float, float]] = [(end, 0.0)]
    terminal = "。！？!?；"
    for index, segment in enumerate(segments):
        if abs(segment.start - start) <= BOUNDARY_SEARCH_SECONDS:
            previous = segments[index - 1] if index else None
            pause = segment.start - previous.end if previous is not None else 8.0
            sentence_bonus = 3.0 if previous is not None and previous.text.rstrip().endswith(tuple(terminal)) else 0.0
            score = min(10.0, max(0.0, pause)) * 1.4 + sentence_bonus - abs(segment.start - start) * 0.18
            start_options.append((max(0.0, segment.start), score))
        if abs(segment.end - end) <= BOUNDARY_SEARCH_SECONDS:
            following = segments[index + 1] if index + 1 < len(segments) else None
            pause = following.start - segment.end if following is not None else 8.0
            sentence_bonus = 3.0 if segment.text.rstrip().endswith(tuple(terminal)) else 0.0
            score = min(10.0, max(0.0, pause)) * 1.4 + sentence_bonus - abs(segment.end - end) * 0.18
            end_options.append((min(duration, segment.end), score))

    best = (start, end)
    best_score = 0.0
    for candidate_start, start_score in start_options:
        for candidate_end, end_score in end_options:
            candidate_duration = candidate_end - candidate_start
            if candidate_start > anchor or candidate_end < anchor:
                continue
            if candidate_duration < minimum - 0.5 or candidate_duration > maximum + 0.5:
                continue
            score = start_score + end_score
            if score > best_score:
                best = (candidate_start, candidate_end)
                best_score = score
    return round(best[0], 2), round(best[1], 2)


def _headline_excerpt(value: str) -> str:
    clauses = [item.strip(" ，。！？!?；：:") for item in re.split(r"[。！？!?；\n]", value) if item.strip()]
    if not clauses:
        clauses = [value.strip()]
    cleaned = [
        re.sub(r"^(然后|就是说|就是|那个|这个)+", "", clause).strip()
        for clause in clauses
    ]
    cleaned = [clause for clause in cleaned if clause]
    if not cleaned:
        return value.strip()[:56]

    def clause_score(clause: str) -> tuple[float, bool, int]:
        signal = _segment_interest_score(TranscriptSegment(0, 1, clause))
        return signal, 10 <= len(clause) <= 42, min(len(clause), 42)

    excerpt = max(cleaned, key=clause_score)
    secondary = [clause for clause in cleaned if clause != excerpt]
    if secondary and len(excerpt) < 38:
        addition = max(secondary, key=clause_score)
        if clause_score(addition)[0] >= 4.0 and len(excerpt) + len(addition) <= 56:
            excerpt = f"{excerpt}，{addition}"
    return excerpt[:58]


def _candidate_title(topics: tuple[TopicMatch, ...], evidence: str, streamer: str = "") -> str:
    excerpt = _headline_excerpt(evidence)
    subject = streamer.strip()[:18] or "主播"
    asks_question = bool(_marker_count(evidence, QUESTION_MARKERS) or "？" in evidence or "?" in evidence)
    has_stance = bool(_marker_count(evidence, STANCE_MARKERS))
    tells_story = bool(_marker_count(evidence, STORY_MARKERS))
    has_conflict = bool(_marker_count(evidence, CONFLICT_MARKERS) or _marker_count(evidence, EMOTION_MARKERS))
    has_numbers = bool(_NUMBER_SIGNAL.search(evidence))
    if topics:
        topic = topics[0].label
        if asks_question:
            lead = f"{subject}追问{topic}"
        elif has_stance:
            lead = f"{subject}直言{topic}"
        elif tells_story:
            lead = f"{subject}讲起{topic}经历"
        elif has_conflict:
            lead = f"{subject}谈{topic}争议"
        elif has_numbers:
            lead = f"{subject}谈{topic}直接报出数字"
        else:
            lead = f"{subject}聊到{topic}"
        title = f"{lead}：{excerpt}"
    else:
        if asks_question:
            title = f"{subject}一句反问直指问题：{excerpt}"
        elif has_stance:
            title = f"{subject}观点很直接：{excerpt}"
        elif tells_story:
            title = f"{subject}讲起一段经历：{excerpt}"
        elif has_conflict:
            title = f"{subject}谈到争议点：{excerpt}"
        elif has_numbers:
            title = f"{subject}直接报出一组真实数字：{excerpt}"
        else:
            title = f"{subject}聊到这个问题：{excerpt}"
    title = title.strip(" ：:") or "直播高能切片"
    if title[-1] not in "！？!?。":
        title += "？" if asks_question else "！"
    return title[:80]


def _candidate_cover_title(title: str, streamer: str = "") -> str:
    """Create a shorter editable yellow-cover headline from a grounded title."""

    clean = " ".join(title.replace("\r", " ").replace("\n", " ").split()).strip(" ：:！!。")
    if streamer and clean.startswith(streamer):
        clean = clean[len(streamer) :].lstrip(" ：:")
    if "：" in clean or ":" in clean:
        parts = [item.strip(" ：:") for item in re.split(r"[：:]", clean, maxsplit=1)]
        clean = parts[-1] or parts[0]
    return (clean or "直播高能观点")[:34]


def _make_candidate(
    start: float,
    end: float,
    segments: tuple[TranscriptSegment, ...],
    ordinal: int,
    streamer: str = "",
    anchor: float | None = None,
) -> ClipCandidate:
    within = tuple(segment for segment in segments if segment.end >= start and segment.start <= end)
    focus_anchor = anchor if anchor is not None else (start + end) / 2
    focused = tuple(
        segment
        for segment in within
        if segment.end >= max(start, focus_anchor - 55) and segment.start <= min(end, focus_anchor + 75)
    )
    if not focused:
        focused = within
    nearby_topics = _matched_topics(focused)
    evidence = _best_evidence(focused)
    evidence_segment = TranscriptSegment(focus_anchor, focus_anchor + 1, evidence)
    topics = _matched_topics((evidence_segment,))
    # Plain speech sometimes needs a topic term to be useful (especially a
    # combined label spread across adjacent subtitles).  Strong opinions and
    # story hooks keep their independently selected evidence instead.
    if nearby_topics and _segment_interest_score(evidence_segment) < 8.0:
        topic_evidence = _best_evidence(focused, nearby_topics)
        topic_segment = TranscriptSegment(focus_anchor, focus_anchor + 1, topic_evidence)
        topic_matches = _matched_topics((topic_segment,))
        if topic_matches:
            evidence = topic_evidence
            evidence_segment = topic_segment
            topics = topic_matches
    unique_topics = tuple(dict.fromkeys(item.label for item in topics))
    speech_chars = sum(len(segment.text.strip()) for segment in within)
    interest_scores = tuple(max(0.0, _segment_interest_score(segment)) for segment in focused)
    best_interest = max(interest_scores, default=0.0)
    interest_total = sum(max(0.0, score - 4.0) for score in interest_scores)
    topic_strength = sum(min(3, len(topic.keywords)) for topic in topics)
    score = min(
        99.0,
        round(
            22
            + min(24, speech_chars / 26)
            + min(25, topic_strength * 4.5)
            + min(28, best_interest * 1.1 + interest_total * 0.12),
            1,
        ),
    )
    title = _candidate_title(topics, evidence, streamer)
    return ClipCandidate(
        id=f"c{ordinal:02d}-{int(start):06d}",
        start=round(start, 2),
        end=round(end, 2),
        score=score,
        title=title,
        topics=unique_topics,
        evidence=evidence[:180],
        cover_title=_candidate_cover_title(title, streamer),
        signals=_interest_labels(evidence),
    )


def generate_candidates(
    segments: Iterable[TranscriptSegment], duration: float, count: int | None = None, streamer: str = ""
) -> tuple[ClipCandidate, ...]:
    """Rank evidence-backed, mostly non-overlapping review candidates."""
    source_segments = tuple(sorted(segments, key=lambda segment: segment.start))
    if duration <= 0:
        return ()
    desired_count = count or candidate_count_for_duration(duration)
    desired_count = max(1, desired_count)
    # Three to five minutes is the preferred range.  Boundary refinement may
    # float to 2.5--6 minutes when that preserves a complete discussion.
    desired_length = min(
        PREFERRED_MAX_CLIP_SECONDS,
        max(PREFERRED_MIN_CLIP_SECONDS, duration / max(desired_count * 3.0, 1.0)),
    )
    topic_hits = _topic_anchor_hits(source_segments)
    interest_hits = _interest_anchor_hits(source_segments)
    weighted_anchors = [
        (
            hit.at_seconds,
            18.0
            + len(hit.keywords) * 3.0
            + _segment_interest_score(TranscriptSegment(hit.at_seconds, hit.at_seconds + 1, hit.evidence)),
        )
        for hit in topic_hits
    ]
    weighted_anchors.extend(interest_hits)
    weighted_anchors.sort(key=lambda item: (-item[1], item[0]))
    # Strong semantic hooks arrive first, then evenly spaced anchors fill any
    # quiet areas.  De-duplication prevents one discussion filling all slots.
    anchors = [anchor for anchor, _score in weighted_anchors]
    anchors.extend(duration * (index + 0.5) / desired_count for index in range(desired_count))

    candidates: list[ClipCandidate] = []
    for anchor in anchors:
        start, end = _candidate_window(anchor, duration, desired_length)
        start, end = _refine_window_to_speech_boundaries(
            start,
            end,
            source_segments,
            anchor,
            duration,
        )
        if any(abs(start - item.start) < min(75.0, desired_length * 0.45) for item in candidates):
            continue
        candidates.append(_make_candidate(start, end, source_segments, len(candidates) + 1, streamer, anchor))

    candidates.sort(key=lambda item: (-item.score, item.start))
    selected: list[ClipCandidate] = []
    for candidate in candidates:
        # Keep at most a brief edge overlap so the batch does not contain the
        # same conversation repeatedly.
        overlap = max(
            (max(0.0, min(candidate.end, kept.end) - max(candidate.start, kept.start)) for kept in selected),
            default=0.0,
        )
        if overlap > min(45.0, candidate.duration * 0.3):
            continue
        selected.append(candidate)
        if len(selected) >= desired_count:
            break
    if len(selected) < desired_count:
        for candidate in candidates:
            if candidate not in selected:
                selected.append(candidate)
                if len(selected) >= desired_count:
                    break
    return tuple(sorted(selected, key=lambda item: item.start))


def analyze_video(
    source: Path,
    cache_root: Path,
    python: Path,
    model: Path,
    progress: ProgressCallback | None = None,
    streamer: str = "",
) -> ClipAnalysis:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ClipperError(f"原始录像不存在：{source}")
    if progress:
        progress("正在读取录像时长……")
    duration = probe_duration(source)
    directory = cache_directory(cache_root, source)
    transcript_file = directory / "transcript.json"
    segments = _load_cached_transcript(transcript_file, source)
    from_cache = segments is not None
    if segments is None:
        segments = _transcribe(source, python, model, progress)
        directory.mkdir(parents=True, exist_ok=True)
        transcript_file.write_text(
            json.dumps(
                {
                    "source": str(source),
                    "fingerprint": _source_fingerprint(source),
                    "duration": duration,
                    "segments": [asdict(segment) for segment in segments],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    elif progress:
        progress("已复用这条录像的本地转写缓存，正在生成候选……")
    candidates = generate_candidates(segments, duration, streamer=streamer)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "candidates.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "duration": duration,
                "target_count": candidate_count_for_duration(duration),
                "candidates": [candidate.as_dict() for candidate in candidates],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if progress:
        progress(f"分析完成：已生成 {len(candidates)} 个候选，先审核标题和命中依据再生成成片")
    return ClipAnalysis(source, duration, directory, from_cache, candidates)
