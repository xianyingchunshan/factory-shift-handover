"""repo_checks 的单元测试。测试样例中的违规字符串均由拼接构造，
源码里不出现完整违规字面量。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import repo_checks  # noqa: E402


class TestCleanText(unittest.TestCase):
    def test_clean_text_has_no_findings(self):
        self.assertEqual(repo_checks.scan_text("正常文本，无违规内容。\n第二行也正常。\n"), [])

    def test_shared_rule_word_is_not_flagged(self):
        # 提到"个人署名"这条规则本身不算署名违规（无冒号跟随）。
        text = "源码、文档及生成产物不得添加个人署名。"
        self.assertEqual(
            [f for f in repo_checks.scan_text(text) if f[0] == "个人署名"], []
        )


class TestForbiddenPatterns(unittest.TestCase):
    def test_personal_path_is_flagged(self):
        text = "配置在 " + "C" + ":" + chr(92) + "Users" + chr(92) + "demo 目录下"
        labels = [label for label, _ in repo_checks.scan_text(text)]
        self.assertIn("个人绝对路径", labels)

    def test_token_is_flagged(self):
        text = "token=" + "gh" + "p_" + "a" * 30
        labels = [label for label, _ in repo_checks.scan_text(text)]
        self.assertIn("疑似密钥/令牌", labels)

    def test_signature_line_is_flagged(self):
        text = "署名" + "：" + "某人"
        labels = [label for label, _ in repo_checks.scan_text(text)]
        self.assertIn("个人署名", labels)

    def test_phone_number_is_flagged(self):
        text = "联系电话 " + "139" + "1234" + "5678"
        labels = [label for label, _ in repo_checks.scan_text(text)]
        self.assertIn("疑似真实手机号", labels)


class TestScanFile(unittest.TestCase):
    def test_binary_file_is_skipped(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob.bin"
            path.write_bytes(b"\0" + b"\xff" * 16)
            self.assertEqual(repo_checks.scan_file(path), [])


if __name__ == "__main__":
    unittest.main()
