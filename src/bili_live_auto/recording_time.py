from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_FILENAME_TIMES = (
    re.compile(
        r"(?<!\d)(?P<year>20\d{2})(?P<month>\d{2})(?P<day>\d{2})[-_]"
        r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})(?:[-_]\d{3})?(?!\d)"
    ),
    re.compile(
        r"(?<!\d)(?P<year>20\d{2})[-_.](?P<month>\d{2})[-_.](?P<day>\d{2})[T _-]"
        r"(?P<hour>\d{2})[:_.-](?P<minute>\d{2})[:_.-](?P<second>\d{2})(?!\d)"
    ),
)


def _time_from_filename(path: Path) -> datetime | None:
    match = next((value for pattern in _FILENAME_TIMES if (value := pattern.search(path.stem))), None)
    if not match:
        return None
    try:
        return datetime(
            int(match["year"]),
            int(match["month"]),
            int(match["day"]),
            int(match["hour"]),
            int(match["minute"]),
            int(match["second"]),
        )
    except ValueError:
        return None


def recording_start_time(files: list[str] | tuple[str, ...], fallback: datetime | None = None) -> datetime:
    paths = [Path(value) for value in files]
    filename_times = [value for path in paths if (value := _time_from_filename(path)) is not None]
    if filename_times:
        return min(filename_times)
    modified_times: list[datetime] = []
    for path in paths:
        try:
            modified_times.append(datetime.fromtimestamp(path.stat().st_mtime))
        except OSError:
            continue
    return min(modified_times) if modified_times else (fallback or datetime.now())
