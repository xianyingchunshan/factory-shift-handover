"""合成测试数据工厂（workflow 测试用；非测试模块，不被 discover 收集）。

**所有数据均为虚构**：人员/交接线/事件ID 一律 ``SYNTH-`` 前缀，不含真实姓名、
真实群/表 ID、真实业务数据，也不含个人绝对路径（AGENTS.md）。时间用 Asia/Shanghai
固定偏移，时钟固定注入；表格读写一律走合成适配器，不发任何网络请求。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.errors import ContractError  # noqa: E402
from contracts.identity import IdentityRef  # noqa: E402
from contracts.shift import ShiftRecord, new_shift  # noqa: E402
from contracts.timebase import SHANGHAI  # noqa: E402

from integrations.aitable.synthetic import SyntheticAitableAdapter  # noqa: E402
from integrations.aitable.tables import SHIFT_TABLE  # noqa: E402
from workflow.checklist import ShiftChecklistService  # noqa: E402
from workflow.intake import ShiftIntakeService  # noqa: E402
from workflow.todos import TodoAssignmentService  # noqa: E402

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


def row(event_id: str, **overrides: Any) -> dict[str, Any]:
    """默认一条合规录入行；``OMIT`` 删字段、``None`` 置空。"""
    data: dict[str, Any] = dict(
        event_id=event_id,
        shift_id=SHIFT_ID,
        category="equipment",
        description=f"SYNTH 事项 {event_id}",
        occurred_at="2026-01-02 09:30",
        severity="normal",
        status="done",
        ref_no="SYNTH-REF-001",
        note="",
    )
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not OMIT}


#: 三条合规事项：1 条一般已办、1 条一般进行中、1 条关键级移交（重大事项）。
CLEAN_ROWS: tuple[dict[str, Any], ...] = (
    row("SYNTH-EVT-0001", category="runtime", occurred_at="2026-01-02 08:10"),
    row("SYNTH-EVT-0002", category="equipment", status="in_progress", occurred_at="2026-01-02 10:00"),
    row(
        "SYNTH-EVT-0003",
        category="重大事项",
        status="移交接班人",
        occurred_at="2026-01-02 14:20",
    ),
)

#: 四种告警各一条（E001 拒收 / E002 时间不完整 / E003 越界 / E004 关键字段缺失）。
ALARM_ROWS: tuple[dict[str, Any], ...] = (
    row("SYNTH-EVT-0001", category="runtime", occurred_at="2026-01-02 08:10"),
    row("SYNTH-EVT-0002", occurred_at="2026-01-02"),
    row("SYNTH-EVT-0003", occurred_at="2026-01-02 23:00"),
    row("SYNTH-EVT-0004", severity=OMIT, occurred_at="2026-01-02 11:00"),
    row("SYNTH-EVT-0005", occurred_at=None),
)


@dataclass
class Flow:
    """一套三段服务的合成装配（同一适配器上共享表格）。"""

    adapter: SyntheticAitableAdapter
    shift: ShiftRecord
    intake: ShiftIntakeService
    checklist: ShiftChecklistService
    todos: TodoAssignmentService

    def submit(self, rows: Iterable[Mapping[str, Any]]):
        return self.intake.submit_all(rows)

    def board_row(self, unit_id: str) -> dict[str, str]:
        return self.todos.board_fields(unit_id)

    def event_rows(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(row.fields) for row in self.intake.event_table.list_rows(self.shift.shift_id))

    def shift_row(self) -> dict[str, str]:
        record_id = self.intake.shift_table.record_id_of(self.shift.shift_id)
        return dict(self.adapter.get_record(SHIFT_TABLE, record_id).fields)


def make_flow(
    *,
    shift: ShiftRecord | None = None,
    adapter: SyntheticAitableAdapter | None = None,
    moment: datetime = STAMP,
    create_shift: bool = True,
) -> Flow:
    """装配三段服务；默认同时落班次表。"""
    target_adapter = adapter if adapter is not None else make_adapter()
    target_shift = shift if shift is not None else make_shift()
    clock = fixed_clock(moment)
    intake = ShiftIntakeService(target_shift, target_adapter, clock=clock)
    if create_shift:
        intake.create_shift()
    checklist = ShiftChecklistService(
        target_shift, intake.store, intake.ledger, target_adapter, clock=clock
    )
    todos = TodoAssignmentService(target_adapter, target_shift, clock=clock)
    return Flow(
        adapter=target_adapter,
        shift=target_shift,
        intake=intake,
        checklist=checklist,
        todos=todos,
    )


def clean_flow(**kwargs: Any) -> Flow:
    """录制三条合规事项（无告警），可直接出清单。"""
    flow = make_flow(**kwargs)
    flow.submit(CLEAN_ROWS)
    return flow


def alarm_flow(**kwargs: Any) -> Flow:
    """录制四种告警各一条（含 E001 拒收），用于闸门与恢复测试。"""
    flow = make_flow(**kwargs)
    flow.submit(ALARM_ROWS)
    return flow


def repair_all(flow: Flow) -> Flow:
    """按业务路径补全/复核/重录并清告警（不绕过服务，不直接改账本）。"""
    flow.intake.repair_time("SYNTH-EVT-0002", "2026-01-02 10:05")
    flow.intake.review_out_of_range("SYNTH-EVT-0003", note="SYNTH 复核确认越界属实")
    flow.intake.fill_required("SYNTH-EVT-0004", severity="normal")
    flow.intake.submit(row("SYNTH-EVT-0005", occurred_at="2026-01-02 11:30"))
    return flow


def report_of(flow: Flow):
    """出清单（带待办创建）。"""
    return flow.checklist.generate(todo_id_factory=flow.todos.todo_id_factory)


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
    "ALARM_ROWS",
    "CLEAN_ROWS",
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
    "Flow",
    "alarm_flow",
    "at",
    "clean_flow",
    "expect_code",
    "fixed_clock",
    "make_adapter",
    "make_flow",
    "make_shift",
    "repair_all",
    "report_of",
    "row",
]
