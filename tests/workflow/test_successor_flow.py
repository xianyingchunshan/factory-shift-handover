"""T03 实现测试：前置询问入口 + 换人变更 + 路由跟随 + 重启恢复（issue #12 验收 L1/L2）。

覆盖：

- 建班次前置询问："本班接班人是谁？" → 解析后写入 ``handover_to``（含班次表列）；
  解析不到 / 答复为空 → ``AUTH_REQUIRED`` 且**一行都不写**（缺输入不可代填）；
- 换人变更：归档 / 告警未清拒绝；变更留痕（谁 / 何时 / 从谁改到谁）落表可回读；
- 路由跟随：旧待办**显式作废**（待办状态列 + 原因 + 时间，行不删除）、同单元对新人
  **重发**（新单元号 ``#R1``），新接班人可确认 → 回写，旧人不再持有活动待办；
  提醒分发（只单发交班人 + 接班人）随新接班人跟随；
- 重启恢复：变更留痕、作废态、重发单元都能从辅助表读回，重启后不丢也不复活；
- 离线边界：非合成适配器 / 非合成本地解析器一律被拒（真实钉钉解析与待办重发属 L4）。

合成数据：人员 / 班次 / 答复一律 ``SYNTH-`` 前缀，无真实姓名、群、表 ID 与业务数据。
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

from contracts.confirmation import ConfirmationUnit  # noqa: E402
from contracts.aitable_mapping import SHIFT_TABLE  # noqa: E402
from contracts.enums import ConfirmationStatus, ShiftStatus, WritebackStatus  # noqa: E402
from contracts.errors import (  # noqa: E402
    ALARM_NOT_CLEARED,
    AUTH_REQUIRED,
    ContractViolation,
)
from contracts.identity import IdentityRef  # noqa: E402
from contracts.shift import new_shift  # noqa: E402
from contracts.successor import SUCCESSOR_QUESTION  # noqa: E402

from integrations.aitable.cells import SHIFT_COLUMNS  # noqa: E402
from integrations.aitable.tables import (  # noqa: E402
    SUCCESSOR_CHANGE_COLUMNS,
    SUCCESSOR_CHANGE_TABLE,
    TODO_COLUMNS,
    TODO_STATE_ACTIVE,
    TODO_STATE_VOIDED,
    TODO_TABLE,
)

from workflow import state as wfstate  # noqa: E402
from workflow.successor import (  # noqa: E402
    SuccessorPrequery,
    SuccessorService,
    SyntheticSuccessorResolver,
    assert_synthetic_resolver,
    reissued_unit_id,
)

import wf_synth as synth  # noqa: E402

NEW_TO = IdentityRef(source="dingtalk", user_id="SYNTH-uid-newto", display_name="SYNTH-接班人D")
ACTOR = IdentityRef(source="dingtalk", user_id="SYNTH-uid-master", display_name="SYNTH-主控")

#: 合成解析器只认识这些答复（查不到就是"钉钉里找不到"）。
DIRECTORY: dict[str, tuple[IdentityRef, ...]] = {
    "接班人是D": (NEW_TO,),
    "两个同名人": (NEW_TO, synth.STRANGER),
    "查无此人": (),
}

CHANGE_TIME = synth.at(21, 0)


def resolver(directory=None) -> SyntheticSuccessorResolver:
    return SyntheticSuccessorResolver(DIRECTORY if directory is None else directory)


def shift_params(**overrides):
    """建班次参数（不含 handover_to：那一项由前置询问解析决定）。"""
    params = dict(
        shift_id=synth.SHIFT_ID,
        handover_line=synth.LINE,
        shift_date=synth.SHIFT_DATE,
        shift_name="early",
        start_time=synth.START,
        end_time=synth.END,
        handover_from=synth.FROM,
        critical_standard=("重大事项", "移交接班人"),
    )
    params.update(overrides)
    return params


def submitted_flow():
    """走完整流程：三条合规事项（含关键级移交）→ 提交 → 出清单 → 建待办并登记。"""
    flow = synth.clean_flow()
    flow.intake.submit_shift()
    report = synth.report_of(flow)
    flow.todos.create_plan(report)
    return flow, report


def service_of(flow, *, directory=None, clock=synth.fixed_clock(CHANGE_TIME)) -> SuccessorService:
    return SuccessorService(
        flow.shift,
        flow.adapter,
        resolver=resolver(directory),
        todos=flow.todos,
        clock=clock,
    )


class TestPrequeryEntry(unittest.TestCase):
    """建班次前置询问：解析后写入 handover_to；解析失败即拒绝（不落表）。"""

    def setUp(self):
        self.adapter = synth.make_adapter()
        self.prequery = SuccessorPrequery(resolver(), clock=synth.fixed_clock(synth.at(7, 30)))

    def test_open_shift_resolves_and_writes_handover_to(self):
        result = self.prequery.open_shift("接班人是D", adapter=self.adapter, **shift_params())
        self.assertEqual(result.ask.question, SUCCESSOR_QUESTION)
        self.assertEqual(result.ask.answer, "接班人是D")
        self.assertTrue(result.ask.identity.same_person(NEW_TO))
        self.assertTrue(result.shift.handover_to.same_person(NEW_TO))
        self.assertTrue(result.shift.config_snapshot.handover_to.same_person(NEW_TO))
        self.assertTrue(result.register.created)
        row = self.adapter.list_records(SHIFT_TABLE)[0].fields
        self.assertEqual(row[SHIFT_COLUMNS["handover_to"]], "dingtalk:SYNTH-uid-newto")
        self.assertEqual(result.to_dict()["handover_to"]["user_id"], NEW_TO.user_id)

    def test_unknown_answer_is_refused_and_writes_nothing(self):
        with synth.expect_code(AUTH_REQUIRED):
            self.prequery.open_shift("张三是谁", adapter=self.adapter, **shift_params())
        self.assertEqual(self.adapter.row_count(SHIFT_TABLE), 0, "解析不到身份：班次不落表")

    def test_blank_answer_is_refused(self):
        for answer in (None, "", "   "):
            with synth.expect_code(AUTH_REQUIRED):
                self.prequery.open_shift(answer, adapter=self.adapter, **shift_params())
        self.assertEqual(self.adapter.row_count(SHIFT_TABLE), 0)

    def test_ambiguous_answer_is_refused(self):
        with synth.expect_code(AUTH_REQUIRED):
            self.prequery.ask("两个同名人")

    def test_build_shift_keeps_other_snapshot_fields(self):
        ask, shift = self.prequery.build_shift("接班人是D", **shift_params())
        self.assertTrue(ask.identity.same_person(NEW_TO))
        self.assertEqual(shift.config_snapshot.critical_standard, ("重大事项", "移交接班人"))
        self.assertEqual(shift.config_snapshot.handover_line, synth.LINE)
        self.assertEqual(shift.config_revision, 0, "建班次锁定不算配置变更")

    def test_build_shift_accepts_a_plain_shift_record_too(self):
        shift = new_shift(handover_to=NEW_TO, **shift_params())
        self.assertTrue(shift.handover_to.same_person(NEW_TO))


class TestChangeGuards(unittest.TestCase):
    """换人前置拒绝：归档 / 告警未清 / 解析不到 —— 拒绝即不改班次、不落留痕。"""

    def _change_count(self, adapter) -> int:
        return adapter.row_count(SUCCESSOR_CHANGE_TABLE)

    def test_archived_shift_refuses_change(self):
        flow, report = submitted_flow()
        flow.shift.transition(ShiftStatus.CONFIRMED)
        flow.shift.transition(ShiftStatus.ARCHIVED)
        service = service_of(flow)
        with self.assertRaises(ContractViolation):
            service.change(answer="接班人是D", actor=ACTOR, at=CHANGE_TIME)
        self.assertTrue(flow.shift.handover_to.same_person(synth.TO))
        self.assertEqual(self._change_count(flow.adapter), 0, "拒绝的变更不留痕")
        self.assertEqual(
            sorted(service.active_unit_ids()), sorted(unit.unit_id for unit in report.confirmation_area)
        )
        self.assertEqual(flow.todos.board_fields("SYNTH-EVT-0003")[TODO_COLUMNS["todo_state"]],
                         TODO_STATE_ACTIVE)

    def test_blocked_shift_refuses_change(self):
        flow = synth.alarm_flow()
        flow.shift.suspend_for_alarms(flow.intake.ledger.has_open)
        self.assertEqual(flow.shift.status, ShiftStatus.BLOCKED.value)
        service = service_of(flow)
        with synth.expect_code(ALARM_NOT_CLEARED):
            service.change(answer="接班人是D", actor=ACTOR, at=CHANGE_TIME)
        self.assertTrue(flow.shift.handover_to.same_person(synth.TO))
        self.assertEqual(self._change_count(flow.adapter), 0)

    def test_unresolvable_answer_refuses_change(self):
        flow, _ = submitted_flow()
        service = service_of(flow)
        with synth.expect_code(AUTH_REQUIRED):
            service.change(answer="查无此人", actor=ACTOR, at=CHANGE_TIME)
        self.assertTrue(flow.shift.handover_to.same_person(synth.TO))
        self.assertEqual(self._change_count(flow.adapter), 0)
        self.assertEqual(service.changes_of_shift(), ())
        self.assertEqual(flow.todos.board_fields("SYNTH-EVT-0003")[TODO_COLUMNS["todo_state"]],
                         TODO_STATE_ACTIVE)

    def test_missing_actor_is_refused(self):
        flow, _ = submitted_flow()
        service = service_of(flow)
        with synth.expect_code(AUTH_REQUIRED):
            service.change(answer="接班人是D", actor=None, at=CHANGE_TIME)
        self.assertEqual(self._change_count(flow.adapter), 0)

    def test_change_requires_an_answer_or_a_resolved_identity(self):
        flow, _ = submitted_flow()
        service = service_of(flow)
        with self.assertRaises(ContractViolation):
            service.change(actor=ACTOR, at=CHANGE_TIME)

    def test_answer_and_identity_must_agree(self):
        flow, _ = submitted_flow()
        service = service_of(flow)
        stranger = IdentityRef(source="ehr", user_id="SYNTH-uid-stranger")
        with self.assertRaises(ContractViolation):
            service.change(
                answer="接班人是D", new_successor=stranger, actor=ACTOR, at=CHANGE_TIME
            )

    def test_same_person_is_not_a_change(self):
        flow, _ = submitted_flow()
        service = service_of(flow)
        with self.assertRaises(ContractViolation):
            service.change(new_successor=synth.TO, actor=ACTOR, at=CHANGE_TIME)


class TestChangeTrailAndRouting(unittest.TestCase):
    """换人留痕 + 路由跟随：旧待办显式作废、新待办重发、提醒跟随新接班人。"""

    def setUp(self):
        self.flow, self.report = submitted_flow()
        self.service = service_of(self.flow)
        self.before_units = tuple(unit.unit_id for unit in self.report.confirmation_area)
        self.outcome = self.service.change(
            answer="接班人是D", actor=ACTOR, at=CHANGE_TIME, reason="SYNTH 换人演练"
        )

    # ---- 留痕 -----------------------------------------------------------

    def test_change_trail_is_readable_from_the_trail_table(self):
        stored = self.service.changes_of_shift()
        self.assertEqual(len(stored), 1)
        change = stored[0]
        self.assertEqual(change.change_id, f"{synth.SHIFT_ID}#1")
        self.assertTrue(change.from_identity.same_person(synth.TO))
        self.assertTrue(change.to_identity.same_person(NEW_TO))
        self.assertTrue(change.changed_by.same_person(ACTOR))
        self.assertEqual(change.changed_at, CHANGE_TIME)
        self.assertEqual(change.request_text, "接班人是D")
        self.assertEqual(change.resolver, "synthetic")
        self.assertEqual(change.reason, "SYNTH 换人演练")
        self.assertTrue(change.is_routed)
        self.assertEqual(self.service.latest_change(), change)
        self.assertEqual(self.service.log.entries(), stored)

    def test_trail_row_keeps_identity_columns_for_humans(self):
        row = self.flow.adapter.list_records(SUCCESSOR_CHANGE_TABLE)[0].fields
        self.assertEqual(row[SUCCESSOR_CHANGE_COLUMNS["from_identity"]], "eam:SYNTH-uid-to")
        self.assertEqual(row[SUCCESSOR_CHANGE_COLUMNS["to_identity"]], "dingtalk:SYNTH-uid-newto")
        self.assertEqual(row[SUCCESSOR_CHANGE_COLUMNS["changed_by"]], "dingtalk:SYNTH-uid-master")
        self.assertEqual(row[SUCCESSOR_CHANGE_COLUMNS["changed_at"]], "2026-01-02 21:00")

    def test_shift_row_and_snapshot_follow_the_new_successor(self):
        self.assertTrue(self.flow.shift.handover_to.same_person(NEW_TO))
        self.assertEqual(
            [snapshot.handover_to.user_id for snapshot in self.flow.shift.snapshot_history],
            [synth.TO.user_id],
        )
        row = self.flow.intake.shift_table.get(synth.SHIFT_ID)
        self.assertTrue(row.handover_to.same_person(NEW_TO))
        self.assertTrue(row.config_snapshot.handover_to.same_person(NEW_TO))
        stored = self.flow.intake.shift_table.record_id_of(synth.SHIFT_ID)
        self.assertEqual(
            self.flow.adapter.get_record(SHIFT_TABLE, stored).fields[SHIFT_COLUMNS["handover_to"]],
            "dingtalk:SYNTH-uid-newto",
        )

    def test_routing_evidence_is_backfilled_into_the_trail(self):
        change = self.service.latest_change()
        self.assertEqual(set(change.voided_todo_ids), set(self.outcome.voided_unit_ids))
        self.assertEqual(set(change.issued_todo_ids), set(self.outcome.issued_unit_ids))
        self.assertEqual(self.service.check_consistency(), ())

    # ---- 路由跟随 -------------------------------------------------------

    def test_old_todos_are_voided_explicitly(self):
        self.assertEqual(set(self.outcome.voided_unit_ids), set(self.before_units))
        for unit_id in self.outcome.voided_unit_ids:
            fields = self.flow.todos.board_fields(unit_id)
            self.assertEqual(fields[TODO_COLUMNS["todo_state"]], TODO_STATE_VOIDED)
            self.assertEqual(fields[TODO_COLUMNS["voided_at"]], "2026-01-02 21:00")
            self.assertIn("接班人变更", fields[TODO_COLUMNS["void_reason"]])
            self.assertTrue(self.flow.todos.is_voided(unit_id))
        self.assertEqual(
            sorted(record.unit_id for record in self.flow.todos.voided()),
            sorted(self.outcome.voided_unit_ids),
        )

    def test_voided_units_are_not_silently_revived(self):
        for unit_id in self.outcome.voided_unit_ids:
            with self.assertRaises(ContractViolation):
                self.flow.todos.get(unit_id)
            with self.assertRaises(ContractViolation):
                self.flow.todos.void_unit(unit_id, reason="重复作废")

    def test_new_todos_are_reissued_to_the_new_successor(self):
        expected = {reissued_unit_id(unit_id, ordinal=1) for unit_id in self.before_units}
        self.assertEqual(set(self.outcome.issued_unit_ids), expected)
        for unit_id in self.outcome.issued_unit_ids:
            fields = self.flow.todos.board_fields(unit_id)
            self.assertEqual(fields[TODO_COLUMNS["assignee"]], "dingtalk:SYNTH-uid-newto")
            self.assertEqual(fields[TODO_COLUMNS["todo_state"]], TODO_STATE_ACTIVE)
            self.assertTrue(fields[TODO_COLUMNS["todo_id"]])
        active = [self.flow.todos.get(unit_id) for unit_id in self.service.active_unit_ids()]
        self.assertTrue(all(record.assignee.same_person(NEW_TO) for record in active))

    def test_new_successor_can_confirm_and_write_back(self):
        unit_id = self.outcome.issued_unit_ids[0]
        with synth.expect_code(AUTH_REQUIRED):
            self.flow.todos.confirm(unit_id, synth.TO)
        self.flow.todos.confirm(unit_id, NEW_TO)
        outcome = self.flow.todos.run_writeback(unit_id)
        self.assertEqual(outcome.status, ConfirmationStatus.WRITTEN_BACK.value)
        self.assertEqual(outcome.writeback_status, WritebackStatus.VERIFIED.value)

    def test_completed_units_are_kept_not_voided(self):
        flow, _ = submitted_flow()
        flow.todos.confirm("SYNTH-EVT-0003", synth.TO)
        flow.todos.run_writeback("SYNTH-EVT-0003")
        service = service_of(flow)
        outcome = service.change(answer="接班人是D", actor=ACTOR, at=CHANGE_TIME)
        self.assertEqual(outcome.kept_unit_ids, ("SYNTH-EVT-0003",))
        self.assertEqual(outcome.voided_unit_ids, (synth.SHIFT_ID,))
        fields = flow.todos.board_fields("SYNTH-EVT-0003")
        self.assertEqual(fields[TODO_COLUMNS["todo_state"]], TODO_STATE_ACTIVE)
        self.assertEqual(fields[TODO_COLUMNS["status"]], ConfirmationStatus.WRITTEN_BACK.value)
        with self.assertRaises(ContractViolation):
            flow.todos.void_unit("SYNTH-EVT-0003", reason="历史留痕不得作废")

    # ---- 提醒路由 -------------------------------------------------------

    def test_report_dispatch_follows_the_new_successor(self):
        report = self.flow.checklist.generate()
        self.assertTrue(report.dispatch.recipients[1].same_person(NEW_TO))
        self.assertTrue(report.dispatch.recipients[0].same_person(synth.FROM))
        self.assertIn(NEW_TO.user_id, report.recipient_ids())
        self.assertNotIn(synth.TO.user_id, report.recipient_ids())
        report.dispatch.validate_targets([NEW_TO])
        with self.assertRaises(ContractViolation):
            report.dispatch.validate_targets([synth.TO])

    def test_rebind_report_swaps_voided_units_for_reissued_ones(self):
        rebound = self.service.rebind_report(
            self.flow.checklist.generate(todo_id_factory=lambda unit: ""), self.outcome
        )
        ids = [unit.unit_id for unit in rebound.confirmation_area]
        self.assertEqual(ids, [
            reissued_unit_id(unit_id, ordinal=1) for unit_id in self.before_units
        ])
        for unit in rebound.confirmation_area:
            self.assertTrue(unit.assignee.same_person(NEW_TO))
            self.assertTrue(unit.has_todo)

    def test_dispatch_recipients_helper_follows_change(self):
        self.assertEqual(self.service.dispatch_recipients(), (synth.FROM, NEW_TO))
        summary = self.flow.todos.summary()
        self.assertEqual(summary["voided"], len(self.before_units))
        self.assertEqual(summary["todos"], len(self.before_units) * 2)
        self.assertEqual(summary["active_todos"], len(self.before_units))


class TestSecondChangeOnReissuedUnits(unittest.TestCase):
    def test_second_change_chains_reissue_unit_ids(self):
        flow, _ = submitted_flow()
        service = service_of(flow)
        first = service.change(answer="接班人是D", actor=ACTOR, at=CHANGE_TIME)
        back = IdentityRef(source="eam", user_id="SYNTH-uid-to", display_name="SYNTH-接班人B")
        second = service.change(
            new_successor=back, actor=ACTOR, at=synth.at(22, 0), reason="SYNTH 再换回"
        )
        logical = tuple(first.voided_unit_ids)
        self.assertEqual(second.previous_successor.user_id, NEW_TO.user_id)
        self.assertEqual(second.new_successor.user_id, synth.TO.user_id)
        self.assertEqual(
            set(second.voided_unit_ids), {reissued_unit_id(unit_id, ordinal=1) for unit_id in logical}
        )
        self.assertEqual(
            set(second.issued_unit_ids), {reissued_unit_id(unit_id, ordinal=2) for unit_id in logical}
        )
        stored = service.changes_of_shift()
        self.assertEqual([entry.change_id for entry in stored], [
            f"{synth.SHIFT_ID}#1",
            f"{synth.SHIFT_ID}#2",
        ])
        self.assertEqual(service.check_consistency(), ())
        self.assertTrue(
            all(
                flow.todos.get(unit_id).assignee.same_person(back)
                for unit_id in service.active_unit_ids()
            )
        )


class TestRestartKeepsChangeAndRouting(unittest.TestCase):
    """重启恢复：变更留痕、作废态、重发待办都不丢（不重发、不复活）。"""

    def setUp(self):
        self.flow, self.report = submitted_flow()
        self.service = service_of(self.flow)
        self.outcome = self.service.change(answer="接班人是D", actor=ACTOR, at=CHANGE_TIME)
        self.before_changes = self.service.changes_of_shift()
        self.before_todo_rows = self.flow.adapter.row_count(TODO_TABLE)

    def _restart(self, **_kwargs):
        payload = wfstate.dump_tables(self.flow.adapter)
        fresh = synth.make_adapter()
        return wfstate.restart(
            payload,
            adapter=fresh,
            shift_id=synth.SHIFT_ID,
            clock=synth.fixed_clock(CHANGE_TIME),
            resolver=resolver(),
        )

    def test_restart_restores_the_change_trail(self):
        bundle = self._restart()
        self.assertEqual(bundle.issues, ())
        self.assertIsNotNone(bundle.successor)
        restored = bundle.successor.changes_of_shift()
        self.assertEqual(restored, self.before_changes)
        self.assertEqual(bundle.successor.log.entries(), self.before_changes)
        self.assertTrue(bundle.shift.handover_to.same_person(NEW_TO))
        self.assertEqual(bundle.to_dict()["successor_changes"], 1)

    def test_restart_keeps_voided_units_voided(self):
        bundle = self._restart()
        self.assertEqual(
            sorted(record.unit_id for record in bundle.todos.voided()),
            sorted(self.outcome.voided_unit_ids),
        )
        for unit_id in self.outcome.voided_unit_ids:
            self.assertTrue(bundle.todos.is_voided(unit_id))
            with self.assertRaises(ContractViolation):
                bundle.todos.get(unit_id)
        self.assertEqual(
            bundle.todos.summary()["voided"], len(self.outcome.voided_unit_ids)
        )
        self.assertEqual(
            sorted(bundle.successor.active_unit_ids()), sorted(self.outcome.issued_unit_ids)
        )
        self.assertEqual(bundle.successor.check_consistency(), ())

    def test_restart_does_not_resend_or_duplicate_todos(self):
        bundle = self._restart()
        self.assertEqual(bundle.intake.adapter.row_count(TODO_TABLE), self.before_todo_rows)
        self.assertEqual(bundle.successor.check_consistency(), ())

    def test_reissued_units_survive_restart_and_can_be_confirmed(self):
        bundle = self._restart()
        unit_id = self.outcome.issued_unit_ids[0]
        self.assertTrue(bundle.todos.get(unit_id).assignee.same_person(NEW_TO))
        bundle.todos.confirm(unit_id, NEW_TO)
        outcome = bundle.todos.run_writeback(unit_id)
        self.assertEqual(outcome.writeback_status, WritebackStatus.VERIFIED.value)

    def test_successor_service_is_optional_and_resolver_can_be_supplied_later(self):
        payload = wfstate.dump_tables(self.flow.adapter)
        bundle = wfstate.restart(
            payload,
            adapter=synth.make_adapter(),
            shift_id=synth.SHIFT_ID,
            clock=synth.fixed_clock(CHANGE_TIME),
        )
        self.assertIsNone(bundle.successor)
        with self.assertRaises(ContractViolation):
            bundle.successor_service()
        service = bundle.successor_service(resolver())
        self.assertEqual(service.changes_of_shift(), self.before_changes)

    def test_missing_change_row_is_reported_not_silently_repaired(self):
        payload = wfstate.dump_tables(self.flow.adapter)
        payload[SUCCESSOR_CHANGE_TABLE] = []
        bundle = wfstate.restart(
            payload,
            adapter=synth.make_adapter(),
            shift_id=synth.SHIFT_ID,
            clock=synth.fixed_clock(CHANGE_TIME),
            resolver=resolver(),
        )
        self.assertEqual(bundle.successor.changes_of_shift(), ())
        self.assertEqual(len(bundle.todos.voided()), len(self.outcome.voided_unit_ids))
        problems = bundle.successor.check_consistency()
        self.assertTrue(problems, "留痕缺失必须报出来")
        self.assertIn("作废留痕与待办表不一致", problems[0])
        self.assertEqual(
            bundle.intake.adapter.row_count(SUCCESSOR_CHANGE_TABLE), 0, "恢复只报问题，不自动补写"
        )

    def test_manually_unvoided_row_is_reported_not_silently_fixed(self):
        payload = wfstate.dump_tables(self.flow.adapter)
        fresh = synth.make_adapter()
        bundle = wfstate.restart(
            payload,
            adapter=fresh,
            shift_id=synth.SHIFT_ID,
            clock=synth.fixed_clock(CHANGE_TIME),
            resolver=resolver(),
        )
        unit_id = self.outcome.voided_unit_ids[0]
        row = bundle.todos.board.find_row(unit_id)
        fresh.update_record(TODO_TABLE, row.record_id, {TODO_COLUMNS["todo_state"]: TODO_STATE_ACTIVE})
        problems = bundle.successor.check_consistency()
        self.assertTrue(problems)
        self.assertIn("作废留痕与待办表不一致", problems[0])


class TestOfflineBoundaries(unittest.TestCase):
    """离线边界：非合成适配器 / 非合成本地解析器一律拒绝（真实钉钉属 L4）。"""

    def test_online_resolver_is_refused(self):
        class DingtalkResolver:
            name = "dingtalk"
            offline = False

            def resolve(self, query: str):
                raise AssertionError("不得被调用")

        with self.assertRaises(AssertionError):
            assert_synthetic_resolver(DingtalkResolver())
        with self.assertRaises(AssertionError):
            SuccessorPrequery(DingtalkResolver())
        with self.assertRaises(AssertionError):
            SuccessorService(
                synth.make_shift(), synth.make_adapter(), resolver=DingtalkResolver()
            )

    def test_real_adapter_is_refused(self):
        class RealAdapter:
            name = "aitable"
            offline = False

        with self.assertRaises(AssertionError):
            SuccessorService(synth.make_shift(), RealAdapter(), resolver=resolver())

    def test_synthetic_resolver_flags(self):
        item = resolver()
        self.assertEqual(item.name, "synthetic")
        self.assertTrue(item.offline)
        self.assertEqual(item.aliases, tuple(sorted(DIRECTORY)))
        assert_synthetic_resolver(item)

    def test_reissued_unit_id_requires_a_unit(self):
        self.assertEqual(reissued_unit_id("SYNTH-EVT-0001", ordinal=3), "SYNTH-EVT-0001#R3")
        with self.assertRaises(ContractViolation):
            reissued_unit_id("  ", ordinal=1)

    def test_attach_unit_requires_a_todo(self):
        flow, _ = submitted_flow()
        unit = ConfirmationUnit(
            unit_id="SYNTH-EVT-9999", scope="event", shift_id=synth.SHIFT_ID, assignee=NEW_TO
        )
        with self.assertRaises(ContractViolation):
            flow.todos.attach_unit(unit)


if __name__ == "__main__":
    unittest.main()
