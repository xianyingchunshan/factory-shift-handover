"""T05 合成 EAM 适配器：内存夹具，**不发任何真实请求**（issue #23 第 1/5 条）。

用途与保证：

- 拉取只读内存里的合成记录（``SYNTH-`` 前缀显式标注为合成数据，AGENTS.md）；
  没有 socket / HTTP 客户端 / 凭据，:mod:`integrations.aitable.isolation` 的静态审计
  覆盖本包（``integrations`` 属待审计包），测试另断言“未引入网络模块”。
- 窗口过滤复用 :func:`integrations.eam.adapter.record_in_window`（= ``contracts/timebase``
  口径），**不留第二套窗口语义**；发现时间缺失/无法解析的记录原样返回，不静默丢弃。
- ``assert_synthetic_eam`` 供上游入口自检：误接真实适配器直接失败。
- 留痕：``calls`` 记录每次拉取的方法名与窗口，作为“本卡只做只读拉取”的 L2 证据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from contracts.errors import ContractViolation

from ..aitable.adapter import SYNTH_PREFIX
from .adapter import (
    KIND_DEFECT,
    KIND_HAZARD,
    EamReadOnlyAdapter,
    EamRecord,
    EamWindow,
    record_in_window,
)


@dataclass(frozen=True)
class ReadCall:
    """一次只读拉取的留痕（不含凭据；窗口是合成值）。"""

    method: str
    shift_id: str
    window: str

    def to_dict(self) -> dict[str, str]:
        return {"method": self.method, "shift_id": self.shift_id, "window": self.window}


class SyntheticEamAdapter(EamReadOnlyAdapter):
    """内存合成 EAM 适配器（离线、确定性、可回读拉取留痕）。"""

    name = "synthetic"
    offline = True

    def __init__(
        self,
        *,
        defects: Iterable[EamRecord] = (),
        hazards: Iterable[EamRecord] = (),
    ) -> None:
        self._defects = self._check(KIND_DEFECT, defects)
        self._hazards = self._check(KIND_HAZARD, hazards)
        self._calls: list[ReadCall] = []

    # ---- 只读拉取 -------------------------------------------------------

    def list_defects(self, window: EamWindow) -> tuple[EamRecord, ...]:
        return self._select("list_defects", self._defects, window)

    def list_hazards(self, window: EamWindow) -> tuple[EamRecord, ...]:
        return self._select("list_hazards", self._hazards, window)

    # ---- 留痕与回读 -----------------------------------------------------

    @property
    def calls(self) -> tuple[ReadCall, ...]:
        return tuple(self._calls)

    def calls_for(self, method: str) -> tuple[ReadCall, ...]:
        return tuple(call for call in self._calls if call.method == method)

    def fixture_count(self, kind: str | None = None) -> int:
        """夹具条数（回读用；不含窗口过滤）。"""
        if kind is None:
            return len(self._defects) + len(self._hazards)
        return len(self._fixtures(kind))

    # ---- 内部 -----------------------------------------------------------

    def _fixtures(self, kind: str) -> tuple[EamRecord, ...]:
        if kind == KIND_DEFECT:
            return self._defects
        if kind == KIND_HAZARD:
            return self._hazards
        raise ContractViolation(f"未知来源类别 {kind!r}")

    def _select(
        self, method: str, fixtures: tuple[EamRecord, ...], window: EamWindow
    ) -> tuple[EamRecord, ...]:
        if not isinstance(window, EamWindow):
            raise ContractViolation(
                f"拉取入参必须是 EamWindow（驻场期窗口），收到 {type(window).__name__}"
            )
        self._calls.append(
            ReadCall(method=method, shift_id=window.shift_id, window=window.describe())
        )
        return tuple(record for record in fixtures if record_in_window(record, window))

    @staticmethod
    def _check(kind: str, records: Iterable[EamRecord]) -> tuple[EamRecord, ...]:
        items = tuple(records)
        for record in items:
            if not isinstance(record, EamRecord):
                raise ContractViolation(
                    f"EAM 夹具必须是 EamRecord，收到 {type(record).__name__}"
                )
            if record.kind != kind:
                raise ContractViolation(
                    f"夹具类别不符：{kind} 列表里出现 {record.kind}（编号 {record.ref_no or '空'}）"
                )
        return items


def assert_synthetic_eam(adapter: Any) -> None:
    """防御性自检：本卡流程只允许接合成/离线 EAM 适配器。

    仿 ``integrations.aitable.synthetic.assert_synthetic`` 的模式：误接真实适配器
    （``offline=False`` 或 ``name != 'synthetic'``）立即失败，不进入拉取。
    """
    if not getattr(adapter, "offline", False) or getattr(adapter, "name", "") != "synthetic":
        raise AssertionError(
            "本卡流程只允许接合成 EAM 适配器（offline=True, name='synthetic'）；"
            "真实 EAM 读取属 L4，由主控在隔离环境执行"
        )


def empty_adapter() -> SyntheticEamAdapter:
    """空夹具适配器（无记录）。"""
    return SyntheticEamAdapter()


def synth_ref(seq: int, kind: str = KIND_DEFECT) -> str:
    """合成编号（``SYNTH-`` 前缀；编号本身也是虚构值）。"""
    return f"{SYNTH_PREFIX}EAM-{kind.upper()}-{seq:04d}"


__all__ = [
    "ReadCall",
    "SyntheticEamAdapter",
    "assert_synthetic_eam",
    "empty_adapter",
    "synth_ref",
]
