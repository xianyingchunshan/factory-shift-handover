"""EAM → 交接事件字段映射测试（issue #23 第 2 条验证）。

覆盖：编号 / 等级 / 状态 / 时间四类字段的**正反用例**；未知等级或状态**不猜**
（留空并触发 E004，原始值留痕）；未知取值不得映射成默认值。

端到端告警联动（E001–E004 实际报警）在 ``tests/workflow/test_eam_pull.py``；
本文件只测映射层本身，全部走合成夹具，不联网。
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.enums import Completeness, EventCategory, EventStatus, Severity  # noqa: E402
from contracts.errors import ContractViolation  # noqa: E402
from contracts.events import EventStore  # noqa: E402

from integrations.eam.adapter import KIND_DEFECT, KIND_HAZARD, defect, hazard  # noqa: E402
from integrations.eam.mapping import (  # noqa: E402
    DEFECT_SEVERITY_MAP,
    HAZARD_SEVERITY_MAP,
    KIND_TO_CATEGORY,
    MAPPING_TABLES_VERSION,
    REJECT_BAD_TIME,
    REJECT_MISSING_REF,
    STATUS_MAP,
    build_candidate,
    derive_event_id,
    map_severity,
    map_status,
    mapping_tables_doc,
)

import eam_synth as synth  # noqa: E402


class TestSeverityMapping(unittest.TestCase):
    """等级映射：缺陷 危急/严重→critical、一般→normal；隐患 A/B→critical、C→normal。"""

    def test_defect_severity_positive_cases(self):
        cases = {
            "危急": Severity.CRITICAL.value,
            "严重": Severity.CRITICAL.value,
            "一般": Severity.NORMAL.value,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                mapped = map_severity(KIND_DEFECT, raw)
                self.assertTrue(mapped.known)
                self.assertEqual(mapped.value, expected)
                self.assertEqual(mapped.reason, "")

    def test_hazard_severity_positive_cases(self):
        cases = {
            "A": Severity.CRITICAL.value,
            "B": Severity.CRITICAL.value,
            "C": Severity.NORMAL.value,
            "A级": Severity.CRITICAL.value,
            "B级": Severity.CRITICAL.value,
            "C级": Severity.NORMAL.value,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                mapped = map_severity(KIND_HAZARD, raw)
                self.assertTrue(mapped.known)
                self.assertEqual(mapped.value, expected)

    def test_defect_and_hazard_tables_are_distinct(self):
        """同一原始值在两个来源下不得混用（“A” 只对隐患有意义）。"""
        self.assertIsNone(map_severity(KIND_DEFECT, "A").value)
        self.assertIsNone(map_severity(KIND_HAZARD, "危急").value)

    def test_canonical_values_pass_through(self):
        """上游已归一的规范值原样通过（显式列在表里，不算猜）。"""
        for raw in (Severity.CRITICAL.value, Severity.NORMAL.value):
            self.assertEqual(map_severity(KIND_DEFECT, raw).value, raw)
            self.assertEqual(map_severity(KIND_HAZARD, raw).value, raw)

    def test_whitespace_is_tolerated(self):
        self.assertEqual(map_severity(KIND_DEFECT, "  危急 ").value, Severity.CRITICAL.value)

    def test_unknown_severity_is_not_guessed(self):
        """反例：未知等级 → 不猜（留空），原始值留痕在 reason 里。"""
        for raw in ("特急", "重大", "D", "四级", "紧急"):
            with self.subTest(raw=raw):
                mapped = map_severity(KIND_DEFECT, raw)
                self.assertFalse(mapped.known)
                self.assertIsNone(mapped.value)
                self.assertIn(raw, mapped.reason)
                self.assertIn("不猜", mapped.reason)

    def test_blank_severity_is_not_guessed(self):
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                mapped = map_severity(KIND_DEFECT, raw)
                self.assertFalse(mapped.known)
                self.assertIsNone(mapped.value)
                self.assertIn("空", mapped.reason)

    def test_unknown_kind_is_a_caller_defect(self):
        with self.assertRaises(ContractViolation):
            map_severity("equipment", "一般")

    def test_mapping_tables_are_plain_constants(self):
        """集中映射表就是常量（L4 校准只改这一处）。"""
        for table in (DEFECT_SEVERITY_MAP, HAZARD_SEVERITY_MAP, STATUS_MAP):
            self.assertIsInstance(table, dict)
            self.assertTrue(table)
        self.assertEqual(mapping_tables_doc()["version"], MAPPING_TABLES_VERSION)


class TestStatusMapping(unittest.TestCase):
    """状态映射：待处理/处理中→in_progress；已消缺/已验收→done；已移交→transferred。"""

    def test_status_positive_cases(self):
        cases = {
            "待处理": EventStatus.IN_PROGRESS.value,
            "处理中": EventStatus.IN_PROGRESS.value,
            "已消缺": EventStatus.DONE.value,
            "已验收": EventStatus.DONE.value,
            "已移交": EventStatus.TRANSFERRED.value,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                mapped = map_status(raw)
                self.assertTrue(mapped.known)
                self.assertEqual(mapped.value, expected)

    def test_unknown_status_is_not_guessed(self):
        for raw in ("已关闭", "已挂起", "处理完成", "待复验"):
            with self.subTest(raw=raw):
                mapped = map_status(raw)
                self.assertFalse(mapped.known)
                self.assertIsNone(mapped.value)
                self.assertIn(raw, mapped.reason)

    def test_blank_status_is_not_guessed(self):
        mapped = map_status(None)
        self.assertFalse(mapped.known)
        self.assertIsNone(mapped.value)

    def test_canonical_values_pass_through(self):
        for raw in (EventStatus.IN_PROGRESS.value, EventStatus.DONE.value, EventStatus.TRANSFERRED.value):
            self.assertEqual(map_status(raw).value, raw)


class TestEventIdDerivation(unittest.TestCase):
    """event_id 必须确定性派生（shift_id + 来源类别 + EAM 编号），不得随机/带时间戳。"""

    def test_same_inputs_yield_same_id(self):
        first = derive_event_id(shift_id=synth.SHIFT_ID, kind=KIND_DEFECT, ref_no=synth.D1)
        second = derive_event_id(shift_id=synth.SHIFT_ID, kind=KIND_DEFECT, ref_no=synth.D1)
        self.assertEqual(first, second)

    def test_id_is_derived_from_three_parts(self):
        derived = derive_event_id(shift_id=synth.SHIFT_ID, kind=KIND_HAZARD, ref_no=synth.H1)
        self.assertIn(synth.SHIFT_ID, derived)
        self.assertIn(KIND_HAZARD, derived)
        self.assertIn(synth.H1, derived)
        self.assertTrue(derived.startswith("eam-"))

    def test_no_clock_or_random_source_in_signature(self):
        """派生的入参只有三项，没有时间/随机种子（确定性证据）。"""
        params = list(inspect.signature(derive_event_id).parameters)
        self.assertEqual(params, ["shift_id", "kind", "ref_no"])

    def test_kind_and_shift_change_the_id(self):
        base = derive_event_id(shift_id=synth.SHIFT_ID, kind=KIND_DEFECT, ref_no=synth.D1)
        self.assertNotEqual(
            base, derive_event_id(shift_id=synth.SHIFT_ID, kind=KIND_HAZARD, ref_no=synth.D1)
        )
        self.assertNotEqual(
            base, derive_event_id(shift_id=synth.NEXT_SHIFT_ID, kind=KIND_DEFECT, ref_no=synth.D1)
        )
        self.assertNotEqual(
            base, derive_event_id(shift_id=synth.SHIFT_ID, kind=KIND_DEFECT, ref_no=synth.D2)
        )

    def test_same_ref_in_next_stay_period_is_a_different_event(self):
        """跨驻场期不算重复：未消缺缺陷下个驻场期重新提出是**新**事件。"""
        first = derive_event_id(shift_id=synth.SHIFT_ID, kind=KIND_DEFECT, ref_no=synth.OPEN_REF)
        second = derive_event_id(
            shift_id=synth.NEXT_SHIFT_ID, kind=KIND_DEFECT, ref_no=synth.OPEN_REF
        )
        self.assertNotEqual(first, second)

    def test_missing_parts_are_rejected(self):
        with self.assertRaises(ContractViolation):
            derive_event_id(shift_id="", kind=KIND_DEFECT, ref_no=synth.D1)
        with self.assertRaises(ContractViolation):
            derive_event_id(shift_id=synth.SHIFT_ID, kind=KIND_DEFECT, ref_no="  ")
        with self.assertRaises(ContractViolation):
            derive_event_id(shift_id=synth.SHIFT_ID, kind="other", ref_no=synth.D1)


class TestCandidateMapping(unittest.TestCase):
    """候选行四类字段：类别 / 编号 / 时间 / 归属班次。"""

    def setUp(self):
        self.shift = synth.make_shift()

    def test_category_is_fixed_by_source_kind(self):
        defect_row = build_candidate(synth.defects()[0], shift=self.shift).row
        hazard_row = build_candidate(synth.hazards()[0], shift=self.shift).row
        self.assertEqual(defect_row["category"], EventCategory.DEFECT.value)
        self.assertEqual(hazard_row["category"], EventCategory.HAZARD.value)
        self.assertEqual(KIND_TO_CATEGORY, {"defect": "defect", "hazard": "hazard"})

    def test_ref_no_is_the_eam_number(self):
        record = synth.defects()[0]
        outcome = build_candidate(record, shift=self.shift)
        self.assertEqual(outcome.ref_no, synth.D1)
        self.assertEqual(outcome.row["ref_no"], synth.D1)

    def test_shift_id_comes_from_the_caller_shift(self):
        row = build_candidate(synth.defects()[0], shift=self.shift).row
        self.assertEqual(row["shift_id"], synth.SHIFT_ID)
        other = build_candidate(synth.defects()[0], shift=synth.make_next_shift()).row
        self.assertEqual(other["shift_id"], synth.NEXT_SHIFT_ID)

    def test_discovered_at_is_minute_precise(self):
        """发现时间原样传给契约层，落事件后精确到分（秒截断）。"""
        record = defect(synth.D1, "SYNTH 秒级时间", "2026-03-05 09:30:45", severity="一般", status="处理中")
        outcome = build_candidate(record, shift=self.shift)
        self.assertEqual(outcome.row["occurred_at"], "2026-03-05 09:30:45")
        store = EventStore(self.shift)
        result = store.intake(dict(outcome.row))
        self.assertEqual(result.event.occurred_at.strftime("%Y-%m-%d %H:%M"), "2026-03-05 09:30")
        self.assertEqual(result.event.occurred_at.second, 0)

    def test_severity_and_status_come_from_mapping_tables(self):
        row = build_candidate(synth.defects()[0], shift=self.shift).row
        self.assertEqual(row["severity"], Severity.CRITICAL.value)
        self.assertEqual(row["status"], EventStatus.IN_PROGRESS.value)
        self.assertEqual(row["note"], "")

    def test_unknown_severity_and_status_stay_blank_with_trace(self):
        """未知等级 + 未知状态：留空、不猜、原始值留痕、标记待 E004。"""
        record = synth.defects()[2]  # 等级“特急”、状态“已关闭”
        outcome = build_candidate(record, shift=self.shift)
        self.assertIsNone(outcome.row["severity"])
        self.assertIsNone(outcome.row["status"])
        self.assertEqual(outcome.unmapped_fields, ("severity", "status"))
        note = outcome.row["note"]
        self.assertIn("特急", note)
        self.assertIn("已关闭", note)
        self.assertIn("不猜", note)
        self.assertIn("E004", note)

    def test_unknown_never_becomes_a_default_value(self):
        """反例钉死：未知取值不得落成 critical/normal/in_progress/done/transferred 中的任何一个。"""
        allowed = {Severity.CRITICAL.value, Severity.NORMAL.value}
        for raw in ("特急", "紧急", "I", "未知"):
            outcome = build_candidate(
                defect(synth.D1, "SYNTH 未知等级", "2026-03-05 09:30", severity=raw, status="待处理"),
                shift=self.shift,
            )
            self.assertNotIn(outcome.row["severity"], allowed)
            self.assertIsNone(outcome.row["severity"])

    def test_missing_ref_no_is_reported_not_guessed(self):
        outcome = build_candidate(synth.unmappable_defects()[0], shift=self.shift)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.event_id, "")
        self.assertTrue(outcome.reject_reason.startswith(REJECT_MISSING_REF))

    def test_unparsable_time_is_reported_not_guessed(self):
        outcome = build_candidate(synth.unmappable_defects()[1], shift=self.shift)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.reject_reason.startswith(REJECT_BAD_TIME))
        self.assertEqual(outcome.event_id, derive_event_id(
            shift_id=synth.SHIFT_ID, kind=KIND_DEFECT, ref_no=synth.BAD_TIME_REF
        ))

    def test_description_falls_back_deterministically(self):
        record = defect(synth.D1, "", "2026-03-05 09:30", severity="一般", status="处理中")
        row = build_candidate(record, shift=self.shift).row
        self.assertTrue(str(row["description"]).strip())
        self.assertIn(synth.D1, row["description"])
        self.assertIn("缺陷", row["description"])

    def test_hazard_record_maps_like_defect_but_with_hazard_tables(self):
        row = build_candidate(synth.hazards()[1], shift=self.shift).row  # C级 / 已验收
        self.assertEqual(row["category"], EventCategory.HAZARD.value)
        self.assertEqual(row["severity"], Severity.NORMAL.value)
        self.assertEqual(row["status"], EventStatus.DONE.value)

    def test_row_can_be_intaken_without_alarms(self):
        """映射产出的行直接可用：正常记录经契约层为 complete，无告警。"""
        store = EventStore(self.shift)
        for record in (synth.defects()[0], synth.hazards()[0]):
            result = store.intake(dict(build_candidate(record, shift=self.shift).row))
            self.assertFalse(result.rejected)
            self.assertEqual(result.alarms, ())
            self.assertEqual(result.event.completeness, Completeness.COMPLETE.value)


if __name__ == "__main__":
    unittest.main()
