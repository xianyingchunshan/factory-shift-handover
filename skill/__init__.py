"""T06 可独立运行的 skill 封装（issue #19）：输入（校验）→ 过程（AI表格读写）→ 输出（清单）。

- 入口：``python -m skill.run --input <输入.json> --out <输出目录>``（或 ``python -m skill``）。
- 契约说明：同目录 ``SKILL.md``（输入/输出契约、配置项、告警语义、失败处置、未覆盖边界）。
- 全部读写走**合成适配器**（离线内存实现），不联网、不接真实钉钉/AI表格、无新依赖。

本模块只导出入口所需的最小面，业务编排在 :mod:`skill.pipeline`，输入契约在 :mod:`skill.config`。
"""

from __future__ import annotations

from .config import SKILL_NAME, SKILL_VERSION, SkillInput, SkillInputError, load_input, parse_input
from .pipeline import (
    EXIT_BLOCKED,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    OUTCOME_BLOCKED,
    OUTCOME_CHECKLIST,
    SkillResult,
    run,
    run_resume,
)

__all__ = [
    "EXIT_BLOCKED",
    "EXIT_INPUT_ERROR",
    "EXIT_OK",
    "OUTCOME_BLOCKED",
    "OUTCOME_CHECKLIST",
    "SKILL_NAME",
    "SKILL_VERSION",
    "SkillInput",
    "SkillInputError",
    "SkillResult",
    "load_input",
    "parse_input",
    "run",
    "run_resume",
]
