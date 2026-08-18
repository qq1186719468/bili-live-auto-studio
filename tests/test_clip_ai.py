from __future__ import annotations

import json
import ssl
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bili_live_auto.clip_ai import (
    _endpoint,
    _default_sender,
    _verified_tls_context,
    enhance_analysis,
    enhance_analysis_with_fallback,
    load_current_cc_switch_provider,
    read_api_key,
    save_api_key,
    split_transcript,
    test_api_connection as check_api_connection,
)
from bili_live_auto.clipper import ClipAnalysis, ClipCandidate, TranscriptSegment
from bili_live_auto.config import ClipAISettings


class ClipAITests(unittest.TestCase):
    def _analysis(self, root: Path) -> ClipAnalysis:
        segments = [
            {"start": 120.0, "end": 240.0, "text": "我读了十八年的书，为什么生活还是没有变好"},
            {"start": 352.0, "end": 360.0, "text": "这个问题我真的想了很久"},
        ]
        root.mkdir(parents=True, exist_ok=True)
        (root / "transcript.json").write_text(
            json.dumps({"segments": segments}, ensure_ascii=False),
            encoding="utf-8",
        )
        local = ClipCandidate(
            id="c01-000120",
            start=120.0,
            end=360.0,
            score=70,
            title="测试主播一句反问：为什么生活没有变好？",
            topics=("学历与教育",),
            evidence=segments[0]["text"],
            signals=("反问追问",),
        )
        return ClipAnalysis(Path("G:/不会上传/原录像.mp4"), 600.0, root, True, (local,))

    @staticmethod
    def _settings(protocol: str = "responses") -> ClipAISettings:
        return ClipAISettings(
            enabled=True,
            base_url="https://relay.example.com",
            model="gpt-test",
            protocol=protocol,
            api_key_file=Path("unused-key.txt"),
            timeout_seconds=30,
            chunk_minutes=30,
        )

    @staticmethod
    def _response(candidate: dict[str, object]) -> dict[str, object]:
        content = json.dumps({"candidates": [candidate]}, ensure_ascii=False)
        return {"output": [{"content": [{"type": "output_text", "text": content}]}]}

    @staticmethod
    def _responses(candidates: list[dict[str, object]]) -> dict[str, object]:
        content = json.dumps({"candidates": candidates}, ensure_ascii=False)
        return {"output": [{"content": [{"type": "output_text", "text": content}]}]}

    def _long_analysis(self, root: Path) -> ClipAnalysis:
        segments = [
            {"start": 120.0, "end": 240.0, "text": "我读了十八年的书，为什么生活还是没有变好"},
            {"start": 352.0, "end": 360.0, "text": "这个问题我真的想了很久"},
            {"start": 500.0, "end": 620.0, "text": "公司突然裁员以后我才明白，所谓稳定只是暂时没有变化"},
            {"start": 732.0, "end": 740.0, "text": "这段经历让我重新理解了稳定"},
            {"start": 900.0, "end": 1020.0, "text": "一直躺平也不会让压力消失，真正重要的是重新拿回选择"},
        ]
        root.mkdir(parents=True, exist_ok=True)
        (root / "transcript.json").write_text(
            json.dumps({"segments": segments}, ensure_ascii=False),
            encoding="utf-8",
        )
        local_candidates = tuple(
            ClipCandidate(
                id=f"local-{index}",
                start=120.0 + index * 400,
                end=360.0 + index * 400,
                score=80 - index,
                title=f"本地候选标题{index}",
                topics=("本地",),
                evidence="本地规则结果",
            )
            for index in range(3)
        )
        return ClipAnalysis(Path("G:/不会上传/长录像.mp4"), 3600.0, root, True, local_candidates)

    @staticmethod
    def _valid_candidate_one() -> dict[str, object]:
        return {
            "edit_segments": [{"start": 120, "end": 360, "reason": "完整观点"}],
            "score": 91,
            "title": "主播追问学历回报：读了十八年书，为什么生活还是没有变好",
            "cover_title": "读了十八年书，生活为什么没变好",
            "topic": "学历与生活",
            "evidence_ids": ["s000001"],
            "reason": "有明确反问",
        }

    @staticmethod
    def _valid_candidate_two() -> dict[str, object]:
        return {
            "edit_segments": [{"start": 500, "end": 740, "reason": "经历和结论"}],
            "score": 89,
            "title": "公司突然裁员以后我才明白：所谓稳定只是暂时没有变化",
            "cover_title": "所谓稳定只是暂时没变化",
            "topic": "职场与稳定",
            "evidence_ids": ["s000003"],
            "reason": "经历和结论完整",
        }

    def test_endpoint_accepts_cc_switch_root_and_full_url(self):
        self.assertEqual(_endpoint("https://www.heiyucode.com", "responses"), "https://www.heiyucode.com/v1/responses")
        self.assertEqual(
            _endpoint("https://www.heiyucode.com/v1/responses", "responses"),
            "https://www.heiyucode.com/v1/responses",
        )
        self.assertEqual(
            _endpoint("https://www.heiyucode.com/v1/responses", "chat_completions"),
            "https://www.heiyucode.com/v1/chat/completions",
        )

    def test_bundled_ca_context_keeps_strict_tls_verification(self):
        context = _verified_tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_default_sender_passes_verified_tls_context(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit: int) -> bytes:
                return b'{"ok":true}'

        with mock.patch("bili_live_auto.clip_ai.urllib.request.urlopen", return_value=Response()) as opened:
            result = _default_sender("https://relay.example.com/v1/responses", {"model": "test"}, "key", 10)
        self.assertEqual(result, {"ok": True})
        context = opened.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_split_transcript_keeps_stable_evidence_ids(self):
        segments = tuple(TranscriptSegment(index * 600, index * 600 + 5, f"第{index}段字幕") for index in range(5))
        chunks = split_transcript(segments, chunk_minutes=15, overlap_seconds=60)
        self.assertGreaterEqual(len(chunks), 2)
        all_ids = {segment_id for chunk in chunks for segment_id, _segment in chunk.items}
        self.assertEqual(all_ids, {"s000001", "s000002", "s000003", "s000004", "s000005"})

    def test_imports_current_cc_switch_codex_provider_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "cc-switch.db"
            connection = sqlite3.connect(database)
            connection.execute(
                "create table providers (id text, app_type text, name text, settings_config text, is_current integer)"
            )
            connection.execute("create table provider_endpoints (provider_id text, app_type text, url text)")
            config = (
                'model_provider = "custom"\nmodel = "gpt-test"\n'
                '[model_providers.custom]\nwire_api = "responses"\nbase_url = "https://relay.example.com"\n'
            )
            settings = json.dumps({"auth": {"OPENAI_API_KEY": "test-secret"}, "config": config})
            connection.execute(
                "insert into providers values (?, ?, ?, ?, ?)",
                ("p1", "codex", "测试供应商", settings, 1),
            )
            connection.commit()
            connection.close()
            provider = load_current_cc_switch_provider(database)
            self.assertEqual(provider.name, "测试供应商")
            self.assertEqual(provider.base_url, "https://relay.example.com")
            self.assertEqual(provider.model, "gpt-test")
            self.assertEqual(provider.protocol, "responses")
            self.assertEqual(provider.api_key, "test-secret")

    def test_api_enhancement_sends_only_transcript_and_locally_verifies_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            analysis = self._analysis(Path(directory))
            captured: list[tuple[str, dict[str, object], str]] = []

            def sender(url: str, body: dict[str, object], key: str, _timeout: float) -> dict[str, object]:
                captured.append((url, body, key))
                return self._response(
                    {
                        "edit_segments": [{"start": 120, "end": 360, "reason": "完整观点"}],
                        "score": 91,
                        "title": "主播追问学历回报：读了十八年书，为什么生活还是没有变好",
                        "cover_title": "读了十八年书，生活为什么没变好",
                        "topic": "学历与生活",
                        "evidence_ids": ["s000001"],
                        "reason": "有明确反问",
                    }
                )

            result = enhance_analysis(
                analysis,
                self._settings(),
                "test-key",
                sender=sender,
                streamer="测试主播",
            )
            self.assertEqual(result.candidate_source, "api")
            self.assertEqual(result.candidates[0].origin, "api")
            self.assertTrue(result.candidates[0].title.startswith("测试主播追问"))
            self.assertEqual(result.candidates[0].cover_title, "读了十八年书，生活为什么没变好")
            self.assertEqual(result.candidates[0].edit_ranges, ((120.0, 360.0),))
            self.assertEqual(captured[0][0], "https://relay.example.com/v1/responses")
            serialized = json.dumps(captured[0][1], ensure_ascii=False)
            self.assertIn("我读了十八年的书", serialized)
            self.assertIn("3 到 5 分钟", serialized)
            self.assertIn("2.5 到 6 分钟", serialized)
            self.assertIn("不要把相隔很远的金句拼成碎片", serialized)
            self.assertNotIn("G:/不会上传", serialized)
            self.assertNotIn("测试主播", serialized)
            self.assertEqual(captured[0][2], "test-key")
            self.assertTrue((Path(directory) / "api-candidates.json").is_file())

    def test_invalid_evidence_id_returns_api_failed_without_local_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            analysis = self._analysis(Path(directory))

            def sender(_url: str, _body: dict[str, object], _key: str, _timeout: float) -> dict[str, object]:
                return self._response(
                    {
                        "edit_segments": [{"start": 120, "end": 360, "reason": "伪造内容"}],
                        "score": 99,
                        "title": "主播编造了一个完全不存在的观点",
                        "cover_title": "完全不存在的观点",
                        "topic": "虚构话题",
                        "evidence_ids": ["missing"],
                    }
                )

            result = enhance_analysis_with_fallback(
                analysis,
                self._settings(),
                "test-key",
                sender=sender,
                streamer="测试主播",
            )
            self.assertEqual(result.candidate_source, "api_failed")
            self.assertEqual(result.candidates, ())

    def test_api_candidate_below_flexible_minimum_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            analysis = self._analysis(Path(directory))

            def sender(_url: str, _body: dict[str, object], _key: str, _timeout: float) -> dict[str, object]:
                return self._response(
                    {
                        "edit_segments": [{"start": 120, "end": 240, "reason": "时长不足"}],
                        "score": 95,
                        "title": "主播追问学历回报：读了十八年书为什么生活还是没有变好",
                        "cover_title": "读了十八年书为何没变好",
                        "topic": "学历与生活",
                        "evidence_ids": ["s000001"],
                    }
                )

            result = enhance_analysis_with_fallback(
                analysis,
                self._settings(),
                "test-key",
                sender=sender,
                streamer="测试主播",
            )
            self.assertEqual(result.candidate_source, "api_failed")
            self.assertEqual(result.candidates, ())

    def test_api_candidate_shortfall_never_uses_local_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            analysis = self._long_analysis(Path(directory))
            calls = 0

            def sender(_url: str, _body: dict[str, object], _key: str, _timeout: float) -> dict[str, object]:
                nonlocal calls
                calls += 1
                return self._response(self._valid_candidate_one())

            result = enhance_analysis(
                analysis,
                self._settings(),
                "test-key",
                sender=sender,
                streamer="测试主播",
            )
            self.assertEqual(calls, 2)
            self.assertEqual(result.candidate_source, "api")
            self.assertEqual(len(result.candidates), 1)
            self.assertTrue(all(item.origin == "api" for item in result.candidates))
            self.assertIn("不使用本地补位", result.candidate_note)

    def test_legacy_api_start_end_is_accepted_and_snapped_to_subtitle_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            analysis = self._analysis(Path(directory))
            candidate = self._valid_candidate_one()
            candidate.pop("edit_segments")
            candidate.pop("evidence_ids")
            candidate["start"] = 127
            candidate["end"] = 354

            def sender(_url: str, _body: dict[str, object], _key: str, _timeout: float) -> dict[str, object]:
                return self._response(candidate)

            result = enhance_analysis(
                analysis,
                self._settings(),
                "test-key",
                sender=sender,
                streamer="测试主播",
            )
            self.assertEqual(result.candidate_source, "api")
            self.assertEqual(result.candidates[0].edit_ranges, ((120.0, 360.0),))
            self.assertIn("读了十八年的书", result.candidates[0].evidence)

    def test_missing_evidence_ids_are_filled_from_each_kept_api_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            analysis = self._long_analysis(Path(directory))
            candidate = {
                "edit_segments": [
                    {"start": 120, "end": 240, "reason": "学历问题"},
                    {"start": 500, "end": 620, "reason": "裁员后的结论"},
                ],
                "score": 92,
                "title": "读了十八年书生活还是没变好：公司突然裁员以后才明白，所谓稳定只是暂时没有变化",
                "cover_title": "裁员以后才明白稳定只是暂时",
                "topic": "学历与职场稳定",
                "reason": "两个片段共同支撑主题",
            }

            def sender(_url: str, _body: dict[str, object], _key: str, _timeout: float) -> dict[str, object]:
                return self._response(candidate)

            result = enhance_analysis(
                analysis,
                self._settings(),
                "test-key",
                sender=sender,
                streamer="测试主播",
            )
            self.assertEqual(result.candidates[0].edit_ranges, ((120.0, 240.0), (500.0, 620.0)))
            self.assertIn("读了十八年的书", result.candidates[0].evidence)
            self.assertIn("公司突然裁员", result.candidates[0].evidence)

    def test_second_api_request_can_add_new_verified_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            analysis = self._long_analysis(Path(directory))
            prompts: list[str] = []

            def sender(_url: str, body: dict[str, object], _key: str, _timeout: float) -> dict[str, object]:
                prompts.append(str(body["input"]))
                candidate = self._valid_candidate_one() if len(prompts) == 1 else self._valid_candidate_two()
                return self._response(candidate)

            result = enhance_analysis(
                analysis,
                self._settings(),
                "test-key",
                sender=sender,
                streamer="测试主播",
            )
            self.assertEqual(len(prompts), 2)
            self.assertIn("补足请求", prompts[1])
            self.assertIn("不要重复", prompts[1])
            self.assertEqual(len(result.candidates), 2)
            self.assertTrue(all(item.origin == "api" for item in result.candidates))

    def test_duplicate_returned_by_second_api_request_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            analysis = self._long_analysis(Path(directory))

            def sender(_url: str, _body: dict[str, object], _key: str, _timeout: float) -> dict[str, object]:
                return self._response(self._valid_candidate_one())

            result = enhance_analysis(
                analysis,
                self._settings(),
                "test-key",
                sender=sender,
                streamer="测试主播",
            )
            self.assertEqual(len(result.candidates), 1)

    def test_api_can_return_verified_reordered_high_density_edit_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            analysis = self._long_analysis(Path(directory))
            edited = {
                "edit_segments": [
                    {"start": 900, "end": 1020, "reason": "先给出结论"},
                    {"start": 120, "end": 240, "reason": "再补充问题背景"},
                ],
                "score": 94,
                "title": "读了十八年书生活仍没有变好：一直躺平不会让压力消失，真正重要的是重新拿回选择",
                "cover_title": "躺平不会让压力消失",
                "topic": "学历、压力与选择",
                "evidence_ids": ["s000005", "s000001"],
                "reason": "删去中间跑题内容并用结论开场",
            }

            def sender(_url: str, _body: dict[str, object], _key: str, _timeout: float) -> dict[str, object]:
                return self._response(edited)

            result = enhance_analysis(
                analysis,
                self._settings(),
                "test-key",
                sender=sender,
                streamer="测试主播",
            )
            candidate = result.candidates[0]
            self.assertEqual(candidate.edit_ranges, ((900.0, 1020.0), (120.0, 240.0)))
            self.assertEqual(candidate.duration, 240.0)
            self.assertIn("多段精剪", candidate.signals)
            self.assertIn("重排叙事", candidate.signals)

    def test_missing_cover_title_is_rejected_instead_of_generated_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            analysis = self._analysis(Path(directory))
            candidate = self._valid_candidate_one()
            candidate.pop("cover_title")

            def sender(_url: str, _body: dict[str, object], _key: str, _timeout: float) -> dict[str, object]:
                return self._response(candidate)

            result = enhance_analysis_with_fallback(
                analysis,
                self._settings(),
                "test-key",
                sender=sender,
                streamer="测试主播",
            )
            self.assertEqual(result.candidate_source, "api_failed")
            self.assertEqual(result.candidates, ())
            self.assertIn("封面标题", result.candidate_note)

    def test_api_key_uses_separate_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets" / "clip-ai-key.txt"
            save_api_key(path, "secret-value")
            self.assertEqual(read_api_key(path), "secret-value")

    def test_chat_completions_connection_response_is_supported(self):
        captured: list[str] = []

        def sender(url: str, _body: dict[str, object], _key: str, _timeout: float) -> dict[str, object]:
            captured.append(url)
            return {"choices": [{"message": {"content": "连接正常"}}]}

        message = check_api_connection(self._settings("chat_completions"), "test-key", sender)
        self.assertIn("chat_completions", message)
        self.assertEqual(captured[0], "https://relay.example.com/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
