"""EAM 只读适配器测试（issue #23 第 1/5 条验证）。

三层证据：

1. **协议无写方法**：``EamReadOnlyAdapter`` 声明的公开方法只有 ``list_defects`` /
   ``list_hazards``；带写方法命名特征（create/update/delete/submit/...）的属性一个都
   没有——协议层、合成实现层、模块函数层三处都扫；
2. **合成夹具行为**：窗口过滤按日历日下推（分钟级越界留给 E003）、发现时间缺失或
   无法解析的记录**原样返回不静默丢弃**、拉取留痕可回读；
3. **入口守卫与离线审计**：误接真实适配器直接失败；``integrations/eam/`` 零网络导入。

端到端入表与告警联动见 ``tests/workflow/test_eam_pull.py``。
"""

from __future__ import annotations

import ast
import inspect
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.errors import ContractViolation  # noqa: E402

import integrations.eam.adapter as eam_adapter  # noqa: E402
import integrations.eam.mapping as eam_mapping  # noqa: E402
import integrations.eam.synthetic as eam_synthetic  # noqa: E402
from integrations.aitable.isolation import (  # noqa: E402
    find_forbidden_imports,
    iter_python_files,
)
from integrations.eam.adapter import (  # noqa: E402
    KIND_DEFECT,
    KIND_HAZARD,
    READ_METHODS,
    READ_ONLY_AUDIT_HELPERS,
    READ_ONLY_NOTE,
    WRITE_METHOD_TOKENS,
    EamReadOnlyAdapter,
    EamRecord,
    EamWindow,
    defect,
    read_only_method_names,
    record_in_window,
    write_like_method_names,
)
from integrations.eam.synthetic import (  # noqa: E402
    SyntheticEamAdapter,
    assert_synthetic_eam,
    empty_adapter,
)

import eam_synth as synth  # noqa: E402

_EAM_DIR = _ROOT / "integrations" / "eam"
_EAM_PY = sorted(path.name for path in _EAM_DIR.glob("*.py"))


class TestReadOnlyProtocol(unittest.TestCase):
    """只读协议：没有写方法，没有写方法命名的入口。"""

    def test_protocol_declares_exactly_the_two_read_methods(self):
        self.assertEqual(read_only_method_names(), READ_METHODS)
        self.assertEqual(READ_METHODS, ("list_defects", "list_hazards"))

    def test_protocol_has_no_write_like_methods(self):
        self.assertEqual(write_like_method_names(EamReadOnlyAdapter), ())

    def test_synthetic_adapter_has_no_write_like_methods(self):
        self.assertEqual(write_like_method_names(SyntheticEamAdapter), ())

    def test_writable_tokens_are_real(self):
        for token in ("create", "update", "delete"):
            self.assertIn(token, WRITE_METHOD_TOKENS)

    def test_module_level_functions_have_no_write_semantics(self):
        """模块级公开函数同样不得出现写方法命名特征（本包整体只读）。

        只读审计辅助函数（名字里含 “write” 但只做扫描）按 :data:`READ_ONLY_AUDIT_HELPERS`
        显式豁免——这本身是可审查的清单，不是宽泛的白名单。
        """
        offenders: list[str] = []
        for module in (eam_adapter, eam_mapping, eam_synthetic):
            for name, value in vars(module).items():
                if name.startswith("_") or not inspect.isfunction(value):
                    continue
                if name in READ_ONLY_AUDIT_HELPERS:
                    continue
                if any(token in name.lower() for token in WRITE_METHOD_TOKENS):
                    offenders.append(f"{module.__name__}.{name}")
        self.assertEqual(offenders, [])

    def test_audit_helper_exemptions_are_real_and_narrow(self):
        self.assertEqual(READ_ONLY_AUDIT_HELPERS, ("write_like_method_names",))
        for name in READ_ONLY_AUDIT_HELPERS:
            self.assertTrue(callable(getattr(eam_adapter, name, None)))

    def test_abstract_methods_are_read_only(self):
        self.assertEqual(
            sorted(getattr(EamReadOnlyAdapter, "__abstractmethods__", ())), sorted(READ_METHODS)
        )

    def test_protocol_docstring_states_read_only(self):
        text = (EamReadOnlyAdapter.__doc__ or "") + READ_ONLY_NOTE
        self.assertIn("只读", text)
        self.assertIn("没有任何写方法", READ_ONLY_NOTE)

    def test_records_are_frozen_snapshots(self):
        record = defect(synth.D1, "SYNTH 只读快照", "2026-03-05 09:30", severity="一般")
        with self.assertRaises(Exception):
            record.ref_no = "SYNTH-OTHER"  # type: ignore[misc]

    def test_protocol_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            EamReadOnlyAdapter()  # type: ignore[abstract]

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ContractViolation):
            EamRecord(kind="equipment", ref_no=synth.D1)


