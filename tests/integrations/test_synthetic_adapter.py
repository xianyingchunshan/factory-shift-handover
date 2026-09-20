"""合成适配器测试（issue #9 第 2 段）：增改查、故障注入、状态搬运、离线自证。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from integrations.aitable.adapter import (  # noqa: E402
    AitableRejected,
    AitableWriteUnknown,
)
from integrations.aitable.synthetic import (  # noqa: E402
    CREATE,
    FAIL_LOSE,
    FAIL_REJECTED,
    FAIL_UNKNOWN,
    GET,
    LIST,
    UPDATE,
    SyntheticAitableAdapter,
    assert_synthetic,
)
from integrations.aitable.tables import EVENT_TABLE, SHIFT_TABLE  # noqa: E402

import aitable_synth as synth  # noqa: E402


class TestSyntheticCrud(unittest.TestCase):
    def setUp(self):
        self.adapter = synth.make_adapter()

    def test_offline_and_synthetic_identity(self):
        self.assertEqual(self.adapter.name, "synthetic")
        self.assertTrue(self.adapter.offline)
        assert_synthetic(self.adapter)  # 不抛异常即通过

    def test_create_get_update_list_round_trip(self):
        fields = synth.event_row("SYNTH-EVT-0001")
        record_id = self.adapter.create_record(EVENT_TABLE, fields)
        self.assertTrue(record_id.startswith("SYNTH-REC-"))
        row = self.adapter.get_record(EVENT_TABLE, record_id)
        self.assertIsNotNone(row)
        self.assertEqual(row.record_id, record_id)
        self.assertEqual(row.fields, fields)

        self.adapter.update_record(EVENT_TABLE, record_id, {"备注": "SYNTH 更新"})
        again = self.adapter.get_record(EVENT_TABLE, record_id)
        self.assertEqual(again.get("备注"), "SYNTH 更新")

        rows = self.adapter.list_records(EVENT_TABLE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.adapter.row_count(EVENT_TABLE), 1)

    def test_columns_are_always_raw_strings(self):
        fields = synth.event_row("SYNTH-EVT-0002")
        record_id = self.adapter.create_record(EVENT_TABLE, fields)
        row = self.adapter.get_record(EVENT_TABLE, record_id)
        for column, value in row.fields.items():
            self.assertIsInstance(column, str)
            self.assertIsInstance(value, str, f"列 {column} 必须是原始字符串")

    def test_missing_record_returns_none_and_update_is_rejected(self):
        self.assertIsNone(self.adapter.get_record(EVENT_TABLE, "SYNTH-REC-999999"))
        with self.assertRaises(AitableRejected):
            self.adapter.update_record(EVENT_TABLE, "SYNTH-REC-999999", {"备注": "x"})

    def test_call_log_records_ops_without_secrets(self):
        record_id = self.adapter.create_record(EVENT_TABLE, synth.event_row())
        self.adapter.get_record(EVENT_TABLE, record_id)
        ops = [call.op for call in self.adapter.calls]
        self.assertEqual(ops, [CREATE, GET])
        self.assertEqual(self.adapter.calls_for(CREATE)[0].table, EVENT_TABLE)
        self.assertNotIn("token", str(self.adapter.calls[0].to_dict()).lower())


class TestFaultInjection(unittest.TestCase):
    """受理不明 / 明确失败 / 写入丢失三条反向路径。"""

    def setUp(self):
        self.adapter = synth.make_adapter()

    def test_unknown_failure_raises_write_unknown(self):
        self.adapter.queue_failure(CREATE, FAIL_UNKNOWN)
        with self.assertRaises(AitableWriteUnknown):
            self.adapter.create_record(SHIFT_TABLE, synth.shift_row())
        self.assertEqual(self.adapter.row_count(SHIFT_TABLE), 0)

    def test_rejected_failure_raises_rejected(self):
        self.adapter.queue_failure(UPDATE, FAIL_REJECTED)
        with self.assertRaises(AitableRejected):
            self.adapter.update_record(SHIFT_TABLE, "SYNTH-REC-000001", {"状态": "draft"})

    def test_lost_write_returns_id_but_stores_nothing(self):
        self.adapter.queue_failure(CREATE, FAIL_LOSE)
        record_id = self.adapter.create_record(EVENT_TABLE, synth.event_row())
        self.assertTrue(record_id)
        self.assertIsNone(self.adapter.get_record(EVENT_TABLE, record_id))
        self.assertEqual(self.adapter.row_count(EVENT_TABLE), 0)

    def test_get_and_list_failures(self):
        self.adapter.queue_failure(GET, FAIL_UNKNOWN)
        with self.assertRaises(AitableWriteUnknown):
            self.adapter.get_record(EVENT_TABLE, "SYNTH-REC-000001")
        self.adapter.queue_failure(LIST, FAIL_REJECTED)
        with self.assertRaises(AitableRejected):
            self.adapter.list_records(EVENT_TABLE)

    def test_failure_queue_is_fifo_and_consumed(self):
        self.adapter.queue_failure(CREATE, FAIL_UNKNOWN)
        self.adapter.queue_failure(CREATE, FAIL_REJECTED)
        with self.assertRaises(AitableWriteUnknown):
            self.adapter.create_record(EVENT_TABLE, synth.event_row())
        with self.assertRaises(AitableRejected):
            self.adapter.create_record(EVENT_TABLE, synth.event_row())
        record_id = self.adapter.create_record(EVENT_TABLE, synth.event_row())
        self.assertTrue(record_id)

    def test_unknown_op_or_kind_is_rejected_locally(self):
        with self.assertRaises(ValueError):
            self.adapter.queue_failure("delete")
        with self.assertRaises(ValueError):
            self.adapter.queue_failure(CREATE, "whatever")


class TestStateTransfer(unittest.TestCase):
    """模拟重启：状态搬运必须完整且互相独立。"""

    def test_export_import_round_trip(self):
        adapter = synth.make_adapter()
        adapter.create_record(SHIFT_TABLE, synth.shift_row())
        adapter.create_record(EVENT_TABLE, synth.event_row("SYNTH-EVT-0001"))
        payload = adapter.export_state()

        fresh = synth.make_adapter()
        fresh.import_state(payload)
        self.assertEqual(fresh.row_count(SHIFT_TABLE), 1)
        self.assertEqual(fresh.row_count(EVENT_TABLE), 1)
        self.assertEqual(fresh.export_state()["tables"], payload["tables"])

    def test_clone_is_independent(self):
        adapter = synth.make_adapter()
        adapter.create_record(EVENT_TABLE, synth.event_row("SYNTH-EVT-0001"))
        clone = adapter.clone()
        clone.create_record(EVENT_TABLE, synth.event_row("SYNTH-EVT-0002"))
        self.assertEqual(adapter.row_count(EVENT_TABLE), 1)
        self.assertEqual(clone.row_count(EVENT_TABLE), 2)

    def test_import_replaces_existing_content(self):
        adapter = synth.make_adapter()
        adapter.create_record(EVENT_TABLE, synth.event_row("SYNTH-EVT-0001"))
        adapter.import_state({"seq": 0, "tables": {}})
        self.assertEqual(adapter.row_count(EVENT_TABLE), 0)
        self.assertEqual(adapter.tables(), ())


class FakeOnlineAdapter:
    """假的"真实"适配器：只用于验证 offline 自检会拦下它（不会被真正使用）。"""

    name = "real"
    offline = False


class TestOfflineGuard(unittest.TestCase):
    def test_assert_synthetic_rejects_non_synthetic_adapter(self):
        with self.assertRaises(AssertionError):
            assert_synthetic(FakeOnlineAdapter())


if __name__ == "__main__":
    unittest.main()
