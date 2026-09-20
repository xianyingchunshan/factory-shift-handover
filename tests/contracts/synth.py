"""合成测试数据工厂（非测试模块，不被 discover 收集）。

**所有数据均为虚构**：人员/交接线/编号一律 ``SYNTH-`` 前缀，不含真实姓名、
真实群/表 ID、真实业务数据，也不含个人绝对路径（AGENTS.md）。
时间统一用 Asia/Shanghai 固定偏移，时钟固定注入，测试不依赖真实当前时间。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date, datetime
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


class _Omit:
    """标记"该字段整体缺失"（与显式填 None 区分）。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return "OMIT"


OMIT = _Omit()


def at(hour: int, minute: int = 0, day: int = 2) -> datetime:
    """合成时刻（Asia/Shanghai）。"""
    return datetime(2026, 1, day, hour, minute, tzinfo=SHANGHAI)


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
    "STRANGER",
    "TO",
    "at",
    "clear_all",
    "expect_code",
    "fixed_clock",
    "make_shift",
    "make_store",
    "raw_event",
]
