"""工作流三段实现（issue #9）：

- :mod:`workflow.intake`    阶段① 输入校验（九类字段 + 四条告警 + 关键级强制升级）
- :mod:`workflow.checklist` 阶段③ 清单生成（关键级置顶 → 时间序全量 → 未完移交 →
  完整性汇总 → 确认区；告警未清禁止出清单）
- :mod:`workflow.todos`     阶段③ 确认待办与回写状态机（critical 逐项 / normal 整单）
- :mod:`workflow.state`     重启恢复（从合成存储重建 / 序列化恢复）

业务层直接复用冻结契约 ``contracts/``，不复制其中的规则；本包不联网，
表格读写一律经 :mod:`integrations.aitable` 的合成适配器。
"""

from __future__ import annotations

from .checklist import ConsistencyReport, ShiftChecklistService
from .intake import IntakeOutcome, ShiftIntakeService
from .state import RestartBundle, dump_tables, dumps, load_tables, loads, rebuild
from .todos import TodoAssignmentService

__all__ = [
    "ConsistencyReport",
    "IntakeOutcome",
    "RestartBundle",
    "ShiftChecklistService",
    "ShiftIntakeService",
    "TodoAssignmentService",
    "dump_tables",
    "dumps",
    "load_tables",
    "loads",
    "rebuild",
]
