"""T03 实现：接班人前置询问与换人路由跟随（issue #12）。

三件事（契约复用，规则不复制）：

1. **建班次前置询问**（:class:`SuccessorPrequery`）：把契约问题
   :data:`contracts.successor.SUCCESSOR_QUESTION`（"本班接班人是谁？"）作为建班次
   的前置问题，答复经**注入式解析器**解析为唯一可信身份后写入 ``handover_to``
   并锁定配置快照；解析不到 / 答复为空 → :data:`~contracts.errors.AUTH_REQUIRED`，
   **班次不落表**（缺输入不可代填）。
2. **换人变更**（:class:`SuccessorService.change`）：复用契约
   :func:`contracts.successor.change_successor`（态约束 + 唯一身份 + 显式留痕），
   变更行落工作流辅助表 ``接班人变更留痕表（工作流辅助）``，重启可读回；
   同时把新接班人写回班次表（``接班人`` 列）。
3. **路由跟随**：确认待办与提醒路由跟随新接班人 —— 旧人的待办**显式作废**
   （待办表 ``待办状态=voided`` + 作废时间/原因，行不删除、不静默残留），
   同一确认单元以 ``单元ID#R<变更序号>`` 新单元号对新人**重发待办**；
   提醒分发（只单发交班人 + 接班人两人）随班次接班人一并跟随。已回写完成的单元
   属历史留痕，**不作废**，在结果里显式列出（``kept_unit_ids``）。

边界（写进 PR 待主控确认项）：

- 真实钉钉身份解析与真实待办重发属 **L4**，本卡一律用合成解析器
  （:class:`SyntheticSuccessorResolver`）与合成适配器；``require_offline=True`` 时
  非合成实现直接被拒。
- 同一确认单元不重复建待办（待办表守卫）；换人重发用**新单元号**，
  旧单元行保留为作废留痕——因此"一个单元一条待办"的防重不变量不被削弱。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from contracts.confirmation import ConfirmationUnit
from contracts.errors import ContractViolation
from contracts.identity import IdentityRef, require_identity
from contracts.report import HandoverReport
from contracts.shift import RegisterResult, ShiftRecord
from contracts.successor import (
    SUCCESSOR_QUESTION,
    SuccessorChange,
    SuccessorChangeLog,
    apply_prequery,
    change_successor,
    resolve_unique_identity,
)
from contracts.timebase import format_minute, now_shanghai, truncate_to_minute

from integrations.aitable.adapter import AitableAdapter
from integrations.aitable.synthetic import assert_synthetic
from integrations.aitable.tables import (
    SUCCESSOR_CHANGE_TABLE,
    TODO_COLUMNS,
    TODO_STATE_VOIDED,
    ShiftTable,
    SuccessorChangeTable,
    TodoTable,
)

from .intake import ShiftIntakeService
from .todos import TodoAssignmentService

#: 合成解析器标识（真实钉钉解析属 L4，由主控注入）。
SYNTHETIC_RESOLVER = "synthetic"

#: 换人重发时新确认单元号的模板（``原单元号#R<变更序号>``）。
REISSUE_SUFFIX = "#R"


def reissued_unit_id(unit_id: str, *, ordinal: int) -> str:
    """换人重发的新单元号：``原单元号#R<变更序号>``（与旧单元留痕并存，不覆盖）。"""
    text = str(unit_id or "").strip()
    if not text:
        raise ContractViolation("单元号不能为空")
    return f"{logical_unit_id(text)}{REISSUE_SUFFIX}{int(ordinal)}"


def logical_unit_id(unit_id: str) -> str:
    """去掉换人重发后缀，取回逻辑单元号（连续换人时单元号不叠加后缀）。"""
    return str(unit_id or "").strip().split(REISSUE_SUFFIX, 1)[0]


class SyntheticSuccessorResolver:
    """合成注入式解析器：只在本地字典里查答复，不联网、不猜人。

    ``directory``：``答复原文 → 候选身份序列``。查不到即返回空序列
    （由契约 :func:`contracts.successor.resolve_unique_identity` 判为"解析不到"并拒绝）。
    """

    name = SYNTHETIC_RESOLVER
    offline = True

    def __init__(self, directory: Mapping[str, Iterable[Any]] | None = None) -> None:
        self.directory: dict[str, tuple[IdentityRef, ...]] = {
            str(alias).strip(): tuple(
                require_identity(item, field="resolver.candidate")
                for item in identities
            )
            for alias, identities in (directory or {}).items()
        }

    def resolve(self, query: str) -> tuple[IdentityRef, ...]:
        return self.directory.get(str(query or "").strip(), ())

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(sorted(self.directory))


