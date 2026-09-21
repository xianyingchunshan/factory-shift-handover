"""合成测试数据工厂（非测试模块，不被 discover 收集）。

**所有数据均为虚构**：人员/交接线/编号一律 ``SYNTH-`` 前缀，不含真实姓名、
真实群/表 ID、真实业务数据，也不含个人绝对路径（AGENTS.md）。
时间统一用 Asia/Shanghai 固定偏移，时钟固定注入，测试不依赖真实当前时间。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.alarms import AlarmLedger  # noqa: E402
from contracts.errors import ContractError  # noqa: E402
from contracts.events import EventStore  # noqa: E402
from contracts.identity import IdentityRef  # noqa: E402
from contracts.shift import ShiftRecord, new_shift  # noqa: E402
from contracts.timebase import SHANGHAI  # noqa: E402

#: 合成标识
SHIFT_ID = "SYNTH-SHIFT-0001"
LINE = "SYNTH-LINE-A"
FROM = IdentityRef(source="dingtalk", user_id="SYNTH-uid-from", display_name="SYNTH-交班人A")
TO = IdentityRef(source="eam", user_id="SYNTH-uid-to", display_name="SYNTH-接班人B")
STRANGER = IdentityRef(source="ehr", user_id="SYNTH-uid-stranger", display_name="SYNTH-无关人C")

SHIFT_DATE = date(2026, 1, 2)
START = datetime(2026, 1, 2, 8, 0, tzinfo=SHANGHAI)
END = datetime(2026, 1, 2, 20, 0, tzinfo=SHANGHAI)
STAMP = datetime(2026, 1, 2, 20, 0, tzinfo=SHANGHAI)

#: 驻场期（T04 厂级口径）：一次轮换一张单，30 天整、跨月、跨零点相连日。
STAY_PERIOD_SHIFT_ID = "SYNTH-SHIFT-STAY-0001"
STAY_PERIOD_FIRST_DAY = date(2026, 3, 2)
STAY_PERIOD_LAST_DAY = date(2026, 4, 1)
STAY_PERIOD_START = datetime(2026, 3, 2, 8, 0, tzinfo=SHANGHAI)
STAY_PERIOD_END = datetime(2026, 4, 1, 8, 0, tzinfo=SHANGHAI)
STAY_PERIOD_DAYS = 30
STAY_PERIOD_CALENDAR_DAYS = 31


class _Omit:
    """标记"该字段整体缺失"（与显式填 None 区分）。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return "OMIT"


OMIT = _Omit()


def at(hour: int, minute: int = 0, day: int = 2) -> datetime:
    """合成时刻（Asia/Shanghai）。"""
    return datetime(2026, 1, day, hour, minute, tzinfo=SHANGHAI)


def period_at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """驻场期内的合成时刻（Asia/Shanghai）——用于跨月/跨零点边界断言。"""
    return datetime(2026, month, day, hour, minute, tzinfo=SHANGHAI)


def stay_period_day(offset: int) -> date:
    """驻场期第 ``offset`` 个日历日（0 = 首日）。"""
    return STAY_PERIOD_FIRST_DAY + timedelta(days=offset)


def fixed_clock(moment: datetime = STAMP):
    """固定时钟，供 EventStore 使用。"""
    return lambda: moment


def make_shift(**overrides: Any) -> ShiftRecord:
    """默认一个合规班次（早班 08:00~20:00）。"""
    params: dict[str, Any] = dict(
        shift_id=SHIFT_ID,
        handover_line=LINE,
        shift_date=SHIFT_DATE,
        shift_name="early",
        start_time=START,
        end_time=END,
        handover_from=FROM,
        handover_to=TO,
        critical_standard=("重大事项", "移交接班人"),
    )
    params.update(overrides)
    return new_shift(**params)


