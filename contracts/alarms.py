"""T01 公共契约：完整性告警 CompletenessAlarm（issue #6 §3）。

四条硬校验与错误码一一对应：

======  ==========================  ==========================
规则     含义                        处置
======  ==========================  ==========================
E001     发生时间缺失                **拒收，不落表**
E002     时间不完整（有日期无时:分）   告警，要求补全
E003     时间越出班次区间             告警复核
E004     关键字段缺失（类别/重要级/状态） 告警
======  ==========================  ==========================

不变量：提交前全表复验存在未清除告警 → ``shift.status = blocked``，
**禁止生成清单**（:meth:`AlarmLedger.require_clear` → :data:`ALARM_NOT_CLEARED`）。
告警带 ``created_at`` / ``cleared_at``，可做"是否已清除"断言。

E003 边界口径（T04 增补，SPEC §0 厂级定位）：**班次区间即驻场期边界**——一次轮换
一张单、覆盖整个驻场期；判定对 ``[start, end]`` 整体区间做，跨零点相连日历日
**均属期内**，不按单日切分（见 :data:`E003_SCOPE_NOTE` 与
:func:`contracts.timebase.is_out_of_window`）。告警文案与既有取值不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .errors import (
    ALARM_CODES,
    ALARM_NOT_CLEARED,
    COMPLETENESS_RULES,
    E001,
    ContractError,
    ContractViolation,
)
from .timebase import ensure_aware, format_minute, truncate_to_minute

#: 告警规则码 → 含义（与错误码说明一致，便于渲染）。
ALARM_RULE_LABELS: dict[str, str] = {
    E001: "发生时间缺失（拒收，不落表）",
    "E002": "时间不完整（有日期无时:分）",
    "E003": "时间越出班次区间",
    "E004": "关键字段缺失（类别/重要级/状态）",
}

#: T04 增补：E003 判定边界说明（厂级口径下"班次区间"= 驻场期边界）。
#: 只声明口径，不改 ``E003`` 的规则码、触发条件与告警文案。
E003_SCOPE_NOTE = (
    "E003 越界判定边界 = 班次区间（厂级口径：驻场期边界）；"
    "对 [start, end] 整体区间判定，跨零点相连日历日均属期内，不按单日切分"
)


def alarm_id_for(rule: str, shift_id: str, event_id: str | None, field_name: str) -> str:
    """稳定可断言的告警 ID（同一规则/事件/字段只留一条，达到幂等）。"""
    return f"{rule}:{shift_id}:{event_id or '-'}:{field_name}"


@dataclass
class CompletenessAlarm:
    """单条完整性告警。"""

    rule: str
    shift_id: str
    field: str
    detail: str
    created_at: datetime
    event_id: str | None = None
    cleared_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.rule not in COMPLETENESS_RULES:
            raise ContractViolation(
                f"未知完整性规则 {self.rule!r}（可用: {', '.join(COMPLETENESS_RULES)}）"
            )
        if not str(self.field).strip():
            raise ContractViolation("告警 field 不能为空")
        self.created_at = truncate_to_minute(
            ensure_aware(self.created_at, field="alarm.created_at")
        )
        if self.cleared_at is not None:
            self.cleared_at = truncate_to_minute(
                ensure_aware(self.cleared_at, field="alarm.cleared_at")
            )

    @classmethod
    def make(
        cls,
        rule: str,
        *,
        shift_id: str,
        field: str,
        detail: str,
        created_at: datetime,
        event_id: str | None = None,
    ) -> "CompletenessAlarm":
        return cls(
            rule=rule,
            shift_id=shift_id,
            field=field,
            detail=detail,
            created_at=created_at,
            event_id=event_id,
        )

    @property
    def alarm_id(self) -> str:
        return alarm_id_for(self.rule, self.shift_id, self.event_id, self.field)

    @property
    def is_open(self) -> bool:
        return self.cleared_at is None

    @property
    def is_cleared(self) -> bool:
        return self.cleared_at is not None

    @property
    def is_rejecting(self) -> bool:
        """是否属于"拒收"（E001 不落表）。"""
        return self.rule == E001

    def clear(self, at: datetime) -> "CompletenessAlarm":
        """清除告警；已在旧时刻清除过则保持首次清除时间（幂等）。"""
        return self.cleared_at or self._set_cleared(at)

    def _set_cleared(self, at: datetime) -> "CompletenessAlarm":
        self.cleared_at = truncate_to_minute(ensure_aware(at, field="alarm.cleared_at"))
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "alarm_id": self.alarm_id,
            "rule": self.rule,
            "rule_label": ALARM_RULE_LABELS.get(self.rule, self.rule),
            "shift_id": self.shift_id,
            "event_id": self.event_id,
            "field": self.field,
            "detail": self.detail,
            "created_at": format_minute(self.created_at),
            "cleared_at": format_minute(self.cleared_at),
            "is_open": self.is_open,
        }


@dataclass
class AlarmLedger:
    """某个班次的告警账本。"""

    shift_id: str
    _alarms: dict[str, CompletenessAlarm] = field(default_factory=dict)

    def record(self, alarm: CompletenessAlarm) -> CompletenessAlarm:
        """登记告警；同一 ``alarm_id`` 幂等（保留首次登记，不重复出告警）。"""
        if alarm.shift_id != self.shift_id:
            raise ContractViolation(
                f"告警属于班次 {alarm.shift_id}，不能登记到 {self.shift_id}"
            )
        existing = self._alarms.get(alarm.alarm_id)
        if existing is not None:
            return existing
        self._alarms[alarm.alarm_id] = alarm
        return alarm

    def record_all(self, alarms: Iterable[CompletenessAlarm]) -> tuple[CompletenessAlarm, ...]:
        return tuple(self.record(alarm) for alarm in alarms)

    def get(self, alarm_id: str) -> CompletenessAlarm | None:
        return self._alarms.get(alarm_id)

    def clear(self, alarm_id: str, at: datetime) -> CompletenessAlarm:
        alarm = self._alarms.get(alarm_id)
        if alarm is None:
            raise ContractViolation(f"告警不存在: {alarm_id}")
        return alarm.clear(at)

    def clear_event(self, event_id: str, at: datetime) -> tuple[CompletenessAlarm, ...]:
        """清除某事件名下全部未清告警（补全/复核完成后调用）。"""
        cleared = []
        for alarm in self._alarms.values():
            if alarm.event_id == event_id and alarm.is_open:
                cleared.append(alarm.clear(at))
        return tuple(cleared)

    def open_alarms(self) -> tuple[CompletenessAlarm, ...]:
        return tuple(
            alarm for alarm in sorted(self._alarms.values(), key=lambda a: a.alarm_id) if alarm.is_open
        )

    def all_alarms(self) -> tuple[CompletenessAlarm, ...]:
        return tuple(sorted(self._alarms.values(), key=lambda a: a.alarm_id))

    def counts_by_rule(self) -> dict[str, int]:
        counts = {rule: 0 for rule in COMPLETENESS_RULES}
        for alarm in self._alarms.values():
            counts[alarm.rule] = counts.get(alarm.rule, 0) + 1
        return counts

    @property
    def has_open(self) -> bool:
        return any(alarm.is_open for alarm in self._alarms.values())

    @property
    def open_count(self) -> int:
        return len(self.open_alarms())

    @property
    def alarm_count(self) -> int:
        """告警总数（含已清除），对应清单的 alarm_count。"""
        return len(self._alarms)

    def require_clear(self) -> None:
        """提交/出清单前的闸门：有未清告警即拒绝。"""
        opened = self.open_alarms()
        if opened:
            raise ContractError(
                ALARM_NOT_CLEARED,
                f"存在 {len(opened)} 条未清除告警，禁止提交或生成清单",
                detail={
                    "shift_id": self.shift_id,
                    "open_count": len(opened),
                    "rules": sorted({alarm.rule for alarm in opened}),
                    "alarm_ids": [alarm.alarm_id for alarm in opened],
                },
            )

    def summary(self, *, event_total: int, event_complete: int) -> dict[str, Any]:
        """清单"完整性校验结果"段的数据源（issue §4：total/complete/alarm_count）。"""
        rate = 1.0 if event_total <= 0 else round(event_complete / event_total, 4)
        return {
            "total": event_total,
            "complete": event_complete,
            "time_complete_rate": rate,
            "alarm_count": self.alarm_count,
            "open_alarm_count": self.open_count,
        }


__all__ = [
    "ALARM_RULE_LABELS",
    "E003_SCOPE_NOTE",
    "AlarmLedger",
    "CompletenessAlarm",
    "alarm_id_for",
]