class TestSyntheticAdapterReads(unittest.TestCase):
    """合成夹具：窗口过滤口径 + 不静默丢弃 + 拉取留痕。"""

    def setUp(self):
        self.adapter = synth.make_adapter()
        self.window = synth.make_window()

    def test_reads_the_fixtures_for_the_window(self):
        self.assertEqual(len(self.adapter.list_defects(self.window)), len(synth.defects()))
        self.assertEqual(len(self.adapter.list_hazards(self.window)), len(synth.hazards()))
        self.assertEqual(self.adapter.fixture_count(), synth.expected_fetched())

    def test_out_of_day_range_records_are_filtered(self):
        adapter = SyntheticEamAdapter(defects=synth.boundary_defects())
        refs = [record.ref_no for record in adapter.list_defects(self.window)]
        self.assertIn(synth.LAST_DAY_LATE, refs)  # 同日历日晚于窗口结束 → 仍返回（由 E003 判定）
        self.assertNotIn(synth.OUT_OF_DAY_RANGE, refs)  # 下一个日历日 → 不在本窗口

    def test_records_with_missing_or_unparsable_time_are_never_silently_dropped(self):
        adapter = SyntheticEamAdapter(defects=synth.unmappable_defects())
        refs = [record.ref_no for record in adapter.list_defects(self.window)]
        self.assertIn(synth.BAD_TIME_REF, refs)
        # 缺编号的记录也在（编号为空照样返回，由上层显式报出）
        self.assertIn("", refs)

        with_missing_time = SyntheticEamAdapter(defects=synth.defects()[3:4])  # 缺发现时间
        self.assertEqual(len(with_missing_time.list_defects(self.window)), 1)

    def test_date_only_record_in_day_range_is_returned(self):
        adapter = SyntheticEamAdapter(defects=synth.defects()[4:5])  # 只有日期
        self.assertEqual(len(adapter.list_defects(self.window)), 1)

    def test_window_filter_uses_calendar_days_not_minute_cuts(self):
        """跨零点相连日历日：首日 23:30 属期内（分钟级越界留给 E003，适配器不裁）。"""
        record = defect(synth.D1, "SYNTH 跨零点", "2026-03-02 23:30")
        self.assertTrue(record_in_window(record, self.window))
        parsed_day = defect(synth.D1, "SYNTH 期末日", "2026-04-01 12:00")
        self.assertTrue(record_in_window(parsed_day, self.window))

    def test_other_window_returns_only_time_missing_records(self):
        """别的窗口拉不到期内记录，但**缺发现时间**的记录仍原样返回（不静默丢）。

        这是协议口径：窗口过滤只按可解析的发现时间判定，缺失/无法解析的记录一律
        交上层显式拒收（E001），适配器不得替它做“丢掉”的决定。
        """
        next_window = EamWindow.from_shift(synth.make_next_shift())
        self.assertEqual(
            tuple(record.ref_no for record in self.adapter.list_defects(next_window)),
            (synth.D4,),
        )
        self.assertEqual(self.adapter.list_hazards(next_window), ())

    def test_read_calls_are_traceable(self):
        self.adapter.list_defects(self.window)
        self.adapter.list_hazards(self.window)
        methods = [call.method for call in self.adapter.calls]
        self.assertEqual(methods, ["list_defects", "list_hazards"])
        for call in self.adapter.calls:
            self.assertEqual(call.shift_id, synth.SHIFT_ID)
            self.assertIn("2026-03-02 08:00", call.window)

    def test_window_is_required(self):
        with self.assertRaises(ContractViolation):
            self.adapter.list_defects("2026-03-02")  # type: ignore[arg-type]

    def test_fixture_kind_is_checked(self):
        with self.assertRaises(ContractViolation):
            SyntheticEamAdapter(defects=(synth.hazards()[0],))
        with self.assertRaises(ContractViolation):
            SyntheticEamAdapter(hazards=(synth.defects()[0],))

    def test_window_requires_aware_boundaries(self):
        from datetime import datetime

        with self.assertRaises(ContractViolation):
            EamWindow(shift_id=synth.SHIFT_ID, start=datetime(2026, 3, 2, 8, 0), end=synth.END)
        with self.assertRaises(ContractViolation):
            EamWindow(shift_id="", start=synth.START, end=synth.END)
        with self.assertRaises(ContractViolation):
            EamWindow(shift_id=synth.SHIFT_ID, start=synth.END, end=synth.START)

    def test_window_reads_back_the_stay_period(self):
        facts = self.window.to_dict()
        self.assertEqual(facts["shift_id"], synth.SHIFT_ID)
        self.assertEqual(facts["start"], "2026-03-02 08:00")
        self.assertEqual(facts["end"], "2026-04-01 08:00")
        self.assertEqual(facts["calendar_days"], 31)
        self.assertEqual(len(self.window.calendar_days()), 31)

    def test_empty_adapter_reads_nothing(self):
        adapter = empty_adapter()
        self.assertEqual(adapter.list_defects(self.window), ())
        self.assertEqual(adapter.fixture_count(), 0)


