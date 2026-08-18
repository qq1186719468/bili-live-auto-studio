from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class LiveRoom:
    room_id: int
    short_id: int
    title: str
    streamer: str
    live_status: int
    live_time: str

    @property
    def is_live(self) -> bool:
        return self.live_status == 1

    @property
    def url(self) -> str:
        return f"https://live.bilibili.com/{self.room_id}"

    @property
    def event_key(self) -> str:
        start = self.live_time.strip() or "unknown"
        return f"{self.room_id}:{start}"


@dataclass(frozen=True, slots=True)
class Recording:
    event_key: str
    room_id: int
    path: str
    title: str
    streamer: str
    room_title: str
    room_url: str
    start_time: str
    recorded_at: str
    parts: tuple[str, ...] = ()

    @classmethod
    def from_room(
        cls,
        room: LiveRoom,
        path: str | list[str] | tuple[str, ...],
        now: datetime,
        event_key: str | None = None,
    ) -> "Recording":
        paths = (path,) if isinstance(path, str) else tuple(path)
        if not paths:
            raise ValueError("录像文件不能为空")
        raw_start = room.live_time.strip()
        try:
            parsed_start = datetime.fromisoformat(raw_start)
            if parsed_start.year < 2000:
                raise ValueError
            start_time = parsed_start.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            start_time = now.strftime("%Y-%m-%d %H:%M:%S")
        return cls(
            event_key=event_key or room.event_key,
            room_id=room.room_id,
            path=paths[0],
            title=room.title,
            streamer=room.streamer,
            room_title=room.title,
            room_url=room.url,
            start_time=start_time,
            recorded_at=now.isoformat(timespec="seconds"),
            parts=paths[1:],
        )

    @property
    def files(self) -> tuple[str, ...]:
        return (self.path, *tuple(self.parts))

    def template_context(self) -> dict[str, str | int]:
        return {
            "room_id": self.room_id,
            "streamer": self.streamer,
            "room_title": self.room_title,
            "room_url": self.room_url,
            "start_time": self.start_time,
            "recorded_at": self.recorded_at,
            "filename": self.path,
            "filenames": ", ".join(self.files),
        }
