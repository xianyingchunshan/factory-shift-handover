"""skill 告警拦截测试（T06 / issue #19 验收：告警拦截路径可复现）。

覆盖：

- **E001 拒收不落表**：事件表没有该行，证据在输入留痕表；
- **E002 / E003 / E004 落表 + 告警**：行在表内、告警在账本；
- **未清告警禁提交/禁出清单**：退出码 2、班次置 ``blocked``、不产出 ``checklist.txt``；
- **E003 对驻场期整体区间判定**（口径 3）：跨零点相连日属期内不告警，仅期前/期后 1 分钟告警。

数据全部为 ``SYNTH-`` 前缀合成值；不联网、不接真实钉钉/AI表格。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.enums import ShiftStatus  # noqa: E402

from integrations.aitable.cells import EVENT_COLUMNS  # noqa: E402
from integrations.aitable.tables import EventTable, ShiftTable  # noqa: E402

from skill.config import parse_input  # noqa: E402
from skill.pipeline import OUTCOME_BLOCKED, run  # noqa: E402

import skill_synth as synth  # noqa: E402

#: 告警示例里的五条事件（4 条落表 + 1 条 E001 拒收）。
E001_EVENT = "SYNTH-STAY-EVT-1005"
E002_EVENT = "SYNTH-STAY-EVT-1002"
E003_EVENT = "SYNTH-STAY-EVT-1003"
E004_EVENT = "SYNTH-STAY-EVT-1004"


def _outcomes_by_id(payload: dict) -> dict:
    return {item["event_id"]: item for item in payload["intake"]["outcomes"]}


def _rules(outcome) -> list[str]:
    return [alarm.rule for alarm in outcome.alarms]


def _row(event_id: str, occurred_at: str) -> dict:
    return {
        "event_id": event_id,
        "category": "runtime",
        "description": f"SYNTH 边界用例 {event_id}",
        "occurred_at": occurred_at,
        "severity": "normal",
        "status": "done",
        "ref_no": "SYNTH-REF-200",
        "note": "",
    }


class TestBlockedRunProducesNoChecklist(unittest.TestCase):
    """未清告警：禁提交、禁出清单，只落结果与快照。"""

    def test_blocked_example_exits_two_without_checklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            proc = synth.run_cli("--input", str(synth.BLOCKED_EXAMPLE), "--out", str(out))
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("不出清单", proc.stdout)
            self.assertFalse((out / "checklist.txt").exists())
            self.assertEqual(sorted(item.name for item in out.iterdir()), ["result.json", "state.json"])
            payload = synth.result_of(out)
            self.assertEqual(payload["outcome"], OUTCOME_BLOCKED)
            self.assertEqual(payload["exit_code"], 2)
            self.assertIsNone(payload["checklist"])
            self.assertIsNone(payload["summary"])  # 没有清单就没有汇总
            # 被拦也不改标题口径（口径 2）
            self.assertEqual(payload["title"], "交接班清单 · 驻场期")

    def test_shift_is_marked_blocked_and_not_submitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            self.assertEqual(synth.run_cli("--input", str(synth.BLOCKED_EXAMPLE), "--out", str(out)).returncode, 2)
            payload = synth.result_of(out)
            self.assertEqual(payload["shift"]["status"], ShiftStatus.BLOCKED.value)
            self.assertTrue(payload["shift"]["is_blocked"])
            self.assertEqual(payload["blocked"]["status"], ShiftStatus.BLOCKED.value)
            self.assertTrue(payload["blocked"]["blocked"])
            self.assertEqual(payload["blocked"]["generated_at"], "2026-04-01 09:00")

    def test_shift_table_row_carries_the_blocked_status(self):
        spec = synth.spec_of(synth.BLOCKED_EXAMPLE)
        result = run(spec)
        self.assertEqual(result.outcome, OUTCOME_BLOCKED)
        self.assertEqual(result.exit_code, 2)
        row = ShiftTable(result.adapter).get(spec.shift.shift_id)
        self.assertIsNotNone(row)
        self.assertEqual(row.status, ShiftStatus.BLOCKED.value)


class TestFourAlarmRules(unittest.TestCase):
    """E001–E004 的取舍在结果里逐条可复现。"""

    def test_e001_is_rejected_and_never_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            self.assertEqual(synth.run_cli("--input", str(synth.BLOCKED_EXAMPLE), "--out", str(out)).returncode, 2)
            payload = synth.result_of(out)
            outcomes = _outcomes_by_id(payload)
            self.assertTrue(outcomes[E001_EVENT]["rejected"])
            self.assertFalse(outcomes[E001_EVENT]["stored"])
            self.assertEqual(outcomes[E001_EVENT]["record_id"], "")
            self.assertEqual(outcomes[E001_EVENT]["alarms"], ["E001"])
            self.assertEqual(payload["tables"]["events"], 4)  # 拒收不落表
            self.assertEqual(payload["tables"]["intake_journal"], 5)  # 拒收证据在留痕表
            rejected = payload["blocked"]["rejected_entries"]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["处置"], "rejected")

    def test_e002_e003_e004_are_stored_with_alarms(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            self.assertEqual(synth.run_cli("--input", str(synth.BLOCKED_EXAMPLE), "--out", str(out)).returncode, 2)
            payload = synth.result_of(out)
            outcomes = _outcomes_by_id(payload)
            for event_id, rule in ((E002_EVENT, "E002"), (E003_EVENT, "E003"), (E004_EVENT, "E004")):
                with self.subTest(event_id=event_id):
                    self.assertTrue(outcomes[event_id]["stored"])
                    self.assertFalse(outcomes[event_id]["rejected"])
                    self.assertEqual(outcomes[event_id]["alarms"], [rule])
                    self.assertTrue(outcomes[event_id]["record_id"])
            self.assertEqual(
                payload["alarms"]["counts_by_rule"],
                {"E001": 1, "E002": 1, "E003": 1, "E004": 1},
            )
            self.assertEqual(payload["alarms"]["open_count"], 4)
            self.assertEqual(len(payload["blocked"]["open_alarms"]), 4)
            self.assertEqual(
                sorted(alarm["rule"] for alarm in payload["blocked"]["open_alarms"]),
                ["E001", "E002", "E003", "E004"],
            )

    def test_e002_keeps_the_raw_time_text_in_the_table(self):
        spec = synth.spec_of(synth.BLOCKED_EXAMPLE)
        result = run(spec)
        rows = {
            row.fields[EVENT_COLUMNS["event_id"]]: row.fields
            for row in EventTable(result.adapter).list_rows()
        }
        self.assertIn("2026-03-20", rows[E002_EVENT][EVENT_COLUMNS["occurred_at"]])
        self.assertEqual(rows[E002_EVENT][EVENT_COLUMNS["completeness"]], "partial_time")

    def test_alarm_journal_is_empty_when_nothing_is_cleared(self):
        """本 skill 不代替人工清告警：跑完仍是 4 条未清，清除留痕为空。"""
        result = run(synth.spec_of(synth.BLOCKED_EXAMPLE))
        payload = result.to_dict()
        self.assertEqual(payload["tables"]["alarm_journal"], 0)
        self.assertEqual(payload["alarms"]["open_count"], payload["alarms"]["alarm_count"])
        self.assertEqual(len(payload["blocked"]["open_alarms"]), len(payload["alarms"]["all"]))


class TestE003JudgesTheWholeStayPeriod(unittest.TestCase):
    """口径 3：E003 对 ``[start_time, end_time]`` 整体区间判定，跨零点相连日属期内。"""

    def _payload(self) -> dict:
        payload = synth.payload_of(synth.BLOCKED_EXAMPLE)
        payload["events"] = [
            _row("SYNTH-STAY-EVT-2001", "2026-03-02 23:59"),  # 首日末尾：期内
            _row("SYNTH-STAY-EVT-2002", "2026-03-03 00:01"),  # 跨零点相连日：期内
            _row("SYNTH-STAY-EVT-2003", "2026-03-02 07:59"),  # 期前 1 分钟：越界
            _row("SYNTH-STAY-EVT-2004", "2026-04-01 08:01"),  # 期后 1 分钟：越界
        ]
        return payload

    def test_only_out_of_period_minutes_raise_e003(self):
        result = run(parse_input(self._payload(), source="in-memory"))
        outcomes = {item.event_id: item for item in result.outcomes}
        self.assertEqual(_rules(outcomes["SYNTH-STAY-EVT-2001"]), [])
        self.assertEqual(_rules(outcomes["SYNTH-STAY-EVT-2002"]), [])
        self.assertEqual(_rules(outcomes["SYNTH-STAY-EVT-2003"]), ["E003"])
        self.assertEqual(_rules(outcomes["SYNTH-STAY-EVT-2004"]), ["E003"])
        self.assertTrue(all(item.stored for item in result.outcomes))  # E003 落表
        self.assertEqual(result.to_dict()["tables"]["events"], 4)
        self.assertEqual(result.outcome, OUTCOME_BLOCKED)  # 未清 → 不出清单
        self.assertFalse(result.checklist_lines)

    def test_e003_detail_quotes_the_stay_period_window(self):
        result = run(parse_input(self._payload(), source="in-memory"))
        details = [alarm.to_dict()["detail"] for alarm in result.ledger().all_alarms()]
        self.assertEqual(len(details), 2)
        for detail in details:
            self.assertIn("2026-03-02 08:00~2026-04-01 08:00", detail)


if __name__ == "__main__":
    unittest.main()
