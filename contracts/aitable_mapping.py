"""T01 公共契约：钉钉AI表格字段映射（issue #6 §6）。

口径：

- 班次表 ↔ :class:`~contracts.shift.ShiftRecord`、交接事件表 ↔
  :class:`~contracts.events.HandoverEvent`，**一一对应**。
- 表格列存**原始字符串**，枚举校验在业务层；校验结果回写 ``completeness`` 列
  （告警明细单独走告警账本，不占事件表列）。
- 表 ID / 视图 ID / 群 ID 等**由运行时配置提供，不进仓库**（AGENTS.md 隐私规则）。

``unmapped_fields`` 用于自检"契约字段无一遗漏列"：后续模块不必猜字段。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Iterable

#: 表格名称（与 issue §2 一致）。
SHIFT_TABLE = "班次表"
EVENT_TABLE = "交接事件表"

TABLE_ID_NOTE = "表 ID / 视图 ID 由运行时配置提供，不入仓库、不写死"

RAW_STRING_NOTE = "表格列存原始字符串；枚举校验在业务层，校验结果回写 completeness 列"

ENUM_COLUMNS: tuple[str, ...] = ("shift_name", "status", "category", "severity", "completeness")


@dataclass(frozen=True)
class FieldMapping:
    """契约字段 ↔ 表格列。"""

    field: str
    column: str
    kind: str
    required: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "column": self.column,
            "kind": self.kind,
            "required": self.required,
            "note": self.note,
        }


#: 班次表列（顺序即建议列顺序）。
SHIFT_FIELD_MAPPINGS: tuple[FieldMapping, ...] = (
    FieldMapping("shift_id", "班次ID", "string", True, "稳定唯一，幂等键"),
    FieldMapping("handover_line", "交接线", "string", True, "多方向，配置决定"),
    FieldMapping("shift_date", "日期", "date", True, "业务日期，跨零点班次取起始日"),
    FieldMapping("shift_name", "班次", "enum", True, "早/中/晚/自定义"),
    FieldMapping("start_time", "开始时间", "datetime+tz", True, "Asia/Shanghai"),
    FieldMapping("end_time", "结束时间", "datetime+tz", True, "E003 越界依据"),
    FieldMapping("handover_from", "交班人", "identity_ref", True, "source+user_id"),
    FieldMapping("handover_to", "接班人", "identity_ref", True, "确认待办指向此人"),
    FieldMapping("status", "状态", "enum", True, "draft/submitted/confirmed/archived/blocked"),
    FieldMapping("config_snapshot", "配置快照", "json", True, "建立时锁定，变更须显式确认"),
)

#: 班次表不单独落列的契约内部字段（随状态/快照体现）。
SHIFT_INTERNAL_FIELDS: tuple[str, ...] = (
    "config_revision",
    "snapshot_history",
    "blocked_from",
)

#: 交接事件表列（顺序即建议列顺序，九类统一字段）。
EVENT_FIELD_MAPPINGS: tuple[FieldMapping, ...] = (
    FieldMapping("event_id", "事件ID", "string", True, "唯一"),
    FieldMapping("shift_id", "班次ID", "ref", True, "关联班次表"),
    FieldMapping("category", "类别", "enum", True, "九类，缺失→E004"),
    FieldMapping("description", "事项描述", "string", True, "必填"),
    FieldMapping("occurred_at", "发生时间", "datetime+tz", True, "必填精确到分；缺失→E001"),
    FieldMapping("severity", "重要级", "enum", True, "critical/normal；两条强制规则不可降"),
    FieldMapping("status", "状态", "enum", True, "in_progress/done/transferred"),
    FieldMapping("owner", "责任人", "identity_ref", False, "一般事项可空"),
    FieldMapping("ref_no", "关联编号", "string", False, "EAM 缺陷号/隐患编号/票号"),
    FieldMapping("note", "备注", "string", False, "可空"),
    FieldMapping(
        "completeness",
        "完整性",
        "enum",
        False,
        "四条校验产出；告警态不得进清单",
    ),
    FieldMapping("severity_forced", "关键级强制", "bool", False, "两条强制升级留痕"),
    FieldMapping("severity_forced_reason", "强制原因", "string", False, "category_major/status_transferred"),
    FieldMapping("original_severity", "原报重要级", "enum", False, "被强制升级前的申报值"),
)

#: 事件表不单独落列的契约内部字段。
EVENT_INTERNAL_FIELDS: tuple[str, ...] = ()


def _mapped_names(mappings: Iterable[FieldMapping]) -> set[str]:
    return {mapping.field for mapping in mappings}


def unmapped_fields(model: Any, mappings: Iterable[FieldMapping]) -> set[str]:
    """契约 dataclass 中没被映射到表格列的字段（期望为空或仅内部字段）。"""
    if not is_dataclass(model):
        raise TypeError("unmapped_fields 需要 dataclass 类型或实例")
    names = {item.name for item in fields(model)}
    return names - _mapped_names(mappings)


def mapping_table(mappings: Iterable[FieldMapping]) -> dict[str, str]:
    """``字段 → 列名`` 的速查表。"""
    return {mapping.field: mapping.column for mapping in mappings}


def column_table(mappings: Iterable[FieldMapping]) -> dict[str, str]:
    """``列名 → 字段`` 的速查表。"""
    return {mapping.column: mapping.field for mapping in mappings}


def enum_field_names() -> tuple[str, ...]:
    """需要在业务层做枚举校验的字段名（表格列存原始字符串）。"""
    return ENUM_COLUMNS


def mapping_doc() -> dict[str, Any]:
    """给后续模块与审查者看的映射说明（不含任何真实表 ID）。"""
    return {
        "shift_table": SHIFT_TABLE,
        "event_table": EVENT_TABLE,
        "table_id_note": TABLE_ID_NOTE,
        "raw_string_note": RAW_STRING_NOTE,
        "shift_columns": [mapping.to_dict() for mapping in SHIFT_FIELD_MAPPINGS],
        "event_columns": [mapping.to_dict() for mapping in EVENT_FIELD_MAPPINGS],
        "shift_internal_fields": list(SHIFT_INTERNAL_FIELDS),
        "event_internal_fields": list(EVENT_INTERNAL_FIELDS),
    }


__all__ = [
    "ENUM_COLUMNS",
    "EVENT_FIELD_MAPPINGS",
    "EVENT_INTERNAL_FIELDS",
    "EVENT_TABLE",
    "FieldMapping",
    "RAW_STRING_NOTE",
    "SHIFT_FIELD_MAPPINGS",
    "SHIFT_INTERNAL_FIELDS",
    "SHIFT_TABLE",
    "TABLE_ID_NOTE",
    "column_table",
    "enum_field_names",
    "mapping_doc",
    "mapping_table",
    "unmapped_fields",
]
