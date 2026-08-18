from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bili_live_auto.online_source import (
    OnlineSourceError,
    build_download_command,
    validate_online_url,
)


class OnlineSourceTests(unittest.TestCase):
    def test_validate_online_url_requires_http(self):
        self.assertEqual(validate_online_url("https://www.bilibili.com/video/BV1xx"), "https://www.bilibili.com/video/BV1xx")
        with self.assertRaises(OnlineSourceError):
            validate_online_url("G:/录播/source.mp4")

    def test_download_command_is_single_video_local_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            command = build_download_command("yt-dlp", "https://example.com/video", Path(directory))
        self.assertIn("--no-playlist", command)
        self.assertIn("--merge-output-format", command)
        self.assertIn("mp4", command)
        self.assertNotIn("--write-subs", command)


if __name__ == "__main__":
    unittest.main()
