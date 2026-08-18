from __future__ import annotations

"""Optional OpenAI-compatible semantic enhancement for clip candidates.

Only timestamped transcript segments are sent to the configured endpoint.  A
video path, video bytes, cache path, and streamer identity never enter the API
request.  Every returned candidate is rebuilt from locally verified segment
IDs before it can appear in the desktop client.
"""

import hashlib
import json
import math
import re
import ssl
import sqlite3
import tomllib
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

import certifi

from .clipper import (
    ClipAnalysis,
    ClipCandidate,
    MAX_CLIP_SECONDS,
    MIN_CLIP_SECONDS,
    TranscriptSegment,
    _interest_labels,
    _matched_topics,
    _segments_from_payload,
    candidate_count_for_duration,
    format_timestamp,
)
from .config import ClipAISettings


# Bump this when candidate count policy changes so stale API caches are not
# silently reused with a different target count.
PROMPT_VERSION = "clip-reference-style-api-edl-tolerant-v7-20260818-3perhour"
ApiSender = Callable[[str, dict[str, object], str, float], dict[str, object]]
ProgressCallback = Callable[[str], None]


class ClipAIError(RuntimeError):
    pass


@dataclass(frozen=True)
class CCSwitchProvider:
    name: str
    base_url: str
    model: str
    protocol: str
    api_key: str


@dataclass(frozen=True)
class TranscriptChunk:
    index: int
    items: tuple[tuple[str, TranscriptSegment], ...]

    @property
    def start(self) -> float:
        return self.items[0][1].start

    @property
    def end(self) -> float:
        return self.items[-1][1].end


_SYSTEM_PROMPT = """你是中文直播切片编辑。任务是从带全局时间戳和唯一 ID 的字幕中选出观点明确、冲突明显、故事完整或信息密度高的候选片段。

剪辑风格：
- 每条只讲一个完整主题，保留必要的起因、铺垫、核心观点和结论；不要把相隔很远的金句拼成碎片。
- 优先选择能引起讨论的真实内容，例如明确立场、反常识反问、亲历故事、冲突转折和具体数字，但热门话题必须确实出现在字幕里。
- 尽量避开开场调试、欢迎观众、感谢礼物、唱歌、长段重复口头语和与主题无关的插话；开始和结束都放在完整句子或自然停顿处。
- 标题参考“问题/冲突 + 主播的明确结论”结构，可以有力度、有讨论性、稍长一些；不要只做宽泛概括，也不要编造观点。
- cover_title 是封面上的黄色短标题，比视频 title 更精炼，必须仍是字幕里的真实观点。
- 先确定 title 的唯一主题，再设计 edit_segments。只保留能推进这个主题的高信息量内容，删除跑题、重复、长停顿、无意义口头语和不连贯插话。
- edit_segments 按最终播放顺序排列；允许在确有必要时倒序或插叙，但必须让起因、观点、例子和结论仍然听得懂。不要为了炫技改变顺序。

必须遵守：
1. 只能依据输入字幕，禁止添加字幕里没有的人名、事件、观点、数字和热门话题。
2. 每条使用 1 到 6 个 edit_segments；每段至少 12 秒，start/end 必须取自输入字幕行的起止边界。段落之间不要重叠，同一内容不要重复使用。
3. 所有 edit_segments 拼接后的成片优先为 180 到 300 秒（3 到 5 分钟）；为了保住自然开头或完整结论，可在 150 到 360 秒（2.5 到 6 分钟）内适量浮动。
4. 每个 edit_segment 都必须包含至少一个 evidence_ids 指向的字幕，证明这一段与标题主题有关；evidence_ids 必须来自输入，共选 1 到 6 条。
5. 切点不能落在半句话中，拼接后不能突然缺少主语、前因或结论；最多使用必要的少量切点，避免过碎和过多停顿影响观感。
6. title 不写主播姓名、不写时间戳，长度建议 24 到 76 个中文字符；cover_title 不写主播姓名、不写时间戳，建议 8 到 28 个中文字符。二者都必须表达字幕中的明确观点。
7. 同一事件不要重复，宁缺毋滥。score 要同时考虑标题贴合度、信息密度、叙事完整度和剪后连贯性。
8. 只返回 JSON 对象，不要 Markdown、解释或代码围栏。

JSON 格式：
{"candidates":[{"edit_segments":[{"start":120.0,"end":210.0,"reason":"交代问题"},{"start":260.0,"end":410.0,"reason":"核心观点和结论"}],"score":88,"title":"为什么努力工作仍然没有生活？主播直言真正消耗人的不是忙，而是失去选择","cover_title":"忙不可怕，失去选择才可怕","topic":"职场与收入","evidence_ids":["s000001","s000002"],"reason":"删去跑题内容后观点集中且结论完整"}]}"""

