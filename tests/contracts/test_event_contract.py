"""契约测试：交接事件 HandoverEvent（issue #6 §2）。

覆盖：九类枚举、必填字段、关键级强制升级两条规则（不可降）、
occurred_at 精度约束、告警态不变量、字段顺序。
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
    CATEGORY_COUNT,
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    EventCategory,
    EventStatus,
    Severity,
    label_of,
)
from contracts.errors import ContractViolation  # noqa: E402
from contracts.events import (  # noqa: E402
    EVENT_FIELD_ORDER,
    REQUIRED_ENUM_FIELDS,
    HandoverEvent,
    resolve_severity,
)

import synth  # noqa: E402

EXPECTED_CATEGORIES = (
    "runtime",
    "equipment",
    "defect",
    "hazard",
    "major",
    "two_ticket",
    "field_work",
    "directive",
    "other",
)


def make_event(**overrides):
    params = dict(
        event_id="SYNTH-EVT-0001",
        shift_id=synth.SHIFT_ID,
        category="equipment",
        description="SYNTH 设备情况记录",
        occurred_at=synth.at(9, 30),
        severity="normal",
        status="done",
    )
    params.update(overrides)
    return HandoverEvent(**params)


class TestNineCategories(unittest.TestCase):
    def test_category_enum_has_exactly_nine_members(self):
        self.assertEqual(CATEGORY_COUNT, 9)
        self.assertEqual(len(CATEGORY_ORDER), 9)

    def test_category_values_match_the_spec_order(self):
        self.assertEqual(tuple(member.value for member in CATEGORY_ORDER), EXPECTED_CATEGORIES)

    def test_every_category_has_a_chinese_label(self):
        self.assertEqual(set(CATEGORY_LABELS), set(CATEGORY_ORDER))
        self.assertEqual(label_of(EventCategory, "major"), "重大事项")
        self.assertEqual(label_of(EventCategory, "two_ticket"), "两票执行")

    def test_category_accepts_chinese_label_input(self):
        event = make_event(category="重大事项")
        self.assertEqual(event.category, "major")


class TestSeverityEscalation(unittest.TestCase):
    def test_major_category_forces_critical(self):
        event = make_event(category="major", severity="normal")
        self.assertEqual(event.severity, Severity.CRITICAL)
        self.assertTrue(event.severity_forced)
        self.assertEqual(event.severity_forced_reason, "category_major")
        self.assertEqual(event.original_severity, "normal")

    def test_transferred_status_forces_critical(self):
        event = make_event(category="equipment", status="transferred", severity="normal")
        self.assertEqual(event.severity, "critical")
        self.assertTrue(event.severity_forced)
        self.assertEqual(event.severity_forced_reason, "status_transferred")

    def test_both_rules_are_recorded_together(self):
        event = make_event(category="major", status="transferred", severity="normal")
        self.assertEqual(event.severity, "critical")
        self.assertEqual(event.severity_forced_reason, "category_major+status_transferred")

    def test_forced_critical_cannot_be_downgraded(self):
        event = make_event(category="major", severity="normal")
        self.assertEqual(event.severity, "critical")
        event.severity = "normal"
        event._apply_forced_severity()
        self.assertEqual(event.severity, "critical", "关键级强制升级不可降")

    def test_plain_event_keeps_requested_severity(self):
        event = make_event(category="equipment", severity="normal")
        self.assertEqual(event.severity, "normal")
        self.assertFalse(event.severity_forced)
        self.assertEqual(event.severity_forced_reason, "")

    def test_resolve_severity_is_a_pure_rule_function(self):
        self.assertEqual(resolve_severity("equipment", "done", "normal"), ("normal", False, ""))
        self.assertEqual(
            resolve_severity("major", "done", "normal"),
            ("critical", True, "category_major"),
        )
        self.assertEqual(
            resolve_severity(None, "transferred", "critical"),
            ("critical", True, "status_transferred"),
        )
        self.assertEqual(
            resolve_severity("major", "transferred", "normal"),
            ("critical", True, "category_major+status_transferred"),
        )

    def test_severity_may_be_absent_only_with_an_e004_alarm_state(self):
        event = make_event(severity=None, completeness="missing_required")
        self.assertIsNone(event.severity)
        self.assertTrue(event.is_held_by_alarm)
        self.assertFalse(event.can_enter_report())


class TestEventInvariants(unittest.TestCase):
    def test_required_enum_fields_are_category_severity_status(self):
        self.assertEqual(REQUIRED_ENUM_FIELDS, ("category", "severity", "status"))

    def test_blank_description_is_a_caller_defect(self):
        with self.assertRaises(ContractViolation):
            make_event(description="   ")

    def test_unknown_category_is_a_caller_defect(self):
        with self.assertRaises(ContractViolation):
            make_event(category="不存在类别")

    def test_occurred_at_cannot_be_none_when_complete(self):
        with self.assertRaises(ContractViolation):
            make_event(occurred_at=None)

    def test_occurred_at_none_allowed_for_partial_and_missing_states(self):
        partial = make_event(occurred_at=None, completeness="partial_time")
        self.assertIsNone(partial.occurred_at)
        missing = make_event(occurred_at=None, completeness="missing_time")
        self.assertIsNone(missing.occurred_at)

    def test_occurred_at_none_with_out_of_range_state_is_rejected(self):
        with self.assertRaises(ContractViolation):
            make_event(occurred_at=None, completeness="out_of_range")

    def test_complete_state_requires_all_required_values(self):
        with self.assertRaises(ContractViolation):
            make_event(category=None, completeness="complete")

    def test_event_id_and_shift_id_required(self):
        with self.assertRaises(ContractViolation):
            make_event(event_id="  ")
        with self.assertRaises(ContractViolation):
            make_event(shift_id="")

    def test_occurred_at_is_truncated_to_minute(self):
        event = make_event(occurred_at=synth.at(9, 30).replace(second=42, microsecond=7))
        self.assertEqual((event.occurred_at.second, event.occurred_at.microsecond), (0, 0))

    def test_owner_accepts_identity_mapping(self):
        event = make_event(owner={"source": "dingtalk", "user_id": "SYNTH-uid-owner"})
        self.assertEqual(event.owner.user_id, "SYNTH-uid-owner")
        self.assertIsNone(make_event(owner=None).owner)

    def test_optional_fields_default_to_blank(self):
        event = make_event(ref_no=None, note=None)
        self.assertEqual(event.ref_no, "")
        self.assertEqual(event.note, "")

    def test_unknown_status_is_a_caller_defect(self):
        with self.assertRaises(ContractViolation):
            make_event(status="转接班")


class TestEventQueries(unittest.TestCase):
    def test_is_critical_and_transfer_pending_flags(self):
        critical = make_event(category="major")
        self.assertTrue(critical.is_critical)
        self.assertFalse(critical.is_transfer_pending)
        pending = make_event(status="transferred")
        self.assertTrue(pending.is_transfer_pending)
        self.assertTrue(pending.is_critical, "移交接班人自动升级为关键级")

    def test_can_enter_report_requires_complete_state(self):
        self.assertTrue(make_event().can_enter_report())
        held = make_event(category=None, completeness="missing_required")
        self.assertFalse(held.can_enter_report())
        with self.assertRaises(ContractViolation):
            held.require_report_ready()

    def test_sort_key_orders_by_time_then_event_id(self):
        early = make_event(event_id="SYNTH-EVT-0002", occurred_at=synth.at(8, 5))
        late = make_event(event_id="SYNTH-EVT-0001", occurred_at=synth.at(9, 30))
        self.assertEqual(sorted([late, early], key=lambda e: e.sort_key()), [early, late])

    def test_to_dict_carries_every_contract_field(self):
        payload = make_event().to_dict()
        for field_name in EVENT_FIELD_ORDER:
            self.assertIn(field_name, payload, field_name)

    def test_fill_required_and_review_paths_restore_complete_state(self):
        event = make_event(category=None, completeness="missing_required")
        event.fill_required(category="hazard")
        self.assertEqual(event.completeness, "complete")
        self.assertTrue(event.can_enter_report())

    def test_enum_labels_render_for_report_lines(self):
        event = make_event(status="in_progress")
        self.assertEqual(label_of(EventStatus, event.status), "进行中")
        self.assertEqual(label_of(Severity, event.severity), "一般")


if __name__ == "__main__":
    unittest.main()
