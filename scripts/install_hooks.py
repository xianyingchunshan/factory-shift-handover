"""安装本地提交钩子：git config core.hooksPath .githooks。

本地钩子可被跳过，不是完整安全边界；远程 CI 与受保护分支才是合并检查依据。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    hook = ROOT / ".githooks" / "pre-commit"
    if not hook.is_file():
        print("缺少 .githooks/pre-commit，无法安装。", file=sys.stderr)
        return 1
    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"], cwd=ROOT, check=True
    )
    print("已设置 core.hooksPath=.githooks，提交前将运行 repo_checks --staged。")
    print("注意：本地钩子可被跳过，远程 CI 才是合并检查依据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
