from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bili_live_auto.clipper import (
    MAX_CLIP_SECONDS,
    MIN_CLIP_SECONDS,
    PREFERRED_MAX_CLIP_SECONDS,
    PREFERRED_MIN_CLIP_SECONDS,
    TranscriptSegment,
    _candidate_window,
    _refine_window_to_speech_boundaries,
    _transcription_request_payload,
    candidate_count_for_duration,
    generate_candidates,
    merge_sources,
)


class ClipperTests(unittest.TestCase):
    def test_transcription_request_roundtrips_chinese_windows_paths_as_ascii_json(self):
        source = Path("G:/佳子直播录屏/战狼铠甲佳宝/P3-佳宝.mp4")
        model = Path("C:/模型/faster-whisper-small")
        payload = _transcription_request_payload(source, model)
        self.assertTrue(payload.isascii())
        decoded = json.loads(payload.encode("utf-8").decode("utf-8"))
        self.assertEqual(decoded["source"], str(source))
        self.assertEqual(decoded["model"], str(model))
        self.assertGreaterEqual(decoded["cpu_threads"], 1)

    def test_single_source_merge_is_a_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "part.mp4"
            source.write_bytes(b"placeholder")
            self.assertEqual(merge_sources((source,), Path(temporary)), source.resolve())

    def test_candidate_count_scales_with_duration(self):
        self.assertEqual(candidate_count_for_duration(10 * 60), 1)
        self.assertEqual(candidate_count_for_duration(20 * 60), 3)
        self.assertEqual(candidate_count_for_duration(60 * 60), 3)
        self.assertEqual(candidate_count_for_duration(2 * 60 * 60), 6)
        self.assertEqual(candidate_count_for_duration(3 * 60 * 60), 9)

    def test_candidate_windows_prefer_three_to_five_minutes(self):
        short = _candidate_window(500, 3600, 120)
        long = _candidate_window(500, 3600, 900)
        self.assertEqual(short, (438.8, 618.8))
        self.assertEqual(long, (398.0, 698.0))
        self.assertAlmostEqual(short[1] - short[0], PREFERRED_MIN_CLIP_SECONDS)
        self.assertAlmostEqual(long[1] - long[0], PREFERRED_MAX_CLIP_SECONDS)

    def test_speech_boundary_refinement_keeps_duration_contract(self):
        segments = (
            TranscriptSegment(390, 399, "前一个话题说完了。"),
            TranscriptSegment(405, 414, "现在开始讲完整的新话题"),
            TranscriptSegment(680, 690, "这个话题到这里就结束了。"),
            TranscriptSegment(698, 704, "下面换一个内容"),
        )
        start, end = _refine_window_to_speech_boundaries(400, 700, segments, 500, 3600)
        self.assertGreaterEqual(end - start, MIN_CLIP_SECONDS)
        self.assertLessEqual(end - start, MAX_CLIP_SECONDS)
        self.assertEqual((start, end), (405, 690))

    def test_hot_topic_title_needs_transcript_evidence(self):
        candidates = generate_candidates(
            (TranscriptSegment(50, 56, "今天聊结婚和彩礼，大家都有不同看法"),),
            duration=300,
            count=1,
        )
        self.assertEqual(candidates[0].topics, ("结婚",))
        self.assertIn("结婚", candidates[0].title)
        self.assertIn("结婚", candidates[0].evidence)

    def test_sensitive_combined_topic_is_not_invented(self):
        candidates = generate_candidates(
            (TranscriptSegment(50, 56, "今天只是提到擦边这个词，没有说别的"),),
            duration=300,
            count=1,
        )
        self.assertNotIn("擦边主播", candidates[0].topics)
        self.assertNotIn("擦边主播", candidates[0].title)

    def test_explicit_combined_topic_is_allowed_with_evidence(self):
        candidates = generate_candidates(
            (TranscriptSegment(50, 56, "大家在讨论擦边主播这个现象"),),
            duration=300,
            count=1,
        )
        self.assertIn("擦边主播", candidates[0].topics)
        self.assertIn("擦边主播", candidates[0].title)

    def test_combined_topic_can_be_grounded_across_neighboring_subtitles(self):
        candidates = generate_candidates(
            (
                TranscriptSegment(50, 54, "最近有个主播引起讨论"),
                TranscriptSegment(55, 59, "后面又聊到了擦边现象"),
            ),
            duration=300,
            count=1,
        )
        self.assertIn("擦边主播", candidates[0].topics)
        self.assertTrue("主播" in candidates[0].evidence or "擦边" in candidates[0].evidence)

    def test_reference_style_title_uses_streamer_and_real_high_impact_words(self):
        candidates = generate_candidates(
            (TranscriptSegment(50, 58, "我第一次失业以后才发现找工作真的很难"),),
            duration=300,
            count=1,
            streamer="测试主播",
        )
        self.assertTrue(candidates[0].title.startswith("测试主播直言失业："))
        self.assertIn("第一次失业", candidates[0].title)
        self.assertTrue(candidates[0].cover_title)
        self.assertNotIn("测试主播", candidates[0].cover_title)

    def test_broad_topic_discovery_is_not_limited_to_requested_hotwords(self):
        candidates = generate_candidates(
            (TranscriptSegment(50, 58, "这个游戏的匹配机制真的离谱，普通玩家根本玩不下去"),),
            duration=300,
            count=1,
            streamer="测试主播",
        )
        self.assertIn("游戏体验", candidates[0].topics)
        self.assertIn("匹配机制", candidates[0].title)

    def test_clear_viewpoint_title_is_longer_but_remains_grounded(self):
        candidates = generate_candidates(
            (
                TranscriptSegment(50, 58, "我觉得月薪三千根本不能接受"),
                TranscriptSegment(59, 67, "每天加班到半夜，完全没有自己的生活"),
            ),
            duration=300,
            count=1,
            streamer="测试主播",
        )
        title = candidates[0].title
        self.assertIn("测试主播直言", title)
        self.assertIn("月薪三千", title)
        self.assertGreaterEqual(len(title), 28)
        self.assertLessEqual(len(title), 80)

    def test_story_hook_can_be_selected_without_a_predefined_topic(self):
        candidates = generate_candidates(
            (
                TranscriptSegment(40, 48, "今天先随便聊几句"),
                TranscriptSegment(700, 710, "当时我在车站等了三个小时，结果却没人来接我，真的很无语"),
            ),
            duration=1200,
            count=1,
            streamer="测试主播",
        )
        self.assertIn("车站等了三个小时", candidates[0].evidence)
        self.assertIn("讲起一段经历", candidates[0].title)
        self.assertIn("故事经历", candidates[0].signals)
        self.assertIn("数字信息", candidates[0].signals)

    def test_title_topic_is_tied_to_selected_evidence_not_other_window_chat(self):
        candidates = generate_candidates(
            (
                TranscriptSegment(50, 56, "前面随口提到有人躺平摆烂"),
                TranscriptSegment(100, 108, "你当时为什么会决定买一个房子呢"),
            ),
            duration=300,
            count=1,
            streamer="测试主播",
        )
        self.assertIn("消费与房子", candidates[0].topics)
        self.assertNotIn("躺平", candidates[0].topics)
        self.assertIn("为什么会决定买一个房子", candidates[0].evidence)

    def test_generates_reviewable_batch(self):
        segments = tuple(
            TranscriptSegment(start, start + 8, f"第 {index} 段普通对话")
            for index, start in enumerate(range(40, 3500, 300), start=1)
        )
        candidates = generate_candidates(segments, duration=3600)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(tuple(sorted(item.start for item in candidates)), tuple(item.start for item in candidates))
        self.assertTrue(all(MIN_CLIP_SECONDS <= item.duration <= MAX_CLIP_SECONDS for item in candidates))


if __name__ == "__main__":
    unittest.main()