def assert_synthetic_resolver(resolver: Any) -> None:
    """防御性自检：本卡流程只允许接合成解析器（真实钉钉解析属 L4）。"""
    if not getattr(resolver, "offline", False) or getattr(resolver, "name", "") != SYNTHETIC_RESOLVER:
        raise AssertionError(
            "本卡流程只允许注入合成解析器（offline=True, name='synthetic'）；"
            "真实钉钉身份解析属 L4，由主控在隔离环境执行"
        )


@dataclass(frozen=True)
class SuccessorAsk:
    """一次前置询问的问答留痕：问题、答复原文、解析出的唯一身份。"""

    question: str
    answer: str
    identity: IdentityRef

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "identity": self.identity.to_dict(),
        }


@dataclass(frozen=True)
class OpenShiftResult:
    """建班次前置询问的落地结果（班次 + 阶段①服务 + 登记结果）。"""

    ask: SuccessorAsk
    shift: ShiftRecord
    intake: ShiftIntakeService
    register: RegisterResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "ask": self.ask.to_dict(),
            "shift_id": self.shift.shift_id,
            "handover_to": self.shift.handover_to.to_dict(),
            "created": self.register.created,
            "idempotent_noop": self.register.idempotent_noop,
        }


class SuccessorPrequery:
    """建班次前置询问入口：先问"本班接班人是谁？"，解析成功才建班次。"""

    def __init__(
        self,
        resolver: Any,
        *,
        question: str = SUCCESSOR_QUESTION,
        clock: Callable[[], datetime] | None = None,
        require_offline: bool = True,
    ) -> None:
        if require_offline:
            assert_synthetic_resolver(resolver)
        text = str(question or "").strip()
        if not text:
            raise ContractViolation("前置询问的问题不能为空")
        self.resolver = resolver
        self.question = text
        self.clock = clock or now_shanghai

    # ---- 询问 -----------------------------------------------------------

    def ask(self, answer: Any) -> SuccessorAsk:
        """解析答复为唯一可信身份；解析不到 / 答复为空即拒绝。"""
        identity = resolve_unique_identity(self.resolver, answer, field="handover_to")
        return SuccessorAsk(question=self.question, answer=str(answer).strip(), identity=identity)

    # ---- 落地 -----------------------------------------------------------

    def build_shift(self, answer: Any, **shift_params: Any) -> tuple[SuccessorAsk, ShiftRecord]:
        """解析后构造班次：``handover_to`` 取解析结果并同时锁定配置快照。"""
        from contracts.shift import new_shift

        ask = self.ask(answer)
        shift = new_shift(handover_to=ask.identity, **shift_params)
        apply_prequery(shift, ask.identity, at=self.clock())
        return ask, shift

    def open_shift(
        self, answer: Any, *, adapter: AitableAdapter, require_offline: bool = True, **shift_params: Any
    ) -> OpenShiftResult:
        """一步到位：前置询问 → 建班次 → 落班次表（解析失败则一行都不写）。"""
        ask, shift = self.build_shift(answer, **shift_params)
        intake = ShiftIntakeService(
            shift, adapter, clock=self.clock, require_offline=require_offline
        )
        register = intake.create_shift()
        return OpenShiftResult(ask=ask, shift=shift, intake=intake, register=register)


@dataclass(frozen=True)
class SuccessorChangeOutcome:
    """一次换人的落地结果：变更留痕 + 路由跟随（作废/重发/保留）明细。"""

    change: SuccessorChange
    voided_unit_ids: tuple[str, ...] = ()
    issued_unit_ids: tuple[str, ...] = ()
    kept_unit_ids: tuple[str, ...] = ()
    change_row_id: str = ""

    @property
    def previous_successor(self) -> IdentityRef:
        return self.change.from_identity

    @property
    def new_successor(self) -> IdentityRef:
        return self.change.to_identity

    @property
    def is_routed(self) -> bool:
        return self.change.is_routed

    def to_dict(self) -> dict[str, Any]:
        return {
            "change": self.change.to_dict(),
            "voided_unit_ids": list(self.voided_unit_ids),
            "issued_unit_ids": list(self.issued_unit_ids),
            "kept_unit_ids": list(self.kept_unit_ids),
            "change_row_id": self.change_row_id,
        }