_STRICT_CLAIMS = (
    "结婚",
    "离婚",
    "彩礼",
    "出轨",
    "怀孕",
    "失业",
    "裁员",
    "被裁",
    "开除",
    "躺平",
    "擦边",
    "户晨风",
    "峰哥亡命天涯",
    "犯罪",
    "吸毒",
    "自杀",
    "死亡",
    "欠债",
)


def read_api_key(path: Path) -> str:
    try:
        value = path.expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ClipAIError(f"无法读取 API Key 文件：{path}") from exc
    if not value:
        raise ClipAIError("API Key 文件为空")
    if "\n" in value or "\r" in value:
        raise ClipAIError("API Key 文件只能包含一行密钥")
    return value


def save_api_key(path: Path, api_key: str) -> None:
    value = api_key.strip()
    if not value or "\n" in value or "\r" in value:
        raise ClipAIError("API Key 必须是非空的单行文本")
    target = path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(target)


def load_current_cc_switch_provider(db_path: Path | None = None) -> CCSwitchProvider:
    """Read CC Switch's current Codex provider without modifying its database."""
    database = (db_path or (Path.home() / ".cc-switch" / "cc-switch.db")).expanduser()
    if not database.is_file():
        raise ClipAIError(f"没有找到 CC Switch 数据库：{database}")
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "select id, name, settings_config from providers "
            "where app_type='codex' and is_current=1 limit 1"
        ).fetchone()
        if row is None:
            raise ClipAIError("CC Switch 没有当前启用的 Codex 供应商")
        payload = json.loads(row["settings_config"] or "{}")
        auth = payload.get("auth") if isinstance(payload, dict) else None
        api_key = str(auth.get("OPENAI_API_KEY", "") if isinstance(auth, dict) else "").strip()
        config_text = str(payload.get("config", "") if isinstance(payload, dict) else "")
        config = tomllib.loads(config_text) if config_text.strip() else {}
        provider_id = str(config.get("model_provider", "custom"))
        provider_tables = config.get("model_providers", {})
        provider_config = provider_tables.get(provider_id, {}) if isinstance(provider_tables, dict) else {}
        base_url = str(provider_config.get("base_url", "") if isinstance(provider_config, dict) else "").strip()
        wire_api = str(provider_config.get("wire_api", "responses") if isinstance(provider_config, dict) else "responses")
        model = str(config.get("model", "")).strip()
        if not base_url:
            endpoint = connection.execute(
                "select url from provider_endpoints where provider_id=? and app_type='codex' limit 1",
                (row["id"],),
            ).fetchone()
            base_url = str(endpoint["url"] if endpoint else "").strip()
    except ClipAIError:
        raise
    except (sqlite3.Error, ValueError, tomllib.TOMLDecodeError) as exc:
        raise ClipAIError(f"读取 CC Switch 当前供应商失败：{type(exc).__name__}") from exc
    finally:
        try:
            connection.close()
        except (NameError, sqlite3.Error):
            pass
    protocol = "responses" if wire_api.lower() == "responses" else "chat_completions"
    if not base_url or not model or not api_key:
        raise ClipAIError("CC Switch 当前 Codex 供应商缺少请求地址、模型或 API Key")
    return CCSwitchProvider(str(row["name"]), base_url, model, protocol, api_key)


