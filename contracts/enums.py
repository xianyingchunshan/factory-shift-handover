"""T01 公共契约：枚举取值与中文别名（issue #6 §1/§2/§5）。

约定（issue #6 §6）：**表格列存原始字符串，枚举校验在业务层**。
本模块的规范化入口同时接受两种写法：

- 规范值（ASCII，落库/落契约用），例如 ``"major"``；
- 中文标签（录入便利），例如 ``"重大事项"``。

空值（None / 空串 / 纯空白）在 :func:`optional_enum` 中统一归一为 ``None``，
由调用方按 E004（关键字段缺失）出告警；取值既非空也不认识时抛
:class:`~contracts.errors.ContractViolation`（调用方缺陷，不是业务告警）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypeVar

from .errors import ContractViolation

_E = TypeVar("_E", bound=StrEnum)


class EventCategory(StrEnum):
    """交接事件九类（SPEC 第二版 §1，顺序即文档顺序）。"""

    RUNTIME = "runtime"
    EQUIPMENT = "equipment"
    DEFECT = "defect"
    HAZARD = "hazard"
    MAJOR = "major"
    TWO_TICKET = "two_ticket"
    FIELD_WORK = "field_work"
    DIRECTIVE = "directive"
    OTHER = "other"


CATEGORY_LABELS: dict[EventCategory, str] = {
    EventCategory.RUNTIME: "运行工况",
    EventCategory.EQUIPMENT: "设备情况",
    EventCategory.DEFECT: "缺陷完成情况",
    EventCategory.HAZARD: "隐患完成情况",
    EventCategory.MAJOR: "重大事项",
    EventCategory.TWO_TICKET: "两票执行",
    EventCategory.FIELD_WORK: "作业与外委",
    EventCategory.DIRECTIVE: "上级指令与通知",
    EventCategory.OTHER: "其他",
}

#: 九类，顺序固定；后续模块可直接遍历生成表格列或清单分组。
CATEGORY_ORDER: tuple[EventCategory, ...] = tuple(CATEGORY_LABELS)
CATEGORY_COUNT = 9


class Severity(StrEnum):
    """重要级。"""

    CRITICAL = "critical"
    NORMAL = "normal"


SEVERITY_LABELS: dict[Severity, str] = {
    Severity.CRITICAL: "关键",
    Severity.NORMAL: "一般",
}


class EventStatus(StrEnum):
    """事项状态。"""

    IN_PROGRESS = "in_progress"
    DONE = "done"
    TRANSFERRED = "transferred"


EVENT_STATUS_LABELS: dict[EventStatus, str] = {
    EventStatus.IN_PROGRESS: "进行中",
    EventStatus.DONE: "已办",
    EventStatus.TRANSFERRED: "移交接班人",
}


class ShiftName(StrEnum):
    """班次名。"""

    EARLY = "early"
    MIDDLE = "middle"
    LATE = "late"
    CUSTOM = "custom"


SHIFT_NAME_LABELS: dict[ShiftName, str] = {
    ShiftName.EARLY: "早",
    ShiftName.MIDDLE: "中",
    ShiftName.LATE: "晚",
    ShiftName.CUSTOM: "自定义",
}


class ShiftStatus(StrEnum):
    """班次状态机；``blocked`` = 告警未清（可回到 previous 状态）。"""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    ARCHIVED = "archived"
    BLOCKED = "blocked"


SHIFT_STATUS_LABELS: dict[ShiftStatus, str] = {
    ShiftStatus.DRAFT: "草稿",
    ShiftStatus.SUBMITTED: "已提交",
    ShiftStatus.CONFIRMED: "接班已确认",
    ShiftStatus.ARCHIVED: "已归档",
    ShiftStatus.BLOCKED: "告警未清",
}


class Completeness(StrEnum):
    """四条校验的产出标记，落表列 ``completeness``。"""

    COMPLETE = "complete"
    MISSING_TIME = "missing_time"
    PARTIAL_TIME = "partial_time"
    OUT_OF_RANGE = "out_of_range"
    MISSING_REQUIRED = "missing_required"


COMPLETENESS_LABELS: dict[Completeness, str] = {
    Completeness.COMPLETE: "完整",
    Completeness.MISSING_TIME: "时间缺失",
    Completeness.PARTIAL_TIME: "时间不完整",
    Completeness.OUT_OF_RANGE: "时间越界",
    Completeness.MISSING_REQUIRED: "关键字段缺失",
}

#: 允许"事件保留在表内但进不了清单"的 completeness 取值。
ALARM_HOLDING_COMPLETENESS: frozenset[Completeness] = frozenset(
    {
        Completeness.MISSING_TIME,
        Completeness.PARTIAL_TIME,
        Completeness.OUT_OF_RANGE,
        Completeness.MISSING_REQUIRED,
    }
)


class WritebackStatus(StrEnum):
    """外部写入结果词表（AGENTS.md：写入不等于完成，回读才算）。"""

    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"
    VERIFIED = "verified"
    NOT_APPLIED = "not_applied"


WRITEBACK_LABELS: dict[WritebackStatus, str] = {
    WritebackStatus.NOT_SENT: "未发送",
    WritebackStatus.UNKNOWN: "受理不明（只回查）",
    WritebackStatus.VERIFIED: "已回读确认",
    WritebackStatus.NOT_APPLIED: "未生效",
}


class ConfirmationStatus(StrEnum):
    """确认回写状态机：pending → confirmed → written_back。"""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    WRITTEN_BACK = "written_back"


CONFIRMATION_LABELS: dict[ConfirmationStatus, str] = {
    ConfirmationStatus.PENDING: "待确认",
    ConfirmationStatus.CONFIRMED: "已确认",
    ConfirmationStatus.WRITTEN_BACK: "已回写",
}


class IdentitySource(StrEnum):
    """可信身份源不唯一（Probe-01 三源实测成立）。"""

    DINGTALK = "dingtalk"
    EAM = "eam"
    EHR = "ehr"


IDENTITY_SOURCE_LABELS: dict[IdentitySource, str] = {
    IdentitySource.DINGTALK: "钉钉",
    IdentitySource.EAM: "EAM",
    IdentitySource.EHR: "EHR",
}


class ConfirmationScope(StrEnum):
    """确认单元粒度：关键级逐项（event 级待办）/ 一般事务整单（shift 级待办）。"""

    EVENT = "event"
    SHIFT = "shift"


CONFIRMATION_SCOPE_LABELS: dict[ConfirmationScope, str] = {
    ConfirmationScope.EVENT: "关键事项逐项",
    ConfirmationScope.SHIFT: "一般事务整单",
}


_LABEL_TABLES: dict[type, dict[Any, str]] = {
    EventCategory: CATEGORY_LABELS,  # type: ignore[dict-item]
    Severity: SEVERITY_LABELS,  # type: ignore[dict-item]
    EventStatus: EVENT_STATUS_LABELS,  # type: ignore[dict-item]
    ShiftName: SHIFT_NAME_LABELS,  # type: ignore[dict-item]
    ShiftStatus: SHIFT_STATUS_LABELS,  # type: ignore[dict-item]
    Completeness: COMPLETENESS_LABELS,  # type: ignore[dict-item]
    WritebackStatus: WRITEBACK_LABELS,  # type: ignore[dict-item]
    ConfirmationStatus: CONFIRMATION_LABELS,  # type: ignore[dict-item]
    IdentitySource: IDENTITY_SOURCE_LABELS,  # type: ignore[dict-item]
    ConfirmationScope: CONFIRMATION_SCOPE_LABELS,  # type: ignore[dict-item]
}

_ALIAS_CACHE: dict[type, dict[str, Any]] = {}


def is_blank(value: Any) -> bool:
    """None / 空串 / 纯空白 视为"未填"。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def label_table(enum_cls: type) -> dict[Any, str]:
    """该枚举的中文标签表。"""
    return dict(_LABEL_TABLES[enum_cls])


