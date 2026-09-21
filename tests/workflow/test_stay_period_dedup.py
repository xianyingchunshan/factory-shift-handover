"""工作流集成测试：驻场期单重复建单防线（T07，issue #21）。

本卡最易漏的点是"**两层都要挡**"，故这里的重点是**逐层隔离**用例：

- ``test_table_layer_alone_...``：内存键层**什么都不知道**（独立登记簿、零条记录）时，
  仍必须被表层（:meth:`ShiftTable.find` → ``create_shift``）拦住 → 只改内存层过不了这关。
- ``test_memory_layer_alone_...``：班次表里**一行都没有**时，仍必须被内存键层
  （:meth:`ShiftRegistry.register`）拦住 → 只改表层过不了这关。

另有：修前缺口场景修后必被拦（G1 构造期 / 键 / 窗口重叠三条路径）、四组边界端到端
（重叠/包含/相接/不相交，**相接放行**）、既有四类班次零回归。

合成数据：``SYNTH-`` 前缀、全部虚构；时钟固定注入；不发网络请求、不接真实系统。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.errors import DUPLICATE_SHIFT, ContractViolation  # noqa: E402
from contracts.shift import ShiftRecord  # noqa: E402
from contracts.timebase import SHANGHAI  # noqa: E402

import wf_synth as synth  # noqa: E402

BASE_ID = "SYNTH-STAY-A"  # 合成用的"第二张驻场期单"（窗口同基准）
DEFAULT_STAY_ID = synth.STAY_PERIOD_SHIFT_ID  # stay_period_flow 默认那张单的 ID
LEGACY_NAMES: tuple[str, ...] = ("early", "middle", "late", "custom")


def moment(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=SHANGHAI)


def stay(shift_id: str, start: datetime, end: datetime, **overrides) -> ShiftRecord:
    """按 G1 口径造驻场期单：``shift_date`` 自动取起始日。"""
    params = {
        "shift_id": shift_id,
        "shift_date": start.date(),
        "start_time": start,
        "end_time": end,
    }
    params.update(overrides)
    return synth.make_stay_period_shift(**params)


def base_stay(shift_id: str = BASE_ID) -> ShiftRecord:
    return stay(shift_id, synth.STAY_PERIOD_START, synth.STAY_PERIOD_END)


class TestOriginalGapIsClosed(unittest.TestCase):
    """修前缺口：同交接线同驻场期、两个不同 ``shift_date`` 两次建单都能成功。"""

    def test_original_repro_construction_is_now_rejected_by_g1(self):
        """修前：``shift_date=2026-03-20``（区间内）能构造成功；修后：G1 直接拒绝。"""
        with self.assertRaises(ContractViolation) as caught:
            synth.make_stay_period_shift(shift_id="SYNTH-STAY-B", shift_date=date(2026, 3, 20))
        self.assertIn("驻场期单以起始日为业务日期", str(caught.exception))

    def test_same_window_same_business_date_cannot_create_a_second_row(self):
        """同起始日的两次建单落同一个键 → 拒绝，且表里只有一行。"""
        flow = synth.stay_period_flow()
        rival = synth.stay_period_flow(
            shift=base_stay("SYNTH-STAY-B"), adapter=flow.adapter, create_shift=False
        )
        with synth.expect_code(DUPLICATE_SHIFT):
            rival.intake.create_shift()
        self.assertEqual(flow.intake.shift_table.count(), 1)

    def test_overlapping_window_cannot_create_a_second_row(self):
        """同驻场期的不同窗口（= 同一驻场期的另一种写法）也建不出第二单。"""
        flow = synth.stay_period_flow()
        rival = synth.stay_period_flow(
            shift=stay("SYNTH-STAY-B", moment(3, 20, 8, 0), moment(4, 10, 8, 0)),
            adapter=flow.adapter,
            create_shift=False,
        )
        with synth.expect_code(DUPLICATE_SHIFT):
            rival.intake.create_shift()
        self.assertEqual(flow.intake.shift_table.count(), 1)


class TestBothLayersBlock(unittest.TestCase):
    """两层都要挡：逐层隔离，任何一层缺失都会漏放。"""

    def test_table_layer_alone_blocks_when_the_registry_knows_nothing(self):
        """表层单独挡：登记簿是新建的（零条记录），重复建单仍被拒。"""
        flow_a = synth.stay_period_flow()
        rival = synth.stay_period_flow(
            shift=stay("SYNTH-STAY-B", moment(3, 20, 8, 0), moment(4, 10, 8, 0)),
            adapter=flow_a.adapter,
            create_shift=False,
        )
        self.assertEqual(rival.intake.registry.created_count, 0, "内存键层此时不知情")
        self.assertEqual(rival.intake.shift_table.count(), 1, "表里有第一单")
        with synth.expect_code(DUPLICATE_SHIFT):
            rival.intake.create_shift()
        self.assertEqual(flow_a.intake.shift_table.count(), 1, "不双写")

    def test_memory_layer_alone_blocks_when_the_table_has_no_row(self):
        """内存键层单独挡：班次表零行，重复建单仍被拒。"""
        flow = synth.stay_period_flow(
            shift=stay("SYNTH-STAY-B", moment(3, 20, 8, 0), moment(4, 10, 8, 0)),
            create_shift=False,
        )
        flow.intake.registry.register(base_stay())
        self.assertEqual(flow.intake.shift_table.count(), 0, "表里此时没有任何行")
        with synth.expect_code(DUPLICATE_SHIFT):
            flow.intake.create_shift()
        self.assertEqual(flow.intake.shift_table.count(), 0, "被拦在落表之前")

    def test_table_layer_duplicate_detail_names_the_overlap_reason(self):
        flow_a = synth.stay_period_flow()
        incoming = stay("SYNTH-STAY-B", moment(3, 20, 8, 0), moment(4, 10, 8, 0))
        rival = synth.stay_period_flow(
            shift=incoming, adapter=flow_a.adapter, create_shift=False
        )
        try:
            rival.intake.create_shift()
        except Exception as exc:  # noqa: BLE001 - 测试断言错误内容
            self.assertEqual(exc.code, DUPLICATE_SHIFT)
            self.assertEqual(exc.detail["reason"], "stay_period_window_overlap")
            self.assertEqual(exc.detail["existing_shift_id"], DEFAULT_STAY_ID)
            self.assertEqual(exc.detail["incoming_window"], "2026-03-20 08:00~2026-04-10 08:00")
        else:  # pragma: no cover - 未抛错即失败
            self.fail("应当抛出 DUPLICATE_SHIFT")


class TestFourBoundariesEndToEnd(unittest.TestCase):
    """四组边界：重叠 / 包含（拒绝）；相接 / 不相交（放行）。"""

    def _rival(self, flow, start: datetime, end: datetime, shift_id: str = "SYNTH-STAY-B"):
        return synth.stay_period_flow(
            shift=stay(shift_id, start, end), adapter=flow.adapter, create_shift=False
        )

    def test_contained_window_is_refused(self):
        flow = synth.stay_period_flow()
        rival = self._rival(flow, moment(3, 10, 8, 0), moment(3, 20, 8, 0))
        with synth.expect_code(DUPLICATE_SHIFT):
            rival.intake.create_shift()
        self.assertEqual(flow.intake.shift_table.count(), 1)

    def test_touching_window_is_allowed(self):
        """相接放行：03-02~04-01 与 04-01~05-01。"""
        flow = synth.stay_period_flow()
        rival = self._rival(flow, moment(4, 1, 8, 0), moment(5, 1, 8, 0))
        result = rival.intake.create_shift()
        self.assertTrue(result.created)
        self.assertEqual(flow.intake.shift_table.count(), 2)
        self.assertEqual(
            [shift.shift_id for shift in flow.intake.shift_table.list_all()],
            [DEFAULT_STAY_ID, "SYNTH-STAY-B"],
        )

    def test_touching_window_before_is_allowed(self):
        flow = synth.stay_period_flow()
        rival = self._rival(flow, moment(1, 30, 8, 0), moment(3, 2, 8, 0))
        self.assertTrue(rival.intake.create_shift().created)
        self.assertEqual(flow.intake.shift_table.count(), 2)

    def test_disjoint_window_is_allowed(self):
        flow = synth.stay_period_flow()
        rival = self._rival(flow, moment(5, 2, 8, 0), moment(5, 20, 8, 0))
        self.assertTrue(rival.intake.create_shift().created)
        self.assertEqual(flow.intake.shift_table.count(), 2)

    def test_other_line_same_window_is_allowed(self):
        flow = synth.stay_period_flow()
        rival = synth.stay_period_flow(
            shift=stay(
                "SYNTH-STAY-B",
                moment(3, 20, 8, 0),
                moment(4, 10, 8, 0),
                handover_line="SYNTH-LINE-B",
            ),
            adapter=flow.adapter,
            create_shift=False,
        )
        self.assertTrue(rival.intake.create_shift().created)
        self.assertEqual(flow.intake.shift_table.count(), 2)

    def test_same_shift_id_resubmit_is_still_idempotent(self):
        flow = synth.stay_period_flow()
        again = flow.intake.create_shift()
        self.assertFalse(again.created)
        self.assertTrue(again.idempotent_noop)
        self.assertEqual(flow.intake.shift_table.count(), 1)


class TestLegacyFlowsAreUnchanged(unittest.TestCase):
    """零回归：既有四类班次的建单与判重路径不受驻场期口径影响。"""

    def test_legacy_shift_can_coexist_with_a_stay_period_on_the_same_day(self):
        flow = synth.stay_period_flow()
        day = moment(3, 20, 8, 0)
        for name in LEGACY_NAMES:
            with self.subTest(shift_name=name):
                rival = synth.make_flow(
                    shift=synth.make_shift(
                        shift_id=f"SYNTH-SHIFT-{name}",
                        shift_name=name,
                        shift_date=date(2026, 3, 20),
                        start_time=day,
                        end_time=moment(3, 20, 20, 0),
                    ),
                    adapter=flow.adapter,
                    create_shift=False,
                )
                self.assertTrue(rival.intake.create_shift().created)
        self.assertEqual(flow.intake.shift_table.count(), 1 + len(LEGACY_NAMES))

    def test_legacy_duplicate_message_is_byte_identical(self):
        for name in LEGACY_NAMES:
            with self.subTest(shift_name=name):
                flow = synth.make_flow(shift=synth.make_shift(shift_name=name))
                rival = synth.make_flow(
                    shift=synth.make_shift(shift_id=synth.OTHER_SHIFT_ID, shift_name=name),
                    adapter=flow.adapter,
                    create_shift=False,
                )
                try:
                    rival.intake.create_shift()
                except Exception as exc:  # noqa: BLE001 - 测试断言错误内容
                    self.assertEqual(exc.code, DUPLICATE_SHIFT)
                    self.assertEqual(exc.detail["reason"], "same_idempotency_key")
                    self.assertEqual(
                        exc.message,
                        "同班同线已有班次 SYNTH-SHIFT-0001，"
                        "不为 SYNTH-SHIFT-0002 建第二单",
                    )
                else:  # pragma: no cover - 未抛错即失败
                    self.fail("应当抛出 DUPLICATE_SHIFT")
                self.assertEqual(flow.intake.shift_table.count(), 1)

    def test_legacy_shift_slot_key_is_unchanged(self):
        for name in LEGACY_NAMES:
            with self.subTest(shift_name=name):
                shift = synth.make_shift(shift_name=name)
                self.assertEqual(
                    shift.idempotency_key, ("SYNTH-LINE-A", "2026-01-02", name)
                )


if __name__ == "__main__":
    unittest.main()
