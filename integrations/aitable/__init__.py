"""钉钉AI表格读写的**合成**实现（issue #9 第 2 段）。

本包只提供：

- 抽象口 :class:`~integrations.aitable.adapter.AitableAdapter`（真实实现由主控在
  隔离环境提供，仓库内不落任何真实表 ID/凭据）；
- 合成适配器 :class:`~integrations.aitable.synthetic.SyntheticAitableAdapter`
  （内存、离线、可注入故障、可搬运状态）；
- 契约行编解码 :mod:`integrations.aitable.cells` 与三张表的读写
  :mod:`integrations.aitable.tables`；
- 离线审计 :mod:`integrations.aitable.isolation`。

测试与脚本一律接合成适配器；真实表格读写属 L4，不在本卡范围。
"""

from __future__ import annotations

from .adapter import (
    SYNTH_PREFIX,
    AitableAdapter,
    AitableError,
    AitableRejected,
    AitableWriteUnknown,
    TableRow,
)
from .cells import (
    EVENT_COLUMNS,
    SHIFT_COLUMNS,
    bool_from_cell,
    bool_to_cell,
    event_from_fields,
    event_to_fields,
    identity_from_cell,
    identity_to_cell,
    shift_from_fields,
    shift_to_fields,
)
from .isolation import AUDITED_PACKAGES, audit_offline, find_forbidden_imports
from .synthetic import (
    CREATE,
    FAIL_LOSE,
    FAIL_REJECTED,
    FAIL_UNKNOWN,
    GET,
    LIST,
    UPDATE,
    Call,
    SyntheticAitableAdapter,
    assert_synthetic,
)
from .tables import (
    ACTION_CLEARED,
    ALARM_JOURNAL_TABLE,
    EVENT_TABLE,
    INTAKE_JOURNAL_TABLE,
    OUTCOME_DUPLICATE,
    OUTCOME_REJECTED,
    OUTCOME_STORED,
    OUTCOME_WRITE_UNKNOWN,
    SHIFT_TABLE,
    TODO_TABLE,
    AlarmJournal,
    AlarmJournalEntry,
    EventTable,
    IntakeJournal,
    JournalEntry,
    ShiftTable,
    TodoTable,
    todo_columns_doc,
)

__all__ = [
    "ACTION_CLEARED",
    "ALARM_JOURNAL_TABLE",
    "AUDITED_PACKAGES",
    "AitableAdapter",
    "AitableError",
    "AitableRejected",
    "AitableWriteUnknown",
    "AlarmJournal",
    "AlarmJournalEntry",
    "CREATE",
    "Call",
    "EVENT_COLUMNS",
    "EVENT_TABLE",
    "EventTable",
    "FAIL_LOSE",
    "FAIL_REJECTED",
    "FAIL_UNKNOWN",
    "GET",
    "INTAKE_JOURNAL_TABLE",
    "IntakeJournal",
    "JournalEntry",
    "LIST",
    "OUTCOME_DUPLICATE",
    "OUTCOME_REJECTED",
    "OUTCOME_STORED",
    "OUTCOME_WRITE_UNKNOWN",
    "SHIFT_COLUMNS",
    "SHIFT_TABLE",
    "SYNTH_PREFIX",
    "ShiftTable",
    "SyntheticAitableAdapter",
    "TODO_TABLE",
    "TableRow",
    "TodoTable",
    "UPDATE",
    "assert_synthetic",
    "audit_offline",
    "bool_from_cell",
    "bool_to_cell",
    "event_from_fields",
    "event_to_fields",
    "find_forbidden_imports",
    "identity_from_cell",
    "identity_to_cell",
    "shift_from_fields",
    "shift_to_fields",
    "todo_columns_doc",
]
