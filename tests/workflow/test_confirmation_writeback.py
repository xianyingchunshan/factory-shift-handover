"""待办与回写状态机测试（issue #9 第 3 段）。

验收 L1/L2：关键级逐项 / 一般事务整单两种待办粒度、错人不放行、
无回读不得标确认、未确认不得回写、受理不明只回查不重放、
确认前状态不变、状态机全部守卫路径。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.confirmation import Confirmation, ReadbackEvidence  # noqa: E402
from contracts.enums import (  # noqa: E402
    ConfirmationScope,
    ConfirmationStatus,
    WritebackStatus,
)
from contracts.errors import (  # noqa: E402
    AUTH_REQUIRED,
    NOT_CONFIRMED,
    WRITE_UNKNOWN,
    ContractViolation,
)

from integrations.aitable.tables import TODO_TABLE  # noqa: E402

import wf_synth as synth  # noqa: E402


def plan_flow():
    """出清单 + 建待办 + 登记待确认（关键级 1 项 / 一般事务整单 1 项）。"""
    flow = synth.clean_flow()
    report = synth.report_of(flow)
    flow.todos.create_plan(report)
    return flow, report


class TestTodoGranularity(unittest.TestCase):
    def test_units_match_critical_and_normal_split(self):
        flow, report = plan_flow()
        units = report.confirmation_area
        self.assertEqual(
            [(unit.unit_id, unit.scope) for unit in units],
            [("SYNTH-EVT-0003", ConfirmationScope.EVENT.value), (synth.SHIFT_ID, ConfirmationScope.SHIFT.value)],
        )
        self.assertTrue(all(unit.has_todo for unit in units))
        self.assertTrue(all(unit.assignee.same_person(synth.TO) for unit in units))
        self.assertEqual(
            [(record.unit_id, record.scope) for record in flow.todos.confirmations],
            [(unit.unit_id, unit.scope) for unit in units],
        )

    def test_multiple_critical_events_get_individual_todos(self):
        flow = synth.make_flow()
        flow.submit(
            [
                synth.row("SYNTH-EVT-0001", category="重大事项", occurred_at="2026-01-02 09:00"),
                synth.row(
                    "SYNTH-EVT-0002",
                    category="equipment",
                    status="移交接班人",
                    occurred_at="2026-01-02 10:00",
                ),
            ]
        )
        report = synth.report_of(flow)
        flow.todos.create_plan(report)
        event_units = [unit for unit in report.confirmation_area if unit.scope == "event"]
        self.assertEqual([unit.unit_id for unit in event_units], ["SYNTH-EVT-0001", "SYNTH-EVT-0002"])
        self.assertEqual(len(report.confirmation_area), 2, "没有一般事项就不发整单待办")

    def test_normal_only_flow_has_single_shift_todo(self):
        flow = synth.make_flow()
        flow.submit([synth.row("SYNTH-EVT-0001", occurred_at="2026-01-02 09:00")])
        report = synth.report_of(flow)
        flow.todos.create_plan(report)
        self.assertEqual(
            [(unit.unit_id, unit.scope) for unit in report.confirmation_area],
            [(synth.SHIFT_ID, "shift")],
        )

    def test_creating_the_plan_twice_does_not_duplicate_todos(self):
        flow, report = plan_flow()
        before = flow.adapter.row_count(TODO_TABLE)
        flow.todos.create_plan(report)
        self.assertEqual(flow.adapter.row_count(TODO_TABLE), before)
        self.assertEqual(len(flow.todos.confirmations), 2)

    def test_units_without_todo_cannot_be_registered(self):
        flow = synth.make_flow()
        flow.submit([synth.row("SYNTH-EVT-0001", occurred_at="2026-01-02 09:00")])
        report = flow.checklist.generate()  # 不传 todo_id_factory → 确认区无待办
        with self.assertRaises(ContractViolation):
            flow.todos.attach(report)


class TestConfirmGuards(unittest.TestCase):
    def test_wrong_person_is_refused(self):
        flow, _ = plan_flow()
        with synth.expect_code(AUTH_REQUIRED):
            flow.todos.confirm("SYNTH-EVT-0003", synth.STRANGER)
        self.assertTrue(flow.todos.get("SYNTH-EVT-0003").is_pending)

    def test_missing_identity_is_refused(self):
        flow, _ = plan_flow()
        with synth.expect_code(AUTH_REQUIRED):
            flow.todos.confirm("SYNTH-EVT-0003", None)
        with synth.expect_code(AUTH_REQUIRED):
            flow.todos.confirm("SYNTH-EVT-0003", "   ")
        self.assertTrue(flow.todos.get("SYNTH-EVT-0003").is_pending)

    def test_standalone_operator_check(self):
        flow, _ = plan_flow()
        operator = flow.todos.require_operator("SYNTH-EVT-0003", synth.TO)
        self.assertTrue(operator.same_person(synth.TO))
        with synth.expect_code(AUTH_REQUIRED):
            flow.todos.require_operator("SYNTH-EVT-0003", synth.STRANGER)

    def test_confirm_without_readback_is_write_unknown(self):
        flow, _ = plan_flow()
        with synth.expect_code(WRITE_UNKNOWN):
            flow.todos.confirm("SYNTH-EVT-0003", synth.TO, allow_missing_readback=True)
        record = flow.todos.get("SYNTH-EVT-0003")
        self.assertTrue(record.is_pending)
        self.assertIsNone(record.confirmed_by)
        self.assertIsNone(record.confirmed_at)

    def test_confirm_with_readback_reporting_not_applied_is_write_unknown(self):
        flow, _ = plan_flow()
        evidence = flow.todos.readback("SYNTH-EVT-0003", applied=False)
        with synth.expect_code(WRITE_UNKNOWN):
            flow.todos.confirm("SYNTH-EVT-0003", synth.TO, readback=evidence)
        self.assertTrue(flow.todos.get("SYNTH-EVT-0003").is_pending)

    def test_contract_level_history_guard_on_missing_readback(self):
        """契约层守卫：无回读不得标 confirmed（本服务的 readback 会兜住正常路径）。"""
        flow, _ = plan_flow()
        record = flow.todos.get("SYNTH-EVT-0003")
        with synth.expect_code(WRITE_UNKNOWN):
            record.mark_confirmed(synth.TO, at=synth.STAMP, readback=None)
        self.assertTrue(record.is_pending)

    def test_confirm_records_operator_and_time(self):
        flow, _ = plan_flow()
        record = flow.todos.confirm("SYNTH-EVT-0003", synth.TO, note="SYNTH 接班确认")
        self.assertTrue(record.is_confirmed)
        self.assertEqual(record.status, ConfirmationStatus.CONFIRMED.value)
        self.assertTrue(record.confirmed_by.same_person(synth.TO))
        self.assertEqual(record.confirmed_at, synth.STAMP)
        self.assertEqual(record.notes, ["SYNTH 接班确认"])
        stored = flow.board_row("SYNTH-EVT-0003")
        self.assertEqual(stored["确认状态"], "confirmed")
        self.assertEqual(stored["确认人"], "eam:SYNTH-uid-to")
        self.assertEqual(stored["确认时间"], "2026-01-02 20:00")

    def test_double_confirm_is_idempotent(self):
        flow, _ = plan_flow()
        first = flow.todos.confirm("SYNTH-EVT-0003", synth.TO)
        confirmed_at = first.confirmed_at
        again = flow.todos.confirm("SYNTH-EVT-0003", synth.TO)
        self.assertTrue(again.is_confirmed)
        self.assertEqual(again.confirmed_at, confirmed_at)

    def test_unknown_unit_is_refused(self):
        flow, _ = plan_flow()
        with self.assertRaises(ContractViolation):
            flow.todos.get("SYNTH-EVT-9999")


class TestStateUnchangedBeforeConfirmation(unittest.TestCase):
    """确认前状态不变：事件行与班次行在确认前必须原样。"""

    def test_rows_are_untouched_until_confirmation(self):
        flow, report = plan_flow()
        events_before = flow.event_rows()
        shift_before = flow.shift_row()
        board_before = flow.board_row("SYNTH-EVT-0003")
        self.assertEqual(board_before["确认状态"], "pending")
        self.assertEqual(board_before["确认人"], "")
        self.assertEqual(board_before["回写状态"], WritebackStatus.NOT_SENT.value)

        flow.todos.confirm("SYNTH-EVT-0003", synth.TO)

        self.assertEqual(flow.event_rows(), events_before)
        self.assertEqual(flow.shift_row(), shift_before)
        self.assertEqual(flow.todos.get(synth.SHIFT_ID).status, ConfirmationStatus.PENDING.value)

    def test_unconfirmed_units_stay_pending_in_summary(self):
        flow, _ = plan_flow()
        flow.todos.confirm("SYNTH-EVT-0003", synth.TO)
        summary = flow.todos.summary()
        self.assertEqual(summary["units"], 2)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(summary["written_back"], 0)
        self.assertEqual(summary["todos"], 2)


class TestWritebackStateMachine(unittest.TestCase):
    def test_writeback_before_confirmation_is_refused(self):
        flow, _ = plan_flow()
        with synth.expect_code(NOT_CONFIRMED):
            flow.todos.writeback("SYNTH-EVT-0003", WritebackStatus.VERIFIED.value)
        self.assertTrue(flow.todos.get("SYNTH-EVT-0003").is_pending)

    def test_verified_writeback_requires_readback(self):
        """契约层守卫：标 verified 必须带回读，否则结果不明。"""
        flow, _ = plan_flow()
        record = flow.todos.get("SYNTH-EVT-0003")
        record.mark_confirmed(synth.TO, at=synth.STAMP, readback=flow.todos.readback("SYNTH-EVT-0003"))
        with synth.expect_code(WRITE_UNKNOWN):
            record.record_writeback(WritebackStatus.VERIFIED.value, readback=None)
        self.assertFalse(record.is_written_back)

    def test_run_writeback_marks_written_back_with_readback(self):
        flow, _ = plan_flow()
        flow.todos.confirm("SYNTH-EVT-0003", synth.TO)
        outcome = flow.todos.run_writeback("SYNTH-EVT-0003")
        self.assertEqual(outcome.status, ConfirmationStatus.WRITTEN_BACK.value)
        self.assertEqual(outcome.writeback_status, WritebackStatus.VERIFIED.value)
        self.assertTrue(outcome.readback_applied)
        stored = flow.board_row("SYNTH-EVT-0003")
        self.assertEqual(stored["确认状态"], "written_back")
        self.assertEqual(stored["回写状态"], "verified")

    def test_run_writeback_before_confirmation_is_guarded(self):
        flow, _ = plan_flow()
        with synth.expect_code(NOT_CONFIRMED):
            flow.todos.run_writeback("SYNTH-EVT-0003")

    def test_unknown_writeback_blocks_replay_and_needs_recheck(self):
        flow, _ = plan_flow()
        flow.todos.confirm("SYNTH-EVT-0003", synth.TO)
        outcome = flow.todos.run_writeback("SYNTH-EVT-0003", simulate="unknown")
        self.assertTrue(outcome.needs_recheck)
        record = flow.todos.get("SYNTH-EVT-0003")
        self.assertEqual(record.writeback_status, WritebackStatus.UNKNOWN.value)
        self.assertTrue(record.is_confirmed, "结果不明不等于未确认")
        with synth.expect_code(WRITE_UNKNOWN):
            flow.todos.run_writeback("SYNTH-EVT-0003")
        with synth.expect_code(WRITE_UNKNOWN):
            flow.todos.guard_resend("SYNTH-EVT-0003")
        self.assertEqual([record.unit_id for record in flow.todos.needs_recheck()], ["SYNTH-EVT-0003"])

    def test_recheck_applied_finishes_the_unit(self):
        flow, _ = plan_flow()
        flow.todos.confirm("SYNTH-EVT-0003", synth.TO)
        flow.todos.run_writeback("SYNTH-EVT-0003", simulate="unknown")
        outcome = flow.todos.recheck_unit("SYNTH-EVT-0003", applied=True)
        self.assertEqual(outcome.status, ConfirmationStatus.WRITTEN_BACK.value)
        self.assertEqual(outcome.writeback_status, WritebackStatus.VERIFIED.value)
        self.assertFalse(outcome.needs_recheck)

    def test_recheck_not_applied_keeps_confirmed(self):
        flow, _ = plan_flow()
        flow.todos.confirm("SYNTH-EVT-0003", synth.TO)
        flow.todos.run_writeback("SYNTH-EVT-0003", simulate="unknown")
        outcome = flow.todos.recheck_unit("SYNTH-EVT-0003", applied=False)
        self.assertEqual(outcome.status, ConfirmationStatus.CONFIRMED.value)
        self.assertEqual(outcome.writeback_status, WritebackStatus.NOT_APPLIED.value)
        self.assertFalse(outcome.needs_recheck)
        self.assertTrue(flow.todos.get("SYNTH-EVT-0003").is_confirmed)

    def test_recheck_after_not_applied_can_replay_once(self):
        flow, _ = plan_flow()
        flow.todos.confirm("SYNTH-EVT-0003", synth.TO)
        flow.todos.run_writeback("SYNTH-EVT-0003", simulate="unknown")
        flow.todos.recheck_unit("SYNTH-EVT-0003", applied=False)
        outcome = flow.todos.run_writeback("SYNTH-EVT-0003")
        self.assertEqual(outcome.status, ConfirmationStatus.WRITTEN_BACK.value)

    def test_recheck_on_a_healthy_unit_is_refused(self):
        flow, _ = plan_flow()
        flow.todos.confirm("SYNTH-EVT-0003", synth.TO)
        with self.assertRaises(ContractViolation):
            flow.todos.recheck_unit("SYNTH-EVT-0003", applied=True)

    def test_finished_unit_accepts_no_further_changes(self):
        flow, _ = plan_flow()
        flow.todos.confirm("SYNTH-EVT-0003", synth.TO)
        flow.todos.run_writeback("SYNTH-EVT-0003")
        with self.assertRaises(ContractViolation):
            flow.todos.confirm("SYNTH-EVT-0003", synth.TO)
        with self.assertRaises(ContractViolation):
            flow.todos.writeback("SYNTH-EVT-0003", WritebackStatus.VERIFIED.value)
        with self.assertRaises(ContractViolation):
            flow.todos.guard_resend("SYNTH-EVT-0003")

    def test_shift_level_unit_walks_the_whole_machine(self):
        flow, report = plan_flow()
        unit_id = synth.SHIFT_ID
        self.assertEqual(flow.todos.get(unit_id).scope, ConfirmationScope.SHIFT.value)
        flow.todos.confirm(unit_id, synth.TO)
        self.assertEqual(flow.board_row(unit_id)["确认状态"], "confirmed")
        flow.todos.run_writeback(unit_id, simulate="unknown")
        flow.todos.recheck_unit(unit_id, applied=True)
        self.assertTrue(flow.todos.get(unit_id).is_written_back)
        self.assertEqual(flow.todos.summary()["written_back"], 1)

    def test_readback_evidence_source_is_the_aitable(self):
        flow, _ = plan_flow()
        evidence = flow.todos.readback("SYNTH-EVT-0003")
        self.assertIsInstance(evidence, ReadbackEvidence)
        self.assertEqual(evidence.source, "aitable")
        self.assertTrue(evidence.applied)
        self.assertEqual(evidence.payload["unit_id"], "SYNTH-EVT-0003")

    def test_confirmation_requires_a_todo_id(self):
        with self.assertRaises(ContractViolation):
            Confirmation(
                unit_id="SYNTH-EVT-0003",
                scope="event",
                shift_id=synth.SHIFT_ID,
                todo_id="",
                assignee=synth.TO,
            )


if __name__ == "__main__":
    unittest.main()
