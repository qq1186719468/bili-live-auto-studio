from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from bili_live_auto.api import BilibiliLiveAPI, RoomIdentityMismatchError, streamer_names_match
from bili_live_auto.app import Application
from bili_live_auto.config import RecordingSettings, UploadSettings, load_config
from bili_live_auto.models import LiveRoom, Recording
from bili_live_auto.recorder import Recorder, safe_filename
from bili_live_auto.directory_recorder import DirectoryRecorder
from bili_live_auto.gui import (
    clip_runtime_status,
    clip_upload_title,
    find_generated_clip_cover,
    find_local_recordings,
    find_videos_in_directory,
    recording_file_title,
    recording_identity,
    recording_streamer_name,
    write_config,
)
from bili_live_auto.recorder_config import RecorderRoomMismatchError, read_recorder_room_ids, validate_recorder_room
from bili_live_auto.state import StateStore
from bili_live_auto.uploader import (
    Uploader,
    extract_upload_percent,
    render,
    scheduled_publish_timestamp,
    summarize_upload_failure,
)


class FakeResponse:
    def __init__(self, value: dict) -> None:
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.value).encode()


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.room = LiveRoom(123, 0, "测试直播", "测试主播", 1, "2026-08-17 10:00:00")
        self.recording = Recording.from_room(self.room, "C:/video/test.mp4", datetime(2026, 8, 17, 12, 0))

    def test_safe_filename_removes_windows_characters(self):
        self.assertEqual(safe_filename('a<b>:c/d\\e|f?g*'), "a_b__c_d_e_f_g_")

    def test_local_ledger_scan_finds_all_room_directories_and_filters_small_fragments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_room = root / "123456-主播甲"
            second_room = root / "987654-主播乙"
            first_room.mkdir()
            second_room.mkdir()
            small = first_room / "录制-123456-小分段.flv"
            normal = first_room / "录制-123456-完整分段.flv"
            other_room = second_room / "录制-987654-录像.mp4"
            top_level = root / "临时导出的直播录像.mkv"
            small.write_bytes(b"x" * 128)
            normal.write_bytes(b"n" * (1024 * 1024))
            other_room.write_bytes(b"y" * (1024 * 1024 + 1))
            top_level.write_bytes(b"z" * (1024 * 1024 + 2))
            ignored_data = root / "data" / "upload-test.mp4"
            ignored_data.parent.mkdir()
            ignored_data.write_bytes(b"d" * 256)
            ignored_clip = root / "智能切片成片" / "clip.mp4"
            ignored_clip.parent.mkdir()
            ignored_clip.write_bytes(b"c" * (1024 * 1024 + 3))

            result = set(find_local_recordings(root))
            clip_result = find_videos_in_directory(root / "智能切片成片")

            self.assertEqual(result, {normal.resolve(), other_room.resolve(), top_level.resolve()})
            self.assertEqual(clip_result, [ignored_clip.resolve()])

    def test_recording_identity_comes_from_room_folder_not_current_config(self):
        path = Path(r"C:\recordings\26188314-战狼铠甲佳宝\录制-26188314-20260817-214019-250-宇宙家里蹲.flv")
        self.assertEqual(recording_identity(path), (26188314, "战狼铠甲佳宝"))
        self.assertEqual(recording_streamer_name(path, "其他主播"), "战狼铠甲佳宝")
        self.assertEqual(recording_file_title(path), "宇宙家里蹲")

    def test_recording_identity_accepts_underscore_folder_and_fallback(self):
        path = Path(r"C:\recordings\22747736_主播甲\record.flv")
        self.assertEqual(recording_identity(path), (22747736, "主播甲"))
        self.assertEqual(recording_streamer_name(Path(r"C:\recordings\misc\record.flv"), "配置主播"), "配置主播")
        self.assertEqual(recording_file_title(Path(r"C:\recordings\misc\record.flv"), "文件标题"), "文件标题")

    def test_recorder_command_contains_room_and_output(self):
        settings = RecordingSettings(cookie_file=Path("cookies.txt"), extra_args=("--verbose",))
        command = Recorder(settings, Path("out")).build_command(self.room, Path("out/test.mp4"))
        self.assertEqual(command[-1], "https://live.bilibili.com/123")
        self.assertIn("cookies.txt", command)
        self.assertIn("--verbose", command)

    def test_upload_command_uses_safe_default_reprint_metadata(self):
        settings = UploadSettings(enabled=True, cookie_file=Path("cookie.json"))
        command = Uploader(settings).build_command(self.recording)
        self.assertEqual(command[0], "biliup")
        self.assertEqual(command[command.index("--submit") + 1], "web")
        self.assertIn("--copyright", command)
        self.assertEqual(command[command.index("--copyright") + 1], "2")
        self.assertIn("测试主播直播录像", command[command.index("--title") + 1])

    def test_upload_command_supports_schedule_and_visibility_options(self):
        settings = UploadSettings(
            enabled=True,
            cookie_file=Path("cookie.json"),
            publish_at="2030-01-02 15:04",
            dynamic="新稿件：{streamer}",
            is_only_self=True,
            no_reprint=True,
            charging_pay=True,
        )
        command = Uploader(settings).build_command(self.recording)
        self.assertIn("--dtime", command)
        self.assertEqual(command[command.index("--dynamic") + 1], "新稿件：测试主播")
        self.assertEqual(command[command.index("--is-only-self") + 1], "1")
        self.assertEqual(command[command.index("--no-reprint") + 1], "1")
        self.assertEqual(command[command.index("--charging-pay") + 1], "1")

    def test_schedule_requires_four_hours_and_uses_beijing_timezone(self):
        self.assertIsNone(scheduled_publish_timestamp("", now=0))
        self.assertEqual(scheduled_publish_timestamp("1970-01-02 12:00", now=0), 100800)
        with self.assertRaisesRegex(Exception, "至少在当前时间 4 小时"):
            scheduled_publish_timestamp("1970-01-01 03:00", now=0)

    def test_upload_command_can_include_clip_cover(self):
        cover = Path("C:/video/cover.jpg")
        command = Uploader(UploadSettings(enabled=True)).build_command(self.recording, cover=cover)
        self.assertEqual(command[command.index("--cover") + 1], str(cover))

    def test_extracts_biliup_progress_percent(self):
        self.assertEqual(extract_upload_percent("uploading 37.5% 12MiB/s"), 37.5)
        self.assertEqual(extract_upload_percent("progress: 100%"), 100.0)
        self.assertIsNone(extract_upload_percent("waiting for server"))

    def test_upload_failure_keeps_actionable_biliup_output(self):
        message = summarize_upload_failure(["upload start", "Error: account not logged in"], 1)
        self.assertIn("退出码：1", message)
        self.assertIn("account not logged in", message)

    def test_upload_failure_redacts_credential_like_output(self):
        message = summarize_upload_failure(["token=very-secret-value"], 1)
        self.assertNotIn("very-secret-value", message)
        self.assertIn("<已隐藏>", message)

    def test_clip_title_does_not_add_timestamp(self):
        value = clip_upload_title("切片标题")
        self.assertEqual(value, "切片标题")
        self.assertNotIn("2026", value)

    def test_clip_title_stays_within_bilibili_limit(self):
        value = clip_upload_title("很长的切片标题" * 20)
        self.assertLessEqual(len(value), 80)

    def test_clip_title_removes_generated_filename_prefix_and_hash(self):
        value = clip_upload_title("a04-01-006172-真正的切片标题-a8f27444")
        self.assertEqual(value, "真正的切片标题")

    def test_generated_clip_cover_is_found_next_to_video(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "a01-01-000001-标题-deadbeef.mp4"
            cover = video.with_suffix(".jpg")
            video.write_bytes(b"video")
            cover.write_bytes(b"jpg")
            self.assertEqual(find_generated_clip_cover(video), cover)

    def test_clip_runtime_detects_retained_local_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".clip-venv-standalone" / "Scripts").mkdir(parents=True)
            (root / ".clip-venv-standalone" / "Scripts" / "python.exe").write_bytes(b"python")
            (root / "models" / "faster-whisper-small").mkdir(parents=True)
            (root / "models" / "faster-whisper-small" / "model.bin").write_bytes(b"model")
            ready, _message = clip_runtime_status(root)
            self.assertTrue(ready)

    def test_append_command_targets_existing_submission(self):
        command = Uploader(UploadSettings(enabled=True)).build_append_command(self.recording, "BV123abc")
        self.assertIn("append", command)
        self.assertEqual(command[command.index("--vid") + 1], "BV123abc")
        self.assertIn(self.recording.path, command)

    def test_multi_part_recording_is_one_upload(self):
        recording = Recording.from_room(
            self.room,
            ["C:/video/part1.mp4", "C:/video/part2.mp4"],
            datetime(2026, 8, 17, 12, 0),
        )
        command = Uploader(UploadSettings(enabled=True)).build_command(recording)
        self.assertLess(command.index("C:/video/part1.mp4"), command.index("--line"))
        self.assertLess(command.index("C:/video/part2.mp4"), command.index("--line"))

    def test_segment_keys_change_but_keep_same_live_session(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "part.flv"
            video.write_bytes(b"part-one")
            first = Application._segment_event_key(self.room, [str(video)])
            video.write_bytes(b"part-two-is-different")
            second = Application._segment_event_key(self.room, [str(video)])
            self.assertNotEqual(first, second)
            first_recording = Recording.from_room(self.room, str(video), datetime.now(), event_key=first)
            second_recording = Recording.from_room(self.room, str(video), datetime.now(), event_key=second)
            self.assertEqual(Application._session_key(first_recording), Application._session_key(second_recording))

    def test_directory_recorder_snapshot_finds_supported_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "record.mp4"
            video.write_bytes(b"video")
            (root / "note.txt").write_text("ignore")
            settings = RecordingSettings(watch_dir=root)
            snapshot = DirectoryRecorder(settings).snapshot()
            self.assertEqual(list(snapshot), [video.resolve()])

    def test_directory_recorder_detects_manually_stopped_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "record-123-manual-stop.flv"
            video.write_bytes(b"start")
            settings = RecordingSettings(
                watch_dir=root,
                min_file_size_mb=0,
                local_scan_seconds=0.01,
                manual_stop_stable_seconds=0.05,
            )
            recorder = DirectoryRecorder(settings)

            def finish_file():
                time.sleep(0.03)
                video.write_bytes(b"completed recording")

            writer = threading.Thread(target=finish_file)
            writer.start()
            result = recorder.record(self.room, lambda: True, threading.Event(), poll_seconds=30)
            writer.join()
            self.assertEqual(result, [video.resolve()])

    def test_template_rejects_unknown_fields(self):
        with self.assertRaises(Exception):
            render("{missing}", self.recording)

    def test_state_round_trip_and_mark_uploaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(path)
            store.add_pending(self.recording)
            self.assertTrue(store.has_recorded(self.recording.event_key))
            self.assertEqual(store.pending(), (self.recording,))
            store.mark_uploaded(self.recording.event_key)
            self.assertEqual(StateStore(path).pending(), ())

    def test_api_resolves_canonical_room(self):
        values = iter(
            [
                {"code": 0, "data": {"room_id": 123, "short_id": 7, "live_status": 1, "exist": True}},
                {"code": 0, "data": {"title": "标题", "uid": 42, "live_status": 1, "live_time": "now"}},
                {"code": 0, "data": {"info": {"uname": "主播"}}},
            ]
        )

        def opener(*_args, **_kwargs):
            return FakeResponse(next(values))

        room = BilibiliLiveAPI(opener=opener).get_room(7, "主播")
        self.assertEqual(room.room_id, 123)
        self.assertTrue(room.is_live)
        self.assertEqual(room.streamer, "主播")

    def test_zero_live_time_is_treated_as_missing(self):
        values = iter(
            [
                {"code": 0, "data": {"room_id": 123, "exist": True}},
                {"code": 0, "data": {"title": "标题", "uid": 42, "live_status": 0, "live_time": "0000-00-00 00:00:00"}},
                {"code": 0, "data": {"info": {"uname": "主播"}}},
            ]
        )

        def opener(*_args, **_kwargs):
            return FakeResponse(next(values))

        room = BilibiliLiveAPI(opener=opener).get_room(123, "主播")
        self.assertEqual(room.live_time, "")

    def test_api_rejects_room_and_streamer_mismatch(self):
        values = iter(
            [
                {"code": 0, "data": {"room_id": 22747736, "exist": True}},
                {"code": 0, "data": {"title": "标题", "uid": 42, "live_status": 1}},
                {"code": 0, "data": {"info": {"uname": "不死鸟总监"}}},
            ]
        )

        def opener(*_args, **_kwargs):
            return FakeResponse(next(values))

        with self.assertRaises(RoomIdentityMismatchError) as caught:
            BilibiliLiveAPI(opener=opener).get_room(22747736, "战狼铠甲佳宝")
        self.assertEqual(caught.exception.actual_name, "不死鸟总监")

    def test_streamer_name_comparison_normalizes_width_case_and_spaces(self):
        self.assertTrue(streamer_names_match(" Ａbc 主播 ", "abc主播"))

    def test_recorder_config_room_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "version": 3,
                        "rooms": [
                            {"RoomId": {"HasValue": True, "Value": 26188314}},
                            {"RoomId": {"HasValue": True, "Value": 22747736}},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(read_recorder_room_ids(root), {26188314, 22747736})
            validate_recorder_room(root, 22747736)
            with self.assertRaises(RecorderRoomMismatchError):
                validate_recorder_room(root, 999)

    def test_load_config_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[app]\nwork_dir="./runtime"\n[recording]\n[upload]\ncookie_file="./secret/c.json"\n'
                '[[rooms]]\nid=123\nname="主播"\n',
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.app.work_dir, Path(directory) / "runtime")
            self.assertEqual(config.upload.cookie_file, Path(directory) / "secret" / "c.json")
            self.assertEqual(config.rooms[0].id, 123)
            self.assertEqual(config.recording.backend, "bililiverecorder")
            self.assertEqual(config.clip_upload.cookie_file, Path(directory) / "secret" / "c.json")
            self.assertEqual(config.clip_upload.tags, config.upload.tags)

    def test_gui_writes_loadable_client_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "bili_live_auto.toml"
            write_config(
                {
                    "room_id": 26188314,
                    "streamer": "测试主播",
                    "work_dir": str(root),
                    "upload_enabled": False,
                    "biliup_executable": "biliup",
                    "cookie_file": str(root / "secrets" / "cookies.json"),
                    "tid": 171,
                    "copyright": 2,
                    "tags": "直播录像, 录播",
                    "title": "{streamer} {start_time}",
                    "description": "直播间：{room_url}\n标题：{room_title}",
                    "clip_biliup_executable": "clip-biliup",
                    "clip_cookie_file": str(root / "secrets" / "clip-cookies.json"),
                    "clip_tid": 138,
                    "clip_copyright": 1,
                    "clip_tags": "直播切片, 高能切片",
                    "clip_description": "切片来源：{room_url}",
                    "clip_ai_enabled": True,
                    "clip_ai_base_url": "https://relay.example.com",
                    "clip_ai_model": "gpt-test",
                    "clip_ai_protocol": "responses",
                    "clip_ai_key_file": str(root / "secrets" / "clip-ai-key.txt"),
                    "clip_ai_timeout_seconds": 120,
                    "clip_ai_chunk_minutes": 30,
                },
                path,
            )
            config = load_config(path)
            self.assertEqual(config.rooms[0].id, 26188314)
            self.assertEqual(config.upload.tags, ("直播录像", "录播"))
            self.assertFalse(config.upload.enabled)
            self.assertEqual(config.app.theme, "dark")
            self.assertEqual(config.clip_upload.executable, "clip-biliup")
            self.assertEqual(config.clip_upload.cookie_file, root / "secrets" / "clip-cookies.json")
            self.assertEqual(config.clip_upload.tid, 138)
            self.assertEqual(config.clip_upload.copyright, 1)
            self.assertEqual(config.clip_upload.tags, ("直播切片", "高能切片"))
            self.assertTrue(config.clip_ai.enabled)
            self.assertEqual(config.clip_ai.base_url, "https://relay.example.com")
            self.assertEqual(config.clip_ai.model, "gpt-test")
            self.assertEqual(config.clip_ai.protocol, "responses")
            self.assertEqual(config.clip_ai.api_key_file, root / "secrets" / "clip-ai-key.txt")


if __name__ == "__main__":
    unittest.main()
