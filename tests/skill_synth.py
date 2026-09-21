"""skill 测试用的合成装配与真实命令调用（非测试模块，不被 discover 收集）。

**全部数据为虚构**：示例输入与断言取值一律 ``SYNTH-`` 前缀，不含真实人名、群/表 ID、
个人绝对路径或账号配置；表格读写一律走合成适配器，不发任何网络请求。

测试位置说明：本卡新增测试放 ``tests/`` **根目录**——CI 逐目录显式运行
``tests/`` / ``tests/contracts`` / ``tests/workflow`` / ``tests/integrations``，
且仓库守卫 ``tests/test_repo_structure.py`` 要求每个含 ``test_*.py`` 的 ``tests/`` 子目录
都被 CI 显式运行；本卡不允许改 CI，故不新建子目录（详见 ``skill/SKILL.md`` §9）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
for _path in (str(ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from skill.config import SkillInput, parse_input  # noqa: E402

#: 示例输入（仓库内合成夹具）。
EXAMPLES = ROOT / "skill" / "examples"
CLEAN_EXAMPLE = EXAMPLES / "stay_period_handover.json"
BLOCKED_EXAMPLE = EXAMPLES / "stay_period_blocked.json"

#: 真实命令调用的超时（防 CI 卡死）。
CLI_TIMEOUT = 120


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def read_json(path: str | Path) -> Any:
    return json.loads(read_text(path))


def write_json(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def payload_of(path: str | Path = CLEAN_EXAMPLE) -> dict[str, Any]:
    return read_json(path)


def spec_of(path: str | Path = CLEAN_EXAMPLE) -> SkillInput:
    target = Path(path)
    return parse_input(payload_of(target), source=target.name)


def run_cli(*args: str, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """以真实命令跑入口（``python -m skill.run ...``），返回 CompletedProcess。"""
    return subprocess.run(
        [sys.executable, "-B", "-m", "skill.run", *args],
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CLI_TIMEOUT,
    )


def checklist_lines(out_dir: str | Path) -> list[str]:
    return read_text(Path(out_dir) / "checklist.txt").splitlines()


def result_of(out_dir: str | Path) -> dict[str, Any]:
    return read_json(Path(out_dir) / "result.json")


def section_lines(lines: Iterable[str], start_marker: str, end_marker: str) -> list[str]:
    """取 ``■ <start_marker>`` 与 ``■ <end_marker>`` 之间的正文行。"""
    collected: list[str] = []
    started = False
    for line in lines:
        if not started:
            started = line.startswith("■") and start_marker in line
            continue
        if line.startswith("■") and end_marker in line:
            break
        collected.append(line)
    return collected


def timestamps_of(lines: Iterable[str]) -> list[str]:
    """取每行前 16 个字符里的 ``YYYY-MM-DD HH:MM`` 时刻（清单正文口径）。

    正文行可能是 ``- `` 普通项、``** `` 关键级置顶项或 ``  [..]`` 确认单元，
    这里统一剥掉列表标记后再取时刻。
    """
    stamps: list[str] = []
    for line in lines:
        text = line.strip()
        while text.startswith(("*", "-")):
            text = text[1:].lstrip()
        if len(text) >= 16 and text[4] == "-" and text[13] == ":":
            stamps.append(text[:16])
    return stamps


__all__ = [
    "BLOCKED_EXAMPLE",
    "CLEAN_EXAMPLE",
    "CLI_TIMEOUT",
    "EXAMPLES",
    "ROOT",
    "checklist_lines",
    "payload_of",
    "read_json",
    "read_text",
    "result_of",
    "run_cli",
    "section_lines",
    "spec_of",
    "timestamps_of",
    "write_json",
]
