"""T03 契约增补：班次接班人变更（SPEC「接班人确定」，2026-09-21 主控拍板）。

本模块是**纯增补**：既有五类契约（``shift`` / ``events`` / ``alarms`` / ``report`` /
``confirmation``）的语义、字段与行为零改动，错误码最小集不新增码，只在既有码上
补一条换人路径。规则：

- **唯一可信身份**：新接班人必须经解析得到**唯一**身份
  （:func:`resolve_unique_identity`）。答复为空 / 解析不到人 / 解析出多人 →
  :data:`~contracts.errors.AUTH_REQUIRED`——**缺输入不可代填**，不猜、不回落默认人。
- **态约束**：``archived``（已归档）是终态，拒绝变更；``blocked``（告警未清）
  或调用方明示存在未清告警，拒绝变更 → :data:`~contracts.errors.ALARM_NOT_CLEARED`。
- **显式操作 + 留痕**：变更必须带显式操作者身份（``actor`` 缺失即拒，不静默），
  并产出 :class:`SuccessorChange` 留痕（谁 / 何时 / 从谁改到谁 / 依据答复），
  可回读（:class:`SuccessorChangeLog`）与序列化（``to_dict`` / ``from_dict``）。
- **不静默改历史**：换人走既有 :meth:`contracts.shift.ShiftRecord.change_config`，
  旧配置快照进 ``snapshot_history``、``config_revision`` 递增（SPEC §5 接班人例外条款）。

错误码映射（复用既有最小集，判据集中在 :func:`precheck_successor_change`）：

===============  ====================================================
场景              处置
===============  ====================================================
答复为空/解析不到/不唯一  ``AUTH_REQUIRED``（拒绝，不改班次）
``blocked`` / 告警未清     ``ALARM_NOT_CLEARED``（拒绝，不改班次）
``archived`` 终态          ``ContractViolation``（终态拒绝，与既有
                          ``contracts.confirmation`` 的终态拒绝写法一致）
改成同一个人              ``ContractViolation``（不构成变更）
===============  ====================================================

真实钉钉身份解析属 L4（主控在隔离环境执行）；本模块只定义**解析器契约**与校验，
解析实现由调用方注入（本卡一律用合成解析器，见 ``workflow.successor``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .enums import ShiftStatus, coerce_enum, is_blank
from .errors import ALARM_NOT_CLEARED, AUTH_REQUIRED, ContractError, ContractViolation
from .identity import IdentityRef, as_identity, require_identity
from .shift import ConfigSnapshot, ShiftRecord
from .timebase import ensure_aware, format_minute, now_shanghai, truncate_to_minute

#: 建班次前置询问的问题原文（SPEC：接班人不由配置写死，建班次时前置询问）。
SUCCESSOR_QUESTION = "本班接班人是谁？"


class SuccessorResolver(Protocol):
    """注入式身份解析器契约（真实钉钉实现属 L4，由主控注入）。

    实现约定：

    - ``resolve(答复文本)`` 返回**候选身份序列**（:class:`~contracts.identity.IdentityRef`
      或身份映射）；返回空序列表示"解析不到"。
    - **不得**回落默认人、不得猜人；多命中时如实返回全部候选，由
      :func:`resolve_unique_identity` 判定"不唯一"并拒绝。
    - 不得联网（本卡只接合成解析器；真实解析属 L4）。
    """

    name: str

    def resolve(self, query: str) -> Sequence[IdentityRef] | IdentityRef | None:
        ...  # pragma: no cover - 协议声明，无实现


def identity_key(ref: IdentityRef) -> tuple[str, str]:
    """身份判重键 ``(身份源, user_id)``；显示名不参与判定。"""
    return (ref.source, ref.user_id)


def _candidates(raw: Any) -> tuple[IdentityRef, ...]:
    """把解析器返回值归一化为身份序列；形态非法即拒绝（调用方缺陷）。"""
    if raw is None:
        return ()
    if isinstance(raw, (IdentityRef, Mapping)):
        return (as_identity(raw),)
    if isinstance(raw, (str, bytes)):
        raise ContractViolation(
            "解析器必须返回身份引用或其序列，不得直接返回文本（不做文字猜人）"
        )
    try:
        items = tuple(raw)
    except TypeError as exc:  # pragma: no cover - 防御
        raise ContractViolation(f"解析器返回值不支持迭代: {type(raw).__name__}") from exc
    return tuple(as_identity(item) for item in items)


def resolve_unique_identity(
    resolver: Any,
    answer: Any,
    *,
    field: str = "handover_to",
) -> IdentityRef:
    """把答复解析为**唯一可信身份**；解析不到 / 不唯一 / 答复为空即拒绝。

    拒绝一律 :data:`AUTH_REQUIRED`（缺输入不可代填），不回落默认人。
    """
    if is_blank(answer):
        raise ContractError(
            AUTH_REQUIRED,
            f"{field} 答复为空：缺输入不可代填，拒绝变更接班人",
            detail={"field": field, "query": ""},
        )
    query = str(answer).strip()
    if not hasattr(resolver, "resolve"):
        raise ContractViolation(
            f"{field} 解析器必须实现 resolve(query)，收到 {type(resolver).__name__}"
        )
    people = _candidates(resolver.resolve(query))
    unique: dict[tuple[str, str], IdentityRef] = {}
    for person in people:
        unique.setdefault(identity_key(person), person)
    if not unique:
        raise ContractError(
            AUTH_REQUIRED,
            f"{field} 答复 {query!r} 解析不到可信身份（钉钉里找不到）：拒绝变更",
            detail={"field": field, "query": query, "candidates": 0},
        )
    if len(unique) > 1:
        raise ContractError(
            AUTH_REQUIRED,
            f"{field} 答复 {query!r} 解析出 {len(unique)} 个身份，不唯一：拒绝变更",
            detail={
                "field": field,
                "query": query,
                "candidates": len(unique),
                "sources": sorted(key[0] for key in unique),
            },
        )
    return next(iter(unique.values()))


def precheck_successor_change(shift: ShiftRecord, *, has_open_alarms: bool = False) -> None:
    """换人前置检查（判据只写在这一处）：告警未清 / 已归档 / 状态非法。

    调用方只要照着抛出的异常处置即可；本函数不改动班次。
    """
    status = coerce_enum(ShiftStatus, shift.status, field="status")
    if has_open_alarms or status == ShiftStatus.BLOCKED:
        raise ContractError(
            ALARM_NOT_CLEARED,
            f"班次 {shift.shift_id} 告警未清（blocked），拒绝变更接班人",
            detail={"shift_id": shift.shift_id, "status": status.value},
        )
    if status == ShiftStatus.ARCHIVED:
        raise ContractViolation(
            f"班次 {shift.shift_id} 已归档（archived）：终态不接受接班人变更"
        )
    return None


def apply_prequery(shift: ShiftRecord, successor: IdentityRef, *, at: datetime) -> ConfigSnapshot:
    """建班次**前置询问**落地：把已解析的接班人写入 ``handover_to`` 并锁定配置快照。

    只允许在建班次阶段（``draft``）调用；写入前不校验身份以外的内容，
    身份校验由 :func:`resolve_unique_identity` 负责（缺输入不可代填）。
    """
    status = coerce_enum(ShiftStatus, shift.status, field="status")
    if status != ShiftStatus.DRAFT:
        raise ContractViolation(
            f"班次 {shift.shift_id} 已提交（{status.value}）：换人须走 change_successor，"
            "前置询问只在建班次阶段"
        )
    person = as_identity(successor)
    moment = truncate_to_minute(ensure_aware(at, field="locked_at"))
    shift.handover_to = person
    previous = shift.config_snapshot
    snapshot = ConfigSnapshot(
        handover_line=shift.handover_line,
        handover_from=shift.handover_from,
        handover_to=person,
        locked_at=moment,
        critical_standard=tuple(previous.critical_standard) if previous is not None else (),
    )
    shift.lock_config(snapshot)
    return snapshot


@dataclass(frozen=True)
class SuccessorChange:
    """一条接班人变更留痕：谁 / 何时 / 从谁改到谁 / 依据哪句答复。

    ``voided_todo_ids`` / ``issued_todo_ids`` 由工作流层在"路由跟随"完成后回填
    （旧待办显式作废、新待办重发），契约层不掺业务动作。
    """

    shift_id: str
    from_identity: IdentityRef
    to_identity: IdentityRef
    changed_by: IdentityRef
    changed_at: datetime
    ordinal: int = 0
    request_text: str = ""
    resolver: str = ""
    reason: str = ""
    voided_todo_ids: tuple[str, ...] = ()
    issued_todo_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if is_blank(self.shift_id):
            raise ContractViolation("接班人变更留痕缺少 shift_id")
        object.__setattr__(self, "shift_id", str(self.shift_id).strip())
        object.__setattr__(self, "from_identity", as_identity(self.from_identity))
        object.__setattr__(self, "to_identity", as_identity(self.to_identity))
        object.__setattr__(self, "changed_by", as_identity(self.changed_by))
        object.__setattr__(
            self,
            "changed_at",
            truncate_to_minute(ensure_aware(self.changed_at, field="changed_at")),
        )
        ordinal = int(self.ordinal)
        if ordinal < 0:
            raise ContractViolation("接班人变更留痕的序号不能为负")
        object.__setattr__(self, "ordinal", ordinal)
        for name in ("request_text", "resolver", "reason"):
            object.__setattr__(self, name, str(getattr(self, name) or "").strip())
        for name in ("voided_todo_ids", "issued_todo_ids"):
            object.__setattr__(
                self, name, tuple(str(item).strip() for item in getattr(self, name))
            )

    # ---- 标识与派生 -----------------------------------------------------

    @property
    def change_id(self) -> str:
        """留痕主键：``班次ID#序号``（序号从 1 开始）。"""
        return f"{self.shift_id}#{self.ordinal}"

    @property
    def is_routed(self) -> bool:
        """路由跟随是否已留痕（旧待办作废 + 新待办重发都有据）。"""
        return bool(self.voided_todo_ids) and bool(self.issued_todo_ids)

    def with_routing(
        self, *, voided_todo_ids: Iterable[str] = (), issued_todo_ids: Iterable[str] = ()
    ) -> "SuccessorChange":
        """回填路由跟随证据（不可变对象：返回新实例）。"""
        return replace(
            self,
            voided_todo_ids=tuple(voided_todo_ids),
            issued_todo_ids=tuple(issued_todo_ids),
        )

    # ---- 序列化（留痕可回读） -------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "shift_id": self.shift_id,
            "ordinal": self.ordinal,
            "from_identity": self.from_identity.to_dict(),
            "to_identity": self.to_identity.to_dict(),
            "changed_by": self.changed_by.to_dict(),
            "changed_at": format_minute(self.changed_at),
            "request_text": self.request_text,
            "resolver": self.resolver,
            "reason": self.reason,
            "voided_todo_ids": list(self.voided_todo_ids),
            "issued_todo_ids": list(self.issued_todo_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SuccessorChange":
        if not isinstance(data, Mapping):
            raise ContractViolation(f"接班人变更留痕需要映射，收到 {type(data).__name__}")
        missing = [
            key
            for key in ("shift_id", "from_identity", "to_identity", "changed_by", "changed_at")
            if is_blank(data.get(key))
        ]
        if missing:
            raise ContractViolation(f"接班人变更留痕缺少字段: {', '.join(missing)}")
        from .timebase import parse_time_text

        parsed = parse_time_text(data.get("changed_at"), field="changed_at")
        if parsed.value is None:
            raise ContractViolation("接班人变更留痕的 changed_at 必须精确到分")
        return cls(
            shift_id=data["shift_id"],
            from_identity=data["from_identity"],
            to_identity=data["to_identity"],
            changed_by=data["changed_by"],
            changed_at=parsed.value,
            ordinal=int(data.get("ordinal") or 0),
            request_text=str(data.get("request_text") or ""),
            resolver=str(data.get("resolver") or ""),
            reason=str(data.get("reason") or ""),
            voided_todo_ids=tuple(data.get("voided_todo_ids") or ()),
            issued_todo_ids=tuple(data.get("issued_todo_ids") or ()),
        )


@dataclass
class SuccessorChangeLog:
    """一个班次的接班人变更留痕账本（内存 + 可序列化；落表由工作流层负责）。"""

    shift_id: str
    _entries: list[SuccessorChange] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if is_blank(self.shift_id):
            raise ContractViolation("变更留痕账本缺少 shift_id")
        self.shift_id = str(self.shift_id).strip()

    # ---- 写入 -----------------------------------------------------------

    def record(
        self,
        change: SuccessorChange | None = None,
        *,
        from_identity: IdentityRef | None = None,
        to_identity: IdentityRef | None = None,
        changed_by: IdentityRef | None = None,
        changed_at: datetime | None = None,
        request_text: str = "",
        resolver: str = "",
        reason: str = "",
        voided_todo_ids: Iterable[str] = (),
        issued_todo_ids: Iterable[str] = (),
    ) -> SuccessorChange:
        """登记一条变更：序号自动递增（同一班次内从 1 开始）。"""
        if change is None:
            if from_identity is None or to_identity is None or changed_by is None:
                raise ContractViolation("变更留痕缺少 从谁/改成谁/操作人 之一")
            change = SuccessorChange(
                shift_id=self.shift_id,
                from_identity=from_identity,
                to_identity=to_identity,
                changed_by=changed_by,
                changed_at=changed_at or now_shanghai(),
                request_text=request_text,
                resolver=resolver,
                reason=reason,
                voided_todo_ids=tuple(voided_todo_ids),
                issued_todo_ids=tuple(issued_todo_ids),
            )
        if change.shift_id != self.shift_id:
            raise ContractViolation(
                f"变更留痕属于班次 {change.shift_id}，不能记进 {self.shift_id} 的账本"
            )
        change = change.with_routing(
            voided_todo_ids=change.voided_todo_ids or tuple(voided_todo_ids),
            issued_todo_ids=change.issued_todo_ids or tuple(issued_todo_ids),
        )
        record = replace(change, ordinal=len(self._entries) + 1)
        self._entries.append(record)
        return record

    def replace(self, change: SuccessorChange) -> SuccessorChange:
        """按 ``change_id`` 就地更新一条留痕（路由跟随证据回填用）。"""
        for index, known in enumerate(self._entries):
            if known.change_id == change.change_id:
                self._entries[index] = change
                return change
        raise ContractViolation(f"账本里没有变更留痕 {change.change_id!r}")

    def restore(self, changes: Iterable[SuccessorChange]) -> "SuccessorChangeLog":
        """按外部存储（工作流辅助表）重建账本：清空后按序号升序重放。"""
        self._entries = sorted(changes, key=lambda item: item.ordinal)
        for index, entry in enumerate(self._entries, start=1):
            if entry.shift_id != self.shift_id:
                raise ContractViolation(
                    f"变更留痕属于班次 {entry.shift_id}，不能记进 {self.shift_id} 的账本"
                )
            if entry.ordinal != index:
                raise ContractViolation(
                    f"变更留痕序号不连续：期望 {index}，实际 {entry.ordinal}"
                )
        return self

    # ---- 回读 -----------------------------------------------------------

    def entries(self) -> tuple[SuccessorChange, ...]:
        """按序号升序的留痕（只读快照）。"""
        return tuple(self._entries)

    def latest(self) -> SuccessorChange | None:
        return self._entries[-1] if self._entries else None

    def get(self, change_id: str) -> SuccessorChange | None:
        for entry in self._entries:
            if entry.change_id == change_id:
                return entry
        return None

    @property
    def count(self) -> int:
        return len(self._entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "changes": [entry.to_dict() for entry in self._entries],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SuccessorChangeLog":
        if not isinstance(payload, Mapping):
            raise ContractViolation("变更留痕账本需要映射")
        shift_id = str(payload.get("shift_id") or "").strip()
        if is_blank(shift_id):
            raise ContractViolation("变更留痕账本缺少 shift_id")
        log = cls(shift_id=shift_id)
        return log.restore(
            SuccessorChange.from_dict(item) for item in payload.get("changes") or ()
        )


def change_successor(
    shift: ShiftRecord,
    new_successor: IdentityRef | Mapping[str, Any] | None,
    *,
    actor: Any,
    at: datetime | None = None,
    log: SuccessorChangeLog | None = None,
    request_text: str = "",
    resolver: str = "",
    reason: str = "",
    has_open_alarms: bool = False,
) -> SuccessorChange:
    """变更班次接班人：态约束 → 身份校验 → 显式留痕 → 写班次（不静默）。

    顺序固定：所有拒绝都在改动班次**之前**抛出（拒绝即原样不动）。

    - 归档 / 告警未清 → 拒绝（见 :func:`precheck_successor_change`）；
    - 新接班人 / 操作者缺身份 → :data:`AUTH_REQUIRED`；
    - 与现任同一人 → ``ContractViolation``（不构成变更，不制造假留痕）；
    - 通过后：写 ``handover_to`` → 走 ``change_config``（旧快照进历史、revision+1）
      → 返回留痕（``log`` 非空时同时记入账本）。
    """
    precheck_successor_change(shift, has_open_alarms=has_open_alarms)
    successor = require_identity(new_successor, field="new_successor")
    changed_by = require_identity(actor, field="changed_by")
    moment = truncate_to_minute(ensure_aware(at or now_shanghai(), field="changed_at"))
    previous = shift.handover_to
    if previous.same_person(successor):
        raise ContractViolation(
            f"新接班人与现任接班人同为 {successor.user_id}：不构成变更，不做无意义留痕"
        )
    change = SuccessorChange(
        shift_id=shift.shift_id,
        from_identity=previous,
        to_identity=successor,
        changed_by=changed_by,
        changed_at=moment,
        request_text=request_text,
        resolver=resolver,
        reason=reason,
    )
    if log is not None:
        change = log.record(change)
    shift.handover_to = successor
    prefix = shift.config_snapshot
    snapshot = ConfigSnapshot(
        handover_line=shift.handover_line,
        handover_from=shift.handover_from,
        handover_to=successor,
        locked_at=moment,
        critical_standard=tuple(prefix.critical_standard) if prefix is not None else (),
    )
    shift.change_config(snapshot, confirmed_by=changed_by)
    return change


__all__ = [
    "SUCCESSOR_QUESTION",
    "SuccessorChange",
    "SuccessorChangeLog",
    "SuccessorResolver",
    "apply_prequery",
    "change_successor",
    "identity_key",
    "precheck_successor_change",
    "resolve_unique_identity",
]
