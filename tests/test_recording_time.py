from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from bili_live_auto.recording_time import recording_start_time


class RecordingTimeTests(unittest.TestCase):
    def test_extracts_iso_style_live_recording_filename_time(self):
        value = recording_start_time(
            ("P1-\u4f73\u5b9d2026-05-21T22_06_51.mp4",),
            fallback=datetime(2030, 1, 1),
        )
        self.assertEqual(value, datetime(2026, 5, 21, 22, 6, 51))

    def test_extracts_bililive_recorder_filename_time(self):
        value = recording_start_time(
            [r"C:\test-recordings\123456-主播\录制-123456-20260817-050619-408-标题.flv"]
        )
        self.assertEqual(value, datetime(2026, 8, 17, 5, 6, 19))

    def test_multiple_parts_use_earliest_time(self):
        files = [
            "录制-22747736-20260817-050619-408-A.flv",
            "录制-22747736-20260817-043411-091-B.flv",
        ]
        self.assertEqual(recording_start_time(files), datetime(2026, 8, 17, 4, 34, 11))

    def test_falls_back_to_file_modified_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.flv"
            path.write_bytes(b"video")
            stamp = datetime(2026, 8, 17, 3, 2, 1).timestamp()
            os.utime(path, (stamp, stamp))
            self.assertEqual(recording_start_time([str(path)]), datetime(2026, 8, 17, 3, 2, 1))


if __name__ == "__main__":
    unittest.main()
