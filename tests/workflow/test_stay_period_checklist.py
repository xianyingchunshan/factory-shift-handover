"""工作流测试：驻场期清单口径与长周期（T04，issue #17；SPEC §0 一次轮换一张单）。

覆盖：

- **清单标题口径跟随**：驻场期单 = 「交接班清单 · 驻场期」；早/晚/自定义单的标题
  与既有口径一字不改；
- **汇总口径跟随**：标题 + 驻场期边界 + 覆盖日历日（30 天跨月 → 31 个相连日历日）；
- **长周期（30 天）**：全量事件严格时间序、汇总数 vs 表格行数、清单自检通过；
- **跨零点边界**：相连日历日属期内，仅期前/期后 1 分钟判越界；
- 驻场期单跨重启后口径不变。

落表一律走合成适配器（离线），数据全部 ``SYNTH-`` 前缀虚构。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.enums import ShiftStatus  # noqa: E402
from contracts.errors import ALARM_NOT_CLEARED  # noqa: E402
from contracts.timebase import format_minute  # noqa: E402

from integrations.aitable.cells import SHIFT_COLUMNS  # noqa: E402
from workflow import state as wfstate  # noqa: E402
from workflow.checklist import (  # noqa: E402
    CHECKLIST_TITLE_BASE,
    checklist_title,
    stay_period_facts,
)

import wf_synth  # noqa: E402

#: 30 天驻场期内的一批事件（端点 + 每日 00:05 / 23:55，跨零点相连日成对出现）。
_INSIDE_HOURS: tuple[tuple[int, int], ...] = ((0, 5), (23, 55))


def _inside_rows() -> tuple[dict[str, object], ...]:
    """驻场期内的全部候选行（逐条按期过滤；含首尾端点）。"""
    shift = wf_synth.make_stay_period_shift()
    rows = [
        wf_synth.stay_period_row("SYNTH-STAY-EVT-START", "2026-03-02 08:00"),
        wf_synth.stay_period_row("SYNTH-STAY-EVT-END", "2026-04-01 08:00"),
    ]
    for offset in range(wf_synth.STAY_PERIOD_CALENDAR_DAYS):
        day = wf_synth.stay_period_day(offset)
        for hour, minute in _INSIDE_HOURS:
            moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=wf_synth.SHANGHAI)
            if shift.contains(moment):
                rows.append(
                    wf_synth.stay_period_row(
                        f"SYNTH-STAY-EVT-{offset:02d}{hour:02d}{minute:02d}",
                        format_minute(moment),
                    )
                )
    return tuple(rows)


class TestChecklistTitleFollowsTheShiftName(unittest.TestCase):
    """清单标题口径：随班次名走，驻场期单即「交接班清单 · 驻场期」。"""

    def test_early_shift_title_is_unchanged(self):
        self.assertEqual(CHECKLIST_TITLE_BASE, "交接班清单")
        self.assertEqual(checklist_title(wf_synth.make_shift()), "交接班清单 · 早")
        self.assertEqual(checklist_title(wf_synth.make_shift(shift_name="late")), "交接班清单 · 晚")
        self.assertEqual(
            checklist_title(wf_synth.make_shift(shift_name="custom")), "交接班清单 · 自定义"
        )

    def test_stay_period_title_uses_the_stay_period_label(self):
        self.assertEqual(checklist_title(wf_synth.make_stay_period_shift()), "交接班清单 · 驻场期")
        self.assertEqual(
            checklist_title(wf_synth.make_stay_period_shift(shift_name="驻场期")),
            "交接班清单 · 驻场期",
        )

    def test_service_exposes_the_title_and_period_facts(self):
        flow = wf_synth.stay_period_flow()
        self.assertEqual(flow.checklist.title, "交接班清单 · 驻场期")
        self.assertEqual(flow.checklist.period_facts(), stay_period_facts(flow.shift))

    def test_period_facts_summarise_the_whole_stay_period(self):
        facts = stay_period_facts(wf_synth.make_stay_period_shift())
        self.assertEqual(facts["title"], "交接班清单 · 驻场期")
        self.assertTrue(facts["is_stay_period"])
        self.assertEqual(facts["shift_name_label"], "驻场期")
        self.assertEqual(facts["window_start"], "2026-03-02 08:00")
        self.assertEqual(facts["window_end"], "2026-04-01 08:00")
        self.assertEqual(
            (facts["first_day"], facts["last_day"]), ("2026-03-02", "2026-04-01")
        )
        self.assertEqual(facts["calendar_days"], wf_synth.STAY_PERIOD_CALENDAR_DAYS)
        self.assertTrue(facts["crosses_midnight"])

    def test_early_shift_facts_keep_the_frozen_shape(self):
        facts = stay_period_facts(wf_synth.make_shift())
        self.assertFalse(facts["is_stay_period"])
        self.assertEqual(facts["title"], "交接班清单 · 早")
        self.assertEqual(facts["calendar_days"], 1)
        self.assertFalse(facts["crosses_midnight"])


class TestStayPeriodShiftRowKeepsTheContractValue(unittest.TestCase):
    """表格行的"班次"列口径：驻场期写 ``stay_period``（AI 表格选项更新属 L4）。"""

    def test_shift_row_stores_the_stay_period_value(self):
        flow = wf_synth.stay_period_flow()
        row = flow.shift_row()
        self.assertEqual(row[SHIFT_COLUMNS["shift_name"]], "stay_period")
        self.assertEqual(row[SHIFT_COLUMNS["start_time"]], "2026-03-02 08:00")
        self.assertEqual(row[SHIFT_COLUMNS["end_time"]], "2026-04-01 08:00")

    def test_reading_the_shift_back_keeps_the_stay_period_semantics(self):
        flow = wf_synth.stay_period_flow()
        restored = flow.intake.shift_table.get(wf_synth.STAY_PERIOD_SHIFT_ID)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.shift_name, "stay_period")
        self.assertTrue(restored.is_stay_period)
        self.assertEqual(len(restored.calendar_days()), wf_synth.STAY_PERIOD_CALENDAR_DAYS)
        self.assertEqual(restored.idempotency_key[2], "stay_period")


class TestThirtyDayStayPeriodChecklist(unittest.TestCase):
    """长周期（30 天）下事件排序、汇总与清单自检。"""

    def test_long_period_flow_is_ordered_and_summarised(self):
        flow = wf_synth.stay_period_flow()
        rows = _inside_rows()
        outcomes = flow.submit(rows)
        self.assertTrue(all(outcome.stored for outcome in outcomes))
        self.assertFalse(flow.intake.ledger.has_open)

        report = wf_synth.report_of(flow)
        times = [format_minute(event.occurred_at) for event in report.event_flow]
        self.assertEqual(len(times), len(rows))
        self.assertEqual(times, sorted(times))
        self.assertEqual(report.event_flow[0].event_id, "SYNTH-STAY-EVT-START")
        self.assertEqual(report.event_flow[-1].event_id, "SYNTH-STAY-EVT-END")
        summary = report.completeness_summary
        self.assertEqual(summary["total"], len(rows))
        self.assertEqual(summary["complete"], len(rows))
        self.assertEqual(summary["time_complete_rate"], 1.0)
        self.assertEqual(summary["alarm_count"], 0)
        self.assertEqual(
            (report.window_start, report.window_end),
            (wf_synth.STAY_PERIOD_START, wf_synth.STAY_PERIOD_END),
        )

        consistency = flow.checklist.verify(report)
        self.assertEqual(consistency.problems, ())
        self.assertTrue(consistency.ok)
        self.assertEqual(consistency.checked["table_rows"], len(rows))
        self.assertEqual(consistency.checked["summary_total"], len(rows))

    def test_render_document_carries_the_stay_period_title(self):
        flow = wf_synth.stay_period_flow()
        flow.submit(_inside_rows())
        report = wf_synth.report_of(flow)
        lines = flow.checklist.render_document(report)
        self.assertEqual(lines[0], "交接班清单 · 驻场期")
        self.assertEqual(lines[1:], report.render_lines())  # 五段渲染保持冻结
        self.assertIn("驻场期", lines[1])  # 契约层渲染口径同为班次名标签
        self.assertIn(f"全量 {len(report.event_flow)} 项", "\n".join(lines))

    def test_cross_midnight_events_stay_adjacent_in_the_flow(self):
        flow = wf_synth.stay_period_flow()
        flow.submit(_inside_rows())
        report = wf_synth.report_of(flow)
        ids = [event.event_id for event in report.event_flow]
        for offset in range(wf_synth.STAY_PERIOD_CALENDAR_DAYS - 1):
            before = f"SYNTH-STAY-EVT-{offset:02d}2355"
            after = f"SYNTH-STAY-EVT-{offset + 1:02d}0005"
            if before in ids and after in ids:
                with self.subTest(pair=(before, after)):
                    self.assertEqual(ids.index(after), ids.index(before) + 1)

    def test_out_of_period_event_blocks_the_checklist_until_reviewed(self):
        flow = wf_synth.stay_period_flow()
        flow.submit(
            (
                wf_synth.stay_period_row("SYNTH-STAY-EVT-OUT", "2026-03-02 07:59"),  # 期前 1 分钟
                wf_synth.stay_period_row("SYNTH-STAY-EVT-IN", "2026-03-02 08:05"),
            )
        )
        with wf_synth.expect_code(ALARM_NOT_CLEARED):
            wf_synth.report_of(flow)
        self.assertEqual(flow.shift.status, ShiftStatus.BLOCKED)
        blocked = flow.checklist.blocked_summary()
        self.assertTrue(blocked["blocked"])
        self.assertEqual([alarm["rule"] for alarm in blocked["open_alarms"]], ["E003"])

        flow.intake.review_out_of_range("SYNTH-STAY-EVT-OUT", note="SYNTH 复核确认越界属实")
        report = wf_synth.report_of(flow)
        self.assertEqual(
            [event.event_id for event in report.event_flow],
            ["SYNTH-STAY-EVT-OUT", "SYNTH-STAY-EVT-IN"],
        )
        self.assertEqual(flow.checklist.title, "交接班清单 · 驻场期")


class TestCrossMidnightBoundaryInsideTheStayPeriod(unittest.TestCase):
    """跨零点：相连日历日属期内，只有期外 1 分钟判越界。"""

    def test_only_out_of_period_minutes_raise_e003(self):
        flow = wf_synth.stay_period_flow()
        rows = (
            wf_synth.stay_period_row("SYNTH-STAY-EVT-0001", "2026-03-02 07:59"),  # 期前
            wf_synth.stay_period_row("SYNTH-STAY-EVT-0002", "2026-03-02 08:00"),  # 起始端点
            wf_synth.stay_period_row("SYNTH-STAY-EVT-0003", "2026-03-02 23:59"),  # 首日末尾
            wf_synth.stay_period_row("SYNTH-STAY-EVT-0004", "2026-03-03 00:01"),  # 次日开头
            wf_synth.stay_period_row("SYNTH-STAY-EVT-0005", "2026-04-01 08:00"),  # 结束端点
            wf_synth.stay_period_row("SYNTH-STAY-EVT-0006", "2026-04-01 08:01"),  # 期后
        )
        outcomes = flow.submit(rows)
        self.assertEqual(
            [alarm.rule for outcome in outcomes for alarm in outcome.alarms], ["E003", "E003"]
        )
        self.assertEqual(flow.intake.ledger.counts_by_rule()["E003"], 2)
        self.assertEqual(flow.intake.ledger.open_count, 2)

        for event_id in ("SYNTH-STAY-EVT-0001", "SYNTH-STAY-EVT-0006"):
            flow.intake.review_out_of_range(event_id, note="SYNTH 复核确认越界属实")
        report = wf_synth.report_of(flow)
        self.assertEqual(
            [event.event_id for event in report.event_flow],
            [
                "SYNTH-STAY-EVT-0001",
                "SYNTH-STAY-EVT-0002",
                "SYNTH-STAY-EVT-0003",
                "SYNTH-STAY-EVT-0004",
                "SYNTH-STAY-EVT-0005",
                "SYNTH-STAY-EVT-0006",
            ],
        )
        ids = [event.event_id for event in report.event_flow]
        self.assertEqual(ids.index("SYNTH-STAY-EVT-0004"), ids.index("SYNTH-STAY-EVT-0003") + 1)


class TestFourAlarmsUnderTheStayPeriodGate(unittest.TestCase):
    """工作流层：驻场期边界下 E001–E004 全回归，含"未清不出清单"闸门。"""

    def test_four_alarm_rows_under_the_stay_period(self):
        flow = wf_synth.stay_period_flow()
        outcomes = flow.submit(
            (
                wf_synth.stay_period_row("SYNTH-STAY-EVT-0001", "2026-03-20 09:30"),  # 合规
                wf_synth.stay_period_row("SYNTH-STAY-EVT-0002", None),  # E001 拒收
                wf_synth.stay_period_row("SYNTH-STAY-EVT-0003", "2026-03-20"),  # E002
                wf_synth.stay_period_row("SYNTH-STAY-EVT-0004", "2026-04-02 09:00"),  # E003
                wf_synth.stay_period_row(
                    "SYNTH-STAY-EVT-0005", "2026-03-21 09:00", status=wf_synth.OMIT
                ),  # E004
            )
        )
        self.assertTrue(outcomes[0].stored)
        self.assertTrue(outcomes[1].rejected)
        self.assertEqual(
            [alarm.rule for outcome in outcomes for alarm in outcome.alarms],
            ["E001", "E002", "E003", "E004"],
        )
        self.assertEqual(
            flow.intake.ledger.counts_by_rule(), {"E001": 1, "E002": 1, "E003": 1, "E004": 1}
        )
        self.assertEqual(len(flow.event_rows()), 4)  # E001 不落表
        self.assertEqual(flow.intake.store.complete_count, 1)

        with wf_synth.expect_code(ALARM_NOT_CLEARED):
            wf_synth.report_of(flow)
        self.assertEqual(flow.shift.status, ShiftStatus.BLOCKED)
        self.assertEqual(flow.shift_row()[SHIFT_COLUMNS["status"]], "blocked")

        flow.intake.repair_time("SYNTH-STAY-EVT-0003", "2026-03-20 10:05")
        flow.intake.review_out_of_range("SYNTH-STAY-EVT-0004", note="SYNTH 复核确认越界属实")
        flow.intake.fill_required("SYNTH-STAY-EVT-0005", status="done")
        flow.intake.submit(wf_synth.stay_period_row("SYNTH-STAY-EVT-0002", "2026-03-20 11:30"))
        report = wf_synth.report_of(flow)
        self.assertEqual(len(report.event_flow), 5)
        self.assertEqual(
            [event.event_id for event in report.event_flow],
            [
                "SYNTH-STAY-EVT-0001",
                "SYNTH-STAY-EVT-0003",
                "SYNTH-STAY-EVT-0002",
                "SYNTH-STAY-EVT-0005",
                "SYNTH-STAY-EVT-0004",
            ],
        )
        self.assertEqual(flow.checklist.title, "交接班清单 · 驻场期")


class TestStayPeriodSurvivesRestart(unittest.TestCase):
    """驻场期内必然跨进程重启：恢复后口径与事件不丢。"""

    def test_rebuild_keeps_the_stay_period_and_rebuilds_the_checklist(self):
        flow = wf_synth.stay_period_flow()
        rows = (
            wf_synth.stay_period_row("SYNTH-STAY-EVT-0001", "2026-03-02 08:00"),
            wf_synth.stay_period_row("SYNTH-STAY-EVT-0002", "2026-03-02 23:59"),
            wf_synth.stay_period_row("SYNTH-STAY-EVT-0003", "2026-03-03 00:01"),
        )
        flow.submit(rows)

        bundle = wfstate.rebuild(
            flow.adapter, shift_id=wf_synth.STAY_PERIOD_SHIFT_ID, clock=flow.intake.clock
        )
        self.assertEqual(bundle.issues, ())
        self.assertEqual(bundle.shift.shift_name, "stay_period")
        self.assertTrue(bundle.shift.is_stay_period)
        service = bundle.checklist()
        self.assertEqual(service.title, "交接班清单 · 驻场期")
        self.assertEqual(service.period_facts()["calendar_days"], wf_synth.STAY_PERIOD_CALENDAR_DAYS)
        report = service.generate(todo_id_factory=flow.todos.todo_id_factory)
        self.assertEqual([event.event_id for event in report.event_flow], [
            "SYNTH-STAY-EVT-0001",
            "SYNTH-STAY-EVT-0002",
            "SYNTH-STAY-EVT-0003",
        ])


if __name__ == "__main__":
    unittest.main()
