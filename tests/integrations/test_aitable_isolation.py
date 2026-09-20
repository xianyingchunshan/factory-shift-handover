"""离线隔离审计测试（issue #9：禁止任何真实网络/钉钉请求）。

证据链：

1. 静态审计：``integrations/``、``workflow/`` 未引入网络/进程类模块（AST 解析）；
2. 负向对照：审计器必须能抓到故意植入的违规导入（否则审计无效）；
3. 运行时自检：流程只接合成适配器（``assert_synthetic``）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from integrations.aitable.isolation import (  # noqa: E402
    AUDITED_PACKAGES,
    FORBIDDEN_MODULES,
    audit_offline,
    find_forbidden_imports,
    imported_top_level_modules,
    iter_python_files,
)
from integrations.aitable.synthetic import assert_synthetic  # noqa: E402

import aitable_synth as synth  # noqa: E402


class TestOfflineAudit(unittest.TestCase):
    def test_audited_packages_exist(self):
        for package in AUDITED_PACKAGES:
            self.assertTrue((_ROOT / package).is_dir(), f"缺少待审计包: {package}")

    def test_no_forbidden_imports_in_new_packages(self):
        findings = find_forbidden_imports(_ROOT)
        self.assertEqual(findings, [], f"发现网络/进程类导入: {findings}")

    def test_audit_summary_is_clean(self):
        report = audit_offline(_ROOT)
        self.assertTrue(report["ok"])
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["packages"], list(AUDITED_PACKAGES))
        self.assertIn("socket", report["forbidden_modules"])

    def test_audit_scans_all_python_files(self):
        files = [path.relative_to(_ROOT).as_posix() for path in iter_python_files(_ROOT)]
        self.assertIn("integrations/aitable/adapter.py", files)
        self.assertIn("integrations/aitable/synthetic.py", files)
        self.assertIn("workflow/intake.py", files)
        self.assertGreaterEqual(len(files), 8)

    def test_forbidden_imports_are_real(self):
        for module in ("socket", "urllib", "requests"):
            self.assertIn(module, FORBIDDEN_MODULES)


class TestAuditNegativeControl(unittest.TestCase):
    """负向对照：审计器必须抓到植入的违规导入。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, name: str, text: str) -> Path:
        path = self.root / "synthetic_pkg" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_network_import_is_flagged(self):
        self._write("bad.py", "import socket\n\n\ndef send():\n    return socket\n")
        findings = find_forbidden_imports(self.root, ("synthetic_pkg",))
        self.assertEqual(findings, [("synthetic_pkg/bad.py", "socket")])

    def test_from_import_is_flagged(self):
        self._write("bad.py", "from urllib.request import urlopen\n")
        findings = find_forbidden_imports(self.root, ("synthetic_pkg",))
        self.assertEqual(findings, [("synthetic_pkg/bad.py", "urllib")])

    def test_relative_and_safe_imports_are_clean(self):
        self._write("good.py", "import json\nfrom . import helper\nfrom decimal import Decimal\n")
        self.assertEqual(find_forbidden_imports(self.root, ("synthetic_pkg",)), [])

    def test_comment_mentioning_socket_is_not_a_finding(self):
        self._write("good.py", "# 这里不 import socket，只写在注释里\nVALUE = 1\n")
        self.assertEqual(find_forbidden_imports(self.root, ("synthetic_pkg",)), [])

    def test_broken_file_does_not_crash_audit(self):
        self._write("broken.py", "def broken(:\n")
        view = imported_top_level_modules(self.root / "synthetic_pkg" / "broken.py")
        self.assertEqual(view, ())


class TestRuntimeGuard(unittest.TestCase):
    def test_flow_only_accepts_synthetic_adapter(self):
        assert_synthetic(synth.make_adapter())

    def test_flow_rejects_online_adapter(self):
        class Online:
            name = "real"
            offline = False

        with self.assertRaises(AssertionError):
            assert_synthetic(Online())

    def test_synthetic_adapter_never_reports_online(self):
        adapter = synth.make_adapter()
        self.assertTrue(adapter.offline)
        self.assertEqual(adapter.name, "synthetic")


if __name__ == "__main__":
    unittest.main()
