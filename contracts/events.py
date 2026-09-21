"""T01 公共契约：交接事件 HandoverEvent（issue #6 §2）。

九类统一字段 + 四条校验产出。关键规则：

- ``occurred_at`` **必填精确到分**：缺 → E001 拒收（不落表）；只有日期没时:分 →
  E002 告警（落表但 ``occurred_at=None``，待补全）。
- 越出班次区间 → E003 告警（``completeness=out_of_range``）。
- 类别/重要级/状态为空 → E004 告警（``completeness=missing_required``）。
- **关键级强制升级两条**：``category=major`` 与 ``status=transferred`` 一律
  ``severity=critical``，且**不可降**（原值只作留痕 ``severity_forced``）。
- 只有 ``completeness=complete`` 的事件才能进入输出清单。

E003 判定边界（T04 增补，SPEC §0 厂级定位）：**班次区间即驻场期边界**——一次轮换
一张单、覆盖整个驻场期；判定走 :meth:`contracts.shift.ShiftRecord.contains`，对
``[start, end]`` 整体区间做，跨零点相连日历日**均属期内**，不按单日切分。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from .alarms import AlarmLedger, CompletenessAlarm
from .enums import (
    ALARM_HOLDING_COMPLETENESS,
    Completeness,
    EventCategory,
    EventStatus,
    Severity,
    coerce_enum,
    is_blank,
    label_of,
    optional_enum,
)
from .errors import (
    E001,
    E002,
    E003,
    E004,
    ContractViolation,
)
from .identity import IdentityRef, as_identity
from .shift import ShiftRecord
from .timebase import (
    PRECISION_DATE_ONLY,
    PRECISION_MINUTE,
    PRECISION_MISSING,
    format_minute,
    now_shanghai,
    parse_occurred_at,
    truncate_to_minute,
)

#: 事件字段顺序（表格列与 to_dict 均按此顺序，后续模块不必猜字段）。
EVENT_FIELD_ORDER: tuple[str, ...] = (
    "event_id",
    "shift_id",
    "category",
    "description",
    "occurred_at",
    "severity",
    "status",
    "owner",
    "ref_no",
    "note",
    "completeness",
)

#: E004 覆盖的三个关键字段（issue §3：类别/重要级/状态）。
REQUIRED_ENUM_FIELDS: tuple[str, ...] = ("category", "severity", "status")

#: 强制升级原因标记。
FORCE_CATEGORY_MAJOR = "category_major"
FORCE_STATUS_TRANSFERRED = "status_transferred"


def resolve_severity(
    category: str | None,
    status: str | None,
    requested: str | None,
) -> tuple[str | None, bool, str]:
    """计算最终重要级：``(severity, forced, reason)``。

    两条强制规则（issue §1/§2）：第 5 类默认关键级；状态"移交接班人"自动升级关键级。
    命中任一条即 ``critical``，请求值只作留痕，**不可降**。
    """
    category_member = optional_enum(EventCategory, category, field="category")
    status_member = optional_enum(EventStatus, status, field="status")
    severity_member = optional_enum(Severity, requested, field="severity")
    reasons: list[str] = []
    if category_member == EventCategory.MAJOR:
        reasons.append(FORCE_CATEGORY_MAJOR)
    if status_member == EventStatus.TRANSFERRED:
        reasons.append(FORCE_STATUS_TRANSFERRED)
    if reasons:
        return Severity.CRITICAL.value, True, "+".join(reasons)
    return (None if severity_member is None else severity_member.value), False, ""


@dataclass
class HandoverEvent:
    """一条交接事件（钉钉AI表格：交接事件表一行）。"""

    event_id: str
    shift_id: str
    category: str | None
    description: str
    occurred_at: datetime | None
    severity: str | None
    status: str | None
    owner: IdentityRef | None = None
    ref_no: str = ""
    note: str = ""
    completeness: str = Completeness.COMPLETE
    severity_forced: bool = False
    severity_forced_reason: str = ""
    original_severity: str | None = None

    def __post_init__(self) -> None:
        if is_blank(self.event_id):
            raise ContractViolation("event_id 不能为空（唯一）")
        if is_blank(self.shift_id):
            raise ContractViolation("shift_id 不能为空（归属班次）")
        if is_blank(self.description):
            raise ContractViolation(
                "description 必填：四条告警未覆盖该字段，缺失按调用方缺陷拒绝"
            )
        self.event_id = str(self.event_id).strip()
        self.shift_id = str(self.shift_id).strip()
        self.description = str(self.description).strip()
        self.category = (
            None
            if optional_enum(EventCategory, self.category, field="category") is None
            else coerce_enum(EventCategory, self.category, field="category").value
        )
        self.severity = (
            None
            if optional_enum(Severity, self.severity, field="severity") is None
            else coerce_enum(Severity, self.severity, field="severity").value
        )
        self.status = (
            None
            if optional_enum(EventStatus, self.status, field="status") is None
            else coerce_enum(EventStatus, self.status, field="status").value
        )
        self.completeness = coerce_enum(
            Completeness, self.completeness, field="completeness"
        ).value
        if self.occurred_at is not None:
            self.occurred_at = truncate_to_minute(self.occurred_at)
        if self.owner is not None:
            self.owner = as_identity(self.owner)
        self.ref_no = str(self.ref_no or "").strip()
        self.note = str(self.note or "").strip()
        self.original_severity = (
            None
            if self.original_severity is None
            else coerce_enum(Severity, self.original_severity, field="original_severity").value
        )
        self._check_state_invariants()
        self._apply_forced_severity()

    # ---- 不变量 ---------------------------------------------------------

    def _check_state_invariants(self) -> None:
        holding = self.completeness in ALARM_HOLDING_COMPLETENESS
        if self.completeness == Completeness.COMPLETE:
            missing = [name for name in REQUIRED_ENUM_FIELDS if getattr(self, name) is None]
            if missing or self.occurred_at is None:
                raise ContractViolation(
                    "completeness=complete 时不得缺 类别/重要级/状态/发生时间；"
                    "告警态请用对应 completeness 取值"
                )
        if self.occurred_at is None and self.completeness not in (
            Completeness.MISSING_TIME,
            Completeness.PARTIAL_TIME,
        ):
            raise ContractViolation(
                "occurred_at 为空只允许出现在时间缺失/不完整（E001/E002）的告警态"
            )
        if not holding and self.completeness != Completeness.COMPLETE:
            raise ContractViolation(f"未知 completeness 取值: {self.completeness!r}")

    def _apply_forced_severity(self) -> None:
        """按两条强制规则重算重要级；请求值只作留痕。"""
        severity, forced, reason = resolve_severity(self.category, self.status, self.severity)
        if forced:
            if self.original_severity is None:
                self.original_severity = self.severity
            self.severity = severity
            self.severity_forced = True
            self.severity_forced_reason = reason
        else:
            self.severity = severity
            self.severity_forced = False
            self.severity_forced_reason = ""

    # ---- 查询 -----------------------------------------------------------

    @property
    def is_critical(self) -> bool:
        return self.severity == Severity.CRITICAL

    @property
    def is_transfer_pending(self) -> bool:
        return self.status == EventStatus.TRANSFERRED

    @property
    def is_held_by_alarm(self) -> bool:
        return self.completeness in ALARM_HOLDING_COMPLETENESS

    def can_enter_report(self) -> bool:
        """是否可进入输出清单（告警态一律不行）。"""
        return (
            self.completeness == Completeness.COMPLETE
            and self.occurred_at is not None
            and all(getattr(self, name) is not None for name in REQUIRED_ENUM_FIELDS)
        )

    def require_report_ready(self) -> None:
        if not self.can_enter_report():
            raise ContractViolation(
                f"事件 {self.event_id} 处于告警态（completeness={self.completeness}），"
                "补全/复核并清除告警后才能进入清单"
            )

    def sort_key(self) -> tuple[str, str]:
        """清单时间序排序键（occurred_at 升序，同刻按 event_id 稳定排序）。"""
        return (format_minute(self.occurred_at), self.event_id)

    # ---- 告警态修复（E002 补全 / E003 复核 / E004 补字段） ----------------

    def resolve_time(self, occurred_at: Any, *, note: str = "") -> "HandoverEvent":
        """补全或修正发生时间（调用方须同步清除对应告警）。

        补全后按实际状态刷新 ``completeness``：仍缺关键字段 → ``missing_required``，
        否则 → ``complete``。
        """
        parsed = parse_occurred_at(occurred_at)
        if parsed.value is None or parsed.precision == PRECISION_DATE_ONLY:
            raise ContractViolation("补全仍缺时:分，时间精度必须到分")
        self.occurred_at = parsed.value
        if note:
            self.note = (self.note + ("；" if self.note else "") + note).strip()
        self._refresh_completeness_after_repair()
        return self

    def fill_required(
        self,
        *,
        category: str | None = None,
        severity: str | None = None,
        status: str | None = None,
        note: str = "",
    ) -> "HandoverEvent":
        """补齐 E004 涉及的类别/重要级/状态。"""
        if category is not None:
            self.category = coerce_enum(EventCategory, category, field="category").value
        if status is not None:
            self.status = coerce_enum(EventStatus, status, field="status").value
        if severity is not None:
            self.severity = coerce_enum(Severity, severity, field="severity").value
        if note:
            self.note = (self.note + ("；" if self.note else "") + note).strip()
        self._apply_forced_severity()
        self._refresh_completeness_after_repair()
        return self

    def mark_reviewed(self, *, note: str = "") -> "HandoverEvent":
        """E003 复核通过：确认越界属实并保留（时间不改，解除告警态）。"""
        if self.occurred_at is None:
            raise ContractViolation("时间缺失的事件不能直接复核通过，先补全时间")
        text = note or "复核确认：时间越界属实，保留原记录"
        self.note = (self.note + ("；" if self.note else "") + text).strip()
        self._refresh_completeness_after_repair()
        return self

    def _refresh_completeness_after_repair(self) -> None:
        """按实际字段状态刷新 completeness（告警清态与补全必须同步）。"""
        if self.occurred_at is None:
            self.completeness = Completeness.PARTIAL_TIME
        elif self._missing_required_fields():
            self.completeness = Completeness.MISSING_REQUIRED
        else:
            self.completeness = Completeness.COMPLETE

    def _missing_required_fields(self) -> list[str]:
        return [name for name in REQUIRED_ENUM_FIELDS if getattr(self, name) is None]

    # ---- 输出 -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "shift_id": self.shift_id,
            "category": self.category,
            "category_label": "" if self.category is None else label_of(EventCategory, self.category),
            "description": self.description,
            "occurred_at": format_minute(self.occurred_at),
            "severity": self.severity,
            "severity_label": "" if self.severity is None else label_of(Severity, self.severity),
            "severity_forced": self.severity_forced,
            "severity_forced_reason": self.severity_forced_reason,
            "status": self.status,
            "status_label": "" if self.status is None else label_of(EventStatus, self.status),
            "owner": None if self.owner is None else self.owner.to_dict(),
            "ref_no": self.ref_no,
            "note": self.note,
            "completeness": self.completeness,
        }


@dataclass(frozen=True)
class IntakeResult:
    """一次录入的结果：被拒（E001）或落表（可能带告警）。"""

    rejected: bool
    event: HandoverEvent | None
    alarms: tuple[CompletenessAlarm, ...] = ()
    duplicate: bool = False

    @property
    def stored(self) -> bool:
        return not self.rejected and self.event is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rejected": self.rejected,
            "duplicate": self.duplicate,
            "event_id": None if self.event is None else self.event.event_id,
            "alarms": [alarm.to_dict() for alarm in self.alarms],
        }


class EventStore:
    """某班次的事件集合 + 四条校验的录入闸门。

    - E001：拒收，**不入表**（``IntakeResult.rejected`` / ``event=None``）。
    - E002/E003/E004：入表（``completeness`` 标记告警态）并出告警。
    - 同一 ``event_id`` 重复录入幂等：不双写，不重复出告警。
    """

    def __init__(
        self,
        shift: ShiftRecord,
        ledger: AlarmLedger | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.shift = shift
        self.ledger = ledger if ledger is not None else AlarmLedger(shift_id=shift.shift_id)
        self._clock = clock or now_shanghai
        self._events: dict[str, HandoverEvent] = {}
        self._order: list[str] = []

    # ---- 录入 -----------------------------------------------------------

    def intake(self, raw: Mapping[str, Any]) -> IntakeResult:
        """按四条规则处理一行表格原始数据。"""
        event_id = str(raw.get("event_id") or "").strip()
        if not event_id:
            raise ContractViolation("录入行缺少 event_id")
        shift_id = str(raw.get("shift_id") or self.shift.shift_id).strip()
        if shift_id != self.shift.shift_id:
            raise ContractViolation(
                f"事件 shift_id {shift_id!r} 与本班次 {self.shift.shift_id!r} 不符"
            )
        known = self._events.get(event_id)
        if known is not None:
            return IntakeResult(rejected=False, event=known, alarms=(), duplicate=True)

        now = truncate_to_minute(self._clock())
        description = str(raw.get("description") or "").strip()
        parsed = parse_occurred_at(raw.get("occurred_at"))

        if parsed.value is None and parsed.precision == PRECISION_MISSING:
            # E001：拒收，不落表。
            alarm = CompletenessAlarm.make(
                E001,
                shift_id=self.shift.shift_id,
                event_id=event_id,
                field="occurred_at",
                detail=f"发生时间缺失，拒收且不落表（原始值: {parsed.raw or '空'}）",
                created_at=now,
            )
            self.ledger.record(alarm)
            return IntakeResult(rejected=True, event=None, alarms=(alarm,))

        category = optional_enum(EventCategory, raw.get("category"), field="category")
        status = optional_enum(EventStatus, raw.get("status"), field="status")
        severity_requested = optional_enum(Severity, raw.get("severity"), field="severity")
        severity, forced, reason = resolve_severity(category, status, severity_requested)

        alarms: list[CompletenessAlarm] = []
        completeness = Completeness.COMPLETE
        if parsed.precision == PRECISION_DATE_ONLY:
            completeness = Completeness.PARTIAL_TIME
            alarms.append(
                CompletenessAlarm.make(
                    E002,
                    shift_id=self.shift.shift_id,
                    event_id=event_id,
                    field="occurred_at",
                    detail=f"只有日期没有时:分，需补全（原始值: {parsed.raw}）",
                    created_at=now,
                )
            )
        elif parsed.value is not None and not self.shift.contains(parsed.value):
            completeness = Completeness.OUT_OF_RANGE
            alarms.append(
                CompletenessAlarm.make(
                    E003,
                    shift_id=self.shift.shift_id,
                    event_id=event_id,
                    field="occurred_at",
                    detail=(
                        f"发生时间 {format_minute(parsed.value)} 不在班次区间 "
                        f"{format_minute(self.shift.start_time)}~{format_minute(self.shift.end_time)}，需复核"
                    ),
                    created_at=now,
                )
            )

        missing_fields = [
            name
            for name, value in (
                ("category", category),
                ("severity", severity),
                ("status", status),
            )
            if value is None
        ]
        if missing_fields:
            if completeness == Completeness.COMPLETE:
                completeness = Completeness.MISSING_REQUIRED
            for name in missing_fields:
                alarms.append(
                    CompletenessAlarm.make(
                        E004,
                        shift_id=self.shift.shift_id,
                        event_id=event_id,
                        field=name,
                        detail=f"关键字段缺失: {name}（交班提交前全表复验）",
                        created_at=now,
                    )
                )

        event = HandoverEvent(
            event_id=event_id,
            shift_id=self.shift.shift_id,
            category=category,
            description=description,
            occurred_at=parsed.value,
            severity=severity,
            status=status,
            owner=raw.get("owner"),
            ref_no=raw.get("ref_no", ""),
            note=raw.get("note", ""),
            completeness=completeness,
        )
        self._events[event_id] = event
        self._order.append(event_id)
        self.ledger.record_all(alarms)
        return IntakeResult(rejected=False, event=event, alarms=tuple(alarms))

    def intake_all(self, rows: Iterable[Mapping[str, Any]]) -> tuple[IntakeResult, ...]:
        return tuple(self.intake(row) for row in rows)

    # ---- 查询 -----------------------------------------------------------

    def get(self, event_id: str) -> HandoverEvent | None:
        return self._events.get(event_id)

    @property
    def events(self) -> tuple[HandoverEvent, ...]:
        return tuple(self._events[event_id] for event_id in self._order)

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def complete_count(self) -> int:
        return sum(1 for event in self._events.values() if event.can_enter_report())

    def critical_events(self) -> tuple[HandoverEvent, ...]:
        return tuple(event for event in self.events if event.is_critical)

    def transfer_pending(self) -> tuple[HandoverEvent, ...]:
        return tuple(event for event in self.events if event.is_transfer_pending)

    def held_events(self) -> tuple[HandoverEvent, ...]:
        return tuple(event for event in self.events if event.is_held_by_alarm)

    def require_all_report_ready(self) -> None:
        """告警已清后再次自检：仍有告警态事件说明"清告警"与"补全"未同步。"""
        held = self.held_events()
        if held:
            raise ContractViolation(
                "存在未补全的告警态事件，先补全/复核再出清单: "
                + ", ".join(event.event_id for event in held)
            )

    def copy_event(self, event_id: str, **changes: Any) -> HandoverEvent:
        """按字段生成修订副本（保留原事件不变更，便于留痕）。"""
        event = self._events.get(event_id)
        if event is None:
            raise ContractViolation(f"事件不存在: {event_id}")
        return replace(event, **changes)


__all__ = [
    "EVENT_FIELD_ORDER",
    "FORCE_CATEGORY_MAJOR",
    "FORCE_STATUS_TRANSFERRED",
    "EventStore",
    "HandoverEvent",
    "IntakeResult",
    "REQUIRED_ENUM_FIELDS",
    "resolve_severity",
]
