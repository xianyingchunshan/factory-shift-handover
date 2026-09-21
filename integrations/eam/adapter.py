"""T05 只读适配器协议：EAM 缺陷登记 / 隐患台账（issue #23 第 1 条）。

口径（SPEC §9：自动化只做只读拉取，不写外部系统）：

- **只读**：协议只声明两个拉取方法 ``list_defects`` / ``list_hazards``，入参 =
  驻场期窗口；协议内**没有任何写方法**，也不持有 base_url / 账号 / 表 ID 之类的
  运行时配置（真实连接属 L4，由主控在隔离环境执行）。
- **窗口复用 T04 冻结口径**：判定走 :mod:`contracts.timebase`——对 ``[start, end]``
  **整体区间**做，跨零点相连日历日**均属期内**，**不另立一套窗口语义**。
- **不得静默丢弃**：窗口过滤只按“可解析的发现时间”判定；发现时间缺失或无法解析的
  记录必须**原样返回**，由上层显式拒收 / 告警（E001 等），不许在适配器里悄悄滤掉。
- 仓库内只提供合成实现 :class:`~integrations.eam.synthetic.SyntheticEamAdapter`；
  合成数据一律 ``SYNTH-`` 前缀（AGENTS.md）。

本模块只依赖标准库与冻结契约，不联网。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from contracts.errors import ContractViolation
from contracts.timebase import (
    PRECISION_DATE_ONLY,
    calendar_days_in_window,
    covers_calendar_day,
    ensure_aware,
    format_minute,
    parse_occurred_at,
    truncate_to_minute,
)

from ..aitable.adapter import SYNTH_PREFIX

#: 来源类别（= 交接事件的 ``category``；由来源决定，不来自 EAM 文本）。
KIND_DEFECT = "defect"
KIND_HAZARD = "hazard"
KINDS: tuple[str, ...] = (KIND_DEFECT, KIND_HAZARD)

#: 中文标签（清单/报告渲染用）。
KIND_LABELS: Mapping[str, str] = {KIND_DEFECT: "缺陷", KIND_HAZARD: "隐患"}

#: 协议内允许出现的方法名（只读白名单；测试用它钉死“协议无写方法”）。
READ_METHODS: tuple[str, ...] = ("list_defects", "list_hazards")

#: 写方法的命名特征（协议与合成实现都不允许出现；测试逐名扫描）。
WRITE_METHOD_TOKENS: tuple[str, ...] = (
    "create",
    "write",
    "update",
    "delete",
    "remove",
    "insert",
    "save",
    "submit",
    "post",
    "put",
    "patch",
    "close",
    "assign",
)

#: 只读口径说明（写进 PR / 报告，便于审查者逐条比对）。
READ_ONLY_NOTE = (
    "EAM 适配器只读：协议仅 list_defects / list_hazards 两个拉取方法，"
    "没有任何写方法；真实连接与写入属 L4，由主控执行，本仓不实现"
)

#: 只读审计辅助函数名（它们的**名字**里含写方法特征词，但只做扫描、不做写操作；
#: 测试断言“模块内没有写方法型函数”时按本清单豁免）。
READ_ONLY_AUDIT_HELPERS: tuple[str, ...] = ("write_like_method_names",)

#: 窗口过滤口径说明（与 T04 冻结口径同源）。
WINDOW_SCOPE_NOTE = (
    "窗口 = 驻场期区间（contracts/timebase 口径）；适配器按**日历日**下推检索"
    "（真实 EAM 按日期区间查），分钟级越界判定不在适配器做——由入表时既有的 E003"
    "按整体区间判定（跨零点相连日历日均属期内）；发现时间缺失/无法解析的记录原样返回，"
    "由上层显式拒收（E001），不静默丢弃"
)


class EamError(RuntimeError):
    """EAM 适配器层错误的基类（不是契约错误码）。"""


class EamReadError(EamError):
    """拉取失败：调用方按“只读拉取未生效”处置，不重放、不伪造数据。"""


@dataclass(frozen=True)
class EamWindow:
    """一次只读拉取的窗口（= 驻场期边界）。

    构造只接受带时区的 :class:`datetime`（naive 直接拒绝，不猜），
    与 :mod:`contracts.timebase` 同一口径；``from_shift`` 是业务侧入口。
    """

    shift_id: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not str(self.shift_id or "").strip():
            raise ContractViolation("窗口必须绑定 shift_id（窗口 = 驻场期）")
        object.__setattr__(self, "shift_id", str(self.shift_id).strip())
        object.__setattr__(
            self, "start", truncate_to_minute(ensure_aware(self.start, field="window.start"))
        )
        object.__setattr__(
            self, "end", truncate_to_minute(ensure_aware(self.end, field="window.end"))
        )
        if self.end <= self.start:
            raise ContractViolation("窗口 end 必须晚于 start")

    @classmethod
    def from_shift(cls, shift: Any) -> "EamWindow":
        """按驻场期班次构造窗口（``start_time`` / ``end_time`` 即驻场期边界）。"""
        return cls(shift_id=shift.shift_id, start=shift.start_time, end=shift.end_time)

    def covers_date(self, day: date) -> bool:
        """日历日是否属期内（与驻场期 ``[start, end]`` 有交集即属期内）。"""
        return covers_calendar_day(day, self.start, self.end)

    def calendar_days(self) -> tuple[date, ...]:
        """期内全部相连日历日。"""
        return calendar_days_in_window(self.start, self.end)

    def describe(self) -> str:
        return f"{format_minute(self.start)}~{format_minute(self.end)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "start": format_minute(self.start),
            "end": format_minute(self.end),
            "calendar_days": len(self.calendar_days()),
        }


@dataclass(frozen=True)
class EamRecord:
    """一条 EAM 原始记录（缺陷登记或隐患台账），**只读快照**，字段全是原始值。

    - ``ref_no`` = EAM 缺陷编号 / 隐患台账编号（``event_id`` 的派生依据之一）；
    - ``discovered_at`` = 发现时间原始值（精确到分；可能只有日期、缺失或无法解析，
      由上层按四条告警规则判定，**适配器不代判、不补值**）；
    - ``severity`` / ``status`` = 等级 / 状态**原始取值**（不认识的取值一律留给
      映射层按“不猜”处理，见 :mod:`integrations.eam.mapping`）。
    """

    kind: str
    ref_no: str
    title: str = ""
    discovered_at: Any = None
    severity: str = ""
    status: str = ""
    owner: Any = None
    note: str = ""

    def __post_init__(self) -> None:
        kind_text = str(self.kind or "").strip()
        if kind_text not in KINDS:
            raise ContractViolation(
                f"未知来源类别 {self.kind!r}（可用: {', '.join(KINDS)}）"
            )
        object.__setattr__(self, "kind", kind_text)
        object.__setattr__(self, "ref_no", str(self.ref_no or "").strip())
        object.__setattr__(self, "title", str(self.title or "").strip())
        object.__setattr__(self, "severity", str(self.severity or "").strip())
        object.__setattr__(self, "status", str(self.status or "").strip())
        object.__setattr__(self, "note", str(self.note or "").strip())
        raw_time = self.discovered_at
        if isinstance(raw_time, str):
            raw_time = raw_time.strip()
        object.__setattr__(self, "discovered_at", raw_time)

    @property
    def label(self) -> str:
        return KIND_LABELS[self.kind]

    def source_ref(self, index: int | None = None) -> str:
        """来源编号（报告用）：缺编号时退化为 ``@序号``，保证“拒收也带得出处”。"""
        if self.ref_no:
            return self.ref_no
        return f"@{index}" if index is not None else "@?"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "kind_label": self.label,
            "ref_no": self.ref_no,
            "title": self.title,
            "discovered_at": "" if self.discovered_at is None else str(self.discovered_at),
            "severity": self.severity,
            "status": self.status,
        }


def defect(
    ref_no: str,
    title: str = "",
    discovered_at: Any = None,
    *,
    severity: str = "",
    status: str = "",
    owner: Any = None,
    note: str = "",
) -> EamRecord:
    """构造一条缺陷登记记录（``category=defect``）。"""
    return EamRecord(
        kind=KIND_DEFECT,
        ref_no=ref_no,
        title=title,
        discovered_at=discovered_at,
        severity=severity,
        status=status,
        owner=owner,
        note=note,
    )


def hazard(
    ref_no: str,
    title: str = "",
    discovered_at: Any = None,
    *,
    severity: str = "",
    status: str = "",
    owner: Any = None,
    note: str = "",
) -> EamRecord:
    """构造一条隐患台账记录（``category=hazard``）。"""
    return EamRecord(
        kind=KIND_HAZARD,
        ref_no=ref_no,
        title=title,
        discovered_at=discovered_at,
        severity=severity,
        status=status,
        owner=owner,
        note=note,
    )


class EamReadOnlyAdapter(ABC):
    """EAM 只读拉取抽象口（**协议内无任何写方法**）。

    ``name`` / ``offline`` 用于入口自检：合成实现固定 ``synthetic`` / ``True``，
    误接真实适配器即失败（:func:`integrations.eam.synthetic.assert_synthetic_eam`）。
    """

    name = "abstract"

    #: 该适配器是否完全离线（真实实现应为 False，仓库内实现必须为 True）。
    offline = True

    @abstractmethod
    def list_defects(self, window: EamWindow) -> tuple[EamRecord, ...]:
        """拉取窗口内的缺陷登记记录（**只读**；发现时间缺失的记录也要返回）。"""

    @abstractmethod
    def list_hazards(self, window: EamWindow) -> tuple[EamRecord, ...]:
        """拉取窗口内的隐患台账记录（**只读**；发现时间缺失的记录也要返回）。"""


def record_in_window(record: EamRecord, window: EamWindow) -> bool:
    """该记录是否属本次拉取窗口（窗口过滤口径的**唯一**实现，真实适配器复用同一判定）。

    粒度 = **日历日**（真实 EAM 按日期区间下推检索）：

    - 发现时间精确到分 → 取其日历日，按 ``covers_calendar_day`` 判定（驻场期内
      跨零点相连日历日均属期内）——分钟级越界**不在适配器判定**，
      由入表时既有的 E003 按驻场期整体区间判定；
    - 只有日期 → 同样按日历日判定；
    - 缺失或无法解析 → **视为属窗口**（原样返回，交上层显式拒收/告警），
      绝不静默丢弃。
    """
    try:
        parsed = parse_occurred_at(record.discovered_at)
    except ContractViolation:
        return True
    if parsed.value is None:
        if parsed.precision == PRECISION_DATE_ONLY:
            return window.covers_date(date.fromisoformat(parsed.raw))
        return True
    return window.covers_date(parsed.value.date())


def read_only_method_names() -> tuple[str, ...]:
    """协议上声明的公开方法名（测试断言只含 :data:`READ_METHODS`）。"""
    return tuple(
        name
        for name in vars(EamReadOnlyAdapter)
        if not name.startswith("_") and callable(getattr(EamReadOnlyAdapter, name, None))
    )


def write_like_method_names(cls: type = EamReadOnlyAdapter) -> tuple[str, ...]:
    """扫描类里带写方法命名特征的属性名（应当恒为空）。"""
    found: list[str] = []
    for name in dir(cls):
        if name.startswith("_"):
            continue
        lowered = name.lower()
        if any(token in lowered for token in WRITE_METHOD_TOKENS):
            found.append(name)
    return tuple(sorted(found))


def count_records(groups: Iterable[Iterable[EamRecord]]) -> int:
    """拉取条数（报告用）。"""
    return sum(1 for group in groups for _ in group)


__all__ = [
    "KIND_DEFECT",
    "KIND_HAZARD",
    "KIND_LABELS",
    "KINDS",
    "READ_METHODS",
    "READ_ONLY_AUDIT_HELPERS",
    "READ_ONLY_NOTE",
    "SYNTH_PREFIX",
    "WINDOW_SCOPE_NOTE",
    "WRITE_METHOD_TOKENS",
    "EamError",
    "EamReadError",
    "EamReadOnlyAdapter",
    "EamRecord",
    "EamWindow",
    "count_records",
    "defect",
    "hazard",
    "read_only_method_names",
    "record_in_window",
    "write_like_method_names",
]