class TestRuntimeGuard(unittest.TestCase):
    """入口守卫：误接真实适配器直接失败（仿 assert_synthetic 模式）。"""

    def test_synthetic_adapter_passes(self):
        assert_synthetic_eam(synth.make_adapter())

    def test_real_looking_adapter_is_rejected(self):
        class RealLooking(EamReadOnlyAdapter):
            name = "eam"
            offline = False

            def list_defects(self, window):  # pragma: no cover - 不会被调用
                return ()

            def list_hazards(self, window):  # pragma: no cover - 不会被调用
                return ()

        with self.assertRaises(AssertionError):
            assert_synthetic_eam(RealLooking())

    def test_synthetic_adapter_never_reports_online(self):
        adapter = synth.make_adapter()
        self.assertTrue(adapter.offline)
        self.assertEqual(adapter.name, "synthetic")


class TestOfflineAudit(unittest.TestCase):
    """离线证据：本包只依赖标准库与冻结契约。"""

    def test_eam_package_files_exist(self):
        for name in ("__init__.py", "adapter.py", "mapping.py", "synthetic.py"):
            self.assertIn(name, _EAM_PY)

    def test_repo_audit_has_no_forbidden_imports(self):
        self.assertEqual(find_forbidden_imports(_ROOT), [])

    def test_eam_files_are_covered_by_the_audit(self):
        audited = {path.relative_to(_ROOT).as_posix() for path in iter_python_files(_ROOT)}
        for name in ("adapter.py", "mapping.py", "synthetic.py"):
            self.assertIn(f"integrations/eam/{name}", audited)

    def test_no_socket_or_http_usage_in_eam_sources(self):
        for path in _EAM_DIR.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], {"socket", "http", "urllib", "subprocess"})
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn(
                        node.module.split(".")[0], {"socket", "http", "urllib", "subprocess"}
                    )

    def test_kind_constants_are_the_two_allowed_sources(self):
        self.assertEqual((KIND_DEFECT, KIND_HAZARD), ("defect", "hazard"))


if __name__ == "__main__":
    unittest.main()
