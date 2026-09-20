"""班次表 / 交接事件表 / 辅助表读写测试（issue #9 第 2 段）。

覆盖：列存原始字符串、枚举校验在业务层、中文↔enum 映射复用契约映射表、
同一事件不双写、幂等键查重、辅助表留痕与待办映射的守卫。
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

from contracts.aitable_mapping import (  # noqa: E402
    EVENT_FIELD_MAPPINGS,
    SHIFT_FIELD_MAPPINGS,
    mapping_table,
)
from contracts.enums import (  # noqa: E402
    CATEGORY_ORDER,
    Completeness,
    ConfirmationStatus,
    EventCategory,
    EventStatus,
    Severity,
    ShiftStatus,
    WritebackStatus,
)
from contracts.errors import ContractViolation  # noqa: E402
from contracts.identity import IdentityRef  # noqa: E402

from integrations.aitable.cells import (  # noqa: E402
    bool_from_cell,
    bool_to_cell,
    event_from_fields,
    identity_from_cell,
    identity_to_cell,
    json_from_cell,
    shift_from_fields,
)
from integrations.aitable.tables import (  # noqa: E402
    ALARM_JOURNAL_TABLE,
    EVENT_TABLE,
    INTAKE_JOURNAL_TABLE,
    SHIFT_TABLE,
    TODO_TABLE,
    AlarmJournal,
    AlarmJournalEntry,
    EventTable,
    IntakeJournal,
    JournalEntry,
    ShiftTable,
    TodoTable,
    todo_columns_doc,
)

import aitable_synth as synth  # noqa: E402

SHIFT_COLUMNS = mapping_table(SHIFT_FIELD_MAPPINGS)
EVENT_COLUMNS = mapping_table(EVENT_FIELD_MAPPINGS)


class TestShiftTable(unittest.TestCase):
    def setUp(self):
        self.adapter = synth.make_adapter()
        self.table = ShiftTable(self.adapter)

    def test_round_trip_preserves_contract_values(self):
        shift = synth.make_shift()
        self.table.upsert(shift)
        loaded = self.table.get(synth.SHIFT_ID)
        self.assertEqual(loaded.shift_id, shift.shift_id)
        self.assertEqual(loaded.handover_line, shift.handover_line)
        self.assertEqual(loaded.shift_date, shift.shift_date)
        self.assertEqual(loaded.shift_name, shift.shift_name)
        self.assertEqual(loaded.start_time, shift.start_time)
        self.assertEqual(loaded.end_time, shift.end_time)
        self.assertTrue(loaded.handover_from.same_person(shift.handover_from))
        self.assertTrue(loaded.handover_to.same_person(shift.handover_to))
        self.assertEqual(loaded.status, shift.status)
        self.assertEqual(
            loaded.config_snapshot.critical_standard, shift.config_snapshot.critical_standard
        )
        self.assertEqual(loaded.idempotency_key, shift.idempotency_key)

    def test_upsert_updates_instead_of_adding_a_row(self):
        self.table.upsert(synth.make_shift())
        self.table.upsert(synth.make_shift())
        self.assertEqual(self.table.count(), 1)

    def test_enums_are_stored_as_canonical_strings(self):
        shift = synth.make_shift(shift_name="早")
        self.table.upsert(shift)
        stored = self.adapter.list_records(SHIFT_TABLE)[0].fields
        self.assertEqual(stored[SHIFT_COLUMNS["shift_name"]], "early")
        self.assertEqual(stored[SHIFT_COLUMNS["status"]], "draft")
        self.assertNotIn("ShiftName", stored[SHIFT_COLUMNS["shift_name"]])

    def test_find_uses_idempotency_key(self):
        self.table.upsert(synth.make_shift())
        found = self.table.find(synth.LINE, synth.SHIFT_DATE, "早")
        self.assertIsNotNone(found)
        self.assertEqual(found.shift_id, synth.SHIFT_ID)
        self.assertIsNone(self.table.find("SYNTH-LINE-Z", synth.SHIFT_DATE, "早"))
        self.assertIsNone(self.table.find(synth.LINE, synth.SHIFT_DATE, "晚"))

    def test_set_status_writes_enum_column(self):
        self.table.upsert(synth.make_shift())
        self.table.set_status(synth.SHIFT_ID, ShiftStatus.SUBMITTED.value)
        self.assertEqual(self.table.get(synth.SHIFT_ID).status, "submitted")

    def test_set_status_on_missing_shift_is_rejected(self):
        with self.assertRaises(ContractViolation):
            self.table.set_status("SYNTH-SHIFT-9999", "submitted")

    def test_unknown_enum_value_in_cell_is_rejected_in_business_layer(self):
        fields = synth.shift_row()
        fields[SHIFT_COLUMNS["shift_name"]] = "SYNTH-不存在的班次"
        with self.assertRaises(ContractViolation):
            shift_from_fields(fields)

    def test_blank_required_column_is_rejected(self):
        fields = synth.shift_row()
        fields[SHIFT_COLUMNS["shift_id"]] = "   "
        with self.assertRaises(ContractViolation):
            shift_from_fields(fields)

    def test_missing_column_is_rejected(self):
        fields = synth.shift_row()
        fields.pop(SHIFT_COLUMNS["shift_date"])
        with self.assertRaises(ContractViolation):
            shift_from_fields(fields)

    def test_unparsable_time_is_rejected(self):
        fields = synth.shift_row()
        fields[SHIFT_COLUMNS["start_time"]] = "SYNTH-不是时间"
        with self.assertRaises(ContractViolation):
            shift_from_fields(fields)


class TestEventTable(unittest.TestCase):
    def setUp(self):
        self.adapter = synth.make_adapter()
        self.table = EventTable(self.adapter)

    def test_append_and_read_back(self):
        event = synth.make_event()
        record_id = self.table.append(event)
        self.assertTrue(record_id.startswith("SYNTH-REC-"))
        loaded = self.table.get(event.event_id)
        self.assertEqual(loaded.event_id, event.event_id)
        self.assertEqual(loaded.shift_id, event.shift_id)
        self.assertEqual(loaded.category, event.category)
        self.assertEqual(loaded.occurred_at, event.occurred_at)
        self.assertEqual(loaded.severity, event.severity)
        self.assertEqual(loaded.status, event.status)
        self.assertEqual(loaded.completeness, Completeness.COMPLETE.value)
        self.assertTrue(loaded.owner.same_person(synth.TO))

    def test_all_nine_categories_round_trip(self):
        for index, category in enumerate(CATEGORY_ORDER, start=1):
            event = synth.make_event(f"SYNTH-EVT-{index:04d}", category=category)
            self.table.append(event)
            loaded = self.table.get(event.event_id)
            self.assertEqual(loaded.category, category.value)
        self.assertEqual(self.table.count(), len(CATEGORY_ORDER))

    def test_chinese_category_is_normalised_to_enum_value(self):
        event = synth.make_event(category="重大事项")
        self.table.append(event)
        stored = self.adapter.list_records(EVENT_TABLE)[0].fields
        self.assertEqual(stored[EVENT_COLUMNS["category"]], "major")

    def test_duplicate_append_is_refused(self):
        self.table.append(synth.make_event())
        with self.assertRaises(ContractViolation):
            self.table.append(synth.make_event())
        self.assertEqual(self.table.count(), 1)

    def test_update_requires_existing_row(self):
        with self.assertRaises(ContractViolation):
            self.table.update(synth.make_event("SYNTH-EVT-9999"))

    def test_filter_by_shift(self):
        self.table.append(synth.make_event("SYNTH-EVT-0001"))
        other = synth.make_event("SYNTH-EVT-0002", shift_id=synth.OTHER_SHIFT_ID)
        self.table.append(other)
        self.assertEqual(self.table.count(), 2)
        self.assertEqual(self.table.count(synth.SHIFT_ID), 1)
        self.assertEqual(self.table.event_ids(synth.SHIFT_ID), ("SYNTH-EVT-0001",))
        self.assertEqual(len(self.table.list_for_shift(synth.OTHER_SHIFT_ID)), 1)

    def test_unknown_enum_value_is_rejected_in_business_layer(self):
        record_id = self.adapter.create_record(EVENT_TABLE, synth.event_row())
        self.adapter.update_record(EVENT_TABLE, record_id, {EVENT_COLUMNS["status"]: "SYNTH-乱写"})
        with self.assertRaises(ContractViolation):
            event_from_fields(self.adapter.get_record(EVENT_TABLE, record_id).fields)

    def test_forced_severity_columns_are_stored(self):
        event = synth.make_event(
            "SYNTH-EVT-0003", category=EventCategory.MAJOR.value, severity=Severity.NORMAL.value
        )
        self.table.append(event)
        stored = self.adapter.list_records(EVENT_TABLE)[0].fields
        self.assertEqual(stored[EVENT_COLUMNS["severity"]], "critical")
        self.assertEqual(stored[EVENT_COLUMNS["severity_forced"]], "true")
        self.assertEqual(stored[EVENT_COLUMNS["original_severity"]], "normal")
        self.assertEqual(bool_from_cell(stored[EVENT_COLUMNS["severity_forced"]]), True)
        self.assertEqual(bool_to_cell(False), "false")


class TestCellCodec(unittest.TestCase):
    def test_identity_cell_round_trip(self):
        self.assertEqual(identity_to_cell(synth.TO), "eam:SYNTH-uid-to")
        self.assertEqual(identity_to_cell(None), "")
        decoded = identity_from_cell("eam:SYNTH-uid-to")
        self.assertTrue(decoded.same_person(synth.TO))
        self.assertIsNone(identity_from_cell("  "))

    def test_identity_cell_keeps_user_id_with_colon(self):
        decoded = identity_from_cell("dingtalk:SYNTH-uid:with:colons")
        self.assertEqual(decoded.user_id, "SYNTH-uid:with:colons")

    def test_identity_cell_without_separator_is_rejected(self):
        with self.assertRaises(ContractViolation):
            identity_from_cell("SYNTH-no-separator")

    def test_unknown_identity_source_is_rejected(self):
        with self.assertRaises(ContractViolation):
            identity_from_cell("SYNTH-source:SYNTH-uid")

    def test_json_cell_round_trip_and_errors(self):
        self.assertEqual(json_from_cell(""), {})
        self.assertEqual(json_from_cell('{"a":1}'), {"a": 1})
        with self.assertRaises(ContractViolation):
            json_from_cell("{not json}")
        with self.assertRaises(ContractViolation):
            json_from_cell("[1,2,3]")


class TestAuxiliaryTables(unittest.TestCase):
    def setUp(self):
        self.adapter = synth.make_adapter()
        self.journal = IntakeJournal(self.adapter)
        self.alarms = AlarmJournal(self.adapter)
        self.todos = TodoTable(self.adapter)

    def test_intake_journal_round_trip(self):
        entry = JournalEntry(
            event_id="SYNTH-EVT-0001",
            shift_id=synth.SHIFT_ID,
            outcome="stored",
            raw={"event_id": "SYNTH-EVT-0001", "occurred_at": "2026-01-02 09:30"},
            record_id="SYNTH-REC-000001",
            logged_at="2026-01-02 20:00",
        )
        self.journal.record(entry)
        loaded = self.journal.entries(synth.SHIFT_ID)[0]
        self.assertEqual(loaded.event_id, entry.event_id)
        self.assertEqual(loaded.outcome, "stored")
        self.assertEqual(loaded.raw["occurred_at"], "2026-01-02 09:30")
        self.assertEqual(loaded.record_id, "SYNTH-REC-000001")
        self.assertEqual(self.adapter.row_count(INTAKE_JOURNAL_TABLE), 1)

    def test_intake_journal_rejected_filter(self):
        self.journal.record(
            JournalEntry("SYNTH-EVT-0001", synth.SHIFT_ID, "rejected", {"event_id": "SYNTH-EVT-0001"})
        )
        self.journal.record(
            JournalEntry("SYNTH-EVT-0002", synth.SHIFT_ID, "stored", {"event_id": "SYNTH-EVT-0002"})
        )
        self.assertEqual(len(self.journal.rejected_entries(synth.SHIFT_ID)), 1)
        self.assertEqual(len(self.journal.entries()), 2)

    def test_unknown_outcome_is_rejected(self):
        fields = JournalEntry(
            "SYNTH-EVT-0001", synth.SHIFT_ID, "stored", {"event_id": "SYNTH-EVT-0001"}
        ).to_fields()
        fields["处置"] = "SYNTH-未知处置"
        with self.assertRaises(ContractViolation):
            JournalEntry.from_fields(fields)

    def test_alarm_journal_round_trip_and_action_guard(self):
        entry = AlarmJournalEntry(
            alarm_id="E002:SYNTH-SHIFT-0001:SYNTH-EVT-0002:occurred_at",
            shift_id=synth.SHIFT_ID,
            rule="E002",
            field="occurred_at",
            event_id="SYNTH-EVT-0002",
            detail="只有日期没有时:分，需补全",
            created_at="2026-01-02 20:00",
            action_at="2026-01-02 20:05",
            reason="补全发生时间",
        )
        self.alarms.record(entry)
        loaded = self.alarms.entries(synth.SHIFT_ID)[0]
        self.assertEqual(loaded.alarm_id, entry.alarm_id)
        self.assertEqual(loaded.action, "cleared")
        self.assertEqual(loaded.created_at, "2026-01-02 20:00")
        self.assertEqual(self.alarms.cleared_ids(synth.SHIFT_ID), (entry.alarm_id,))
        self.assertEqual(self.adapter.row_count(ALARM_JOURNAL_TABLE), 1)

        fields = entry.to_fields()
        fields["处置"] = "SYNTH-未知动作"
        with self.assertRaises(ContractViolation):
            AlarmJournalEntry.from_fields(fields)

    def test_todo_table_create_and_guard(self):
        todo_id = self.todos.create(
            unit_id="SYNTH-EVT-0001",
            scope="event",
            shift_id=synth.SHIFT_ID,
            assignee=synth.TO,
            label="关键事项：SYNTH 事件",
        )
        self.assertEqual(todo_id, self.todos.get_fields("SYNTH-EVT-0001")["待办ID"])
        self.assertTrue(self.todos.has_todo("SYNTH-EVT-0001"))
        self.assertTrue(self.todos.assignee_of("SYNTH-EVT-0001").same_person(synth.TO))
        self.assertEqual(self.todos.get_fields("SYNTH-EVT-0001")["确认状态"], "pending")
        self.assertEqual(self.adapter.row_count(TODO_TABLE), 1)

    def test_todo_table_refuses_second_todo_for_same_unit(self):
        self.todos.create(
            unit_id="SYNTH-EVT-0001", scope="event", shift_id=synth.SHIFT_ID, assignee=synth.TO
        )
        with self.assertRaises(ContractViolation):
            self.todos.create(
                unit_id="SYNTH-EVT-0001", scope="event", shift_id=synth.SHIFT_ID, assignee=synth.TO
            )
        self.assertEqual(self.adapter.row_count(TODO_TABLE), 1)

    def test_todo_table_scope_is_validated(self):
        with self.assertRaises(ContractViolation):
            self.todos.create(
                unit_id="SYNTH-EVT-0001",
                scope="SYNTH-乱写",
                shift_id=synth.SHIFT_ID,
                assignee=synth.TO,
            )

    def test_todo_table_update_progress_and_missing_row(self):
        self.todos.create(
            unit_id="SYNTH-EVT-0001", scope="event", shift_id=synth.SHIFT_ID, assignee=synth.TO
        )
        self.todos.update_progress(
            "SYNTH-EVT-0001",
            {
                "确认状态": ConfirmationStatus.CONFIRMED.value,
                "回写状态": WritebackStatus.VERIFIED.value,
            },
        )
        fields = self.todos.get_fields("SYNTH-EVT-0001")
        self.assertEqual(fields["确认状态"], "confirmed")
        self.assertEqual(fields["回写状态"], "verified")
        with self.assertRaises(ContractViolation):
            self.todos.update_progress("SYNTH-EVT-9999", {"确认状态": "confirmed"})

    def test_auxiliary_table_doc_mentions_no_table_ids(self):
        doc = todo_columns_doc()
        self.assertEqual(doc["todo_table"], TODO_TABLE)
        text = str(doc)
        self.assertNotIn("app_", text)
        self.assertNotIn("base_", text)


class TestRawStringStorageContract(unittest.TestCase):
    """表格列存原始字符串：写入的每一格都必须是 str，枚举落规范值。"""

    def test_every_stored_cell_is_a_plain_string(self):
        adapter = synth.make_adapter()
        ShiftTable(adapter).upsert(synth.make_shift())
        EventTable(adapter).append(synth.make_event(category="重大事项", status="移交接班人"))
        for table in (SHIFT_TABLE, EVENT_TABLE):
            for row in adapter.list_records(table):
                for column, value in row.fields.items():
                    self.assertIsInstance(value, str)
        event_cells = adapter.list_records(EVENT_TABLE)[0].fields
        self.assertEqual(event_cells[EVENT_COLUMNS["category"]], "major")
        self.assertEqual(event_cells[EVENT_COLUMNS["status"]], "transferred")
        self.assertEqual(event_cells[EVENT_COLUMNS["severity"]], "critical")
        self.assertEqual(event_cells[EVENT_COLUMNS["owner"]], "eam:SYNTH-uid-to")


if __name__ == "__main__":
    unittest.main()
