"""契约测试：驻场期单判重（T07，issue #21；SPEC §5 防重口径）。

覆盖四件事：

1. **G1 契约校验**：驻场期单 ``shift_date`` 必须 == ``start_time.date()``（以起始日为业务日期），
   违反 → :class:`~contracts.errors.ContractViolation`；**只对 ``stay_period`` 生效**，
   既有四类班次仍按"落在区间内"判定。
2. **G2 内存键层**：:meth:`contracts.shift.ShiftRegistry.register` 对同交接线、同班次名的
   驻场期**窗口有交集**（含包含）→ ``DUPLICATE_SHIFT``（复用冻结错误码，不新增）。
3. **四组边界**：重叠 / 包含 / 相接 / 不相交；**端点相接必须放行**。
4. **零回归钉死**：既有四类班次（早/中/晚/自定义）的判重键、错误文案与明细逐字节不变。

合成数据：``SYNTH-`` 前缀、全部虚构；时间固定注入，不依赖真实当前时间。
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

from contracts.enums import ShiftName, label_of  # noqa: E402
from contracts.errors import DUPLICATE_SHIFT, ERROR_CODES, ContractViolation  # noqa: E402
from contracts.shift import (  # noqa: E402
    STAY_PERIOD_DEDUP_NOTE,
    ShiftRecord,
    ShiftRegistry,
    stay_period_conflict,
)
from contracts.timebase import format_minute  # noqa: E402

import synth  # noqa: E402

#: 基准驻场期单（30 天：2026-03-02 08:00 ~ 2026-04-01 08:00）。
BASE_ID = "SYNTH-STAY-A"

#: 既有四类班次名（本卡必须零回归）。
LEGACY_NAMES: tuple[str, ...] = ("early", "middle", "late", "custom")


def stay(shift_id: str, start: datetime, end: datetime, **overrides) -> ShiftRecord:
    """按 G1 口径造驻场期单：``shift_date`` 自动取起始日，窗口显式给定。"""
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


class TestStayPeriodBusinessDate(unittest.TestCase):
    """G1：驻场期单以起始日为业务日期（键重新唯一的唯一前提）。"""

    def test_business_date_other_than_the_start_day_is_rejected(self):
        for bad in (date(2026, 3, 3), date(2026, 3, 20), synth.STAY_PERIOD_LAST_DAY):
            with self.subTest(shift_date=bad.isoformat()):
                with self.assertRaises(ContractViolation) as caught:
                    synth.make_stay_period_shift(shift_date=bad)
                message = str(caught.exception)
                self.assertIn("驻场期单以起始日为业务日期", message)
                self.assertIn("2026-03-02 08:00", message)  # 指出真实起始时刻

    def test_start_day_is_the_only_accepted_business_date(self):
        shift = synth.make_stay_period_shift(shift_date=synth.STAY_PERIOD_FIRST_DAY)
        self.assertEqual(shift.shift_date, synth.STAY_PERIOD_FIRST_DAY)
        self.assertEqual(
            shift.idempotency_key,
            (synth.LINE, "2026-03-02", "stay_period"),
        )

    def test_g1_is_scoped_to_stay_period_only(self):
        """既有四类班次不受影响：仍按"shift_date 落在区间内"判定。"""
        window_start = synth.period_at(1, 2, 8, 0)
        window_end = synth.period_at(1, 5, 20, 0)
        for name in LEGACY_NAMES:
            with self.subTest(shift_name=name):
                shift = synth.make_shift(
                    shift_id=f"SYNTH-SHIFT-{name}",
                    shift_name=name,
                    shift_date=date(2026, 1, 4),  # 区间内、非起始日 → 旧口径允许
                    start_time=window_start,
                    end_time=window_end,
                )
                self.assertEqual(shift.shift_date, date(2026, 1, 4))
                self.assertEqual(
                    shift.idempotency_key, (synth.LINE, "2026-01-04", name)
                )
                self.assertFalse(shift.is_stay_period)
        # 旧契约文案逐字节不变
        with self.assertRaises(ContractViolation) as caught:
            synth.make_shift(
                shift_id="SYNTH-SHIFT-OUT",
                shift_date=date(2026, 1, 9),
                start_time=window_start,
                end_time=window_end,
            )
        self.assertEqual(
            str(caught.exception), "shift_date 必须落在班次区间内（跨零点班次取起始日）"
        )

    def test_dedup_note_pins_the_rule(self):
        for fragment in (
            "驻场期单以起始日为业务日期",
            "端点相接不算",
            "DUPLICATE_SHIFT",
        ):
            self.assertIn(fragment, STAY_PERIOD_DEDUP_NOTE)

    def test_error_code_set_is_not_extended(self):
        """复用冻结错误码：一个不增，一个不减。"""
        self.assertEqual(
            ERROR_CODES,
            (
                "E001",
                "E002",
                "E003",
                "E004",
                "AUTH_REQUIRED",
                "DUPLICATE_SHIFT",
                "WRITE_UNKNOWN",
                "NOT_CONFIRMED",
                "ALARM_NOT_CLEARED",
            ),
        )
        self.assertIn(DUPLICATE_SHIFT, ERROR_CODES)


class TestStayPeriodOverlapInRegistry(unittest.TestCase):
    """G2 内存键层：同交接线同驻场期（窗口有交集且非同一 shift_id）不建第二单。"""

    def setUp(self):
        self.registry = ShiftRegistry()
        self.base = base_stay()
        self.registry.register(self.base)

    def _register(self, shift: ShiftRecord):
        return self.registry.register(shift)

    def test_partial_overlap_is_duplicate(self):
        """重叠：03-02~04-01 与 03-20~04-10。"""
        incoming = stay(
            "SYNTH-STAY-B", synth.period_at(3, 20, 8, 0), synth.period_at(4, 10, 8, 0)
        )
        with synth.expect_code(DUPLICATE_SHIFT):
            self._register(incoming)
        self.assertEqual(self.registry.created_count, 1)

    def test_contained_window_is_duplicate(self):
        """包含：03-10~03-20 完全落在基准驻场期内。"""
        incoming = stay(
            "SYNTH-STAY-B", synth.period_at(3, 10, 8, 0), synth.period_at(3, 20, 8, 0)
        )
        with synth.expect_code(DUPLICATE_SHIFT):
            self._register(incoming)
        self.assertEqual(self.registry.created_count, 1)

    def test_containing_window_is_duplicate(self):
        """包含（反向）：基准驻场期落在 02-01~05-01 之内。"""
        incoming = stay(
            "SYNTH-STAY-B", synth.period_at(2, 1, 8, 0), synth.period_at(5, 1, 8, 0)
        )
        with synth.expect_code(DUPLICATE_SHIFT):
            self._register(incoming)
        self.assertEqual(self.registry.created_count, 1)

    def test_touching_window_after_is_allowed(self):
        """相接放行：03-02~04-01 与 04-01~05-01（端点相接不算重叠）。"""
        incoming = stay(
            "SYNTH-STAY-B", synth.period_at(4, 1, 8, 0), synth.period_at(5, 1, 8, 0)
        )
        result = self._register(incoming)
        self.assertTrue(result.created)
        self.assertEqual(self.registry.created_count, 2)

    def test_touching_window_before_is_allowed(self):
        """相接放行（反向）：01-30~03-02 与 03-02~04-01。"""
        incoming = stay(
            "SYNTH-STAY-B", synth.period_at(1, 30, 8, 0), synth.period_at(3, 2, 8, 0)
        )
        result = self._register(incoming)
        self.assertTrue(result.created)
        self.assertEqual(self.registry.created_count, 2)

    def test_disjoint_windows_are_allowed(self):
        """不相交：两个方向都放行。"""
        after = stay(
            "SYNTH-STAY-B", synth.period_at(5, 2, 8, 0), synth.period_at(5, 20, 8, 0)
        )
        before = stay(
            "SYNTH-STAY-C", synth.period_at(1, 1, 8, 0), synth.period_at(2, 1, 8, 0)
        )
        self.assertTrue(self._register(after).created)
        self.assertTrue(self._register(before).created)
        self.assertEqual(self.registry.created_count, 3)

    def test_other_line_is_allowed(self):
        incoming = stay(
            "SYNTH-STAY-B",
            synth.period_at(3, 20, 8, 0),
            synth.period_at(4, 10, 8, 0),
            handover_line="SYNTH-LINE-B",
        )
        self.assertTrue(self._register(incoming).created)
        self.assertEqual(self.registry.created_count, 2)

    def test_overlap_with_another_shift_name_is_allowed(self):
        """G2 只对驻场期生效：既有班次与驻场期可以同区间共存。"""
        legacy = synth.make_shift(
            shift_id="SYNTH-SHIFT-CUSTOM",
            shift_name="custom",
            shift_date=date(2026, 3, 20),
            start_time=synth.period_at(3, 20, 8, 0),
            end_time=synth.period_at(3, 20, 20, 0),
        )
        self.assertTrue(self._register(legacy).created)
        self.assertEqual(self.registry.created_count, 2, "既有班次不被驻场期窗口误伤")

    def test_legacy_shift_is_not_blocked_by_the_stay_period(self):
        """反向：驻场期已在册时，既有四类班次仍按自己的键登记。"""
        for name in LEGACY_NAMES:
            with self.subTest(shift_name=name):
                registry = ShiftRegistry()
                registry.register(base_stay())
                legacy = synth.make_shift(
                    shift_id=f"SYNTH-SHIFT-{name}",
                    shift_name=name,
                    shift_date=date(2026, 3, 20),
                    start_time=synth.period_at(3, 20, 8, 0),
                    end_time=synth.period_at(3, 20, 20, 0),
                )
                self.assertTrue(registry.register(legacy).created)
                self.assertEqual(registry.created_count, 2)

    def test_same_shift_id_resubmit_stays_idempotent(self):
        """同 shift_id 重复登记仍是幂等 no-op（不抛 DUPLICATE_SHIFT，不双写）。"""
        again = self._register(base_stay())
        self.assertFalse(again.created)
        self.assertTrue(again.idempotent_noop)
        self.assertIs(again.record, self.base)
        self.assertEqual(self.registry.created_count, 1)

    def test_duplicate_error_detail_and_message_point_at_the_existing_shift(self):
        incoming = stay(
            "SYNTH-STAY-B", synth.period_at(3, 20, 8, 0), synth.period_at(4, 10, 8, 0)
        )
        try:
            self._register(incoming)
        except Exception as exc:  # noqa: BLE001 - 测试断言错误内容
            self.assertEqual(exc.code, DUPLICATE_SHIFT)
            self.assertEqual(exc.detail["reason"], "stay_period_window_overlap")
            self.assertEqual(exc.detail["existing_shift_id"], BASE_ID)
            self.assertEqual(exc.detail["incoming_shift_id"], "SYNTH-STAY-B")
            self.assertEqual(
                exc.detail["incoming_window"],
                f"{format_minute(incoming.start_time)}~{format_minute(incoming.end_time)}",
            )
            self.assertIn("同交接线同驻场期不重复建单", exc.message)
            self.assertIn("2026-03-02 08:00~2026-04-01 08:00", exc.message)
        else:  # pragma: no cover - 未抛错即失败
            self.fail("应当抛出 DUPLICATE_SHIFT")

    def test_conflict_helper_pins_the_rule(self):
        """:func:`stay_period_conflict` 直接口径：命中返回已存在的那一条，否则 None。"""
        overlapping = stay(
            "SYNTH-STAY-B", synth.period_at(3, 20, 8, 0), synth.period_at(4, 10, 8, 0)
        )
        touching = stay(
            "SYNTH-STAY-C", synth.period_at(4, 1, 8, 0), synth.period_at(5, 1, 8, 0)
        )
        legacy = synth.make_shift(
            shift_id="SYNTH-SHIFT-LATE",
            shift_name="late",
            shift_date=date(2026, 3, 20),
            start_time=synth.period_at(3, 20, 16, 0),
            end_time=synth.period_at(3, 20, 23, 0),
        )
        known = (self.base, legacy)
        self.assertIs(stay_period_conflict(overlapping, known), self.base)
        self.assertIsNone(stay_period_conflict(touching, known))
        self.assertIsNone(stay_period_conflict(self.base, known), "同 shift_id 不算冲突")
        self.assertIsNone(
            stay_period_conflict(legacy, known), "非驻场期单一律不参与窗口重叠判定"
        )


class TestLegacyFourShiftNamesAreUnchanged(unittest.TestCase):
    """零回归钉死：早/中/晚/自定义的判重键、错误文案与明细逐字节不变。"""

    def test_idempotency_keys_are_byte_identical(self):
        for name in LEGACY_NAMES:
            with self.subTest(shift_name=name):
                shift = synth.make_shift(shift_id=f"SYNTH-SHIFT-{name}", shift_name=name)
                self.assertEqual(
                    shift.idempotency_key,
                    ("SYNTH-LINE-A", "2026-01-02", name),
                )
                self.assertEqual(
                    shift.label, f"SYNTH-LINE-A/2026-01-02/{label_of(ShiftName, name)}"
                )

    def test_duplicate_message_and_detail_are_byte_identical(self):
        for name in LEGACY_NAMES:
            with self.subTest(shift_name=name):
                registry = ShiftRegistry()
                registry.register(synth.make_shift(shift_name=name))
                try:
                    registry.register(
                        synth.make_shift(
                            shift_id="SYNTH-SHIFT-0002", shift_name=name
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - 测试断言错误内容
                    self.assertEqual(exc.code, DUPLICATE_SHIFT)
                    self.assertEqual(
                        exc.message,
                        "同班同线已有班次 SYNTH-SHIFT-0001，不为 SYNTH-SHIFT-0002 建第二单",
                    )
                    self.assertEqual(
                        str(exc),
                        "[DUPLICATE_SHIFT] 同班同线已有班次 SYNTH-SHIFT-0001，"
                        "不为 SYNTH-SHIFT-0002 建第二单",
                    )
                    self.assertEqual(exc.detail["reason"], "same_idempotency_key")
                    for key in (
                        "handover_line",
                        "shift_date",
                        "shift_name",
                        "existing_shift_id",
                        "incoming_shift_id",
                    ):
                        self.assertIn(key, exc.detail)
                    self.assertEqual(exc.detail["shift_name"], name)
                else:  # pragma: no cover - 未抛错即失败
                    self.fail("应当抛出 DUPLICATE_SHIFT")

    def test_same_name_on_another_day_is_still_allowed(self):
        for name in LEGACY_NAMES:
            with self.subTest(shift_name=name):
                registry = ShiftRegistry()
                registry.register(synth.make_shift(shift_name=name))
                other_day = synth.make_shift(
                    shift_id="SYNTH-SHIFT-0002",
                    shift_name=name,
                    shift_date=date(2026, 1, 3),
                    start_time=synth.at(8, 0, day=3),
                    end_time=synth.at(20, 0, day=3),
                )
                self.assertTrue(registry.register(other_day).created)
                self.assertEqual(registry.created_count, 2)


if __name__ == "__main__":
    unittest.main()
