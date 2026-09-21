"""T05 编排层：EAM 只读拉取 → 九类候选行 → 既有四条告警入表（issue #23 第 3/4 条）。

职责边界：

- **只读拉取**：经 :class:`integrations.eam.adapter.EamReadOnlyAdapter` 取窗口内记录，
  入参窗口 = 驻场期（``EamWindow.from_shift``，复用 ``contracts/timebase`` 口径）；
- **映射**：走 :mod:`integrations.eam.mapping` 的集中映射表（未知取值不猜 → 留空 + E004）；
- **入表**：候选行交给阶段① 既有的 :meth:`workflow.intake.ShiftIntakeService.submit`，
  四条告警规则（E001 拒收不落表 / E002 / E003 / E004）**原样复用，不重写**；
- **两层幂等**（T07 同款思路）：

  1. **编排层先查表跳过**：``existing_event_ids()`` 里已有的事件 ID 直接记 ``skipped``
     ——不 submit、不 append、不出新告警，也不碰已存在的行；
  2. **表格层兜底拒绝**：预检之后若表里出现了同一 ``event_id``（并发/外部写入），
     ``EventTable.append`` 抛拒绝；本层按“兜底拒绝”记为 ``skipped``，
     **不双写、不中断**，并把原因显式写进报告。

- **只 append、不 update**：拉取路径**从不调用** ``event_table.update``——
  状态与“处理进展”是人工维护的，自动化不得覆盖（钉死测试见
  ``tests/workflow/test_eam_pull.py``）。
- **显式报出**：拒收与告警一律进 :class:`EamPullReport`（条数 + 原因 + 来源编号），
  不静默丢；受理不明（``write_unknown``）**只回查不重放**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from contracts.alarms import AlarmLedger
from contracts.errors import ALARM_NOT_CLEARED, ContractViolation
from contracts.timebase import format_minute, now_shanghai

from integrations.eam.adapter import (
    KIND_LABELS,
    EamReadOnlyAdapter,
    EamRecord,
    EamWindow,
)
from integrations.eam.mapping import build_candidate
from integrations.eam.synthetic import assert_synthetic_eam
from .intake import IntakeOutcome, ShiftIntakeService

#: 逐条处置（报告的“显式报出”口径）。
ACTION_CREATED = "created"
ACTION_SKIPPED = "skipped"
ACTION_REJECTED = "rejected"
ACTION_UNMAPPED = "unmapped"
ACTION_PENDING = "pending_write"
ACTIONS: tuple[str, ...] = (
    ACTION_CREATED,
    ACTION_SKIPPED,
    ACTION_REJECTED,
    ACTION_UNMAPPED,
    ACTION_PENDING,
)

#: 跳过原因。
REASON_ALREADY_IN_TABLE = "already_in_table: 交接事件表已有同一 event_id，不双写"
REASON_MEMORY_DUPLICATE = "memory_duplicate: 内存已有同一事件，不重复复验"
REASON_TABLE_LAYER = (
    "table_layer_rejected_duplicate: 表格层兜底拒绝（预检之后表里出现同一 event_id）"
)
REASON_PENDING = "write_unknown: 落表受理不明，只回查不重放（见 reconcile_pending）"

#: 告警联动说明（写进报告，便于逐条比对）。
ALARM_LINKAGE_NOTE = (
    "候选行走既有四条规则：E001 缺时间→拒收不落表；E002 只有日期→落表待补；"
    "E003 越界→按驻场期整体区间判定；E004 关键字段缺失（含未知/为空映射结果）"
)


@dataclass(frozen=True)
class PullEntry:
    """一条 EAM 记录的拉取处置（来源编号 + 动作 + 原因 + 本条产生的告警规则）。"""

    ref_no: str
    kind: str
    event_id: str
    action: str
    reason: str = ""
    alarms: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_no": self.ref_no,
            "kind": self.kind,
            "kind_label": self.label,
            "event_id": self.event_id,
            "action": self.action,
            "reason": self.reason,
            "alarms": list(self.alarms),
        }


@dataclass(frozen=True)
class EamPullReport:
    """一次拉取的显式结果（新增 N / 跳过 M / 拒收 K / 未映射 J / 告警 A）。

    ``alarm_count`` = **本次新增**的告警条数（账本里已有的告警不重复计数，
    否则“跳过”就不是真跳过）；``open_alarm_count`` = 账本当前未清总数，
    两者一起构成“告警未清不得出清单”的证据。
    """

    shift_id: str
    window_start: str
    window_end: str
    fetched: int
    created: int
    skipped: int
    rejected: int
    unmapped: int
    pending: int
    alarm_count: int
    open_alarm_count: int
    table_rows: int
    generated_at: str = ""
    entries: tuple[PullEntry, ...] = ()
    open_alarms: tuple[Mapping[str, Any], ...] = ()

    # ---- 计数回读 -------------------------------------------------------

    def refs(self, action: str) -> tuple[str, ...]:
        """某类处置的来源编号（拒收/告警都能追到出处）。"""
        return tuple(entry.ref_no for entry in self.entries if entry.action == action)

    @property
    def created_refs(self) -> tuple[str, ...]:
        return self.refs(ACTION_CREATED)

    @property
    def skipped_refs(self) -> tuple[str, ...]:
        return self.refs(ACTION_SKIPPED)

    @property
    def rejected_refs(self) -> tuple[str, ...]:
        return self.refs(ACTION_REJECTED)

    @property
    def unmapped_refs(self) -> tuple[str, ...]:
        return self.refs(ACTION_UNMAPPED)

    @property
    def alarm_rules(self) -> tuple[str, ...]:
        """本次产生的告警规则码（去重、稳定序）。"""
        return tuple(sorted({rule for entry in self.entries for rule in entry.alarms}))

    def summary_line(self) -> str:
        """显式回读口径：新增 N / 跳过 M / 拒收 K / 未映射 J / 受理不明 P / 告警 A。"""
        return (
            f"新增 {self.created} / 跳过 {self.skipped} / 拒收 {self.rejected} / "
            f"未映射 {self.unmapped} / 受理不明 {self.pending} / "
            f"告警 {self.alarm_count}（未清 {self.open_alarm_count}）"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "window": {"start": self.window_start, "end": self.window_end},
            "generated_at": self.generated_at,
            "counts": {
                "fetched": self.fetched,
                "created": self.created,
                "skipped": self.skipped,
                "rejected": self.rejected,
                "unmapped": self.unmapped,
                "pending": self.pending,
                "alarm_count": self.alarm_count,
                "open_alarm_count": self.open_alarm_count,
                "table_rows": self.table_rows,
            },
            "summary": self.summary_line(),
            "entries": [entry.to_dict() for entry in self.entries],
            "open_alarms": [dict(item) for item in self.open_alarms],
        }


class EamPullService:
    """编排层：把 EAM 只读拉取接到阶段① 既有告警流上（本卡唯一入口）。"""

    def __init__(
        self,
        shift: Any,
        intake: ShiftIntakeService,
        eam: EamReadOnlyAdapter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        assert_synthetic_eam(eam)  # 误接真实适配器 → 立即失败，不拉取
        if intake.shift.shift_id != shift.shift_id:
            raise ContractViolation(
                f"编排层班次 {shift.shift_id!r} 与录入服务 {intake.shift.shift_id!r} 不符"
            )
        self.shift = shift
        self.intake = intake
        self.eam = eam
        self.clock = clock or now_shanghai

    # ---- 窗口 -----------------------------------------------------------

    def window(self) -> EamWindow:
        """本单窗口 = 驻场期边界（T04 冻结口径，不另立一套）。"""
        return EamWindow.from_shift(self.shift)

    # ---- 两层幂等 -------------------------------------------------------

    def existing_event_ids(self) -> frozenset[str]:
        """**第一层**（编排层）：交接事件表里已存在的事件 ID。

        子类可覆盖为 ``frozenset()`` 以验证**第二层（表格层兜底拒绝）独立成立**
        ——见 ``tests/workflow/test_eam_pull.py`` 的两层幂等用例。
        """
        return frozenset(self.intake.event_table.event_ids())

    # ---- 拉取 -----------------------------------------------------------

    def pull(self, *, window: EamWindow | None = None) -> EamPullReport:
        """拉取一个窗口的缺陷 + 隐患，映射后走既有告警流入表；重复拉取幂等跳过。"""
        target = window if window is not None else self.window()
        if target.shift_id != self.shift.shift_id:
            raise ContractViolation(
                f"窗口 shift_id {target.shift_id!r} 与本次拉取班次 "
                f"{self.shift.shift_id!r} 不符（跨驻场期请另建单，不是重复）"
            )
        records = list(self.eam.list_defects(target)) + list(self.eam.list_hazards(target))
        known = self.existing_event_ids()
        entries = tuple(self._process(record, known) for record in records)
        return self._report(target, entries)

    def _process(self, record: EamRecord, known: frozenset[str]) -> PullEntry:
        outcome = build_candidate(record, shift=self.shift)
        base = {"ref_no": outcome.ref_no or record.source_ref(), "kind": record.kind}
        if not outcome.ok:
            # 本地数据缺陷（缺编号/时间无法解析）：显式报出，不猜、不静默丢。
            return PullEntry(
                **base,
                event_id=outcome.event_id,
                action=ACTION_UNMAPPED,
                reason=outcome.reject_reason,
            )
        row = dict(outcome.row or {})
        event_id = str(row.get("event_id") or "")
        if event_id in known:
            # 第一层：编排层先查表跳过（不 submit、不 append、不出新告警、不碰既有行）。
            return PullEntry(
                **base,
                event_id=event_id,
                action=ACTION_SKIPPED,
                reason=REASON_ALREADY_IN_TABLE,
            )
        # 告警账本快照：submit 前后对比，用于只统计**本次新增**的告警（重复拉取不得
        # 把账本里已有的告警再报一遍——否则“跳过”就不是真跳过）。
        before = {alarm.alarm_id for alarm in self.intake.ledger.all_alarms()}
        try:
            result: IntakeOutcome = self.intake.submit(row)
        except ContractViolation as exc:
            if self.intake.event_table.find_row(event_id) is not None:
                # 第二层：表格层 append 兜底拒绝（预检之后表里出现了同一 event_id）。
                return PullEntry(
                    **base,
                    event_id=event_id,
                    action=ACTION_SKIPPED,
                    reason=f"{REASON_TABLE_LAYER}（{exc}）",
                )
            raise
        rules = tuple(
            alarm.rule for alarm in result.alarms if alarm.alarm_id not in before
        )
        if result.rejected:
            detail = "；".join(alarm.detail for alarm in result.alarms)
            return PullEntry(
                **base,
                event_id=event_id,
                action=ACTION_REJECTED,
                reason=f"E001 {detail}；补全时间后按同一编号重录（本行未落表）",
                alarms=rules,
            )
        if result.pending_write:
            return PullEntry(
                **base,
                event_id=event_id,
                action=ACTION_PENDING,
                reason=REASON_PENDING,
            )
        if result.duplicate:
            return PullEntry(
                **base,
                event_id=event_id,
                action=ACTION_SKIPPED,
                reason=REASON_MEMORY_DUPLICATE,
            )
        return PullEntry(
            **base,
            event_id=event_id,
            action=ACTION_CREATED,
            reason=(
                "落表成功（无告警）"
                if not rules
                else f"落表成功，带告警 {', '.join(rules)}（待补全/复核，未清不出清单）"
            ),
            alarms=rules,
        )

    def _report(self, window: EamWindow, entries: tuple[PullEntry, ...]) -> EamPullReport:
        def count(action: str) -> int:
            return sum(1 for entry in entries if entry.action == action)

        ledger = self.intake.ledger
        ref_by_event = {entry.event_id: entry.ref_no for entry in entries if entry.event_id}
        return EamPullReport(
            shift_id=self.shift.shift_id,
            window_start=format_minute(window.start),
            window_end=format_minute(window.end),
            fetched=len(entries),
            created=count(ACTION_CREATED),
            skipped=count(ACTION_SKIPPED),
            rejected=count(ACTION_REJECTED),
            unmapped=count(ACTION_UNMAPPED),
            pending=count(ACTION_PENDING),
            alarm_count=sum(len(entry.alarms) for entry in entries),
            open_alarm_count=ledger.open_count,
            table_rows=self.intake.event_table.count(),
            generated_at=format_minute(self.clock()),
            entries=entries,
            open_alarms=tuple(
                {
                    "rule": alarm.rule,
                    "field": alarm.field,
                    "event_id": alarm.event_id or "",
                    "ref_no": ref_by_event.get(alarm.event_id or "", ""),
                    "detail": alarm.detail,
                    "created_at": format_minute(alarm.created_at),
                }
                for alarm in ledger.open_alarms()
            ),
        )

    # ---- 闸门与处置 -----------------------------------------------------

    def require_clear(self) -> AlarmLedger:
        """告警未清不得出清单（**复用既有 ``ALARM_NOT_CLEARED``**，不新造闸门）。"""
        ledger = self.intake.ledger
        ledger.require_clear()
        return ledger

    def open_alarms(self) -> tuple[Mapping[str, Any], ...]:
        """当前未清告警（带规则 / 字段 / 事件 / 时间）。"""
        return tuple(
            {
                "rule": alarm.rule,
                "field": alarm.field,
                "event_id": alarm.event_id or "",
                "detail": alarm.detail,
                "created_at": format_minute(alarm.created_at),
            }
            for alarm in self.intake.ledger.open_alarms()
        )

    def pending_events(self) -> tuple[str, ...]:
        """受理不明的落表（只回查，不重放）。"""
        return self.intake.pending_events()

    def reconcile_pending(self) -> dict[str, str]:
        """回查全部受理不明的落表：``verified`` / ``not_applied``（不自动补写）。"""
        return {event_id: self.intake.reconcile(event_id) for event_id in self.pending_events()}


def pull_report_summary(report: EamPullReport) -> dict[str, Any]:
    """报告摘要（供 skill / CLI 复用；与 ``report.to_dict()['counts']`` 同源）。"""
    return dict(report.to_dict()["counts"])


__all__ = [
    "ACTIONS",
    "ACTION_CREATED",
    "ACTION_PENDING",
    "ACTION_REJECTED",
    "ACTION_SKIPPED",
    "ACTION_UNMAPPED",
    "ALARM_LINKAGE_NOTE",
    "ALARM_NOT_CLEARED",
    "EamPullReport",
    "EamPullService",
    "PullEntry",
    "REASON_ALREADY_IN_TABLE",
    "REASON_MEMORY_DUPLICATE",
    "REASON_PENDING",
    "REASON_TABLE_LAYER",
    "pull_report_summary",
]
