"""合成表格适配器：内存实现，**不发任何真实请求**（issue #9 第 2 段）。

用途与保证：

- 全部读写落在内存字典，无 socket / 无 HTTP 客户端 / 无凭据；
  :mod:`integrations.aitable.isolation` 提供静态审计，测试断言“未引入网络模块”。
- 记录 ID 由本适配器确定性生成（``SYNTH-REC-000001`` 递增），便于断言。
- 可注入故障，用于覆盖“受理不明先回查”“明确失败可重试”两条路径：
  ``unknown`` → 抛 :class:`AitableWriteUnknown`；
  ``rejected`` → 抛 :class:`AitableRejected`；
  ``lose`` → 返回 record_id 但不落库（模拟写入丢失，回读应得 not_applied）。
- ``export_state`` / ``import_state`` 支持“模拟重启”：把存储搬到新进程/新适配器实例。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .adapter import (
    SYNTH_PREFIX,
    AitableAdapter,
    AitableRejected,
    AitableWriteUnknown,
    TableRow,
)

#: 可注入故障的动作名。
CREATE = "create"
UPDATE = "update"
GET = "get"
LIST = "list"
OPS: tuple[str, ...] = (CREATE, UPDATE, GET, LIST)

#: 故障类型。
FAIL_UNKNOWN = "unknown"
FAIL_REJECTED = "rejected"
FAIL_LOSE = "lose"
FAIL_KINDS: tuple[str, ...] = (FAIL_UNKNOWN, FAIL_REJECTED, FAIL_LOSE)


@dataclass(frozen=True)
class Call:
    """一次适配器调用的留痕（不含凭据，字段值是合成数据）。"""

    op: str
    table: str
    record_id: str = ""
    fields: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "table": self.table,
            "record_id": self.record_id,
            "columns": sorted(self.fields),
        }


@dataclass(frozen=True)
class _Failure:
    op: str
    kind: str


class SyntheticAitableAdapter(AitableAdapter):
    """内存合成适配器（离线、可注入故障、可搬运状态）。"""

    name = "synthetic"
    offline = True

    def __init__(self, *, record_prefix: str = f"{SYNTH_PREFIX}REC") -> None:
        self.record_prefix = str(record_prefix).strip() or f"{SYNTH_PREFIX}REC"
        self._tables: dict[str, dict[str, dict[str, str]]] = {}
        self._calls: list[Call] = []
        self._failures: list[_Failure] = []
        self._seq = 0

    # ---- 故障注入（测试与反向验证用） -----------------------------------

    def queue_failure(self, op: str, kind: str = FAIL_UNKNOWN) -> None:
        """为下一个 ``op`` 动作排队一个故障。"""
        if op not in OPS:
            raise ValueError(f"未知动作 {op!r}（可用: {', '.join(OPS)}）")
        if kind not in FAIL_KINDS:
            raise ValueError(f"未知故障类型 {kind!r}（可用: {', '.join(FAIL_KINDS)}）")
        self._failures.append(_Failure(op=op, kind=kind))

    def clear_failures(self) -> None:
        self._failures.clear()

    def _take_failure(self, op: str) -> str | None:
        for index, failure in enumerate(self._failures):
            if failure.op == op:
                del self._failures[index]
                return failure.kind
        return None

    # ---- 读写 -----------------------------------------------------------

    def create_record(self, table: str, fields: Mapping[str, str]) -> str:
        self._calls.append(Call(op=CREATE, table=table, fields=dict(fields)))
        kind = self._take_failure(CREATE)
        if kind == FAIL_UNKNOWN:
            raise AitableWriteUnknown(f"合成适配器：{table} 新增受理不明（注入故障）")
        if kind == FAIL_REJECTED:
            raise AitableRejected(f"合成适配器：{table} 新增被拒绝（注入故障）")
        record_id = self._next_record_id()
        if kind == FAIL_LOSE:
            # 受理不明→ 客户端拿到 ID，但库里没有这行（回读应得 not_applied）。
            return record_id
        self._tables.setdefault(table, {})[record_id] = self._clean(fields)
        return record_id

    def update_record(self, table: str, record_id: str, fields: Mapping[str, str]) -> str:
        self._calls.append(
            Call(op=UPDATE, table=table, record_id=record_id, fields=dict(fields))
        )
        kind = self._take_failure(UPDATE)
        if kind == FAIL_UNKNOWN:
            raise AitableWriteUnknown(f"合成适配器：{table}/{record_id} 更新受理不明（注入故障）")
        if kind == FAIL_REJECTED:
            raise AitableRejected(f"合成适配器：{table}/{record_id} 更新被拒绝（注入故障）")
        if kind == FAIL_LOSE:
            return record_id
        bucket = self._tables.get(table, {})
        if record_id not in bucket:
            raise AitableRejected(f"合成适配器：{table} 无此记录 {record_id}")
        bucket[record_id].update(self._clean(fields))
        return record_id

    def get_record(self, table: str, record_id: str) -> TableRow | None:
        self._calls.append(Call(op=GET, table=table, record_id=record_id))
        kind = self._take_failure(GET)
        if kind == FAIL_UNKNOWN:
            raise AitableWriteUnknown(f"合成适配器：{table}/{record_id} 读回受理不明（注入故障）")
        if kind == FAIL_REJECTED:
            raise AitableRejected(f"合成适配器：{table} 读回被拒绝（注入故障）")
        row = self._tables.get(table, {}).get(record_id)
        if row is None:
            return None
        return TableRow(record_id=record_id, fields=dict(row))

    def list_records(self, table: str) -> tuple[TableRow, ...]:
        self._calls.append(Call(op=LIST, table=table))
        kind = self._take_failure(LIST)
        if kind == FAIL_UNKNOWN:
            raise AitableWriteUnknown(f"合成适配器：{table} 列举受理不明（注入故障）")
        if kind == FAIL_REJECTED:
            raise AitableRejected(f"合成适配器：{table} 列举被拒绝（注入故障）")
        return tuple(
            TableRow(record_id=record_id, fields=dict(row))
            for record_id, row in self._tables.get(table, {}).items()
        )

    # ---- 留痕与状态搬运 -------------------------------------------------

    @property
    def calls(self) -> tuple[Call, ...]:
        return tuple(self._calls)

    def calls_for(self, op: str) -> tuple[Call, ...]:
        return tuple(call for call in self._calls if call.op == op)

    def tables(self) -> tuple[str, ...]:
        return tuple(self._tables)

    def row_count(self, table: str) -> int:
        return len(self._tables.get(table, {}))

    def export_state(self) -> dict[str, Any]:
        """导出全部表（原始字符串），供“模拟重启”或快照序列化。"""
        return {
            "record_prefix": self.record_prefix,
            "seq": self._seq,
            "tables": {
                table: {record_id: dict(row) for record_id, row in rows.items()}
                for table, rows in self._tables.items()
            },
        }

    def import_state(self, payload: Mapping[str, Any]) -> None:
        """导入 :meth:`export_state` 的产物（覆盖当前内存内容）。"""
        self.record_prefix = str(payload.get("record_prefix") or self.record_prefix)
        self._seq = int(payload.get("seq") or 0)
        tables = payload.get("tables") or {}
        self._tables = {
            str(table): {
                str(record_id): self._clean(fields) for record_id, fields in rows.items()
            }
            for table, rows in tables.items()
        }
        self._calls = []
        self._failures = []

    def clone(self) -> "SyntheticAitableAdapter":
        """复制一个等价的适配器（用于“同一份存储、两个进程”的重启测试）。"""
        other = SyntheticAitableAdapter(record_prefix=self.record_prefix)
        other.import_state(self.export_state())
        return other

    # ---- 内部 -----------------------------------------------------------

    def _next_record_id(self) -> str:
        self._seq += 1
        return f"{self.record_prefix}-{self._seq:06d}"

    @staticmethod
    def _clean(fields: Mapping[str, str]) -> dict[str, str]:
        """列值一律落成原始字符串（契约 §6）。"""
        cleaned: dict[str, str] = {}
        for column, value in fields.items():
            cleaned[str(column)] = "" if value is None else str(value)
        return cleaned


def rows_for_table(adapter: AitableAdapter, table: str) -> tuple[TableRow, ...]:
    """便捷入口：列出一张表（保留给调用方做业务侧筛选）。"""
    return adapter.list_records(table)


def assert_synthetic(adapter: AitableAdapter) -> None:
    """防御性自检：本卡流程只允许接合成/离线适配器。"""
    if not getattr(adapter, "offline", False) or adapter.name != "synthetic":
        raise AssertionError(
            "本卡流程只允许接合成适配器（offline=True, name='synthetic'）；"
            "真实钉钉读写属 L4，由主控在隔离环境执行"
        )


def count_rows(rows: Iterable[TableRow]) -> int:
    return sum(1 for _ in rows)


__all__ = [
    "CREATE",
    "FAIL_KINDS",
    "FAIL_LOSE",
    "FAIL_REJECTED",
    "FAIL_UNKNOWN",
    "GET",
    "LIST",
    "OPS",
    "UPDATE",
    "Call",
    "SyntheticAitableAdapter",
    "assert_synthetic",
    "count_rows",
    "rows_for_table",
]
