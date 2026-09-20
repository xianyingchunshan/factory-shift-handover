"""契约测试（L1 核心）：四条完整性告警与告警账本（issue #6 §3）。

- E001 拒收（不落表）
- E002 / E003 / E004 必告警
- 告警可清除、可断言；未清告警禁止提交与出清单
- 重复录入幂等（不双写、不重复出告警）
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

from contracts.alarms import AlarmLedger, CompletenessAlarm  # noqa: E402
from contracts.errors import (  # noqa: E402
    ALARM_NOT_CLEARED,
    E002,
    E003,
    E004,
    ContractViolation,
)

import synth  # noqa: E402


class TestE001MissingTimeRejected(unittest.TestCase):
    def test_missing_time_is_rejected_and_not_stored(self):
        _, ledger, store = synth.make_store()
        result = store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at=synth.OMIT))
        self.assertTrue(result.rejected)
        self.assertIsNone(result.event)
        self.assertFalse(result.stored)
        self.assertEqual(store.event_count, 0, "E001 拒收，不落表")

    def test_blank_time_string_is_also_e001(self):
        _, ledger, store = synth.make_store()
        result = store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="   "))
        self.assertTrue(result.rejected)
        self.assertEqual([alarm.rule for alarm in result.alarms], ["E001"])

    def test_e001_alarm_records_rule_field_and_event(self):
        _, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at=synth.OMIT))
        alarm = ledger.open_alarms()[0]
        self.assertEqual(alarm.rule, "E001")
        self.assertEqual(alarm.field, "occurred_at")
        self.assertEqual(alarm.event_id, "SYNTH-EVT-0001")
        self.assertEqual(alarm.shift_id, synth.SHIFT_ID)
        self.assertTrue(alarm.is_rejecting)
        self.assertTrue(alarm.is_open)

    def test_rejected_row_does_not_appear_in_events(self):
        _, _, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at=synth.OMIT))
        store.intake(synth.raw_event("SYNTH-EVT-0002"))
        self.assertEqual(store.event_count, 1)
        self.assertIsNone(store.get("SYNTH-EVT-0001"))
        self.assertIsNotNone(store.get("SYNTH-EVT-0002"))


class TestE002PartialTime(unittest.TestCase):
    def test_date_only_is_stored_with_alarm_and_no_time(self):
        _, ledger, store = synth.make_store()
        result = store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02"))
        self.assertFalse(result.rejected)
        self.assertEqual(result.event.completeness, "partial_time")
        self.assertIsNone(result.event.occurred_at, "有日期无时:分，不臆造 00:00")
        self.assertEqual([alarm.rule for alarm in result.alarms], ["E002"])
        self.assertTrue(ledger.has_open)

    def test_completion_then_clear_makes_event_reportable(self):
        _, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02"))
        event = store.get("SYNTH-EVT-0001")
        alarm = ledger.open_alarms()[0]
        event.resolve_time("2026-01-02 09:40")
        ledger.clear(alarm.alarm_id, synth.at(20, 1))
        self.assertEqual(event.completeness, "complete")
        self.assertTrue(event.can_enter_report())
        self.assertFalse(ledger.has_open)

    def test_completion_without_minute_is_rejected(self):
        _, _, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02"))
        with self.assertRaises(ContractViolation):
            store.get("SYNTH-EVT-0001").resolve_time("2026-01-02")


class TestE003OutOfRange(unittest.TestCase):
    def test_time_after_shift_end_alarms(self):
        _, ledger, store = synth.make_store()
        result = store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02 23:10"))
        self.assertEqual(result.event.completeness, "out_of_range")
        self.assertEqual([alarm.rule for alarm in result.alarms], ["E003"])
        self.assertIn("23:10", result.alarms[0].detail)

    def test_time_before_shift_start_alarms(self):
        _, _, store = synth.make_store()
        result = store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02 07:10"))
        self.assertEqual([alarm.rule for alarm in result.alarms], ["E003"])

    def test_boundary_times_are_inside_the_window(self):
        _, ledger, store = synth.make_store()
        start = store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02 08:00"))
        end = store.intake(synth.raw_event("SYNTH-EVT-0002", occurred_at="2026-01-02 20:00"))
        self.assertEqual(start.alarms, ())
        self.assertEqual(end.alarms, ())
        self.assertFalse(ledger.has_open)

    def test_review_clears_out_of_range_state(self):
        _, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02 23:10"))
        event = store.get("SYNTH-EVT-0001")
        event.mark_reviewed()
        ledger.clear_event(event.event_id, synth.at(20, 5))
        self.assertEqual(event.completeness, "complete")
        self.assertTrue(event.can_enter_report())
        self.assertFalse(ledger.has_open)


class TestE004MissingRequiredFields(unittest.TestCase):
    def test_each_missing_required_field_gets_its_own_alarm(self):
        _, ledger, store = synth.make_store()
        result = store.intake(
            synth.raw_event("SYNTH-EVT-0001", category=synth.OMIT, severity=synth.OMIT)
        )
        self.assertEqual(result.event.completeness, "missing_required")
        self.assertEqual([alarm.rule for alarm in result.alarms], [E004, E004])
        self.assertEqual({alarm.field for alarm in result.alarms}, {"category", "severity"})
        self.assertEqual(ledger.open_count, 2)

    def test_blank_string_counts_as_missing(self):
        _, _, store = synth.make_store()
        result = store.intake(synth.raw_event("SYNTH-EVT-0001", category="", status="  "))
        self.assertEqual({alarm.field for alarm in result.alarms}, {"category", "status"})

    def test_no_e004_when_all_required_fields_present(self):
        _, ledger, store = synth.make_store()
        result = store.intake(synth.raw_event("SYNTH-EVT-0001"))
        self.assertEqual(result.alarms, ())
        self.assertEqual(result.event.completeness, "complete")
        self.assertFalse(ledger.has_open)

    def test_time_rule_wins_over_missing_required_marker(self):
        _, _, store = synth.make_store()
        result = store.intake(
            synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02 23:10", category=synth.OMIT)
        )
        self.assertEqual(result.event.completeness, "out_of_range", "时间类规则优先级更高")
        self.assertEqual([alarm.rule for alarm in result.alarms], ["E003", E004])


class TestIntakeGuards(unittest.TestCase):
    def test_duplicate_event_id_is_idempotent(self):
        _, ledger, store = synth.make_store()
        row = synth.raw_event("SYNTH-EVT-0001")
        first = store.intake(row)
        before = len(ledger.all_alarms())
        again = store.intake(row)
        self.assertTrue(again.duplicate)
        self.assertIs(again.event, first.event)
        self.assertEqual(store.event_count, 1, "重复录入不双写")
        self.assertEqual(len(ledger.all_alarms()), before, "重复录入不重复出告警")

    def test_event_from_another_shift_is_rejected(self):
        _, _, store = synth.make_store()
        with self.assertRaises(ContractViolation):
            store.intake(synth.raw_event("SYNTH-EVT-0001", shift_id="SYNTH-SHIFT-9999"))

    def test_row_without_event_id_is_rejected(self):
        _, _, store = synth.make_store()
        with self.assertRaises(ContractViolation):
            store.intake({"description": "SYNTH 无 ID"})

    def test_unparseable_time_is_a_caller_defect(self):
        _, _, store = synth.make_store()
        with self.assertRaises(ContractViolation):
            store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="昨天下午三点"))


class TestAlarmLedger(unittest.TestCase):
    def test_alarm_id_is_stable_per_rule_event_field(self):
        alarm = CompletenessAlarm.make(
            E002,
            shift_id=synth.SHIFT_ID,
            event_id="SYNTH-EVT-0001",
            field="occurred_at",
            detail="合成",
            created_at=synth.STAMP,
        )
        self.assertEqual(alarm.alarm_id, f"E002:{synth.SHIFT_ID}:SYNTH-EVT-0001:occurred_at")
        again = CompletenessAlarm.make(
            E002,
            shift_id=synth.SHIFT_ID,
            event_id="SYNTH-EVT-0001",
            field="occurred_at",
            detail="合成（重复）",
            created_at=synth.at(20, 30),
        )
        self.assertEqual(alarm.alarm_id, again.alarm_id)

    def test_unknown_rule_is_a_caller_defect(self):
        with self.assertRaises(ContractViolation):
            CompletenessAlarm.make(
                "E009",
                shift_id=synth.SHIFT_ID,
                event_id=None,
                field="occurred_at",
                detail="合成",
                created_at=synth.STAMP,
            )

    def test_record_is_idempotent(self):
        ledger = AlarmLedger(shift_id=synth.SHIFT_ID)
        alarm = CompletenessAlarm.make(
            E003,
            shift_id=synth.SHIFT_ID,
            event_id="SYNTH-EVT-0001",
            field="occurred_at",
            detail="合成",
            created_at=synth.STAMP,
        )
        ledger.record(alarm)
        ledger.record(alarm)
        self.assertEqual(len(ledger.all_alarms()), 1)

    def test_record_rejects_alarm_from_another_shift(self):
        ledger = AlarmLedger(shift_id=synth.SHIFT_ID)
        foreign = CompletenessAlarm.make(
            E004,
            shift_id="SYNTH-SHIFT-9999",
            event_id="SYNTH-EVT-0001",
            field="status",
            detail="合成",
            created_at=synth.STAMP,
        )
        with self.assertRaises(ContractViolation):
            ledger.record(foreign)

    def test_clear_is_idempotent_and_keeps_first_time(self):
        _, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02"))
        alarm = ledger.open_alarms()[0]
        ledger.clear(alarm.alarm_id, synth.at(20, 1))
        self.assertTrue(alarm.is_cleared)
        ledger.clear(alarm.alarm_id, synth.at(20, 30))
        self.assertEqual(alarm.cleared_at.hour, 20)
        self.assertEqual(alarm.cleared_at.minute, 1)

    def test_clear_unknown_alarm_is_a_caller_defect(self):
        ledger = AlarmLedger(shift_id=synth.SHIFT_ID)
        with self.assertRaises(ContractViolation):
            ledger.clear("E002:未登记:occurred_at", synth.STAMP)

    def test_clear_event_clears_all_open_alarms_of_that_event(self):
        _, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", category=synth.OMIT, severity=synth.OMIT))
        self.assertEqual(ledger.open_count, 2)
        ledger.clear_event("SYNTH-EVT-0001", synth.at(20, 1))
        self.assertEqual(ledger.open_count, 0)

    def test_counts_by_rule_and_alarm_count(self):
        _, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at=synth.OMIT))
        store.intake(synth.raw_event("SYNTH-EVT-0002", occurred_at="2026-01-02"))
        counts = ledger.counts_by_rule()
        self.assertEqual(counts["E001"], 1)
        self.assertEqual(counts["E002"], 1)
        self.assertEqual(ledger.alarm_count, 2)

    def test_require_clear_raises_alarm_not_cleared_with_detail(self):
        _, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at=synth.OMIT))
        with synth.expect_code(ALARM_NOT_CLEARED):
            ledger.require_clear()
        try:
            ledger.require_clear()
        except Exception as exc:  # noqa: BLE001 - 断言错误内容
            self.assertEqual(exc.detail["open_count"], 1)
            self.assertEqual(exc.detail["rules"], ["E001"])
        synth.clear_all(ledger)
        ledger.require_clear()

    def test_summary_reports_total_complete_rate_and_alarm_count(self):
        _, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001"))
        store.intake(synth.raw_event("SYNTH-EVT-0002", occurred_at="2026-01-02"))
        summary = ledger.summary(
            event_total=store.event_count, event_complete=store.complete_count
        )
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["complete"], 1)
        self.assertEqual(summary["time_complete_rate"], 0.5)
        self.assertEqual(summary["alarm_count"], 1)

    def test_held_events_tracked_until_repaired(self):
        _, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", occurred_at="2026-01-02"))
        self.assertEqual([e.event_id for e in store.held_events()], ["SYNTH-EVT-0001"])
        store.get("SYNTH-EVT-0001").resolve_time("2026-01-02 09:40")
        synth.clear_all(ledger)
        self.assertEqual(store.held_events(), ())
        store.require_all_report_ready()


if __name__ == "__main__":
    unittest.main()
