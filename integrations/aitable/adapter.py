"""钉钉AI表格读写的最小抽象口（issue #9 第 2 段）。

口径：

- 只定义业务真正需要的四种动作：``create_record`` / ``update_record`` /
  ``get_record`` / ``list_records``；表名与列名来自 :mod:`contracts.aitable_mapping`。
- **列值一律是原始字符串**（契约 §6：表格列存原始字符串，枚举校验在业务层）。
- 该抽象口**不持有** base_url、app_key、表 ID 之类的运行时配置；真实实现由主控
  在隔离环境中提供，仓库内只提供 :class:`~integrations.aitable.synthetic.SyntheticAitableAdapter`。
- “写入不等于完成”：受理不明一律抛 :class:`AitableWriteUnknown`，调用方只能回查，
  不得重放（SPEC §5 通用护栏）。

本模块只依赖标准库。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping

#: 合成数据显式标识（AGENTS.md：合成测试数据必须可辨识）。
SYNTH_PREFIX = "SYNTH-"


class AitableError(RuntimeError):
    """适配器层错误的基类（不是契约错误码）。"""


class AitableWriteUnknown(AitableError):
    """写入受理结果不明：**只回查，不重放**（对应 ``WRITE_UNKNOWN`` 语义）。"""


class AitableRejected(AitableError):
    """请求被明确拒绝（表不存在、记录不存在等）：可修正后重试，不属“受理不明”。"""


@dataclass(frozen=True)
class TableRow:
    """一条表格记录：稳定 record_id + 原始字符串列值。"""

    record_id: str
    fields: Mapping[str, str]

    def get(self, column: str, default: str = "") -> str:
        value = self.fields.get(column, default)
        return "" if value is None else str(value)

    def to_dict(self) -> dict[str, Any]:
        return {"record_id": self.record_id, "fields": dict(self.fields)}


class AitableAdapter(ABC):
    """表格适配器抽象口。

    ``name`` 用于自检与留痕：合成实现固定为 ``synthetic``，便于断言“测试未接真实系统”。
    """

    name = "abstract"

    #: 该适配器是否完全离线（真实实现应为 False，仓库内实现必须为 True）。
    offline = True

    @abstractmethod
    def create_record(self, table: str, fields: Mapping[str, str]) -> str:
        """新增一行，返回 ``record_id``；受理不明抛 :class:`AitableWriteUnknown`。"""

    @abstractmethod
    def update_record(self, table: str, record_id: str, fields: Mapping[str, str]) -> str:
        """更新已有行（只覆盖传入的列），返回 ``record_id``。"""

    @abstractmethod
    def get_record(self, table: str, record_id: str) -> TableRow | None:
        """按 ``record_id`` 读回一行；不存在返回 ``None``（读操作不抛 unknown）。"""

    @abstractmethod
    def list_records(self, table: str) -> tuple[TableRow, ...]:
        """列出整表（合成适配器保序）；业务侧在此之上做筛选。"""


__all__ = [
    "SYNTH_PREFIX",
    "AitableAdapter",
    "AitableError",
    "AitableRejected",
    "AitableWriteUnknown",
    "TableRow",
]
