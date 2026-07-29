from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


_CHINA_TIME_ZONE = ZoneInfo("Asia/Shanghai")


def china_day_bounds(day: date) -> tuple[datetime, datetime]:
    local_start = datetime.combine(day, time.min, tzinfo=_CHINA_TIME_ZONE)
    local_end = datetime.combine(
        day + timedelta(days=1),
        time.min,
        tzinfo=_CHINA_TIME_ZONE,
    )
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)
