"""EAM 只读拉取编排层测试（issue #23 第 3/4 条验证）。

覆盖：

- **字段映射**：编号 / 等级 / 状态 / 时间四类字段落表口径；
- **告警联动四条**：E001 缺时间→拒收不落表；E002 只有日期→落表待补；E003 越界→
  按驻场期整体区间判定（跨零点相连日历日均属期内）；E004 关键字段缺失（未知映射值）；
  **拒收与告警显式报出**（条数 + 原因 + 来源编号），告警未清不得出清单；
- **两层幂等**：编排层先查表跳过 + 表格层 append 兜底拒绝；同窗口重复拉取不双写、
  不抛错、不重复出告警；**已存在行一律不覆盖**（人工填的处理进展不被 update）；
  跨驻场期不算重复（未消缺缺陷下个驻场期重新提出）；
- **只读入口**：误接真实 EAM 适配器直接失败。

全部走合成夹具（``SYNTH-``），不联网、不接真实系统。
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

from contracts.enums import Completeness, EventCategory, EventStatus, Severity  # noqa: E402
from contracts.errors import ALARM_NOT_CLEARED, E001, E002, E003, E004  # noqa: E402

from integrations.aitable.cells import EVENT_COLUMNS  # noqa: E402
from integrations.aitable.synthetic import CREATE, UPDATE  # noqa: E402
from integrations.aitable.tables import EVENT_TABLE  # noqa: E402
from integrations.eam.adapter import EamReadOnlyAdapter, defect, hazard  # noqa: E402
from integrations.eam.mapping import REJECT_BAD_TIME, REJECT_MISSING_REF  # noqa: E402
from workflow.eam_pull import (  # noqa: E402
    ACTION_CREATED,
    ACTION_REJECTED,
    ACTION_SKIPPED,
    ACTION_UNMAPPED,
    REASON_ALREADY_IN_TABLE,
    REASON_TABLE_LAYER,
    EamPullService,
)

import wf_synth as synth  # noqa: E402

HUMAN_NOTE = "SYNTH 人工填写的处理进展"
HUMAN_STATUS = EventStatus.TRANSFERRED.value


class TestFieldMappingThroughPull(unittest.TestCase):
    """映射四类字段经真链路落表后的口径。"""

    def setUp(self):
        self.flow = synth.eam_flow()
        self.report = self.flow.run()

    def test_counts_are_reported_explicitly(self):
        self.assertEqual(self.report.fetched, synth.EAM_FETCHED)
        self.assertEqual(self.report.created, synth.EAM_CREATED)
        self.assertEqual(self.report.rejected, 1)
        self.assertEqual(self.report.unmapped, 0)
        self.assertEqual(self.report.alarm_count, synth.EAM_ALARM_COUNT)
        self.assertEqual(self.report.table_rows, synth.EAM_CREATED)
        self.assertEqual(
            self.report.summary_line(),
            "新增 8 / 跳过 0 / 拒收 1 / 未映射 0 / 受理不明 0 / 告警 4（未清 4）",
        )
        self.assertEqual(self.report.open_alarm_count, synth.EAM_ALARM_COUNT)
        self.assertEqual(self.report.generated_at, "2026-04-01 09:00")
        self.assertEqual(
            sorted(self.report.alarm_rules), sorted([E001, E002, E004])
        )

    def test_defect_row_has_mapped_fields(self):
        row = self.flow.row_of(self.flow.event_id_of(synth.EAM_D1))
        self.assertEqual(row[EVENT_COLUMNS["category"]], EventCategory.DEFECT.value)
        self.assertEqual(row[EVENT_COLUMNS["ref_no"]], synth.EAM_D1)
        self.assertEqual(row[EVENT_COLUMNS["shift_id"]], synth.STAY_PERIOD_SHIFT_ID)
        self.assertEqual(row[EVENT_COLUMNS["severity"]], Severity.CRITICAL.value)
        self.assertEqual(row[EVENT_COLUMNS["status"]], EventStatus.IN_PROGRESS.value)
        self.assertEqual(row[EVENT_COLUMNS["occurred_at"]], "2026-03-05 09:30")
        self.assertEqual(row[EVENT_COLUMNS["completeness"]], Completeness.COMPLETE.value)

    def test_hazard_row_uses_hazard_tables(self):
        row = self.flow.row_of(self.flow.event_id_of(synth.EAM_H2, kind="hazard"))
        self.assertEqual(row[EVENT_COLUMNS["category"]], EventCategory.HAZARD.value)
        self.assertEqual(row[EVENT_COLUMNS["ref_no"]], synth.EAM_H2)
        self.assertEqual(row[EVENT_COLUMNS["severity"]], Severity.NORMAL.value)
        self.assertEqual(row[EVENT_COLUMNS["status"]], EventStatus.DONE.value)

    def test_hazard_transferred_status_forces_critical(self):
        """状态“已移交”走既有强制升级规则（不可降，原值留痕）。"""
        row = self.flow.row_of(self.flow.event_id_of(synth.EAM_H4, kind="hazard"))
        self.assertEqual(row[EVENT_COLUMNS["status"]], EventStatus.TRANSFERRED.value)
        self.assertEqual(row[EVENT_COLUMNS["severity"]], Severity.CRITICAL.value)
        self.assertEqual(row[EVENT_COLUMNS["severity_forced"]], "true")
        self.assertEqual(row[EVENT_COLUMNS["original_severity"]], Severity.NORMAL.value)

    def test_occurred_at_is_minute_precise(self):
        rows = {row[EVENT_COLUMNS["ref_no"]]: row for row in self.flow.event_rows()}
        for ref in (synth.EAM_D1, synth.EAM_D3, synth.EAM_H1, synth.EAM_H3):
            with self.subTest(ref=ref):
                self.assertRegex(rows[ref][EVENT_COLUMNS["occurred_at"]], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_event_ids_are_deterministic_without_time_or_randomness(self):
        event_id = self.flow.event_id_of(synth.EAM_D1)
        self.assertEqual(
            event_id,
            self.flow.event_id_of(synth.EAM_D1),  # 再派生一次，逐字一致
        )
        self.assertEqual(self.report.created_refs.count(synth.EAM_D1), 1)
        self.assertEqual(self.flow.intake.event_table.find_row(event_id).get("事件ID"), event_id)
        self.assertEqual(len(set(self.report.created_refs)), synth.EAM_CREATED)


class TestUnknownValuesAreNotGuessed(unittest.TestCase):
    """未知等级 / 状态：不猜 → 留空 + E004，原始值留痕。"""

    def setUp(self):
        self.flow = synth.eam_flow()
        self.report = self.flow.run()
        self.event_id = self.flow.event_id_of(synth.EAM_D3)

    def test_unknown_maps_to_blank_not_a_default(self):
        row = self.flow.row_of(self.event_id)
        self.assertEqual(row[EVENT_COLUMNS["severity"]], "")
        self.assertEqual(row[EVENT_COLUMNS["status"]], "")
        self.assertEqual(row[EVENT_COLUMNS["completeness"]], Completeness.MISSING_REQUIRED.value)

    def test_e004_is_raised_for_both_fields(self):
        rules = [item for item in self.report.open_alarms if item["event_id"] == self.event_id]
        self.assertEqual([item["rule"] for item in rules], [E004, E004])
        self.assertEqual(sorted(item["field"] for item in rules), ["severity", "status"])
        self.assertEqual({item["ref_no"] for item in rules}, {synth.EAM_D3})

    def test_raw_values_are_kept_on_the_row(self):
        note = self.flow.row_of(self.event_id)[EVENT_COLUMNS["note"]]
        self.assertIn("特急", note)
        self.assertIn("已关闭", note)
        self.assertIn("不猜", note)

    def test_report_entry_carries_ref_and_reason(self):
        entry = next(item for item in self.report.entries if item.ref_no == synth.EAM_D3)
        self.assertEqual(entry.action, ACTION_CREATED)
        self.assertEqual(entry.alarms, (E004, E004))
        self.assertIn(E004, entry.reason)


class TestAlarmLinkage(unittest.TestCase):
    """四条告警规则经 EAM 拉取的联动（含显式报出与不出清单）。"""

    def test_e001_missing_time_is_rejected_and_not_stored(self):
        flow = synth.eam_flow()
        report = flow.run()
        event_id = flow.event_id_of(synth.EAM_D4)
        self.assertEqual(report.rejected_refs, (synth.EAM_D4,))
        self.assertEqual(flow.intake.event_table.find_row(event_id), None)
        self.assertEqual(
            [row[EVENT_COLUMNS["ref_no"]] for row in flow.event_rows()].count(synth.EAM_D4), 0
        )
        entry = next(item for item in report.entries if item.ref_no == synth.EAM_D4)
        self.assertEqual(entry.action, ACTION_REJECTED)
        self.assertEqual(entry.alarms, (E001,))
        self.assertIn("E001", entry.reason)
        self.assertIn(synth.EAM_D4, entry.event_id)  # 来源编号可追
        self.assertIn(E001, [item["rule"] for item in report.open_alarms])

    def test_e002_date_only_is_stored_with_raw_text_and_alarm(self):
        flow = synth.eam_flow()
        report = flow.run()
        row = flow.row_of(flow.event_id_of(synth.EAM_D5))
        self.assertEqual(row[EVENT_COLUMNS["completeness"]], Completeness.PARTIAL_TIME.value)
        self.assertEqual(row[EVENT_COLUMNS["occurred_at"]], "2026-03-10")  # 原始日期文本留痕
        alarm = next(item for item in report.open_alarms if item["ref_no"] == synth.EAM_D5)
        self.assertEqual(alarm["rule"], E002)
        self.assertEqual(alarm["field"], "occurred_at")

    def test_e003_boundary_uses_the_whole_stay_period(self):
        """末日 12:00 晚于窗口结束 08:00（同一日历日）→ 越界；首日 23:30 → 期内。"""
        flow = synth.eam_flow(
            eam=synth.make_eam_adapter(
                defect_records=(synth.eam_defects()[0], defect(synth.EAM_LATE, "SYNTH 末日晚间缺陷", "2026-04-01 12:00", severity="一般", status="处理中")),
                hazard_records=(synth.eam_hazards()[2],),
            )
        )
        report = flow.run()
        late_row = flow.row_of(flow.event_id_of(synth.EAM_LATE))
        self.assertEqual(late_row[EVENT_COLUMNS["completeness"]], Completeness.OUT_OF_RANGE.value)
        rules = {item["rule"]: item for item in report.open_alarms}
        self.assertIn(E003, rules)
        self.assertEqual(rules[E003]["ref_no"], synth.EAM_LATE)
        self.assertIn("2026-04-01 08:00", rules[E003]["detail"])
        # 首日跨零点相连日历日：属期内，不得出 E003
        cross_midnight = flow.row_of(flow.event_id_of(synth.EAM_H3, kind="hazard"))
        self.assertEqual(cross_midnight[EVENT_COLUMNS["occurred_at"]], "2026-03-02 23:30")
        self.assertEqual(cross_midnight[EVENT_COLUMNS["completeness"]], Completeness.COMPLETE.value)
        self.assertEqual(
            [item["ref_no"] for item in report.open_alarms if item["rule"] == E003],
            [synth.EAM_LATE],
        )

    def test_out_of_day_range_record_is_not_fetched(self):
        flow = synth.eam_flow(
            eam=synth.make_eam_adapter(
                defect_records=(defect("SYNTH-EAM-DEFECT-0021", "SYNTH 下个窗口缺陷", "2026-04-02 09:00"),),
                hazard_records=(),
            )
        )
        report = flow.run()
        self.assertEqual(report.fetched, 0)
        self.assertEqual(report.created, 0)
        self.assertEqual(flow.event_rows(), ())

    def test_unmappable_records_are_reported_not_dropped(self):
        flow = synth.eam_flow(
            eam=synth.make_eam_adapter(
                defect_records=(
                    defect("", "SYNTH 缺编号缺陷", "2026-03-12 10:00", severity="一般", status="处理中"),
                    defect(synth.EAM_BAD_TIME, "SYNTH 时间写法异常缺陷", "2026/03/12", severity="一般", status="处理中"),
                ),
                hazard_records=(),
            )
        )
        report = flow.run()
        self.assertEqual(report.fetched, 2)
        self.assertEqual(report.unmapped, 2)
        self.assertEqual(report.created, 0)
        self.assertEqual(
            sorted(entry.reason.split(":")[0] for entry in report.entries),
            sorted([REJECT_MISSING_REF, REJECT_BAD_TIME]),
        )
        self.assertTrue(all(entry.action == ACTION_UNMAPPED for entry in report.entries))
        self.assertEqual(flow.pull.intake.event_table.count(), 0)

    def test_open_alarm_blocks_the_checklist(self):
        flow = synth.eam_flow()
        flow.run()
        with self.assertRaises(Exception) as ctx:
            flow.pull.require_clear()
        self.assertEqual(ctx.exception.code, ALARM_NOT_CLEARED)
        with self.assertRaises(Exception) as ctx2:
            flow.generate()
        self.assertEqual(ctx2.exception.code, ALARM_NOT_CLEARED)
        self.assertTrue(flow.shift.is_blocked)
        self.assertEqual(flow.shift.status, "blocked")

    def test_checklist_is_generated_after_alarms_are_cleared(self):
        """按业务路径补全/复核/重录（不绕过服务）后告警清零 → 正常出清单。"""
        flow = synth.eam_flow()
        flow.run()
        # E002：补全时间；E004：补关键字段；E001 拒收：EAM 侧补时间后按同一编号重录成功
        flow.intake.repair_time(flow.event_id_of(synth.EAM_D5), "2026-03-10 09:00")
        flow.intake.fill_required(
            flow.event_id_of(synth.EAM_D3),
            severity=Severity.NORMAL.value,
            status=EventStatus.IN_PROGRESS.value,
            note="SYNTH 人工补等级/状态",
        )
        repaired = defect(
            synth.EAM_D4,
            "SYNTH 集电线路避雷器异常（EAM 补时间后重录）",
            "2026-03-12 15:20",
            severity="严重",
            status="待处理",
        )
        fix_report = EamPullService(
            flow.shift, flow.intake, synth.make_eam_adapter(defect_records=(repaired,), hazard_records=()),
            clock=synth.fixed_clock(),
        ).pull()
        self.assertEqual(fix_report.created, 1)
        self.assertEqual(flow.pull.require_clear().open_count, 0)
        flow.intake.submit_shift()  # 告警清零后走既有提交路径（draft → submitted）
        report = flow.generate()
        text = "\n".join(flow.checklist.render_document(report))
        self.assertIn("交接班清单 · 驻场期", text)
        self.assertIn("SYNTH 风机齿轮箱温度偏高", text)  # 拉进来的缺陷描述进入清单
        self.assertIn("SYNTH 升压站消防通道占用", text)  # 隐患同理
        self.assertEqual(len(report.event_flow), synth.EAM_CREATED + 1)
        self.assertEqual(flow.shift.status, "submitted")


class TestIdempotency(unittest.TestCase):
    """两层幂等：编排层先查表跳过 + 表格层 append 兜底拒绝。"""

    def test_second_pull_in_same_window_skips_everything(self):
        flow = synth.eam_flow()
        first = flow.run()
        second = flow.run()
        self.assertEqual(first.created, synth.EAM_CREATED)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.skipped, synth.EAM_CREATED)
        self.assertEqual(second.rejected, 1)  # 缺时间的行仍未落表，如实再报一次拒收
        self.assertEqual(second.alarm_count, 0)  # 不重复出告警
        self.assertEqual(second.open_alarm_count, first.open_alarm_count)
        self.assertEqual(second.table_rows, first.table_rows)
        self.assertEqual(
            {entry.reason for entry in second.entries if entry.action == ACTION_SKIPPED},
            {REASON_ALREADY_IN_TABLE},
        )
        self.assertEqual(
            second.summary_line(),
            "新增 0 / 跳过 8 / 拒收 1 / 未映射 0 / 受理不明 0 / 告警 0（未清 4）",
        )

    def test_second_pull_writes_nothing_to_the_event_table(self):
        flow = synth.eam_flow()
        flow.run()
        before_create = flow.table_writes(CREATE)
        before_update = flow.table_writes(UPDATE)
        flow.run()
        self.assertEqual(flow.table_writes(CREATE) - before_create, 0)
        self.assertEqual(flow.table_writes(UPDATE) - before_update, 0)

    def test_existing_rows_are_never_updated(self):
        """钉死测试：人工填的“处理进展/状态”不被自动化覆盖（只 append 不 update）。"""
        flow = synth.eam_flow()
        flow.run()
        record_id = flow.intake.event_table.record_id_of(flow.event_id_of(synth.EAM_D1))
        flow.adapter.update_record(
            EVENT_TABLE,
            record_id,
            {EVENT_COLUMNS["status"]: HUMAN_STATUS, EVENT_COLUMNS["note"]: HUMAN_NOTE},
        )
        human_row = flow.row_of(flow.event_id_of(synth.EAM_D1))
        self.assertEqual(human_row[EVENT_COLUMNS["note"]], HUMAN_NOTE)

        before_create = flow.table_writes(CREATE)
        before_update = flow.table_writes(UPDATE)
        report = flow.run()

        self.assertEqual(report.created, 0)
        self.assertEqual(report.skipped, synth.EAM_CREATED)
        self.assertEqual(flow.table_writes(CREATE) - before_create, 0)
        self.assertEqual(flow.table_writes(UPDATE) - before_update, 0)
        after_row = flow.row_of(flow.event_id_of(synth.EAM_D1))
        self.assertEqual(after_row[EVENT_COLUMNS["note"]], HUMAN_NOTE)
        self.assertEqual(after_row[EVENT_COLUMNS["status"]], HUMAN_STATUS)
        self.assertEqual(len(flow.event_rows()), synth.EAM_CREATED)

    def test_table_layer_rejects_duplicates_independently(self):
        """第二层独立成立：预检被绕过时，表格层 append 兜底拒绝（不双写、不中断）。"""

        class BlindPrecheck(EamPullService):
            """把第一层预检关掉（返回空集），验证第二层单独也能挡住。"""

            def existing_event_ids(self):
                return frozenset()

        flow = synth.eam_flow()
        first = flow.run()
        # 新进程/新内存（未 restore）：表里有行、内存为空 → 只能靠表格层挡
        restarted = synth.eam_flow(adapter=flow.adapter, create_shift=True)
        self.assertEqual(restarted.intake.store.event_count, 0)
        before_creates = sum(
            1 for call in restarted.adapter.calls_for(CREATE) if call.table == EVENT_TABLE
        )
        blind = BlindPrecheck(restarted.shift, restarted.intake, restarted.eam)
        report = blind.pull()
        self.assertEqual(report.created, 0)
        self.assertEqual(report.skipped, synth.EAM_CREATED)
        self.assertEqual(report.rejected, 1)
        skipped_reasons = [
            entry.reason for entry in report.entries if entry.action == ACTION_SKIPPED
        ]
        self.assertEqual(len(skipped_reasons), synth.EAM_CREATED)
        for reason in skipped_reasons:
            self.assertIn(REASON_TABLE_LAYER, reason)
            self.assertIn("不重复落表", reason)
        d1_reason = next(
            entry.reason
            for entry in report.entries
            if entry.ref_no == synth.EAM_D1
        )
        self.assertIn(flow.event_id_of(synth.EAM_D1), d1_reason)
        self.assertEqual(restarted.intake.event_table.count(), first.table_rows)
        event_table_creates = (
            sum(1 for call in restarted.adapter.calls_for(CREATE) if call.table == EVENT_TABLE)
            - before_creates
        )
        self.assertEqual(event_table_creates, 0)  # 表格层拒绝 → 没有第二行
        self.assertEqual(flow.intake.event_table.count(), synth.EAM_CREATED)

    def test_cross_stay_period_is_not_a_duplicate(self):
        """未消缺缺陷在下个驻场期重新提出：不同 shift_id → 不同 event_id → 如实新增。"""
        flow = synth.eam_flow(
            eam=synth.make_eam_adapter(
                defect_records=(synth.eam_open_defect(synth.STAY_PERIOD_SHIFT_ID, "2026-03-15 10:00"),),
                hazard_records=(),
            )
        )
        first = flow.run()
        self.assertEqual(first.created, 1)
        self.assertEqual(first.table_rows, 1)

        next_shift = synth.make_stay_period_shift(
            shift_id="SYNTH-SHIFT-STAY-0002",
            shift_date=synth.stay_period_day(30),  # 2026-04-01
            start_time=synth.STAY_PERIOD_END,
            end_time=synth.STAY_PERIOD_END.replace(month=5, day=1),
        )
        next_flow = synth.eam_flow(
            shift=next_shift,
            adapter=flow.adapter,
            eam=synth.make_eam_adapter(
                defect_records=(synth.eam_open_defect(next_shift.shift_id, "2026-04-10 10:00"),),
                hazard_records=(),
            ),
        )
        second = next_flow.run()
        self.assertEqual(second.created, 1)
        self.assertEqual(second.skipped, 0)
        self.assertNotEqual(
            flow.event_id_of(synth.EAM_OPEN), next_flow.event_id_of(synth.EAM_OPEN)
        )
        refs = [
            row[EVENT_COLUMNS["ref_no"]]
            for row in flow.event_rows() + next_flow.event_rows()
        ]
        self.assertEqual(refs.count(synth.EAM_OPEN), 2)
        self.assertEqual(flow.intake.event_table.count(), 2)

    def test_repeat_pull_does_not_replay_writes_or_raise(self):
        flow = synth.eam_flow()
        flow.run()
        for _ in range(3):
            report = flow.run()  # 不抛错、不中断
            self.assertEqual(report.created, 0)
        self.assertEqual(flow.intake.event_table.count(), synth.EAM_CREATED)


class TestEntryGuards(unittest.TestCase):
    """入口守卫：只读 + 只接合成适配器。"""

    def test_real_looking_eam_adapter_is_rejected_at_entry(self):
        class RealLookingEam(EamReadOnlyAdapter):
            name = "eam"
            offline = False

            def list_defects(self, window):  # pragma: no cover - 不会被调用
                return ()

            def list_hazards(self, window):  # pragma: no cover - 不会被调用
                return ()

        flow = synth.eam_flow()
        with self.assertRaises(AssertionError):
            EamPullService(flow.shift, flow.intake, RealLookingEam())

    def test_mismatched_shift_is_rejected(self):
        flow = synth.eam_flow()
        other = synth.make_stay_period_shift(shift_id="SYNTH-SHIFT-STAY-0009")
        with self.assertRaises(Exception):
            EamPullService(other, flow.intake, flow.eam)

    def test_window_of_another_shift_is_rejected(self):
        flow = synth.eam_flow()
        other_window = flow.pull.window()
        mismatched = type(other_window)(
            shift_id="SYNTH-SHIFT-STAY-0009", start=other_window.start, end=other_window.end
        )
        with self.assertRaises(Exception):
            flow.run(window=mismatched)

    def test_pull_reads_only(self):
        """拉取期间对交接事件表只有新增，没有更新（只 append 不 update）。"""
        flow = synth.eam_flow()
        flow.run()
        self.assertEqual(flow.table_writes(UPDATE), 0)
        self.assertGreater(flow.table_writes(CREATE), 0)

    def test_eam_reads_are_traceable(self):
        flow = synth.eam_flow()
        flow.run()
        methods = [call.method for call in flow.eam.calls]
        self.assertEqual(methods, ["list_defects", "list_hazards"])


if __name__ == "__main__":
    unittest.main()
