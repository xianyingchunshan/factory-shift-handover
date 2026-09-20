"""阶段③后半：确认待办与回写状态机（issue #9 第 3 段）。

粒度（SPEC §4）：关键事项**逐项**发待办（``scope=event``）；一般事务**整单**发待办
（``scope=shift``）。计划由契约 :func:`contracts.confirmation.build_confirmation_plan`
生成，本层只负责：

- 把确认单元落成钉钉待办（经**合成适配器**，离线）；
- 承载回写状态机 :class:`contracts.confirmation.Confirmation`
  （``pending → confirmed → written_back``）；
- 回读：写入不等于完成，回读才算——无回读不得标 ``confirmed``（``WRITE_UNKNOWN``）；
- 错人 / 缺身份不放行（``AUTH_REQUIRED``，走 :func:`contracts.identity.require_operator`）；
- 受理不明（``unknown``）**只回查不重放**（``WRITE_UNKNOWN``，唯一出路
  :meth:`TodoAssignmentService.recheck_unit`）；
- 未确认先回写 → ``NOT_CONFIRMED``。

落库：待办映射与回写进度写入工作流辅助表 ``待办表``，供重启恢复。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from contracts.confirmation import (
    Confirmation,
    ConfirmationScope,
    ConfirmationUnit,
    ReadbackEvidence,
    build_confirmation_plan,
)
from contracts.enums import ConfirmationStatus, WritebackStatus, coerce_enum, is_blank
from contracts.errors import AUTH_REQUIRED, NOT_CONFIRMED, WRITE_UNKNOWN, ContractError, ContractViolation
from contracts.identity import IdentityRef, as_identity, require_operator
from contracts.report import HandoverReport
from contracts.shift import ShiftRecord
from contracts.timebase import format_minute, now_shanghai

from integrations.aitable.adapter import AitableAdapter
from integrations.aitable.synthetic import assert_synthetic
from integrations.aitable.tables import TODO_COLUMNS, TodoTable
from integrations.aitable.cells import identity_from_cell, identity_to_cell

#: 回读来源：本层只读 AI 表格（待办表）与待办。
READBACK_SOURCE = "aitable"


@dataclass(frozen=True)
class WritebackOutcome:
    """一次回写动作的结果：状态 + 是否必须回查。"""

    unit_id: str
    status: str
    writeback_status: str
    needs_recheck: bool
    readback_applied: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "status": self.status,
            "writeback_status": self.writeback_status,
            "needs_recheck": self.needs_recheck,
            "readback_applied": self.readback_applied,
        }


class TodoAssignmentService:
    """确认单元 → 待办 → 确认 → 回写（全部落合成适配器）。"""

    def __init__(
        self,
        adapter: AitableAdapter,
        shift: ShiftRecord,
        *,
        clock: Callable[[], datetime] | None = None,
        require_offline: bool = True,
    ) -> None:
        if require_offline:
            assert_synthetic(adapter)
        self.adapter = adapter
        self.shift = shift
        self.clock = clock or now_shanghai
        self.board = TodoTable(adapter)
        self._confirmations: dict[str, Confirmation] = {}

    # ---- 计划与待办 -----------------------------------------------------

    def plan(self, report: HandoverReport) -> tuple[ConfirmationUnit, ...]:
        """确保清单确认区每个单元都带待办；**已建待办不重复创建**。

        确认区为空时走契约 :func:`contracts.confirmation.build_confirmation_plan`
        （关键级逐项 / 一般事务整单，粒度由契约决定）。
        """
        existing = tuple(report.confirmation_area)
        if existing:
            units = tuple(
                unit if unit.has_todo else unit.attach_todo(self.todo_id_factory(unit))
                for unit in existing
            )
        else:
            units = build_confirmation_plan(
                self.shift, report, todo_id_factory=self.todo_id_factory
            )
        report.confirmation_area = units
        return units

    def todo_id_factory(self, unit: ConfirmationUnit) -> str:
        """待办创建口：经合成适配器落一行待办表并返回待办 ID。"""
        return self.board.create(
            unit_id=unit.unit_id,
            scope=unit.scope,
            shift_id=unit.shift_id,
            assignee=unit.assignee,
            label=unit.label,
        )

    def attach(self, report: HandoverReport) -> tuple[ConfirmationUnit, ...]:
        """为清单确认区登记待确认记录（**幂等**：已登记的单元不重复登记）。"""
        units = tuple(report.confirmation_area)
        for unit in units:
            if not unit.has_todo:
                raise ContractViolation(f"确认单元 {unit.unit_id} 未创建待办，不得登记回写")
            if unit.unit_id in self._confirmations:
                continue
            record = Confirmation.for_unit(unit)
            self._confirmations[record.unit_id] = record
            self._persist(record)
        report.confirmation_area = units
        return units

    def create_plan(self, report: HandoverReport) -> tuple[ConfirmationUnit, ...]:
        """一步到位：生成待办 + 登记待确认（清单确认区随之带上 todo_id）。"""
        self.plan(report)
        return self.attach(report)

    # ---- 查询 -----------------------------------------------------------

    def get(self, unit_id: str) -> Confirmation:
        record = self._confirmations.get(unit_id)
        if record is None:
            raise ContractViolation(f"没有确认单元 {unit_id!r}（先创建待办）")
        return record

    @property
    def confirmations(self) -> tuple[Confirmation, ...]:
        return tuple(self._confirmations.values())

    def pending_units(self) -> tuple[Confirmation, ...]:
        return tuple(record for record in self.confirmations if record.is_pending)

    def needs_recheck(self) -> tuple[Confirmation, ...]:
        return tuple(record for record in self.confirmations if record.needs_recheck)

    def summary(self) -> dict[str, Any]:
        records = self.confirmations
        return {
            "units": len(records),
            "pending": sum(1 for record in records if record.is_pending),
            "confirmed": sum(1 for record in records if record.is_confirmed),
            "written_back": sum(1 for record in records if record.is_written_back),
            "needs_recheck": sum(1 for record in records if record.needs_recheck),
            "todos": self.board.count(self.shift.shift_id),
        }

    def board_fields(self, unit_id: str) -> dict[str, str]:
        fields = self.board.get_fields(unit_id)
        if fields is None:
            raise ContractViolation(f"待办表中没有确认单元 {unit_id!r}")
        return fields

    # ---- 回读 -----------------------------------------------------------

    def readback(
        self,
        unit_id: str,
        *,
        at: datetime | None = None,
        applied: bool | None = None,
    ) -> ReadbackEvidence:
        """回读待办表：待办确实存在才算“已生效”，否则不得标记完成。"""
        fields = self.board.get_fields(unit_id)
        todo_id = "" if fields is None else str(fields.get(TODO_COLUMNS["todo_id"]) or "").strip()
        flag = bool(todo_id) if applied is None else bool(applied)
        return ReadbackEvidence(
            source=READBACK_SOURCE,
            observed_at=at or self.clock(),
            applied=flag,
            payload={"unit_id": unit_id, "todo_id": todo_id},
        )

    # ---- 确认 -----------------------------------------------------------

    def confirm(
        self,
        unit_id: str,
        operator: Any,
        *,
        at: datetime | None = None,
        readback: ReadbackEvidence | None = None,
        note: str = "",
        allow_missing_readback: bool = False,
    ) -> Confirmation:
        """登记确认：必须有回读证据与正确操作者（缺 → ``WRITE_UNKNOWN``/``AUTH_REQUIRED``）。

        ``allow_missing_readback=True`` 仅供测试“无回读不放行”的守卫路径使用。
        """
        record = self.get(unit_id)
        evidence = readback
        if evidence is None and not allow_missing_readback:
            evidence = self.readback(unit_id, at=at)
        record.mark_confirmed(operator, at=at or self.clock(), readback=evidence, note=note)
        self._persist(record)
        return record

    def require_operator(self, unit_id: str, operator: Any) -> IdentityRef:
        """独立身份核验入口：缺身份 / 错人 → ``AUTH_REQUIRED``。"""
        record = self.get(unit_id)
        return require_operator(record.assignee, operator, field="confirmed_by")

    # ---- 回写 -----------------------------------------------------------

    def writeback(
        self,
        unit_id: str,
        result: str,
        *,
        readback: ReadbackEvidence | None = None,
        at: datetime | None = None,
    ) -> WritebackOutcome:
        """登记回写结果；``verified`` 必须带回读（无回读不得完成）。"""
        record = self.get(unit_id)
        target = coerce_enum(WritebackStatus, result, field="writeback_status")
        evidence = readback
        if target == WritebackStatus.VERIFIED and evidence is None:
            evidence = self.readback(unit_id, at=at)
        record.record_writeback(target.value, readback=evidence, at=at or self.clock())
        self._persist(record)
        return WritebackOutcome(
            unit_id=unit_id,
            status=record.status,
            writeback_status=record.writeback_status,
            needs_recheck=record.needs_recheck,
            readback_applied=None if evidence is None else bool(evidence.applied),
        )

    def run_writeback(
        self,
        unit_id: str,
        *,
        at: datetime | None = None,
        simulate: str = WritebackStatus.VERIFIED,
        write_to_board: bool = True,
    ) -> WritebackOutcome:
        """执行一次"把确认结果回写 AI 表格状态"：写入 → 回读 → 登记结果。

        - ``simulate=verified``：写待办表并回读确认已生效 → ``written_back``；
        - ``simulate=unknown``：受理不明 → 记 ``unknown``，**只回查不重放**。
        """
        record = self.get(unit_id)
        record.guard_resend()  # 未确认 / 已回写 / 上次受理不明 → 这里就拒绝
        mode = coerce_enum(WritebackStatus, simulate, field="simulate")
        if mode == WritebackStatus.UNKNOWN:
            record.add_note("回写受理不明：只回查，不重放")
            outcome = self.writeback(unit_id, WritebackStatus.UNKNOWN.value, at=at)
            return outcome
        if write_to_board:
            self.board.update_progress(
                unit_id,
                {
                    TODO_COLUMNS["status"]: ConfirmationStatus.CONFIRMED.value,
                    TODO_COLUMNS["writeback_status"]: mode.value,
                },
            )
        evidence = self.readback(unit_id, at=at)
        return self.writeback(unit_id, mode.value, readback=evidence, at=at)

    def guard_resend(self, unit_id: str) -> None:
        """重放闸门：受理不明只回查；未确认不得写入；已回写不得重复。"""
        self.get(unit_id).guard_resend()

    def recheck_unit(
        self, unit_id: str, *, at: datetime | None = None, applied: bool | None = None,
        readback: ReadbackEvidence | None = None,
    ) -> WritebackOutcome:
        """回查（``unknown`` 的唯一出路）：按回读结果落 ``written_back`` / ``not_applied``。"""
        record = self.get(unit_id)
        evidence = readback if readback is not None else self.readback(unit_id, at=at, applied=applied)
        record.recheck(evidence, at=at or self.clock())
        self._persist(record)
        return WritebackOutcome(
            unit_id=unit_id,
            status=record.status,
            writeback_status=record.writeback_status,
            needs_recheck=record.needs_recheck,
            readback_applied=bool(evidence.applied),
        )

    # ---- 落库与恢复 -----------------------------------------------------

    def _persist(self, record: Confirmation) -> str:
        """把回写进度写进待办表（原始字符串列）。"""
        fields = {
            TODO_COLUMNS["status"]: record.status,
            TODO_COLUMNS["confirmed_by"]: identity_to_cell(record.confirmed_by),
            TODO_COLUMNS["confirmed_at"]: format_minute(record.confirmed_at),
            TODO_COLUMNS["writeback_status"]: record.writeback_status,
            TODO_COLUMNS["writeback_at"]: format_minute(record.writeback_at),
            TODO_COLUMNS["notes"]: json.dumps(list(record.notes), ensure_ascii=False),
        }
        return self.board.update_progress(record.unit_id, fields)

    def restore(self) -> tuple[Confirmation, ...]:
        """重启恢复：从待办表重建确认记录（不重发待办、不重放回写）。"""
        restored: list[Confirmation] = []
        for row in self.board.rows(self.shift.shift_id):
            record = confirmation_from_row(row.fields)
            self._confirmations[record.unit_id] = record
            restored.append(record)
        return tuple(restored)


def confirmation_from_row(fields: Mapping[str, Any]) -> Confirmation:
    """待办表一行 → 确认记录（枚举在业务层校验）。"""
    unit_id = str(fields.get(TODO_COLUMNS["unit_id"]) or "").strip()
    todo_id = str(fields.get(TODO_COLUMNS["todo_id"]) or "").strip()
    if is_blank(unit_id):
        raise ContractViolation("待办表行缺少 单元ID")
    if is_blank(todo_id):
        raise ContractViolation(f"待办表行 {unit_id} 缺少 待办ID（待办未创建不得登记回写）")
    scope = coerce_enum(
        ConfirmationScope, fields.get(TODO_COLUMNS["scope"]), field="粒度"
    ).value
    shift_id = str(fields.get(TODO_COLUMNS["shift_id"]) or "").strip()
    if is_blank(shift_id):
        raise ContractViolation(f"待办表行 {unit_id} 缺少 班次ID")
    assignee = identity_from_cell(fields.get(TODO_COLUMNS["assignee"]))
    if assignee is None:
        raise ContractViolation(f"待办表行 {unit_id} 缺少 负责人")
    status = coerce_enum(
        ConfirmationStatus, fields.get(TODO_COLUMNS["status"]), field="确认状态"
    ).value
    writeback_status = coerce_enum(
        WritebackStatus, fields.get(TODO_COLUMNS["writeback_status"]), field="回写状态"
    ).value
    confirmed_by = identity_from_cell(fields.get(TODO_COLUMNS["confirmed_by"]))
    confirmed_at = _moment(fields.get(TODO_COLUMNS["confirmed_at"]))
    writeback_at = _moment(fields.get(TODO_COLUMNS["writeback_at"]))
    notes_text = str(fields.get(TODO_COLUMNS["notes"]) or "").strip()
    try:
        notes = [str(item) for item in json.loads(notes_text)] if notes_text else []
    except json.JSONDecodeError as exc:  # pragma: no cover - 防御
        raise ContractViolation(f"待办表备注不是合法 JSON: {exc}") from exc
    return Confirmation(
        unit_id=unit_id,
        scope=scope,
        shift_id=shift_id,
        todo_id=todo_id,
        assignee=assignee,
        status=status,
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
        writeback_status=writeback_status,
        writeback_at=writeback_at,
        notes=list(notes),
    )


def _moment(text: Any) -> datetime | None:
    from contracts.timebase import parse_occurred_at

    if text is None or not str(text).strip():
        return None
    return parse_occurred_at(text).value


def assignee_of_unit(unit: ConfirmationUnit) -> IdentityRef:
    return as_identity(unit.assignee)


__all__ = [
    "AUTH_REQUIRED",
    "NOT_CONFIRMED",
    "READBACK_SOURCE",
    "WRITE_UNKNOWN",
    "ConfirmationUnit",
    "TodoAssignmentService",
    "WritebackOutcome",
    "assignee_of_unit",
    "confirmation_from_row",
]
