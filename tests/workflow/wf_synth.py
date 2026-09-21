"""合成测试数据工厂（workflow 测试用；非测试模块，不被 discover 收集）。

**所有数据均为虚构**：人员/交接线/事件ID 一律 ``SYNTH-`` 前缀，不含真实姓名、
真实群/表 ID、真实业务数据，也不含个人绝对路径（AGENTS.md）。时间用 Asia/Shanghai
固定偏移，时钟固定注入；表格读写一律走合成适配器，不发任何网络请求。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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

from integrations.aitable.synthetic import (  # noqa: E402
    CREATE,
    UPDATE,
    SyntheticAitableAdapter,
)
from integrations.aitable.tables import EVENT_TABLE, SHIFT_TABLE  # noqa: E402
from integrations.eam.adapter import defect as eam_defect  # noqa: E402
from integrations.eam.adapter import hazard as eam_hazard  # noqa: E402
from integrations.eam.synthetic import SyntheticEamAdapter  # noqa: E402
from workflow.checklist import ShiftChecklistService  # noqa: E402
from workflow.eam_pull import EamPullService  # noqa: E402
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

#: 驻场期（T04 厂级口径）：一次轮换一张单，30 天整、跨月、跨零点相连日。
STAY_PERIOD_SHIFT_ID = "SYNTH-SHIFT-STAY-0001"
STAY_PERIOD_FIRST_DAY = date(2026, 3, 2)
STAY_PERIOD_LAST_DAY = date(2026, 4, 1)
STAY_PERIOD_START = datetime(2026, 3, 2, 8, 0, tzinfo=SHANGHAI)
STAY_PERIOD_END = datetime(2026, 4, 1, 8, 0, tzinfo=SHANGHAI)
STAY_PERIOD_STAMP = datetime(2026, 4, 1, 9, 0, tzinfo=SHANGHAI)
STAY_PERIOD_DAYS = 30
STAY_PERIOD_CALENDAR_DAYS = 31


class _Omit:
    """标记"该字段整体缺失"（与显式填 None 区分）。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return "OMIT"


OMIT = _Omit()


def at(hour: int, minute: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 1, day, hour, minute, tzinfo=SHANGHAI)


def period_at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """驻场期内的合成时刻（Asia/Shanghai）——用于跨月/跨零点边界断言。"""
    return datetime(2026, month, day, hour, minute, tzinfo=SHANGHAI)


def stay_period_day(offset: int) -> date:
    """驻场期第 ``offset`` 个日历日（0 = 首日）。"""
    return STAY_PERIOD_FIRST_DAY + timedelta(days=offset)


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


def stay_period_row(
    event_id: str, occurred_at: str = "2026-03-20 09:30", **overrides: Any
) -> dict[str, Any]:
    """驻场期事件的录入行（默认归属驻场期班次、时间落在驻场期内）。"""
    overrides.setdefault("shift_id", STAY_PERIOD_SHIFT_ID)
    return row(event_id, occurred_at=occurred_at, **overrides)


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


def stay_period_flow(**kwargs: Any) -> Flow:
    """驻场期三段装配（默认班次 = 30 天驻场期，时钟固定在驻场期最后一日）。"""
    kwargs.setdefault("shift", make_stay_period_shift())
    kwargs.setdefault("moment", STAY_PERIOD_STAMP)
    return make_flow(**kwargs)


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


# ---- T05 EAM 只读拉取（合成夹具） ---------------------------------------

