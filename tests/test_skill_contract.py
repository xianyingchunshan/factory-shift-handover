"""skill 输入契约 / 离线隔离 / 重启重放测试（T06 / issue #19 验收）。

覆盖：

- **输入前置校验**：结构、必填留痕、枚举取值、时间精度与可解析性、``shift_id`` / ``shift_date``
  不符、``version`` 不支持 → 输入非法；命令退出码 3，**不落表、不写任何输出文件**；
- **离线隔离**：``skill`` 包静态审计零网络/进程类导入；示例夹具全部 ``SYNTH-`` 合成值；
  产出物不含本机绝对路径；
- **重启 / 重复拉取幂等可回放**：``--resume state.json`` 只读重放，清单与新鲜运行逐字一致，
  不新增表格行、不重复创建待办、恢复期问题为空。

数据全部为 ``SYNTH-`` 前缀合成值；不联网、不接真实钉钉/AI表格。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from integrations.aitable.isolation import find_forbidden_imports  # noqa: E402
from integrations.aitable.tables import SHIFT_TABLE  # noqa: E402

from skill.config import SkillInputError, parse_input  # noqa: E402

import skill_synth as synth  # noqa: E402

#: (用例名, 变异函数, 期望诊断片段)
INVALID_CASES = (
    ("缺 shift", lambda data: data.pop("shift"), "缺少 shift 对象"),
    ("缺 events", lambda data: data.pop("events"), "缺少 events 数组"),
    ("缺 start_time", lambda data: data["shift"].pop("start_time"), "shift 缺少必填字段: start_time"),
    ("时间未到分", lambda data: data["shift"].__setitem__("start_time", "2026-03-02"), "必须精确到分"),
    ("shift_date 非起始日", lambda data: data["shift"].__setitem__("shift_date", "2026-03-15"), "必须等于驻场期起始日"),
    ("枚举非法", lambda data: data["events"][0].__setitem__("category", "unknown_cat"), "取值非法"),
    ("时间不可解析", lambda data: data["events"][0].__setitem__("occurred_at", "2026/3/2 8点"), "无法解析"),
    ("缺 description", lambda data: data["events"][0].__setitem__("description", "  "), "缺少 description"),
    ("缺 event_id", lambda data: data["events"][0].pop("event_id"), "缺少 event_id"),
    ("shift_id 不符", lambda data: data["events"][0].__setitem__("shift_id", "SYNTH-OTHER-0001"), "与班次不符"),
    ("version 不支持", lambda data: data.__setitem__("version", 2), "不支持的输入版本"),
)


class TestInputPreconditions(unittest.TestCase):
    """输入前置校验：一次性报全部问题，不落表、不写文件。"""

    def test_invalid_inputs_are_rejected_with_diagnostics(self):
        base = synth.payload_of()
        for name, mutate, expected in INVALID_CASES:
            with self.subTest(case=name):
                data = json.loads(json.dumps(base))
                mutate(data)
                with self.assertRaises(SkillInputError) as ctx:
                    parse_input(data)
                self.assertIn(expected, str(ctx.exception))

    def test_alarm_paths_are_not_input_errors(self):
        """E001–E004 是业务告警路径：空时间 / 只给日期 / 越界 / 缺字段都不得被前置校验拒掉。"""
        payload = synth.payload_of(synth.BLOCKED_EXAMPLE)
        spec = parse_input(payload)
        self.assertEqual(len(spec.events), 5)
        self.assertIsNone(spec.events[4]["occurred_at"])  # E001 行照旧进入录入
        self.assertEqual(spec.events[1]["occurred_at"], "2026-03-20")  # E002
        self.assertEqual(spec.events[3]["severity"], "")  # E004

    def test_missing_shift_id_on_a_row_defaults_to_the_shift(self):
        payload = synth.payload_of()
        payload["events"][0].pop("shift_id", None)
        spec = parse_input(payload)
        self.assertEqual(spec.events[0]["shift_id"], spec.shift.shift_id)

    def test_cli_exits_three_and_writes_nothing_on_invalid_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = synth.write_json(Path(tmp) / "bad.json", {"version": 1, "shift": {}})
            out = Path(tmp) / "out"
            proc = synth.run_cli("--input", str(bad), "--out", str(out))
            self.assertEqual(proc.returncode, 3)
            self.assertIn("输入非法", proc.stderr)
            self.assertFalse(out.exists())

    def test_cli_exits_three_on_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "broken.json"
            bad.write_text("{ 这不是 JSON", encoding="utf-8")
            out = Path(tmp) / "out"
            proc = synth.run_cli("--input", str(bad), "--out", str(out))
            self.assertEqual(proc.returncode, 3)
            self.assertIn("不是合法 JSON", proc.stderr)
            self.assertFalse(out.exists())

    def test_cli_requires_exactly_one_input_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            both = synth.run_cli(
                "--input", str(synth.CLEAN_EXAMPLE), "--resume", str(synth.CLEAN_EXAMPLE),
                "--out", str(Path(tmp) / "out"),
            )
            self.assertEqual(both.returncode, 2)  # argparse 用法错误
            missing = synth.run_cli("--out", str(Path(tmp) / "out"))
            self.assertEqual(missing.returncode, 2)


class TestOfflineIsolation(unittest.TestCase):
    """离线隔离：静态审计 + 合成数据标注 + 产出物无本机路径。"""

    def test_skill_package_has_no_network_or_process_imports(self):
        findings = find_forbidden_imports(synth.ROOT, packages=("skill",))
        self.assertEqual(findings, [], "skill 包不得引入网络/进程类模块")

    def test_example_fixtures_are_synthetic(self):
        for path in (synth.CLEAN_EXAMPLE, synth.BLOCKED_EXAMPLE):
            payload = synth.payload_of(path)
            shift = payload["shift"]
            with self.subTest(fixture=Path(path).name):
                self.assertTrue(shift["shift_id"].startswith("SYNTH-"))
                self.assertTrue(shift["handover_line"].startswith("SYNTH-"))
                self.assertTrue(shift["handover_from"]["user_id"].startswith("SYNTH-"))
                self.assertTrue(shift["handover_to"]["user_id"].startswith("SYNTH-"))
                for event in payload["events"]:
                    self.assertTrue(event["event_id"].startswith("SYNTH-"))
                    self.assertTrue(event["description"].startswith("SYNTH"))

    def test_outputs_do_not_leak_local_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            self.assertEqual(
                synth.run_cli("--input", str(synth.CLEAN_EXAMPLE), "--out", str(out)).returncode, 0
            )
            result_text = synth.read_text(out / "result.json")
            self.assertNotIn(str(synth.ROOT), result_text)
            self.assertNotIn(str(synth.ROOT), synth.read_text(out / "state.json"))
            self.assertEqual(synth.result_of(out)["source"], synth.CLEAN_EXAMPLE.name)


class TestResumeReplay(unittest.TestCase):
    """重启 / 重复拉取：从快照只读重放，清单逐字一致，不产生新写入。"""

    def _fresh(self, tmp: Path) -> Path:
        fresh = tmp / "fresh"
        proc = synth.run_cli("--input", str(synth.CLEAN_EXAMPLE), "--out", str(fresh))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return fresh

    def test_resume_reproduces_the_same_checklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            fresh = self._fresh(Path(tmp))
            resumed = Path(tmp) / "resumed"
            proc = synth.run_cli("--resume", str(fresh / "state.json"), "--out", str(resumed))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("重放模式", proc.stdout)
            self.assertEqual(
                synth.read_text(fresh / "checklist.txt"),
                synth.read_text(resumed / "checklist.txt"),
            )

    def test_resume_does_not_write_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            fresh = self._fresh(Path(tmp))
            resumed = Path(tmp) / "resumed"
            self.assertEqual(
                synth.run_cli("--resume", str(fresh / "state.json"), "--out", str(resumed)).returncode, 0
            )
            before, after = synth.result_of(fresh), synth.result_of(resumed)
            self.assertEqual(after["mode"], "resume")
            self.assertEqual(after["outcome"], "checklist")
            self.assertEqual(after["title"], before["title"])
            self.assertEqual(after["shift"], before["shift"])
            self.assertEqual(after["summary"], before["summary"])
            self.assertEqual(after["tables"], before["tables"])  # 不新增事件行、不重复建待办
            self.assertEqual(after["restore_issues"], [])
            self.assertIsNone(after["intake"])  # 重放不重放外部写入

    def test_resume_twice_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fresh = self._fresh(Path(tmp))
            first, second = Path(tmp) / "r1", Path(tmp) / "r2"
            for out in (first, second):
                self.assertEqual(
                    synth.run_cli("--resume", str(fresh / "state.json"), "--out", str(out)).returncode, 0
                )
            self.assertEqual(
                synth.read_text(first / "checklist.txt"),
                synth.read_text(second / "checklist.txt"),
            )
            self.assertEqual(synth.result_of(first)["tables"], synth.result_of(second)["tables"])

    def test_resume_rejects_an_unusable_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            missing = synth.run_cli("--resume", str(Path(tmp) / "nope.json"), "--out", str(out))
            self.assertEqual(missing.returncode, 3)
            self.assertIn("快照文件不可读", missing.stderr)
            empty = synth.write_json(Path(tmp) / "empty.json", {SHIFT_TABLE: []})
            proc = synth.run_cli("--resume", str(empty), "--out", str(out))
            self.assertEqual(proc.returncode, 3)
            self.assertIn("没有班次表", proc.stderr)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
