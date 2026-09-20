"""仓库保守检查：必需文件齐全，扫描个人路径、个人署名、疑似密钥与手机号。

这是保守检查，不覆盖所有敏感信息形态；提交者必须人工检查 diff，
远程 CI 与受保护分支才是多人合并约束。

用法：
    python scripts/repo_checks.py            # 检查仓库全部文件
    python scripts/repo_checks.py --staged   # 仅检查暂存文件（提交钩子使用）
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FILES = [
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SPEC.md",
    "tickets.md",
    ".github/ISSUE_TEMPLATE/task.md",
    ".github/ISSUE_TEMPLATE/probe.md",
    ".github/pull_request_template.md",
    ".github/CODEOWNERS",
    ".github/workflows/checks.yml",
    "scripts/repo_checks.py",
    "scripts/install_hooks.py",
    ".githooks/pre-commit",
]

# 保守规则清单：只查明确形态，不覆盖所有敏感信息形态。
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("个人绝对路径", re.compile(r"[CF]:[\\/]Users", re.IGNORECASE)),
    ("个人绝对路径", re.compile(r"F:[\\/]Claude Code", re.IGNORECASE)),
    ("个人署名", re.compile(r"署名[:：]")),
    ("疑似密钥/令牌", re.compile(r"gh[posu]_[A-Za-z0-9]{20,}")),
    ("疑似密钥/令牌", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("疑似密钥/令牌", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("疑似密钥/令牌", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("疑似真实手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
]

SKIP_DIRS = {".git", "__pycache__", ".venv", ".pytest_cache", ".local"}


def _force_utf8_stdio() -> None:
    """Windows 默认控制台编码（cp1252/GBK）打印中文会崩，统一改 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_force_utf8_stdio()


def scan_text(text: str) -> list[tuple[str, int]]:
    """返回 (违规类别, 行号) 列表。"""
    findings: list[tuple[str, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in PATTERNS:
            if pattern.search(line):
                findings.append((label, lineno))
    return findings


def scan_file(path: Path) -> list[str]:
    """扫描单个文件，返回问题消息；二进制或无法解码的文件跳过。"""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if b"\0" in data:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    rel = path.relative_to(ROOT)
    return [f"{rel}: {label} (第 {lineno} 行)" for label, lineno in scan_text(text)]


def git_files(args: list[str]) -> list[Path]:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return []
    return [ROOT / line for line in result.stdout.splitlines() if line.strip()]


def workspace_files() -> list[Path]:
    """优先取 git 跟踪文件；无提交历史时回退到全目录扫描。"""
    tracked = git_files(["ls-files"])
    if tracked:
        return tracked
    return [
        p
        for p in ROOT.rglob("*")
        if p.is_file() and not (set(p.parts) & SKIP_DIRS)
    ]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    problems: list[str] = []

    missing = [p for p in REQUIRED_FILES if not (ROOT / p).is_file()]
    problems += [f"缺少必需文件: {p}" for p in missing]

    if "--staged" in argv:
        paths = git_files(["diff", "--cached", "--name-only"])
    else:
        paths = workspace_files()
    for path in paths:
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & SKIP_DIRS):
            problems += scan_file(path)

    for problem in problems:
        print(problem)
    if problems:
        print(f"共 {len(problems)} 项问题；这是保守检查，另请人工检查 diff。")
        return 1
    print("仓库检查通过（保守清单；不覆盖所有敏感信息形态，请再人工检查 diff）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
