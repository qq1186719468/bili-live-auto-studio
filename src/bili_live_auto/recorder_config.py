from __future__ import annotations

import json
from pathlib import Path


class RecorderConfigError(RuntimeError):
    pass


class RecorderRoomMismatchError(RecorderConfigError):
    def __init__(self, room_id: int, configured_rooms: set[int]) -> None:
        self.room_id = room_id
        self.configured_rooms = configured_rooms
        rooms = "、".join(map(str, sorted(configured_rooms))) or "无"
        super().__init__(
            f"房间号 {room_id} 尚未加入 B站录播姬；录播姬当前房间：{rooms}。"
            "请先在录播姬中添加并启用该房间"
        )


def read_recorder_room_ids(work_dir: Path) -> set[int]:
    path = work_dir / "config.json"
    if not path.is_file():
        raise RecorderConfigError(f"录播姬配置不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecorderConfigError(f"无法读取录播姬配置 {path}：{exc}") from exc
    rooms = data.get("rooms", []) if isinstance(data, dict) else []
    if not isinstance(rooms, list):
        raise RecorderConfigError(f"录播姬配置中的 rooms 格式无效：{path}")
    result: set[int] = set()
    for room in rooms:
        if not isinstance(room, dict):
            continue
        value = room.get("RoomId")
        if isinstance(value, dict):
            if value.get("HasValue") is False:
                continue
            value = value.get("Value")
        try:
            room_id = int(value)
        except (TypeError, ValueError):
            continue
        if room_id > 0:
            result.add(room_id)
    return result


def validate_recorder_room(work_dir: Path, room_id: int) -> None:
    rooms = read_recorder_room_ids(work_dir)
    if room_id not in rooms:
        raise RecorderRoomMismatchError(room_id, rooms)