def split_transcript(
    segments: Iterable[TranscriptSegment],
    chunk_minutes: int = 30,
    max_chars: int = 42000,
    overlap_seconds: float = 75.0,
) -> tuple[TranscriptChunk, ...]:
    source = tuple(sorted(segments, key=lambda item: item.start))
    if not source:
        return ()
    indexed = tuple((f"s{index:06d}", segment) for index, segment in enumerate(source, start=1))
    chunks: list[TranscriptChunk] = []
    cursor = 0
    chunk_seconds = max(300.0, float(chunk_minutes) * 60.0)
    while cursor < len(indexed):
        first_start = indexed[cursor][1].start
        limit_time = first_start + chunk_seconds
        end = cursor
        char_count = 0
        while end < len(indexed):
            segment = indexed[end][1]
            added = len(segment.text) + 28
            if end > cursor and (segment.start > limit_time or char_count + added > max_chars):
                break
            char_count += added
            end += 1
        items = indexed[cursor:end]
        chunks.append(TranscriptChunk(len(chunks) + 1, items))
        if end >= len(indexed):
            break
        overlap_start = max(items[0][1].start, items[-1][1].end - overlap_seconds)
        next_cursor = end
        for candidate in range(cursor + 1, end):
            if indexed[candidate][1].start >= overlap_start:
                next_cursor = candidate
                break
        cursor = max(cursor + 1, next_cursor)
    return tuple(chunks)


def _endpoint(base_url: str, protocol: str) -> str:
    clean = base_url.strip().rstrip("/")
    if not clean.lower().startswith(("http://", "https://")):
        raise ClipAIError("API 请求地址必须以 http:// 或 https:// 开头")
    lowered = clean.lower()
    suffixes = ("/v1/responses", "/responses", "/v1/chat/completions", "/chat/completions")
    root = clean
    for suffix in suffixes:
        if lowered.endswith(suffix):
            root = clean[: -len(suffix)].rstrip("/")
            if suffix.startswith("/v1/"):
                root += "/v1"
            break
    if protocol == "responses":
        return f"{root}/responses" if root.lower().endswith("/v1") else f"{root}/v1/responses"
    if protocol == "chat_completions":
        return f"{root}/chat/completions" if root.lower().endswith("/v1") else f"{root}/v1/chat/completions"
    raise ClipAIError(f"不支持的 API 协议：{protocol}")


def _safe_remote_error(value: str) -> str:
    clean = re.sub(r"(?i)(bearer\s+|api[_-]?key[\"'=:\s]+)[A-Za-z0-9._-]+", r"\1<hidden>", value)
    return " ".join(clean.split())[:300]


def _verified_tls_context() -> ssl.SSLContext:
    """Use a current bundled CA store while keeping strict TLS validation."""

    try:
        return ssl.create_default_context(cafile=certifi.where())
    except (OSError, ssl.SSLError) as exc:
        raise ClipAIError(f"无法加载 HTTPS 可信证书集合：{_safe_remote_error(str(exc))}") from exc


def _network_error_message(reason: object) -> str:
    detail = _safe_remote_error(str(reason))
    lowered = detail.casefold()
    if isinstance(reason, ssl.SSLCertVerificationError) or "certificate_verify_failed" in lowered:
        if "expired" in lowered:
            return "HTTPS 证书链包含过期证书；请让中转站更新完整证书链，或切换到证书正常的 CC Switch 供应商"
        return "HTTPS 证书校验失败；请检查中转地址和服务器证书链，客户端不会关闭安全校验"
    return detail


