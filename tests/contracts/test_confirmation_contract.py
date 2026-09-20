"""契约测试：确认回写 Confirmation（issue #6 §5）。

覆盖：确认单元粒度（关键级逐项 / 一般事务整单）、状态机
pending → confirmed → written_back、无回读不得 confirmed、
未确认不得回写、unknown 只回查不重放、缺身份/错人拒绝（AUTH_REQUIRED）。
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

from contracts.confirmation import (  # noqa: E402
    Confirmation,
    ConfirmationUnit,
    ReadbackEvidence,
    build_confirmation_plan,
    plan_from_units,
    require_operator_for,
)
from contracts.errors import (  # noqa: E402
    AUTH_REQUIRED,
    NOT_CONFIRMED,
    WRITE_UNKNOWN,
    ContractViolation,
)
from contracts.report import build_report  # noqa: E402

import synth  # noqa: E402


def make_plan(todo_id_factory=None):
    """合成：1 条关键级（逐项待办）+ 1 条一般事项（整单待办）。"""
    shift, ledger, store = synth.make_store()
    store.intake(
        synth.raw_event("SYNTH-EVT-0001", category="equipment", status="done")
    )
    store.intake(
        synth.raw_event(
            "SYNTH-EVT-0002",
            category="重大事项",
            status="移交接班人",
            occurred_at="2026-01-02 14:20",
        )
    )
    factory = todo_id_factory
    if factory is None:
        factory = lambda unit: "SYNTH-TODO-" + unit.unit_id[-4:]  # noqa: E731
    report = build_report(
        shift, store, ledger, generated_at=synth.at(20, 5), todo_id_factory=factory
    )
    return shift, report


def todo_readback(applied: bool = True) -> ReadbackEvidence:
    return ReadbackEvidence("todo", synth.at(20, 10), applied=applied)


def sheet_readback(applied: bool = True) -> ReadbackEvidence:
    return ReadbackEvidence("aitable", synth.at(20, 12), applied=applied)


class TestConfirmationPlan(unittest.TestCase):
    def test_critical_events_get_their_own_unit(self):
        shift, report = make_plan()
        units = report.confirmation_area
        self.assertEqual(units[0].unit_id, "SYNTH-EVT-0002")
        self.assertEqual(units[0].scope, "event")
        self.assertTrue(units[0].has_todo)

    def test_normal_events_are_grouped_into_one_shift_unit(self):
        shift, report = make_plan()
        tail = report.confirmation_area[-1]
        self.assertEqual(tail.unit_id, synth.SHIFT_ID)
        self.assertEqual(tail.scope, "shift")
        self.assertIn("1 项", tail.label)

    def test_unit_assignee_is_the_incoming_operator(self):
        shift, report = make_plan()
        self.assertTrue(
            all(unit.assignee.same_person(synth.TO) for unit in report.confirmation_area)
        )

    def test_plan_without_todo_id_cannot_start_writeback(self):
        shift, report = make_plan(todo_id_factory=lambda unit: "")
        unit = report.confirmation_area[0]
        self.assertFalse(unit.has_todo)
        with self.assertRaises(ContractViolation):
            Confirmation.for_unit(unit)

    def test_attach_todo_returns_a_filled_copy(self):
        shift, report = make_plan(todo_id_factory=lambda unit: "")
        unit = report.confirmation_area[0]
        filled = unit.attach_todo("SYNTH-TODO-XXXX")
        self.assertTrue(filled.has_todo)
        self.assertFalse(unit.has_todo, "原计划条目不变")

    def test_plan_from_units_builds_pending_confirmations(self):
        shift, report = make_plan()
        confirmations = plan_from_units(report.confirmation_area)
        self.assertEqual(len(confirmations), 2)
        self.assertTrue(all(item.is_pending for item in confirmations))
        self.assertTrue(all(item.writeback_status == "not_sent" for item in confirmations))

    def test_build_confirmation_plan_skips_shift_unit_without_normal_events(self):
        shift, ledger, store = synth.make_store()
        store.intake(synth.raw_event("SYNTH-EVT-0001", category="major", status="done"))
        report = build_report(shift, store, ledger, generated_at=synth.at(20, 5))
        units = build_confirmation_plan(shift, report)
        self.assertEqual([unit.scope for unit in units], ["event"])


class TestConfirmationStateMachine(unittest.TestCase):
    def setUp(self):
        self.shift, self.report = make_plan()
        self.confirmation = plan_from_units(self.report.confirmation_area)[0]

    def test_missing_identity_is_rejected(self):
        with synth.expect_code(AUTH_REQUIRED):
            self.confirmation.mark_confirmed(None, at=synth.at(20, 10), readback=todo_readback())

    def test_wrong_person_is_rejected(self):
        with synth.expect_code(AUTH_REQUIRED):
            self.confirmation.mark_confirmed(
                synth.STRANGER, at=synth.at(20, 10), readback=todo_readback()
            )
        self.assertTrue(self.confirmation.is_pending, "错人不放行，状态不变")

    def test_mark_confirmed_without_readback_is_write_unknown(self):
        with synth.expect_code(WRITE_UNKNOWN):
            self.confirmation.mark_confirmed(
                synth.TO, at=synth.at(20, 10), readback=None
            )
        self.assertTrue(self.confirmation.is_pending)

    def test_mark_confirmed_with_unapplied_readback_is_write_unknown(self):
        with synth.expect_code(WRITE_UNKNOWN):
            self.confirmation.mark_confirmed(
                synth.TO, at=synth.at(20, 10), readback=todo_readback(applied=False)
            )

    def test_mark_confirmed_sets_operator_and_time(self):
        self.confirmation.mark_confirmed(synth.TO, at=synth.at(20, 10), readback=todo_readback())
        self.assertEqual(self.confirmation.status, "confirmed")
        self.assertEqual(self.confirmation.confirmed_by.user_id, synth.TO.user_id)
        self.assertEqual(self.confirmation.confirmed_at, synth.at(20, 10))
        self.assertIsNotNone(self.confirmation.readback)

    def test_repeated_confirmation_is_idempotent(self):
        self.confirmation.mark_confirmed(synth.TO, at=synth.at(20, 10), readback=todo_readback())
        self.confirmation.mark_confirmed(synth.TO, at=synth.at(20, 30), readback=todo_readback())
        self.assertEqual(self.confirmation.confirmed_at, synth.at(20, 10))

    def test_writeback_before_confirmation_is_rejected(self):
        with synth.expect_code(NOT_CONFIRMED):
            self.confirmation.record_writeback(
                "verified", readback=sheet_readback(), at=synth.at(20, 12)
            )
        self.assertTrue(self.confirmation.is_pending)
        self.assertEqual(self.confirmation.writeback_status, "not_sent", "确认前状态不变")

    def test_verified_without_readback_is_write_unknown(self):
        self.confirmation.mark_confirmed(synth.TO, at=synth.at(20, 10), readback=todo_readback())
        with synth.expect_code(WRITE_UNKNOWN):
            self.confirmation.record_writeback("verified", readback=None, at=synth.at(20, 12))
        self.assertFalse(self.confirmation.is_written_back)

    def test_happy_path_pending_confirmed_written_back(self):
        self.confirmation.mark_confirmed(synth.TO, at=synth.at(20, 10), readback=todo_readback())
        self.confirmation.record_writeback(
            "verified", readback=sheet_readback(), at=synth.at(20, 12)
        )
        self.assertTrue(self.confirmation.is_written_back)
        self.assertEqual(self.confirmation.writeback_status, "verified")

    def test_confirmation_after_written_back_is_rejected(self):
        self.confirmation.mark_confirmed(synth.TO, at=synth.at(20, 10), readback=todo_readback())
        self.confirmation.record_writeback(
            "verified", readback=sheet_readback(), at=synth.at(20, 12)
        )
        with self.assertRaises(ContractViolation):
            self.confirmation.mark_confirmed(
                synth.TO, at=synth.at(20, 40), readback=todo_readback()
            )


class TestWriteUnknownRecheckOnly(unittest.TestCase):
    def setUp(self):
        shift, report = make_plan()
        self.confirmation = plan_from_units(report.confirmation_area)[0]
        self.confirmation.mark_confirmed(synth.TO, at=synth.at(20, 10), readback=todo_readback())

    def test_unknown_writeback_keeps_status_confirmed(self):
        self.confirmation.record_writeback("unknown", at=synth.at(20, 12))
        self.assertEqual(self.confirmation.writeback_status, "unknown")
        self.assertEqual(self.confirmation.status, "confirmed")
        self.assertTrue(self.confirmation.needs_recheck)

    def test_resend_is_refused_while_unknown(self):
        self.confirmation.record_writeback("unknown", at=synth.at(20, 12))
        with synth.expect_code(WRITE_UNKNOWN):
            self.confirmation.guard_resend()

    def test_recheck_with_applied_evidence_completes_writeback(self):
        self.confirmation.record_writeback("unknown", at=synth.at(20, 12))
        self.confirmation.recheck(sheet_readback(applied=True), at=synth.at(20, 14))
        self.assertTrue(self.confirmation.is_written_back)
        self.assertEqual(self.confirmation.writeback_status, "verified")
        self.assertFalse(self.confirmation.needs_recheck)

    def test_recheck_without_evidence_still_unknown(self):
        self.confirmation.record_writeback("unknown", at=synth.at(20, 12))
        with synth.expect_code(WRITE_UNKNOWN):
            self.confirmation.recheck(None)  # type: ignore[arg-type]
        self.assertTrue(self.confirmation.needs_recheck)

    def test_recheck_not_applied_keeps_confirmed_and_marks_not_applied(self):
        self.confirmation.record_writeback("unknown", at=synth.at(20, 12))
        self.confirmation.recheck(sheet_readback(applied=False), at=synth.at(20, 14))
        self.assertEqual(self.confirmation.writeback_status, "not_applied")
        self.assertEqual(self.confirmation.status, "confirmed")
        self.assertFalse(self.confirmation.is_written_back)

    def test_recheck_without_unknown_state_is_rejected(self):
        with self.assertRaises(ContractViolation):
            self.confirmation.recheck(sheet_readback())

    def test_guard_blocks_write_while_pending(self):
        shift, report = make_plan()
        fresh = plan_from_units(report.confirmation_area)[0]
        with synth.expect_code(NOT_CONFIRMED):
            fresh.guard_resend()

    def test_guard_blocks_replay_after_written_back(self):
        self.confirmation.record_writeback(
            "verified", readback=sheet_readback(), at=synth.at(20, 12)
        )
        with self.assertRaises(ContractViolation):
            self.confirmation.guard_resend()


class TestConfirmationInputs(unittest.TestCase):
    def test_invalid_readback_source_is_a_caller_defect(self):
        with self.assertRaises(ContractViolation):
            ReadbackEvidence("email", synth.at(20, 10))

    def test_readback_requires_timezone_aware_time(self):
        from datetime import datetime

        with self.assertRaises(ContractViolation):
            ReadbackEvidence("todo", datetime(2026, 1, 2, 20, 10))

    def test_unit_requires_shift_id_and_assignee(self):
        with self.assertRaises(ContractViolation):
            ConfirmationUnit(unit_id="SYNTH-EVT-0001", scope="event", shift_id="  ", assignee=synth.TO)

    def test_require_operator_for_helper(self):
        shift, report = make_plan()
        confirmation = plan_from_units(report.confirmation_area)[0]
        self.assertEqual(
            require_operator_for(confirmation, synth.TO).user_id, synth.TO.user_id
        )
        with synth.expect_code(AUTH_REQUIRED):
            require_operator_for(confirmation, synth.STRANGER)

    def test_confirmation_payload_exposes_contract_fields(self):
        shift, report = make_plan()
        confirmation = plan_from_units(report.confirmation_area)[0]
        payload = confirmation.to_dict()
        for key in ("unit_id", "scope", "shift_id", "todo_id", "assignee", "status",
                    "confirmed_by", "confirmed_at", "writeback_status"):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
