from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from bili_live_auto.config import RecordingSettings
from bili_live_auto.retention import RetentionManager
from bili_live_auto.upload_history import UploadHistoryStore


class RetentionTests(unittest.TestCase):
    def _old_file(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"recording")
        old = time.time() - 200 * 3600
        os.utime(path, (old, old))

    def test_safe_mode_deletes_uploaded_and_keeps_untracked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploaded = root / "123-streamer" / "uploaded.flv"
            untracked = root / "123-streamer" / "untracked.flv"
            self._old_file(uploaded)
            self._old_file(untracked)
            history = UploadHistoryStore(root / "data" / "history.json")
            item_id = history.create([str(uploaded)], "uploaded", "auto")
            history.update(item_id, "success", "ok", bvid="BV123")
            settings = RecordingSettings(
                watch_dir=root,
                retention_hours=168,
                delete_only_uploaded=True,
            )
            report = RetentionManager(settings, history).cleanup({123})
            self.assertFalse(uploaded.exists())
            self.assertTrue(untracked.exists())
            self.assertEqual(report.deleted_files, 1)
            self.assertEqual(report.retained_unuploaded, 1)

    def test_full_cleanup_still_excludes_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recording = root / "123-streamer" / "old.flv"
            protected = root / "data" / "123-test.flv"
            self._old_file(recording)
            self._old_file(protected)
            history = UploadHistoryStore(root / "data" / "history.json")
            settings = RecordingSettings(
                watch_dir=root,
                retention_hours=168,
                delete_only_uploaded=False,
            )
            report = RetentionManager(settings, history).cleanup({123})
            self.assertFalse(recording.exists())
            self.assertTrue(protected.exists())
            self.assertEqual(report.deleted_files, 1)


if __name__ == "__main__":
    unittest.main()
