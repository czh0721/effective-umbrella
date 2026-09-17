"""统一的时间感知。

服务器时区是 UTC，但用户与分身都生活在本地时区（默认东八区）。所有「现在是
几点、今天是哪天」的判断都必须走这里，避免把 UTC 时间当成当地时间说给用户听。

时区可用环境变量 ``PERSONA_TIMEZONE`` 覆盖，默认 ``Asia/Shanghai``；系统缺少
tzdata 时退回固定的 UTC+8。
"""

import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ 自带 zoneinfo
    ZoneInfo = None

DEFAULT_TIMEZONE = "Asia/Shanghai"
_FALLBACK = timezone(timedelta(hours=8))

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def timezone_name() -> str:
    return (os.getenv("PERSONA_TIMEZONE") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE


def local_tz() -> timezone:
    if ZoneInfo is not None:
        try:
            return ZoneInfo(timezone_name())
        except Exception:  # noqa: BLE001 - 时区数据缺失时退回固定偏移
            pass
    return _FALLBACK


def now_local() -> datetime:
    """当前本地时间（带时区）。"""
    return datetime.now(local_tz())


def period(hour: int) -> str:
    """把小时映射成口语化的时段。"""
    if hour < 6:
        return "深夜"
    if hour < 11:
        return "早上"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"


def describe(now: datetime | None = None) -> str:
    """例如「2026年9月17日 星期四 晚上 19:50」。"""
    moment = now or now_local()
    return (
        f"{moment.year}年{moment.month}月{moment.day}日 "
        f"{_WEEKDAYS[moment.weekday()]} {period(moment.hour)} {moment:%H:%M}"
    )


def block(now: datetime | None = None) -> str:
    """注入聊天系统提示的时间区块，让分身知道自己身处何时。"""
    return (
        "【当下时间】\n"
        f"现在是 {describe(now)}。\n"
        "你要清楚今天是哪一天、现在是什么时段（早上/中午/下午/晚上/深夜）、是不是周末，"
        "并据此自然地决定语气、问候和聊天话题。除非对方主动问起，否则不要直接报出日期或时间。"
    )


def parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _humanize(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    if seconds < 60:
        return "刚刚"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}分钟前"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}小时前"
    days = hours / 24
    if days < 2:
        return "昨天"
    if days < 30:
        return f"{int(days)}天前"
    return f"{int(days / 30)}个月前"


def gap_hint(last_iso: str | None, now: datetime | None = None, min_seconds: float = 3600) -> str:
    """根据上一轮对话时间给出「隔了多久」的提示。

    间隔不足 ``min_seconds``（默认 1 小时）时返回空串，避免正常连聊时反复注入
    无意义的提示。
    """
    parsed = parse_iso(last_iso) if last_iso else None
    if parsed is None:
        return ""
    moment = now or now_local()
    delta = (moment - parsed).total_seconds()
    if delta < min_seconds:
        return ""
    phrase = _humanize(delta)
    if delta >= 48 * 3600:
        return (
            f"【时间间隔】\n对方上一次说话已经是{phrase}了，中间隔了挺久。"
            "请自然地带出时间感（比如久别重逢的语气、问问近况），不要生硬地报时间。"
        )
    return f"【时间间隔】\n对方上一次说话是在{phrase}。请自然地意识到这段间隔。"