#: 驻场期内的合成缺陷编号（``SYNTH-`` 显式标注）。
EAM_D1 = "SYNTH-EAM-DEFECT-0001"  # 危急 / 处理中 / 2026-03-05 09:30
EAM_D2 = "SYNTH-EAM-DEFECT-0002"  # 一般 / 已消缺 / 2026-03-20 14:05
EAM_D3 = "SYNTH-EAM-DEFECT-0003"  # 特急（未知等级） / 已关闭（未知状态）
EAM_D4 = "SYNTH-EAM-DEFECT-0004"  # 严重 / 待处理 / **缺发现时间** → E001 拒收
EAM_D5 = "SYNTH-EAM-DEFECT-0005"  # 一般 / 处理中 / **只有日期** → E002
EAM_LATE = "SYNTH-EAM-DEFECT-0020"  # 一般 / 处理中 / 2026-04-01 12:00 → E003（晚于窗口结束）
EAM_H1 = "SYNTH-EAM-HAZARD-0001"  # A级 / 处理中 / 2026-03-08 10:15
EAM_H2 = "SYNTH-EAM-HAZARD-0002"  # C级 / 已验收 / 2026-03-25 16:40
EAM_H3 = "SYNTH-EAM-HAZARD-0003"  # B / 待处理 / 2026-03-02 23:30（首日跨零点，期内）
EAM_H4 = "SYNTH-EAM-HAZARD-0004"  # C级 / 已移交 / 2026-03-18 09:00
EAM_NO_REF = ""  # 编号为空 → 无法派生确定性 event_id（显式报出，不猜）
EAM_BAD_TIME = "SYNTH-EAM-DEFECT-0031"  # 发现时间写法无法解析（显式报出，不猜）

#: 跨驻场期重新提出的未消缺缺陷（两个驻场期各一条，同一编号）。
EAM_OPEN = "SYNTH-EAM-DEFECT-0090"

#: 默认 EAM 夹具的预期条数与处置（测试直接引用，避免散落的魔术数）。
EAM_DEFECT_COUNT = 5
EAM_HAZARD_COUNT = 4
EAM_FETCHED = EAM_DEFECT_COUNT + EAM_HAZARD_COUNT
EAM_CREATED = EAM_FETCHED - 1  # 缺时间的 D4 被 E001 拒收，不落表
EAM_ALARM_COUNT = 4  # E001（D4 拒收）+ E002（D5 只有日期）+ E004 ×2（D3 未知等级/状态）


def eam_defects() -> tuple[Any, ...]:
    """默认缺陷夹具（含正常 / 未知 / 缺时间 / 只有日期四类）。"""
    return (
        eam_defect(EAM_D1, "SYNTH 风机齿轮箱温度偏高", "2026-03-05 09:30", severity="危急", status="处理中"),
        eam_defect(EAM_D2, "SYNTH 箱变渗油", "2026-03-20 14:05", severity="一般", status="已消缺"),
        eam_defect(EAM_D3, "SYNTH 逆变器通讯中断", "2026-03-11 08:12", severity="特急", status="已关闭"),
        eam_defect(EAM_D4, "SYNTH 集电线路避雷器异常", None, severity="严重", status="待处理"),
        eam_defect(EAM_D5, "SYNTH SVG 模块告警", "2026-03-10", severity="一般", status="处理中"),
    )


def eam_hazards() -> tuple[Any, ...]:
    """默认隐患夹具（A/B/C 级各覆盖，含首日跨零点时刻）。"""
    return (
        eam_hazard(EAM_H1, "SYNTH 升压站消防通道占用", "2026-03-08 10:15", severity="A级", status="处理中"),
        eam_hazard(EAM_H2, "SYNTH 备品备件库房堆放不规范", "2026-03-25 16:40", severity="C级", status="已验收"),
        eam_hazard(EAM_H3, "SYNTH 箱变围栏缺失", "2026-03-02 23:30", severity="B", status="待处理"),
        eam_hazard(EAM_H4, "SYNTH 安全工器具超期", "2026-03-18 09:00", severity="C级", status="已移交"),
    )


def make_eam_adapter(
    defect_records: tuple[Any, ...] | None = None,
    hazard_records: tuple[Any, ...] | None = None,
) -> SyntheticEamAdapter:
    """默认 EAM 合成适配器（五条缺陷 + 四条隐患）。"""
    return SyntheticEamAdapter(
        defects=eam_defects() if defect_records is None else defect_records,
        hazards=eam_hazards() if hazard_records is None else hazard_records,
    )


def eam_open_defect(shift_id: str, discovered_at: str):
    """未消缺缺陷（跨驻场期重新提出用例）。"""
    return eam_defect(
        EAM_OPEN,
        "SYNTH 未消缺缺陷（跨驻场期跟踪）",
        discovered_at,
        severity="一般",
        status="处理中",
        note=f"SYNTH 夹具：{shift_id} 期内未消缺",
    )


