"""清单一致性测试（issue #9 第 3 段）。

验收 L1/L2：五段固定结构、关键级置顶、``occurred_at`` 升序全量流水、
未完移交段、完整性汇总数**与交接事件表严格一致**，篡改即被拒。
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

from contracts.identity import IdentityRef  # noqa: E402
from contracts.events import HandoverEvent  # noqa: E402
from contracts.report import REPORT_SECTIONS  # noqa: E402
from contracts.errors import ContractViolation  # noqa: E402

from integrations.aitable.cells import event_to_fields  # noqa: E402
from integrations.aitable.tables import EVENT_TABLE  # noqa: E402

import wf_synth as synth  # noqa: E402


class TestReportStructure(unittest.TestCase):
    def setUp(self):
        self.flow = synth.clean_flow()
        self.report = synth.report_of(self.flow)

    def test_sections_follow_the_contract_order(self):
        keys = [key for key in self.report.to_dict() if key in REPORT_SECTIONS]
        self.assertEqual(keys, list(REPORT_SECTIONS))

    def test_critical_items_are_placed_first_and_bold(self):
        critical_ids = [event.event_id for event in self.report.critical_items]
        self.assertEqual(critical_ids, ["SYNTH-EVT-0003"])
        lines = self.report.render_lines()
        bold = [line for line in lines if line.strip().startswith("**")]
        self.assertEqual(len(bold), 1)
        self.assertIn(self.report.critical_items[0].description, bold[0])
        first_section = next(index for index, line in enumerate(lines) if line.startswith("■ 一、"))
        bold_index = next(
            index for index, line in enumerate(lines) if line.strip().startswith("**")
        )
        self.assertGreater(bold_index, first_section, "关键级在第一段内且加粗")
        self.assertTrue(all(event.is_critical for event in self.report.critical_items))

    def test_event_flow_is_complete_and_time_ordered(self):
        flow_ids = [event.event_id for event in self.report.event_flow]
        self.assertEqual(flow_ids, ["SYNTH-EVT-0001", "SYNTH-EVT-0002", "SYNTH-EVT-0003"])
        times = [event.occurred_at for event in self.report.event_flow]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(self.report.event_flow), self.flow.intake.event_table.count())

    def test_unsorted_time_rows_are_sorted_in_the_report(self):
        flow = synth.make_flow()
        flow.submit(
            [
                synth.row("SYNTH-EVT-0001", occurred_at="2026-01-02 12:00"),
                synth.row("SYNTH-EVT-0002", occurred_at="2026-01-02 08:30"),
                synth.row("SYNTH-EVT-0003", occurred_at="2026-01-02 19:10"),
            ]
        )
        report = synth.report_of(flow)
        self.assertEqual(
            [event.event_id for event in report.event_flow],
            ["SYNTH-EVT-0002", "SYNTH-EVT-0001", "SYNTH-EVT-0003"],
        )

    def test_pending_transfers_only_contain_transferred_events(self):
        self.assertEqual(
            [event.event_id for event in self.report.pending_transfers], ["SYNTH-EVT-0003"]
        )
        self.assertTrue(all(event.is_transfer_pending for event in self.report.pending_transfers))

    def test_summary_counts_match_the_event_table(self):
        summary = self.report.completeness_summary
        self.assertEqual(summary["total"], self.flow.intake.event_table.count(synth.SHIFT_ID))
        self.assertEqual(summary["complete"], 3)
        self.assertEqual(summary["time_complete_rate"], 1.0)
        self.assertEqual(summary["alarm_count"], 0)

    def test_render_text_has_five_sections_and_metadata(self):
        text = "\n".join(self.report.render_lines())
        for header in ("一、", "二、", "三、", "四、", "五、"):
            self.assertIn(f"■ {header}", text)
        self.assertIn("交接线=SYNTH-LINE-A", text)
        self.assertIn("时间完整率=100%", text)
        self.assertIn("事件数=3", text)


class TestConsistencyCheck(unittest.TestCase):
    def setUp(self):
        self.flow = synth.clean_flow()
        self.report = synth.report_of(self.flow)

    def test_verify_passes_and_reports_counts(self):
        result = self.flow.checklist.verify(self.report)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.problems, ())
        self.assertEqual(result.checked["table_rows"], 3)
        self.assertEqual(result.checked["flow"], 3)
        self.assertEqual(result.checked["critical"], 1)
        self.assertEqual(result.checked["pending_transfers"], 1)
        self.assertEqual(result.checked["summary_total"], 3)
        self.assertEqual(result.checked["alarm_count"], 0)

    def test_verify_detects_dropped_flow_event(self):
        self.report.event_flow = self.report.event_flow[:-1]
        result = self.flow.checklist.verify(self.report)
        self.assertFalse(result.ok)
        self.assertTrue(any("流水与交接事件表不一致" in problem for problem in result.problems))

    def test_verify_detects_wrong_summary_total(self):
        self.report.completeness_summary = dict(self.report.completeness_summary, total=99)
        result = self.flow.checklist.verify(self.report)
        self.assertFalse(result.ok)
        self.assertTrue(any("汇总事件数与表格行数不一致" in p for p in result.problems))

    def test_verify_detects_critical_section_mismatch(self):
        self.report.critical_items = ()
        result = self.flow.checklist.verify(self.report)
        self.assertFalse(result.ok)
        self.assertTrue(any("关键级置顶段" in problem for problem in result.problems))

    def test_verify_detects_pending_section_mismatch(self):
        self.report.pending_transfers = ()
        result = self.flow.checklist.verify(self.report)
        self.assertFalse(result.ok)
        self.assertTrue(any("未完移交段" in problem for problem in result.problems))

    def test_generate_refuses_when_table_has_an_extra_row(self):
        """表里多了一行而内存不知道（外部直写）→ 出清单前必须被拦。"""
        event = HandoverEvent(
            event_id="SYNTH-EVT-0009",
            shift_id=synth.SHIFT_ID,
            category="equipment",
            description="SYNTH 外部直写行",
            occurred_at=synth.at(15, 0),
            severity="normal",
            status="done",
        )
        self.flow.adapter.create_record(EVENT_TABLE, event_to_fields(event))
        problems = self.flow.checklist.source_problems()
        self.assertTrue(problems)
        self.assertTrue(any("SYNTH-EVT-0009" in problem for problem in problems))
        with self.assertRaises(ContractViolation):
            synth.report_of(self.flow)


class TestReportMetadata(unittest.TestCase):
    def test_window_and_recipients_come_from_the_shift(self):
        flow = synth.clean_flow()
        report = synth.report_of(flow)
        self.assertEqual(report.shift_id, synth.SHIFT_ID)
        self.assertEqual(report.window_start, synth.START)
        self.assertEqual(report.window_end, synth.END)
        self.assertEqual(report.recipient_ids(), (synth.FROM.user_id, synth.TO.user_id))
        self.assertEqual(report.dispatch.channel, "direct_single")
        self.assertFalse(report.dispatch.allow_group)
        self.assertEqual(report.dispatch.to_dict()["expected_message_count"], 2)

    def test_third_party_cannot_be_added_to_dispatch(self):
        flow = synth.clean_flow()
        report = synth.report_of(flow)
        with self.assertRaises(ContractViolation):
            report.dispatch.validate_targets([synth.STRANGER])
        self.assertEqual(
            len(report.dispatch.recipients), 2, "只单发交班人与接班人两人，不进群"
        )

    def test_generated_at_uses_injected_clock(self):
        flow = synth.clean_flow()
        report = synth.report_of(flow)
        self.assertEqual(report.to_dict()["generated_at"], "2026-01-02 20:00")


class TestAlarmStateNeverEntersReport(unittest.TestCase):
    def test_held_events_are_excluded_from_the_flow(self):
        flow = synth.alarm_flow()
        synth.repair_all(flow)
        report = synth.report_of(flow)
        self.assertTrue(all(event.can_enter_report() for event in report.event_flow))
        self.assertEqual(report.completeness_summary["open_alarm_count"], 0)

    def test_repairing_only_some_events_still_blocks(self):
        flow = synth.alarm_flow()
        flow.intake.repair_time("SYNTH-EVT-0002", "2026-01-02 10:05")
        with synth.expect_code("ALARM_NOT_CLEARED"):
            synth.report_of(flow)


if __name__ == "__main__":
    unittest.main()
