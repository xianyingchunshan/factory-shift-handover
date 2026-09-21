"""skill 端到端测试（T06 / issue #19 第 1 段验收：输入 → 校验 → 表格 → 清单）。

覆盖：

- **真实命令**端到端：``python -m skill.run --input <合成夹具> --out <目录>`` → 退出码 0，
  产出 ``checklist.txt`` / ``result.json`` / ``state.json``；
- 清单**标题口径**（口径 2）：驻场期单 = 「交接班清单 · 驻场期」，与
  :func:`workflow.checklist.checklist_title` 同源；``custom`` 等历史取值口径不变（口径 1）；
- 五段结构、时间序、关键级置顶、未完移交、完整性汇总；
- 默认班次名 = 驻场期（口径 1）；``shift_date`` 取驻场期起始日（口径 4）；
- 确定性：同一输入两次运行产物逐字一致；同一输入内重复 ``event_id`` 不双写（幂等）；
- 离线自检：接非合成适配器直接断言失败。

数据全部为 ``SYNTH-`` 前缀合成值；命令不联网、不接真实钉钉/AI表格。
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

from contracts.shift import ShiftRecord  # noqa: E402

from skill.config import SkillInputError, parse_input  # noqa: E402
from skill.pipeline import OUTCOME_CHECKLIST, run  # noqa: E402
from workflow.checklist import checklist_title  # noqa: E402

import skill_synth as synth  # noqa: E402

#: 合规示例（5 条事件）的预期时间序（口径：occurred_at 升序，全量）。
EXPECTED_FLOW_TIMES = [
    "2026-03-02 08:10",
    "2026-03-02 23:59",
    "2026-03-15 10:00",
    "2026-03-20 14:20",
    "2026-04-01 08:00",
]


class TestSkillCliEndToEnd(unittest.TestCase):
    """真实命令端到端：一条命令跑通三步，产出清单与结果。"""

    def test_cli_runs_the_three_stages_and_writes_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            proc = synth.run_cli("--input", str(synth.CLEAN_EXAMPLE), "--out", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                sorted(item.name for item in out.iterdir()),
                ["checklist.txt", "result.json", "state.json"],
            )
            # 过程段：合成适配器、无网络、落表/拒收/重复计数
            self.assertIn("过程（合成适配器，无网络）", proc.stdout)
            self.assertIn("落表 5 / 拒收 0 / 重复 0", proc.stdout)

    def test_checklist_title_follows_the_stay_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            self.assertEqual(synth.run_cli("--input", str(synth.CLEAN_EXAMPLE), "--out", str(out)).returncode, 0)
            lines = synth.checklist_lines(out)
            self.assertEqual(lines[0], "交接班清单 · 驻场期")
            shift = synth.spec_of().shift
            self.assertEqual(lines[0], checklist_title(shift))  # 与口径函数同源，不另立一套
            self.assertTrue(shift.is_stay_period)
            payload = synth.result_of(out)
            self.assertEqual(payload["title"], "交接班清单 · 驻场期")
            self.assertEqual(payload["shift"]["shift_name_label"], "驻场期")
            self.assertTrue(payload["shift"]["is_stay_period"])

    def test_checklist_has_five_sections_in_contract_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            self.assertEqual(synth.run_cli("--input", str(synth.CLEAN_EXAMPLE), "--out", str(out)).returncode, 0)
            lines = synth.checklist_lines(out)
            markers = ["■ 一、重要事项", "■ 二、事件流水", "■ 三、未完事项移交接班人", "■ 四、完整性校验", "■ 五、确认区"]
            positions = [next(i for i, line in enumerate(lines) if line.startswith(marker)) for marker in markers]
            self.assertEqual(positions, sorted(positions))
            self.assertEqual(len(set(positions)), 5)
            self.assertIn("全量 5 项", lines[positions[1]])
            self.assertIn("关键级 1 项", lines[positions[0]])

    def test_event_flow_is_complete_and_in_time_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            self.assertEqual(synth.run_cli("--input", str(synth.CLEAN_EXAMPLE), "--out", str(out)).returncode, 0)
            lines = synth.checklist_lines(out)
            flow = synth.section_lines(lines, "二、事件流水", "三、未完事项")
            self.assertEqual(synth.timestamps_of(flow), EXPECTED_FLOW_TIMES)
            critical = synth.section_lines(lines, "一、重要事项", "二、事件流水")
            self.assertEqual(synth.timestamps_of(critical), ["2026-03-20 14:20"])
            self.assertTrue(critical[0].strip().startswith("**"))  # 关键级置顶加粗
            pending = synth.section_lines(lines, "三、未完事项", "四、完整性校验")
            self.assertEqual(synth.timestamps_of(pending), ["2026-03-20 14:20"])
            summary = synth.result_of(out)["summary"]
            self.assertEqual(
                (summary["total"], summary["complete"], summary["time_complete_rate"], summary["alarm_count"]),
                (5, 5, 1.0, 0),
            )
            self.assertEqual((summary["critical_items"], summary["pending_transfers"]), (1, 1))

    def test_result_payload_documents_the_three_stages_and_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            self.assertEqual(synth.run_cli("--input", str(synth.CLEAN_EXAMPLE), "--out", str(out)).returncode, 0)
            payload = synth.result_of(out)
            self.assertEqual(payload["outcome"], OUTCOME_CHECKLIST)
            self.assertEqual(payload["exit_code"], 0)
            self.assertEqual(payload["mode"], "fresh")
            self.assertEqual(payload["source"], synth.CLEAN_EXAMPLE.name)
            # 输入段：班次边界 = 驻场期边界；shift_date = 驻场期起始日（口径 4）
            self.assertEqual(payload["shift"]["window_start"], "2026-03-02 08:00")
            self.assertEqual(payload["shift"]["window_end"], "2026-04-01 08:00")
            self.assertEqual(
                (payload["shift"]["first_day"], payload["shift"]["last_day"]),
                ("2026-03-02", "2026-04-01"),
            )
            self.assertEqual(payload["shift"]["calendar_days"], 31)
            self.assertEqual(payload["shift"]["shift_date"], "2026-03-02")
            self.assertEqual(payload["shift"]["status"], "submitted")
            # 过程段与输出段
            self.assertEqual(payload["intake"]["stored"], 5)
            self.assertEqual(payload["tables"]["events"], 5)
            self.assertEqual(payload["consistency"]["ok"], True)
            self.assertIsNone(payload["blocked"])
            self.assertEqual(payload["checklist"]["first_line"], "交接班清单 · 驻场期")
            self.assertEqual(len(payload["checklist"]["sha256"]), 64)
            # 随卡口径逐条在结果里留痕
            for note in (
                "默认班次名=驻场期（stay_period）；custom 等保留给历史数据与例外",
                "清单标题走 workflow.checklist.checklist_title()；驻场期单=「交接班清单 · 驻场期」",
                "驻场期边界=start_time/end_time，E003 对整体区间判定（T04 冻结口径，不另立一套）",
                "建单 shift_date 取驻场期起始日（start_time 的日期）",
            ):
                self.assertIn(note, payload["protocol"])

    def test_rerun_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a", Path(tmp) / "b"
            for out in (first, second):
                self.assertEqual(synth.run_cli("--input", str(synth.CLEAN_EXAMPLE), "--out", str(out)).returncode, 0)
            for name in ("checklist.txt", "result.json", "state.json"):
                self.assertEqual(
                    synth.read_text(first / name),
                    synth.read_text(second / name),
                    f"{name} 两次运行应逐字一致（确定性、无墙钟）",
                )


class TestInputContractDefaults(unittest.TestCase):
    """输入口径：默认驻场期、custom 例外、shift_date 取起始日。"""

    def test_shift_name_defaults_to_stay_period(self):
        payload = synth.payload_of(synth.BLOCKED_EXAMPLE)  # 该夹具未写 shift_name
        self.assertNotIn("shift_name", payload["shift"])
        spec = parse_input(payload, source=Path(synth.BLOCKED_EXAMPLE).name)
        self.assertEqual(spec.shift.shift_name, "stay_period")
        self.assertEqual(checklist_title(spec.shift), "交接班清单 · 驻场期")

    def test_custom_shift_name_keeps_the_historical_title(self):
        payload = synth.payload_of()
        payload["shift"]["shift_name"] = "custom"
        spec = parse_input(payload)
        self.assertEqual(spec.shift.shift_name, "custom")
        self.assertEqual(checklist_title(spec.shift), "交接班清单 · 自定义")
        result = run(spec)
        self.assertEqual(result.title, "交接班清单 · 自定义")

    def test_shift_date_defaults_to_the_first_day_of_the_stay_period(self):
        payload = synth.payload_of()
        payload["shift"].pop("shift_date", None)
        spec = parse_input(payload)
        self.assertEqual(spec.shift.shift_date.isoformat(), "2026-03-02")
        # 显式给出也只允许等于起始日
        payload["shift"]["shift_date"] = "2026-03-02"
        self.assertEqual(parse_input(payload).shift.shift_date.isoformat(), "2026-03-02")

    def test_shift_date_inside_the_period_is_rejected(self):
        payload = synth.payload_of()
        payload["shift"]["shift_date"] = "2026-03-15"
        with self.assertRaises(SkillInputError) as ctx:
            parse_input(payload)
        self.assertIn("必须等于驻场期起始日 2026-03-02", str(ctx.exception))


class TestDuplicateRowsAreIdempotent(unittest.TestCase):
    """同一输入内重复 ``event_id``：不双写，标记为 duplicate。"""

    def test_duplicate_event_id_is_not_written_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = synth.payload_of()
            payload["events"] = [*payload["events"], dict(payload["events"][0])]
            path = synth.write_json(Path(tmp) / "dup.json", payload)
            out = Path(tmp) / "out"
            proc = synth.run_cli("--input", str(path), "--out", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = synth.result_of(out)
            self.assertEqual(result["intake"]["submitted"], 6)
            self.assertEqual(result["intake"]["duplicate"], 1)
            self.assertEqual(result["tables"]["events"], 5)  # 事件表仍是 5 行
            self.assertEqual(result["intake"]["outcomes"][-1]["duplicate"], True)
            self.assertEqual(result["intake"]["outcomes"][-1]["record_id"], "")


class TestOfflineGuard(unittest.TestCase):
    """离线口径：真实适配器一律拒绝（真实读写属 L4）。"""

    def test_non_synthetic_adapter_is_refused(self):
        class _RealAdapter:  # 模拟真实钉钉适配器（非合成）
            name = "dingtalk"
            offline = False

        with self.assertRaises(AssertionError) as ctx:
            run(synth.spec_of(), adapter=_RealAdapter())
        self.assertIn("只允许接合成适配器", str(ctx.exception))


class TestSkillInputDataclass(unittest.TestCase):
    """Spec 摘要只含合成业务键，不含本机绝对路径。"""

    def test_input_summary_is_path_free(self):
        spec = synth.spec_of()
        summary = spec.to_dict()
        self.assertEqual(summary["source"], synth.CLEAN_EXAMPLE.name)
        self.assertNotIn(str(synth.ROOT), str(summary))
        self.assertIsInstance(spec.shift, ShiftRecord)


if __name__ == "__main__":
    unittest.main()
