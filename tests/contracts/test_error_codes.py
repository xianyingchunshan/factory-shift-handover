"""契约测试：错误码最小集（issue #6 §7）。

合成数据；不联网；不触碰外部系统。
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
    ALARM_CODES,
    ALARM_NOT_CLEARED,
    AUTH_REQUIRED,
    COMPLETENESS_RULES,
    ContractError,
    ContractViolation,
    DUPLICATE_SHIFT,
    E001,
    E002,
    E003,
    E004,
    ERROR_CODES,
    ERROR_CODE_LABELS,
    NOT_CONFIRMED,
    REJECTING_CODES,
    WRITE_UNKNOWN,
    describe_codes,
    is_known_code,
)

EXPECTED_CODES = (
    "E001",
    "E002",
    "E003",
    "E004",
    "AUTH_REQUIRED",
    "DUPLICATE_SHIFT",
    "WRITE_UNKNOWN",
    "NOT_CONFIRMED",
    "ALARM_NOT_CLEARED",
)


class TestErrorCodeMinimumSet(unittest.TestCase):
    def test_minimum_set_is_exactly_the_nine_codes(self):
        self.assertEqual(ERROR_CODES, EXPECTED_CODES)
        self.assertEqual(len(ERROR_CODES), 9)

    def test_module_constants_match_the_set(self):
        self.assertEqual(E001, "E001")
        self.assertEqual(E002, "E002")
        self.assertEqual(E003, "E003")
        self.assertEqual(E004, "E004")
        self.assertIn(AUTH_REQUIRED, ERROR_CODES)
        self.assertIn(DUPLICATE_SHIFT, ERROR_CODES)
        self.assertIn(WRITE_UNKNOWN, ERROR_CODES)
        self.assertIn(NOT_CONFIRMED, ERROR_CODES)
        self.assertIn(ALARM_NOT_CLEARED, ERROR_CODES)

    def test_every_code_has_a_chinese_label(self):
        labels = describe_codes()
        self.assertEqual(set(labels), set(ERROR_CODES))
        for code in ERROR_CODES:
            self.assertTrue(labels[code].strip(), code)
        self.assertEqual(set(ERROR_CODE_LABELS), set(ERROR_CODES))

    def test_completeness_rules_are_ordered_e001_to_e004(self):
        self.assertEqual(COMPLETENESS_RULES, ("E001", "E002", "E003", "E004"))

    def test_only_e001_rejects_and_e002_to_e004_alarm(self):
        self.assertEqual(REJECTING_CODES, frozenset({"E001"}))
        self.assertEqual(ALARM_CODES, frozenset({"E002", "E003", "E004"}))

    def test_is_known_code_guards_against_invented_codes(self):
        self.assertTrue(is_known_code("E001"))
        self.assertFalse(is_known_code("E999"))
        self.assertFalse(is_known_code("SHIFT_LOCKED"))

    def test_contract_error_rejects_unregistered_code(self):
        with self.assertRaises(ValueError):
            ContractError("E999", "私造错误码")

    def test_contract_error_carries_code_message_and_detail(self):
        error = ContractError(E003, "时间越界", detail={"event_id": "SYNTH-EVT-0001"})
        self.assertEqual(error.code, E003)
        self.assertEqual(error.detail["event_id"], "SYNTH-EVT-0001")
        payload = error.to_dict()
        self.assertEqual(payload["code"], E003)
        self.assertEqual(payload["label"], ERROR_CODE_LABELS[E003])
        self.assertIn("[E003]", str(error))

    def test_contract_violation_is_a_value_error_without_business_code(self):
        violation = ContractViolation("调用方参数非法")
        self.assertIsInstance(violation, ValueError)
        self.assertFalse(hasattr(violation, "code"))


if __name__ == "__main__":
    unittest.main()
