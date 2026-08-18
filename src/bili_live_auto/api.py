from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import LiveRoom


class BilibiliAPIError(RuntimeError):
    pass


class RoomIdentityMismatchError(BilibiliAPIError):
    def __init__(self, room_id: int, configured_name: str, actual_name: str) -> None:
        self.room_id = room_id
        self.configured_name = configured_name
        self.actual_name = actual_name
        super().__init__(
            f"房间号 {room_id} 实际主播为“{actual_name}”，与配置的“{configured_name}”不一致；"
            "请点击“查询房间”核对并更新主播名称"
        )


def normalize_streamer_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", "", value).casefold()


def streamer_names_match(configured_name: str, actual_name: str) -> bool:
    return normalize_streamer_name(configured_name) == normalize_streamer_name(actual_name)


class BilibiliLiveAPI:
    API_ROOT = "https://api.live.bilibili.com"

    def __init__(self, opener: Callable[..., Any] = urlopen, timeout: float = 10.0) -> None:
        self._opener = opener
        self.timeout = timeout

    def _get(self, endpoint: str, **params: int) -> dict[str, Any]:
        url = f"{self.API_ROOT}{endpoint}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 bili-live-auto/0.1",
                "Referer": "https://live.bilibili.com/",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise BilibiliAPIError(f"请求直播接口失败：{exc}") from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            message = payload.get("message", "未知错误") if isinstance(payload, dict) else "响应格式错误"
            raise BilibiliAPIError(f"直播接口返回错误：{message}")
        result = payload.get("data")
        if not isinstance(result, dict):
            raise BilibiliAPIError("直播接口缺少 data")
        return result

    def get_room(self, room_id: int, configured_name: str = "") -> LiveRoom:
        initial = self._get("/room/v1/Room/room_init", id=room_id)
        canonical_id = int(initial.get("room_id") or room_id)
        if not initial.get("exist", True):
            raise BilibiliAPIError(f"直播间不存在：{room_id}")
        info = self._get("/room/v1/Room/get_info", room_id=canonical_id)
        configured_name = configured_name.strip()
        actual_streamer = ""
        try:
            anchor = self._get("/live_user/v1/UserInfo/get_anchor_in_room", roomid=canonical_id)
            anchor_info = anchor.get("info", {})
            if isinstance(anchor_info, dict):
                actual_streamer = str(anchor_info.get("uname") or "").strip()
        except BilibiliAPIError:
            if configured_name:
                raise BilibiliAPIError(f"暂时无法核验房间 {room_id} 对应的真实主播，请稍后重试")
        actual_streamer = actual_streamer or f"主播{info.get('uid', '')}"
        if configured_name and not streamer_names_match(configured_name, actual_streamer):
            raise RoomIdentityMismatchError(room_id, configured_name, actual_streamer)
        live_time = str(info.get("live_time") or "").strip()
        if live_time.startswith("0000-00-00"):
            live_time = ""
        return LiveRoom(
            room_id=canonical_id,
            short_id=int(initial.get("short_id") or 0),
            title=str(info.get("title") or "未命名直播"),
            streamer=actual_streamer,
            live_status=int(info.get("live_status") or initial.get("live_status") or 0),
            live_time=live_time,
        )
