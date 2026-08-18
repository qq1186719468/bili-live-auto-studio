from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from bili_live_auto.clip_renderer import (
    _edited_filter_complex,
    _safe_filename,
    _source_time_at_output_ratio,
    choose_sharpest_cover_frame,
    create_yellow_title_cover,
    write_candidate_subtitles,
)
from bili_live_auto.clipper import ClipCandidate, TranscriptSegment
from bili_live_auto.zh_simplify import to_simplified


class ClipRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = ClipCandidate(
            id="c01-000100",
            start=100,
            end=130,
            score=80,
            title="测试主播：第一次失业以后才发现找工作真的很难！",
            topics=("失业",),
            evidence="第一次失业以后才发现找工作真的很难",
        )

    def test_ass_subtitles_are_relative_and_bottom_aligned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.ass"
            count = write_candidate_subtitles(
                path,
                self.candidate,
                (
                    TranscriptSegment(99, 101, "片段开头"),
                    TranscriptSegment(105, 108, "这是一句很长很长的底部字幕用于换行测试"),
                    TranscriptSegment(140, 145, "范围外字幕"),
                ),
            )
            text = path.read_text(encoding="utf-8-sig")
            self.assertEqual(count, 2)
            self.assertIn("Alignment, MarginL", text)
            self.assertIn("0:00:00.00", text)
            self.assertIn(r"\N", text)
            self.assertNotIn("范围外字幕", text)

    def test_reordered_edit_ranges_remap_subtitles_to_final_timeline(self):
        candidate = ClipCandidate(
            id="a01-01-000100",
            start=100,
            end=260,
            score=92,
            title="测试标题",
            topics=("测试",),
            evidence="测试依据",
            origin="api",
            edit_ranges=((200, 260), (100, 160)),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edited.ass"
            count = write_candidate_subtitles(
                path,
                candidate,
                (
                    TranscriptSegment(105, 108, "原录像前段，成片后放"),
                    TranscriptSegment(205, 208, "原录像后段，成片先放"),
                    TranscriptSegment(170, 175, "已经删除的跑题内容"),
                ),
            )
            text = path.read_text(encoding="utf-8-sig")
            self.assertEqual(count, 2)
            self.assertIn("0:00:05.00,0:00:08.00", text)
            self.assertIn("0:01:05.00,0:01:08.00", text)
            self.assertLess(text.index("原录像后段"), text.index("原录像前段"))
            self.assertNotIn("已经删除", text)

    def test_edit_filter_concatenates_in_api_order_and_maps_cover_time(self):
        candidate = ClipCandidate(
            id="a01-01-000100",
            start=100,
            end=260,
            score=92,
            title="测试标题",
            topics=("测试",),
            evidence="测试依据",
            origin="api",
            edit_ranges=((200, 260), (100, 160)),
        )
        filter_complex, audio = _edited_filter_complex(candidate, "edited.ass", True, 100)
        self.assertIn("trim=start=100.000:end=160.000", filter_complex)
        self.assertIn("trim=start=0.000:end=60.000", filter_complex)
        self.assertIn("[ev0][ev1]concat=n=2:v=1:a=0", filter_complex)
        self.assertEqual(audio, "[editeda]")
        self.assertAlmostEqual(_source_time_at_output_ratio(candidate, 0.25), 230.0)
        self.assertAlmostEqual(_source_time_at_output_ratio(candidate, 0.75), 130.0)

    def test_cover_keeps_yellow_black_outline_title_style(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = root / "frame.jpg"
            output = root / "cover.jpg"
            Image.new("RGB", (1280, 720), (110, 120, 130)).save(frame)
            create_yellow_title_cover(frame, output, self.candidate.title)
            with Image.open(output) as image:
                self.assertEqual(image.size, (1280, 720))
                pixels = image.crop((50, 320, 1240, 560)).convert("RGB")
                self.assertTrue(any(red > 180 and green > 150 and blue < 100 for red, green, blue in pixels.getdata()))

    def test_cover_frame_selection_prefers_sharper_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sharp = root / "sharp.jpg"
            blurred = root / "blurred.jpg"
            image = Image.new("RGB", (640, 360), "white")
            draw = ImageDraw.Draw(image)
            for x in range(0, 640, 20):
                draw.rectangle((x, 0, x + 9, 359), fill="black")
            image.save(sharp)
            image.filter(ImageFilter.GaussianBlur(8)).save(blurred)
            self.assertEqual(choose_sharpest_cover_frame((blurred, sharp)), sharp)

    def test_subtitle_text_is_simplified(self):
        self.assertEqual(to_simplified("這是一個測試"), "这是一个测试")

    def test_long_source_folder_truncation_never_ends_with_windows_trimmed_space(self):
        value = _safe_filename("A" * 47 + " 中文名")[:48].rstrip(" .")
        self.assertEqual(len(value), 47)
        self.assertFalse(value.endswith((" ", ".")))


if __name__ == "__main__":
    unittest.main()
