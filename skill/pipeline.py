"""T06 skill 过程与输出（issue #19 第 2/3 段）：合成 AI 表格读写 → 清单输出。

过程（第 2 段，全部落在**合成适配器**的内存表上，无网络、无真实钉钉/AI表格调用）：

1. 建成班次行（``create_shift``，同班同线重复建单即 ``DUPLICATE_SHIFT``，不建第二单）；
2. 逐条录入事件行（``submit``）：E001 拒收**不落表**，E002/E003/E004 落表 + 告警；
3. 交班提交前全表复验（``recheck``）：未清告警 → ``ALARM_NOT_CLEARED``（班次置 ``blocked``）。

输出（第 3 段）：复验通过才提交并生成清单——标题走
:func:`workflow.checklist.checklist_title`（驻场期单 = 「交接班清单 · 驻场期」），
五段渲染与契约同源（``render_document`` = 标题行 + 契约五段），另落一份确定性
``result.json`` 与表格快照 ``state.json``（可 ``--resume`` 重放）。

确定性：时钟取输入 ``now``（默认驻场期结束时刻），不使用墙钟；同样的输入跑两次
产出逐字一致（``result.json`` / ``checklist.txt`` / ``state.json``）。重放（``run_resume``）
只读库中已有数据，**不重写表格、不重放外部写入、不重复创建待办**。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from contracts.enums import ShiftName, label_of
from contracts.errors import ALARM_NOT_CLEARED, ContractError, ContractViolation
from contracts.report import HandoverReport
from contracts.shift import ShiftRecord
from contracts.timebase import truncate_to_minute

from integrations.aitable.adapter import AitableAdapter
from integrations.aitable.synthetic import SyntheticAitableAdapter, assert_synthetic
from integrations.aitable.tables import (
    AlarmJournal,
    EventTable,
    IntakeJournal,
    ShiftTable,
    TODO_COLUMNS,
    TodoTable,
)
from workflow import state as wfstate
from workflow.checklist import ShiftChecklistService, stay_period_facts
from workflow.intake import IntakeOutcome, ShiftIntakeService
from workflow.todos import TodoAssignmentService

from .config import SKILL_NAME, SKILL_VERSION, SkillInput, SkillInputError

#: 退出码（SKILL.md 有对照表）。
EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_INPUT_ERROR = 3

#: 结果口径。
OUTCOME_CHECKLIST = "checklist"
OUTCOME_BLOCKED = "blocked"

MODE_FRESH = "fresh"
MODE_RESUME = "resume"

#: 随卡口径（写进 result.json，便于验收逐条比对）。
PROTOCOL_NOTES: tuple[str, ...] = (
    "默认班次名=驻场期（stay_period）；custom 等保留给历史数据与例外",
    "清单标题走 workflow.checklist.checklist_title()；驻场期单=「交接班清单 · 驻场期」",
    "驻场期边界=start_time/end_time，E003 对整体区间判定（T04 冻结口径，不另立一套）",
    "建单 shift_date 取驻场期起始日（start_time 的日期）",
    "告警未清不出清单（E001 拒收不落表；E002–E004 落表告警、未清禁提交/出清单）",
    "全部读写走合成适配器：不联网、不接真实钉钉/AI表格；真实联调属 L4，由主控执行",
)


def fixed_clock(moment: datetime):
    """确定性时钟：同一输入永远得到同一时刻（不用墙钟）。"""

    def now() -> datetime:
        return moment

    return now


@dataclass
class SkillResult:
    """一次 skill 运行的完整结果（可序列化为 ``result.json``，可写盘）。"""

    mode: str
    outcome: str
    source: str
    shift: ShiftRecord
    now: datetime
    title: str
    facts: dict[str, Any]
    adapter: AitableAdapter
    intake: ShiftIntakeService | None = None
    outcomes: tuple[IntakeOutcome, ...] = ()
    report: HandoverReport | None = None
    checklist_lines: tuple[str, ...] = ()
    blocked: dict[str, Any] | None = None
    recheck: dict[str, Any] | None = None
    consistency: dict[str, Any] | None = None
    restore_issues: tuple[Any, ...] = ()

    # ---- 基本属性 -------------------------------------------------------

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.outcome == OUTCOME_CHECKLIST else EXIT_BLOCKED

    @property
    def checklist_text(self) -> str:
        return "\n".join(self.checklist_lines) + "\n" if self.checklist_lines else ""

    def checklist_sha256(self) -> str:
        return hashlib.sha256(self.checklist_text.encode("utf-8")).hexdigest()

    def tables(self) -> dict[str, int]:
        """六张表的行数（班次 / 交接事件 / 输入留痕 / 告警留痕 / 待办）。"""
        shift_id = self.shift.shift_id
        return {
            "shift": ShiftTable(self.adapter).count(),
            "events": EventTable(self.adapter).count(shift_id),
            "events_all": EventTable(self.adapter).count(),
            "intake_journal": IntakeJournal(self.adapter).count(),
            "alarm_journal": AlarmJournal(self.adapter).count(),
            "todos": TodoTable(self.adapter).count(shift_id),
        }

    def ledger(self):
        return self.intake.ledger

    # ---- 序列化 ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """确定性结果载荷（``sort_keys=True`` 输出，逐字可 diff）。"""
        payload: dict[str, Any] = {
            "skill": {"name": SKILL_NAME, "version": SKILL_VERSION},
            "mode": self.mode,
            "source": self.source,
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "shift": {
                **self.facts,
                "shift_date": self.shift.shift_date.isoformat(),
                "status": str(self.shift.status),
                "is_blocked": self.shift.is_blocked,
            },
            "title": self.title,
            "alarms": self._alarms_dict(),
            "tables": self.tables(),
            "checklist": (
                {
                    "lines": len(self.checklist_lines),
                    "first_line": self.checklist_lines[0],
                    "sha256": self.checklist_sha256(),
                }
                if self.checklist_lines
                else None
            ),
            "intake": self._intake_dict(),
            "recheck": self.recheck,
            "consistency": self.consistency,
            "blocked": self.blocked,
            "restore_issues": [
                issue.to_dict() if hasattr(issue, "to_dict") else str(issue)
                for issue in self.restore_issues
            ],
            "protocol": list(PROTOCOL_NOTES),
        }
        if self.report is not None:
            payload["summary"] = {
                **dict(self.report.completeness_summary),
                "critical_items": len(self.report.critical_items),
                "pending_transfers": len(self.report.pending_transfers),
                "confirmation_units": len(self.report.confirmation_area),
            }
        else:
            payload["summary"] = None
        return _jsonable(payload)

    def write(self, out_dir: str | Path) -> tuple[Path, ...]:
        """落输出文件：``checklist.txt``（仅在出清单时）、``result.json``、``state.json``。"""
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        if self.checklist_lines:
            path = target / "checklist.txt"
            path.write_text(self.checklist_text, encoding="utf-8")
            written.append(path)
        path = target / "result.json"
        path.write_text(_json_text(self.to_dict()), encoding="utf-8")
        written.append(path)
        path = target / "state.json"
        path.write_text(wfstate.dumps(wfstate.dump_tables(self.adapter)), encoding="utf-8")
        written.append(path)
        return tuple(written)

    # ---- 内部 -----------------------------------------------------------

    def _alarms_dict(self) -> dict[str, Any]:
        ledger = self.intake.ledger
        return {
            "counts_by_rule": {rule: int(count) for rule, count in ledger.counts_by_rule().items()},
            "open_count": ledger.open_count,
            "alarm_count": ledger.alarm_count,
            "all": [alarm.to_dict() for alarm in ledger.all_alarms()],
        }

    def _intake_dict(self) -> dict[str, Any] | None:
        if self.mode != MODE_FRESH:
            return None
        outcomes = self.outcomes
        return {
            "submitted": len(outcomes),
            "stored": sum(1 for item in outcomes if item.stored),
            "rejected": sum(1 for item in outcomes if item.rejected),
            "duplicate": sum(1 for item in outcomes if item.duplicate),
            "pending_write": sum(1 for item in outcomes if item.pending_write),
            "outcomes": [
                {
                    "event_id": item.event_id,
                    "stored": item.stored,
                    "rejected": item.rejected,
                    "duplicate": item.duplicate,
                    "record_id": item.record_id,
                    "alarms": [alarm.rule for alarm in item.alarms],
                }
                for item in outcomes
            ],
        }


#: 记录服务实例经 ``build_result`` 一并装配（见其结果字段）。


def run(spec: SkillInput, *, adapter: AitableAdapter | None = None) -> SkillResult:
    """新单闭环：输入校验后的 ``spec`` → 合成表格读写 → 清单（或告警拦截）。"""
    target = SyntheticAitableAdapter() if adapter is None else adapter
    assert_synthetic(target)  # 只允许合成/离线适配器（真实读写属 L4）
    clock = fixed_clock(spec.now)
    intake = ShiftIntakeService(spec.shift, target, clock=clock)
    intake.create_shift()
    outcomes = _submit_all(intake, spec.events)
    checklist = ShiftChecklistService(spec.shift, intake.store, intake.ledger, target, clock=clock)
    todos = TodoAssignmentService(target, spec.shift, clock=clock)

    try:
        recheck = _recheck(intake)
    except ContractError as exc:
        if exc.code != ALARM_NOT_CLEARED:
            raise
        return _blocked_result(
            mode=MODE_FRESH,
            source=spec.source,
            shift=spec.shift,
            now=spec.now,
            checklist=checklist,
            adapter=target,
            intake=intake,
            outcomes=outcomes,
        )

    intake.submit_shift()  # 复验通过才提交：draft → submitted 并回写班次表
    report = checklist.generate(todo_id_factory=todos.todo_id_factory)
    todos.create_plan(report)
    consistency = checklist.verify(report)
    if not consistency.ok:  # generate 已自检，这里防御性再确认
        raise ContractViolation("清单一致性自检失败：" + "；".join(consistency.problems))
    return _checklist_result(
        mode=MODE_FRESH,
        source=spec.source,
        shift=spec.shift,
        now=spec.now,
        title=checklist.title,
        adapter=target,
        intake=intake,
        outcomes=outcomes,
        report=report,
        lines=tuple(checklist.render_document(report)),
        recheck=recheck.to_dict(),
        consistency=consistency.to_dict(),
    )


def run_resume(
    payload: Mapping[str, Any],
    *,
    shift_id: str,
    now: datetime,
    adapter: AitableAdapter | None = None,
    source: str = "",
) -> SkillResult:
    """重启/重复拉取的重放：从 ``state.json`` 快照重建，只读重出清单。

    - 不重写表格、不重放外部写入、不重复创建待办（确认区按库中已有待办回填）；
    - 恢复期发现的不一致只记录在 ``restore_issues``，不自动补写。
    """
    target = SyntheticAitableAdapter() if adapter is None else adapter
    assert_synthetic(target)
    clock = fixed_clock(truncate_to_minute(now))
    bundle = wfstate.restart(payload, adapter=target, shift_id=shift_id, clock=clock)
    checklist = bundle.checklist(clock=clock)
    try:
        _recheck(bundle.intake)
    except ContractError as exc:
        if exc.code != ALARM_NOT_CLEARED:
            raise
        return _blocked_result(
            mode=MODE_RESUME,
            source=source,
            shift=bundle.shift,
            now=now,
            checklist=checklist,
            adapter=target,
            intake=bundle.intake,
            outcomes=(),
            issues=bundle.issues,
        )
    report = checklist.generate(todo_id_factory=board_todo_factory(target))
    consistency = checklist.verify(report)
    if not consistency.ok:  # pragma: no cover - 防御
        raise ContractViolation("重放清单一致性自检失败：" + "；".join(consistency.problems))
    return _checklist_result(
        mode=MODE_RESUME,
        source=source,
        shift=bundle.shift,
        now=now,
        title=checklist.title,
        adapter=target,
        intake=bundle.intake,
        outcomes=(),
        report=report,
        lines=tuple(checklist.render_document(report)),
        recheck=None,
        consistency=consistency.to_dict(),
        restore_issues=bundle.issues,
    )


# ---- 装配 ---------------------------------------------------------------


def build_result(
    *,
    mode: str,
    outcome: str,
    source: str,
    shift: ShiftRecord,
    now: datetime,
    title: str,
    adapter: AitableAdapter,
    intake: ShiftIntakeService,
    **extra: Any,
) -> SkillResult:
    """统一装配结果对象（记录服务实例一并带上，供结果序列化取告警账本）。"""
    return SkillResult(
        mode=mode,
        outcome=outcome,
        source=source,
        shift=shift,
        now=now,
        title=title,
        facts=stay_period_facts(shift),
        adapter=adapter,
        intake=intake,
        **extra,
    )


def _checklist_result(**kwargs: Any) -> SkillResult:
    lines = kwargs.pop("lines")
    return build_result(outcome=OUTCOME_CHECKLIST, checklist_lines=lines, **kwargs)


def _blocked_result(*, checklist: ShiftChecklistService, outcomes, issues=(), **kwargs: Any) -> SkillResult:
    # 被拦也不改口径：title 仍走 checklist_title（只是这次不出清单文件）。
    kwargs.setdefault("title", checklist.title)
    blocked = checklist.blocked_summary()
    return build_result(
        outcome=OUTCOME_BLOCKED,
        blocked=blocked,
        restore_issues=tuple(issues),
        outcomes=tuple(outcomes),
        **kwargs,
    )


def _submit_all(intake: ShiftIntakeService, rows) -> tuple[IntakeOutcome, ...]:
    outcomes: list[IntakeOutcome] = []
    for row in rows:
        try:
            outcomes.append(intake.submit(row))
        except ContractViolation as exc:
            # 前置校验已拦下调用方缺陷；仍出现即按输入非法处置（不写任何输出文件）。
            raise SkillInputError(f"录入行被拒绝（调用方缺陷）：{exc}") from exc
    return tuple(outcomes)


def _recheck(intake: ShiftIntakeService):
    """交班提交前全表复验；未清告警 → ``ALARM_NOT_CLEARED``。"""
    return intake.recheck()


def board_todo_factory(adapter: AitableAdapter):
    """只读取数工厂：确认单元的待办 ID 从库里已有行回填，**不新建、不重发**。"""
    board = TodoTable(adapter)

    def factory(unit: Any) -> str:
        row = board.find_row(unit.unit_id)
        if row is None:
            return ""
        return str(row.fields.get(TODO_COLUMNS["todo_id"]) or row.record_id)

    return factory


def _json_text(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _jsonable(value: Any) -> Any:
    """把结果载荷收敛成 JSON 可序列化形态（留痕行等对象走 ``to_dict``/``to_fields``）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    for attribute in ("to_dict", "to_fields"):
        method = getattr(value, attribute, None)
        if callable(method):
            return _jsonable(method())
    return str(value)


def shift_label(shift: ShiftRecord) -> str:
    """班次名的中文标签（用于 CLI 打印，与清单标题同源）。"""
    return label_of(ShiftName, shift.shift_name)


__all__ = [
    "EXIT_BLOCKED",
    "EXIT_INPUT_ERROR",
    "EXIT_OK",
    "MODE_FRESH",
    "MODE_RESUME",
    "OUTCOME_BLOCKED",
    "OUTCOME_CHECKLIST",
    "PROTOCOL_NOTES",
    "SkillResult",
    "board_todo_factory",
    "build_result",
    "fixed_clock",
    "run",
    "run_resume",
    "shift_label",
]
