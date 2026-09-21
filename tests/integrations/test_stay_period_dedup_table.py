"""集成测试：班次表驻场期判重匹配口径（T07，issue #21）。

覆盖：:meth:`integrations.aitable.tables.ShiftTable.find` 的**匹配口径**——

1. 幂等键（交接线 + 日期 + 班次）路径**逐字节不变**；不传 ``incoming`` 时旧口径不变。
2. 传 ``incoming`` 且它是驻场期单时，追加**窗口重叠**匹配（同交接线、同班次名、
   有交集且非同一 ``shift_id``）；四组边界：重叠 / 包含 / 相接 / 不相交，**相接放行**。
3. **既有四类班次永不参与窗口重叠**（既有行哪怕覆盖整个驻场期也不命中）。
4. 驻场期行解析失败时**不静默跳过**（fail-closed，不放行重复建单）。

合成数据：``SYNTH-`` 前缀、全部虚构；时间固定注入，不依赖真实当前时间；不发网络请求。
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

from contracts.aitable_mapping import SHIFT_FIELD_MAPPINGS, mapping_table  # noqa: E402
from contracts.errors import ContractViolation  # noqa: E402
from contracts.shift import ShiftRecord  # noqa: E402
from contracts.timebase import SHANGHAI  # noqa: E402

from integrations.aitable.tables import SHIFT_TABLE, ShiftTable  # noqa: E402

import aitable_synth as synth  # noqa: E402

SHIFT_COLUMNS = mapping_table(SHIFT_FIELD_MAPPINGS)

BASE_ID = "SYNTH-STAY-A"


def moment(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """驻场期内的合成时刻（Asia/Shanghai）。"""
    return datetime(2026, month, day, hour, minute, tzinfo=SHANGHAI)


def stay(shift_id: str, start: datetime, end: datetime, **overrides) -> ShiftRecord:
    """按 G1 口径造驻场期单：``shift_date`` 自动取起始日。"""
    params = {
        "shift_id": shift_id,
        "shift_name": "stay_period",
        "shift_date": start.date(),
        "start_time": start,
        "end_time": end,
    }
    params.update(overrides)
    return synth.make_shift(**params)


def base_stay(shift_id: str = BASE_ID) -> ShiftRecord:
    """基准驻场期：2026-03-02 08:00 ~ 2026-04-01 08:00。"""
    return stay(shift_id, moment(3, 2, 8, 0), moment(4, 1, 8, 0))


class TestShiftTableFindMatching(unittest.TestCase):
    def setUp(self):
        self.adapter = synth.make_adapter()
        self.table = ShiftTable(self.adapter)

    # ---- 既有口径（不传 incoming） -------------------------------------------------

    def test_exact_key_match_is_unchanged(self):
        self.table.upsert(synth.make_shift())
        found = self.table.find(synth.LINE, synth.SHIFT_DATE, "早")
        self.assertIsNotNone(found)
        self.assertEqual(found.shift_id, synth.SHIFT_ID)
        self.assertIsNone(self.table.find("SYNTH-LINE-Z", synth.SHIFT_DATE, "早"))
        self.assertIsNone(self.table.find(synth.LINE, synth.SHIFT_DATE, "晚"))

    def test_legacy_names_are_not_scanned_for_window_overlap(self):
        """既有行哪怕覆盖整个驻场期，也不参与窗口重叠匹配。"""
        self.table.upsert(
            synth.make_shift(
                shift_id="SYNTH-SHIFT-CUSTOM",
                shift_name="custom",
                shift_date=date(2026, 3, 1),
                start_time=moment(3, 1, 8, 0),
                end_time=moment(4, 30, 20, 0),
            )
        )
        incoming = base_stay()
        self.assertIsNone(
            self.table.find(synth.LINE, incoming.shift_date, "stay_period", incoming=incoming)
        )
        self.assertIsNone(
            self.table.find(synth.LINE, date(2026, 3, 20), "stay_period", incoming=incoming)
        )

    def test_stay_period_without_incoming_keeps_the_old_key_matching(self):
        self.table.upsert(base_stay())
        self.assertIsNotNone(self.table.find(synth.LINE, date(2026, 3, 2), "stay_period"))
        self.assertIsNone(
            self.table.find(synth.LINE, date(2026, 3, 20), "stay_period"),
            "不传 incoming 时不启用窗口重叠口径",
        )

    # ---- 驻场期键命中与窗口重叠 ---------------------------------------------------

    def test_stay_period_key_hit_returns_the_same_record(self):
        self.table.upsert(base_stay())
        incoming = base_stay()  # 同一单重复提交
        found = self.table.find(
            synth.LINE, incoming.shift_date, incoming.shift_name, incoming=incoming
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.shift_id, BASE_ID)

    def test_overlapping_window_is_matched(self):
        """重叠：03-02~04-01 与 03-20~04-10（不同 shift_date、不同 shift_id）。"""
        self.table.upsert(base_stay())
        incoming = stay("SYNTH-STAY-B", moment(3, 20, 8, 0), moment(4, 10, 8, 0))
        found = self.table.find(
            synth.LINE, incoming.shift_date, incoming.shift_name, incoming=incoming
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.shift_id, BASE_ID)

    def test_contained_window_is_matched(self):
        self.table.upsert(base_stay())
        incoming = stay("SYNTH-STAY-B", moment(3, 10, 8, 0), moment(3, 20, 8, 0))
        found = self.table.find(
            synth.LINE, incoming.shift_date, incoming.shift_name, incoming=incoming
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.shift_id, BASE_ID)

    def test_touching_window_is_not_matched(self):
        """相接放行：04-01 08:00 起点接在基准驻场期结束端点上。"""
        self.table.upsert(base_stay())
        incoming = stay("SYNTH-STAY-B", moment(4, 1, 8, 0), moment(5, 1, 8, 0))
        self.assertIsNone(
            self.table.find(synth.LINE, incoming.shift_date, incoming.shift_name, incoming=incoming)
        )

    def test_touching_window_before_is_not_matched(self):
        self.table.upsert(base_stay())
        incoming = stay("SYNTH-STAY-B", moment(1, 30, 8, 0), moment(3, 2, 8, 0))
        self.assertIsNone(
            self.table.find(synth.LINE, incoming.shift_date, incoming.shift_name, incoming=incoming)
        )

    def test_disjoint_window_is_not_matched(self):
        self.table.upsert(base_stay())
        incoming = stay("SYNTH-STAY-B", moment(5, 2, 8, 0), moment(5, 20, 8, 0))
        self.assertIsNone(
            self.table.find(synth.LINE, incoming.shift_date, incoming.shift_name, incoming=incoming)
        )

    def test_other_line_is_not_matched(self):
        self.table.upsert(base_stay())
        incoming = stay(
            "SYNTH-STAY-B", moment(3, 20, 8, 0), moment(4, 10, 8, 0), handover_line="SYNTH-LINE-B"
        )
        self.assertIsNone(
            self.table.find(
                "SYNTH-LINE-B", incoming.shift_date, incoming.shift_name, incoming=incoming
            )
        )

    # ---- 脏行处置 ---------------------------------------------------------------

    def test_dirty_stay_period_row_fails_closed(self):
        """驻场期行解析失败 → 抛出（不静默跳过，避免漏放重复建单）。"""
        fields = synth.shift_row(
            shift_id="SYNTH-STAY-BAD",
            shift_name="stay_period",
            shift_date="2026-03-25",
            start_time="SYNTH-非法时间",
        )
        self.adapter.create_record(SHIFT_TABLE, fields)
        incoming = stay("SYNTH-STAY-B", moment(3, 20, 8, 0), moment(4, 10, 8, 0))
        with self.assertRaises(ContractViolation):
            self.table.find(
                synth.LINE, incoming.shift_date, incoming.shift_name, incoming=incoming
            )


if __name__ == "__main__":
    unittest.main()
