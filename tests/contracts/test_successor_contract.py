"""T03 契约增补测试：接班人唯一身份解析、态约束、变更留痕（issue #12 验收 L1）。

覆盖：

- 解析不到身份 / 答复为空 / 解析出多人 → ``AUTH_REQUIRED``（拒绝，班次原样不动）；
- ``archived`` 终态 → 拒绝；``blocked``（告警未清）→ ``ALARM_NOT_CLEARED``；
- 变更留痕（谁 / 何时 / 从谁改到谁 / 依据答复）可回读、可序列化往返；
- 换人**不静默改历史**：旧配置快照进 ``snapshot_history``，``config_revision`` 递增；
- 建班次前置询问落地（``apply_prequery``）只在 ``draft`` 阶段生效。

合成数据：人员 / 班次 / 答复一律 ``SYNTH-`` 前缀，非真实人员与业务数据。
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

from contracts.enums import ShiftStatus  # noqa: E402
from contracts.errors import (  # noqa: E402
    ALARM_NOT_CLEARED,
    AUTH_REQUIRED,
    ContractViolation,
)
from contracts.identity import IdentityRef  # noqa: E402
from contracts.shift import new_shift  # noqa: E402
from contracts.successor import (  # noqa: E402
    SUCCESSOR_QUESTION,
    SuccessorChange,
    SuccessorChangeLog,
    apply_prequery,
    change_successor,
    identity_key,
    precheck_successor_change,
    resolve_unique_identity,
)
from contracts.timebase import SHANGHAI  # noqa: E402

import synth  # noqa: E402

#: 合成解析器只认识这些答复；答不出就是"钉钉里找不到"。
NEW_TO = IdentityRef(source="dingtalk", user_id="SYNTH-uid-newto", display_name="SYNTH-接班人D")
ACTOR = IdentityRef(source="dingtalk", user_id="SYNTH-uid-master", display_name="SYNTH-主控")
DIRECTORY: dict[str, tuple[IdentityRef, ...]] = {
    "接班人是D": (NEW_TO,),
    "同一个人写两遍": (NEW_TO, IdentityRef(source="dingtalk", user_id="SYNTH-uid-newto")),
    "两个同名人": (NEW_TO, synth.STRANGER),
    "查无此人": (),
}


class SyntheticResolver:
    """合成注入式解析器：只查本地表，不联网、不猜人。"""

    name = "synthetic"
    offline = True

    def __init__(self, directory=None) -> None:
        self.directory = dict(directory if directory is not None else DIRECTORY)

    def resolve(self, query: str):
        return self.directory.get(query, ())


def change_time(hour: int = 21, minute: int = 0) -> datetime:
    return datetime(2026, 1, 2, hour, minute, tzinfo=SHANGHAI)


def submitted_shift(**overrides):
    """已提交态班次（换人可发生在归档前的任一非终态）。"""
    shift = synth.make_shift(**overrides)
    shift.transition(ShiftStatus.SUBMITTED)
    return shift


class TestUniqueIdentityResolution(unittest.TestCase):
    def setUp(self):
        self.resolver = SyntheticResolver()

    def test_exact_answer_resolves_to_one_identity(self):
        person = resolve_unique_identity(self.resolver, "接班人是D")
        self.assertTrue(person.same_person(NEW_TO))

    def test_blank_answer_is_refused(self):
        for answer in (None, "", "   "):
            with synth.expect_code(AUTH_REQUIRED):
                resolve_unique_identity(self.resolver, answer)

    def test_unknown_answer_is_refused(self):
        with synth.expect_code(AUTH_REQUIRED):
            resolve_unique_identity(self.resolver, "张三")

    def test_ambiguous_answer_is_refused(self):
        with synth.expect_code(AUTH_REQUIRED):
            resolve_unique_identity(self.resolver, "两个同名人")

    def test_same_person_written_twice_is_still_unique(self):
        person = resolve_unique_identity(self.resolver, "同一个人写两遍")
        self.assertTrue(person.same_person(NEW_TO))
        self.assertEqual(identity_key(person), identity_key(NEW_TO))

    def test_resolver_returning_raw_text_is_refused(self):
        class TextResolver:
            name = "text"

            def resolve(self, query: str):
                return "SYNTH-uid-newto"

        with self.assertRaises(ContractViolation):
            resolve_unique_identity(TextResolver(), "接班人是D")

    def test_missing_resolver_interface_is_refused(self):
        with self.assertRaises(ContractViolation):
            resolve_unique_identity(object(), "接班人是D")

    def test_question_text_is_fixed(self):
        self.assertEqual(SUCCESSOR_QUESTION, "本班接班人是谁？")


class TestChangeRefusals(unittest.TestCase):
    def setUp(self):
        self.resolver = SyntheticResolver()

    def _shift(self):
        return submitted_shift()

    def test_archived_shift_refuses_change(self):
        shift = self._shift()
        shift.transition(ShiftStatus.CONFIRMED)
        shift.transition(ShiftStatus.ARCHIVED)
        with self.assertRaises(ContractViolation) as ctx:
            change_successor(shift, NEW_TO, actor=ACTOR, at=change_time())
        self.assertIn("已归档", str(ctx.exception))
        self.assertTrue(shift.handover_to.same_person(synth.TO))
        self.assertEqual(shift.config_revision, 0)

    def test_blocked_shift_refuses_change(self):
        shift = self._shift()
        shift.suspend_for_alarms(True)
        self.assertEqual(shift.status, ShiftStatus.BLOCKED.value)
        with synth.expect_code(ALARM_NOT_CLEARED):
            change_successor(shift, NEW_TO, actor=ACTOR, at=change_time())
        self.assertTrue(shift.handover_to.same_person(synth.TO))

    def test_open_alarms_flag_refuses_change_even_if_status_is_not_blocked(self):
        shift = self._shift()
        with synth.expect_code(ALARM_NOT_CLEARED):
            change_successor(
                shift, NEW_TO, actor=ACTOR, at=change_time(), has_open_alarms=True
            )
        self.assertTrue(shift.handover_to.same_person(synth.TO))

    def test_missing_actor_is_refused(self):
        shift = self._shift()
        for actor in (None, "   "):
            with synth.expect_code(AUTH_REQUIRED):
                change_successor(shift, NEW_TO, actor=actor, at=change_time())
        self.assertTrue(shift.handover_to.same_person(synth.TO))

    def test_missing_new_successor_is_refused(self):
        shift = self._shift()
        with synth.expect_code(AUTH_REQUIRED):
            change_successor(shift, None, actor=ACTOR, at=change_time())

    def test_same_person_is_not_a_change(self):
        shift = self._shift()
        with self.assertRaises(ContractViolation):
            change_successor(shift, synth.TO, actor=ACTOR, at=change_time())

    def test_naive_change_time_is_refused(self):
        shift = self._shift()
        with self.assertRaises(ContractViolation):
            change_successor(
                shift, NEW_TO, actor=ACTOR, at=datetime(2026, 1, 2, 21, 0)
            )

    def test_precheck_passes_for_open_states_only(self):
        steps = {
            ShiftStatus.DRAFT: (),
            ShiftStatus.SUBMITTED: (ShiftStatus.SUBMITTED,),
            ShiftStatus.CONFIRMED: (ShiftStatus.SUBMITTED, ShiftStatus.CONFIRMED),
        }
        for status, path in steps.items():
            shift = synth.make_shift()
            for step in path:
                shift.transition(step)
            self.assertEqual(shift.status, status.value)
            self.assertIsNone(precheck_successor_change(shift))
        archived = synth.make_shift()
        archived.transition(ShiftStatus.SUBMITTED)
        archived.transition(ShiftStatus.CONFIRMED)
        archived.transition(ShiftStatus.ARCHIVED)
        with self.assertRaises(ContractViolation):
            precheck_successor_change(archived)


class TestChangeTrail(unittest.TestCase):
    """变更留痕：谁 / 何时 / 从谁改到谁，可回读、可序列化。"""

    def setUp(self):
        self.shift = submitted_shift()
        self.log = SuccessorChangeLog(self.shift.shift_id)

    def test_change_writes_a_readable_trail(self):
        change = change_successor(
            self.shift,
            NEW_TO,
            actor=ACTOR,
            at=change_time(21, 5),
            log=self.log,
            request_text="接班人是D",
            resolver="synthetic",
            reason="SYNTH 换人演练",
        )
        self.assertEqual(change.change_id, f"{synth.SHIFT_ID}#1")
        self.assertEqual(change.ordinal, 1)
        self.assertTrue(change.from_identity.same_person(synth.TO))
        self.assertTrue(change.to_identity.same_person(NEW_TO))
        self.assertTrue(change.changed_by.same_person(ACTOR))
        self.assertEqual(change.changed_at, change_time(21, 5))
        self.assertEqual(change.request_text, "接班人是D")
        self.assertEqual(self.log.count, 1)
        self.assertEqual(self.log.entries()[0], change)
        self.assertEqual(self.log.latest(), change)
        self.assertEqual(self.log.get(change.change_id), change)

    def test_change_updates_shift_without_silently_rewriting_history(self):
        before = self.shift.config_snapshot
        change_successor(
            self.shift, NEW_TO, actor=ACTOR, at=change_time(), log=self.log, reason="换人"
        )
        self.assertTrue(self.shift.handover_to.same_person(NEW_TO))
        self.assertEqual(self.shift.config_revision, 1)
        self.assertEqual(len(self.shift.snapshot_history), 1)
        self.assertTrue(self.shift.snapshot_history[0].handover_to.same_person(synth.TO))
        self.assertTrue(self.shift.config_snapshot.handover_to.same_person(NEW_TO))
        self.assertEqual(
            self.shift.config_snapshot.critical_standard if before is None else before.critical_standard,
            self.shift.config_snapshot.critical_standard,
        )
        self.assertEqual(self.shift.config_snapshot.locked_at, change_time())

    def test_second_change_keeps_the_first_trail(self):
        first = change_successor(
            self.shift, NEW_TO, actor=ACTOR, at=change_time(), log=self.log, reason="第一次"
        )
        back = IdentityRef(source="eam", user_id="SYNTH-uid-to", display_name="SYNTH-接班人B")
        second = change_successor(
            self.shift, back, actor=ACTOR, at=change_time(22, 0), log=self.log, reason="第二次"
        )
        self.assertEqual([change.change_id for change in self.log.entries()], [
            f"{synth.SHIFT_ID}#1",
            f"{synth.SHIFT_ID}#2",
        ])
        self.assertTrue(second.from_identity.same_person(first.to_identity))
        self.assertEqual(
            [snapshot.handover_to.user_id for snapshot in self.shift.snapshot_history],
            [synth.TO.user_id, NEW_TO.user_id],
            "每次换人的旧快照都进历史，不静默覆盖",
        )
        self.assertEqual(self.shift.config_revision, 2)

    def test_routing_evidence_can_be_backfilled(self):
        change = change_successor(
            self.shift, NEW_TO, actor=ACTOR, at=change_time(), log=self.log
        )
        self.assertFalse(change.is_routed)
        routed = change.with_routing(
            voided_todo_ids=("SYNTH-EVT-0003",), issued_todo_ids=("SYNTH-EVT-0003#R1",)
        )
        self.assertTrue(routed.is_routed)
        self.assertEqual(self.log.replace(routed), routed)
        self.assertTrue(self.log.latest().is_routed)
        self.assertEqual(self.log.count, 1)

    def test_replace_unknown_change_is_refused(self):
        with self.assertRaises(ContractViolation):
            self.log.replace(
                SuccessorChange(
                    shift_id=synth.SHIFT_ID,
                    from_identity=synth.TO,
                    to_identity=NEW_TO,
                    changed_by=ACTOR,
                    changed_at=change_time(),
                    ordinal=9,
                )
            )


class TestTrailSerialization(unittest.TestCase):
    def setUp(self):
        self.shift = submitted_shift()
        self.log = SuccessorChangeLog(self.shift.shift_id)
        self.change = change_successor(
            self.shift,
            NEW_TO,
            actor=ACTOR,
            at=change_time(21, 5),
            log=self.log,
            request_text="接班人是D",
            resolver="synthetic",
            reason="SYNTH 换人演练",
        ).with_routing(voided_todo_ids=("SYNTH-EVT-0003",), issued_todo_ids=("SYNTH-EVT-0003#R1",))

    def test_change_round_trip(self):
        payload = self.change.to_dict()
        self.assertEqual(payload["change_id"], f"{synth.SHIFT_ID}#1")
        self.assertEqual(payload["changed_at"], "2026-01-02 21:05")
        self.assertEqual(payload["voided_todo_ids"], ["SYNTH-EVT-0003"])
        self.assertEqual(SuccessorChange.from_dict(payload), self.change)

    def test_change_from_dict_rejects_incomplete_payloads(self):
        payload = self.change.to_dict()
        for key in ("shift_id", "from_identity", "to_identity", "changed_by", "changed_at"):
            broken = dict(payload)
            broken.pop(key)
            with self.assertRaises(ContractViolation):
                SuccessorChange.from_dict(broken)
        with self.assertRaises(ContractViolation):
            SuccessorChange.from_dict({"shift_id": synth.SHIFT_ID, "changed_at": "SYNTH-乱写"})

    def test_log_round_trip_and_restore(self):
        payload = self.log.to_dict()
        self.assertEqual(payload["shift_id"], synth.SHIFT_ID)
        restored = SuccessorChangeLog.from_dict(payload)
        self.assertEqual(restored.entries(), self.log.entries())
        self.assertEqual(restored.to_dict(), payload)

    def test_restore_rejects_foreign_or_gapped_trails(self):
        with self.assertRaises(ContractViolation):
            SuccessorChangeLog(synth.SHIFT_ID).restore(
                [
                    SuccessorChange(
                        shift_id="SYNTH-SHIFT-0002",
                        from_identity=synth.TO,
                        to_identity=NEW_TO,
                        changed_by=ACTOR,
                        changed_at=change_time(),
                        ordinal=1,
                    )
                ]
            )
        with self.assertRaises(ContractViolation):
            SuccessorChangeLog(synth.SHIFT_ID).restore(
                [SuccessorChange.from_dict(dict(self.change.to_dict(), ordinal=2))]
            )

    def test_foreign_change_cannot_be_recorded(self):
        with self.assertRaises(ContractViolation):
            self.log.record(
                SuccessorChange(
                    shift_id="SYNTH-SHIFT-0002",
                    from_identity=synth.TO,
                    to_identity=NEW_TO,
                    changed_by=ACTOR,
                    changed_at=change_time(),
                )
            )


class TestPrequeryApplication(unittest.TestCase):
    """建班次前置询问落地：解析后写 handover_to；只在建班次（draft）阶段生效。"""

    def test_prequery_writes_successor_and_locks_snapshot(self):
        shift = synth.make_shift()
        origin = shift.config_snapshot
        snapshot = apply_prequery(shift, NEW_TO, at=change_time(7, 30))
        self.assertTrue(shift.handover_to.same_person(NEW_TO))
        self.assertTrue(shift.config_snapshot.handover_to.same_person(NEW_TO))
        self.assertEqual(snapshot.locked_at, change_time(7, 30))
        self.assertEqual(snapshot.critical_standard, origin.critical_standard)
        self.assertEqual(shift.config_revision, 0, "建班次阶段锁定不算配置变更")
        self.assertEqual(shift.status, ShiftStatus.DRAFT.value)

    def test_prequery_after_submit_is_refused(self):
        shift = submitted_shift()
        with self.assertRaises(ContractViolation):
            apply_prequery(shift, NEW_TO, at=change_time())


if __name__ == "__main__":
    unittest.main()
