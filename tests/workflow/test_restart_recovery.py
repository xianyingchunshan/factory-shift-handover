"""重启恢复测试（issue #9 验收：重启后状态可恢复）。

两条恢复路径：

1. **从合成存储重建**（``workflow.state.rebuild``）：读班次表 + 输入留痕表 + 告警处置
   留痕表 + 待办表，重新复验事件与告警，**不重写表格、不重放外部写入**；
2. **序列化恢复**（``dump_tables`` / ``dumps`` / ``loads`` / ``load_tables``）：
   快照搬到新适配器后重建。

覆盖：班次状态、事件全量、告警账本（含已清）、拒收留痕、确认与回写进度、
待办不重复创建、清单可重新生成且与重启前一致。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.enums import ConfirmationStatus, WritebackStatus  # noqa: E402
from contracts.errors import ALARM_NOT_CLEARED, ContractViolation  # noqa: E402

from integrations.aitable.cells import EVENT_COLUMNS  # noqa: E402
from integrations.aitable.tables import (  # noqa: E402
    ALARM_JOURNAL_TABLE,
    EVENT_TABLE,
    INTAKE_JOURNAL_TABLE,
    SHIFT_TABLE,
    TODO_TABLE,
)
from workflow import state as wfstate  # noqa: E402

import wf_synth as synth  # noqa: E402


def prepared_flow():
    """走完整流程：四种告警 → 补全/复核/重录 → 新增关键事项 → 提交 → 出清单 → 确认与回写。"""
    flow = synth.alarm_flow()
    synth.repair_all(flow)
    flow.intake.submit(
        synth.row(
            "SYNTH-EVT-0006",
            category="重大事项",
            status="移交接班人",
            severity="normal",
            occurred_at="2026-01-02 14:20",
        )
    )
    flow.intake.submit_shift()
    report = synth.report_of(flow)
    flow.todos.create_plan(report)
    flow.todos.confirm("SYNTH-EVT-0006", synth.TO)
    flow.todos.run_writeback("SYNTH-EVT-0006")
    flow.todos.confirm(synth.SHIFT_ID, synth.TO)
    flow.todos.run_writeback(synth.SHIFT_ID, simulate="unknown")
    return flow, report


def row_counts(adapter) -> dict[str, int]:
    return {
        table: adapter.row_count(table)
        for table in (SHIFT_TABLE, EVENT_TABLE, INTAKE_JOURNAL_TABLE, ALARM_JOURNAL_TABLE, TODO_TABLE)
    }


class TestRestartFromSyntheticStore(unittest.TestCase):
    def setUp(self):
        self.flow, self.report = prepared_flow()
        self.before = row_counts(self.flow.adapter)
        self.before_alarms = self.flow.intake.ledger.counts_by_rule()
        self.before_records = {
            record.unit_id: (record.status, record.writeback_status)
            for record in self.flow.todos.confirmations
        }

    def _restart_new_process(self):
        payload = wfstate.dump_tables(self.flow.adapter)
        text = wfstate.dumps(payload)
        fresh = synth.make_adapter()
        return wfstate.restart(
            wfstate.loads(text),
            adapter=fresh,
            shift_id=synth.SHIFT_ID,
            clock=synth.fixed_clock(),
        )

    def test_restart_restores_shift_events_and_alarms(self):
        bundle = self._restart_new_process()
        self.assertEqual(bundle.issues, ())
        self.assertEqual(bundle.shift.status, "submitted")
        self.assertEqual(bundle.shift.shift_id, synth.SHIFT_ID)
        self.assertEqual(
            bundle.intake.event_table.event_ids(synth.SHIFT_ID),
            self.flow.intake.event_table.event_ids(synth.SHIFT_ID),
        )
        self.assertEqual(
            [event.event_id for event in bundle.intake.events],
            [event.event_id for event in self.flow.intake.events],
        )
        self.assertEqual(bundle.intake.ledger.counts_by_rule(), self.before_alarms)
        self.assertEqual(bundle.intake.ledger.open_count, 0)
        self.assertEqual(row_counts(bundle.intake.adapter), self.before)

    def test_restart_restores_confirmations_without_resending_todos(self):
        bundle = self._restart_new_process()
        restored = {
            record.unit_id: (record.status, record.writeback_status)
            for record in bundle.todos.confirmations
        }
        self.assertEqual(restored, self.before_records)
        self.assertEqual(
            bundle.todos.get("SYNTH-EVT-0006").status, ConfirmationStatus.WRITTEN_BACK.value
        )
        self.assertEqual(
            bundle.todos.get(synth.SHIFT_ID).writeback_status, WritebackStatus.UNKNOWN.value
        )
        self.assertTrue(bundle.todos.get(synth.SHIFT_ID).needs_recheck)
        self.assertEqual(bundle.intake.adapter.row_count(TODO_TABLE), 2)

    def test_restart_regenerates_the_same_report(self):
        bundle = self._restart_new_process()
        report = bundle.checklist().generate(todo_id_factory=lambda unit: "")
        self.assertEqual(
            [event.event_id for event in report.event_flow],
            [event.event_id for event in self.report.event_flow],
        )
        self.assertEqual(
            [event.event_id for event in report.critical_items],
            [event.event_id for event in self.report.critical_items],
        )
        self.assertEqual(report.completeness_summary["total"], self.report.completeness_summary["total"])
        self.assertEqual(report.completeness_summary["alarm_count"], self.report.completeness_summary["alarm_count"])

    def test_rebuild_writes_no_new_rows(self):
        payload = wfstate.dump_tables(self.flow.adapter)
        fresh = synth.make_adapter()
        wfstate.load_tables(fresh, payload)
        counts_after_import = row_counts(fresh)
        wfstate.rebuild(fresh, shift_id=synth.SHIFT_ID, clock=synth.fixed_clock())
        self.assertEqual(row_counts(fresh), counts_after_import)

    def test_restart_via_clone_keeps_record_ids(self):
        clone = self.flow.adapter.clone()
        bundle = wfstate.rebuild(clone, shift_id=synth.SHIFT_ID, clock=synth.fixed_clock())
        self.assertEqual(bundle.issues, ())
        self.assertEqual(
            bundle.todos.get("SYNTH-EVT-0006").todo_id,
            self.flow.todos.get("SYNTH-EVT-0006").todo_id,
        )

    def test_restart_can_submit_nothing_new_but_can_recheck(self):
        bundle = self._restart_new_process()
        recheck = bundle.intake.recheck()
        self.assertEqual(recheck.open_alarm_count, 0)
        self.assertEqual(recheck.stored_count, 6)
        self.assertEqual(recheck.alarm_count, 4)
        status = bundle.todos.recheck_unit(synth.SHIFT_ID, applied=True)
        self.assertEqual(status.writeback_status, WritebackStatus.VERIFIED.value)


class TestRestartKeepsRejectionsBlocking(unittest.TestCase):
    def test_e001_rejection_still_blocks_after_restart(self):
        flow = synth.alarm_flow()
        # 只补全/复核/补字段，不重录被拒收的那条 → E001 仍未清
        flow.intake.repair_time("SYNTH-EVT-0002", "2026-01-02 10:05")
        flow.intake.review_out_of_range("SYNTH-EVT-0003")
        flow.intake.fill_required("SYNTH-EVT-0004", severity="normal")
        with synth.expect_code(ALARM_NOT_CLEARED):
            synth.report_of(flow)

        payload = wfstate.dump_tables(flow.adapter)
        fresh = synth.make_adapter()
        bundle = wfstate.restart(
            payload, adapter=fresh, shift_id=synth.SHIFT_ID, clock=synth.fixed_clock()
        )
        self.assertEqual(bundle.issues, ())
        self.assertEqual(bundle.intake.ledger.open_count, 1)
        self.assertEqual(bundle.intake.ledger.open_alarms()[0].rule, "E001")
        with synth.expect_code(ALARM_NOT_CLEARED):
            bundle.checklist().generate()

    def test_reintake_after_restart_clears_the_rejection(self):
        flow = synth.alarm_flow()
        payload = wfstate.dump_tables(flow.adapter)
        bundle = wfstate.restart(
            payload, adapter=synth.make_adapter(), shift_id=synth.SHIFT_ID, clock=synth.fixed_clock()
        )
        self.assertTrue(bundle.intake.ledger.has_open)
        outcome = bundle.intake.submit(synth.row("SYNTH-EVT-0005", occurred_at="2026-01-02 11:30"))
        self.assertTrue(outcome.stored)
        reject_alarms = [
            alarm for alarm in bundle.intake.ledger.all_alarms() if alarm.rule == "E001"
        ]
        self.assertTrue(all(alarm.is_cleared for alarm in reject_alarms))


class TestRestartReportsInconsistencies(unittest.TestCase):
    """恢复只报问题、不自动补写（写入不等于完成，人工处置优先）。"""

    def setUp(self):
        self.flow = synth.clean_flow()

    def _payload(self) -> dict:
        return wfstate.dump_tables(self.flow.adapter)

    def test_missing_event_row_is_reported(self):
        payload = self._payload()
        payload[EVENT_TABLE] = [
            row for row in payload[EVENT_TABLE] if row["fields"]["事件ID"] != "SYNTH-EVT-0001"
        ]
        fresh = synth.make_adapter()
        bundle = wfstate.restart(
            payload, adapter=fresh, shift_id=synth.SHIFT_ID, clock=synth.fixed_clock()
        )
        self.assertEqual(len(bundle.issues), 1)
        self.assertIn("交接事件表里没有这一行", bundle.issues[0].problem)
        self.assertEqual(bundle.intake.event_table.count(synth.SHIFT_ID), 2)
        self.assertIsNone(bundle.intake.store.get("SYNTH-EVT-0001"))

    def test_orphan_event_row_is_reported(self):
        payload = self._payload()
        payload[EVENT_TABLE].append(
            {"record_id": "SYNTH-REC-999999", "fields": dict(payload[EVENT_TABLE][0]["fields"], 事件ID="SYNTH-EVT-9999")}
        )
        fresh = synth.make_adapter()
        bundle = wfstate.restart(
            payload, adapter=fresh, shift_id=synth.SHIFT_ID, clock=synth.fixed_clock()
        )
        self.assertEqual(len(bundle.issues), 1)
        self.assertEqual(bundle.issues[0].event_id, "SYNTH-EVT-9999")
        self.assertIn("没有对应输入留痕", bundle.issues[0].problem)

    def test_pending_write_unknown_is_reported_and_not_replayed(self):
        flow = synth.make_flow()
        flow.adapter.queue_failure("create", "unknown")
        outcome = flow.submit([synth.row("SYNTH-EVT-0001")])
        self.assertTrue(outcome[0].write_unknown)
        payload = wfstate.dump_tables(flow.adapter)
        fresh = synth.make_adapter()
        bundle = wfstate.restart(
            payload, adapter=fresh, shift_id=synth.SHIFT_ID, clock=synth.fixed_clock()
        )
        self.assertEqual(len(bundle.issues), 1)
        self.assertIn("受理不明", bundle.issues[0].problem)
        self.assertEqual(bundle.intake.event_table.count(synth.SHIFT_ID), 0, "不得自动补写")

    def test_rebuild_requires_an_existing_shift(self):
        with self.assertRaises(ContractViolation):
            wfstate.rebuild(synth.make_adapter(), shift_id="SYNTH-SHIFT-9999")

    def test_restoring_an_unknown_shift_id_is_refused(self):
        with self.assertRaises(ContractViolation):
            wfstate.restart(self._payload(), adapter=synth.make_adapter(), shift_id="SYNTH-SHIFT-9999")


class TestSnapshotSerialization(unittest.TestCase):
    def test_dumps_loads_round_trip_keeps_chinese_and_is_stable(self):
        flow = synth.clean_flow()
        payload = wfstate.dump_tables(flow.adapter)
        text = wfstate.dumps(payload)
        self.assertIn("交接事件表", text)
        self.assertNotIn("\\u4ea4", text, "中文不得转义（便于人工核对）")
        self.assertEqual(wfstate.loads(text), payload)
        self.assertEqual(wfstate.dumps(wfstate.loads(text)), text, "序列化必须稳定可 diff")

    def test_loads_rejects_broken_json(self):
        with self.assertRaises(ContractViolation):
            wfstate.loads("{not json}")
        with self.assertRaises(ContractViolation):
            wfstate.loads("[1, 2, 3]")

    def test_load_tables_rejects_bad_shape(self):
        fresh = synth.make_adapter()
        with self.assertRaises(ContractViolation):
            wfstate.load_tables(fresh, {EVENT_TABLE: "not-a-list"})
        with self.assertRaises(ContractViolation):
            wfstate.load_tables(fresh, {EVENT_TABLE: [42]})

    def test_snapshot_file_round_trip(self):
        flow = synth.clean_flow()
        with tempfile.TemporaryDirectory() as tmp:
            path = wfstate.write_snapshot(Path(tmp) / "synth_state.json", flow.adapter)
            payload = wfstate.read_snapshot(path)
        self.assertEqual(payload, wfstate.dump_tables(flow.adapter))

    def test_dump_covers_all_restore_tables(self):
        flow, _ = prepared_flow()
        payload = wfstate.dump_tables(flow.adapter)
        self.assertEqual(set(payload), set(wfstate.RESTORE_TABLES))


class TestPartialTimeFidelity(unittest.TestCase):
    """时间不完整的行：表格列保留交班人**原始文本**，重启后复验得出同样的 E002。"""

    def test_partial_time_row_keeps_raw_text_and_same_alarm_after_restart(self):
        flow = synth.make_flow()
        flow.submit([synth.row("SYNTH-EVT-0001", occurred_at="2026-01-02")])
        row = flow.intake.event_table.find_row("SYNTH-EVT-0001")
        self.assertEqual(row.fields[EVENT_COLUMNS["occurred_at"]], "2026-01-02")
        self.assertIsNone(flow.intake.event_table.get("SYNTH-EVT-0001").occurred_at)

        payload = wfstate.dump_tables(flow.adapter)
        bundle = wfstate.restart(
            payload, adapter=synth.make_adapter(), shift_id=synth.SHIFT_ID, clock=synth.fixed_clock()
        )
        self.assertEqual(bundle.issues, ())
        self.assertEqual([event.event_id for event in bundle.intake.events], ["SYNTH-EVT-0001"])
        self.assertEqual(bundle.intake.ledger.counts_by_rule()["E002"], 1)
        self.assertEqual(
            bundle.intake.event_table.find_row("SYNTH-EVT-0001").fields[EVENT_COLUMNS["occurred_at"]],
            "2026-01-02",
        )

    def test_partial_time_row_can_be_repaired_after_restart(self):
        flow = synth.make_flow()
        flow.submit([synth.row("SYNTH-EVT-0001", occurred_at="2026-01-02")])
        payload = wfstate.dump_tables(flow.adapter)
        bundle = wfstate.restart(
            payload, adapter=synth.make_adapter(), shift_id=synth.SHIFT_ID, clock=synth.fixed_clock()
        )
        bundle.intake.repair_time("SYNTH-EVT-0001", "2026-01-02 09:15")
        self.assertEqual(
            bundle.intake.event_table.find_row("SYNTH-EVT-0001").fields[EVENT_COLUMNS["occurred_at"]],
            "2026-01-02 09:15",
        )
        self.assertEqual(bundle.intake.ledger.open_count, 0)

    def test_pending_write_keeps_raw_text_too(self):
        flow = synth.make_flow()
        flow.adapter.queue_failure("create", "unknown")
        flow.submit([synth.row("SYNTH-EVT-0001", occurred_at="2026-01-02")])
        self.assertEqual(flow.intake.reconcile("SYNTH-EVT-0001"), "not_applied")
        flow.intake.retry_persist("SYNTH-EVT-0001")
        row = flow.intake.event_table.find_row("SYNTH-EVT-0001")
        self.assertEqual(row.fields[EVENT_COLUMNS["occurred_at"]], "2026-01-02")


if __name__ == "__main__":
    unittest.main()
