"""重启恢复：从合成存储重建 / 序列化恢复（issue #9 验收项）。

两条恢复路径（都不需要重新写表、不重放外部写入）：

1. **从合成存储重建**：:func:`rebuild` 读班次表 + 输入留痕表 + 待办表，
   重建服务对象；事件按**输入留痕重放复验**（同一规则、同一结论：告警重新建立、
   拒收重新成立），**不产生第二行**。
2. **序列化恢复**：:func:`dump_tables` / :func:`dumps` 导出四张表的原始字符串快照，
   :func:`loads` / :func:`load_tables` 导入到新适配器（模拟换进程/换机器），再走
   :func:`rebuild`。

已知边界：导入到新适配器时记录 ID 由目标适配器重新分配（真实实现换 Base 同理）；
业务键（班次ID / 事件ID / 单元ID / 待办ID 列值）保持不变，恢复后行为一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from contracts.errors import ContractViolation
from contracts.shift import ShiftRecord

from integrations.aitable.adapter import AitableAdapter
from integrations.aitable.synthetic import assert_synthetic
from integrations.aitable.tables import (
    ALARM_JOURNAL_TABLE,
    EVENT_TABLE,
    INTAKE_JOURNAL_TABLE,
    SHIFT_TABLE,
    TODO_TABLE,
    ShiftTable,
)

from .checklist import ShiftChecklistService
from .intake import RestoreIssue, ShiftIntakeService
from .todos import TodoAssignmentService

#: 参与恢复的五张表（两张契约表 + 三张工作流辅助表）。
RESTORE_TABLES: tuple[str, ...] = (
    SHIFT_TABLE,
    EVENT_TABLE,
    INTAKE_JOURNAL_TABLE,
    ALARM_JOURNAL_TABLE,
    TODO_TABLE,
)


@dataclass(frozen=True)
class RestartBundle:
    """重启后的工作流状态（班次 + 阶段①服务 + 待办服务 + 恢复期发现的问题）。"""

    shift: ShiftRecord
    intake: ShiftIntakeService
    todos: TodoAssignmentService
    issues: tuple[RestoreIssue, ...] = ()

    def checklist(self, *, clock: Callable[[], datetime] | None = None) -> ShiftChecklistService:
        """按恢复出的班次/事件/告警账本构造清单服务（告警未清仍会被拦）。"""
        return ShiftChecklistService(
            self.shift,
            self.intake.store,
            self.intake.ledger,
            self.intake.adapter,
            clock=clock or self.intake.clock,
            require_offline=False,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift.shift_id,
            "status": self.shift.status,
            "events": len(self.intake.events),
            "stored_rows": self.intake.event_table.count(self.shift.shift_id),
            "open_alarms": self.intake.ledger.open_count,
            "confirmations": len(self.todos.confirmations),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def dump_tables(
    adapter: AitableAdapter, tables: Iterable[str] = RESTORE_TABLES
) -> dict[str, list[dict[str, Any]]]:
    """导出指定表的原始字符串快照（保序）。"""
    payload: dict[str, list[dict[str, Any]]] = {}
    for table in tables:
        payload[table] = [
            {"record_id": row.record_id, "fields": dict(row.fields)}
            for row in adapter.list_records(table)
        ]
    return payload


def load_tables(adapter: AitableAdapter, payload: Mapping[str, Any]) -> None:
    """把快照导入适配器（记录 ID 由目标适配器重新分配，业务键保持不变）。"""
    for table, rows in payload.items():
        if not isinstance(rows, list):
            raise ContractViolation(f"快照表 {table!r} 必须是记录列表")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ContractViolation(f"快照表 {table!r} 的记录必须是映射")
            fields = row.get("fields") or {}
            if not isinstance(fields, Mapping):
                raise ContractViolation(f"快照表 {table!r} 的 fields 必须是映射")
            adapter.create_record(str(table), {str(k): str(v) for k, v in fields.items()})


def dumps(payload: Mapping[str, Any]) -> str:
    """序列化为 JSON 文本（稳定排序，便于 diff 与逐字记录）。"""
    return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2)


def loads(text: str) -> dict[str, Any]:
    """从 JSON 文本恢复快照。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractViolation(f"状态快照不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractViolation("状态快照必须是 JSON 对象")
    return payload


def write_snapshot(path: str | Path, adapter: AitableAdapter) -> Path:
    """把快照写到文件（调用方自选路径；仓库内不得提交运行数据）。"""
    target = Path(path)
    target.write_text(dumps(dump_tables(adapter)), encoding="utf-8")
    return target


def read_snapshot(path: str | Path) -> dict[str, Any]:
    return loads(Path(path).read_text(encoding="utf-8"))


def rebuild(
    adapter: AitableAdapter,
    *,
    shift_id: str,
    clock: Callable[[], datetime] | None = None,
    require_offline: bool = True,
) -> RestartBundle:
    """从存储重建工作流状态（模拟重启）：不重写表格、不重放外部写入。"""
    if require_offline:
        assert_synthetic(adapter)
    shift = ShiftTable(adapter).get(shift_id)
    if shift is None:
        raise ContractViolation(f"班次表里没有班次 {shift_id!r}，无法恢复")
    intake = ShiftIntakeService(shift, adapter, clock=clock, require_offline=require_offline)
    issues = intake.restore()
    todos = TodoAssignmentService(adapter, shift, clock=clock, require_offline=require_offline)
    todos.restore()
    return RestartBundle(shift=shift, intake=intake, todos=todos, issues=issues)


def restart(
    payload: Mapping[str, Any] | None = None,
    *,
    adapter: AitableAdapter | None = None,
    shift_id: str,
    clock: Callable[[], datetime] | None = None,
) -> RestartBundle:
    """便利入口：``payload`` 为序列化快照时先导入再重建；否则直接在原适配器上重建。"""
    target = adapter
    if target is None:
        from integrations.aitable.synthetic import SyntheticAitableAdapter

        target = SyntheticAitableAdapter()
    if payload is not None:
        load_tables(target, payload)
    return rebuild(target, shift_id=shift_id, clock=clock)


__all__ = [
    "RESTORE_TABLES",
    "RestartBundle",
    "dump_tables",
    "dumps",
    "load_tables",
    "loads",
    "read_snapshot",
    "rebuild",
    "restart",
    "write_snapshot",
]
