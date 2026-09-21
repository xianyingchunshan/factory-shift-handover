"""班次表/交接事件表读写（issue #9 第 2 段）。

三张表：

- 契约表（列定义来自 :mod:`contracts.aitable_mapping`，与契约一一对应）：
  ``班次表`` 对应 :class:`~contracts.shift.ShiftRecord`，
  ``交接事件表`` 对应 :class:`~contracts.events.HandoverEvent`。
- 工作流辅助表（**非契约表**，只在工作流层使用，用于重启恢复与待办回写留痕）：
  ``输入留痕表`` 保存阶段①的原始输入行与处置结果；
  ``待办表`` 保存确认单元的钉钉待办映射与回写进度。

口径：

- 表名常量取自契约（``SHIFT_TABLE`` / ``EVENT_TABLE``），辅助表名单独定义并标注。
- 一切读写经 :class:`~integrations.aitable.adapter.AitableAdapter`，本模块**不**联网。
- 业务侧筛选：适配器只提供整表列举，按列过滤在本层做（真实实现可用视图/筛选，
  口径与结果一致）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from contracts.aitable_mapping import EVENT_TABLE, SHIFT_TABLE
from contracts.confirmation import ConfirmationScope
from contracts.enums import ShiftName, ShiftStatus, coerce_enum, is_blank, is_stay_period
from contracts.errors import ContractViolation
from contracts.events import HandoverEvent
from contracts.identity import IdentityRef
from contracts.shift import ShiftRecord, stay_period_conflict
from contracts.successor import SuccessorChange
from contracts.timebase import format_minute

from .adapter import AitableAdapter, TableRow
from .cells import (
    EVENT_COLUMNS,
    SHIFT_COLUMNS,
    bool_from_cell,
    bool_to_cell,
    event_from_fields,
    event_id_from_fields,
    event_to_fields,
    identity_from_cell,
    identity_to_cell,
    json_from_cell,
    json_to_cell,
    shift_from_fields,
    shift_to_fields,
)

#: 工作流辅助表（非契约表；命名已显式标注用途，避免与契约表混淆）。
INTAKE_JOURNAL_TABLE = "输入留痕表（工作流辅助）"
ALARM_JOURNAL_TABLE = "告警处置留痕表（工作流辅助）"
TODO_TABLE = "待办表（工作流辅助）"
SUCCESSOR_CHANGE_TABLE = "接班人变更留痕表（工作流辅助）"

JOURNAL_COLUMNS: Mapping[str, str] = {
    "event_id": "事件ID",
    "shift_id": "班次ID",
    "outcome": "处置",
    "raw": "原始行",
    "record_id": "记录ID",
    "logged_at": "留痕时间",
}

#: 输入留痕的处置取值。
OUTCOME_STORED = "stored"
OUTCOME_REJECTED = "rejected"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_WRITE_UNKNOWN = "write_unknown"
OUTCOMES: tuple[str, ...] = (
    OUTCOME_STORED,
    OUTCOME_REJECTED,
    OUTCOME_DUPLICATE,
    OUTCOME_WRITE_UNKNOWN,
)

TODO_COLUMNS: Mapping[str, str] = {
    "unit_id": "单元ID",
    "scope": "粒度",
    "shift_id": "班次ID",
    "todo_id": "待办ID",
    "assignee": "负责人",
    "label": "标签",
    "status": "确认状态",
    "confirmed_by": "确认人",
    "confirmed_at": "确认时间",
    "writeback_status": "回写状态",
    "writeback_at": "回写时间",
    "notes": "备注",
    "todo_state": "待办状态",
    "voided_at": "作废时间",
    "void_reason": "作废原因",
}

#: 待办状态（工作流辅助列，非契约列）：换人后旧待办**显式作废**，不静默删除也不静默保留。
TODO_STATE_ACTIVE = "active"
TODO_STATE_VOIDED = "voided"
TODO_STATES: tuple[str, ...] = (TODO_STATE_ACTIVE, TODO_STATE_VOIDED)

#: 接班人变更留痕表的列（工作流辅助表；契约 :class:`contracts.successor.SuccessorChange`）。
SUCCESSOR_CHANGE_COLUMNS: Mapping[str, str] = {
    "change_id": "变更ID",
    "shift_id": "班次ID",
    "ordinal": "序号",
    "from_identity": "原接班人",
    "to_identity": "新接班人",
    "changed_by": "变更人",
    "changed_at": "变更时间",
    "request_text": "答复原文",
    "resolver": "解析器",
    "reason": "说明",
    "voided_todo_ids": "作废待办",
    "issued_todo_ids": "重发待办",
    "payload": "留痕JSON",
}

ALARM_JOURNAL_COLUMNS: Mapping[str, str] = {
    "alarm_id": "告警ID",
    "shift_id": "班次ID",
    "event_id": "事件ID",
    "rule": "规则",
    "field": "字段",
    "detail": "告警说明",
    "created_at": "告警生成时间",
    "action": "处置",
    "action_at": "处置时间",
    "reason": "说明",
}

#: 告警处置动作（当前只支持“清除”；补全/复核后调用）。
ACTION_CLEARED = "cleared"
ALARM_ACTIONS: tuple[str, ...] = (ACTION_CLEARED,)


def _rows_by_column(rows: Iterable[TableRow], column: str, value: str) -> tuple[TableRow, ...]:
    return tuple(row for row in rows if row.get(column) == value)


@dataclass(frozen=True)
class JournalEntry:
    """一条输入留痕（阶段①原始行 + 处置结果）。"""

    event_id: str
    shift_id: str
    outcome: str
    raw: Mapping[str, str]
    record_id: str = ""
    logged_at: str = ""

    def to_fields(self) -> dict[str, str]:
        return {
            JOURNAL_COLUMNS["event_id"]: self.event_id,
            JOURNAL_COLUMNS["shift_id"]: self.shift_id,
            JOURNAL_COLUMNS["outcome"]: self.outcome,
            JOURNAL_COLUMNS["raw"]: json.dumps(
                {str(k): "" if v is None else str(v) for k, v in self.raw.items()},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            JOURNAL_COLUMNS["record_id"]: self.record_id,
            JOURNAL_COLUMNS["logged_at"]: self.logged_at,
        }

    @classmethod
    def from_fields(cls, fields: Mapping[str, Any]) -> "JournalEntry":
        raw_text = str(fields.get(JOURNAL_COLUMNS["raw"]) or "").strip()
        try:
            raw = json.loads(raw_text) if raw_text else {}
        except json.JSONDecodeError as exc:
            raise ContractViolation(f"输入留痕的原始行不是合法 JSON: {exc}") from exc
        outcome = str(fields.get(JOURNAL_COLUMNS["outcome"]) or "").strip()
        if outcome not in OUTCOMES:
            raise ContractViolation(
                f"输入留痕的处置取值非法: {outcome!r}（可用: {', '.join(OUTCOMES)}）"
            )
        return cls(
            event_id=str(fields.get(JOURNAL_COLUMNS["event_id"]) or "").strip(),
            shift_id=str(fields.get(JOURNAL_COLUMNS["shift_id"]) or "").strip(),
            outcome=outcome,
            raw={str(k): str(v) for k, v in raw.items()},
            record_id=str(fields.get(JOURNAL_COLUMNS["record_id"]) or "").strip(),
            logged_at=str(fields.get(JOURNAL_COLUMNS["logged_at"]) or "").strip(),
        )


class ShiftTable:
    """班次表读写：新增/更新/按 ID 取/按幂等键查。"""

    def __init__(self, adapter: AitableAdapter) -> None:
        self.adapter = adapter

    def upsert(self, shift: ShiftRecord) -> str:
        """按班次ID落一行；已存在则更新（不产生第二行）。"""
        record_id = self.record_id_of(shift.shift_id)
        fields = shift_to_fields(shift)
        if record_id is None:
            return self.adapter.create_record(SHIFT_TABLE, fields)
        return self.adapter.update_record(SHIFT_TABLE, record_id, fields)

    def record_id_of(self, shift_id: str) -> str | None:
        column = SHIFT_COLUMNS["shift_id"]
        rows = _rows_by_column(self.adapter.list_records(SHIFT_TABLE), column, str(shift_id))
        return rows[0].record_id if rows else None

    def get(self, shift_id: str) -> ShiftRecord | None:
        record_id = self.record_id_of(shift_id)
        if record_id is None:
            return None
        row = self.adapter.get_record(SHIFT_TABLE, record_id)
        return None if row is None else shift_from_fields(row.fields)

    def find(
        self,
        handover_line: str,
        shift_date: Any,
        shift_name: str,
        *,
        incoming: ShiftRecord | None = None,
    ) -> ShiftRecord | None:
        """按幂等键（交接线 + 日期 + 班次）查已有班次；用于同班同线防重。

        T07 匹配口径（**只对驻场期生效**）：``incoming`` 是驻场期单时，除幂等键外
        再按**驻场期窗口重叠**匹配——同交接线、同班次名、窗口有交集且非同一
        ``shift_id``（端点相接不算重叠，见 :func:`contracts.shift.stay_period_conflict`）。
        既有四类班次（早/中/晚/自定义）与未传 ``incoming`` 的调用口径不变。
        """
        date_text = shift_date.isoformat() if hasattr(shift_date, "isoformat") else str(shift_date)
        name_value = coerce_enum(ShiftName, shift_name, field="班次").value
        rows = self.adapter.list_records(SHIFT_TABLE)
        for row in rows:
            if (
                row.get(SHIFT_COLUMNS["handover_line"]) == str(handover_line).strip()
                and row.get(SHIFT_COLUMNS["shift_date"]) == date_text
                and row.get(SHIFT_COLUMNS["shift_name"]) == name_value
            ):
                return shift_from_fields(row.fields)
        if incoming is None or not incoming.is_stay_period:
            return None
        # 只解析"同交接线的驻场期行"（按原始列先筛，既有四类班次的行永不进入本路径）；
        # 行解析失败按数据脏处理：抛出而不静默跳过（fail-closed，不放行重复建单）。
        line_text = str(handover_line).strip()
        candidates = [
            shift_from_fields(row.fields)
            for row in rows
            if row.get(SHIFT_COLUMNS["handover_line"]) == line_text
            and is_stay_period(row.get(SHIFT_COLUMNS["shift_name"]))
        ]
        return stay_period_conflict(incoming, candidates)

    def set_status(self, shift_id: str, status: str) -> str:
        """更新班次状态列（原始字符串）。"""
        record_id = self.record_id_of(shift_id)
        if record_id is None:
            raise ContractViolation(f"班次表中没有班次 {shift_id!r}，无法更新状态")
        value = coerce_enum(ShiftStatus, status, field="状态").value
        return self.adapter.update_record(
            SHIFT_TABLE, record_id, {SHIFT_COLUMNS["status"]: value}
        )

    def list_all(self) -> tuple[ShiftRecord, ...]:
        return tuple(shift_from_fields(row.fields) for row in self.adapter.list_records(SHIFT_TABLE))

    def count(self) -> int:
        return self.adapter.row_count(SHIFT_TABLE)


class EventTable:
    """交接事件表读写：追加/更新/按 ID 取/按班次筛选。"""

    def __init__(self, adapter: AitableAdapter) -> None:
        self.adapter = adapter

    def append(self, event: HandoverEvent, *, occurred_at_raw: str | None = None) -> str:
        """追加一行；同一事件ID已存在 → 拒绝（不双写）。"""
        if self.find_row(event.event_id) is not None:
            raise ContractViolation(f"交接事件表已有事件 {event.event_id}，不重复落表")
        return self.adapter.create_record(
            EVENT_TABLE, event_to_fields(event, occurred_at_raw=occurred_at_raw)
        )

    def update(self, event: HandoverEvent, *, occurred_at_raw: str | None = None) -> str:
        record_id = self.record_id_of(event.event_id)
        if record_id is None:
            raise ContractViolation(f"交接事件表中没有事件 {event.event_id}，无法更新")
        return self.adapter.update_record(
            EVENT_TABLE, record_id, event_to_fields(event, occurred_at_raw=occurred_at_raw)
        )

    def record_id_of(self, event_id: str) -> str | None:
        row = self.find_row(event_id)
        return None if row is None else row.record_id

    def find_row(self, event_id: str) -> TableRow | None:
        column = EVENT_COLUMNS["event_id"]
        rows = _rows_by_column(self.adapter.list_records(EVENT_TABLE), column, str(event_id))
        return rows[0] if rows else None

    def get(self, event_id: str) -> HandoverEvent | None:
        row = self.find_row(event_id)
        return None if row is None else event_from_fields(row.fields)

    def list_rows(self, shift_id: str | None = None) -> tuple[TableRow, ...]:
        rows = self.adapter.list_records(EVENT_TABLE)
        if shift_id is None:
            return rows
        return _rows_by_column(rows, EVENT_COLUMNS["shift_id"], str(shift_id))

    def list_for_shift(self, shift_id: str) -> tuple[HandoverEvent, ...]:
        return tuple(event_from_fields(row.fields) for row in self.list_rows(shift_id))

    def event_ids(self, shift_id: str | None = None) -> tuple[str, ...]:
        return tuple(event_id_from_fields(row.fields) for row in self.list_rows(shift_id))

    def count(self, shift_id: str | None = None) -> int:
        return len(self.list_rows(shift_id))


class IntakeJournal:
    """输入留痕表（辅助表）：阶段①原始行 + 处置结果，供重启恢复与拒收留痕。"""

    def __init__(self, adapter: AitableAdapter) -> None:
        self.adapter = adapter

    def record(self, entry: JournalEntry) -> str:
        return self.adapter.create_record(INTAKE_JOURNAL_TABLE, entry.to_fields())

    def entries(self, shift_id: str | None = None) -> tuple[JournalEntry, ...]:
        rows = self.adapter.list_records(INTAKE_JOURNAL_TABLE)
        if shift_id is not None:
            rows = _rows_by_column(rows, JOURNAL_COLUMNS["shift_id"], str(shift_id))
        return tuple(JournalEntry.from_fields(row.fields) for row in rows)

    def rejected_entries(self, shift_id: str | None = None) -> tuple[JournalEntry, ...]:
        return tuple(
            entry for entry in self.entries(shift_id) if entry.outcome == OUTCOME_REJECTED
        )

    def count(self) -> int:
        return self.adapter.row_count(INTAKE_JOURNAL_TABLE)


@dataclass(frozen=True)
class AlarmJournalEntry:
    """一条告警处置留痕（清除动作），用于重启后恢复“已清告警”状态与告警账本。"""

    alarm_id: str
    shift_id: str
    rule: str
    field: str
    action: str = ACTION_CLEARED
    event_id: str = ""
    detail: str = ""
    created_at: str = ""
    action_at: str = ""
    reason: str = ""

    def to_fields(self) -> dict[str, str]:
        return {
            ALARM_JOURNAL_COLUMNS["alarm_id"]: self.alarm_id,
            ALARM_JOURNAL_COLUMNS["shift_id"]: self.shift_id,
            ALARM_JOURNAL_COLUMNS["event_id"]: self.event_id,
            ALARM_JOURNAL_COLUMNS["rule"]: self.rule,
            ALARM_JOURNAL_COLUMNS["field"]: self.field,
            ALARM_JOURNAL_COLUMNS["detail"]: self.detail,
            ALARM_JOURNAL_COLUMNS["created_at"]: self.created_at,
            ALARM_JOURNAL_COLUMNS["action"]: self.action,
            ALARM_JOURNAL_COLUMNS["action_at"]: self.action_at,
            ALARM_JOURNAL_COLUMNS["reason"]: self.reason,
        }

    @classmethod
    def from_fields(cls, fields: Mapping[str, Any]) -> "AlarmJournalEntry":
        action = str(fields.get(ALARM_JOURNAL_COLUMNS["action"]) or "").strip()
        if action not in ALARM_ACTIONS:
            raise ContractViolation(
                f"告警处置动作非法: {action!r}（可用: {', '.join(ALARM_ACTIONS)}）"
            )
        alarm_id = str(fields.get(ALARM_JOURNAL_COLUMNS["alarm_id"]) or "").strip()
        if not alarm_id:
            raise ContractViolation("告警处置留痕缺少 告警ID")
        return cls(
            alarm_id=alarm_id,
            shift_id=str(fields.get(ALARM_JOURNAL_COLUMNS["shift_id"]) or "").strip(),
            rule=str(fields.get(ALARM_JOURNAL_COLUMNS["rule"]) or "").strip(),
            field=str(fields.get(ALARM_JOURNAL_COLUMNS["field"]) or "").strip(),
            action=action,
            event_id=str(fields.get(ALARM_JOURNAL_COLUMNS["event_id"]) or "").strip(),
            detail=str(fields.get(ALARM_JOURNAL_COLUMNS["detail"]) or "").strip(),
            created_at=str(fields.get(ALARM_JOURNAL_COLUMNS["created_at"]) or "").strip(),
            action_at=str(fields.get(ALARM_JOURNAL_COLUMNS["action_at"]) or "").strip(),
            reason=str(fields.get(ALARM_JOURNAL_COLUMNS["reason"]) or "").strip(),
        )


class AlarmJournal:
    """告警处置留痕表（辅助表）：清告警动作落表，重启后可恢复已清状态。"""

    def __init__(self, adapter: AitableAdapter) -> None:
        self.adapter = adapter

    def record(self, entry: AlarmJournalEntry) -> str:
        return self.adapter.create_record(ALARM_JOURNAL_TABLE, entry.to_fields())

    def entries(self, shift_id: str | None = None) -> tuple[AlarmJournalEntry, ...]:
        rows = self.adapter.list_records(ALARM_JOURNAL_TABLE)
        if shift_id is not None:
            rows = _rows_by_column(rows, ALARM_JOURNAL_COLUMNS["shift_id"], str(shift_id))
        return tuple(AlarmJournalEntry.from_fields(row.fields) for row in rows)

    def cleared_ids(self, shift_id: str | None = None) -> tuple[str, ...]:
        return tuple(entry.alarm_id for entry in self.entries(shift_id))

    def count(self) -> int:
        return self.adapter.row_count(ALARM_JOURNAL_TABLE)


class TodoTable:
    """待办表（辅助表）：确认单元 ↔ 钉钉待办映射 + 回写进度。"""

    def __init__(self, adapter: AitableAdapter) -> None:
        self.adapter = adapter

    def create(
        self, *, unit_id: str, scope: str, shift_id: str, assignee: IdentityRef, label: str = ""
    ) -> str:
        """创建待办：先落行（待办ID 空），再用记录ID 回填待办ID（模拟钉钉返回 ID 后回填）。

        同一确认单元只允许一条待办：重复创建即拒绝（防重，不产生第二个待办）。
        """
        if is_blank(unit_id):
            raise ContractViolation("确认单元 unit_id 不能为空")
        if self.find_row(unit_id) is not None:
            raise ContractViolation(f"确认单元 {unit_id} 已有待办，不重复创建")
        scope_value = coerce_enum(ConfirmationScope, scope, field="scope").value
        fields = {
            TODO_COLUMNS["unit_id"]: str(unit_id).strip(),
            TODO_COLUMNS["scope"]: scope_value,
            TODO_COLUMNS["shift_id"]: str(shift_id).strip(),
            TODO_COLUMNS["todo_id"]: "",
            TODO_COLUMNS["assignee"]: identity_to_cell(assignee),
            TODO_COLUMNS["label"]: str(label or "").strip(),
            TODO_COLUMNS["status"]: "pending",
            TODO_COLUMNS["confirmed_by"]: "",
            TODO_COLUMNS["confirmed_at"]: "",
            TODO_COLUMNS["writeback_status"]: "not_sent",
            TODO_COLUMNS["writeback_at"]: "",
            TODO_COLUMNS["notes"]: "[]",
            TODO_COLUMNS["todo_state"]: TODO_STATE_ACTIVE,
            TODO_COLUMNS["voided_at"]: "",
            TODO_COLUMNS["void_reason"]: "",
        }
        record_id = self.adapter.create_record(TODO_TABLE, fields)
        self.adapter.update_record(TODO_TABLE, record_id, {TODO_COLUMNS["todo_id"]: record_id})
        return record_id

    def find_row(self, unit_id: str) -> TableRow | None:
        rows = _rows_by_column(
            self.adapter.list_records(TODO_TABLE), TODO_COLUMNS["unit_id"], str(unit_id)
        )
        return rows[0] if rows else None

    def get_fields(self, unit_id: str) -> dict[str, str] | None:
        row = self.find_row(unit_id)
        return None if row is None else dict(row.fields)

    def update_progress(self, unit_id: str, fields: Mapping[str, Any]) -> str:
        row = self.find_row(unit_id)
        if row is None:
            raise ContractViolation(f"待办表中没有确认单元 {unit_id!r}")
        payload = {column: ("" if value is None else str(value)) for column, value in fields.items()}
        return self.adapter.update_record(TODO_TABLE, row.record_id, payload)

    def rows(self, shift_id: str | None = None) -> tuple[TableRow, ...]:
        rows = self.adapter.list_records(TODO_TABLE)
        if shift_id is not None:
            rows = _rows_by_column(rows, TODO_COLUMNS["shift_id"], str(shift_id))
        return rows

    def assignee_of(self, unit_id: str) -> IdentityRef | None:
        fields = self.get_fields(unit_id)
        if fields is None:
            return None
        return identity_from_cell(fields.get(TODO_COLUMNS["assignee"]))

    def has_todo(self, unit_id: str) -> bool:
        fields = self.get_fields(unit_id)
        if fields is None:
            return False
        return bool(str(fields.get(TODO_COLUMNS["todo_id"]) or "").strip())

    # ---- 作废（换人路由跟随的显式动作） ---------------------------------

    def state_of(self, unit_id: str) -> str:
        """待办状态；未写该列的历史行按 ``active`` 处理。"""
        fields = self.get_fields(unit_id)
        if fields is None:
            return ""
        value = str(fields.get(TODO_COLUMNS["todo_state"]) or "").strip()
        return value or TODO_STATE_ACTIVE

    def void(self, unit_id: str, *, reason: str, at: str = "") -> dict[str, str]:
        """**显式作废**一条待办：写 ``待办状态=voided`` + 作废时间/原因，行不删除。

        旧待办不得静默残留：作废后仍可回读"作废过哪一条、为什么、什么时候"。
        """
        row = self.find_row(unit_id)
        if row is None:
            raise ContractViolation(f"待办表中没有确认单元 {unit_id!r}，无法作废")
        if self.state_of(unit_id) == TODO_STATE_VOIDED:
            raise ContractViolation(f"确认单元 {unit_id} 的待办已作废，不重复作废")
        if is_blank(reason):
            raise ContractViolation("待办作废必须写明原因（不静默作废）")
        self.update_progress(
            unit_id,
            {
                TODO_COLUMNS["todo_state"]: TODO_STATE_VOIDED,
                TODO_COLUMNS["voided_at"]: str(at or ""),
                TODO_COLUMNS["void_reason"]: str(reason).strip(),
            },
        )
        fields = self.get_fields(unit_id)
        return {} if fields is None else fields

    def active_rows(self, shift_id: str | None = None) -> tuple[TableRow, ...]:
        """未作废的待办行（作废行保留留痕，不参与后续流转）。"""
        return tuple(
            row
            for row in self.rows(shift_id)
            if str(row.get(TODO_COLUMNS["todo_state"]) or "").strip() != TODO_STATE_VOIDED
        )

    def count(self, shift_id: str | None = None) -> int:
        return len(self.rows(shift_id))


class SuccessorChangeTable:
    """接班人变更留痕表（辅助表）：变更（谁/何时/从谁改到谁）落表并可回读。

    权威内容在 ``留痕JSON`` 列（契约 :meth:`contracts.successor.SuccessorChange.to_dict`
    的原文），关键列另存一份便于人工核对与筛选。
    """

    def __init__(self, adapter: AitableAdapter) -> None:
        self.adapter = adapter

    def record(self, change: SuccessorChange) -> str:
        """新增一条变更留痕；同一变更ID 已存在 → 拒绝（不双写）。"""
        if self.find_row(change.change_id) is not None:
            raise ContractViolation(f"接班人变更留痕表已有 {change.change_id}，不重复落表")
        return self.adapter.create_record(SUCCESSOR_CHANGE_TABLE, _change_fields(change))

    def amend(self, change: SuccessorChange) -> str:
        """就地更新一条留痕（路由跟随证据回填：作废/重发待办 ID）。"""
        row = self.find_row(change.change_id)
        if row is None:
            raise ContractViolation(f"接班人变更留痕表中没有 {change.change_id}，无法更新")
        return self.adapter.update_record(
            SUCCESSOR_CHANGE_TABLE, row.record_id, _change_fields(change)
        )

    def find_row(self, change_id: str) -> TableRow | None:
        rows = _rows_by_column(
            self.adapter.list_records(SUCCESSOR_CHANGE_TABLE),
            SUCCESSOR_CHANGE_COLUMNS["change_id"],
            str(change_id),
        )
        return rows[0] if rows else None

    def rows(self, shift_id: str | None = None) -> tuple[TableRow, ...]:
        rows = self.adapter.list_records(SUCCESSOR_CHANGE_TABLE)
        if shift_id is None:
            return rows
        return _rows_by_column(rows, SUCCESSOR_CHANGE_COLUMNS["shift_id"], str(shift_id))

    def entries(self, shift_id: str | None = None) -> tuple[SuccessorChange, ...]:
        """读回变更留痕（按序号升序）。"""
        changes = [
            SuccessorChange.from_dict(
                json_from_cell(row.get(SUCCESSOR_CHANGE_COLUMNS["payload"]), field="接班人变更留痕")
            )
            for row in self.rows(shift_id)
        ]
        return tuple(sorted(changes, key=lambda item: item.ordinal))

    def get(self, change_id: str) -> SuccessorChange | None:
        row = self.find_row(change_id)
        if row is None:
            return None
        return SuccessorChange.from_dict(
            json_from_cell(row.get(SUCCESSOR_CHANGE_COLUMNS["payload"]), field="接班人变更留痕")
        )

    def count(self, shift_id: str | None = None) -> int:
        return len(self.rows(shift_id))


def _joined(ids: tuple[str, ...]) -> str:
    return ", ".join(ids)


def _change_fields(change: SuccessorChange) -> dict[str, str]:
    """变更留痕 → 表格行（原始字符串；权威内容为 JSON 列）。"""
    return {
        SUCCESSOR_CHANGE_COLUMNS["change_id"]: change.change_id,
        SUCCESSOR_CHANGE_COLUMNS["shift_id"]: change.shift_id,
        SUCCESSOR_CHANGE_COLUMNS["ordinal"]: str(change.ordinal),
        SUCCESSOR_CHANGE_COLUMNS["from_identity"]: identity_to_cell(change.from_identity),
        SUCCESSOR_CHANGE_COLUMNS["to_identity"]: identity_to_cell(change.to_identity),
        SUCCESSOR_CHANGE_COLUMNS["changed_by"]: identity_to_cell(change.changed_by),
        SUCCESSOR_CHANGE_COLUMNS["changed_at"]: format_minute(change.changed_at),
        SUCCESSOR_CHANGE_COLUMNS["request_text"]: change.request_text,
        SUCCESSOR_CHANGE_COLUMNS["resolver"]: change.resolver,
        SUCCESSOR_CHANGE_COLUMNS["reason"]: change.reason,
        SUCCESSOR_CHANGE_COLUMNS["voided_todo_ids"]: _joined(change.voided_todo_ids),
        SUCCESSOR_CHANGE_COLUMNS["issued_todo_ids"]: _joined(change.issued_todo_ids),
        SUCCESSOR_CHANGE_COLUMNS["payload"]: json_to_cell(change.to_dict()),
    }


def todo_columns_doc() -> dict[str, Any]:
    """辅助表列说明（给审查者；不含任何真实表 ID）。"""
    return {
        "todo_table": TODO_TABLE,
        "todo_columns": dict(TODO_COLUMNS),
        "todo_states": list(TODO_STATES),
        "journal_table": INTAKE_JOURNAL_TABLE,
        "journal_columns": dict(JOURNAL_COLUMNS),
        "alarm_journal_table": ALARM_JOURNAL_TABLE,
        "alarm_journal_columns": dict(ALARM_JOURNAL_COLUMNS),
        "successor_change_table": SUCCESSOR_CHANGE_TABLE,
        "successor_change_columns": dict(SUCCESSOR_CHANGE_COLUMNS),
        "note": "辅助表非契约表：只由工作流层使用，用于重启恢复与回写留痕；"
        "是否升格为契约表由主控确认",
    }


__all__ = [
    "ACTION_CLEARED",
    "ALARM_ACTIONS",
    "ALARM_JOURNAL_COLUMNS",
    "ALARM_JOURNAL_TABLE",
    "EVENT_TABLE",
    "INTAKE_JOURNAL_TABLE",
    "JOURNAL_COLUMNS",
    "OUTCOMES",
    "OUTCOME_DUPLICATE",
    "OUTCOME_REJECTED",
    "OUTCOME_STORED",
    "OUTCOME_WRITE_UNKNOWN",
    "SHIFT_TABLE",
    "SUCCESSOR_CHANGE_COLUMNS",
    "SUCCESSOR_CHANGE_TABLE",
    "TODO_COLUMNS",
    "TODO_STATES",
    "TODO_STATE_ACTIVE",
    "TODO_STATE_VOIDED",
    "TODO_TABLE",
    "AlarmJournal",
    "AlarmJournalEntry",
    "EventTable",
    "IntakeJournal",
    "JournalEntry",
    "ShiftTable",
    "SuccessorChangeTable",
    "TodoTable",
    "todo_columns_doc",
]
