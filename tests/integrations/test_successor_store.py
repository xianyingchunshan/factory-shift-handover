"""接班人变更留痕表 + 待办作废列的读写测试（issue #12 的存储层，验收 L2）。

覆盖：

- ``接班人变更留痕表（工作流辅助）``：新增 / 读回 / 更新（路由跟随证据回填）/ 防双写；
- 待办表新增的 ``待办状态 / 作废时间 / 作废原因`` 列：默认 active、作废写入可回读、
  重复作废与空原因被拒、``active_rows`` 只列未作废行；
- 离线审计把**契约层新增模块**也纳入（``contracts/`` + ``workflow/`` + ``integrations/``
  零网络/进程类 import）。

合成数据：人员 / 班次 / 答复一律 ``SYNTH-`` 前缀，无真实姓名、群、表 ID 与业务数据。
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

from contracts.errors import ContractViolation  # noqa: E402
from contracts.identity import IdentityRef  # noqa: E402
from contracts.successor import SuccessorChange  # noqa: E402
from contracts.timebase import SHANGHAI  # noqa: E402

from integrations.aitable.isolation import find_forbidden_imports  # noqa: E402
from integrations.aitable.tables import (  # noqa: E402
    SUCCESSOR_CHANGE_COLUMNS,
    SUCCESSOR_CHANGE_TABLE,
    TODO_COLUMNS,
    TODO_STATE_ACTIVE,
    TODO_STATE_VOIDED,
    TODO_TABLE,
    SuccessorChangeTable,
    TodoTable,
    todo_columns_doc,
)

import aitable_synth as synth  # noqa: E402

NEW_TO = IdentityRef(source="dingtalk", user_id="SYNTH-uid-newto", display_name="SYNTH-接班人D")
ACTOR = IdentityRef(source="dingtalk", user_id="SYNTH-uid-master", display_name="SYNTH-主控")
STAMP = datetime(2026, 1, 2, 21, 0, tzinfo=SHANGHAI)


def make_change(**overrides) -> SuccessorChange:
    params = dict(
        shift_id=synth.SHIFT_ID,
        from_identity=synth.TO,
        to_identity=NEW_TO,
        changed_by=ACTOR,
        changed_at=STAMP,
        ordinal=1,
        request_text="接班人是D",
        resolver="synthetic",
        reason="SYNTH 换人演练",
    )
    params.update(overrides)
    return SuccessorChange(**params)


class TestSuccessorChangeTable(unittest.TestCase):
    def setUp(self):
        self.adapter = synth.make_adapter()
        self.table = SuccessorChangeTable(self.adapter)

    def test_record_and_read_back(self):
        change = make_change().with_routing(
            voided_todo_ids=("SYNTH-EVT-0003",), issued_todo_ids=("SYNTH-EVT-0003#R1",)
        )
        self.assertTrue(self.table.record(change).startswith("SYNTH-REC-"))
        loaded = self.table.get(change.change_id)
        self.assertEqual(loaded, change)
        self.assertEqual(self.table.entries(synth.SHIFT_ID), (change,))
        self.assertEqual(self.table.count(), 1)

    def test_identity_columns_are_plain_strings_for_humans(self):
        self.table.record(make_change())
        row = self.adapter.list_records(SUCCESSOR_CHANGE_TABLE)[0].fields
        self.assertEqual(row[SUCCESSOR_CHANGE_COLUMNS["from_identity"]], "eam:SYNTH-uid-to")
        self.assertEqual(row[SUCCESSOR_CHANGE_COLUMNS["to_identity"]], "dingtalk:SYNTH-uid-newto")
        self.assertEqual(row[SUCCESSOR_CHANGE_COLUMNS["changed_at"]], "2026-01-02 21:00")
        for value in row.values():
            self.assertIsInstance(value, str)

    def test_duplicate_change_id_is_refused(self):
        self.table.record(make_change())
        with self.assertRaises(ContractViolation):
            self.table.record(make_change(request_text="改过一句答复"))
        self.assertEqual(self.table.count(), 1)

    def test_amend_backfills_routing_evidence(self):
        change = make_change()
        self.table.record(change)
        routed = change.with_routing(
            voided_todo_ids=("SYNTH-SHIFT-0001",), issued_todo_ids=("SYNTH-SHIFT-0001#R1",)
        )
        self.table.amend(routed)
        loaded = self.table.get(change.change_id)
        self.assertTrue(loaded.is_routed)
        self.assertEqual(loaded.voided_todo_ids, ("SYNTH-SHIFT-0001",))
        self.assertEqual(self.table.count(), 1, "更新不新增行")

    def test_amend_requires_an_existing_row(self):
        with self.assertRaises(ContractViolation):
            self.table.amend(make_change(ordinal=7))

    def test_entries_are_filtered_by_shift_and_sorted(self):
        self.table.record(make_change(ordinal=2, changed_at=datetime(2026, 1, 2, 22, 0, tzinfo=SHANGHAI)))
        self.table.record(make_change(ordinal=1))
        self.table.record(make_change(shift_id=synth.OTHER_SHIFT_ID, ordinal=1))
        self.assertEqual([entry.ordinal for entry in self.table.entries(synth.SHIFT_ID)], [1, 2])
        self.assertEqual(len(self.table.entries()), 3)
        self.assertEqual(len(self.table.entries(synth.OTHER_SHIFT_ID)), 1)

    def test_broken_payload_is_rejected_on_read(self):
        record_id = self.adapter.create_record(
            SUCCESSOR_CHANGE_TABLE, {SUCCESSOR_CHANGE_COLUMNS["payload"]: "{not json}"}
        )
        self.assertTrue(record_id)
        with self.assertRaises(ContractViolation):
            self.table.entries()


class TestTodoVoidColumns(unittest.TestCase):
    def setUp(self):
        self.adapter = synth.make_adapter()
        self.todos = TodoTable(self.adapter)

    def _create(self, unit_id: str = "SYNTH-EVT-0001") -> str:
        return self.todos.create(
            unit_id=unit_id, scope="event", shift_id=synth.SHIFT_ID, assignee=synth.TO
        )

    def test_new_rows_default_to_active(self):
        self._create()
        fields = self.todos.get_fields("SYNTH-EVT-0001")
        self.assertEqual(fields[TODO_COLUMNS["todo_state"]], TODO_STATE_ACTIVE)
        self.assertEqual(fields[TODO_COLUMNS["voided_at"]], "")
        self.assertEqual(fields[TODO_COLUMNS["void_reason"]], "")
        self.assertEqual(self.todos.state_of("SYNTH-EVT-0001"), TODO_STATE_ACTIVE)

    def test_void_writes_explicit_evidence(self):
        self._create()
        fields = self.todos.void("SYNTH-EVT-0001", reason="接班人变更 SYNTH-SHIFT-0001#1", at="2026-01-02 21:00")
        self.assertEqual(fields[TODO_COLUMNS["todo_state"]], TODO_STATE_VOIDED)
        self.assertEqual(fields[TODO_COLUMNS["voided_at"]], "2026-01-02 21:00")
        self.assertIn("接班人变更", fields[TODO_COLUMNS["void_reason"]])
        self.assertEqual(self.todos.state_of("SYNTH-EVT-0001"), TODO_STATE_VOIDED)

    def test_void_requires_a_reason_and_a_row(self):
        self._create()
        with self.assertRaises(ContractViolation):
            self.todos.void("SYNTH-EVT-0001", reason="   ")
        with self.assertRaises(ContractViolation):
            self.todos.void("SYNTH-EVT-9999", reason="不存在的单元")

    def test_void_is_not_repeatable_and_row_is_kept(self):
        self._create()
        self.todos.void("SYNTH-EVT-0001", reason="换人")
        with self.assertRaises(ContractViolation):
            self.todos.void("SYNTH-EVT-0001", reason="换人")
        self.assertEqual(self.adapter.row_count(TODO_TABLE), 1, "作废不删行：留痕必须可回读")

    def test_active_rows_exclude_voided(self):
        self._create("SYNTH-EVT-0001")
        self._create("SYNTH-EVT-0002")
        self.todos.void("SYNTH-EVT-0001", reason="换人")
        active = [row.get(TODO_COLUMNS["unit_id"]) for row in self.todos.active_rows(synth.SHIFT_ID)]
        self.assertEqual(active, ["SYNTH-EVT-0002"])
        self.assertEqual(self.todos.count(synth.SHIFT_ID), 2)

    def test_legacy_rows_without_the_state_column_count_as_active(self):
        fields = self.todos.create(
            unit_id="SYNTH-EVT-0003", scope="event", shift_id=synth.SHIFT_ID, assignee=synth.TO
        )
        row = self.todos.find_row("SYNTH-EVT-0003")
        self.adapter.update_record(TODO_TABLE, row.record_id, {TODO_COLUMNS["todo_state"]: ""})
        self.assertEqual(self.todos.state_of("SYNTH-EVT-0003"), TODO_STATE_ACTIVE)
        self.assertEqual(len(self.todos.active_rows(synth.SHIFT_ID)), 1)
        self.assertTrue(fields)

    def test_columns_doc_mentions_the_new_columns_without_ids(self):
        doc = todo_columns_doc()
        self.assertEqual(doc["successor_change_table"], SUCCESSOR_CHANGE_TABLE)
        self.assertIn(TODO_STATE_VOIDED, doc["todo_states"])
        self.assertIn("待办状态", doc["todo_columns"].values())
        self.assertNotIn("app_", str(doc))
        self.assertNotIn("base_", str(doc))


class TestOfflineAuditCoversNewModules(unittest.TestCase):
    def test_no_network_or_process_imports_in_contract_or_workflow_layers(self):
        findings = find_forbidden_imports(_ROOT, ("contracts", "workflow", "integrations"))
        self.assertEqual(findings, [], "新增模块不得引入网络/进程类模块")

    def test_successor_modules_are_synthetic_only(self):
        from contracts import successor as contract_module
        from workflow import successor as workflow_module

        names = {name for name in dir(contract_module) if not name.startswith("_")}
        self.assertIn("resolve_unique_identity", names)
        self.assertEqual(workflow_module.SYNTHETIC_RESOLVER, "synthetic")
        self.assertTrue(workflow_module.SyntheticSuccessorResolver.offline)


if __name__ == "__main__":
    unittest.main()
