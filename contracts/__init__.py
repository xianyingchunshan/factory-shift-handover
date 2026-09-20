"""T01 公共契约包（冻结候选，issue #6）。

五类契约：

- :class:`~contracts.shift.ShiftRecord` 班次
- :class:`~contracts.events.HandoverEvent` 交接事件（九类统一字段）
- :class:`~contracts.alarms.CompletenessAlarm` 完整性告警（E001–E004）
- :class:`~contracts.report.HandoverReport` 输出清单（五段结构）
- :class:`~contracts.confirmation.Confirmation` 确认回写（pending→confirmed→written_back）

另有错误码最小集（:mod:`contracts.errors`）与钉钉AI表格字段映射
（:mod:`contracts.aitable_mapping`）。

**这是候选冻结对象**：主控审查合并后才视为冻结；消费者不得把候选当已冻结依赖。
仅依赖标准库，不联网，不写外部系统。
"""

from __future__ import annotations

from .alarms import ALARM_RULE_LABELS, AlarmLedger, CompletenessAlarm
from .aitable_mapping import (
    EVENT_FIELD_MAPPINGS,
    EVENT_TABLE,
    SHIFT_FIELD_MAPPINGS,
    SHIFT_TABLE,
    FieldMapping,
    mapping_doc,
    mapping_table,
    unmapped_fields,
)
from .confirmation import (
    Confirmation,
    ConfirmationUnit,
    ReadbackEvidence,
    build_confirmation_plan,
    plan_from_units,
)
from .enums import (
    CATEGORY_COUNT,
    CATEGORY_ORDER,
    Completeness,
    ConfirmationScope,
    ConfirmationStatus,
    EventCategory,
    EventStatus,
    IdentitySource,
    Severity,
    ShiftName,
    ShiftStatus,
    WritebackStatus,
    coerce_enum,
    label_of,
    optional_enum,
)
from .errors import (
    ALARM_CODES,
    COMPLETENESS_RULES,
    ContractError,
    ContractViolation,
    ERROR_CODES,
    ERROR_CODE_LABELS,
    describe_codes,
)
from .events import EVENT_FIELD_ORDER, EventStore, HandoverEvent, IntakeResult, resolve_severity
from .identity import IdentityRef, as_identity, require_identity, require_operator
from .report import REPORT_SECTIONS, DeliveryReceipt, DispatchPlan, HandoverReport, build_report
from .shift import (
    ConfigSnapshot,
    RegisterResult,
    ShiftRecord,
    ShiftRegistry,
    new_shift,
)
from .timebase import (
    SHANGHAI,
    format_minute,
    now_shanghai,
    parse_occurred_at,
    truncate_to_minute,
)

#: 契约版本（候选冻结标识）。
CONTRACT_VERSION = "t01-candidate-1"

__all__ = [
    "ALARM_CODES",
    "ALARM_RULE_LABELS",
    "AlarmLedger",
    "CATEGORY_COUNT",
    "CATEGORY_ORDER",
    "COMPLETENESS_RULES",
    "CONTRACT_VERSION",
    "Completeness",
    "CompletenessAlarm",
    "ConfigSnapshot",
    "Confirmation",
    "ConfirmationScope",
    "ConfirmationStatus",
    "ConfirmationUnit",
    "ContractError",
    "ContractViolation",
    "DeliveryReceipt",
    "DispatchPlan",
    "ERROR_CODES",
    "ERROR_CODE_LABELS",
    "EVENT_FIELD_MAPPINGS",
    "EVENT_FIELD_ORDER",
    "EVENT_TABLE",
    "EventCategory",
    "EventStatus",
    "EventStore",
    "FieldMapping",
    "HandoverEvent",
    "HandoverReport",
    "IdentityRef",
    "IdentitySource",
    "IntakeResult",
    "REPORT_SECTIONS",
    "ReadbackEvidence",
    "RegisterResult",
    "SHANGHAI",
    "SHIFT_FIELD_MAPPINGS",
    "SHIFT_TABLE",
    "Severity",
    "ShiftName",
    "ShiftRecord",
    "ShiftRegistry",
    "ShiftStatus",
    "WritebackStatus",
    "as_identity",
    "build_confirmation_plan",
    "build_report",
    "coerce_enum",
    "describe_codes",
    "format_minute",
    "label_of",
    "mapping_doc",
    "mapping_table",
    "new_shift",
    "now_shanghai",
    "optional_enum",
    "parse_occurred_at",
    "plan_from_units",
    "require_identity",
    "require_operator",
    "resolve_severity",
    "truncate_to_minute",
    "unmapped_fields",
]
