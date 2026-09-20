"""契约测试：时间口径（issue #6 §2「occurred_at 必填精确到分」+ SPEC §5 含时区）。"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.errors import ContractViolation  # noqa: E402
from contracts.timebase import (  # noqa: E402
    PRECISION_DATE_ONLY,
    PRECISION_MINUTE,
    PRECISION_MISSING,
    SHANGHAI,
    format_minute,
    in_window,
    is_out_of_window,
    parse_occurred_at,
    parse_time_text,
    truncate_to_minute,
)

import synth  # noqa: E402


class TestTimeContract(unittest.TestCase):
    def test_minute_string_parses_to_shanghai_and_truncates_seconds(self):
        parsed = parse_occurred_at("2026-01-02 09:30:45")
        self.assertEqual(parsed.precision, PRECISION_MINUTE)
        self.assertEqual(format_minute(parsed.value), "2026-01-02 09:30")
        self.assertIsNotNone(parsed.value.tzinfo)

    def test_iso_with_offset_is_respected(self):
        parsed = parse_occurred_at("2026-01-02T01:30:00+00:00")
        self.assertEqual(format_minute(parsed.value), "2026-01-02 09:30")

    def test_string_without_offset_defaults_to_shanghai(self):
        parsed = parse_occurred_at("2026-01-02 09:30")
        self.assertEqual(parsed.value.utcoffset(), timedelta(hours=8))
        self.assertEqual(parsed.value.tzinfo, SHANGHAI)

    def test_aware_datetime_passes_through(self):
        parsed = parse_occurred_at(synth.at(9, 30))
        self.assertEqual(parsed.precision, PRECISION_MINUTE)
        self.assertEqual(format_minute(parsed.value), "2026-01-02 09:30")

    def test_naive_datetime_is_rejected_not_guessed(self):
        with self.assertRaises(ContractViolation):
            parse_occurred_at(datetime(2026, 1, 2, 9, 30))

    def test_date_only_input_reports_partial_precision(self):
        parsed = parse_occurred_at("2026-01-02")
        self.assertEqual(parsed.precision, PRECISION_DATE_ONLY)
        self.assertIsNone(parsed.value)
        self.assertTrue(parsed.is_date_only)

    def test_date_object_is_treated_as_date_only(self):
        parsed = parse_occurred_at(date(2026, 1, 2))
        self.assertEqual(parsed.precision, PRECISION_DATE_ONLY)
        self.assertIsNone(parsed.value)

    def test_blank_input_reports_missing(self):
        for raw in (None, "", "   "):
            parsed = parse_occurred_at(raw)
            self.assertEqual(parsed.precision, PRECISION_MISSING, repr(raw))
            self.assertIsNone(parsed.value)

    def test_unparseable_text_is_a_caller_defect(self):
        with self.assertRaises(ContractViolation):
            parse_occurred_at("昨天下午")

    def test_truncate_to_minute_keeps_timezone(self):
        moment = truncate_to_minute(datetime(2026, 1, 2, 9, 30, 59, 999999, tzinfo=SHANGHAI))
        self.assertEqual((moment.second, moment.microsecond), (0, 0))
        self.assertEqual(moment.tzinfo, SHANGHAI)

    def test_window_is_closed_on_both_ends(self):
        self.assertTrue(in_window(synth.START, synth.START, synth.END))
        self.assertTrue(in_window(synth.END, synth.START, synth.END))
        self.assertFalse(is_out_of_window(synth.START, synth.START, synth.END))
        self.assertFalse(is_out_of_window(synth.END, synth.START, synth.END))

    def test_out_of_window_detects_before_and_after(self):
        self.assertTrue(is_out_of_window(synth.at(7, 59), synth.START, synth.END))
        self.assertTrue(is_out_of_window(synth.at(20, 1), synth.START, synth.END))

    def test_other_zone_input_is_converted_for_range_check(self):
        utc = timezone(timedelta(hours=0))
        moment = datetime(2026, 1, 2, 1, 30, tzinfo=utc)  # 上海 09:30
        parsed = parse_time_text(moment)
        self.assertTrue(in_window(parsed.value, synth.START, synth.END))


if __name__ == "__main__":
    unittest.main()
