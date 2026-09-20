"""T01 公共契约：输出清单 HandoverReport（issue #6 §4）。

结构（顺序即契约，不可重排）：

1. ``critical_items``    关键级事项（置顶加粗）
2. ``event_flow``        事件流水（``occurred_at`` 升序，全量）
3. ``pending_transfers`` 未完事项（移交接班人，接班人逐项确认）
4. ``completeness_summary`` 完整性校验结果 ``{total, complete, time_complete_rate, alarm_count}``
5. ``confirmation_area`` 确认区

不变量：

- 关键级置顶加粗；流水严格时间序。
- **告警未清不出清单**：:func:`build_report` 先查告警账本，有未清告警即
  :data:`~contracts.errors.ALARM_NOT_CLEARED`，并把班次置为 ``blocked``。
- 分发：**只单发交班人和接班人两人，不进群**（:class:`DispatchPlan`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from .alarms import AlarmLedger
from .confirmation import ConfirmationUnit, build_confirmation_plan
from .enums import (
    EventCategory,
    EventStatus,
    Severity,
    ShiftName,
    label_of,
)
from .errors import ALARM_NOT_CLEARED, ContractError, ContractViolation
from .events import HandoverEvent
from .identity import IdentityRef, as_identity
from .shift import ShiftRecord
from .timebase import format_minute, now_shanghai, truncate_to_minute

#: 清单五段结构（顺序即契约）。
REPORT_SECTIONS: tuple[str, ...] = (
    "critical_items",
    "event_flow",
    "pending_transfers",
    "completeness_summary",
    "confirmation_area",
)


@dataclass(frozen=True)
class DispatchPlan:
    """分发计划：只单发交班人 + 接班人两人，不进群。"""

    recipients: tuple[IdentityRef, IdentityRef]
    channel: str = "direct_single"
    allow_group: bool = False

    def __post_init__(self) -> None:
        people = tuple(self.recipients)
        if len(people) != 2:
            raise ContractViolation("清单只单发两人：交班人 + 接班人")
        if people[0].same_person(people[1]):
            raise ContractViolation("交班人与接班人不能是同一身份引用")
        object.__setattr__(self, "recipients", (people[0], people[1]))
        if self.channel != "direct_single":
            raise ContractViolation("分发渠道固定为单发（direct_single），不进群")

    def validate_targets(self, targets: Iterable[IdentityRef]) -> None:
        """送达目标只能是这两人；出现群/第三人即拒绝。"""
        allowed = {(person.source, person.user_id) for person in self.recipients}
        for target in targets:
            person = target
            if not isinstance(person, IdentityRef):
                raise ContractViolation("送达目标必须是身份引用")
            if (person.source, person.user_id) not in allowed:
                raise ContractViolation(
                    "清单仅单发交班人与接班人，不得加入其他人员或群"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "allow_group": self.allow_group,
            "recipients": [person.to_dict() for person in self.recipients],
            "expected_message_count": 2,
        }


@dataclass(frozen=True)
class DeliveryReceipt:
    """单发回读凭证：留存两条 message_id（issue §4）。"""

    message_id: str
    recipient: IdentityRef
    read_back_at: datetime
    applied: bool = True

    def __post_init__(self) -> None:
        if not str(self.message_id or "").strip():
            raise ContractViolation("message_id 不能为空（送达需回读证据）")
        object.__setattr__(self, "message_id", str(self.message_id).strip())
        object.__setattr__(self, "recipient", as_identity(self.recipient))
        object.__setattr__(
            self, "read_back_at", truncate_to_minute(self.read_back_at)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "recipient": self.recipient.to_dict(),
            "read_back_at": format_minute(self.read_back_at),
            "applied": bool(self.applied),
        }


@dataclass
class HandoverReport:
    """输出清单（钉钉AI表格 → 人能看懂的时间顺序清单）。"""

    shift_id: str
    handover_line: str
    shift_date: date
    shift_name: str
    window_start: datetime
    window_end: datetime
    critical_items: tuple[HandoverEvent, ...]
    event_flow: tuple[HandoverEvent, ...]
    pending_transfers: tuple[HandoverEvent, ...]
    completeness_summary: Mapping[str, Any]
    confirmation_area: tuple[ConfirmationUnit, ...]
    dispatch: DispatchPlan
    generated_at: datetime
    receipts: list[DeliveryReceipt] = field(default_factory=list)

    # ---- 结构 -----------------------------------------------------------

    @staticmethod
    def section_names() -> tuple[str, ...]:
        return REPORT_SECTIONS

    @property
    def normal_events(self) -> tuple[HandoverEvent, ...]:
        return tuple(event for event in self.event_flow if not event.is_critical)

    def recipient_ids(self) -> tuple[str, ...]:
        return tuple(person.user_id for person in self.dispatch.recipients)

    def record_receipt(self, receipt: DeliveryReceipt) -> "HandoverReport":
        """登记送达回读；目标不在两人内、或重复登记即拒绝。"""
        self.dispatch.validate_targets([receipt.recipient])
        known = {item.recipient.user_id for item in self.receipts}
        if receipt.recipient.user_id in known:
            raise ContractViolation("该收件人已有送达回读，不重复登记")
        if len(self.receipts) >= len(self.dispatch.recipients):
            raise ContractViolation("清单只单发两人，不产生第三条送达")
        self.receipts.append(receipt)
        return self

    @property
    def is_delivery_complete(self) -> bool:
        """两人各有一条回读才算出清单分发完成。"""
        return len({item.recipient.user_id for item in self.receipts}) == 2

    # ---- 渲染 -----------------------------------------------------------

    def render_lines(self) -> list[str]:
        """渲染成人能看懂的文本清单（关键级置顶加粗，流水时间序）。"""
        lines: list[str] = []
        lines.append(
            f"【交接班清单】交接线={self.handover_line} 日期={self.shift_date.isoformat()} "
            f"班次={label_of(ShiftName, self.shift_name)} "
            f"区间={format_minute(self.window_start)}~{format_minute(self.window_end)}"
        )
        lines.append(f"■ 一、重要事项（关键级 {len(self.critical_items)} 项，置顶）")
        for item in self.critical_items:
            lines.append(f"  {self._render_event(item, bold=True)}")
        lines.append(f"■ 二、事件流水（全量 {len(self.event_flow)} 项，时间序）")
        for item in self.event_flow:
            lines.append(f"  {self._render_event(item, bold=False)}")
        lines.append(f"■ 三、未完事项移交接班人（{len(self.pending_transfers)} 项，接班人须逐项确认）")
        for item in self.pending_transfers:
            lines.append(f"  {self._render_event(item, bold=False)}")
        summary = self.completeness_summary
        lines.append(
            "■ 四、完整性校验：事件数={total}，完整={complete}，"
            "时间完整率={rate:.0%}，告警数={alarm_count}".format(
                total=summary.get("total", 0),
                complete=summary.get("complete", 0),
                rate=float(summary.get("time_complete_rate", 0.0)),
                alarm_count=summary.get("alarm_count", 0),
            )
        )
        lines.append(f"■ 五、确认区（{len(self.confirmation_area)} 个确认单元）")
        for unit in self.confirmation_area:
            lines.append(
                f"  [{unit.scope_label}] {unit.unit_id} 待办={unit.todo_id or '未创建'}"
            )
        lines.append(
            "分发：仅单发交班人与接班人两人（不进群），预计 {n} 条，已回读 {m} 条".format(
                n=self.dispatch.to_dict()["expected_message_count"], m=len(self.receipts)
            )
        )
        return lines

    @staticmethod
    def _render_event(event: HandoverEvent, *, bold: bool) -> str:
        prefix = "**" if bold else "-"
        return (
            f"{prefix} {format_minute(event.occurred_at)} "
            f"[{label_of(EventCategory, event.category)}] {event.description} "
            f"（重要级={label_of(Severity, event.severity)}，状态={label_of(EventStatus, event.status)}"
            f"{'，强制升级:' + event.severity_forced_reason if event.severity_forced else ''}）"
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "shift_id": self.shift_id,
            "handover_line": self.handover_line,
            "shift_date": self.shift_date.isoformat(),
            "shift_name": self.shift_name,
            "window_start": format_minute(self.window_start),
            "window_end": format_minute(self.window_end),
            "generated_at": format_minute(self.generated_at),
        }
        payload["critical_items"] = [event.to_dict() for event in self.critical_items]
        payload["event_flow"] = [event.to_dict() for event in self.event_flow]
        payload["pending_transfers"] = [event.to_dict() for event in self.pending_transfers]
        payload["completeness_summary"] = dict(self.completeness_summary)
        payload["confirmation_area"] = [unit.to_dict() for unit in self.confirmation_area]
        payload["dispatch"] = self.dispatch.to_dict()
        return payload


def build_report(
    shift: ShiftRecord,
    events: Any,
    ledger: AlarmLedger,
    *,
    generated_at: datetime | None = None,
    todo_id_factory: Any = None,
) -> HandoverReport:
    """从班次 + 事件集合生成清单；**告警未清禁止生成**。

    ``events``：:class:`~contracts.events.EventStore`（或任何提供 ``events``
    与 ``complete_count`` 的对象）。
    """
    shift.suspend_for_alarms(ledger.has_open)
    if ledger.has_open:
        opened = ledger.open_alarms()
        raise ContractError(
            ALARM_NOT_CLEARED,
            f"存在 {len(opened)} 条未清除告警，禁止生成输出清单（班次已置为 blocked）",
            detail={
                "shift_id": shift.shift_id,
                "status": shift.status,
                "alarm_ids": [alarm.alarm_id for alarm in opened],
            },
        )
    if hasattr(events, "require_all_report_ready"):
        events.require_all_report_ready()

    collected = list(events.events)
    ordered = tuple(sorted(collected, key=lambda event: event.sort_key()))
    critical = tuple(event for event in ordered if event.is_critical)
    pending = tuple(event for event in ordered if event.is_transfer_pending)
    summary = ledger.summary(
        event_total=len(ordered),
        event_complete=sum(1 for event in ordered if event.can_enter_report()),
    )
    stamp = (
        truncate_to_minute(now_shanghai())
        if generated_at is None
        else truncate_to_minute(generated_at)
    )
    dispatch = DispatchPlan(recipients=(shift.handover_from, shift.handover_to))
    report = HandoverReport(
        shift_id=shift.shift_id,
        handover_line=shift.handover_line,
        shift_date=shift.shift_date,
        shift_name=shift.shift_name,
        window_start=shift.start_time,
        window_end=shift.end_time,
        critical_items=critical,
        event_flow=ordered,
        pending_transfers=pending,
        completeness_summary=summary,
        confirmation_area=(),
        dispatch=dispatch,
        generated_at=stamp,
    )
    units = build_confirmation_plan(shift, report, todo_id_factory=todo_id_factory)
    report.confirmation_area = units
    return report


__all__ = [
    "REPORT_SECTIONS",
    "DeliveryReceipt",
    "DispatchPlan",
    "HandoverReport",
    "build_report",
]
