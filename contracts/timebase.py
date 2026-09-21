"""T01 公共契约：时间口径（SPEC §5「所有时间含时区（Asia/Shanghai）」）。

实现要点：

- 时区用标准库固定偏移 ``UTC+08:00`` 表示 ``Asia/Shanghai``：Windows 无系统
  tz 数据库，``zoneinfo`` 需要额外 tzdata 包，固定偏移可保证测试与 CI 一致。
- 表格列存**原始字符串**：不带偏移的字符串按 ``Asia/Shanghai`` 解释；
  已是 ``datetime`` 的入参**必须带时区**（naive 直接拒绝，不猜）。
- ``occurred_at`` 精度到分：秒/微秒一律截断（不四舍五入），
  只有日期没有时:分 → 精度 ``date_only``（E002），完全为空 → ``missing``（E001）。

厂级口径（T04 增补，SPEC §0）：一次轮换一张单，``start``/``end`` 即**驻场期边界**
（可长达数十天）。越界判定（E003 依据）对 ``[start, end]`` **整体区间**做，
**不按单日切分**——跨零点相连的日历日**均属期内**。日历日粒度见
:func:`calendar_days_in_window` / :func:`covers_calendar_day` / :func:`window_span_days`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .errors import ContractViolation

#: Asia/Shanghai 固定偏移（UTC+08:00）。
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")

PRECISION_MISSING = "missing"
PRECISION_DATE_ONLY = "date_only"
PRECISION_MINUTE = "minute"

#: 表格字符串允许的写法（秒与偏移可有可无）。
_STRING_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d %H:%M%z",
    "%Y-%m-%dT%H:%M%z",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S%z",
)


@dataclass(frozen=True)
class TimeParseResult:
    """``occurred_at`` 解析结果：值（可为 None）+ 原始精度 + 原始文本。"""

    value: datetime | None
    precision: str
    raw: str

    @property
    def is_missing(self) -> bool:
        return self.precision == PRECISION_MISSING

    @property
    def is_date_only(self) -> bool:
        return self.precision == PRECISION_DATE_ONLY

    @property
    def is_minute_precise(self) -> bool:
        return self.precision == PRECISION_MINUTE

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": None if self.value is None else format_minute(self.value),
            "precision": self.precision,
            "raw": self.raw,
        }


def now_shanghai() -> datetime:
    """当前时间（Asia/Shanghai，截断到分）。测试请注入固定时钟，勿依赖它。"""
    return truncate_to_minute(datetime.now(tz=SHANGHAI))


def truncate_to_minute(moment: datetime) -> datetime:
    """截断到分（秒与微秒清零），保留时区。"""
    return moment.replace(second=0, microsecond=0)


def ensure_aware(moment: datetime, *, field: str = "时间") -> datetime:
    """要求带时区；naive 直接拒绝（不静默补时区，避免口径漂移）。"""
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ContractViolation(f"{field} 必须包含时区（Asia/Shanghai 用 +08:00 表示）")
    return moment


def to_shanghai(moment: datetime) -> datetime:
    """换算到 Asia/Shanghai。"""
    return ensure_aware(moment).astimezone(SHANGHAI)


def format_minute(moment: datetime | None) -> str:
    """稳定文本格式，便于清单与断言。"""
    if moment is None:
        return ""
    return moment.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")


def parse_time_text(raw: Any, *, field: str = "时间") -> TimeParseResult:
    """解析表格里的时间文本；无法解析抛 :class:`ContractViolation`。"""
    if raw is None:
        return TimeParseResult(None, PRECISION_MISSING, "")
    if isinstance(raw, datetime):
        aware = ensure_aware(raw, field=field)
        return TimeParseResult(truncate_to_minute(aware), PRECISION_MINUTE, aware.isoformat())
    if isinstance(raw, date):
        # 有日期无时:分（date 对象）
        return TimeParseResult(None, PRECISION_DATE_ONLY, raw.isoformat())
    text = str(raw).strip()
    if not text:
        return TimeParseResult(None, PRECISION_MISSING, "")
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return TimeParseResult(None, PRECISION_DATE_ONLY, text)
    for fmt in _STRING_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SHANGHAI)
        return TimeParseResult(truncate_to_minute(parsed), PRECISION_MINUTE, text)
    raise ContractViolation(f"{field} 无法解析: {text!r}（支持 2026-01-02 08:30 / ISO 8601 及秒、偏移）")


def parse_occurred_at(raw: Any) -> TimeParseResult:
    """``occurred_at`` 专用解析（口径与 :func:`parse_time_text` 相同）。"""
    return parse_time_text(raw, field="occurred_at")


def in_window(moment: datetime, start: datetime | None, end: datetime | None) -> bool:
    """闭区间判定；缺任一边界则视为不越界（由调用方保证边界完整）。

    判定对象是 ``[start, end]`` **整体区间**（厂级口径 = 驻场期边界），
    不按单日切分：跨零点相连的日历日同属一个区间。
    """
    if start is None or end is None:
        return True
    return start <= moment <= end


def is_out_of_window(moment: datetime, start: datetime | None, end: datetime | None) -> bool:
    """越界判定（E003 依据）；闭区间，端点不算越界。

    厂级口径（T04）：边界即**驻场期边界**，判定对 ``[start, end]`` 整体区间做，
    **不按单日切分**——驻场期内跨零点的时刻（如首日 23:30、次日 02:00）不判越界。
    """
    if start is None or end is None:
        return False
    return not in_window(moment, start, end)


def _window_day_range(start: Any, end: Any) -> tuple[date, date] | None:
    """``[start, end]`` 覆盖的日历日区间 ``(首日, 末日)``；边界不完整/倒置 → ``None``。"""
    if start is None or end is None:
        return None
    first = start.date() if isinstance(start, datetime) else start
    last = end.date() if isinstance(end, datetime) else end
    if not isinstance(first, date) or not isinstance(last, date):
        return None
    return (first, last) if last >= first else None


def calendar_days_in_window(start: Any, end: Any) -> tuple[date, ...]:
    """``[start, end]`` 覆盖的全部**相连日历日**（含首尾日）。

    厂级口径（T04）：驻场期跨零点时相连日历日**均属期内**，故这里按整体区间
    取日期序列（30 天驻场期跨月 → 31 个相连日历日）。边界不完整 → 空元组。
    """
    span = _window_day_range(start, end)
    if span is None:
        return ()
    first, last = span
    return tuple(first + timedelta(days=offset) for offset in range((last - first).days + 1))


def window_span_days(start: Any, end: Any) -> int:
    """期内日历日数（边界不完整 → ``0``）。"""
    return len(calendar_days_in_window(start, end))


def covers_calendar_day(day: Any, start: Any, end: Any) -> bool:
    """该日历日是否属期内（与 ``[start, end]`` 有交集即属期内）。

    厂级口径（T04）：判定按整体区间，不按单日切分；边界不完整视为不越界
    （与 :func:`in_window` 同口径）。
    """
    span = _window_day_range(start, end)
    if span is None:
        return True
    first, last = span
    target = day.date() if isinstance(day, datetime) else day
    if not isinstance(target, date):
        raise ContractViolation("日历日必须是 date（或带时区的 datetime）")
    return first <= target <= last


__all__ = [
    "PRECISION_DATE_ONLY",
    "PRECISION_MINUTE",
    "PRECISION_MISSING",
    "SHANGHAI",
    "TimeParseResult",
    "calendar_days_in_window",
    "covers_calendar_day",
    "ensure_aware",
    "format_minute",
    "in_window",
    "is_out_of_window",
    "now_shanghai",
    "parse_occurred_at",
    "parse_time_text",
    "to_shanghai",
    "truncate_to_minute",
    "window_span_days",
]
