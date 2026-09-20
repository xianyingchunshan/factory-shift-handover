"""业务入口（演示/联调用）：跑一遍三段实现的离线闭环。

用法（仓库根目录）：

    python -B scripts/handover_demo.py            # 中文清单 + 关键步骤说明
    python -B scripts/handover_demo.py --quiet    # 只打印清单与汇总

数据全部为 **合成**（``SYNTH-`` 前缀），表格读写走内存合成适配器：
本脚本**不联网**、不访问任何真实钉钉/AI 表格接口，也不读取本机配置或凭据。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.errors import ContractError  # noqa: E402
from contracts.identity import IdentityRef  # noqa: E402
from contracts.shift import new_shift  # noqa: E402
from contracts.timebase import SHANGHAI  # noqa: E402

from integrations.aitable.synthetic import SyntheticAitableAdapter  # noqa: E402
from workflow import state as wfstate  # noqa: E402
from workflow.checklist import ShiftChecklistService  # noqa: E402
from workflow.intake import ShiftIntakeService  # noqa: E402
from workflow.todos import TodoAssignmentService  # noqa: E402

SHIFT_ID = "SYNTH-SHIFT-0001"
FROM = IdentityRef(source="dingtalk", user_id="SYNTH-uid-from", display_name="SYNTH-交班人A")
TO = IdentityRef(source="eam", user_id="SYNTH-uid-to", display_name="SYNTH-接班人B")
STRANGER = IdentityRef(source="ehr", user_id="SYNTH-uid-stranger", display_name="SYNTH-无关人C")
STAMP = datetime(2026, 1, 2, 20, 0, tzinfo=SHANGHAI)


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_force_utf8_stdio()


def clock() -> datetime:
    return STAMP


def build_shift():
    return new_shift(
        shift_id=SHIFT_ID,
        handover_line="SYNTH-LINE-A",
        shift_date=date(2026, 1, 2),
        shift_name="early",
        start_time=datetime(2026, 1, 2, 8, 0, tzinfo=SHANGHAI),
        end_time=STAMP,
        handover_from=FROM,
        handover_to=TO,
        critical_standard=("重大事项", "移交接班人"),
    )


def row(event_id: str, **overrides) -> dict:
    data = {
        "event_id": event_id,
        "shift_id": SHIFT_ID,
        "category": "equipment",
        "description": f"SYNTH 记录 {event_id}",
        "occurred_at": "2026-01-02 09:30",
        "severity": "normal",
        "status": "done",
        "ref_no": "SYNTH-REF-001",
        "note": "",
    }
    data.update(overrides)
    return data


#: 演示输入：清一色合成数据，分别触发四种告警与一次关键级强制升级。
INTAKE_ROWS = (
    row("SYNTH-EVT-0001", category="runtime", occurred_at="2026-01-02 08:10"),
    row("SYNTH-EVT-0002", occurred_at="2026-01-02"),  # E002 时间不完整
    row("SYNTH-EVT-0003", occurred_at="2026-01-02 23:00"),  # E003 越出班次窗口
    row("SYNTH-EVT-0004", category="重大事项", status="done", severity="normal"),  # 强制升级
    row("SYNTH-EVT-0005", status="in_progress", occurred_at="2026-01-02 10:00"),  # 未完移交
    row("SYNTH-EVT-0006", occurred_at=None),  # E001 拒收，不落表
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="三段实现离线闭环演示（合成数据）")
    parser.add_argument("--quiet", action="store_true", help="只打印清单与汇总")
    args = parser.parse_args(argv)

    def say(line: str) -> None:
        if not args.quiet:
            print(line)

    adapter = SyntheticAitableAdapter()
    shift = build_shift()
    intake = ShiftIntakeService(shift, adapter, clock=clock)
    intake.create_shift()
    say(f"[1/4] 班次已建：{SHIFT_ID}（合成适配器，无网络）")

    for record in INTAKE_ROWS:
        outcome = intake.submit(record)
        say(
            "  录入 {id}: {state} 告警={alarms} 完整度={completeness}".format(
                id=record["event_id"],
                state=(
                    "拒收（不落表）"
                    if outcome.rejected
                    else ("已落表" if outcome.stored else "未落表")
                ),
                alarms=[alarm.rule for alarm in outcome.alarms] or "-",
                completeness=(outcome.event.completeness if outcome.event else "-"),
            )
        )
    say(
        f"  交接事件表 {intake.event_table.count()} 行；"
        f"告警 {intake.ledger.counts_by_rule()}；未清 {intake.ledger.open_count} 条"
    )

    checklist = ShiftChecklistService(shift, intake.store, intake.ledger, adapter, clock=clock)
    try:
        checklist.generate()
    except ContractError as exc:
        say(f"[2/4] 告警未清 → 清单被拦：{exc.code}（班次状态 {shift.status}）")

    # 补全 / 复核 / 重录：告警与补全必须同步，清除留痕可恢复。
    intake.repair_time("SYNTH-EVT-0002", "2026-01-02 10:05")
    intake.review_out_of_range("SYNTH-EVT-0003")
    intake.submit(row("SYNTH-EVT-0006", occurred_at="2026-01-02 11:00"))
    say(
        f"[3/4] 补全/复核/重录完成；未清告警 {intake.ledger.open_count} 条，"
        f"清除留痕 {len(intake.alarm_journal.entries())} 条"
    )

    todos = TodoAssignmentService(adapter, shift, clock=clock)
    report = checklist.generate(todo_id_factory=todos.todo_id_factory)
    todos.create_plan(report)
    consistency = checklist.verify(report)
    say(f"  一致性校验：{'通过' if consistency.ok else '不通过'}（问题 {len(consistency.problems)} 项）")
    print("\n".join(checklist.render(report)))
    print()

    try:
        todos.confirm("SYNTH-EVT-0004", STRANGER)
    except ContractError as exc:
        say(f"[4/4] 错人确认被拒：{exc.code}")
    try:
        todos.confirm("SYNTH-EVT-0004", TO, allow_missing_readback=True)
    except ContractError as exc:
        say(f"      无回读确认被拒：{exc.code}")
    say(f"      关键级逐项确认：{todos.confirm('SYNTH-EVT-0004', TO).status}")
    say(f"      回写结果：{todos.run_writeback('SYNTH-EVT-0004').to_dict()}")
    todos.confirm(SHIFT_ID, TO)
    todos.run_writeback(SHIFT_ID, simulate="unknown")
    try:
        todos.run_writeback(SHIFT_ID)
    except ContractError as exc:
        say(f"      受理不明只回查不重放：{exc.code}")
    todos.recheck_unit(SHIFT_ID, applied=False)
    todos.run_writeback(SHIFT_ID)
    say(f"      一般事务整单：{todos.summary()}")

    # 重启：快照 → 新适配器 → 重建（不重写表格、不重放外部写入）。
    payload = wfstate.dump_tables(adapter)
    bundle = wfstate.restart(
        wfstate.loads(wfstate.dumps(payload)), adapter=SyntheticAitableAdapter(),
        shift_id=SHIFT_ID, clock=clock,
    )
    say(
        f"      重启恢复：问题 {len(bundle.issues)} 项；班次 {bundle.shift.status}；"
        f"事件 {len(bundle.intake.events)} 条；未清告警 {bundle.intake.ledger.open_count} 条"
    )
    restored = bundle.checklist().generate(todo_id_factory=lambda unit: "")
    same = [event.event_id for event in restored.event_flow] == [
        event.event_id for event in report.event_flow
    ]
    say(f"      重启后清单与重启前一致：{same}")
    print("三段实现离线闭环完成（合成适配器；无任何真实外部调用）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
