"""契约测试：输出清单 HandoverReport（issue #6 §4）。

覆盖：五段固定顺序、关键级置顶加粗、流水严格时间序、完整性汇总数字、
告警未清禁止出清单、分发只单发两人（不进群）、送达两条回读。
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

from contracts.errors import (  # noqa: E402
    ALARM_NOT_CLEARED,
    ContractViolation,
)
from contracts.report import (  # noqa: E402
    REPORT_SECTIONS,
    DeliveryReceipt,
    DispatchPlan,
    build_report,
)

import synth  # noqa: E402

SECTION_HEADERS = ("一、", "二、", "三、", "四、", "五、")


def make_report(*, with_partial: bool = False, todo_id_factory=None):
    """合成一套清单：2 条一般事项 + 1 条关键级（重大事项/移交接班人）。"""
    shift, ledger, store = synth.make_store()
    store.intake(
        synth.raw_event(
            "SYNTH-EVT-0001", category="runtime", status="done", occurred_at="2026-01-02 08:10"
        )
    )
    store.intake(
        synth.raw_event(
            "SYNTH-EVT-0002",
            category="equipment",
            status="in_progress",
            occurred_at="2026-01-02 09:30",
        )
    )
    store.intake(
        synth.raw_event(
            "SYNTH-EVT-0003",
            category="重大事项",
            status="移交接班人",
            occurred_at="2026-01-02 14:20",
        )
    )
    if with_partial:
        store.intake(synth.raw_event("SYNTH-EVT-0004", occurred_at="2026-01-02"))
        store.get("SYNTH-EVT-0004").resolve_time("2026-01-02 10:05")
        synth.clear_all(ledger)
    factory = todo_id_factory
    if factory is None:
        factory = lambda unit: "SYNTH-TODO-" + unit.unit_id[-4:]  # noqa: E731
    report = build_report(
        shift, store, ledger, generated_at=synth.at(20, 5), todo_id_factory=factory
    )
    return shift, ledger, store, report


class TestReportStructure(unittest.TestCase):
    def setUp(self):
        self.shift, self.ledger, self.store, self.report = make_report()

    def test_sections_appear_in_contract_order(self):
        keys = [key for key in self.report.to_dict() if key in REPORT_SECTIONS]
        self.assertEqual(keys, list(REPORT_SECTIONS))
        self.assertEqual(
            REPORT_SECTIONS,
            (
                "critical_items",
                "event_flow",
                "pending_transfers",
                "completeness_summary",
                "confirmation_area",
            ),
        )

    def test_metadata_matches_the_shift(self):
        self.assertEqual(self.report.shift_id, synth.SHIFT_ID)
        self.assertEqual(self.report.handover_line, synth.LINE)
        self.assertEqual(self.report.to_dict()["shift_date"], "2026-01-02")
        self.assertEqual(self.report.to_dict()["generated_at"], "2026-01-02 20:05")
        self.assertEqual(self.report.window_start, synth.START)

    def test_critical_items_are_placed_first_and_kept_bold(self):
        self.assertEqual([e.event_id for e in self.report.critical_items], ["SYNTH-EVT-0003"])
        lines = self.report.render_lines()
        critical_lines = [line for line in lines if line.strip().startswith("**")]
        self.assertEqual(len(critical_lines), 1)
        self.assertIn(self.report.critical_items[0].description, critical_lines[0])
        first_section = next(index for index, line in enumerate(lines) if line.startswith("■ 一、"))
        bold_index = next(index for index, line in enumerate(lines) if line.strip().startswith("**"))
        self.assertGreater(bold_index, first_section, "关键级在第一节内且加粗")

    def test_event_flow_is_strictly_time_ordered(self):
        times = [event.occurred_at for event in self.report.event_flow]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(self.report.event_flow), 3, "流水是全量")

    def test_pending_transfers_contain_only_transferred_events(self):
        self.assertEqual([e.event_id for e in self.report.pending_transfers], ["SYNTH-EVT-0003"])
        self.assertTrue(all(e.is_transfer_pending for e in self.report.pending_transfers))

    def test_completeness_summary_counts(self):
        summary = self.report.completeness_summary
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["complete"], 3)
        self.assertEqual(summary["time_complete_rate"], 1.0)
        self.assertEqual(summary["alarm_count"], 0)

    def test_summary_counts_cleared_alarms(self):
        shift, ledger, store, report = make_report(with_partial=True)
        self.assertEqual(report.completeness_summary["total"], 4)
        self.assertEqual(report.completeness_summary["alarm_count"], 1)
        self.assertEqual(report.completeness_summary["open_alarm_count"], 0)

    def test_confirmation_area_has_event_and_shift_units(self):
        units = self.report.confirmation_area
        self.assertEqual([(u.scope, u.unit_id) for u in units], [
            ("event", "SYNTH-EVT-0003"),
            ("shift", synth.SHIFT_ID),
        ])
        self.assertEqual(units[0].todo_id, "SYNTH-TODO-0003")
        self.assertEqual(units[0].assignee.user_id, synth.TO.user_id)

    def test_units_without_created_todo_have_no_todo_id(self):
        shift, ledger, store, report = make_report(todo_id_factory=lambda unit: "")
        self.assertFalse(any(unit.has_todo for unit in report.confirmation_area))

    def test_render_lines_contain_five_sections(self):
        text = "\n".join(self.report.render_lines())
        for header in SECTION_HEADERS:
            self.assertIn(f"■ {header}", text)
        self.assertIn("交接线=SYNTH-LINE-A", text)
        self.assertIn("时间完整率=100%", text)


class TestReportGateOnAlarms(unittest.TestCase):
    def test_open_alarm_blocks_report_and_marks_shift_blocked(self):
        shift, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02"))
        with synth.expect_code(ALARM_NOT_CLEARED):
            build_report(shift, store, ledger, generated_at=synth.at(20, 5))
        self.assertTrue(shift.is_blocked, "告警未清 → shift.status=blocked")

    def test_reject_alarm_also_blocks_the_report(self):
        shift, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at=synth.OMIT))
        store.intake(synth.raw_event("SYNTH-EVT-0002"))
        with synth.expect_code(ALARM_NOT_CLEARED):
            build_report(shift, store, ledger, generated_at=synth.at(20, 5))

    def test_clearing_alarms_without_repairing_events_still_blocks(self):
        shift, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02"))
        synth.clear_all(ledger)
        with self.assertRaises(ContractViolation):
            build_report(shift, store, ledger, generated_at=synth.at(20, 5))

    def test_report_is_available_after_repair_and_clear(self):
        shift, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02"))
        store.get("SYNTH-EVT-0001").resolve_time("2026-01-02 09:40")
        synth.clear_all(ledger)
        report = build_report(shift, store, ledger, generated_at=synth.at(20, 5))
        self.assertEqual(len(report.event_flow), 1)
        self.assertFalse(shift.is_blocked)
        self.assertEqual(shift.status, "draft")


class TestDispatchAndReceipts(unittest.TestCase):
    def setUp(self):
        self.shift, self.ledger, self.store, self.report = make_report()

    def test_dispatch_targets_only_two_people(self):
        plan = self.report.dispatch
        self.assertEqual(len(plan.recipients), 2)
        self.assertEqual(self.report.recipient_ids(), (synth.FROM.user_id, synth.TO.user_id))
        self.assertEqual(plan.channel, "direct_single")
        self.assertFalse(plan.allow_group)
        self.assertEqual(plan.to_dict()["expected_message_count"], 2)

    def test_third_party_target_is_rejected(self):
        with self.assertRaises(ContractViolation):
            self.report.dispatch.validate_targets([synth.STRANGER])

    def test_same_person_as_handover_from_and_to_is_rejected(self):
        with self.assertRaises(ContractViolation):
            DispatchPlan(recipients=(synth.FROM, synth.FROM))

    def test_two_readbacks_are_required_before_delivery_is_complete(self):
        self.assertFalse(self.report.is_delivery_complete)
        self.report.record_receipt(
            DeliveryReceipt("SYNTH-MSG-1", synth.FROM, synth.at(20, 6))
        )
        self.assertFalse(self.report.is_delivery_complete)
        self.report.record_receipt(
            DeliveryReceipt("SYNTH-MSG-2", synth.TO, synth.at(20, 6))
        )
        self.assertTrue(self.report.is_delivery_complete)

    def test_duplicate_or_third_receipt_is_rejected(self):
        self.report.record_receipt(DeliveryReceipt("SYNTH-MSG-1", synth.FROM, synth.at(20, 6)))
        with self.assertRaises(ContractViolation):
            self.report.record_receipt(DeliveryReceipt("SYNTH-MSG-2", synth.FROM, synth.at(20, 6)))
        with self.assertRaises(ContractViolation):
            self.report.record_receipt(
                DeliveryReceipt("SYNTH-MSG-3", synth.STRANGER, synth.at(20, 6))
            )

    def test_receipt_requires_message_id(self):
        with self.assertRaises(ContractViolation):
            DeliveryReceipt("  ", synth.TO, synth.at(20, 6))


if __name__ == "__main__":
    unittest.main()