def _default_sender(url: str, payload: dict[str, object], api_key: str, timeout: float) -> dict[str, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "bili-live-auto-clip-ai/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_verified_tls_context()) as response:
            raw = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(4096).decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        message = _safe_remote_error(detail) or str(exc.reason)
        raise ClipAIError(f"API HTTP {exc.code}：{message}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ClipAIError(f"API 网络请求失败：{_network_error_message(reason)}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ClipAIError("API 返回的不是有效 UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ClipAIError("API 返回 JSON 顶层不是对象")
    return parsed


def _response_text(payload: dict[str, object], protocol: str) -> str:
    if protocol == "chat_completions":
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    texts = [str(item.get("text", "")) for item in content if isinstance(item, dict)]
                    return "".join(texts)
    direct = payload.get("output_text")
    if isinstance(direct, str):
        return direct
    output = payload.get("output")
    if isinstance(output, list):
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        if texts:
            return "".join(texts)
    error = payload.get("error")
    if error:
        raise ClipAIError(f"API 返回错误：{_safe_remote_error(str(error))}")
    raise ClipAIError("API 响应中没有找到模型文本")


def _request_text(
    settings: ClipAISettings,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    sender: ApiSender | None = None,
) -> tuple[str, str]:
    send = sender or _default_sender
    protocols = ("responses", "chat_completions") if settings.protocol == "auto" else (settings.protocol,)
    errors: list[str] = []
    for protocol in protocols:
        if protocol == "responses":
            body: dict[str, object] = {
                "model": settings.model,
                "instructions": system_prompt,
                "input": user_prompt,
                "store": False,
            }
        else:
            body = {
                "model": settings.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
            }
        try:
            payload = send(_endpoint(settings.base_url, protocol), body, api_key, settings.timeout_seconds)
            return _response_text(payload, protocol), protocol
        except ClipAIError as exc:
            errors.append(f"{protocol}: {exc}")
    raise ClipAIError("；".join(errors)[:500] or "API 请求失败")


def test_api_connection(
    settings: ClipAISettings,
    api_key: str,
    sender: ApiSender | None = None,
) -> str:
    text, protocol = _request_text(
        settings,
        api_key,
        "你是连接测试助手，只需要简短确认服务可用。",
        "请只回复：连接正常",
        sender,
    )
    if not text.strip():
        raise ClipAIError("API 返回了空内容")
    return f"连接正常（{protocol}，模型 {settings.model}）"


def _chunk_prompt(
    chunk: TranscriptChunk,
    wanted: int,
    total_duration: float,
    accepted: tuple[ClipCandidate, ...] = (),
    retry: bool = False,
) -> str:
    lines = [
        f"录像总时长：{format_timestamp(total_duration)}",
        f"本字幕块范围：{format_timestamp(chunk.start)} - {format_timestamp(chunk.end)}",
    ]
    if retry:
        missing = max(1, wanted - len(accepted))
        lines.extend(
            [
                f"这是补足请求：本块已有 {len(accepted)} 条候选通过本地核验，还需要最多 {missing} 条新的候选。",
                "不要重复以下已通过候选的时间范围、主题或观点：",
                *(
                    (
                        f"- {format_timestamp(item.start)}-{format_timestamp(item.end)}：{item.title}"
                        for item in accepted
                    )
                    if accepted
                    else ("- 当前没有候选通过核验，请重新检查格式、时长、字幕依据和标题依据。",)
                ),
                "新候选必须同时提供合法的 edit_segments、title、cover_title 和 evidence_ids；没有新的合格内容可以返回空数组。",
            ]
        )
    else:
        lines.append(f"本块最多返回 {wanted} 条候选；如果没有足够好的内容可以少返回。")
    lines.append("字幕如下：")
    for segment_id, segment in chunk.items:
        text = " ".join(segment.text.split())
        lines.append(
            f"[{segment_id} {format_timestamp(segment.start)}-{format_timestamp(segment.end)}] {text}"
        )
    return "\n".join(lines)


def _parse_candidate_payload(text: str) -> list[object]:
    clean = text.strip()
    clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s*```$", "", clean)
    start = clean.find("{")
    end = clean.rfind("}")
    if start < 0 or end <= start:
        raise ClipAIError("模型没有返回 JSON 对象")
    try:
        payload = json.loads(clean[start : end + 1])
    except ValueError as exc:
        raise ClipAIError("模型返回的候选 JSON 无法解析") from exc
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        raise ClipAIError("模型 JSON 缺少 candidates 数组")
    return candidates


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value).casefold()


def _bigrams(value: str) -> set[str]:
    clean = _normalized(value)
    return {clean[index : index + 2] for index in range(max(0, len(clean) - 1))}


def _title_is_grounded(title: str, evidence: str, streamer: str) -> bool:
    normalized_evidence = _normalized(evidence)
    combined_title = title.replace(streamer, "") if streamer else title
    for claim in _STRICT_CLAIMS:
        if _normalized(claim) in _normalized(combined_title) and _normalized(claim) not in normalized_evidence:
            return False
    numeric_tokens = re.findall(r"\d+(?:\.\d+)?[a-zA-Z%％]?", _normalized(combined_title))
    if any(token.casefold() not in normalized_evidence for token in numeric_tokens):
        return False
    # Text before the first colon is allowed to be a semantic category such as
    # "学历回报".  Concrete claims after the colon still need lexical or
    # numeric support, while sensitive claims above always require exact text.
    claim_text = re.split(r"[：:]", combined_title, maxsplit=1)[-1]
    clauses = [
        item.strip()
        for item in re.split(r"[，,；;！？!?。]", claim_text)
        if len(_normalized(item)) >= 4
    ]
    if not clauses:
        clauses = [combined_title]
    boilerplate = ("主播", "直言", "追问", "聊到", "谈到", "观点", "一句反问", "直指问题")
    clause_checks: list[bool] = []
    for clause in clauses:
        claim = clause
        for phrase in boilerplate:
            claim = claim.replace(phrase, "")
        clean_claim = _normalized(claim)
        if len(clean_claim) < 3:
            continue
        common = _bigrams(claim).intersection(_bigrams(evidence))
        required = 1 if len(clean_claim) < 8 else 2
        clause_checks.append(len(common) >= required)
    if clause_checks and all(clause_checks):
        return True
    # Semantic rewrites often change a whole clause while keeping its concrete
    # topic and conclusion.  Sensitive claims and numbers above stay exact;
    # ordinary wording may pass on aggregate lexical support.
    clean_title = combined_title
    for phrase in boilerplate:
        clean_title = clean_title.replace(phrase, "")
    common = _bigrams(clean_title).intersection(_bigrams(evidence))
    required = 1 if len(_normalized(clean_title)) < 8 else 2
    return len(common) >= required


def _supporting_evidence(
    title: str,
    api_selected: tuple[TranscriptSegment, ...],
    window_segments: tuple[TranscriptSegment, ...],
    streamer: str,
) -> str:
    title_terms = _bigrams(title.replace(streamer, "") if streamer else title)

    def support_score(segment: TranscriptSegment) -> tuple[int, int]:
        overlap = len(title_terms.intersection(_bigrams(segment.text)))
        return overlap, len(segment.text)

    chosen: list[TranscriptSegment] = list(dict.fromkeys(api_selected))
    covered = set().union(*(_bigrams(item.text) for item in chosen)) if chosen else set()
    remaining = [segment for segment in window_segments if segment not in chosen]
    while remaining and len(chosen) < 6:
        segment = max(
            remaining,
            key=lambda item: (len((title_terms - covered).intersection(_bigrams(item.text))), support_score(item)),
        )
        new_terms = (title_terms - covered).intersection(_bigrams(segment.text))
        if not new_terms:
            break
        chosen.append(segment)
        covered.update(_bigrams(segment.text))
        remaining.remove(segment)
    return "；".join(item.text.strip() for item in sorted(chosen, key=lambda item: item.start))[:360]


def _localized_title(value: str, streamer: str) -> str:
    title = " ".join(value.replace("\r", " ").replace("\n", " ").split()).strip(" ：:")
    if streamer:
        if title.startswith("主播"):
            title = f"{streamer}{title[2:]}"
        elif not title.startswith(streamer):
            title = f"{streamer}：{title}"
    if title and title[-1] not in "！？!?。":
        title += "！"
    return title[:80]


def _edit_ranges_from_raw(
    raw: dict[str, object],
    chunk: TranscriptChunk,
    duration: float,
) -> tuple[tuple[float, float], ...] | None:
    raw_ranges = raw.get("edit_segments")
    if not isinstance(raw_ranges, list):
        raw_ranges = raw.get("segments")
    if not isinstance(raw_ranges, list):
        try:
            raw_ranges = [{"start": raw["start"], "end": raw["end"]}]
        except KeyError:
            return None
    if not 1 <= len(raw_ranges) <= 6:
        return None
    starts = tuple(segment.start for _segment_id, segment in chunk.items)
    ends = tuple(segment.end for _segment_id, segment in chunk.items)

    def verified_boundary(value: float, boundaries: tuple[float, ...]) -> float | None:
        nearest = min(boundaries, key=lambda item: abs(item - value))
        return nearest if abs(nearest - value) <= 15.0 else None

    ranges: list[tuple[float, float]] = []
    for item in raw_ranges:
        if not isinstance(item, dict):
            return None
        try:
            requested_start = max(0.0, float(item["start"]))
            requested_end = min(duration, float(item["end"]))
        except (KeyError, TypeError, ValueError):
            return None
        start = verified_boundary(requested_start, starts)
        end = verified_boundary(requested_end, ends)
        if start is None or end is None:
            return None
        if end - start < 12.0:
            return None
        ranges.append((round(start, 2), round(end, 2)))
    source_order = sorted(ranges)
    if any(current[0] < previous[1] - 0.25 for previous, current in zip(source_order, source_order[1:])):
        return None
    edited_duration = sum(end - start for start, end in ranges)
    minimum_duration = min(MIN_CLIP_SECONDS, duration)
    maximum_duration = min(MAX_CLIP_SECONDS, duration)
    if edited_duration < minimum_duration - 0.5 or edited_duration > maximum_duration + 0.5:
        return None
    return tuple(ranges)


def _validated_candidate(
    raw: object,
    chunk: TranscriptChunk,
    duration: float,
    streamer: str,
    ordinal: int,
    rejection: Callable[[str], None] | None = None,
) -> ClipCandidate | None:
    def rejected(reason: str) -> None:
        if rejection is not None:
            rejection(reason)
        return None

    if not isinstance(raw, dict):
        return rejected("候选不是 JSON 对象")
    chunk_segments = dict(chunk.items)
    edit_ranges = _edit_ranges_from_raw(raw, chunk, duration)
    if edit_ranges is None:
        return rejected("剪辑段格式、切点或总时长不合格")
    try:
        score = float(raw.get("score", 75))
    except (TypeError, ValueError):
        return rejected("评分不是数字")
    raw_title = str(raw.get("title", "")).strip()
    if not 12 <= len(raw_title) <= 80 or re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", raw_title):
        return rejected("视频标题缺失、过短、过长或包含时间戳")
    title = _localized_title(raw_title, streamer)
    raw_cover_title = str(raw.get("cover_title", "")).strip()
    cover_title = " ".join(raw_cover_title.replace("\r", " ").replace("\n", " ").split()).strip(" ：:")
    if not 4 <= len(cover_title) <= 40 or re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", cover_title):
        return rejected("封面标题缺失、过短、过长或包含时间戳")

    raw_ids = raw.get("evidence_ids")
    provided_ids = tuple(
        dict.fromkeys(str(item) for item in raw_ids if str(item) in chunk_segments)
    ) if isinstance(raw_ids, list) else ()
    provided = tuple(
        chunk_segments[item]
        for item in provided_ids
        if any(
            chunk_segments[item].end >= start and chunk_segments[item].start <= end
            for start, end in edit_ranges
        )
    )

    title_terms = _bigrams(f"{raw_title} {cover_title}")

    def support_score(segment: TranscriptSegment) -> tuple[int, int]:
        return len(title_terms.intersection(_bigrams(segment.text))), len(segment.text)

    selected_list: list[TranscriptSegment] = []
    for start, end in edit_ranges:
        within = tuple(
            segment
            for _segment_id, segment in chunk.items
            if segment.end >= start and segment.start <= end
        )
        if not within:
            return rejected("剪辑段内没有本地字幕")
        preferred = tuple(segment for segment in provided if segment in within)
        selected_list.append(max(preferred or within, key=support_score))
    for segment in provided:
        if segment not in selected_list and len(selected_list) < 6:
            selected_list.append(segment)
    selected = tuple(sorted(dict.fromkeys(selected_list), key=lambda item: item.start))
    window_segments = tuple(
        segment
        for _segment_id, segment in chunk.items
        if any(segment.end >= start and segment.start <= end for start, end in edit_ranges)
    )
    evidence = _supporting_evidence(title, selected, window_segments, streamer)
    if not evidence:
        return rejected("保留片段中没有可用字幕依据")
    grounding_evidence = "；".join(segment.text.strip() for segment in selected)
    topic = " ".join(str(raw.get("topic", "")).split()).strip(" ：:")[:24]
    if any(
        _normalized(claim) in _normalized(topic)
        and _normalized(claim) not in _normalized(grounding_evidence)
        for claim in _STRICT_CLAIMS
    ):
        return rejected("话题包含保留字幕中不存在的敏感事实")
    if not _title_is_grounded(title, grounding_evidence, streamer):
        return rejected("视频标题缺少字幕依据")
    if not _title_is_grounded(cover_title, grounding_evidence, streamer):
        return rejected("封面标题缺少字幕依据")
    evidence_segment = TranscriptSegment(selected[0].start, selected[-1].end, evidence)
    local_topics = tuple(match.label for match in _matched_topics((evidence_segment,)))
    topics = tuple(dict.fromkeys(((topic,) if topic else ()) + local_topics))
    start = min(item[0] for item in edit_ranges)
    end = max(item[1] for item in edit_ranges)
    editing_signals = ("多段精剪",) if len(edit_ranges) > 1 else ()
    if tuple(sorted(edit_ranges)) != edit_ranges:
        editing_signals += ("重排叙事",)
    return ClipCandidate(
        id=f"a{chunk.index:02d}-{ordinal:02d}-{int(start):06d}",
        start=round(start, 2),
        end=round(end, 2),
        score=round(max(0.0, min(99.0, score)), 1),
        title=title,
        topics=topics,
        evidence=evidence,
        cover_title=cover_title,
        signals=tuple(dict.fromkeys(_interest_labels(evidence) + editing_signals)),
        origin="api",
        edit_ranges=edit_ranges,
    )


def _overlap_ratio(left: ClipCandidate, right: ClipCandidate) -> float:
    overlap = sum(
        max(0.0, min(left_end, right_end) - max(left_start, right_start))
        for left_start, left_end in left.timeline_ranges
        for right_start, right_end in right.timeline_ranges
    )
    return overlap / max(1.0, min(left.duration, right.duration))


def _duplicates(left: ClipCandidate, right: ClipCandidate) -> bool:
    if _overlap_ratio(left, right) >= 0.45:
        return True
    left_terms = _bigrams(left.title)
    right_terms = _bigrams(right.title)
    union = left_terms | right_terms
    similarity = len(left_terms & right_terms) / len(union) if union else 0.0
    return similarity >= 0.72


def _select_api_only(
    api_candidates: Iterable[ClipCandidate],
    target: int,
) -> tuple[ClipCandidate, ...]:
    selected: list[ClipCandidate] = []
    for candidate in sorted(api_candidates, key=lambda item: (-item.score, item.start)):
        if candidate.origin != "api":
            continue
        if any(_duplicates(candidate, kept) for kept in selected):
            continue
        selected.append(candidate)
        if len(selected) >= target:
            break
    if not selected:
        raise ClipAIError("API 没有返回通过本地字幕与时间戳核验的候选")
    return tuple(sorted(selected, key=lambda item: item.start))


def _cache_signature(settings: ClipAISettings, duration: float, streamer: str = "") -> str:
    content = json.dumps(
        {
            "prompt": PROMPT_VERSION,
            "base_url": settings.base_url.rstrip("/"),
            "model": settings.model,
            "protocol": settings.protocol,
            "chunk_minutes": settings.chunk_minutes,
            "duration": round(duration, 2),
            "local_streamer": streamer,
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]


def _candidate_from_cache(raw: object) -> ClipCandidate | None:
    if not isinstance(raw, dict):
        return None
    try:
        edit_ranges = tuple(
            (float(item[0]), float(item[1]))
            for item in raw.get("edit_ranges", [])
            if isinstance(item, (list, tuple)) and len(item) == 2
        )
        return ClipCandidate(
            id=str(raw["id"]),
            start=float(raw["start"]),
            end=float(raw["end"]),
            score=float(raw["score"]),
            title=str(raw["title"]),
            topics=tuple(map(str, raw.get("topics", []))),
            evidence=str(raw["evidence"]),
            cover_title=str(raw.get("cover_title", "")),
            signals=tuple(map(str, raw.get("signals", []))),
            origin=str(raw.get("origin", "api")),
            edit_ranges=edit_ranges,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _load_api_cache(path: Path, signature: str) -> tuple[ClipCandidate, ...] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("signature") != signature:
        return None
    candidates = tuple(
        item for item in (_candidate_from_cache(raw) for raw in payload.get("candidates", [])) if item is not None
    )
    if not candidates or any(
        item.origin != "api"
        or not item.title.strip()
        or not item.cover_title.strip()
        or not item.edit_ranges
        for item in candidates
    ):
        return None
    return candidates


def enhance_analysis(
    analysis: ClipAnalysis,
    settings: ClipAISettings,
    api_key: str,
    progress: ProgressCallback | None = None,
    sender: ApiSender | None = None,
    streamer: str = "",
) -> ClipAnalysis:
    if not settings.enabled:
        return analysis
    if not api_key.strip():
        raise ClipAIError("尚未配置 API Key")
    transcript_path = analysis.cache_dir / "transcript.json"
    try:
        transcript_payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClipAIError("无法读取本地转写缓存，不能进行 API 语义分析") from exc
    segments = _segments_from_payload(transcript_payload.get("segments"))
    if not segments:
        raise ClipAIError("本地转写缓存没有有效字幕")
    signature = _cache_signature(settings, analysis.duration, streamer)
    cache_file = analysis.cache_dir / "api-candidates.json"
    cached = _load_api_cache(cache_file, signature)
    if cached:
        if progress:
            progress("已复用通过本地核验的 API 候选缓存")
        return replace(
            analysis,
            candidates=cached,
            candidate_source="api",
            candidate_note="复用 API 语义候选缓存",
        )
    chunks = split_transcript(segments, settings.chunk_minutes)
    if not chunks:
        raise ClipAIError("没有可以发送的字幕分块")
    target = candidate_count_for_duration(analysis.duration)
    wanted_per_chunk = max(1, min(3, math.ceil(target * 1.6 / len(chunks))))
    valid: list[ClipCandidate] = []
    errors: list[str] = []
    rejections: Counter[str] = Counter()
    used_protocols: set[str] = set()
    for index, chunk in enumerate(chunks, start=1):
        chunk_valid: list[ClipCandidate] = []
        for attempt in range(2):
            if progress:
                action = "补足" if attempt else "处理"
                progress(
                    f"API 语义分析：正在{action}字幕块 {index}/{len(chunks)}"
                    f"（第 {attempt + 1}/2 次，不上传录像）"
                )
            try:
                text, protocol = _request_text(
                    settings,
                    api_key,
                    _SYSTEM_PROMPT,
                    _chunk_prompt(
                        chunk,
                        wanted_per_chunk,
                        analysis.duration,
                        tuple(chunk_valid),
                        retry=attempt > 0,
                    ),
                    sender,
                )
                used_protocols.add(protocol)
                raw_candidates = _parse_candidate_payload(text)
                for ordinal, raw in enumerate(raw_candidates, start=attempt * 100 + 1):
                    candidate = _validated_candidate(
                        raw,
                        chunk,
                        analysis.duration,
                        streamer,
                        ordinal,
                        lambda reason: rejections.update((reason,)),
                    )
                    if candidate is None:
                        continue
                    if any(_duplicates(candidate, kept) for kept in valid):
                        rejections.update(("候选时间或标题重复",))
                        continue
                    valid.append(candidate)
                    chunk_valid.append(candidate)
                if len(chunk_valid) >= wanted_per_chunk:
                    break
            except ClipAIError as exc:
                errors.append(f"第 {index} 块第 {attempt + 1} 次：{exc}")
        if progress and len(chunk_valid) < wanted_per_chunk:
            progress(
                f"字幕块 {index}/{len(chunks)} 两次请求后仅有 {len(chunk_valid)} 条通过核验；"
                "不会使用本地候选补位"
            )
    if not valid:
        details: list[str] = []
        if rejections:
            details.append(
                "核验拒绝：" + "、".join(f"{reason}×{count}" for reason, count in rejections.most_common(3))
            )
        if errors:
            details.append(f"请求错误 {len(errors)} 次：{errors[0]}")
        if not details:
            details.append("API 两次都返回了空 candidates 数组")
        raise ClipAIError("没有可用的 API 候选；" + "；".join(details))
    selected = _select_api_only(valid, target)
    source = "api"
    protocol_note = "/".join(sorted(used_protocols)) or settings.protocol
    note = f"API 语义增强（{protocol_note}，{len(valid)} 条通过本地核验）"
    if errors:
        note += f"；{len(errors)} 次请求未成功"
    if rejections:
        note += "；已拒绝 " + "、".join(
            f"{reason}×{count}" for reason, count in rejections.most_common(2)
        )
    if len(selected) < target:
        note += f"；目标 {target} 条，仅 {len(selected)} 条 API 候选通过核验，不使用本地补位"
    cache_file.write_text(
        json.dumps(
            {
                "signature": signature,
                "candidate_source": source,
                "candidates": [item.as_dict() for item in selected],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return replace(analysis, candidates=selected, candidate_source=source, candidate_note=note)


def enhance_analysis_with_fallback(
    analysis: ClipAnalysis,
    settings: ClipAISettings,
    api_key: str,
    progress: ProgressCallback | None = None,
    sender: ApiSender | None = None,
    streamer: str = "",
) -> ClipAnalysis:
    if not settings.enabled:
        return analysis
    try:
        return enhance_analysis(analysis, settings, api_key, progress, sender, streamer)
    except ClipAIError as exc:
        message = _safe_remote_error(str(exc))
        if progress:
            progress(f"API 不可用，本次不生成候选：{message}")
        return replace(
            analysis,
            candidates=(),
            candidate_source="api_failed",
            candidate_note=f"API 不可用，本次未生成候选：{message}",
        )
