"""仓库结构测试：必需文件齐全、README 状态声明在场。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import repo_checks  # noqa: E402


class TestRepoStructure(unittest.TestCase):
    def test_required_files_exist(self):
        missing = [p for p in repo_checks.REQUIRED_FILES if not (ROOT / p).is_file()]
        self.assertEqual(missing, [])

    def test_readme_states_not_production_ready(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("不能用于生产", text)

    def test_ci_runs_every_test_subdir(self):
        """CI 必须实际运行每个测试子目录（discover 不递归无 __init__.py 的子目录）。"""
        workflow = (ROOT / ".github" / "workflows" / "checks.yml").read_text(encoding="utf-8")
        subdirs = sorted(
            p.name
            for p in (ROOT / "tests").iterdir()
            if p.is_dir() and any(p.glob("test_*.py"))
        )
        self.assertTrue(subdirs, "tests/ 下应存在子目录测试")
        for name in subdirs:
            self.assertIn(
                f"tests/{name}",
                workflow,
                f"CI 未显式运行 tests/{name}（发现不递归子目录，会漏测）",
            )

    def test_issue_templates_declare_master_dispatch(self):
        task = (ROOT / ".github" / "ISSUE_TEMPLATE" / "task.md").read_text(encoding="utf-8")
        self.assertIn("主控指定后再开工", task)
        probe = (ROOT / ".github" / "ISSUE_TEMPLATE" / "probe.md").read_text(encoding="utf-8")
        self.assertIn("停止，不降门槛", probe)


if __name__ == "__main__":
    unittest.main()
