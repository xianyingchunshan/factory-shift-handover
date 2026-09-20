"""告警闸门测试（issue #9 第 3 段）：告警未清禁止出清单 / 禁止提交。

验收 L1/L2：ALARM_NOT_CLEARED 可复现、班次置 blocked、清告警与补全必须同步、
拒收（E001，不落表）同样拦清单、交班提交前全表复验。
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

from contracts.enums import ShiftStatus  # noqa: E402
from contracts.errors import ALARM_NOT_CLEARED, ContractViolation  # noqa: E402
from contracts.shift import ShiftStatus as ShiftStatusEnum  # noqa: E402  (别名，便于阅读)

from workflow.checklist import require_clear_or_block, sync_shift_status  # noqa: E402

import wf_synth as synth  # noqa: E402


class TestReportGate(unittest.TestCase):
    def test_open_alarms_block_the_report_and_mark_shift_blocked(self):
        flow = synth.alarm_flow()
        with synth.expect_code(ALARM_NOT_CLEARED):
            synth.report_of(flow)
        self.assertTrue(flow.shift.is_blocked)
        self.assertEqual(flow.shift.status, ShiftStatusEnum.BLOCKED.value)
        self.assertEqual(flow.intake.ledger.open_count, 4)
        self.assertEqual(flow.intake.shift_table.get(synth.SHIFT_ID).status, "blocked")

    def test_blocked_summary_lists_open_alarms_and_rejections(self):
        flow = synth.alarm_flow()
        summary = flow.checklist.blocked_summary()
        self.assertTrue(summary["blocked"])
        self.assertEqual(len(summary["open_alarms"]), 4)
        self.assertEqual(len(summary["rejected_entries"]), 1)
        self.assertEqual(summary["status"], ShiftStatusEnum.BLOCKED.value)

    def test_report_is_available_after_repair_and_clear(self):
        flow = synth.alarm_flow()
        synth.repair_all(flow)
        report = synth.report_of(flow)
        self.assertFalse(flow.shift.is_blocked)
        self.assertEqual(len(report.event_flow), 5)
        self.assertEqual(report.completeness_summary["alarm_count"], 4)
        self.assertEqual(report.completeness_summary["open_alarm_count"], 0)

    def test_clearing_alarms_without_repairing_events_still_blocks(self):
        flow = synth.alarm_flow()
        # 只清告警、不补全（走服务入口，留痕在案），仍不得出清单
        for alarm in flow.intake.ledger.open_alarms():
            flow.intake.clear_alarm(alarm.alarm_id, reason="SYNTH 只清不补（反向验证）")
        self.assertFalse(flow.intake.ledger.has_open)
        with self.assertRaises(ContractViolation):
            synth.report_of(flow)

    def test_only_reject_alarm_also_blocks_until_reintake(self):
        flow = synth.make_flow()
        flow.submit([synth.row("SYNTH-EVT-0001", occurred_at="2026-01-02 08:10")])
        flow.submit([synth.row("SYNTH-EVT-0002", occurred_at=None)])
        with synth.expect_code(ALARM_NOT_CLEARED):
            synth.report_of(flow)
        # 补全后重新录入 → 拒收告警清除 → 可出清单
        flow.submit([synth.row("SYNTH-EVT-0002", occurred_at="2026-01-02 09:00")])
        report = synth.report_of(flow)
        self.assertEqual([event.event_id for event in report.event_flow], [
            "SYNTH-EVT-0001",
            "SYNTH-EVT-0002",
        ])

    def test_source_inconsistency_blocks_report(self):
        flow = synth.make_flow(create_shift=False)
        flow.intake.create_shift()
        flow.adapter.queue_failure("create", "rejected")
        flow.submit([synth.row("SYNTH-EVT-0001")])
        with self.assertRaises(ContractViolation):
            synth.report_of(flow)

    def test_standalone_gate_helper(self):
        flow = synth.alarm_flow()
        with synth.expect_code(ALARM_NOT_CLEARED):
            require_clear_or_block(flow.shift, flow.intake.ledger)
        synth.repair_all(flow)
        require_clear_or_block(flow.shift, flow.intake.ledger)
        self.assertFalse(flow.shift.is_blocked)


class TestShiftSubmit(unittest.TestCase):
    def test_submit_shift_is_blocked_while_alarms_are_open(self):
        flow = synth.alarm_flow()
        with synth.expect_code(ALARM_NOT_CLEARED):
            flow.intake.submit_shift()
        self.assertEqual(flow.shift.status, ShiftStatusEnum.BLOCKED.value)
        self.assertEqual(flow.intake.shift_table.get(synth.SHIFT_ID).status, "blocked")

    def test_submit_shift_after_repair_writes_status_to_table(self):
        flow = synth.alarm_flow()
        synth.repair_all(flow)
        shift = flow.intake.submit_shift()
        self.assertEqual(shift.status, ShiftStatus.SUBMITTED.value)
        self.assertEqual(flow.intake.shift_table.get(synth.SHIFT_ID).status, "submitted")
        self.assertFalse(shift.is_blocked)

    def test_recheck_reports_counts_after_repair(self):
        flow = synth.alarm_flow()
        synth.repair_all(flow)
        result = flow.intake.recheck()
        self.assertEqual(result.event_total, 5)
        self.assertEqual(result.event_complete, 5)
        self.assertEqual(result.stored_count, 5)
        self.assertEqual(result.open_alarm_count, 0)
        self.assertEqual(result.alarm_count, 4)
        self.assertEqual(result.rejected_count, 1)

    def test_recheck_refuses_when_memory_and_table_disagree(self):
        flow = synth.make_flow(create_shift=False)
        flow.intake.create_shift()
        flow.adapter.queue_failure("create", "rejected")
        flow.submit([synth.row("SYNTH-EVT-0001")])
        with self.assertRaises(ContractViolation):
            flow.intake.recheck()
        problems = flow.intake.verify_source_consistency()
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("内存有但表里没有的事件" in problem for problem in problems))
        self.assertTrue(any("受理不明" in problem for problem in problems))

    def test_submit_shift_twice_is_idempotent(self):
        flow = synth.clean_flow()
        flow.intake.submit_shift()
        again = flow.intake.submit_shift()
        self.assertEqual(again.status, ShiftStatus.SUBMITTED.value)

    def test_blocked_shift_returns_to_previous_status_after_clear(self):
        flow = synth.alarm_flow()
        with synth.expect_code(ALARM_NOT_CLEARED):
            flow.intake.submit_shift()
        self.assertEqual(flow.shift.status, ShiftStatus.BLOCKED.value)
        synth.repair_all(flow)
        flow.intake.recheck()
        self.assertEqual(flow.shift.status, ShiftStatus.DRAFT.value)
        self.assertIsNone(flow.shift.blocked_from)


class TestAlarmLedgerBookkeeping(unittest.TestCase):
    """告警账本：清告警留痕、幂等、计数。"""

    def test_cleared_alarms_are_kept_in_the_ledger(self):
        flow = synth.alarm_flow()
        synth.repair_all(flow)
        self.assertEqual(flow.intake.ledger.open_count, 0)
        self.assertEqual(flow.intake.ledger.alarm_count, 4)
        self.assertEqual(len(flow.intake.alarm_journal.entries(synth.SHIFT_ID)), 4)

    def test_clearing_twice_keeps_first_cleared_at(self):
        flow = synth.alarm_flow()
        alarm_id = flow.intake.ledger.open_alarms()[0].alarm_id
        first = flow.intake.clear_alarm(alarm_id, reason="SYNTH 第一次")
        cleared_at = first.cleared_at
        again = flow.intake.clear_alarm(alarm_id, reason="SYNTH 第二次")
        self.assertEqual(again.cleared_at, cleared_at)

    def test_alarm_ids_are_stable_and_descriptive(self):
        flow = synth.alarm_flow()
        ids = [alarm.alarm_id for alarm in flow.intake.ledger.all_alarms()]
        self.assertIn("E001:SYNTH-SHIFT-0001:SYNTH-EVT-0005:occurred_at", ids)
        self.assertEqual(len(ids), len(set(ids)))


class TestShiftStatusAlias(unittest.TestCase):
    """状态取值来自契约，避免本层自定义字面量。"""

    def test_values_come_from_contract_enum(self):
        self.assertEqual(ShiftStatus.BLOCKED.value, "blocked")
        self.assertEqual(ShiftStatus.SUBMITTED.value, "submitted")
        self.assertEqual(ShiftStatus.DRAFT.value, "draft")

    def test_sync_shift_status_writes_the_state_column(self):
        flow = synth.clean_flow()
        flow.shift.status = ShiftStatus.SUBMITTED.value
        sync_shift_status(flow.shift, flow.adapter)
        self.assertEqual(flow.intake.shift_table.get(synth.SHIFT_ID).status, "submitted")

    def test_sync_shift_status_is_a_noop_without_a_row(self):
        flow = synth.make_flow(create_shift=False)
        flow.shift.status = ShiftStatus.BLOCKED.value
        sync_shift_status(flow.shift, flow.adapter)
        self.assertEqual(flow.intake.shift_table.count(), 0)


if __name__ == "__main__":
    unittest.main()
