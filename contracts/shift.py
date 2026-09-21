"""T01 公共契约：班次 ShiftRecord（issue #6 §1）。

- 幂等键 = (handover_line, shift_date, shift_name)：**同班同线不重复建班次**
  （SPEC §5 防重），重复提交不建第二单。
- 状态机：``draft → submitted → confirmed → archived``；``blocked`` = 告警未清，
  清告警后回到进入 blocked 前的状态（issue #6 §1，§3 不变量）。
- 配置快照：班次建立时锁定交接线/人员/关键事项标准；变更须显式确认，
  不静默改历史记录（历史快照保留在 ``snapshot_history``）。

厂级口径（T04 增补，SPEC §0）：一次轮换一张单，``shift_name=stay_period``（驻场期）
即为该口径的取值；此时 ``start_time`` / ``end_time`` = **驻场期边界**（可长达数十天），
E003 越界判定边界 = 驻场期（**对 ``[start, end]`` 整体区间判定，跨零点相连日历日均属期内**，
不按单日切分，见 :func:`contracts.timebase.is_out_of_window`）。冻结字段与既有语义不变。

判重口径（T07 修订，SPEC §5；**只对 ``stay_period`` 生效，既有四类班次零改动**）：
一单跨数十天时 ``shift_date`` 落在区间内就有数十个合法取值，判重键
``(handover_line, shift_date, shift_name)`` 失去唯一性。补两道防线：

- **G1 契约校验**：驻场期单 ``shift_date`` 必须 == ``start_time.date()``（以起始日为业务日期），
  违反 → :class:`~contracts.errors.ContractViolation`。键因此重新唯一。
- **G2 区间重叠拒绝**：同交接线、同 ``shift_name`` 的驻场期窗口有交集且非同一 ``shift_id``
  → 拒绝（:func:`stay_period_conflict` → :func:`duplicate_shift_error`，**复用
  ``DUPLICATE_SHIFT``，不新增错误码**）；**端点相接不算重叠**。

两层都要挡：内存键层（:meth:`ShiftRegistry.register`）**与**表层
（:meth:`integrations.aitable.tables.ShiftTable.find` / ``workflow.intake.create_shift``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from .enums import (
    SHIFT_NAME_LABELS,
    ShiftName,
    ShiftStatus,
    coerce_enum,
    is_blank,
    label_of,
)
from .errors import (
    ALARM_NOT_CLEARED,
    DUPLICATE_SHIFT,
    ContractError,
    ContractViolation,
)
from .identity import IdentityRef, as_identity
from .timebase import (
    SHANGHAI,
    calendar_days_in_window,
    covers_calendar_day,
    ensure_aware,
    format_minute,
    truncate_to_minute,
)

#: 允许的状态迁移（blocked 的进入/退出由告警清态决定，见 transition）。
ALLOWED_TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    ShiftStatus.DRAFT: (ShiftStatus.SUBMITTED, ShiftStatus.BLOCKED),
    ShiftStatus.SUBMITTED: (ShiftStatus.CONFIRMED, ShiftStatus.BLOCKED),
    ShiftStatus.CONFIRMED: (ShiftStatus.ARCHIVED, ShiftStatus.BLOCKED),
    ShiftStatus.ARCHIVED: (),
    ShiftStatus.BLOCKED: (ShiftStatus.SUBMITTED,),
}

#: 需要"告警已清"才允许进入的状态。
GUARDED_STATUSES = (ShiftStatus.SUBMITTED, ShiftStatus.CONFIRMED, ShiftStatus.ARCHIVED)

#: T07 判重口径说明（文档与测试共用）：驻场期单以起始日为业务日期，同线同驻场期不重复建单。
STAY_PERIOD_DEDUP_NOTE = (
    "驻场期单以起始日为业务日期（shift_date == start_time.date()）；"
    "同交接线同班次名的驻场期窗口有交集（端点相接不算）且非同一 shift_id 时，"
    "复用 DUPLICATE_SHIFT 拒绝建第二单"
)


@dataclass(frozen=True)
class ConfigSnapshot:
    """班次建立时锁定的配置快照：交接线 / 人员 / 关键事项标准。"""

    handover_line: str
    handover_from: IdentityRef
    handover_to: IdentityRef
    locked_at: datetime
    critical_standard: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if is_blank(self.handover_line):
            raise ContractViolation("配置快照 handover_line 不能为空")
        object.__setattr__(self, "handover_line", str(self.handover_line).strip())
        object.__setattr__(self, "handover_from", as_identity(self.handover_from))
        object.__setattr__(self, "handover_to", as_identity(self.handover_to))
        object.__setattr__(
            self,
            "locked_at",
            truncate_to_minute(ensure_aware(self.locked_at, field="locked_at")),
        )
        object.__setattr__(
            self,
            "critical_standard",
            tuple(str(item).strip() for item in self.critical_standard if str(item).strip()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "handover_line": self.handover_line,
            "handover_from": self.handover_from.to_dict(),
            "handover_to": self.handover_to.to_dict(),
            "critical_standard": list(self.critical_standard),
            "locked_at": format_minute(self.locked_at),
        }


@dataclass
class ShiftRecord:
    """一个班次的记录契约（钉钉AI表格：班次表一行）。"""

    shift_id: str
    handover_line: str
    shift_date: date
    shift_name: str
    start_time: datetime
    end_time: datetime
    handover_from: IdentityRef
    handover_to: IdentityRef
    status: str = ShiftStatus.DRAFT
    config_snapshot: ConfigSnapshot | None = None
    config_revision: int = 0
    snapshot_history: list[ConfigSnapshot] = field(default_factory=list)
    blocked_from: str | None = None

    def __post_init__(self) -> None:
        if is_blank(self.shift_id):
            raise ContractViolation("shift_id 不能为空（稳定唯一，幂等键）")
        if is_blank(self.handover_line):
            raise ContractViolation("handover_line 不能为空（交接线由配置决定）")
        self.shift_id = str(self.shift_id).strip()
        self.handover_line = str(self.handover_line).strip()
        self.shift_name = coerce_enum(ShiftName, self.shift_name, field="shift_name").value
        self.status = coerce_enum(ShiftStatus, self.status, field="status").value
        self.handover_from = as_identity(self.handover_from)
        self.handover_to = as_identity(self.handover_to)
        if isinstance(self.shift_date, datetime):
            self.shift_date = self.shift_date.date()
        if not isinstance(self.shift_date, date):
            raise ContractViolation("shift_date 必须是 date")
        self.start_time = truncate_to_minute(ensure_aware(self.start_time, field="start_time"))
        self.end_time = truncate_to_minute(ensure_aware(self.end_time, field="end_time"))
        if self.end_time <= self.start_time:
            raise ContractViolation("end_time 必须晚于 start_time")
        if not (self.start_time.date() <= self.shift_date <= self.end_time.date()):
            raise ContractViolation("shift_date 必须落在班次区间内（跨零点班次取起始日）")
        # T07 G1（仅驻场期）：一单跨数十天，区间内每一天都是合法取值 → 判重键不唯一。
        # 钉死"以起始日为业务日期"后，同期间两次建单必然落到同一个键。
        if (
            self.shift_name == ShiftName.STAY_PERIOD.value
            and self.shift_date != self.start_time.date()
        ):
            raise ContractViolation(
                "驻场期单以起始日为业务日期：shift_date 必须等于 start_time 的日期"
                f"（shift_date={self.shift_date.isoformat()}，"
                f"start_time={format_minute(self.start_time)}）"
            )
        if self.config_snapshot is not None:
            self.config_snapshot = (
                self.config_snapshot
                if isinstance(self.config_snapshot, ConfigSnapshot)
                else ConfigSnapshot(**self.config_snapshot)  # type: ignore[arg-type]
            )

    # ---- 基本属性 -------------------------------------------------------

    @property
    def idempotency_key(self) -> tuple[str, str, str]:
        """同班同线判重键（不含 shift_id，用于识别重复建单）。"""
        return (self.handover_line, self.shift_date.isoformat(), str(self.shift_name))

    @property
    def label(self) -> str:
        return f"{self.handover_line}/{self.shift_date.isoformat()}/{label_of(ShiftName, self.shift_name)}"

    @property
    def is_blocked(self) -> bool:
        return self.status == ShiftStatus.BLOCKED

    def contains(self, moment: datetime) -> bool:
        """时间是否落在本班次闭区间内（E003 越界校验依据）。

        厂级口径（T04）：区间即**驻场期边界**，对 ``[start_time, end_time]``
        **整体区间**判定——跨零点相连日历日均属期内，不按单日切分。
        """
        return self.start_time <= moment <= self.end_time

    def window(self) -> tuple[datetime, datetime]:
        return (self.start_time, self.end_time)

    def overlaps_window(self, other: "ShiftRecord") -> bool:
        """两条班次窗口是否**有交集**（闭区间；T07：**端点相接不算重叠**）。

        判定用严格不等号：``a < d and c < b``。相接（``b == c``）时第二个条件为假 → 放行。
        """
        return self.start_time < other.end_time and other.start_time < self.end_time

    # ---- 驻场期口径（T04 增补，既有语义不变） ---------------------------

    @property
    def is_stay_period(self) -> bool:
        """是否"驻场期"口径（``shift_name=stay_period``：一次轮换一张单）。"""
        return self.shift_name == ShiftName.STAY_PERIOD.value

    def calendar_days(self) -> tuple[date, ...]:
        """期内全部相连日历日（驻场期跨零点时相连日均属期内）。"""
        return calendar_days_in_window(self.start_time, self.end_time)

    def covers_day(self, day: date) -> bool:
        """该日历日是否属期内（按 ``[start_time, end_time]`` 整体区间判定）。"""
        return covers_calendar_day(day, self.start_time, self.end_time)

    # ---- 状态机 ---------------------------------------------------------

    def transition(self, target: str | ShiftStatus, *, has_open_alarms: bool = False) -> "ShiftRecord":
        """状态迁移；告警未清时禁止进入 submitted/confirmed/archived。"""
        target_member = coerce_enum(ShiftStatus, target, field="status")
        if self.status == target_member:
            return self
        if has_open_alarms and target_member in GUARDED_STATUSES:
            raise ContractError(
                ALARM_NOT_CLEARED,
                f"存在未清除告警，班次 {self.shift_id} 不允许进入 {target_member.value}",
                detail={"shift_id": self.shift_id, "target": target_member.value},
            )
        allowed = ALLOWED_TRANSITIONS[self.status]
        if target_member not in allowed:
            raise ContractViolation(
                f"班次状态不允许 {self.status} → {target_member.value}（允许: "
                f"{', '.join(allowed) or '无'}）"
            )
        if target_member == ShiftStatus.BLOCKED and not has_open_alarms:
            raise ContractViolation("blocked 仅用于'告警未清'，不得手工置位")
        previous = self.status
        self.status = target_member.value
        self.blocked_from = previous if target_member == ShiftStatus.BLOCKED else None
        return self

    def suspend_for_alarms(self, has_open_alarms: bool) -> str:
        """按告警清态刷新 blocked：有未清告警 → blocked；已清 → 回到原状态。"""
        if has_open_alarms:
            if self.status != ShiftStatus.BLOCKED:
                self.blocked_from = self.status
                self.status = ShiftStatus.BLOCKED
        elif self.status == ShiftStatus.BLOCKED:
            self.status = self.blocked_from or ShiftStatus.SUBMITTED
            self.blocked_from = None
        return self.status

    # ---- 配置快照 -------------------------------------------------------

    def lock_config(self, snapshot: ConfigSnapshot) -> ConfigSnapshot:
        """班次建立时锁定配置（只允许在 draft 阶段静默锁定）。"""
        if self.status != ShiftStatus.DRAFT:
            raise ContractViolation("班次已提交，配置快照须走 change_config 显式变更")
        self.config_snapshot = snapshot
        return snapshot

    def change_config(self, snapshot: ConfigSnapshot, *, confirmed_by: IdentityRef | None) -> ConfigSnapshot:
        """配置变更须显式确认，且保留旧快照（不静默改历史记录）。"""
        if confirmed_by is None:
            raise ContractViolation("配置变更必须显式确认，不静默改历史记录")
        if self.config_snapshot is not None:
            self.snapshot_history.append(self.config_snapshot)
        self.config_snapshot = snapshot
        self.config_revision += 1
        return snapshot

    # ---- 输出 -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "handover_line": self.handover_line,
            "shift_date": self.shift_date.isoformat(),
            "shift_name": self.shift_name,
            "shift_name_label": label_of(ShiftName, self.shift_name),
            "start_time": format_minute(self.start_time),
            "end_time": format_minute(self.end_time),
            "handover_from": self.handover_from.to_dict(),
            "handover_to": self.handover_to.to_dict(),
            "status": self.status,
            "config_revision": self.config_revision,
            "config_snapshot": None if self.config_snapshot is None else self.config_snapshot.to_dict(),
        }


@dataclass(frozen=True)
class RegisterResult:
    """登记结果：``created`` 表示真的落了一条新班次（重复提交不双写）。"""

    shift_id: str
    created: bool
    idempotent_noop: bool
    record: ShiftRecord

    def to_dict(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "created": self.created,
            "idempotent_noop": self.idempotent_noop,
        }


def stay_period_conflict(
    incoming: ShiftRecord, candidates: Iterable[ShiftRecord]
) -> ShiftRecord | None:
    """在 ``candidates`` 中找出与 ``incoming`` 冲突的驻场期单（T07 G2 口径）。

    冲突条件（全部满足）：

    - 同交接线、同 ``shift_name``，且**双方都是**驻场期（既有四类班次一律不参与）；
    - 窗口有交集（:meth:`ShiftRecord.overlaps_window`，**端点相接不算**）；
    - 不是同一个 ``shift_id``（同一单重复提交仍走幂等，不误报）。

    命中返回已存在的那一条（供错误明细指向它），否则 ``None``。
    """
    if not incoming.is_stay_period:
        return None
    for other in candidates:
        if other.shift_id == incoming.shift_id:
            continue
        if not other.is_stay_period:
            continue
        if other.handover_line != incoming.handover_line:
            continue
        if not incoming.overlaps_window(other):
            continue
        return other
    return None


def duplicate_shift_error(
    existing: ShiftRecord, incoming: ShiftRecord, *, stay_period: bool = False
) -> ContractError:
    """构造 ``DUPLICATE_SHIFT`` 拒绝（T07 **复用冻结错误码，不新增**）。

    ``stay_period=False``（既有四类班次）时文案与明细与本卡之前**逐字节相同**；
    ``stay_period=True``（驻场期窗口重叠）时在文案里点明窗口与口径。
    """
    detail: dict[str, Any] = {
        "handover_line": incoming.handover_line,
        "shift_date": incoming.shift_date.isoformat(),
        "shift_name": str(incoming.shift_name),
        "existing_shift_id": existing.shift_id,
        "incoming_shift_id": incoming.shift_id,
    }
    if stay_period:
        existing_window = f"{format_minute(existing.start_time)}~{format_minute(existing.end_time)}"
        incoming_window = f"{format_minute(incoming.start_time)}~{format_minute(incoming.end_time)}"
        message = (
            "同交接线同驻场期不重复建单（窗口重叠："
            f"{existing_window} 与 {incoming_window}）；"
            f"已有班次 {existing.shift_id}，不为 {incoming.shift_id} 建第二单"
        )
        detail["reason"] = "stay_period_window_overlap"
        detail["existing_window"] = existing_window
        detail["incoming_window"] = incoming_window
    else:
        message = (
            f"同班同线已有班次 {existing.shift_id}，不为 {incoming.shift_id} 建第二单"
        )
        detail["reason"] = "same_idempotency_key"
    return ContractError(DUPLICATE_SHIFT, message, detail=detail)


class ShiftRegistry:
    """班次登记簿：同班同线只允许一套记录，重复提交不建第二单。"""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str], ShiftRecord] = {}
        self._by_id: dict[str, ShiftRecord] = {}

    def register(self, shift: ShiftRecord) -> RegisterResult:
        key = shift.idempotency_key
        known_key_owner = self._by_key.get(key)
        if known_key_owner is not None and known_key_owner.shift_id != shift.shift_id:
            raise duplicate_shift_error(known_key_owner, shift)
        # T07 G2（内存键层）：驻场期一单跨数十天，键之外还要按窗口重叠挡一次。
        conflicting = stay_period_conflict(shift, self._by_id.values())
        if conflicting is not None:
            raise duplicate_shift_error(conflicting, shift, stay_period=True)
        known_id_owner = self._by_id.get(shift.shift_id)
        if known_id_owner is not None:
            if known_id_owner.idempotency_key != key:
                raise ContractViolation(
                    f"shift_id {shift.shift_id} 已绑定另一班次，不得复用"
                )
            return RegisterResult(
                shift_id=shift.shift_id,
                created=False,
                idempotent_noop=True,
                record=known_id_owner,
            )
        self._by_key[key] = shift
        self._by_id[shift.shift_id] = shift
        return RegisterResult(
            shift_id=shift.shift_id, created=True, idempotent_noop=False, record=shift
        )

    def get(self, shift_id: str) -> ShiftRecord | None:
        return self._by_id.get(shift_id)

    def get_by_key(self, key: Iterable[str]) -> ShiftRecord | None:
        return self._by_key.get(tuple(key))  # type: ignore[arg-type]

    def find_same_slot(self, shift: ShiftRecord) -> ShiftRecord | None:
        return self._by_key.get(shift.idempotency_key)

    @property
    def created_count(self) -> int:
        return len(self._by_id)

    def all_shifts(self) -> tuple[ShiftRecord, ...]:
        return tuple(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)


def new_shift(
    *,
    shift_id: str,
    handover_line: str,
    shift_date: date,
    shift_name: str,
    start_time: datetime,
    end_time: datetime,
    handover_from: IdentityRef,
    handover_to: IdentityRef,
    critical_standard: Iterable[str] = (),
    status: str = ShiftStatus.DRAFT,
) -> ShiftRecord:
    """构造班次并同时锁定配置快照（业务侧推荐的建单入口）。"""
    shift = ShiftRecord(
        shift_id=shift_id,
        handover_line=handover_line,
        shift_date=shift_date,
        shift_name=shift_name,
        start_time=start_time,
        end_time=end_time,
        handover_from=handover_from,
        handover_to=handover_to,
        status=status,
    )
    shift.lock_config(
        ConfigSnapshot(
            handover_line=shift.handover_line,
            handover_from=shift.handover_from,
            handover_to=shift.handover_to,
            locked_at=start_time.astimezone(SHANGHAI),
            critical_standard=tuple(critical_standard),
        )
    )
    return shift


__all__ = [
    "ALLOWED_TRANSITIONS",
    "GUARDED_STATUSES",
    "STAY_PERIOD_DEDUP_NOTE",
    "ConfigSnapshot",
    "RegisterResult",
    "ShiftRecord",
    "ShiftRegistry",
    "duplicate_shift_error",
    "new_shift",
    "stay_period_conflict",
]
