"""合成测试数据工厂（integrations 测试用；非测试模块，不被 discover 收集）。

**所有数据均为虚构**：人员/交接线/编号/事件ID 一律 ``SYNTH-`` 前缀，不含真实姓名、
真实群/表 ID、真实业务数据，也不含个人绝对路径（AGENTS.md）。时间用 Asia/Shanghai
固定偏移，时钟固定注入，测试不依赖真实当前时间，也不发任何网络请求。
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

from contracts.enums import Completeness, EventCategory, EventStatus, Severity  # noqa: E402
from contracts.errors import ContractError  # noqa: E402
from contracts.events import HandoverEvent  # noqa: E402
from contracts.identity import IdentityRef  # noqa: E402
from contracts.shift import ShiftRecord, new_shift  # noqa: E402
from contracts.timebase import SHANGHAI  # noqa: E402

from integrations.aitable.cells import (  # noqa: E402
    EVENT_COLUMNS,
    SHIFT_COLUMNS,
    event_to_fields,
    shift_to_fields,
)
from integrations.aitable.synthetic import SyntheticAitableAdapter  # noqa: E402

SHIFT_ID = "SYNTH-SHIFT-0001"
OTHER_SHIFT_ID = "SYNTH-SHIFT-0002"
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
    return lambda: moment


def make_adapter() -> SyntheticAitableAdapter:
    return SyntheticAitableAdapter()


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


def make_event(event_id: str = "SYNTH-EVT-0001", **overrides: Any) -> HandoverEvent:
    """默认一条合规事件（完整态）。"""
    params: dict[str, Any] = dict(
        event_id=event_id,
        shift_id=SHIFT_ID,
        category=EventCategory.EQUIPMENT.value,
        description=f"SYNTH 事件 {event_id}",
        occurred_at=at(9, 30),
        severity=Severity.NORMAL.value,
        status=EventStatus.DONE.value,
        owner=TO,
        ref_no="SYNTH-REF-001",
        note="",
        completeness=Completeness.COMPLETE.value,
    )
    params.update(overrides)
    return HandoverEvent(**params)


def _apply_overrides(
    row: dict[str, str], overrides: dict[str, Any], columns: dict[str, str]
) -> dict[str, str]:
    """覆盖单元格：键可用契约字段名（``occurred_at``）或列名（``发生时间``）。

    ``OMIT`` 表示整列缺失（用于构造"列不存在"的脏数据）。
    """
    for key, value in overrides.items():
        column = columns.get(key, key)
        if value is OMIT:
            row.pop(column, None)
        else:
            row[column] = "" if value is None else str(value)
    return row


def shift_row(**overrides: Any) -> dict[str, str]:
    """班次表的合成行（原始字符串）；覆盖键用字段名或列名。"""
    return _apply_overrides(shift_to_fields(make_shift()), overrides, SHIFT_COLUMNS)


def event_row(event_id: str = "SYNTH-EVT-0001", **overrides: Any) -> dict[str, str]:
    """交接事件表的合成行（原始字符串）；覆盖键用字段名或列名。"""
    params = {key: value for key, value in overrides.items() if key == "event_id"}
    rest = {key: value for key, value in overrides.items() if key != "event_id"}
    if params:
        event_id = str(params["event_id"])
    return _apply_overrides(event_to_fields(make_event(event_id)), rest, EVENT_COLUMNS)


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
    "OTHER_SHIFT_ID",
    "SHIFT_DATE",
    "SHIFT_ID",
    "START",
    "STAMP",
    "STRANGER",
    "TO",
    "at",
    "event_row",
    "expect_code",
    "fixed_clock",
    "make_adapter",
    "make_event",
    "make_shift",
    "shift_row",
]
