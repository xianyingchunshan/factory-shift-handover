"""离线隔离审计（issue #9：禁止任何真实网络/钉钉请求）。

做法：用标准库 :mod:`ast` 解析源码里的 import 语句，检查是否引入了网络/进程类
模块。这不是运行时沙箱，而是**可复现的静态证据**：测试断言本卡新增目录
（``integrations/``、``workflow/``）零命中，配合“测试只接合成适配器”的
运行时自检（:func:`integrations.aitable.synthetic.assert_synthetic`）。

只查明确形态，不覆盖所有网络访问形态（例如通过子进程间接发请求）；提交者仍须
人工检查 diff。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, Iterator

#: 禁止引入的顶层模块（网络 / 传输 / 外部进程）。
FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "socket",
        "ssl",
        "selectors",
        "urllib",
        "http",
        "httplib",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "requests",
        "urllib3",
        "aiohttp",
        "httpx",
        "websockets",
        "websocket",
        "dingtalk",
        "subprocess",
    }
)

#: 需要审计的包（相对仓库根目录）。
AUDITED_PACKAGES: tuple[str, ...] = ("integrations", "workflow")


def iter_python_files(root: Path, packages: Iterable[str] = AUDITED_PACKAGES) -> Iterator[Path]:
    """遍历待审计的包内全部 .py 文件（跳过 ``__pycache__``）。"""
    for package in packages:
        base = Path(root) / package
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def imported_top_level_modules(path: Path) -> tuple[str, ...]:
    """返回该文件 import 的顶层模块名（``from a.b import c`` 记 ``a``）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return ()
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入（本包内）
                continue
            if node.module:
                names.append(node.module.split(".")[0])
    return tuple(sorted(set(names)))


def find_forbidden_imports(
    root: Path, packages: Iterable[str] = AUDITED_PACKAGES
) -> list[tuple[str, str]]:
    """返回 ``[(相对路径, 违规模块名)]``；空列表表示审计通过。"""
    findings: list[tuple[str, str]] = []
    root = Path(root)
    for path in iter_python_files(root, packages):
        for module in imported_top_level_modules(path):
            if module in FORBIDDEN_MODULES:
                findings.append((path.relative_to(root).as_posix(), module))
    return findings


def audit_offline(root: Path, packages: Iterable[str] = AUDITED_PACKAGES) -> dict[str, object]:
    """审计摘要，便于测试里打印证据。"""
    findings = find_forbidden_imports(root, packages)
    return {
        "packages": list(packages),
        "forbidden_modules": sorted(FORBIDDEN_MODULES),
        "findings": findings,
        "ok": not findings,
    }


__all__ = [
    "AUDITED_PACKAGES",
    "FORBIDDEN_MODULES",
    "audit_offline",
    "find_forbidden_imports",
    "imported_top_level_modules",
    "iter_python_files",
]
