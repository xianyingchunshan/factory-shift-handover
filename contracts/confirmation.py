"""T01 公共契约：确认回写 Confirmation（issue #6 §5）。

- 确认单元粒度：关键级事项**逐项**发待办（event 级）；一般事务**整单**发待办（shift 级）。
- 字段：``todo_id``（钉钉待办）、``confirmed_by``、``confirmed_at``、``writeback_status``。
- 状态机：``pending → confirmed → written_back``。
- 硬规则：
  * 无回读不得标 ``confirmed`` → :data:`~contracts.errors.WRITE_UNKNOWN`
    （写入不等于完成，回读才算）。
  * 未确认先回写 → :data:`~contracts.errors.NOT_CONFIRMED`。
  * ``writeback_status=unknown`` 时**只回查不重放** → :meth:`Confirmation.guard_resend`
    抛 :data:`~contracts.errors.WRITE_UNKNOWN`，唯一出路是 :meth:`Confirmation.recheck`。
  * 缺身份 / 操作者与待办指向的人不一致（错人）→ :data:`~contracts.errors.AUTH_REQUIRED`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from .enums import (
    CONFIRMATION_SCOPE_LABELS,
    ConfirmationScope,
    ConfirmationStatus,
    WritebackStatus,
    coerce_enum,
    is_blank,
)
from .errors import (
    AUTH_REQUIRED,
    NOT_CONFIRMED,
    WRITE_UNKNOWN,
    ContractError,
    ContractViolation,
)
from .identity import IdentityRef, as_identity, require_identity, require_operator
from .timebase import ensure_aware, format_minute, truncate_to_minute

#: 可接受的回读来源。
READBACK_SOURCES: tuple[str, ...] = ("todo", "aitable", "message")

#: 确认单元字段顺序（后续模块不必猜字段）。
CONFIRMATION_FIELD_ORDER: tuple[str, ...] = (
    "unit_id",
    "scope",
    "shift_id",
    "todo_id",
    "assignee",
    "status",
    "confirmed_by",
    "confirmed_at",
    "writeback_status",
)


@dataclass(frozen=True)
class ReadbackEvidence:
    """一条回读证据：写入结果只有回读后才能算数。"""

    source: str
    observed_at: datetime
    applied: bool = False
    payload: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        source = str(self.source or "").strip().lower()
        if source not in READBACK_SOURCES:
            raise ContractViolation(
                f"回读来源非法: {self.source!r}（可用: {', '.join(READBACK_SOURCES)}）"
            )
        object.__setattr__(self, "source", source)
        object.__setattr__(
            self,
            "observed_at",
            truncate_to_minute(ensure_aware(self.observed_at, field="readback.observed_at")),
        )
        object.__setattr__(self, "payload", dict(self.payload or {}))

    def is_valid(self) -> bool:
        return self.source in READBACK_SOURCES and self.observed_at is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "observed_at": format_minute(self.observed_at),
            "applied": bool(self.applied),
        }


def _require_applied_readback(
    readback: ReadbackEvidence | None, *, what: str, unit_id: str
) -> ReadbackEvidence:
    """要求有效且"已生效"的回读证据；否则结果不明（WRITE_UNKNOWN）。"""
    if readback is None or not readback.is_valid():
        raise ContractError(
            WRITE_UNKNOWN,
            f"{what} 缺少回读证据：写入不等于完成，回读才算",
            detail={"unit_id": unit_id},
        )
    if not readback.applied:
        raise ContractError(
            WRITE_UNKNOWN,
            f"{what} 的回读显示未生效：不得标记完成，只能继续回查",
            detail={"unit_id": unit_id, "readback_source": readback.source},
        )
    return readback


@dataclass(frozen=True)
class ConfirmationUnit:
    """待办计划条目（确认单元）。``todo_id`` 由待办创建后回填。"""

    unit_id: str
    scope: str
    shift_id: str
    assignee: IdentityRef
    label: str = ""
    todo_id: str = ""

    def __post_init__(self) -> None:
        if is_blank(self.unit_id):
            raise ContractViolation("确认单元 unit_id 不能为空")
        if is_blank(self.shift_id):
            raise ContractViolation("确认单元 shift_id 不能为空")
        scope = coerce_enum(ConfirmationScope, self.scope, field="scope")
        assignee = as_identity(self.assignee)
        todo_id = str(self.todo_id or "").strip()
        object.__setattr__(self, "scope", scope.value)
        object.__setattr__(self, "assignee", assignee)
        object.__setattr__(self, "todo_id", todo_id)
        object.__setattr__(self, "label", str(self.label or "").strip())

    @property
    def scope_label(self) -> str:
        return CONFIRMATION_SCOPE_LABELS[ConfirmationScope(self.scope)]

    @property
    def has_todo(self) -> bool:
        return bool(self.todo_id)

    def attach_todo(self, todo_id: str) -> "ConfirmationUnit":
        if is_blank(todo_id):
            raise ContractViolation("todo_id 不能为空")
        return ConfirmationUnit(
            unit_id=self.unit_id,
            scope=self.scope,
            shift_id=self.shift_id,
            assignee=self.assignee,
            label=self.label,
            todo_id=str(todo_id).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "scope": self.scope,
            "scope_label": self.scope_label,
            "shift_id": self.shift_id,
            "assignee": self.assignee.to_dict(),
            "label": self.label,
            "todo_id": self.todo_id,
            "has_todo": self.has_todo,
        }


@dataclass
class Confirmation:
    """一个确认单元的回写记录。"""

    unit_id: str
    scope: str
    shift_id: str
    todo_id: str
    assignee: IdentityRef
    status: str = ConfirmationStatus.PENDING
    confirmed_by: IdentityRef | None = None
    confirmed_at: datetime | None = None
    writeback_status: str = WritebackStatus.NOT_SENT
    writeback_at: datetime | None = None
    readback: ReadbackEvidence | None = None
    writeback_readback: ReadbackEvidence | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if is_blank(self.todo_id):
            raise ContractViolation("todo_id 不能为空：待办未创建不得登记回写")
        self.todo_id = str(self.todo_id).strip()
        self.scope = coerce_enum(ConfirmationScope, self.scope, field="scope").value
        self.status = coerce_enum(ConfirmationStatus, self.status, field="status").value
        self.writeback_status = coerce_enum(
            WritebackStatus, self.writeback_status, field="writeback_status"
        ).value
        self.assignee = as_identity(self.assignee)

    # ---- 构造 -----------------------------------------------------------

    @classmethod
    def for_unit(cls, unit: ConfirmationUnit) -> "Confirmation":
        """由计划条目建待确认记录；未创建待办（无 todo_id）直接拒绝。"""
        if not unit.has_todo:
            raise ContractViolation(
                f"确认单元 {unit.unit_id} 尚未创建待办（todo_id 为空），不得登记回写"
            )
        return cls(
            unit_id=unit.unit_id,
            scope=unit.scope,
            shift_id=unit.shift_id,
            todo_id=unit.todo_id,
            assignee=unit.assignee,
        )

    # ---- 状态查询 -------------------------------------------------------

    @property
    def is_pending(self) -> bool:
        return self.status == ConfirmationStatus.PENDING

    @property
    def is_confirmed(self) -> bool:
        return self.status != ConfirmationStatus.PENDING

    @property
    def is_written_back(self) -> bool:
        return self.status == ConfirmationStatus.WRITTEN_BACK

    @property
    def needs_recheck(self) -> bool:
        """写入受理不明：只回查，不重放。"""
        return self.writeback_status == WritebackStatus.UNKNOWN

    # ---- 确认 -----------------------------------------------------------

    def mark_confirmed(
        self,
        operator: Any,
        *,
        at: datetime,
        readback: ReadbackEvidence | None,
        note: str = "",
    ) -> "Confirmation":
        """登记确认；必须有回读证据（无回读不得标 confirmed）。

        - 缺身份 / 错人 → :data:`AUTH_REQUIRED`
        - 无回读证据 → :data:`WRITE_UNKNOWN`（结果不明，先回查）
        - 已确认 → 幂等返回；已回写 → 拒绝
        """
        if self.status == ConfirmationStatus.WRITTEN_BACK:
            raise ContractViolation("该确认单元已回写完成，不接受再次确认")
        confirmed_by = require_operator(self.assignee, operator, field="confirmed_by")
        _require_applied_readback(readback, what="确认（pending→confirmed）", unit_id=self.unit_id)
        if self.status == ConfirmationStatus.CONFIRMED:
            return self
        self.confirmed_by = confirmed_by
        self.confirmed_at = truncate_to_minute(ensure_aware(at, field="confirmed_at"))
        self.readback = readback
        self.status = ConfirmationStatus.CONFIRMED.value
        if note:
            self.notes.append(note)
        return self

    # ---- 回写 -----------------------------------------------------------

    def record_writeback(
        self,
        result: str,
        *,
        readback: ReadbackEvidence | None = None,
        at: datetime | None = None,
    ) -> "Confirmation":
        """登记 AI 表格状态回写结果；仅 ``verified``（带回读）才进 ``written_back``。"""
        if self.status == ConfirmationStatus.PENDING:
            raise ContractError(
                NOT_CONFIRMED,
                "未确认不得回写：状态机 pending → confirmed → written_back",
                detail={"unit_id": self.unit_id, "status": self.status},
            )
        if self.status == ConfirmationStatus.WRITTEN_BACK:
            raise ContractViolation("已回写完成的确认单元不得重复回写")
        target = coerce_enum(WritebackStatus, result, field="writeback_status")
        if target == WritebackStatus.VERIFIED:
            _require_applied_readback(
                readback, what="标 verified 的回写", unit_id=self.unit_id
            )
            self.writeback_readback = readback
            self.writeback_status = WritebackStatus.VERIFIED.value
            self.status = ConfirmationStatus.WRITTEN_BACK.value
        else:
            self.writeback_status = target.value
        if at is not None:
            self.writeback_at = truncate_to_minute(ensure_aware(at, field="writeback_at"))
        return self

    def guard_resend(self) -> None:
        """重放前的闸门：结果不明只能回查；未确认或已完成不得写入。"""
        if self.writeback_status == WritebackStatus.UNKNOWN:
            raise ContractError(
                WRITE_UNKNOWN,
                "上次写入受理结果不明：只回查（recheck），不得重放",
                detail={"unit_id": self.unit_id, "todo_id": self.todo_id},
            )
        if self.status == ConfirmationStatus.PENDING:
            raise ContractError(
                NOT_CONFIRMED,
                "未确认不得写入：先取得操作者确认与回读",
                detail={"unit_id": self.unit_id},
            )
        if self.status == ConfirmationStatus.WRITTEN_BACK:
            raise ContractViolation("已回写完成，不得重复写入")

    def recheck(self, readback: ReadbackEvidence, *, at: datetime | None = None) -> "Confirmation":
        """回查（unknown 的唯一出路）：按回读结果落到 verified / not_applied。

        这里不看 ``applied`` 作为通过条件——回查的职责就是读出"到底生效没有"：
        ``applied=True`` → ``written_back``；``applied=False`` → ``not_applied``（保持已确认）。
        """
        if self.writeback_status != WritebackStatus.UNKNOWN:
            raise ContractViolation("仅受理不明（unknown）状态需要回查")
        if readback is None or not readback.is_valid():
            raise ContractError(
                WRITE_UNKNOWN,
                "回查仍无回读证据：结果继续不明，不得标记完成",
                detail={"unit_id": self.unit_id},
            )
        self.writeback_readback = readback
        self.writeback_at = (
            truncate_to_minute(ensure_aware(at, field="writeback_at"))
            if at is not None
            else self.writeback_at
        )
        if readback.applied:
            self.writeback_status = WritebackStatus.VERIFIED.value
            self.status = ConfirmationStatus.WRITTEN_BACK.value
            self.notes.append("回查确认已生效")
        else:
            self.writeback_status = WritebackStatus.NOT_APPLIED.value
            self.notes.append("回查确认未生效：保持已确认，由上层决定是否重新提交")
        return self

    def add_note(self, text: str) -> "Confirmation":
        if is_blank(text):
            raise ContractViolation("备注不能为空")
        self.notes.append(str(text).strip())
        return self

    # ---- 输出 -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "scope": self.scope,
            "shift_id": self.shift_id,
            "todo_id": self.todo_id,
            "assignee": self.assignee.to_dict(),
            "status": self.status,
            "confirmed_by": None if self.confirmed_by is None else self.confirmed_by.to_dict(),
            "confirmed_at": format_minute(self.confirmed_at),
            "writeback_status": self.writeback_status,
            "writeback_readback_applied": (
                None if self.writeback_readback is None else self.writeback_readback.applied
            ),
            "needs_recheck": self.needs_recheck,
            "notes": list(self.notes),
        }


def build_confirmation_plan(
    shift: Any,
    report: Any,
    *,
    todo_id_factory: Callable[[ConfirmationUnit], str] | None = None,
) -> tuple[ConfirmationUnit, ...]:
    """按清单生成确认单元：关键级逐项（event 级）+ 一般事务整单（shift 级）。

    ``report`` 为 :class:`~contracts.report.HandoverReport`（鸭子类型，避免循环依赖）。
    """
    factory = todo_id_factory or (lambda unit: "")
    units: list[ConfirmationUnit] = []
    for item in report.critical_items:
        unit = ConfirmationUnit(
            unit_id=item.event_id,
            scope=ConfirmationScope.EVENT,
            shift_id=shift.shift_id,
            assignee=shift.handover_to,
            label=f"关键事项：{item.description}",
        )
        todo_id = factory(unit)
        units.append(unit.attach_todo(todo_id) if todo_id else unit)
    normal_events = [event for event in report.event_flow if not event.is_critical]
    if normal_events:
        unit = ConfirmationUnit(
            unit_id=shift.shift_id,
            scope=ConfirmationScope.SHIFT,
            shift_id=shift.shift_id,
            assignee=shift.handover_to,
            label=f"一般事务整单（{len(normal_events)} 项）",
        )
        todo_id = factory(unit)
        units.append(unit.attach_todo(todo_id) if todo_id else unit)
    return tuple(units)


def plan_from_units(units: Iterable[ConfirmationUnit]) -> tuple[Confirmation, ...]:
    """把带 todo_id 的计划条目批量转成待确认记录。"""
    return tuple(Confirmation.for_unit(unit) for unit in units)


def require_operator_for(confirmation: Confirmation, operator: Any) -> IdentityRef:
    """独立入口：确认前先核验操作者身份（缺身份/错人 → AUTH_REQUIRED）。"""
    return require_operator(confirmation.assignee, operator, field="confirmed_by")


__all__ = [
    "CONFIRMATION_FIELD_ORDER",
    "READBACK_SOURCES",
    "Confirmation",
    "ConfirmationUnit",
    "ReadbackEvidence",
    "build_confirmation_plan",
    "plan_from_units",
    "require_operator_for",
]
