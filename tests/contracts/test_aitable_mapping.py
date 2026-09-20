"""契约测试：AI表格字段映射（issue #6 §6）。

要求：契约字段无一遗漏落列（或显式登记为内部字段）；列名唯一；
表 ID / 真实标识不进仓库。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.aitable_mapping import (  # noqa: E402
    ENUM_COLUMNS,
    EVENT_FIELD_MAPPINGS,
    EVENT_INTERNAL_FIELDS,
    EVENT_TABLE,
    RAW_STRING_NOTE,
    SHIFT_FIELD_MAPPINGS,
    SHIFT_INTERNAL_FIELDS,
    SHIFT_TABLE,
    TABLE_ID_NOTE,
    column_table,
    enum_field_names,
    mapping_doc,
    mapping_table,
    unmapped_fields,
)
from contracts.events import HandoverEvent  # noqa: E402
from contracts.shift import ShiftRecord  # noqa: E402

import synth  # noqa: E402

#: 疑似真实表格/群标识（表 ID 形如固定前缀 + 长串），仓库内不得出现。
_SUSPECT_ID = re.compile(r"[A-Za-z]{3,}[A-Za-z0-9]{10,}")


class TestMappingCompleteness(unittest.TestCase):
    def test_shift_mapping_covers_every_contract_field(self):
        unmapped = unmapped_fields(ShiftRecord, SHIFT_FIELD_MAPPINGS)
        self.assertEqual(unmapped, set(SHIFT_INTERNAL_FIELDS))

    def test_event_mapping_covers_every_contract_field(self):
        unmapped = unmapped_fields(HandoverEvent, EVENT_FIELD_MAPPINGS)
        self.assertEqual(unmapped, set(EVENT_INTERNAL_FIELDS))
        self.assertEqual(EVENT_INTERNAL_FIELDS, ())

    def test_core_event_fields_are_mapped_to_columns(self):
        table = mapping_table(EVENT_FIELD_MAPPINGS)
        self.assertEqual(table["event_id"], "事件ID")
        self.assertEqual(table["occurred_at"], "发生时间")
        self.assertEqual(table["completeness"], "完整性")
        self.assertEqual(table["shift_id"], "班次ID")

    def test_shift_fields_are_mapped_to_columns(self):
        table = mapping_table(SHIFT_FIELD_MAPPINGS)
        self.assertEqual(table["shift_id"], "班次ID")
        self.assertEqual(table["handover_line"], "交接线")
        self.assertEqual(table["status"], "状态")

    def test_columns_and_fields_are_unique(self):
        for mappings in (SHIFT_FIELD_MAPPINGS, EVENT_FIELD_MAPPINGS):
            columns = [mapping.column for mapping in mappings]
            names = [mapping.field for mapping in mappings]
            self.assertEqual(len(columns), len(set(columns)))
            self.assertEqual(len(names), len(set(names)))

    def test_lookup_tables_round_trip(self):
        forward = mapping_table(EVENT_FIELD_MAPPINGS)
        backward = column_table(EVENT_FIELD_MAPPINGS)
        for field_name, column in forward.items():
            self.assertEqual(backward[column], field_name)

    def test_shift_id_reference_column_is_present_in_both_tables(self):
        self.assertEqual(mapping_table(SHIFT_FIELD_MAPPINGS)["shift_id"], "班次ID")
        self.assertEqual(mapping_table(EVENT_FIELD_MAPPINGS)["shift_id"], "班次ID")


class TestMappingNotes(unittest.TestCase):
    def test_table_names_are_from_the_spec(self):
        self.assertEqual(SHIFT_TABLE, "班次表")
        self.assertEqual(EVENT_TABLE, "交接事件表")

    def test_raw_string_and_table_id_notes_present(self):
        doc = mapping_doc()
        self.assertEqual(doc["table_id_note"], TABLE_ID_NOTE)
        self.assertEqual(doc["raw_string_note"], RAW_STRING_NOTE)
        self.assertIn("原始字符串", doc["raw_string_note"])
        self.assertIn("运行时配置", doc["table_id_note"])

    def test_no_real_table_id_leaks_into_the_mapping(self):
        blob = repr(mapping_doc())
        for token in ("tbl", "base", "sheetId", "groupId", "chatId"):
            self.assertNotIn(token, blob)

    def test_enum_columns_are_declared_for_business_layer_validation(self):
        self.assertEqual(enum_field_names(), ENUM_COLUMNS)
        self.assertIn("category", enum_field_names())
        self.assertIn("severity", enum_field_names())

    def test_mapped_columns_carry_type_and_requirement_metadata(self):
        for mapping in SHIFT_FIELD_MAPPINGS + EVENT_FIELD_MAPPINGS:
            self.assertTrue(mapping.column.strip())
            self.assertTrue(mapping.kind.strip())
            self.assertIsInstance(mapping.required, bool)


class TestSyntheticIdentifiers(unittest.TestCase):
    def test_synthetic_identifiers_are_prefixed(self):
        for person in (synth.FROM, synth.TO, synth.STRANGER):
            self.assertTrue(person.user_id.startswith("SYNTH-"))
            self.assertTrue(person.display_name.startswith("SYNTH-"))
        self.assertTrue(synth.SHIFT_ID.startswith("SYNTH-"))
        self.assertTrue(synth.LINE.startswith("SYNTH-"))

    def test_no_unprefixed_long_identifier_in_synthetic_data(self):
        blob = f"{synth.SHIFT_ID} {synth.LINE} {synth.FROM.user_id} {synth.TO.user_id}"
        blob = blob.replace("SYNTH-", "")
        self.assertIsNone(_SUSPECT_ID.search(blob))


if __name__ == "__main__":
    unittest.main()