class SuccessorService:
    """换人变更 + 路由跟随（班次已建之后；旧待办显式作废、新待办重发）。"""

    def __init__(
        self,
        shift: ShiftRecord,
        adapter: AitableAdapter,
        *,
        resolver: Any,
        todos: TodoAssignmentService | None = None,
        clock: Callable[[], datetime] | None = None,
        require_offline: bool = True,
    ) -> None:
        if require_offline:
            assert_synthetic(adapter)
            assert_synthetic_resolver(resolver)
        self.shift = shift
        self.adapter = adapter
        self.resolver = resolver
        self.clock = clock or now_shanghai
        self.todos = (
            todos
            if todos is not None
            else TodoAssignmentService(adapter, shift, clock=self.clock, require_offline=require_offline)
        )
        self.shift_table = ShiftTable(adapter)
        self.board = TodoTable(adapter)
        self.changes = SuccessorChangeTable(adapter)
        self.log = SuccessorChangeLog(shift.shift_id)
        self._voided_by: dict[str, str] = {}

    # ---- 解析 -----------------------------------------------------------

    def resolve(self, answer: Any) -> IdentityRef:
        """把答复解析为唯一可信身份（换人入口的身份校验走这里）。"""
        return resolve_unique_identity(self.resolver, answer, field="new_successor")

    # ---- 变更 + 路由跟随 ------------------------------------------------

    def change(
        self,
        *,
        actor: Any,
        at: datetime | None = None,
        answer: Any = None,
        new_successor: Any = None,
        reason: str = "",
        has_open_alarms: bool = False,
    ) -> SuccessorChangeOutcome:
        """换人：解析 → 契约层变更（态约束/留痕）→ 落留痕表 → 路由跟随 → 回填。

        拒绝路径（归档 / 告警未清 / 解析不到 / 缺操作者）都在改动班次之前抛出。
        """
        moment = truncate_to_minute(at or self.clock())
        successor = self._target_successor(answer=answer, new_successor=new_successor)
        request_text = "" if answer is None else str(answer).strip()
        change = change_successor(
            self.shift,
            successor,
            actor=actor,
            at=moment,
            log=self.log,
            request_text=request_text,
            resolver=str(getattr(self.resolver, "name", "") or ""),
            reason=reason,
            has_open_alarms=has_open_alarms,
        )
        self.shift_table.upsert(self.shift)
        row_id = self.changes.record(change)
        voided, issued, kept = self._route_follow(change, at=moment)
        if voided or issued:
            routed = change.with_routing(voided_todo_ids=voided, issued_todo_ids=issued)
            self.log.replace(routed)
            self.changes.amend(routed)
            change = routed
        return SuccessorChangeOutcome(
            change=change,
            voided_unit_ids=voided,
            issued_unit_ids=issued,
            kept_unit_ids=kept,
            change_row_id=row_id,
        )

    def _target_successor(self, *, answer: Any, new_successor: Any) -> IdentityRef:
        """新接班人：答复解析优先；与已给身份不一致即拒绝（不猜、不覆盖）。"""
        if answer is None and new_successor is None:
            raise ContractViolation("换人必须给出新接班人答复（answer）或已解析身份（new_successor）")
        if answer is None:
            return require_identity(new_successor, field="new_successor")
        resolved = self.resolve(answer)
        if new_successor is not None:
            given = require_identity(new_successor, field="new_successor")
            if not given.same_person(resolved):
                raise ContractViolation(
                    "答复解析结果与给定接班人身份不一致：拒绝变更（不猜人、不覆盖）"
                )
        return resolved

    def _route_follow(
        self, change: SuccessorChange, *, at: datetime
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """路由跟随：旧人待办显式作废 + 同单元对新接班人重发；已回写的显式保留。"""
        voided: list[str] = []
        issued: list[str] = []
        kept: list[str] = []
        for row in self.board.active_rows(self.shift.shift_id):
            unit_id = str(row.get(TODO_COLUMNS["unit_id"]) or "").strip()
            if not unit_id:
                raise ContractViolation("待办表行缺少 单元ID，换人路由无法跟随")
            record = self.todos.get(unit_id)  # 表里有活动待办，内存必须有对应确认
            if not record.assignee.same_person(change.from_identity):
                continue
            if record.is_written_back:
                kept.append(unit_id)
                continue
            self.todos.void_unit(
                unit_id, reason=f"接班人变更 {change.change_id}：旧待办显式作废", at=at
            )
            self._voided_by[unit_id] = change.change_id
            voided.append(unit_id)
            new_unit = self._reissue(unit_id, record, change)
            issued.append(new_unit.unit_id)
        return tuple(voided), tuple(issued), tuple(kept)

    def _reissue(self, unit_id: str, record: Any, change: SuccessorChange) -> ConfirmationUnit:
        """对同一确认单元按新单元号重建待办并登记（对新接班人）。"""
        fields = self.board.get_fields(unit_id) or {}
        label = str(fields.get(TODO_COLUMNS["label"]) or "").strip()
        unit = ConfirmationUnit(
            unit_id=reissued_unit_id(unit_id, ordinal=change.ordinal),
            scope=record.scope,
            shift_id=self.shift.shift_id,
            assignee=change.to_identity,
            label=label or f"换人重发（{change.change_id}）",
        )
        unit = unit.attach_todo(self.todos.todo_id_factory(unit))
        self.todos.attach_unit(unit)
        return unit

    # ---- 回读 -----------------------------------------------------------

    def changes_of_shift(self) -> tuple[SuccessorChange, ...]:
        """从留痕表**读回**本班次全部变更（谁 / 何时 / 从谁改到谁）。"""
        return self.changes.entries(self.shift.shift_id)

    def latest_change(self) -> SuccessorChange | None:
        entries = self.changes_of_shift()
        return entries[-1] if entries else None

    def voided_by_change(self, change_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(unit_id for unit_id, known in self._voided_by.items() if known == change_id)
        )

    def voided_units(self) -> tuple[Any, ...]:
        """已作废的确认单元（活动集合里已不存在，留痕仍可回读）。"""
        return self.todos.voided()

    def active_unit_ids(self) -> tuple[str, ...]:
        return tuple(sorted(record.unit_id for record in self.todos.confirmations))

    def dispatch_recipients(self) -> tuple[IdentityRef, ...]:
        """提醒路由：清单只单发交班人 + 接班人两人（换人后自动跟随新接班人）。"""
        return (self.shift.handover_from, self.shift.handover_to)

    def rebind_report(self, report: HandoverReport, outcome: SuccessorChangeOutcome) -> HandoverReport:
        """清单确认区跟随新接班人：被作废的单元用重发单元替换（顺序不变）。"""
        replaced = dict(zip(outcome.voided_unit_ids, outcome.issued_unit_ids))
        units: list[ConfirmationUnit] = []
        for unit in report.confirmation_area:
            new_unit_id = replaced.get(unit.unit_id)
            if new_unit_id is None:
                units.append(unit)
                continue
            record = self.todos.get(new_unit_id)
            units.append(
                ConfirmationUnit(
                    unit_id=new_unit_id,
                    scope=unit.scope,
                    shift_id=unit.shift_id,
                    assignee=record.assignee,
                    label=unit.label or record.unit_id,
                    todo_id=record.todo_id,
                )
            )
        report.confirmation_area = tuple(units)
        return report

    # ---- 重启恢复 -------------------------------------------------------

    def restore(self) -> tuple[SuccessorChange, ...]:
        """从留痕表重建变更账本（不重发待办、不重放外部写入）；作废态由待办表恢复。"""
        entries = self.changes_of_shift()
        self.log.restore(entries)
        for entry in entries:
            for unit_id in entry.voided_todo_ids:
                self._voided_by[unit_id] = entry.change_id
        return entries

    def check_consistency(self) -> tuple[str, ...]:
        """留痕一致性：留痕表中的变更 ↔ 账本 ↔ 已作废待办，任一不符即列为问题。"""
        problems: list[str] = []
        stored = self.changes_of_shift()
        if tuple(entry.change_id for entry in stored) != tuple(
            entry.change_id for entry in self.log.entries()
        ):
            problems.append("留痕表与变更账本的变更不一致（重启恢复缺失）")
        for entry in stored:
            if not entry.is_routed:
                problems.append(f"{entry.change_id} 缺少路由跟随留痕（作废/重发待办）")
        voided_rows = {
            str(row.get(TODO_COLUMNS["unit_id"]) or "").strip()
            for row in self.board.rows(self.shift.shift_id)
            if self.board.state_of(str(row.get(TODO_COLUMNS["unit_id"]) or "").strip())
            == TODO_STATE_VOIDED
        }
        expected = {unit for entry in stored for unit in entry.voided_todo_ids}
        if voided_rows != expected:
            problems.append(
                f"作废留痕与待办表不一致：留痕={sorted(expected)}，表={sorted(voided_rows)}"
            )
        return tuple(problems)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift.shift_id,
            "handover_to": self.shift.handover_to.to_dict(),
            "changes": [entry.to_dict() for entry in self.log.entries()],
            "voided_units": [record.unit_id for record in self.todos.voided()],
            "active_units": list(self.active_unit_ids()),
            "change_table": SUCCESSOR_CHANGE_TABLE,
            "generated_at": format_minute(self.clock()),
        }


__all__ = [
    "REISSUE_SUFFIX",
    "SUCCESSOR_QUESTION",
    "SYNTHETIC_RESOLVER",
    "OpenShiftResult",
    "SuccessorAsk",
    "SuccessorChangeOutcome",
    "SuccessorPrequery",
    "SuccessorService",
    "SyntheticSuccessorResolver",
    "assert_synthetic_resolver",
    "logical_unit_id",
    "reissued_unit_id",
]