@dataclass
class EamFlow:
    """EAM 只读拉取的合成装配（同一适配器上共享表格）。"""

    adapter: SyntheticAitableAdapter
    shift: ShiftRecord
    intake: ShiftIntakeService
    eam: SyntheticEamAdapter
    pull: EamPullService
    checklist: ShiftChecklistService
    todos: TodoAssignmentService

    def run(self, **kwargs: Any):
        return self.pull.pull(**kwargs)

    def event_rows(self) -> tuple[dict[str, str], ...]:
        return tuple(
            dict(row.fields) for row in self.intake.event_table.list_rows(self.shift.shift_id)
        )

    def row_of(self, event_id: str) -> dict[str, str]:
        row = self.intake.event_table.find_row(event_id)
        return {} if row is None else dict(row.fields)

    def event_id_of(self, ref_no: str, kind: str = "defect") -> str:
        from integrations.eam.mapping import derive_event_id

        return derive_event_id(shift_id=self.shift.shift_id, kind=kind, ref_no=ref_no)

    def table_writes(self, op: str) -> int:
        """对**交接事件表**的写入次数（``CREATE`` / ``UPDATE``；拉取路径 UPDATE 必须为 0）。"""
        return sum(
            1
            for call in self.adapter.calls_for(op)
            if call.table == EVENT_TABLE
        )

    def generate(self):
        return self.checklist.generate(todo_id_factory=self.todos.todo_id_factory)


def eam_flow(
    *,
    shift: ShiftRecord | None = None,
    adapter: SyntheticAitableAdapter | None = None,
    eam: SyntheticEamAdapter | None = None,
    moment: datetime = STAY_PERIOD_STAMP,
    create_shift: bool = True,
) -> EamFlow:
    """装配 EAM 拉取链路（默认：默认驻场期 + 默认夹具；可选择不落班次表/不拉取）。"""
    target_adapter = adapter if adapter is not None else make_adapter()
    target_shift = shift if shift is not None else make_stay_period_shift()
    target_eam = eam if eam is not None else make_eam_adapter()
    clock = fixed_clock(moment)
    intake = ShiftIntakeService(target_shift, target_adapter, clock=clock)
    if create_shift:
        intake.create_shift()
    pull = EamPullService(target_shift, intake, target_eam, clock=clock)
    checklist = ShiftChecklistService(
        target_shift, intake.store, intake.ledger, target_adapter, clock=clock
    )
    todos = TodoAssignmentService(target_adapter, target_shift, clock=clock)
    return EamFlow(
        adapter=target_adapter,
        shift=target_shift,
        intake=intake,
        eam=target_eam,
        pull=pull,
        checklist=checklist,
        todos=todos,
    )


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
    "EAM_ALARM_COUNT",
    "EAM_BAD_TIME",
    "EAM_CREATED",
    "EAM_D1",
    "EAM_D2",
    "EAM_D3",
    "EAM_D4",
    "EAM_D5",
    "EAM_DEFECT_COUNT",
    "EAM_FETCHED",
    "EAM_H1",
    "EAM_H2",
    "EAM_H3",
    "EAM_H4",
    "EAM_HAZARD_COUNT",
    "EAM_LATE",
    "EAM_NO_REF",
    "EAM_OPEN",
    "END",
    "FROM",
    "LINE",
    "OMIT",
    "OTHER_SHIFT_ID",
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
    "STAY_PERIOD_STAMP",
    "STAY_PERIOD_START",
    "STRANGER",
    "TO",
    "EamFlow",
    "Flow",
    "alarm_flow",
    "at",
    "clean_flow",
    "eam_defects",
    "eam_flow",
    "eam_hazards",
    "eam_open_defect",
    "expect_code",
    "fixed_clock",
    "make_adapter",
    "make_eam_adapter",
    "make_flow",
    "make_shift",
    "make_stay_period_shift",
    "period_at",
    "repair_all",
    "report_of",
    "row",
    "stay_period_day",
    "stay_period_flow",
    "stay_period_row",
]
