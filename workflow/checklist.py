"""阶段③前半：清单生成（issue #9 第 3 段）。

五段固定结构（顺序即契约 :data:`contracts.report.REPORT_SECTIONS`）：

1. 关键级事项置顶加粗
2. 事件流水（全量，``occurred_at`` 升序）
3. 未完事项移交接班人（逐项确认）
4. 完整性校验汇总（事件数 / 完整数 / 时间完整率 / 告警数）
5. 确认区

硬闸门：**告警未清禁止出清单** —— 走 :meth:`contracts.alarms.AlarmLedger.require_clear`
（``ALARM_NOT_CLEARED``，班次同步置 ``blocked``）；补全与清告警不同步走
:meth:`contracts.events.EventStore.require_all_report_ready`。

额外自检（本层）：出清单前先做**源一致性**（内存事件集合 vs 交接事件表），
出清单后再做**清单一致性**（时间序 / 置顶 / 未完移交 / 汇总数 vs 表格行数），
任一不符即拒绝出清单——清单与事件记录必须严格一致。

T04 增补（SPEC §0 厂级口径）：一次轮换一张单，清单标题与汇总口径跟随班次名——
驻场期单即「交接班清单 · 驻场期」（:func:`checklist_title` /
:func:`stay_period_facts`，服务上对应 ``title`` / ``period_facts`` /
``render_document``）。契约层 ``render_lines`` 保持冻结，口径与它同源（都取班次名标签）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from contracts.alarms import AlarmLedger
from contracts.enums import ShiftName, label_of
from contracts.errors import ALARM_NOT_CLEARED, ContractError, ContractViolation
from contracts.events import EventStore, HandoverEvent
from contracts.report import HandoverReport, build_report
from contracts.shift import ShiftRecord
from contracts.timebase import (
    calendar_days_in_window,
    format_minute,
    now_shanghai,
)

from integrations.aitable.adapter import AitableAdapter
from integrations.aitable.cells import EVENT_COLUMNS, SHIFT_COLUMNS
from integrations.aitable.synthetic import assert_synthetic
from integrations.aitable.tables import (
    EVENT_TABLE,
    SHIFT_TABLE,
    EventTable,
    IntakeJournal,
    OUTCOME_REJECTED,
    ShiftTable,
)


@dataclass(frozen=True)
class ConsistencyReport:
    """清单与事件记录的一致性检查结果。"""

    ok: bool
    problems: tuple[str, ...]
    checked: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "problems": list(self.problems), "checked": dict(self.checked)}


#: 清单标题基名（标题口径：``交接班清单 · <班次名标签>``）。
CHECKLIST_TITLE_BASE = "交接班清单"


def checklist_title(shift: ShiftRecord) -> str:
    """清单标题口径：随班次名走——驻场期单即「交接班清单 · 驻场期」。

    T04 增补（SPEC §0 厂级口径：一次轮换一张单）：标题必须体现"这一单覆盖整个驻场期"。
    与契约层 :meth:`contracts.report.HandoverReport.render_lines` **同源**
    （都取 ``label_of(ShiftName, ...)``），不另立一套命名。
    """
    return f"{CHECKLIST_TITLE_BASE} · {label_of(ShiftName, shift.shift_name)}"


def stay_period_facts(shift: ShiftRecord) -> dict[str, Any]:
    """汇总口径：标题 + 驻场期边界 + 覆盖日历日（长周期/跨零点一并给足）。

    ``calendar_days`` 为期内**相连**日历日数：30 天驻场期跨月 → 31 个相连日历日
    （首日与末日各算一天），与 E003「整体区间、不按单日切分」的口径一致。
    """
    days = calendar_days_in_window(shift.start_time, shift.end_time)
    return {
        "title": checklist_title(shift),
        "shift_id": shift.shift_id,
        "shift_name": shift.shift_name,
        "shift_name_label": label_of(ShiftName, shift.shift_name),
        "is_stay_period": shift.is_stay_period,
        "window_start": format_minute(shift.start_time),
        "window_end": format_minute(shift.end_time),
        "first_day": "" if not days else days[0].isoformat(),
        "last_day": "" if not days else days[-1].isoformat(),
        "calendar_days": len(days),
        "crosses_midnight": shift.start_time.date() != shift.end_time.date(),
    }


class ShiftChecklistService:
    """阶段③服务：全表复验 → 清单生成 → 一致性自检 → 渲染。"""

    def __init__(
        self,
        shift: ShiftRecord,
        store: EventStore,
        ledger: AlarmLedger,
        adapter: AitableAdapter,
        *,
        clock: Callable[[], datetime] | None = None,
        require_offline: bool = True,
    ) -> None:
        if require_offline:
            assert_synthetic(adapter)
        self.shift = shift
        self.store = store
        self.ledger = ledger
        self.adapter = adapter
        self.clock = clock or now_shanghai
        self.event_table = EventTable(adapter)
        self.journal = IntakeJournal(adapter)

    # ---- 闸门 -----------------------------------------------------------

    def source_problems(self) -> tuple[str, ...]:
        """源一致性：内存事件集合 vs 交接事件表。"""
        table_ids = set(self.event_table.event_ids(self.shift.shift_id))
        memory_ids = {event.event_id for event in self.store.events}
        problems: list[str] = []
        if memory_ids - table_ids:
            problems.append(
                "内存有但表里没有的事件: " + ", ".join(sorted(memory_ids - table_ids))
            )
        if table_ids - memory_ids:
            problems.append(
                "表里有但内存没有的事件: " + ", ".join(sorted(table_ids - memory_ids))
            )
        return tuple(problems)

    def gate(self) -> AlarmLedger:
        """告警闸门：未清告警 → ``ALARM_NOT_CLEARED`` + 班次置 ``blocked``（并回写班次表）。"""
        self.shift.suspend_for_alarms(self.ledger.has_open)
        sync_shift_status(self.shift, self.adapter)
        self.ledger.require_clear()
        return self.ledger

    def rejected_entries(self) -> tuple[Any, ...]:
        """被拒收（E001，不落表）的输入留痕，供清单前人工核对。"""
        return self.journal.rejected_entries(self.shift.shift_id)

    # ---- 生成 -----------------------------------------------------------

    def generate(
        self,
        *,
        generated_at: datetime | None = None,
        todo_id_factory: Callable[[Any], str] | None = None,
    ) -> HandoverReport:
        """生成清单；任一闸门或一致性检查不过即拒绝，不产出半成品清单。"""
        problems = self.source_problems()
        if problems:
            raise ContractViolation("出清单前源一致性检查失败：" + "；".join(problems))
        self.gate()
        report = build_report(
            self.shift,
            self.store,
            self.ledger,
            generated_at=generated_at or self.clock(),
            todo_id_factory=todo_id_factory,
        )
        consistency = self.verify(report)
        if not consistency.ok:
            raise ContractViolation(
                "清单与事件记录严格一致检查失败：" + "；".join(consistency.problems)
            )
        return report

    def verify(self, report: HandoverReport) -> ConsistencyReport:
        """清单 vs 交接事件表：时间序、关键级置顶、未完移交、汇总数逐项核对。"""
        problems: list[str] = []
        rows = self.event_table.list_rows(self.shift.shift_id)
        table_events = tuple(
            sorted(
                self.event_table.list_for_shift(self.shift.shift_id),
                key=lambda event: event.sort_key(),
            )
        )
        flow_ids = tuple(event.event_id for event in report.event_flow)
        table_ids = tuple(event.event_id for event in table_events)
        if flow_ids != table_ids:
            problems.append(
                f"流水与交接事件表不一致：流水={list(flow_ids)}，表={list(table_ids)}"
            )
        times = [event.occurred_at for event in report.event_flow]
        if any(moment is None for moment in times) or times != sorted(times):
            problems.append("事件流水未按 occurred_at 升序或存在空时间")
        expected_critical = tuple(event for event in report.event_flow if event.is_critical)
        if report.critical_items != expected_critical:
            problems.append("关键级置顶段与流水中的关键级不一致")
        expected_pending = tuple(event for event in report.event_flow if event.is_transfer_pending)
        if report.pending_transfers != expected_pending:
            problems.append("未完移交段与流水中的移交事项不一致")
        if any(not event.can_enter_report() for event in report.event_flow):
            problems.append("流水里混入了告警态事件")

        summary = dict(report.completeness_summary)
        if summary.get("total") != len(rows) or summary.get("total") != len(table_events):
            problems.append(
                f"汇总事件数与表格行数不一致：汇总={summary.get('total')}，表={len(rows)}"
            )
        complete = sum(1 for event in table_events if event.can_enter_report())
        if summary.get("complete") != complete:
            problems.append(f"汇总完整数与表格不一致：汇总={summary.get('complete')}，表={complete}")
        if summary.get("alarm_count") != self.ledger.alarm_count:
            problems.append("汇总告警数与告警账本不一致")
        if report.shift_id != self.shift.shift_id or report.window_start != self.shift.start_time:
            problems.append("清单元数据与班次不一致")
        if tuple(section for section in report.to_dict() if section in report.section_names()) != tuple(
            report.section_names()
        ):
            problems.append("清单五段结构与契约顺序不一致")

        checked = {
            "table_rows": len(rows),
            "flow": len(report.event_flow),
            "critical": len(report.critical_items),
            "pending_transfers": len(report.pending_transfers),
            "summary_total": summary.get("total"),
            "summary_complete": summary.get("complete"),
            "alarm_count": summary.get("alarm_count"),
            "rejected_entries": len(self.rejected_entries()),
        }
        return ConsistencyReport(ok=not problems, problems=tuple(problems), checked=checked)

    # ---- 渲染 -----------------------------------------------------------

    @property
    def title(self) -> str:
        """本单清单标题（口径跟随班次名：驻场期 → 「交接班清单 · 驻场期」）。"""
        return checklist_title(self.shift)

    def period_facts(self) -> dict[str, Any]:
        """本单驻场期汇总口径（标题 / 边界 / 覆盖日历日 / 是否跨零点）。"""
        return stay_period_facts(self.shift)

    def render_document(self, report: HandoverReport) -> list[str]:
        """带标题口径的完整清单：标题行 + 契约五段（五段渲染保持冻结不改）。"""
        return [self.title, *self.render(report)]

    def render(self, report: HandoverReport) -> list[str]:
        """渲染成人能看懂的文本清单（契约 :meth:`HandoverReport.render_lines`）。"""
        return report.render_lines()

    def render_text(self, report: HandoverReport) -> str:
        return "\n".join(self.render(report))

    def blocked_summary(self) -> dict[str, Any]:
        """告警未清时的阻断摘要（便于交班人看到差在哪）。"""
        self.shift.suspend_for_alarms(self.ledger.has_open)
        blocked = self.ledger.has_open or bool(self.rejected_entries())
        return {
            "blocked": blocked,
            "shift_id": self.shift.shift_id,
            "status": self.shift.status,
            "open_alarms": [alarm.to_dict() for alarm in self.ledger.open_alarms()],
            "rejected_entries": [
                entry.to_dict() if hasattr(entry, "to_dict") else entry
                for entry in self.rejected_entries()
            ],
            "generated_at": format_minute(self.clock()),
        }


def sync_shift_status(shift: ShiftRecord, adapter: AitableAdapter) -> None:
    """把内存班次状态同步到班次表（表里没有该班次时跳过，不改历史记录）。"""
    table = ShiftTable(adapter)
    record_id = table.record_id_of(shift.shift_id)
    if record_id is None:
        return
    current = adapter.get_record(SHIFT_TABLE, record_id)
    if current is None or current.get(SHIFT_COLUMNS["status"]) != str(shift.status):
        table.set_status(shift.shift_id, shift.status)


def require_clear_or_block(shift: ShiftRecord, ledger: AlarmLedger) -> None:
    """独立闸门入口（供其它模块复用）：未清告警即拒绝。"""
    shift.suspend_for_alarms(ledger.has_open)
    if ledger.has_open:
        raise ContractError(
            ALARM_NOT_CLEARED,
            f"班次 {shift.shift_id} 存在未清除告警，禁止生成清单",
            detail={"shift_id": shift.shift_id, "status": shift.status},
        )
    ledger.require_clear()


__all__ = [
    "CHECKLIST_TITLE_BASE",
    "ConsistencyReport",
    "ShiftChecklistService",
    "checklist_title",
    "require_clear_or_block",
    "stay_period_facts",
    "sync_shift_status",
]