def make_stay_period_shift(**overrides: Any) -> ShiftRecord:
    """默认一个合规的驻场期班次（30 天：2026-03-02 08:00 ~ 2026-04-01 08:00）。"""
    params: dict[str, Any] = dict(
        shift_id=STAY_PERIOD_SHIFT_ID,
        handover_line=LINE,
        shift_date=STAY_PERIOD_FIRST_DAY,
        shift_name="stay_period",
        start_time=STAY_PERIOD_START,
        end_time=STAY_PERIOD_END,
        handover_from=FROM,
        handover_to=TO,
        critical_standard=("重大事项", "移交接班人"),
    )
    params.update(overrides)
    return new_shift(**params)


def raw_event(event_id: str = "SYNTH-EVT-0001", **overrides: Any) -> dict[str, Any]:
    """默认一条合规的事件录入行；用 ``OMIT`` 删字段、``None`` 置空。"""
    row: dict[str, Any] = dict(
        event_id=event_id,
        shift_id=SHIFT_ID,
        category="equipment",
        description="SYNTH 设备情况记录",
        occurred_at="2026-01-02 09:30",
        severity="normal",
        status="done",
        ref_no="SYNTH-REF-001",
        note="",
    )
    row.update(overrides)
    return {key: value for key, value in row.items() if value is not OMIT}


def make_store(
    shift: ShiftRecord | None = None,
    ledger: AlarmLedger | None = None,
    moment: datetime = STAMP,
) -> tuple[ShiftRecord, AlarmLedger, EventStore]:
    """返回 (班次, 告警账本, 事件集合)，时钟固定。"""
    target = shift or make_shift()
    book = ledger if ledger is not None else AlarmLedger(shift_id=target.shift_id)
    store = EventStore(target, book, clock=fixed_clock(moment))
    return target, book, store


def make_stay_period_store() -> tuple[ShiftRecord, AlarmLedger, EventStore]:
    """驻场期版 :func:`make_store`（30 天边界，时钟固定在末日前）。"""
    return make_store(
        shift=make_stay_period_shift(),
        moment=STAY_PERIOD_END - timedelta(hours=1),
    )


def stay_period_raw_event(event_id: str = "SYNTH-STAY-EVT-0001", **overrides: Any) -> dict[str, Any]:
    """驻场期事件录入行（默认 ``shift_id`` = 驻场期班次，时间落在驻场期内）。"""
    overrides.setdefault("shift_id", STAY_PERIOD_SHIFT_ID)
    overrides.setdefault("occurred_at", "2026-03-20 09:30")
    return raw_event(event_id, **overrides)


def clear_all(ledger: AlarmLedger, moment: datetime | None = None) -> AlarmLedger:
    """清除全部未清告警（仅测试用；真实流程必须先补全/复核再清）。"""
    target = moment or STAMP
    for alarm in list(ledger.open_alarms()):
        ledger.clear(alarm.alarm_id, target)
    return ledger


@contextmanager
def expect_code(code: str) -> Iterator[None]:
    """断言抛出携带指定错误码的 ContractError。"""
    try:
        yield
    except ContractError as exc:
        if exc.code != code:
            raise AssertionError(f"期望错误码 {code}，实际 {exc.code}（{exc.message}）") from exc
    else:
        raise AssertionError(f"期望错误码 {code}，但未抛出 ContractError")


__all__ = [
    "END",
    "FROM",
    "LINE",
    "OMIT",
    "SHIFT_DATE",
    "SHIFT_ID",
    "START",
    "STAMP",
    "STAY_PERIOD_CALENDAR_DAYS",
    "STAY_PERIOD_DAYS",
    "STAY_PERIOD_END",
    "STAY_PERIOD_FIRST_DAY",
    "STAY_PERIOD_LAST_DAY",
    "STAY_PERIOD_SHIFT_ID",
    "STAY_PERIOD_START",
    "STRANGER",
    "TO",
    "at",
    "clear_all",
    "expect_code",
    "fixed_clock",
    "make_shift",
    "make_stay_period_shift",
    "make_stay_period_store",
    "make_store",
    "period_at",
    "raw_event",
    "stay_period_day",
    "stay_period_raw_event",
]
