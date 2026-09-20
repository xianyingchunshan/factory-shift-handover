"""阶段①：交接班输入校验与落表（issue #9 第 1 段）。

复用冻结契约，不复制规则：

- 九类统一字段与四条告警（E001–E004）走 :class:`contracts.events.EventStore`
  （其中 E001 = 拒收且**不落表**；E002/E003/E004 = 落表 + 告警）。
- 关键级强制升级两条规则（``category=major``、``status=transferred``）走
  :func:`contracts.events.resolve_severity`，原值留痕 ``severity_forced`` /
  ``original_severity``，**不可降**。
- 告警账本与"未清不出清单"闸门走 :class:`contracts.alarms.AlarmLedger`
  （``require_clear`` → ``ALARM_NOT_CLEARED``）。
- 同班同线防重走 :class:`contracts.shift.ShiftRegistry`（``DUPLICATE_SHIFT``）。

本层额外承担三件事（契约不管的落地部分）：

1. **落表**：校验通过的事件经合成适配器写入交接事件表；同一 ``event_id`` 不双写。
2. **输入留痕**：原始行与处置结果写入工作流辅助表 ``输入留痕表``，供重启恢复与
   E001 拒收留痕（契约口径：E001 不落表，故拒收证据由本层留痕）。
3. **受理不明处置**：落表受理不明（``AitableWriteUnknown``）时**只回查不重放**；
   只有回查确认"未生效"才允许补写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from contracts.alarms import AlarmLedger, CompletenessAlarm
from contracts.enums import Severity, WritebackStatus, optional_enum
from contracts.errors import (
    ALARM_NOT_CLEARED,
    DUPLICATE_SHIFT,
    E001,
    WRITE_UNKNOWN,
    ContractError,
    ContractViolation,
)
from contracts.events import EventStore, HandoverEvent, IntakeResult
from contracts.shift import RegisterResult, ShiftRecord, ShiftRegistry, ShiftStatus
from contracts.timebase import format_minute, now_shanghai, parse_occurred_at, truncate_to_minute

from integrations.aitable.adapter import AitableAdapter, AitableRejected, AitableWriteUnknown
from integrations.aitable.cells import (
    event_from_fields,
    event_id_from_fields,
    event_raw_from_fields,
)
from integrations.aitable.synthetic import assert_synthetic
from integrations.aitable.tables import (
    OUTCOME_DUPLICATE,
    OUTCOME_REJECTED,
    OUTCOME_STORED,
    OUTCOME_WRITE_UNKNOWN,
    AlarmJournal,
    AlarmJournalEntry,
    EventTable,
    IntakeJournal,
    JournalEntry,
    ShiftTable,
)
from integrations.aitable.tables import ACTION_CLEARED
from .checklist import sync_shift_status

#: 落表待处置状态（受理不明 / 硬失败）。
PENDING_UNKNOWN = "write_unknown"
PENDING_FAILED = "write_failed"
PENDING_ABSENT = "absent"


@dataclass(frozen=True)
class IntakeOutcome:
    """一次录入的落地结果（含是否真的写进了交接事件表）。"""

    event_id: str
    result: IntakeResult
    record_id: str = ""
    write_unknown: bool = False
    write_failed: bool = False
    journal_record_id: str = ""

    @property
    def rejected(self) -> bool:
        return self.result.rejected

    @property
    def duplicate(self) -> bool:
        return self.result.duplicate

    @property
    def event(self) -> HandoverEvent | None:
        return self.result.event

    @property
    def stored(self) -> bool:
        """校验通过且已写入表格。"""
        return not self.rejected and not self.duplicate and bool(self.record_id)

    @property
    def alarms(self):
        return self.result.alarms

    @property
    def pending_write(self) -> bool:
        return self.write_unknown or self.write_failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "rejected": self.rejected,
            "duplicate": self.duplicate,
            "stored": self.stored,
            "record_id": self.record_id,
            "write_unknown": self.write_unknown,
            "write_failed": self.write_failed,
            "alarms": [alarm.to_dict() for alarm in self.alarms],
        }


@dataclass(frozen=True)
class RecheckResult:
    """交班提交前全表复验的结果。"""

    shift_id: str
    event_total: int
    event_complete: int
    stored_count: int
    open_alarm_count: int
    alarm_count: int
    rejected_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "event_total": self.event_total,
            "event_complete": self.event_complete,
            "stored_count": self.stored_count,
            "open_alarm_count": self.open_alarm_count,
            "alarm_count": self.alarm_count,
            "rejected_count": self.rejected_count,
        }


@dataclass(frozen=True)
class RestoreIssue:
    """重启恢复时发现的不一致（应人工处置，不自动补写）。"""

    event_id: str
    problem: str

    def to_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "problem": self.problem}


class ShiftIntakeService:
    """阶段①服务：建班次 → 逐条录入 → 交班提交前全表复验 → 提交。"""

    def __init__(
        self,
        shift: ShiftRecord,
        adapter: AitableAdapter,
        *,
        clock: Callable[[], datetime] | None = None,
        registry: ShiftRegistry | None = None,
        ledger: AlarmLedger | None = None,
        require_offline: bool = True,
    ) -> None:
        if require_offline:
            assert_synthetic(adapter)
        self.shift = shift
        self.adapter = adapter
        self.clock = clock or now_shanghai
        self.ledger = ledger if ledger is not None else AlarmLedger(shift_id=shift.shift_id)
        self.store = EventStore(shift, self.ledger, clock=self.clock)
        self.registry = registry if registry is not None else ShiftRegistry()
        self.shift_table = ShiftTable(adapter)
        self.event_table = EventTable(adapter)
        self.journal = IntakeJournal(adapter)
        self.alarm_journal = AlarmJournal(adapter)
        self._pending: dict[str, str] = {}

    # ---- 建班次（防重） -------------------------------------------------

    def create_shift(self) -> RegisterResult:
        """落班次表并登记幂等键；同班同线重复提交 → ``DUPLICATE_SHIFT``，不建第二单。"""
        known_id = self.shift_table.get(self.shift.shift_id)
        if known_id is not None and known_id.idempotency_key != self.shift.idempotency_key:
            raise ContractViolation(
                f"shift_id {self.shift.shift_id!r} 已绑定另一班次，不得复用"
            )
        known = self.shift_table.find(
            self.shift.handover_line, self.shift.shift_date, self.shift.shift_name
        )
        if known is not None:
            if known.shift_id != self.shift.shift_id:
                raise ContractError(
                    DUPLICATE_SHIFT,
                    f"同班同线已有班次 {known.shift_id}，不为 {self.shift.shift_id} 建第二单",
                    detail={
                        "handover_line": self.shift.handover_line,
                        "shift_date": self.shift.shift_date.isoformat(),
                        "existing_shift_id": known.shift_id,
                        "incoming_shift_id": self.shift.shift_id,
                    },
                )
            return RegisterResult(
                shift_id=known.shift_id,
                created=False,
                idempotent_noop=True,
                record=known,
            )
        self.registry.register(self.shift)
        self.shift_table.upsert(self.shift)
        return RegisterResult(
            shift_id=self.shift.shift_id,
            created=True,
            idempotent_noop=False,
            record=self.shift,
        )

    # ---- 录入 -----------------------------------------------------------

    def submit(self, row: Mapping[str, Any]) -> IntakeOutcome:
        """录入一行：四条校验 → 通过则落表；E001 拒收不落表。"""
        if not isinstance(row, Mapping):
            raise ContractViolation(f"录入行必须是映射，收到 {type(row).__name__}")
        raw = {str(key): value for key, value in row.items()}
        result = self.store.intake(raw)  # 契约层四条校验（可能抛 ContractViolation）
        event_id = (
            result.event.event_id
            if result.event is not None
            else str(raw.get("event_id") or "").strip()
        )
        if result.duplicate:
            return self._finish(event_id, result, raw, OUTCOME_DUPLICATE)
        if result.rejected:
            return self._finish(event_id, result, raw, OUTCOME_REJECTED)
        self._backfill_requested_severity(result.event, raw)
        raw_time = self._raw_time_text(result.event, raw)
        try:
            record_id = self.event_table.append(result.event, occurred_at_raw=raw_time)  # type: ignore[arg-type]
        except AitableWriteUnknown:
            self._pending[event_id] = PENDING_UNKNOWN
            outcome = self._finish(
                event_id, result, raw, OUTCOME_WRITE_UNKNOWN, write_unknown=True
            )
            return outcome
        except AitableRejected:
            self._pending[event_id] = PENDING_FAILED
            outcome = self._finish(
                event_id, result, raw, OUTCOME_WRITE_UNKNOWN, write_failed=True
            )
            return outcome
        self._pending.pop(event_id, None)
        # 补全后重新录入成功：清除该事件此前的 E001 拒收告警（不动本次的 E002/E003/E004 告警）。
        self._clear_reject_alarms(event_id, reason="补全时间后重新录入成功")
        return self._finish(event_id, result, raw, OUTCOME_STORED, record_id=record_id)

    def submit_all(self, rows: Iterable[Mapping[str, Any]]) -> tuple[IntakeOutcome, ...]:
        return tuple(self.submit(row) for row in rows)

    # ---- 受理不明处置 ---------------------------------------------------

    def pending_events(self) -> tuple[str, ...]:
        return tuple(sorted(self._pending))

    def reconcile(self, event_id: str) -> str:
        """回查落表结果（受理不明的唯一出路）：有行 → ``verified``；无行 → ``not_applied``。"""
        record_id = self.event_table.record_id_of(event_id)
        if record_id is None:
            self._pending[event_id] = PENDING_ABSENT
            return WritebackStatus.NOT_APPLIED.value
        self._pending.pop(event_id, None)
        return WritebackStatus.VERIFIED.value

    def retry_persist(self, event_id: str) -> str:
        """补写：**仅**在回查确认"未生效"（``not_applied``）或明确硬失败后允许。"""
        if self._pending.get(event_id) == PENDING_UNKNOWN:
            raise ContractError(
                WRITE_UNKNOWN,
                "上次落表受理不明：只回查（reconcile），不得重放",
                detail={"event_id": event_id},
            )
        state = self._pending.get(event_id)
        if state not in (PENDING_ABSENT, PENDING_FAILED):
            raise ContractViolation(
                f"事件 {event_id} 没有待补写的落表（状态: {state or '正常'}）：先回查再决定"
            )
        event = self.store.get(event_id)
        if event is None:
            raise ContractViolation(f"内存中没有事件 {event_id}，无法补写")
        record_id = self.event_table.append(event, occurred_at_raw=self._raw_time_from_journal(event))
        self._pending.pop(event_id, None)
        self._finish(event_id, IntakeResult(False, event), dict(event.to_dict()), OUTCOME_STORED,
                     record_id=record_id)
        return record_id

    # ---- 告警处置（补全/复核 + 清告警，留痕可恢复） ----------------------

    def clear_alarm(self, alarm_id: str, *, at: datetime | None = None, reason: str = "") -> Any:
        """清除单条告警并留痕（重启后可恢复“已清”状态）。"""
        alarm = self.ledger.get(alarm_id)
        if alarm is None:
            raise ContractViolation(f"本班次没有告警 {alarm_id!r}")
        moment = truncate_to_minute(at or self.clock())
        alarm.clear(moment)
        self._log_clear(alarm, moment, reason)
        return alarm

    def clear_event_alarms(
        self, event_id: str, *, at: datetime | None = None, reason: str = ""
    ) -> tuple[Any, ...]:
        """清除某事件名下全部未清告警（**必须**与补全/复核同步调用）。"""
        moment = truncate_to_minute(at or self.clock())
        cleared = self.ledger.clear_event(event_id, moment)
        for alarm in cleared:
            self._log_clear(alarm, moment, reason)
        return cleared

    def repair_time(
        self, event_id: str, occurred_at: Any, *, note: str = "", at: datetime | None = None
    ) -> HandoverEvent:
        """E002/E003 补全时间：改内存事件 → 更新表格行 → 清对应告警（含留痕）。"""
        event = self._require_event(event_id)
        event.resolve_time(occurred_at, note=note)
        self.event_table.update(event)
        self.clear_event_alarms(event_id, at=at, reason="补全发生时间")
        return event

    def review_out_of_range(
        self, event_id: str, *, note: str = "", at: datetime | None = None
    ) -> HandoverEvent:
        """E003 复核通过：确认越界属实并保留（时间不改）→ 清对应告警。"""
        event = self._require_event(event_id)
        event.mark_reviewed(note=note)
        self.event_table.update(event)
        self.clear_event_alarms(event_id, at=at, reason="越界复核通过")
        return event

    def fill_required(
        self,
        event_id: str,
        *,
        category: str | None = None,
        severity: str | None = None,
        status: str | None = None,
        note: str = "",
        at: datetime | None = None,
    ) -> HandoverEvent:
        """E004 补关键字段（类别/重要级/状态）→ 更新表格行 → 清对应告警。"""
        event = self._require_event(event_id)
        event.fill_required(category=category, severity=severity, status=status, note=note)
        self.event_table.update(event)
        self.clear_event_alarms(event_id, at=at, reason="补全关键字段")
        return event

    def _require_event(self, event_id: str) -> HandoverEvent:
        event = self.store.get(event_id)
        if event is None:
            raise ContractViolation(f"内存中没有事件 {event_id!r}（可能已被拒收或未录入）")
        return event

    @staticmethod
    def _raw_time_text(event: HandoverEvent | None, raw: Mapping[str, Any]) -> str | None:
        """告警态（时间不完整）时把输入行的原始时间文本原样落列，避免丢原始输入。"""
        if event is None or event.occurred_at is not None:
            return None
        text = raw.get("occurred_at")
        return None if text is None else str(text)

    def _raw_time_from_journal(self, event: HandoverEvent) -> str | None:
        """从输入留痕里取该事件的原始时间文本（补写落表时同样保留原样）。"""
        for entry in reversed(self.journal.entries()):
            if entry.event_id == event.event_id and entry.raw:
                return self._raw_time_text(event, entry.raw)
        return None

    def _backfill_requested_severity(
        self, event: HandoverEvent | None, raw: Mapping[str, Any]
    ) -> None:
        """原值留痕：``original_severity`` 记交班人**申报值**。

        契约的 intake 路径把 ``resolve_severity`` 的结果再传给 ``HandoverEvent``，
        导致强制升级时 ``original_severity`` 留痕成了强制后的值；本层以输入行里的
        申报值回填（契约缺口已记入 PR 待主控确认项，不改 ``contracts/``）。
        """
        if event is None or not event.severity_forced:
            return
        requested = optional_enum(Severity, raw.get("severity"), field="severity")
        if requested is not None and event.original_severity != requested.value:
            event.original_severity = requested.value

    def _clear_reject_alarms(
        self, event_id: str, *, at: datetime | None = None, reason: str = ""
    ) -> tuple[Any, ...]:
        """只清除该事件此前的 E001 拒收告警（补全重录场景），不动其它规则告警。"""
        moment = truncate_to_minute(at or self.clock())
        cleared = []
        for alarm in self.ledger.open_alarms():
            if alarm.rule == E001 and alarm.event_id == event_id:
                alarm.clear(moment)
                self._log_clear(alarm, moment, reason)
                cleared.append(alarm)
        return tuple(cleared)

    def _log_clear(self, alarm: Any, moment: datetime, reason: str) -> str:
        return self.alarm_journal.record(
            AlarmJournalEntry(
                alarm_id=alarm.alarm_id,
                shift_id=self.shift.shift_id,
                rule=alarm.rule,
                field=alarm.field,
                action=ACTION_CLEARED,
                event_id=alarm.event_id or "",
                detail=alarm.detail,
                created_at=format_minute(alarm.created_at),
                action_at=format_minute(moment),
                reason=reason,
            )
        )

    # ---- 交班提交前全表复验 ---------------------------------------------

    def verify_source_consistency(self) -> tuple[str, ...]:
        """内存事件集合 vs 交接事件表：必须一一对应（否则清单会与表不一致）。"""
        table_ids = set(self.event_table.event_ids(self.shift.shift_id))
        memory_ids = {event.event_id for event in self.store.events}
        problems: list[str] = []
        missing_in_table = sorted(memory_ids - table_ids)
        missing_in_memory = sorted(table_ids - memory_ids)
        if missing_in_table:
            problems.append(f"内存有但表里没有的事件: {', '.join(missing_in_table)}")
        if missing_in_memory:
            problems.append(f"表里有但内存没有的事件: {', '.join(missing_in_memory)}")
        if self._pending:
            problems.append(f"存在受理不明的落表: {', '.join(self.pending_events())}")
        return tuple(problems)

    def recheck(self) -> RecheckResult:
        """交班提交前全表复验：一致 → 无未清告警 → 才允许提交/出清单。"""
        problems = self.verify_source_consistency()
        if problems:
            raise ContractViolation("交班提交前全表复验失败：" + "；".join(problems))
        self.shift.suspend_for_alarms(self.ledger.has_open)
        sync_shift_status(self.shift, self.adapter)
        self.ledger.require_clear()  # 未清 → ALARM_NOT_CLEARED
        self.store.require_all_report_ready()  # 清态与补全不同步 → ContractViolation
        return RecheckResult(
            shift_id=self.shift.shift_id,
            event_total=self.store.event_count,
            event_complete=self.store.complete_count,
            stored_count=self.event_table.count(self.shift.shift_id),
            open_alarm_count=self.ledger.open_count,
            alarm_count=self.ledger.alarm_count,
            rejected_count=len(self.journal.rejected_entries(self.shift.shift_id)),
        )

    def submit_shift(self) -> ShiftRecord:
        """交班提交：复验通过后 draft → submitted，并把状态回写班次表。"""
        self.recheck()
        self.shift.transition(ShiftStatus.SUBMITTED, has_open_alarms=False)
        self.shift_table.set_status(self.shift.shift_id, self.shift.status)
        return self.shift

    # ---- 重启恢复 -------------------------------------------------------

    def restore(
        self,
        entries: Iterable[JournalEntry] | None = None,
    ) -> tuple[RestoreIssue, ...]:
        """重启恢复：先按留痕复验，再**以交接事件表为准**校正内存状态。

        顺序与依据：

        1. 输入留痕重放：E001 拒收的行重新得出"拒收不落表 + 告警"（表中没有这一行）；
        2. 已落表的行**以表格列值为准**重新复验（表格是权威状态列：
           补全/复核后的 ``completeness``、原报重要级都读表格）；
        3. 应用告警处置留痕：已清告警恢复为已清，不因重启回到告警态；
        4. 核对"表里有行 ↔ 留痕有据"，不一致只报问题，**不自动补写**。
        """
        items = (
            tuple(entries)
            if entries is not None
            else self.journal.entries(self.shift.shift_id)
        )
        rows = {
            event_id_from_fields(row.fields): row
            for row in self.event_table.list_rows(self.shift.shift_id)
        }
        stored_ids = {entry.event_id for entry in items if entry.outcome == OUTCOME_STORED}
        problems: list[RestoreIssue] = []
        for entry in items:
            if entry.outcome == OUTCOME_REJECTED:
                # E001 拒收：重放原始行 → 重新得出"拒收不落表"与告警
                self.store.intake(entry.raw)
                if entry.event_id in rows and entry.event_id not in stored_ids:
                    problems.append(
                        RestoreIssue(entry.event_id, "留痕为拒收，但交接事件表里却有这一行")
                    )
                continue
            if entry.outcome == OUTCOME_DUPLICATE:
                # 同事件ID的首次录入已建立该事件，不重复复验
                continue
            if entry.outcome == OUTCOME_STORED:
                row = rows.get(entry.event_id)
                if row is None:
                    problems.append(
                        RestoreIssue(entry.event_id, "留痕为已落表，但交接事件表里没有这一行")
                    )
                    continue
                self._revive_from_row(row.fields, problems)
                continue
            if entry.event_id not in rows:
                problems.append(
                    RestoreIssue(
                        entry.event_id,
                        "留痕为受理不明且表中无此行：需回查/人工处置，不自动补写",
                    )
                )
        covered = {
            entry.event_id
            for entry in items
            if entry.outcome in (OUTCOME_STORED, OUTCOME_DUPLICATE)
        }
        for event_id in sorted(set(rows) - covered):
            problems.append(
                RestoreIssue(event_id, "交接事件表里有这一行，但没有对应输入留痕（来源不明）")
            )
        problems.extend(self._apply_alarm_journal())
        return tuple(problems)

    def _revive_from_row(self, fields: Mapping[str, Any], problems: list[RestoreIssue]) -> None:
        """以表格列为准复验一行，并把内存事件的状态列校正到与表格一致。"""
        raw = event_raw_from_fields(fields)
        event_id = raw["event_id"]
        if self.store.get(event_id) is not None:
            return
        result = self.store.intake(raw)
        if result.rejected or result.event is None:
            problems.append(
                RestoreIssue(event_id, "按表格列复验却得出拒收：表格数据不完整，需人工处置")
            )
            return
        authoritative = event_from_fields(fields)
        event = result.event
        # 表格的 completeness / 原报重要级是权威留痕，覆盖复验得出的临时值。
        event.completeness = authoritative.completeness
        event.original_severity = authoritative.original_severity

    def _apply_alarm_journal(self) -> tuple[RestoreIssue, ...]:
        """按告警处置留痕恢复告警账本：已清的告警保持已清，历史告警数不丢。

        - 复验后仍存在的告警（如越界未改时间）→ 直接按留痕清掉；
        - 复验后不再产生的告警（如补全时间后不再越界/不完整）→ 按留痕**补回**
          历史告警记录并置为已清，保证 ``alarm_count`` 与重启前一致。
        """
        problems: list[RestoreIssue] = []
        for entry in self.alarm_journal.entries(self.shift.shift_id):
            moment = parse_occurred_at(entry.action_at).value or self.clock()
            alarm = self.ledger.get(entry.alarm_id)
            if alarm is None:
                created = parse_occurred_at(entry.created_at).value or moment
                alarm = CompletenessAlarm(
                    rule=entry.rule,
                    shift_id=self.shift.shift_id,
                    event_id=entry.event_id or None,
                    field=entry.field,
                    detail=entry.detail,
                    created_at=created,
                )
                self.ledger.record(alarm)
            if alarm.alarm_id != entry.alarm_id:  # pragma: no cover - 防御
                problems.append(
                    RestoreIssue(entry.event_id or entry.alarm_id, f"告警ID 与留痕不符: {entry.alarm_id}")
                )
                continue
            alarm.clear(moment)
        return tuple(problems)

    # ---- 查询 -----------------------------------------------------------

    @property
    def events(self) -> tuple[HandoverEvent, ...]:
        return self.store.events

    def stored_events(self) -> tuple[HandoverEvent, ...]:
        """以**交接事件表**为准读回的事件（清单一致性检查的基准）。"""
        return self.event_table.list_for_shift(self.shift.shift_id)

    def alarm_state(self) -> dict[str, Any]:
        return {
            "open": [alarm.to_dict() for alarm in self.ledger.open_alarms()],
            "all": [alarm.to_dict() for alarm in self.ledger.all_alarms()],
            "has_open": self.ledger.has_open,
        }

    # ---- 内部 -----------------------------------------------------------

    def _finish(
        self,
        event_id: str,
        result: IntakeResult,
        raw: Mapping[str, Any],
        outcome: str,
        *,
        record_id: str = "",
        write_unknown: bool = False,
        write_failed: bool = False,
    ) -> IntakeOutcome:
        journal_record_id = self._journal(event_id, outcome, raw, record_id)
        return IntakeOutcome(
            event_id=event_id,
            result=result,
            record_id=record_id,
            write_unknown=write_unknown,
            write_failed=write_failed,
            journal_record_id=journal_record_id,
        )

    def _journal(
        self, event_id: str, outcome: str, raw: Mapping[str, Any], record_id: str = ""
    ) -> str:
        return self.journal.record(
            JournalEntry(
                event_id=event_id,
                shift_id=self.shift.shift_id,
                outcome=outcome,
                raw={str(key): "" if value is None else str(value) for key, value in raw.items()},
                record_id=record_id,
                logged_at=format_minute(self.clock()),
            )
        )


__all__ = [
    "ALARM_NOT_CLEARED",
    "PENDING_ABSENT",
    "PENDING_FAILED",
    "PENDING_UNKNOWN",
    "IntakeOutcome",
    "RecheckResult",
    "RestoreIssue",
    "ShiftIntakeService",
]
