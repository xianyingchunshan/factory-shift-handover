"""契约对象 ↔ 表格原始字符串列的编解码（issue #9 第 2 段）。

口径：

- 列名**不硬编码**：一律走 :mod:`contracts.aitable_mapping` 的 ``mapping_table``，
  契约列变了这里跟着变。
- 表格列存**原始字符串**：时间写 ``YYYY-MM-DD HH:MM``（Asia/Shanghai），
  身份写 ``source:user_id``（展示名不落列），布尔写 ``true``/``false``，
  配置快照写紧凑 JSON；枚举列写规范值字符串。
- **枚举校验在业务层**：读回时用 :func:`contracts.enums.coerce_enum` 校验，
  未知取值抛 :class:`~contracts.errors.ContractViolation`（数据脏，不放行）；
  同时接受中文标签（复用契约的中文↔enum 映射）。

已知边界（写进 PR 待主控确认项）：

- ``occurred_at`` 只有日期（E002 告警态）时，事件对象的 ``occurred_at`` 为 ``None``，
  表格里只能落“空时间 + completeness=partial_time”这一事实；
  原始日期文本由阶段①的输入留痕保存（见 :mod:`workflow.state`），表格本身不重复存。
- 班次 ``blocked_from`` / ``config_revision`` / ``snapshot_history`` 属契约内部字段，
  不单独落列（契约 :data:`~contracts.aitable_mapping.SHIFT_INTERNAL_FIELDS`）。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Mapping

from contracts.aitable_mapping import (
    EVENT_FIELD_MAPPINGS,
    SHIFT_FIELD_MAPPINGS,
    mapping_table,
)
from contracts.enums import (
    Completeness,
    EventCategory,
    EventStatus,
    Severity,
    ShiftName,
    ShiftStatus,
    coerce_enum,
    is_blank,
)
from contracts.errors import ContractViolation
from contracts.events import HandoverEvent
from contracts.identity import IdentityRef
from contracts.shift import ConfigSnapshot, ShiftRecord
from contracts.timebase import format_minute, parse_occurred_at

#: 契约字段 → 表格列（顺序即列顺序）。
SHIFT_COLUMNS: Mapping[str, str] = mapping_table(SHIFT_FIELD_MAPPINGS)
EVENT_COLUMNS: Mapping[str, str] = mapping_table(EVENT_FIELD_MAPPINGS)

IDENTITY_SEP = ":"
BOOL_TRUE = "true"
BOOL_FALSE = "false"

_TRUTHY = {"true", "1", "是", "y", "yes"}


# ---- 基础单元格 ---------------------------------------------------------


def identity_to_cell(ref: IdentityRef | None) -> str:
    """身份 → ``source:user_id``（展示名不落列）。"""
    if ref is None:
        return ""
    return f"{ref.source}{IDENTITY_SEP}{ref.user_id}"


def identity_from_cell(text: Any) -> IdentityRef | None:
    """``source:user_id`` → 身份；空值返回 ``None``，格式或身份源非法即拒绝。"""
    if is_blank(text):
        return None
    raw = str(text).strip()
    source, sep, user_id = raw.partition(IDENTITY_SEP)
    if not sep or not source.strip() or not user_id.strip():
        raise ContractViolation(f"身份单元格格式非法: {raw!r}（期望 source:user_id）")
    return IdentityRef(source=source.strip(), user_id=user_id.strip())


def moment_to_cell(moment: Any) -> str:
    """时间 → ``YYYY-MM-DD HH:MM``；``None`` → 空串。"""
    return format_minute(moment) if moment is not None else ""


def moment_from_cell(text: Any) -> Any:
    """时间文本 → :class:`~contracts.timebase.TimeParseResult`（调用方按精度判定）。"""
    return parse_occurred_at(text)


def date_to_cell(value: date | None) -> str:
    return "" if value is None else value.isoformat()


def date_from_cell(text: Any, *, field: str = "日期") -> date | None:
    if is_blank(text):
        return None
    raw = str(text).strip()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ContractViolation(f"{field} 无法解析: {raw!r}（期望 YYYY-MM-DD）") from exc


def bool_to_cell(flag: Any) -> str:
    return BOOL_TRUE if bool(flag) else BOOL_FALSE


def bool_from_cell(text: Any) -> bool:
    if is_blank(text):
        return False
    return str(text).strip().lower() in _TRUTHY


def json_to_cell(payload: Mapping[str, Any] | None) -> str:
    if not payload:
        return ""
    return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_from_cell(text: Any, *, field: str = "配置快照") -> dict[str, Any]:
    if is_blank(text):
        return {}
    try:
        payload = json.loads(str(text))
    except json.JSONDecodeError as exc:
        raise ContractViolation(f"{field} 不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractViolation(f"{field} 必须是 JSON 对象")
    return payload


def _enum_to_cell(enum_cls: type, value: Any) -> str:
    if value is None:
        return ""
    return coerce_enum(enum_cls, value, field=enum_cls.__name__).value


def _enum_from_cell(enum_cls: type, text: Any, *, field: str, required: bool = False) -> str | None:
    """读回并**在业务层校验**枚举；未知取值抛 ContractViolation。"""
    if is_blank(text):
        if required:
            raise ContractViolation(f"{field} 列不能为空（表格数据不完整）")
        return None
    return coerce_enum(enum_cls, text, field=field).value


def required_cell(fields: Mapping[str, Any], column: str) -> str:
    """取必填列；缺失或空白 → 拒绝（表格数据完整性由业务层兜底）。"""
    raw = fields.get(column, "")
    if is_blank(raw):
        raise ContractViolation(f"表格列 {column!r} 不能为空")
    return str(raw).strip()


# ---- 班次行 -------------------------------------------------------------


def shift_to_fields(shift: ShiftRecord) -> dict[str, str]:
    """班次 → 表格行（原始字符串；enum 列写规范值）。"""
    snapshot = shift.config_snapshot.to_dict() if shift.config_snapshot else {}
    fields = {
        SHIFT_COLUMNS["shift_id"]: str(shift.shift_id),
        SHIFT_COLUMNS["handover_line"]: str(shift.handover_line),
        SHIFT_COLUMNS["shift_date"]: date_to_cell(shift.shift_date),
        SHIFT_COLUMNS["shift_name"]: _enum_to_cell(ShiftName, shift.shift_name),
        SHIFT_COLUMNS["start_time"]: moment_to_cell(shift.start_time),
        SHIFT_COLUMNS["end_time"]: moment_to_cell(shift.end_time),
        SHIFT_COLUMNS["handover_from"]: identity_to_cell(shift.handover_from),
        SHIFT_COLUMNS["handover_to"]: identity_to_cell(shift.handover_to),
        SHIFT_COLUMNS["status"]: _enum_to_cell(ShiftStatus, shift.status),
        SHIFT_COLUMNS["config_snapshot"]: json_to_cell(snapshot),
    }
    return fields


def shift_from_fields(fields: Mapping[str, Any]) -> ShiftRecord:
    """表格行 → 班次；必填列缺失或枚举非法即拒绝（业务层校验）。"""
    shift_id = required_cell(fields, SHIFT_COLUMNS["shift_id"])
    handover_line = required_cell(fields, SHIFT_COLUMNS["handover_line"])
    shift_date = date_from_cell(fields.get(SHIFT_COLUMNS["shift_date"]))
    if shift_date is None:
        raise ContractViolation(f"表格列 {SHIFT_COLUMNS['shift_date']!r} 不能为空")
    shift_name = _enum_from_cell(
        ShiftName, fields.get(SHIFT_COLUMNS["shift_name"]), field="班次", required=True
    )
    status = _enum_from_cell(
        ShiftStatus, fields.get(SHIFT_COLUMNS["status"]), field="状态", required=True
    )
    start = moment_from_cell(fields.get(SHIFT_COLUMNS["start_time"]))
    end = moment_from_cell(fields.get(SHIFT_COLUMNS["end_time"]))
    if start.value is None or end.value is None:
        raise ContractViolation("班次开始/结束时间必须精确到分")
    handover_from = identity_from_cell(fields.get(SHIFT_COLUMNS["handover_from"]))
    handover_to = identity_from_cell(fields.get(SHIFT_COLUMNS["handover_to"]))
    if handover_from is None or handover_to is None:
        raise ContractViolation("班次交班人/接班人不能为空")
    snapshot_payload = json_from_cell(fields.get(SHIFT_COLUMNS["config_snapshot"]))
    snapshot = None
    if snapshot_payload:
        locked = moment_from_cell(snapshot_payload.get("locked_at"))
        if locked.value is None:
            raise ContractViolation("配置快照缺少 locked_at")
        snapshot = ConfigSnapshot(
            handover_line=snapshot_payload.get("handover_line", handover_line),
            handover_from=snapshot_payload.get("handover_from", handover_from.to_dict()),
            handover_to=snapshot_payload.get("handover_to", handover_to.to_dict()),
            locked_at=locked.value,
            critical_standard=tuple(snapshot_payload.get("critical_standard", ()) or ()),
        )
    return ShiftRecord(
        shift_id=shift_id,
        handover_line=handover_line,
        shift_date=shift_date,
        shift_name=shift_name,
        start_time=start.value,
        end_time=end.value,
        handover_from=handover_from,
        handover_to=handover_to,
        status=status,
        config_snapshot=snapshot,
    )


# ---- 事件行 -------------------------------------------------------------


def event_to_fields(event: HandoverEvent, *, occurred_at_raw: str | None = None) -> dict[str, str]:
    """交接事件 → 表格行（原始字符串；enum 列写规范值）。

    ``occurred_at_raw``：事件处于"时间不完整"告警态（``occurred_at is None``）时，
    把交班人输入的**原始时间文本**原样落列（表格列存原始字符串）；这样重启后
    按表格列复验能得出同样的 E002，不丢原始输入。
    """
    return {
        EVENT_COLUMNS["event_id"]: str(event.event_id),
        EVENT_COLUMNS["shift_id"]: str(event.shift_id),
        EVENT_COLUMNS["category"]: _enum_to_cell(EventCategory, event.category),
        EVENT_COLUMNS["description"]: str(event.description),
        EVENT_COLUMNS["occurred_at"]: (
            moment_to_cell(event.occurred_at)
            if event.occurred_at is not None
            else str(occurred_at_raw or "")
        ),
        EVENT_COLUMNS["severity"]: _enum_to_cell(Severity, event.severity),
        EVENT_COLUMNS["status"]: _enum_to_cell(EventStatus, event.status),
        EVENT_COLUMNS["owner"]: identity_to_cell(event.owner),
        EVENT_COLUMNS["ref_no"]: str(event.ref_no or ""),
        EVENT_COLUMNS["note"]: str(event.note or ""),
        EVENT_COLUMNS["completeness"]: _enum_to_cell(Completeness, event.completeness),
        EVENT_COLUMNS["severity_forced"]: bool_to_cell(event.severity_forced),
        EVENT_COLUMNS["severity_forced_reason"]: str(event.severity_forced_reason or ""),
        EVENT_COLUMNS["original_severity"]: _enum_to_cell(Severity, event.original_severity),
    }


def event_from_fields(fields: Mapping[str, Any]) -> HandoverEvent:
    """表格行 → 交接事件；枚举列在业务层校验，未知取值即拒绝。"""
    occurred = moment_from_cell(fields.get(EVENT_COLUMNS["occurred_at"]))
    return HandoverEvent(
        event_id=required_cell(fields, EVENT_COLUMNS["event_id"]),
        shift_id=required_cell(fields, EVENT_COLUMNS["shift_id"]),
        category=_enum_from_cell(
            EventCategory, fields.get(EVENT_COLUMNS["category"]), field="类别"
        ),
        description=required_cell(fields, EVENT_COLUMNS["description"]),
        occurred_at=occurred.value,
        severity=_enum_from_cell(Severity, fields.get(EVENT_COLUMNS["severity"]), field="重要级"),
        status=_enum_from_cell(EventStatus, fields.get(EVENT_COLUMNS["status"]), field="状态"),
        owner=identity_from_cell(fields.get(EVENT_COLUMNS["owner"])),
        ref_no=fields.get(EVENT_COLUMNS["ref_no"], ""),
        note=fields.get(EVENT_COLUMNS["note"], ""),
        completeness=_enum_from_cell(
            Completeness,
            fields.get(EVENT_COLUMNS["completeness"]),
            field="完整性",
            required=True,
        ),
        severity_forced=bool_from_cell(fields.get(EVENT_COLUMNS["severity_forced"])),
        severity_forced_reason=fields.get(EVENT_COLUMNS["severity_forced_reason"], ""),
        original_severity=_enum_from_cell(
            Severity, fields.get(EVENT_COLUMNS["original_severity"]), field="原报重要级"
        ),
    )


def event_id_from_fields(fields: Mapping[str, Any]) -> str:
    return required_cell(fields, EVENT_COLUMNS["event_id"])


def shift_id_from_fields(fields: Mapping[str, Any]) -> str:
    return required_cell(fields, EVENT_COLUMNS["shift_id"])


def event_raw_from_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """表格行 → 录入用原始映射（供"以表格为准重新复验"使用）。

    与 :func:`event_to_fields` 往返：身份转回映射（``EventStore.intake`` 只接受
    身份映射/对象），时间保留列里的原始文本。
    """
    owner = identity_from_cell(fields.get(EVENT_COLUMNS["owner"]))
    return {
        "event_id": required_cell(fields, EVENT_COLUMNS["event_id"]),
        "shift_id": required_cell(fields, EVENT_COLUMNS["shift_id"]),
        "category": fields.get(EVENT_COLUMNS["category"], ""),
        "description": required_cell(fields, EVENT_COLUMNS["description"]),
        "occurred_at": fields.get(EVENT_COLUMNS["occurred_at"], ""),
        "severity": fields.get(EVENT_COLUMNS["severity"], ""),
        "status": fields.get(EVENT_COLUMNS["status"], ""),
        "owner": None if owner is None else owner.to_dict(),
        "ref_no": fields.get(EVENT_COLUMNS["ref_no"], ""),
        "note": fields.get(EVENT_COLUMNS["note"], ""),
    }


__all__ = [
    "BOOL_FALSE",
    "BOOL_TRUE",
    "EVENT_COLUMNS",
    "IDENTITY_SEP",
    "SHIFT_COLUMNS",
    "bool_from_cell",
    "bool_to_cell",
    "date_from_cell",
    "date_to_cell",
    "event_from_fields",
    "event_id_from_fields",
    "event_raw_from_fields",
    "event_to_fields",
    "identity_from_cell",
    "identity_to_cell",
    "json_from_cell",
    "json_to_cell",
    "moment_from_cell",
    "moment_to_cell",
    "required_cell",
    "shift_from_fields",
    "shift_id_from_fields",
    "shift_to_fields",
]