def alias_index(enum_cls: type) -> dict[str, Any]:
    """规范值 + 中文标签 → 成员的索引（缓存）。"""
    index = _ALIAS_CACHE.get(enum_cls)
    if index is None:
        index = {member.value: member for member in enum_cls}
        for member, label in _LABEL_TABLES.get(enum_cls, {}).items():
            index.setdefault(label, member)
        _ALIAS_CACHE[enum_cls] = index
    return index


def coerce_enum(enum_cls: type, value: Any, *, field: str | None = None) -> Any:
    """把取值规范化为枚举成员；空值或未知取值抛 :class:`ContractViolation`。"""
    name = field or getattr(enum_cls, "__name__", str(enum_cls))
    if is_blank(value):
        raise ContractViolation(f"{name} 不能为空")
    if isinstance(value, enum_cls):
        return value
    member = alias_index(enum_cls).get(str(value).strip())
    if member is None:
        allowed = ", ".join(member.value for member in enum_cls)
        raise ContractViolation(f"{name} 取值非法: {value!r}（可用: {allowed}）")
    return member


def optional_enum(enum_cls: type, value: Any, *, field: str | None = None) -> Any:
    """空 → ``None``（供 E004 判定）；否则同 :func:`coerce_enum`。"""
    if is_blank(value):
        return None
    return coerce_enum(enum_cls, value, field=field)


def label_of(enum_cls: type, value: Any) -> str:
    """取中文标签；用于清单渲染。未知取值回落为原字符串。"""
    member = alias_index(enum_cls).get(str(value).strip())
    if member is None:
        return str(value)
    return _LABEL_TABLES[enum_cls][member]


def value_of(enum_cls: type, value: Any) -> str:
    """取规范值字符串（落表用）。"""
    return str(coerce_enum(enum_cls, value))


__all__ = [
    "ALARM_HOLDING_COMPLETENESS",
    "CATEGORY_COUNT",
    "CATEGORY_LABELS",
    "CATEGORY_ORDER",
    "COMPLETENESS_LABELS",
    "CONFIRMATION_LABELS",
    "CONFIRMATION_SCOPE_LABELS",
    "Completeness",
    "ConfirmationScope",
    "ConfirmationStatus",
    "EVENT_STATUS_LABELS",
    "EventCategory",
    "EventStatus",
    "IDENTITY_SOURCE_LABELS",
    "IdentitySource",
    "SEVERITY_LABELS",
    "SHIFT_NAME_LABELS",
    "SHIFT_STATUS_LABELS",
    "Severity",
    "ShiftName",
    "ShiftStatus",
    "WRITEBACK_LABELS",
    "WritebackStatus",
    "alias_index",
    "coerce_enum",
    "is_blank",
    "label_of",
    "label_table",
    "optional_enum",
    "value_of",
]
