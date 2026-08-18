from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bili_live_auto.upload_history import UploadHistoryStore


class UploadHistoryTests(unittest.TestCase):
    def test_discovery_deduplicates_and_persists_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "record-123.flv"
            video.write_bytes(b"video")
            store = UploadHistoryStore(root / "history.json")
            self.assertEqual(store.discover_files([video]), 1)
            self.assertEqual(store.discover_files([video]), 0)
            item = store.items()[0]
            self.assertEqual(item["status"], "untracked")
            store.update(item["id"], "success", "ok", bvid="BV123")
            loaded = UploadHistoryStore(root / "history.json").items()[0]
            self.assertEqual(loaded["status"], "success")
            self.assertEqual(loaded["bvid"], "BV123")
            self.assertTrue(store.files_are_claimed([str(video)]))
            self.assertIsNone(store.session_bvid("session-1"))

    def test_clip_discovery_is_marked_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clip = root / "clip.mp4"
            clip.write_bytes(b"video")
            store = UploadHistoryStore(root / "history.json")
            self.assertEqual(store.discover_files([clip], source="clip_scan"), 1)
            item = store.items()[0]
            self.assertEqual(item["source"], "clip_scan")
            self.assertIn("切片成片", item["message"])

    def test_session_bvid_links_later_parts_to_same_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = UploadHistoryStore(root / "history.json")
            first = store.create(["part1.flv"], "live", "auto", session_key="room:start")
            store.update(first, "success", "ok", bvid="BV123")
            second = store.create(["part2.flv"], "live", "auto", session_key="room:start")
            self.assertEqual(store.session_bvid("room:start"), "BV123")
            self.assertFalse(store.session_waiting_for_first("room:start", second))

    def test_forced_exit_upload_is_marked_as_result_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = UploadHistoryStore(root / "history.json")
            item_id = store.create(["part.flv"], "live", "manual", status="uploading", message="正在调用 biliup")
            self.assertEqual(store.recover_interrupted_uploads(), 1)
            item = store.get(item_id)
            assert item is not None
            self.assertEqual(item["status"], "interrupted")
            self.assertIn("结果待确认", item["message"])
            self.assertTrue(store.files_are_claimed(["part.flv"]))

    def test_ledger_entry_can_be_edited_and_deleted_without_deleting_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            video.write_bytes(b"video")
            store = UploadHistoryStore(root / "history.json")
            item_id = store.create([str(video)], "旧标题", "clip_scan", status="success")
            self.assertTrue(
                store.edit(
                    item_id,
                    files=[str(video)],
                    title="新标题",
                    source="clip",
                    status="failed",
                    message="准备重新投稿",
                    bvid="",
                )
            )
            edited = store.get(item_id)
            assert edited is not None
            self.assertEqual(edited["title"], "新标题")
            self.assertEqual(edited["status"], "failed")
            self.assertEqual(store.delete([item_id]), 1)
            self.assertIsNone(store.get(item_id))
            self.assertTrue(video.is_file())


if __name__ == "__main__":
    unittest.main()
