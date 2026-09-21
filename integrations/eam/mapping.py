"""T05 集中映射表：EAM 原始值 → 九类统一字段（issue #23 第 2 条）。

口径：

- ``category`` 固定 ``defect`` / ``hazard``——由**来源类别**决定，不解析 EAM 文本。
- ``ref_no`` = EAM 编号；``occurred_at`` = 发现时间（精确到分，原始值原样交给
  契约层解析）；``shift_id`` 取自**入参班次**（不是 EAM 字段）。
- ``severity`` / ``status`` **只走本模块的常量表**：L4 用真实 EAM 数据校准时
  **只改这一处**（新增/删除键即可），业务代码不用动。
- **不猜**：表里查不到（去空白、再按大写查一次仍查不到）的取值一律留空 ``None``，
  由上层触发 E004（关键字段缺失告警），并把**原始值原样留痕**
  （事件 ``note`` + 拉取报告条目的 ``reason``）。空值同样留空并触发 E004。
- ``event_id`` 由 ``shift_id`` + 来源类别 + EAM 编号**确定性派生**：
  不含时间戳、不含随机数、不含序号——同驻场期重复拉取必然得到同一个 ID，
  跨驻场期（不同 ``shift_id``）则是不同 ID（未消缺缺陷在下个驻场期重新提出）。

初始映射口径（**待 L4 用真实 EAM 数据校准**）：

=========  ==========================  ============
来源        原始取值                     统一字段
=========  ==========================  ============
缺陷等级    危急 / 严重                   critical
缺陷等级    一般                          normal
隐患等级    A / B（含 “A级”写法）          critical
隐患等级    C（含 “C级”写法）              normal
状态        待处理 / 处理中                in_progress
状态        已消缺 / 已验收                done
状态        已移交                        transferred
=========  ==========================  ============

表里另含**规范值同形键**（``critical`` / ``normal`` / ``in_progress`` / ``done`` /
``transferred``）：上游已归一的数据原样通过，避免二次拉取被误判为“未知”。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from contracts.enums import EventCategory, EventStatus, Severity
from contracts.errors import ContractViolation
from contracts.timebase import parse_occurred_at

from .adapter import (
    KIND_DEFECT,
    KIND_HAZARD,
    KIND_LABELS,
    KINDS,
    EamRecord,
)

#: 映射表版本（改表即改版本号，便于 L4 校准后核对）。
MAPPING_TABLES_VERSION = "t05.1"

#: 来源类别 → 九类 ``category``（固定取值，不解析文本）。
KIND_TO_CATEGORY: Mapping[str, str] = {
    KIND_DEFECT: EventCategory.DEFECT.value,
    KIND_HAZARD: EventCategory.HAZARD.value,
}

#: 缺陷等级映射（L4 校准点 ①）。
DEFECT_SEVERITY_MAP: Mapping[str, str] = {
    "危急": Severity.CRITICAL.value,
    "严重": Severity.CRITICAL.value,
    "一般": Severity.NORMAL.value,
    # 上游已归一：规范值原样通过（显式列出，不算猜）。
    Severity.CRITICAL.value: Severity.CRITICAL.value,
    Severity.NORMAL.value: Severity.NORMAL.value,
}

#: 隐患等级映射（L4 校准点 ②）：A/B → critical，C → normal。
HAZARD_SEVERITY_MAP: Mapping[str, str] = {
    "A": Severity.CRITICAL.value,
    "B": Severity.CRITICAL.value,
    "C": Severity.NORMAL.value,
    # 台账里常见“带级字”的写法，显式列出（未列出的写法一律按未知处理）。
    "A级": Severity.CRITICAL.value,
    "B级": Severity.CRITICAL.value,
    "C级": Severity.NORMAL.value,
    Severity.CRITICAL.value: Severity.CRITICAL.value,
    Severity.NORMAL.value: Severity.NORMAL.value,
}

#: 状态映射（L4 校准点 ③）。
STATUS_MAP: Mapping[str, str] = {
    "待处理": EventStatus.IN_PROGRESS.value,
    "处理中": EventStatus.IN_PROGRESS.value,
    "已消缺": EventStatus.DONE.value,
    "已验收": EventStatus.DONE.value,
    "已移交": EventStatus.TRANSFERRED.value,
    EventStatus.IN_PROGRESS.value: EventStatus.IN_PROGRESS.value,
    EventStatus.DONE.value: EventStatus.DONE.value,
    EventStatus.TRANSFERRED.value: EventStatus.TRANSFERRED.value,
}

#: 来源类别 → 等级映射表。
SEVERITY_MAP_BY_KIND: Mapping[str, Mapping[str, str]] = {
    KIND_DEFECT: DEFECT_SEVERITY_MAP,
    KIND_HAZARD: HAZARD_SEVERITY_MAP,
}

#: 映射表清单（供 L4 校准与报告引用；不含任何真实表 ID / 凭据）。
MAPPING_TABLES: Mapping[str, Mapping[str, str]] = {
    "defect_severity": DEFECT_SEVERITY_MAP,
    "hazard_severity": HAZARD_SEVERITY_MAP,
    "status": STATUS_MAP,
}

#: 字段中文标签（留痕文案用）。
FIELD_LABELS: Mapping[str, str] = {"severity": "等级", "status": "状态"}

#: 事件编号前缀（派生用，不含时间/随机成分）。
EVENT_ID_PREFIX = "eam"
EVENT_ID_SEP = "-"

#: 未产出候选行的原因（本地数据缺陷，必须在报告里显式报出，不得静默丢）。
REJECT_MISSING_REF = "missing_ref_no"
REJECT_BAD_TIME = "unparsable_discovered_at"

#: 描述缺省模板（EAM 未给描述时用的确定性文案，不猜业务内容）。
DESCRIPTION_FALLBACK = "{label}（编号 {ref_no}）：EAM 未提供描述"


def mapping_tables_doc() -> dict[str, Any]:
    """映射表快照（文档/校准用；纯常量，无外部数据）。"""
    return {
        "version": MAPPING_TABLES_VERSION,
        "kind_to_category": dict(KIND_TO_CATEGORY),
        "tables": {name: dict(table) for name, table in MAPPING_TABLES.items()},
        "unknown_policy": "未知取值不猜：留空 + 触发 E004 + 原始值留痕；映射表待 L4 校准",
    }


@dataclass(frozen=True)
class MappedValue:
    """一个字段的映射结果（含原始值留痕）。"""

    field: str
    raw: str
    value: str | None
    known: bool
    reason: str = ""

    @property
    def field_label(self) -> str:
        return FIELD_LABELS.get(self.field, self.field)

    @property
    def token(self) -> str:
        """留痕短语（拼进事件 ``note``）。"""
        if self.raw:
            return f"EAM原始{self.field_label}「{self.raw}」未识别，不猜（E004 待补）"
        return f"EAM原始{self.field_label}为空（E004 待补）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "raw": self.raw,
            "value": self.value,
            "known": self.known,
            "reason": self.reason,
        }


def _text(raw: Any) -> str:
    return "" if raw is None else str(raw).strip()


def _lookup(table: Mapping[str, str], text: str) -> str | None:
    """查表：先去空白，再按大写查一次；查不到返回 ``None``（不猜）。"""
    if not text:
        return None
    for key in (text, text.upper()):
        if key in table:
            return table[key]
    return None


def _mapped(field_name: str, raw: str, table: Mapping[str, str]) -> MappedValue:
    value = _lookup(table, raw)
    if value is not None:
        return MappedValue(field=field_name, raw=raw, value=value, known=True)
    label = FIELD_LABELS.get(field_name, field_name)
    if raw:
        reason = (
            f"{label}原始值「{raw}」未识别：不猜（留空 + E004，原始值已留痕；"
            "映射表待 L4 校准）"
        )
    else:
        reason = f"{label}原始值为空：不猜（留空 + E004，待人工补）"
    return MappedValue(field=field_name, raw=raw, value=None, known=False, reason=reason)


def map_severity(kind: str, raw: Any) -> MappedValue:
    """EAM 等级 → 统一 ``severity``（未知/空 → 留空，不猜）。"""
    table = SEVERITY_MAP_BY_KIND.get(str(kind or "").strip())
    if table is None:
        raise ContractViolation(f"未知来源类别 {kind!r}（可用: {', '.join(KINDS)}）")
    return _mapped("severity", _text(raw), table)


def map_status(raw: Any) -> MappedValue:
    """EAM 状态 → 统一 ``status``（未知/空 → 留空，不猜）。"""
    return _mapped("status", _text(raw), STATUS_MAP)


def derive_event_id(*, shift_id: str, kind: str, ref_no: str) -> str:
    """确定性派生 ``event_id``：``shift_id`` + 来源类别 + EAM 编号。

    同一组入参**永远**得到同一个字符串（无时间戳、无随机、无自增序号）；
    因此同驻场期重复拉取必然命中同一 ID（幂等跳过），跨驻场期重新提出时
    ``shift_id`` 不同 → ID 不同 → 如实新增一行。
    """
    shift_text = _text(shift_id)
    ref_text = _text(ref_no)
    kind_text = _text(kind)
    if not shift_text:
        raise ContractViolation("event_id 派生缺少 shift_id（入参班次必填）")
    if kind_text not in KINDS:
        raise ContractViolation(f"event_id 派生缺少来源类别（可用: {', '.join(KINDS)}）")
    if not ref_text:
        raise ContractViolation("event_id 派生缺少 EAM 编号（ref_no）；缺编号不猜 ID")
    return EVENT_ID_SEP.join((EVENT_ID_PREFIX, kind_text, shift_text, ref_text))


def trace_note(mapped: tuple[MappedValue, ...]) -> str:
    """把未识别字段的原始值拼成留痕短语（仅未识别的进入留痕）。"""
    return "；".join(item.token for item in mapped if not item.known)


def describe_record(record: EamRecord) -> str:
    """候选行描述：EAM 描述原样用；缺失时用确定性兜底文案（不猜业务内容）。"""
    title = _text(record.title)
    if title:
        return title
    return DESCRIPTION_FALLBACK.format(label=KIND_LABELS[record.kind], ref_no=record.ref_no)


@dataclass(frozen=True)
class CandidateOutcome:
    """一条 EAM 记录 → 交接事件候选行的结果。"""

    kind: str
    ref_no: str
    event_id: str = ""
    row: Mapping[str, Any] | None = None
    unmapped: tuple[MappedValue, ...] = ()
    reject_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.row is not None

    @property
    def unmapped_fields(self) -> tuple[str, ...]:
        return tuple(item.field for item in self.unmapped)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref_no": self.ref_no,
            "event_id": self.event_id,
            "mapped": self.ok,
            "unmapped": [item.to_dict() for item in self.unmapped],
            "reject_reason": self.reject_reason,
        }


def build_candidate(record: EamRecord, *, shift: Any) -> CandidateOutcome:
    """EAM 记录 → 交接事件候选行（**只映射，不落表**）。

    产出的是一个可直接交给阶段① ``EventStore.intake`` / ``ShiftIntakeService.submit``
    的原始行（枚举列给规范值或空串，时间给原始值），四条告警规则由既有链路负责，
    本函数不代判、不补值。

    不产出候选行（``reject_reason`` 非空）的两种本地数据缺陷——**必须在报告里显式报出**：

    - ``missing_ref_no``：EAM 编号为空 → 无法派生确定性 ``event_id``（不猜 ID）；
    - ``unparsable_discovered_at``：发现时间无法被契约口径解析（不猜时间）。
    """
    ref_no = _text(record.ref_no)
    if not ref_no:
        return CandidateOutcome(
            kind=record.kind,
            ref_no="",
            reject_reason=(
                f"{REJECT_MISSING_REF}: EAM 编号为空，无法派生确定性 event_id（不猜，需人工补编号）"
            ),
        )
    event_id = derive_event_id(shift_id=shift.shift_id, kind=record.kind, ref_no=ref_no)
    try:
        parse_occurred_at(record.discovered_at)
    except ContractViolation as exc:
        return CandidateOutcome(
            kind=record.kind,
            ref_no=ref_no,
            event_id=event_id,
            reject_reason=f"{REJECT_BAD_TIME}: 发现时间无法解析（{exc}）",
        )
    severity = map_severity(record.kind, record.severity)
    status = map_status(record.status)
    unmapped = tuple(item for item in (severity, status) if not item.known)
    note_parts = [part for part in (_text(record.note), trace_note(unmapped)) if part]
    row: dict[str, Any] = {
        "event_id": event_id,
        "shift_id": shift.shift_id,
        "category": KIND_TO_CATEGORY[record.kind],
        "description": describe_record(record),
        "occurred_at": record.discovered_at,
        "severity": severity.value,
        "status": status.value,
        "owner": record.owner,
        "ref_no": ref_no,
        "note": "；".join(note_parts),
    }
    return CandidateOutcome(
        kind=record.kind,
        ref_no=ref_no,
        event_id=event_id,
        row=row,
        unmapped=unmapped,
    )


__all__ = [
    "DEFECT_SEVERITY_MAP",
    "DESCRIPTION_FALLBACK",
    "EVENT_ID_PREFIX",
    "EVENT_ID_SEP",
    "FIELD_LABELS",
    "HAZARD_SEVERITY_MAP",
    "KIND_TO_CATEGORY",
    "MAPPING_TABLES",
    "MAPPING_TABLES_VERSION",
    "REJECT_BAD_TIME",
    "REJECT_MISSING_REF",
    "SEVERITY_MAP_BY_KIND",
    "STATUS_MAP",
    "CandidateOutcome",
    "MappedValue",
    "build_candidate",
    "derive_event_id",
    "describe_record",
    "map_severity",
    "map_status",
    "mapping_tables_doc",
    "trace_note",
]
