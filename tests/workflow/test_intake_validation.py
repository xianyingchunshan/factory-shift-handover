"""阶段① 输入校验测试（issue #9 第 1 段）。

覆盖验收 L1/L2：九类字段与九类取值、四条告警（E001 拒收不落表；E002/E003/E004 落表告警）、
关键级强制升级两条规则不可降（原值留痕）、错人/缺字段/重复提交（DUPLICATE_SHIFT）不放行、
受理不明只回查不重放。
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

from contracts.enums import (  # noqa: E402
    CATEGORY_ORDER,
    Completeness,
    EventCategory,
    EventStatus,
    Severity,
    WritebackStatus,
)
from contracts.errors import (  # noqa: E402
    DUPLICATE_SHIFT,
    WRITE_UNKNOWN,
    ContractViolation,
)
from contracts.events import REQUIRED_ENUM_FIELDS  # noqa: E402
from contracts.events import HandoverEvent  # noqa: E402
from integrations.aitable.tables import OUTCOME_REJECTED, OUTCOME_STORED  # noqa: E402

import wf_synth as synth  # noqa: E402


class TestNineCategories(unittest.TestCase):
    """九类统一字段：九类取值都能录入并落表，未知取值不放行。"""

    def setUp(self):
        self.flow = synth.make_flow()

    def test_all_nine_categories_are_accepted_and_stored(self):
        outcomes = self.flow.submit(
            synth.row(f"SYNTH-EVT-{index:04d}", category=category)
            for index, category in enumerate(CATEGORY_ORDER, start=1)
        )
        self.assertEqual(len(outcomes), 9)
        self.assertTrue(all(outcome.stored for outcome in outcomes))
        self.assertEqual(self.flow.intake.event_table.count(synth.SHIFT_ID), 9)
        stored = {event.category for event in self.flow.intake.store.events}
        self.assertEqual(stored, {category.value for category in CATEGORY_ORDER})

    def test_unified_fields_survive_the_round_trip(self):
        self.flow.submit(
            [
                synth.row(
                    "SYNTH-EVT-0001",
                    owner={"source": "eam", "user_id": "SYNTH-uid-owner"},
                    ref_no="SYNTH-DEFECT-001",
                    note="SYNTH 备注",
                )
            ]
        )
        loaded = self.flow.intake.event_table.get("SYNTH-EVT-0001")
        self.assertEqual(loaded.description, "SYNTH 事项 SYNTH-EVT-0001")
        self.assertEqual(loaded.ref_no, "SYNTH-DEFECT-001")
        self.assertEqual(loaded.note, "SYNTH 备注")
        self.assertEqual(loaded.owner.user_id, "SYNTH-uid-owner")

    def test_unknown_category_is_refused_and_not_stored(self):
        with self.assertRaises(ContractViolation):
            self.flow.submit([synth.row("SYNTH-EVT-0001", category="SYNTH-第十类")])
        self.assertEqual(self.flow.intake.event_table.count(synth.SHIFT_ID), 0)


class TestFourAlarms(unittest.TestCase):
    """四条告警：E001 拒收不落表；E002/E003/E004 落表 + 告警。"""

    def setUp(self):
        self.flow = synth.make_flow()

    def test_e001_missing_time_is_rejected_and_never_stored(self):
        outcome = self.flow.submit([synth.row("SYNTH-EVT-0001", occurred_at=None)])[0]
        self.assertTrue(outcome.rejected)
        self.assertFalse(outcome.stored)
        self.assertIsNone(outcome.event)
        self.assertEqual([alarm.rule for alarm in outcome.alarms], ["E001"])
        # E001 不落表：交接事件表没有这一行，也不出现在内存事件集合里
        self.assertEqual(self.flow.intake.event_table.count(synth.SHIFT_ID), 0)
        self.assertIsNone(self.flow.intake.event_table.get("SYNTH-EVT-0001"))
        self.assertEqual(self.flow.intake.events, ())
        self.assertEqual(len(self.flow.intake.journal.rejected_entries(synth.SHIFT_ID)), 1)
        self.assertEqual(self.flow.intake.ledger.open_count, 1)

    def test_e001_row_without_time_field_at_all_is_also_rejected(self):
        outcome = self.flow.submit([synth.row("SYNTH-EVT-0001", occurred_at=synth.OMIT)])[0]
        self.assertTrue(outcome.rejected)
        self.assertEqual(self.flow.intake.event_table.count(synth.SHIFT_ID), 0)

    def test_e002_date_only_is_stored_with_alarm(self):
        outcome = self.flow.submit([synth.row("SYNTH-EVT-0001", occurred_at="2026-01-02")])[0]
        self.assertTrue(outcome.stored)
        self.assertEqual(outcome.event.completeness, Completeness.PARTIAL_TIME.value)
        self.assertIsNone(outcome.event.occurred_at)
        self.assertEqual([alarm.rule for alarm in outcome.alarms], ["E002"])
        self.assertEqual(self.flow.intake.event_table.get("SYNTH-EVT-0001").completeness, "partial_time")

    def test_e003_out_of_window_is_stored_with_alarm(self):
        for moment in ("2026-01-02 07:59", "2026-01-02 20:01"):
            with self.subTest(moment=moment):
                flow = synth.make_flow()
                outcome = flow.submit([synth.row("SYNTH-EVT-0001", occurred_at=moment)])[0]
                self.assertTrue(outcome.stored)
                self.assertEqual(outcome.event.completeness, Completeness.OUT_OF_RANGE.value)
                self.assertEqual([alarm.rule for alarm in outcome.alarms], ["E003"])

    def test_window_edges_are_not_out_of_range(self):
        flow = synth.make_flow()
        outcome = flow.submit([synth.row("SYNTH-EVT-0001", occurred_at="2026-01-02 20:00")])[0]
        self.assertEqual(outcome.alarms, ())
        self.assertEqual(outcome.event.completeness, Completeness.COMPLETE.value)

    def test_e004_missing_key_fields_produce_one_alarm_per_field(self):
        outcome = self.flow.submit(
            [synth.row("SYNTH-EVT-0001", category=synth.OMIT, severity=synth.OMIT, status=synth.OMIT)]
        )[0]
        self.assertTrue(outcome.stored)
        self.assertEqual(outcome.event.completeness, Completeness.MISSING_REQUIRED.value)
        self.assertEqual([alarm.rule for alarm in outcome.alarms], ["E004", "E004", "E004"])
        self.assertEqual(
            sorted(alarm.field for alarm in outcome.alarms), sorted(REQUIRED_ENUM_FIELDS)
        )

    def test_e004_each_missing_field_individually(self):
        for field in REQUIRED_ENUM_FIELDS:
            with self.subTest(field=field):
                flow = synth.make_flow()
                outcome = flow.submit([synth.row("SYNTH-EVT-0001", **{field: synth.OMIT})])[0]
                self.assertTrue(outcome.stored)
                self.assertEqual([alarm.field for alarm in outcome.alarms], [field])

    def test_four_alarms_are_reproducible_at_once(self):
        flow = synth.alarm_flow()
        counts = flow.intake.ledger.counts_by_rule()
        self.assertEqual(counts, {"E001": 1, "E002": 1, "E003": 1, "E004": 1})
        self.assertEqual(flow.intake.event_table.count(synth.SHIFT_ID), 4)
        self.assertEqual(len(flow.intake.journal.rejected_entries(synth.SHIFT_ID)), 1)
        self.assertEqual(flow.intake.ledger.open_count, 4)

    def test_alarm_details_are_recorded_for_review(self):
        flow = synth.alarm_flow()
        alarms = {alarm.rule: alarm for alarm in flow.intake.ledger.open_alarms()}
        self.assertIn("2026-01-02", alarms["E002"].detail)
        self.assertIn("不在班次区间", alarms["E003"].detail)
        self.assertIn("severity", alarms["E004"].detail)
        self.assertIn("拒收", alarms["E001"].detail)


class TestSeverityEscalation(unittest.TestCase):
    """关键级强制升级两条规则不可降，原值留痕。"""

    def test_major_category_forces_critical(self):
        flow = synth.make_flow()
        outcome = flow.submit(
            [synth.row("SYNTH-EVT-0001", category="重大事项", severity="normal")]
        )[0]
        event = outcome.event
        self.assertEqual(event.severity, Severity.CRITICAL.value)
        self.assertTrue(event.severity_forced)
        self.assertEqual(event.severity_forced_reason, "category_major")
        self.assertEqual(event.original_severity, Severity.NORMAL.value)
        self.assertTrue(event.is_critical)

    def test_transferred_status_forces_critical(self):
        flow = synth.make_flow()
        outcome = flow.submit(
            [synth.row("SYNTH-EVT-0001", status="移交接班人", severity="normal")]
        )[0]
        event = outcome.event
        self.assertEqual(event.severity, Severity.CRITICAL.value)
        self.assertEqual(event.severity_forced_reason, "status_transferred")
        self.assertEqual(event.original_severity, Severity.NORMAL.value)

    def test_both_rules_are_stacked_and_never_downgraded(self):
        flow = synth.make_flow()
        outcome = flow.submit(
            [
                synth.row(
                    "SYNTH-EVT-0001",
                    category="重大事项",
                    status="移交接班人",
                    severity="normal",
                )
            ]
        )[0]
        event = outcome.event
        self.assertEqual(event.severity, Severity.CRITICAL.value)
        self.assertEqual(
            event.severity_forced_reason, "category_major+status_transferred"
        )
        self.assertEqual(event.original_severity, "normal")

    def test_requested_critical_cannot_be_downgraded(self):
        flow = synth.make_flow()
        outcome = flow.submit(
            [synth.row("SYNTH-EVT-0001", category="重大事项", severity="critical")]
        )[0]
        self.assertEqual(outcome.event.severity, Severity.CRITICAL.value)
        self.assertTrue(outcome.event.severity_forced)

    def test_normal_event_keeps_requested_severity(self):
        flow = synth.make_flow()
        outcome = flow.submit([synth.row("SYNTH-EVT-0001", severity="normal")])[0]
        self.assertEqual(outcome.event.severity, "normal")
        self.assertFalse(outcome.event.severity_forced)
        self.assertEqual(outcome.event.severity_forced_reason, "")
        self.assertIsNone(outcome.event.original_severity)

    def test_forced_severity_is_persisted_to_the_table(self):
        flow = synth.make_flow()
        flow.submit([synth.row("SYNTH-EVT-0001", category="重大事项", severity="normal")])
        stored = flow.intake.event_table.get("SYNTH-EVT-0001")
        self.assertEqual(stored.severity, "critical")
        self.assertTrue(stored.severity_forced)
        self.assertEqual(stored.original_severity, "normal")

    def test_missing_severity_with_major_category_still_forced(self):
        flow = synth.make_flow()
        outcome = flow.submit(
            [synth.row("SYNTH-EVT-0001", category="重大事项", severity=synth.OMIT)]
        )[0]
        self.assertEqual(outcome.event.severity, "critical")
        self.assertFalse(
            [alarm for alarm in outcome.alarms if alarm.field == "severity"],
            "强制升级后重要级不为空，不应出 E004",
        )

    def test_original_severity_keeps_the_reported_value_through_intake(self):
        """原值留痕必须是**交班人申报值**，不是强制升级后的值。

        契约缺口（已记入 PR 待主控确认项）：``EventStore.intake`` 把
        ``resolve_severity`` 的结果传给 ``HandoverEvent``，该路径下构造函数里的
        ``original_severity`` 会记成强制后的值；本层以输入行的申报值回填。
        直接构造契约对象（不走 intake）时契约自身行为正确——下面两段对照即为证据。
        """
        flow = synth.make_flow()
        outcome = flow.submit(
            [synth.row("SYNTH-EVT-0001", category="重大事项", severity="normal")]
        )[0]
        self.assertEqual(outcome.event.original_severity, "normal")
        self.assertNotEqual(outcome.event.original_severity, outcome.event.severity)

        direct = HandoverEvent(
            event_id="SYNTH-EVT-0002",
            shift_id=synth.SHIFT_ID,
            category="major",
            description="SYNTH 直构造对照",
            occurred_at=synth.at(9, 0),
            severity="normal",
            status="done",
        )
        self.assertEqual(direct.severity, "critical")
        self.assertEqual(direct.original_severity, "normal")

        restored = flow.intake.event_table.get("SYNTH-EVT-0001")
        self.assertEqual(restored.original_severity, "normal")
        self.assertTrue(restored.severity_forced)


class TestIntakeRefusals(unittest.TestCase):
    """错人、缺字段、重复提交不放行。"""

    def setUp(self):
        self.flow = synth.make_flow()

    def test_missing_event_id_is_refused(self):
        with self.assertRaises(ContractViolation):
            self.flow.submit([synth.row("  ")])
        self.assertEqual(self.flow.intake.event_table.count(synth.SHIFT_ID), 0)

    def test_missing_description_is_refused(self):
        with self.assertRaises(ContractViolation):
            self.flow.submit([synth.row("SYNTH-EVT-0001", description=synth.OMIT)])
        with self.assertRaises(ContractViolation):
            self.flow.submit([synth.row("SYNTH-EVT-0001", description="   ")])
        self.assertEqual(self.flow.intake.event_table.count(synth.SHIFT_ID), 0)

    def test_row_for_another_shift_is_refused(self):
        with self.assertRaises(ContractViolation):
            self.flow.submit([synth.row("SYNTH-EVT-0001", shift_id=synth.OTHER_SHIFT_ID)])
        self.assertEqual(self.flow.intake.event_table.count(synth.SHIFT_ID), 0)

    def test_non_mapping_row_is_refused(self):
        with self.assertRaises(ContractViolation):
            self.flow.intake.submit(["SYNTH-不是映射"])  # type: ignore[list-item]

    def test_duplicate_event_id_does_not_double_write(self):
        first = self.flow.submit([synth.row("SYNTH-EVT-0001")])[0]
        second = self.flow.submit([synth.row("SYNTH-EVT-0001", description="SYNTH 改过的描述")])[0]
        self.assertTrue(first.stored)
        self.assertTrue(second.duplicate)
        self.assertFalse(second.stored if hasattr(second, "stored") else False)
        self.assertEqual(self.flow.intake.event_table.count(synth.SHIFT_ID), 1)
        self.assertEqual(
            self.flow.intake.event_table.get("SYNTH-EVT-0001").description,
            "SYNTH 事项 SYNTH-EVT-0001",
        )
        outcomes = [entry.outcome for entry in self.flow.intake.journal.entries()]
        self.assertEqual(outcomes, [OUTCOME_STORED, "duplicate"])

    def test_rejected_then_reintake_succeeds_and_clears_e001(self):
        rejected = self.flow.submit([synth.row("SYNTH-EVT-0001", occurred_at=None)])[0]
        self.assertTrue(rejected.rejected)
        self.assertEqual(
            [entry.outcome for entry in self.flow.intake.journal.entries()], [OUTCOME_REJECTED]
        )
        accepted = self.flow.submit(
            [synth.row("SYNTH-EVT-0001", occurred_at="2026-01-02 09:40")]
        )[0]
        self.assertTrue(accepted.stored)
        self.assertEqual(self.flow.intake.event_table.count(synth.SHIFT_ID), 1)
        self.assertFalse(self.flow.intake.ledger.has_open)
        cleared = self.flow.intake.alarm_journal.cleared_ids(synth.SHIFT_ID)
        self.assertEqual(len(cleared), 1)
        self.assertTrue(cleared[0].startswith("E001:"))


class TestShiftRegistration(unittest.TestCase):
    """同班同线不重复建班次（DUPLICATE_SHIFT）。"""

    def test_first_create_writes_one_row(self):
        flow = synth.make_flow(create_shift=False)
        result = flow.intake.create_shift()
        self.assertTrue(result.created)
        self.assertFalse(result.idempotent_noop)
        self.assertEqual(flow.intake.shift_table.count(), 1)

    def test_same_shift_id_is_idempotent_noop(self):
        flow = synth.make_flow(create_shift=False)
        flow.intake.create_shift()
        again = flow.intake.create_shift()
        self.assertFalse(again.created)
        self.assertTrue(again.idempotent_noop)
        self.assertEqual(flow.intake.shift_table.count(), 1)

    def test_same_slot_with_another_shift_id_is_duplicate(self):
        flow = synth.make_flow()
        other = synth.make_shift(shift_id=synth.OTHER_SHIFT_ID)
        rival = synth.make_flow(shift=other, adapter=flow.adapter, create_shift=False)
        with synth.expect_code(DUPLICATE_SHIFT):
            rival.intake.create_shift()
        self.assertEqual(flow.intake.shift_table.count(), 1)

    def test_duplicate_shift_id_with_another_slot_is_refused(self):
        flow = synth.make_flow()
        rival = synth.make_flow(
            shift=synth.make_shift(shift_name="late"), adapter=flow.adapter, create_shift=False
        )
        with self.assertRaises(ContractViolation):
            rival.intake.create_shift()
        self.assertEqual(flow.intake.shift_table.count(), 1)

    def test_other_line_same_day_is_allowed(self):
        flow = synth.make_flow()
        other_line = synth.make_flow(
            shift=synth.make_shift(shift_id=synth.OTHER_SHIFT_ID, handover_line="SYNTH-LINE-B"),
            adapter=flow.adapter,
            create_shift=False,
        )
        result = other_line.intake.create_shift()
        self.assertTrue(result.created)
        self.assertEqual(flow.intake.shift_table.count(), 2)


class TestWriteUnknownHandling(unittest.TestCase):
    """外部写入受理不明：只回查，不重放。"""

    def setUp(self):
        self.flow = synth.make_flow()
        self.flow.intake.create_shift()

    def test_create_unknown_marks_pending_and_does_not_double_write(self):
        self.flow.adapter.queue_failure("create", "unknown")
        outcome = self.flow.submit([synth.row("SYNTH-EVT-0001")])[0]
        self.assertTrue(outcome.write_unknown)
        self.assertFalse(outcome.stored)
        self.assertEqual(self.flow.intake.event_table.count(synth.SHIFT_ID), 0)
        self.assertEqual(self.flow.intake.pending_events(), ("SYNTH-EVT-0001",))

    def test_unknown_cannot_be_replayed_before_recheck(self):
        self.flow.adapter.queue_failure("create", "unknown")
        self.flow.submit([synth.row("SYNTH-EVT-0001")])
        with synth.expect_code(WRITE_UNKNOWN):
            self.flow.intake.retry_persist("SYNTH-EVT-0001")

    def test_recheck_after_real_write_returns_verified(self):
        self.flow.submit([synth.row("SYNTH-EVT-0001")])
        self.assertEqual(
            self.flow.intake.reconcile("SYNTH-EVT-0001"), WritebackStatus.VERIFIED.value
        )

    def test_unknown_then_recheck_not_applied_then_replay(self):
        """受理不明 → 回查确认未生效 → 才允许补写（不盲目重发）。"""
        self.flow.adapter.queue_failure("create", "unknown")
        outcome = self.flow.submit([synth.row("SYNTH-EVT-0001")])[0]
        self.assertTrue(outcome.write_unknown)
        self.assertEqual(
            self.flow.intake.reconcile("SYNTH-EVT-0001"), WritebackStatus.NOT_APPLIED.value
        )
        record_id = self.flow.intake.retry_persist("SYNTH-EVT-0001")
        self.assertTrue(record_id)
        self.assertEqual(self.flow.intake.event_table.count(synth.SHIFT_ID), 1)
        self.assertEqual(self.flow.intake.pending_events(), ())

    def test_unknown_but_row_actually_landed_is_verified_on_recheck(self):
        """受理不明但写入其实已生效：回查得 verified，不得补写第二行。"""
        from integrations.aitable.cells import event_to_fields
        from integrations.aitable.tables import EVENT_TABLE

        self.flow.adapter.queue_failure("create", "unknown")
        outcome = self.flow.submit([synth.row("SYNTH-EVT-0001")])[0]
        self.assertTrue(outcome.write_unknown)
        # 模拟"其实已经写进表里，只是客户端没拿到确认"
        self.flow.adapter.create_record(EVENT_TABLE, event_to_fields(outcome.event))
        self.assertEqual(
            self.flow.intake.reconcile("SYNTH-EVT-0001"), WritebackStatus.VERIFIED.value
        )
        self.assertEqual(self.flow.intake.pending_events(), ())
        with self.assertRaises(ContractViolation):
            self.flow.intake.retry_persist("SYNTH-EVT-0001")
        self.assertEqual(self.flow.intake.event_table.count(synth.SHIFT_ID), 1)

    def test_hard_rejection_is_marked_and_recoverable(self):
        self.flow.adapter.queue_failure("create", "rejected")
        outcome = self.flow.submit([synth.row("SYNTH-EVT-0001")])[0]
        self.assertTrue(outcome.write_failed)
        problems = self.flow.intake.verify_source_consistency()
        self.assertTrue(problems, "受理不明未处置前，源一致性必须报问题")
        self.flow.intake.reconcile("SYNTH-EVT-0001")
        self.flow.intake.retry_persist("SYNTH-EVT-0001")
        self.assertEqual(self.flow.intake.verify_source_consistency(), ())

    def test_retry_without_pending_is_refused(self):
        self.flow.submit([synth.row("SYNTH-EVT-0001")])
        with self.assertRaises(ContractViolation):
            self.flow.intake.retry_persist("SYNTH-EVT-0001")


class TestRepairPaths(unittest.TestCase):
    """补全/复核/补字段：改内存 + 更新表格行 + 清告警（留痕）。"""

    def test_repair_time_updates_row_and_clears_alarm(self):
        flow = synth.make_flow()
        flow.submit([synth.row("SYNTH-EVT-0001", occurred_at="2026-01-02")])
        event = flow.intake.repair_time("SYNTH-EVT-0001", "2026-01-02 09:45")
        self.assertEqual(event.completeness, Completeness.COMPLETE.value)
        self.assertFalse(flow.intake.ledger.has_open)
        stored = flow.intake.event_table.get("SYNTH-EVT-0001")
        self.assertEqual(stored.occurred_at, synth.at(9, 45))
        self.assertEqual(stored.completeness, "complete")
        self.assertEqual(len(flow.intake.alarm_journal.entries()), 1)

    def test_review_out_of_range_keeps_time_and_clears_alarm(self):
        flow = synth.make_flow()
        flow.submit([synth.row("SYNTH-EVT-0001", occurred_at="2026-01-02 22:10")])
        event = flow.intake.review_out_of_range("SYNTH-EVT-0001")
        self.assertEqual(event.occurred_at, synth.at(22, 10))
        self.assertIn("复核", event.note)
        self.assertFalse(flow.intake.ledger.has_open)

    def test_fill_required_clears_e004(self):
        flow = synth.make_flow()
        flow.submit([synth.row("SYNTH-EVT-0001", status=synth.OMIT)])
        event = flow.intake.fill_required("SYNTH-EVT-0001", status="in_progress")
        self.assertEqual(event.status, EventStatus.IN_PROGRESS.value)
        self.assertFalse(flow.intake.ledger.has_open)

    def test_repair_missing_event_is_refused(self):
        flow = synth.make_flow()
        with self.assertRaises(ContractViolation):
            flow.intake.repair_time("SYNTH-EVT-9999", "2026-01-02 09:00")

    def test_clear_unknown_alarm_is_refused(self):
        flow = synth.make_flow()
        with self.assertRaises(ContractViolation):
            flow.intake.clear_alarm("E002:SYNTH-SHIFT-0001:SYNTH-EVT-0009:occurred_at")


if __name__ == "__main__":
    unittest.main()
