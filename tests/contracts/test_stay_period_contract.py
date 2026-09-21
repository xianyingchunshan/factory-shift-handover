"""契约测试：驻场期语义（T04，issue #17；SPEC §0「一次轮换一张单」，只增不改）。

覆盖四件事：

1. **班次名增补**：``ShiftName.STAY_PERIOD``（``stay_period`` / 中文"驻场期"），
   既有 早/中/晚/自定义 的规范值、顺序、标签不变，未知取值仍按调用方缺陷拒绝。
2. **边界口径**：``start_time``/``end_time`` = **驻场期边界**；E003 越界判定对
   ``[start, end]`` **整体区间**做，**不按单日切分**——跨零点相连日历日均属期内。
3. **驻场期边界下 E001–E004 全回归**。
4. **长周期（30 天）**：事件排序与汇总口径、越界事件复核后仍按发生时间归位。

合成数据：``SYNTH-`` 前缀、全部虚构；时间固定注入，不依赖真实当前时间。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.aitable_mapping import (  # noqa: E402
    SHIFT_NAME_STAY_PERIOD_NOTE,
    mapping_doc,
)
from contracts.alarms import E003_SCOPE_NOTE  # noqa: E402
from contracts.enums import (  # noqa: E402
    STAY_PERIOD_ALIASES,
    Completeness,
    ShiftName,
    coerce_enum,
    is_stay_period,
    label_of,
)
from contracts.errors import (  # noqa: E402
    ALARM_NOT_CLEARED,
    E001,
    E002,
    E003,
    E004,
    ContractViolation,
)
from contracts.report import build_report  # noqa: E402
from contracts.timebase import (  # noqa: E402
    calendar_days_in_window,
    covers_calendar_day,
    format_minute,
    is_out_of_window,
    window_span_days,
)

import synth  # noqa: E402

#: 30 天驻场期内的一批事件（端点 + 每日 00:05 / 23:55，跨零点相连日成对出现）。
_INSIDE_HOURS: tuple[tuple[int, int], ...] = ((0, 5), (23, 55))


def _day_moment(day: date, hour: int, minute: int = 0):
    """把 (日, 时:分) 变成带时区的合成时刻。"""
    return synth.period_at(day.month, day.day, hour, minute)


class TestStayPeriodShiftName(unittest.TestCase):
    """班次名"驻场期"是**增补**：既有取值一个不动。"""

    def test_existing_shift_names_are_unchanged_and_stay_period_is_appended(self):
        values = [member.value for member in ShiftName]
        self.assertEqual(values[:4], ["early", "middle", "late", "custom"])
        self.assertEqual(values, ["early", "middle", "late", "custom", "stay_period"])
        for value, label in (("early", "早"), ("middle", "中"), ("late", "晚"), ("custom", "自定义")):
            with self.subTest(value=value):
                self.assertEqual(coerce_enum(ShiftName, value).value, value)
                self.assertEqual(label_of(ShiftName, value), label)

    def test_stay_period_value_label_and_aliases(self):
        self.assertEqual(ShiftName.STAY_PERIOD.value, "stay_period")
        self.assertEqual(label_of(ShiftName, "stay_period"), "驻场期")
        self.assertEqual(label_of(ShiftName, "驻场期"), "驻场期")
        self.assertEqual(coerce_enum(ShiftName, "驻场期").value, "stay_period")
        self.assertEqual(STAY_PERIOD_ALIASES, frozenset({"stay_period", "驻场期"}))

    def test_stay_period_shift_coerces_the_chinese_alias(self):
        shift = synth.make_stay_period_shift(shift_name="驻场期")
        self.assertEqual(shift.shift_name, "stay_period")
        self.assertTrue(shift.is_stay_period)
        self.assertFalse(synth.make_shift().is_stay_period)

    def test_is_stay_period_tolerates_unknown_values(self):
        for value in ("stay_period", "驻场期", ShiftName.STAY_PERIOD):
            self.assertTrue(is_stay_period(value), repr(value))
        for value in ("early", "夜班", None, "", "   "):
            self.assertFalse(is_stay_period(value), repr(value))

    def test_unknown_shift_name_is_still_a_caller_defect(self):
        with self.assertRaises(ContractViolation):
            synth.make_stay_period_shift(shift_name="夜班")

    def test_stay_period_keeps_the_frozen_shift_invariants(self):
        shift = synth.make_stay_period_shift()
        self.assertEqual(
            shift.idempotency_key,
            (synth.LINE, synth.STAY_PERIOD_FIRST_DAY.isoformat(), "stay_period"),
        )
        with self.assertRaises(ContractViolation):  # shift_date 必须落在驻场期内
            synth.make_stay_period_shift(shift_date=date(2026, 4, 15))
        with self.assertRaises(ContractViolation):  # end 必须晚于 start
            synth.make_stay_period_shift(end_time=synth.STAY_PERIOD_START)


class TestStayPeriodBoundaryAsAWholeInterval(unittest.TestCase):
    """边界 = 驻场期：整体区间判定，不按单日切分。"""

    def setUp(self):
        self.shift = synth.make_stay_period_shift()

    def test_window_is_thirty_days_but_thirty_one_connected_calendar_days(self):
        self.assertTrue(self.shift.is_stay_period)
        self.assertEqual(
            (synth.STAY_PERIOD_END - synth.STAY_PERIOD_START), timedelta(days=synth.STAY_PERIOD_DAYS)
        )
        self.assertEqual(self.shift.window(), (synth.STAY_PERIOD_START, synth.STAY_PERIOD_END))
        days = self.shift.calendar_days()
        self.assertEqual(len(days), synth.STAY_PERIOD_CALENDAR_DAYS)
        self.assertEqual(days[0], synth.STAY_PERIOD_FIRST_DAY)
        self.assertEqual(days[-1], synth.STAY_PERIOD_LAST_DAY)
        self.assertEqual(calendar_days_in_window(*self.shift.window()), days)
        self.assertEqual(window_span_days(*self.shift.window()), synth.STAY_PERIOD_CALENDAR_DAYS)

    def test_contains_is_the_closed_stay_period_interval(self):
        self.assertTrue(self.shift.contains(synth.STAY_PERIOD_START))  # 起始端点属期内
        self.assertTrue(self.shift.contains(synth.STAY_PERIOD_END))  # 结束端点属期内
        self.assertFalse(self.shift.contains(synth.period_at(3, 2, 7, 59)))
        self.assertFalse(self.shift.contains(synth.period_at(4, 1, 8, 1)))
        self.assertFalse(is_out_of_window(synth.STAY_PERIOD_START, *self.shift.window()))
        self.assertFalse(is_out_of_window(synth.STAY_PERIOD_END, *self.shift.window()))
        self.assertTrue(is_out_of_window(synth.period_at(3, 2, 7, 59), *self.shift.window()))

    def test_cross_midnight_connected_days_belong_to_the_period(self):
        for moment in (
            synth.period_at(3, 2, 23, 59),  # 首日末尾
            synth.period_at(3, 3, 0, 1),  # 次日开头（相连日历日）
            synth.period_at(3, 31, 23, 59),  # 末日前一日末尾
            synth.period_at(4, 1, 0, 1),  # 末日开头
        ):
            with self.subTest(moment=format_minute(moment)):
                self.assertTrue(self.shift.contains(moment))
                self.assertFalse(is_out_of_window(moment, *self.shift.window()))

    def test_day_judgement_uses_the_whole_interval(self):
        self.assertFalse(self.shift.covers_day(date(2026, 3, 1)))
        self.assertTrue(self.shift.covers_day(synth.STAY_PERIOD_FIRST_DAY))
        self.assertTrue(self.shift.covers_day(date(2026, 3, 20)))
        self.assertTrue(self.shift.covers_day(synth.STAY_PERIOD_LAST_DAY))
        self.assertFalse(self.shift.covers_day(date(2026, 4, 2)))
        self.assertTrue(self.shift.covers_day(synth.STAY_PERIOD_START))  # 也接受 datetime

    def test_covers_calendar_day_rejects_non_dates(self):
        with self.assertRaises(ContractViolation):
            covers_calendar_day("2026-03-02", synth.STAY_PERIOD_START, synth.STAY_PERIOD_END)

    def test_incomplete_bounds_are_not_treated_as_out_of_range(self):
        self.assertFalse(is_out_of_window(synth.STAY_PERIOD_START, None, None))
        self.assertEqual(calendar_days_in_window(None, None), ())
        self.assertEqual(window_span_days(None, None), 0)
        self.assertTrue(covers_calendar_day(synth.STAY_PERIOD_FIRST_DAY, None, None))

    def test_e003_scope_note_pins_the_stay_period_boundary(self):
        self.assertIn("驻场期", E003_SCOPE_NOTE)
        self.assertIn("整体区间", E003_SCOPE_NOTE)
        self.assertIn("不按单日切分", E003_SCOPE_NOTE)

    def test_shift_mapping_declares_the_stay_period_option_and_l4_ownership(self):
        self.assertIn("驻场期", SHIFT_NAME_STAY_PERIOD_NOTE)
        self.assertIn("stay_period", SHIFT_NAME_STAY_PERIOD_NOTE)
        self.assertIn("L4", SHIFT_NAME_STAY_PERIOD_NOTE)
        self.assertEqual(mapping_doc()["shift_name_options_note"], SHIFT_NAME_STAY_PERIOD_NOTE)


class TestFourAlarmsUnderStayPeriodBoundary(unittest.TestCase):
    """驻场期边界下 E001–E004 全回归（规则本身冻结不变）。"""

    def setUp(self):
        self.shift, self.ledger, self.store = synth.make_stay_period_store()

    def _alarm_rules(self, event_id: str):
        return [alarm.rule for alarm in self.ledger.all_alarms() if alarm.event_id == event_id]

    def test_e001_missing_time_is_rejected_and_not_stored(self):
        result = self.store.intake(
            synth.stay_period_raw_event("SYNTH-STAY-EVT-0001", occurred_at=None)
        )
        self.assertTrue(result.rejected)
        self.assertIsNone(result.event)
        self.assertEqual([alarm.rule for alarm in result.alarms], [E001])
        self.assertIsNone(self.store.get("SYNTH-STAY-EVT-0001"))
        self.assertEqual(self.store.event_count, 0)
        self.assertEqual(self.store.complete_count, 0)

    def test_e002_date_only_is_stored_with_alarm(self):
        result = self.store.intake(
            synth.stay_period_raw_event("SYNTH-STAY-EVT-0002", occurred_at="2026-03-20")
        )
        self.assertFalse(result.rejected)
        self.assertEqual([alarm.rule for alarm in result.alarms], [E002])
        self.assertEqual(result.event.completeness, Completeness.PARTIAL_TIME)
        self.assertIsNone(result.event.occurred_at)
        self.assertIsNotNone(self.store.get("SYNTH-STAY-EVT-0002"))

    def test_e004_missing_required_fields_are_listed_one_by_one(self):
        result = self.store.intake(
            synth.stay_period_raw_event("SYNTH-STAY-EVT-0003", severity=synth.OMIT)
        )
        self.assertEqual([alarm.rule for alarm in result.alarms], [E004])
        self.assertEqual([alarm.field for alarm in result.alarms], ["severity"])
        self.assertEqual(result.event.completeness, Completeness.MISSING_REQUIRED)

    def test_e003_boundary_is_the_stay_period_not_a_single_day(self):
        rows = (
            ("SYNTH-STAY-EVT-0011", "2026-03-02 07:59", True),  # 期前 1 分钟 → 越界
            ("SYNTH-STAY-EVT-0012", "2026-03-02 08:00", False),  # 起始端点属期内
            ("SYNTH-STAY-EVT-0013", "2026-03-02 23:59", False),  # 首日末尾
            ("SYNTH-STAY-EVT-0014", "2026-03-03 00:01", False),  # 次日开头（相连日历日）
            ("SYNTH-STAY-EVT-0015", "2026-03-31 23:59", False),  # 末日前一日末尾
            ("SYNTH-STAY-EVT-0016", "2026-04-01 00:01", False),  # 末日开头
            ("SYNTH-STAY-EVT-0017", "2026-04-01 08:00", False),  # 结束端点属期内
            ("SYNTH-STAY-EVT-0018", "2026-04-01 08:01", True),  # 期后 1 分钟 → 越界
        )
        for event_id, occurred_at, out_of_range in rows:
            with self.subTest(occurred_at=occurred_at):
                result = self.store.intake(
                    synth.stay_period_raw_event(event_id, occurred_at=occurred_at)
                )
                self.assertEqual([alarm.rule for alarm in result.alarms], [E003] if out_of_range else [])
                expected = Completeness.OUT_OF_RANGE if out_of_range else Completeness.COMPLETE
                self.assertEqual(result.event.completeness, expected)

    def test_four_rules_coexist_under_the_stay_period(self):
        self.store.intake_all(
            (
                synth.stay_period_raw_event("SYNTH-STAY-EVT-0021", occurred_at=None),  # E001 拒收
                synth.stay_period_raw_event("SYNTH-STAY-EVT-0022", occurred_at="2026-03-20"),  # E002
                synth.stay_period_raw_event(
                    "SYNTH-STAY-EVT-0023", occurred_at="2026-04-02 09:00"
                ),  # E003
                synth.stay_period_raw_event(
                    "SYNTH-STAY-EVT-0024", category=synth.OMIT
                ),  # E004
                synth.stay_period_raw_event("SYNTH-STAY-EVT-0025"),  # 合规
            )
        )
        self.assertEqual(
            self.ledger.counts_by_rule(), {"E001": 1, "E002": 1, "E003": 1, "E004": 1}
        )
        self.assertTrue(self.ledger.has_open)
        self.assertEqual(self.store.event_count, 4)  # E001 不落表
        self.assertEqual(self.store.complete_count, 1)


class TestLongStayPeriodWindow(unittest.TestCase):
    """长周期（30 天）下的排序与汇总口径。"""

    @staticmethod
    def _inside_rows():
        """驻场期内的全部候选行（端点 + 每日 00:05 / 23:55，逐条按期过滤）。"""
        shift = synth.make_stay_period_shift()
        rows = [
            synth.stay_period_raw_event("SYNTH-STAY-EVT-START", occurred_at="2026-03-02 08:00"),
            synth.stay_period_raw_event("SYNTH-STAY-EVT-END", occurred_at="2026-04-01 08:00"),
        ]
        for offset in range(synth.STAY_PERIOD_CALENDAR_DAYS):
            day = synth.stay_period_day(offset)
            for hour, minute in _INSIDE_HOURS:
                moment = _day_moment(day, hour, minute)
                if shift.contains(moment):
                    rows.append(
                        synth.stay_period_raw_event(
                            f"SYNTH-STAY-EVT-{offset:02d}{hour:02d}{minute:02d}",
                            occurred_at=format_minute(moment),
                        )
                    )
        return tuple(rows)

    def test_thirty_day_flow_is_strictly_ordered_and_summarised(self):
        shift, ledger, store = synth.make_stay_period_store()
        rows = self._inside_rows()
        results = store.intake_all(rows)
        self.assertEqual([alarm.rule for result in results for alarm in result.alarms], [])
        self.assertEqual(store.event_count, len(rows))
        self.assertEqual(store.complete_count, len(rows))

        report = build_report(shift, store, ledger, generated_at=synth.STAY_PERIOD_END)
        times = [format_minute(event.occurred_at) for event in report.event_flow]
        self.assertEqual(len(times), len(rows))
        self.assertEqual(times, sorted(times))
        self.assertEqual(report.event_flow[0].event_id, "SYNTH-STAY-EVT-START")
        self.assertEqual(report.event_flow[-1].event_id, "SYNTH-STAY-EVT-END")
        self.assertEqual(report.completeness_summary["total"], len(rows))
        self.assertEqual(report.completeness_summary["complete"], len(rows))
        self.assertEqual(report.completeness_summary["time_complete_rate"], 1.0)
        self.assertEqual(report.completeness_summary["alarm_count"], 0)
        self.assertEqual(
            (report.window_start, report.window_end),
            (synth.STAY_PERIOD_START, synth.STAY_PERIOD_END),
        )
        self.assertEqual(report.to_dict()["shift_name"], "stay_period")

    def test_cross_midnight_events_stay_adjacent_in_the_flow(self):
        shift, ledger, store = synth.make_stay_period_store()
        store.intake_all(self._inside_rows())
        report = build_report(shift, store, ledger, generated_at=synth.STAY_PERIOD_END)
        ids = [event.event_id for event in report.event_flow]
        for offset in range(synth.STAY_PERIOD_CALENDAR_DAYS - 1):
            before = f"SYNTH-STAY-EVT-{offset:02d}2355"
            after = f"SYNTH-STAY-EVT-{offset + 1:02d}0005"
            if before in ids and after in ids:
                with self.subTest(pair=(before, after)):
                    self.assertEqual(ids.index(after), ids.index(before) + 1)

    def test_out_of_period_event_is_flagged_then_keeps_its_chronological_slot(self):
        shift, ledger, store = synth.make_stay_period_store()
        results = store.intake_all(
            (
                synth.stay_period_raw_event("SYNTH-STAY-EVT-A", occurred_at="2026-03-02 08:05"),
                synth.stay_period_raw_event("SYNTH-STAY-EVT-B", occurred_at="2026-03-02 07:59"),
                synth.stay_period_raw_event("SYNTH-STAY-EVT-C", occurred_at="2026-04-01 07:59"),
            )
        )
        self.assertEqual([alarm.rule for alarm in results[1].alarms], [E003])
        self.assertEqual(store.get("SYNTH-STAY-EVT-B").completeness, Completeness.OUT_OF_RANGE)
        with synth.expect_code(ALARM_NOT_CLEARED):
            build_report(shift, store, ledger, generated_at=synth.STAY_PERIOD_END)

        store.get("SYNTH-STAY-EVT-B").mark_reviewed(note="SYNTH 复核确认越界属实")
        synth.clear_all(ledger, moment=synth.period_at(4, 1, 7, 59))
        report = build_report(shift, store, ledger, generated_at=synth.STAY_PERIOD_END)
        self.assertEqual(
            [event.event_id for event in report.event_flow],
            ["SYNTH-STAY-EVT-B", "SYNTH-STAY-EVT-A", "SYNTH-STAY-EVT-C"],
        )


if __name__ == "__main__":
    unittest.main()
