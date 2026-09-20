"""契约测试：班次 ShiftRecord（issue #6 §1，SPEC §5 防重与配置快照）。"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.enums import ShiftStatus  # noqa: E402
from contracts.errors import (  # noqa: E402
    ALARM_NOT_CLEARED,
    DUPLICATE_SHIFT,
    ContractViolation,
)
from contracts.shift import ConfigSnapshot, ShiftRecord, ShiftRegistry, new_shift  # noqa: E402

import synth  # noqa: E402


class TestShiftRecordValidation(unittest.TestCase):
    def test_new_shift_defaults_to_draft_and_locks_config(self):
        shift = synth.make_shift()
        self.assertEqual(shift.status, ShiftStatus.DRAFT)
        self.assertEqual(shift.idempotency_key, (synth.LINE, "2026-01-02", "early"))
        self.assertIsNotNone(shift.config_snapshot)
        self.assertEqual(shift.config_snapshot.critical_standard, ("重大事项", "移交接班人"))
        self.assertEqual(shift.config_revision, 0)

    def test_shift_name_accepts_chinese_alias(self):
        shift = synth.make_shift(shift_name="早")
        self.assertEqual(shift.shift_name, "early")

    def test_unknown_shift_name_is_a_caller_defect(self):
        with self.assertRaises(ContractViolation):
            synth.make_shift(shift_name="夜班")

    def test_end_time_must_be_after_start_time(self):
        with self.assertRaises(ContractViolation):
            synth.make_shift(start_time=synth.at(20), end_time=synth.at(8))

    def test_shift_date_must_fall_inside_the_window(self):
        with self.assertRaises(ContractViolation):
            synth.make_shift(shift_date=date(2026, 1, 5))

    def test_night_shift_across_midnight_is_allowed(self):
        shift = synth.make_shift(
            shift_date=date(2026, 1, 2),
            shift_name="late",
            start_time=synth.at(20, day=2),
            end_time=synth.at(8, day=3),
        )
        self.assertTrue(shift.contains(synth.at(23, 30, day=2)))
        self.assertTrue(shift.contains(synth.at(7, 0, day=3)))
        self.assertFalse(shift.contains(synth.at(9, 0, day=3)))

    def test_contains_is_closed_on_boundaries(self):
        shift = synth.make_shift()
        self.assertTrue(shift.contains(synth.START))
        self.assertTrue(shift.contains(synth.END))
        self.assertFalse(shift.contains(synth.at(7, 59)))

    def test_blank_shift_id_rejected(self):
        with self.assertRaises(ContractViolation):
            synth.make_shift(shift_id="  ")


class TestShiftStatusMachine(unittest.TestCase):
    def test_forward_path_draft_submitted_confirmed_archived(self):
        shift = synth.make_shift()
        self.assertEqual(shift.transition("submitted").status, ShiftStatus.SUBMITTED)
        self.assertEqual(shift.transition("confirmed").status, ShiftStatus.CONFIRMED)
        self.assertEqual(shift.transition("archived").status, ShiftStatus.ARCHIVED)

    def test_skipping_a_state_is_rejected(self):
        shift = synth.make_shift()
        with self.assertRaises(ContractViolation):
            shift.transition("confirmed")

    def test_submit_is_blocked_while_alarms_are_open(self):
        shift = synth.make_shift()
        with synth.expect_code(ALARM_NOT_CLEARED):
            shift.transition("submitted", has_open_alarms=True)
        self.assertEqual(shift.status, ShiftStatus.DRAFT)

    def test_blocked_requires_open_alarms(self):
        shift = synth.make_shift()
        with self.assertRaises(ContractViolation):
            shift.transition("blocked", has_open_alarms=False)

    def test_blocked_then_cleared_returns_to_previous_status(self):
        shift = synth.make_shift()
        shift.transition("submitted")
        shift.transition("blocked", has_open_alarms=True)
        self.assertTrue(shift.is_blocked)
        self.assertEqual(shift.blocked_from, ShiftStatus.SUBMITTED)
        shift.suspend_for_alarms(False)
        self.assertEqual(shift.status, ShiftStatus.SUBMITTED)

    def test_suspend_for_open_alarms_is_idempotent(self):
        shift = synth.make_shift()
        shift.suspend_for_alarms(True)
        shift.suspend_for_alarms(True)
        self.assertEqual(shift.status, ShiftStatus.BLOCKED)
        self.assertEqual(shift.blocked_from, ShiftStatus.DRAFT)

    def test_same_status_transition_is_a_noop(self):
        shift = synth.make_shift()
        self.assertEqual(shift.transition("draft").status, ShiftStatus.DRAFT)


class TestConfigSnapshot(unittest.TestCase):
    def test_change_requires_explicit_confirmation(self):
        shift = synth.make_shift()
        snapshot = ConfigSnapshot(
            handover_line=synth.LINE,
            handover_from=synth.FROM,
            handover_to=synth.STRANGER,
            locked_at=synth.START,
        )
        with self.assertRaises(ContractViolation):
            shift.change_config(snapshot, confirmed_by=None)
        self.assertEqual(shift.config_revision, 0)

    def test_change_keeps_history_and_bumps_revision(self):
        shift = synth.make_shift()
        original = shift.config_snapshot
        snapshot = ConfigSnapshot(
            handover_line=synth.LINE,
            handover_from=synth.FROM,
            handover_to=synth.STRANGER,
            locked_at=synth.START,
        )
        shift.change_config(snapshot, confirmed_by=synth.FROM)
        self.assertEqual(shift.config_revision, 1)
        self.assertEqual(shift.snapshot_history, [original])
        self.assertEqual(shift.config_snapshot.handover_to.user_id, synth.STRANGER.user_id)

    def test_relock_after_submit_is_rejected(self):
        shift = synth.make_shift()
        shift.transition("submitted")
        with self.assertRaises(ContractViolation):
            shift.lock_config(
                ConfigSnapshot(
                    handover_line=synth.LINE,
                    handover_from=synth.FROM,
                    handover_to=synth.TO,
                    locked_at=synth.START,
                )
            )

    def test_snapshot_requires_identities(self):
        with self.assertRaises(ContractViolation):
            ConfigSnapshot(
                handover_line=synth.LINE,
                handover_from="  ",
                handover_to=synth.TO,
                locked_at=synth.START,
            )


class TestShiftRegistryDeduplication(unittest.TestCase):
    def test_first_register_creates_one_record(self):
        registry = ShiftRegistry()
        result = registry.register(synth.make_shift())
        self.assertTrue(result.created)
        self.assertFalse(result.idempotent_noop)
        self.assertEqual(registry.created_count, 1)

    def test_resubmit_same_shift_id_is_idempotent_noop(self):
        registry = ShiftRegistry()
        shift = synth.make_shift()
        registry.register(shift)
        again = registry.register(synth.make_shift())
        self.assertFalse(again.created)
        self.assertTrue(again.idempotent_noop)
        self.assertEqual(registry.created_count, 1, "重复提交不双写")
        self.assertIs(again.record, shift)

    def test_same_slot_with_new_shift_id_raises_duplicate_shift(self):
        registry = ShiftRegistry()
        registry.register(synth.make_shift())
        with synth.expect_code(DUPLICATE_SHIFT):
            registry.register(synth.make_shift(shift_id="SYNTH-SHIFT-0002"))
        self.assertEqual(registry.created_count, 1)

    def test_duplicate_error_detail_points_at_existing_shift(self):
        registry = ShiftRegistry()
        registry.register(synth.make_shift())
        try:
            registry.register(synth.make_shift(shift_id="SYNTH-SHIFT-0002"))
        except Exception as exc:  # noqa: BLE001 - 测试断言错误内容
            self.assertEqual(exc.detail["existing_shift_id"], synth.SHIFT_ID)
            self.assertEqual(exc.detail["incoming_shift_id"], "SYNTH-SHIFT-0002")
        else:  # pragma: no cover - 未抛错即失败
            self.fail("应当抛出 DUPLICATE_SHIFT")

    def test_same_slot_for_another_line_is_allowed(self):
        registry = ShiftRegistry()
        registry.register(synth.make_shift())
        other = registry.register(synth.make_shift(shift_id="SYNTH-SHIFT-0003", handover_line="SYNTH-LINE-B"))
        self.assertTrue(other.created)
        self.assertEqual(registry.created_count, 2)

    def test_reusing_shift_id_for_another_slot_is_rejected(self):
        registry = ShiftRegistry()
        registry.register(synth.make_shift())
        with self.assertRaises(ContractViolation):
            registry.register(synth.make_shift(handover_line="SYNTH-LINE-B"))

    def test_registry_lookup_helpers(self):
        registry = ShiftRegistry()
        shift = synth.make_shift()
        registry.register(shift)
        self.assertIs(registry.get(synth.SHIFT_ID), shift)
        self.assertIs(registry.find_same_slot(synth.make_shift()), shift)
        self.assertEqual(len(registry), 1)


class TestShiftSerialization(unittest.TestCase):
    def test_to_dict_exposes_contract_fields(self):
        payload = synth.make_shift().to_dict()
        for key in (
            "shift_id",
            "handover_line",
            "shift_date",
            "shift_name",
            "start_time",
            "end_time",
            "handover_from",
            "handover_to",
            "status",
            "config_snapshot",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["start_time"], "2026-01-02 08:00")
        self.assertEqual(payload["handover_to"]["source"], "eam")

    def test_new_shift_helper_sets_identity_refs(self):
        shift = new_shift(
            shift_id="SYNTH-SHIFT-0009",
            handover_line=synth.LINE,
            shift_date=date(2026, 1, 2),
            shift_name="middle",
            start_time=datetime(2026, 1, 2, 8, 0, tzinfo=synth.SHANGHAI),
            end_time=datetime(2026, 1, 2, 20, 0, tzinfo=synth.SHANGHAI),
            handover_from=synth.FROM,
            handover_to=synth.TO,
        )
        self.assertIsInstance(shift, ShiftRecord)
        self.assertEqual(shift.handover_from.same_person(synth.FROM), True)


if __name__ == "__main__":
    unittest.main()
