"""T06 skill 输入契约：JSON 输入的解析与**前置校验**（issue #19 第 1 段「输入」）。

职责边界（不复制契约规则）：

- 本模块只拦**调用方缺陷**（结构不符 / 必填留痕缺失 / 枚举取值非法 / 时间不可解析 /
  时间精度不足 / shift_id 与班次不符 / shift_date 与驻场期起始日不符）。
- **业务告警 E001–E004 不在这里判**——它们由 :mod:`workflow.intake` 在录入时按冻结契约
  产出（E001 拒收不落表；E002/E003/E004 落表 + 告警），前置校验**不得**把告警行提前拒掉。

厂级口径（主控随卡已定，见 issue #19）：

1. 默认班次名 = ``stay_period``（驻场期）；``early/middle/late/custom`` 保留给历史数据与例外。
2. 清单标题由 :func:`workflow.checklist.checklist_title` 产出（驻场期单 = 「交接班清单 · 驻场期」），
   本模块不另立标题口径。
3. 驻场期边界 = ``start_time`` / ``end_time``，E003 对 ``[start, end]`` 整体区间判定（T04 冻结口径）。
4. ``shift_date`` **取驻场期起始日**（= ``start_time`` 的日期）；调用方显式给出时也只允许等于该日，
   不接受「期内的任意一天」（判重键另有卡处理，在此之前先按此约定）。

离线口径：本模块只读本地 JSON 文件，不联网、不读环境变量/凭据、不写任何文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from contracts.enums import (
    EventCategory,
    EventStatus,
    Severity,
    ShiftName,
    coerce_enum,
    is_blank,
)
from contracts.errors import ContractViolation
from contracts.identity import IdentityRef, as_identity
from contracts.shift import ShiftRecord, new_shift
from contracts.timebase import SHANGHAI, format_minute, parse_occurred_at, parse_time_text

#: skill 标识（用于 result.json 的 ``skill`` 字段）。
SKILL_NAME = "handover-skill"
#: 输入契约版本（``version`` 字段；不匹配即输入非法）。
SKILL_VERSION = 1

#: 口径 1：默认班次名 = 驻场期（``stay_period``）。
DEFAULT_SHIFT_NAME = ShiftName.STAY_PERIOD.value
#: 保留给历史数据与例外的班次名（历史行照旧可读，口径不变）。
HISTORICAL_SHIFT_NAMES: tuple[str, ...] = (
    ShiftName.EARLY.value,
    ShiftName.MIDDLE.value,
    ShiftName.LATE.value,
    ShiftName.CUSTOM.value,
)

#: 关键级强制标准（SPEC §1：第 5 类默认关键级 / 状态填移交接班人自动升级）。
DEFAULT_CRITICAL_STANDARD: tuple[str, ...] = ("重大事项", "移交接班人")

#: 班次块必填字段。
REQUIRED_SHIFT_FIELDS: tuple[str, ...] = (
    "shift_id",
    "handover_line",
    "start_time",
    "end_time",
    "handover_from",
    "handover_to",
)

#: 事件行中被 E004 覆盖的三个关键字段（空值属业务告警，非输入非法）。
EVENT_ENUM_FIELDS: tuple[tuple[str, type], ...] = (
    ("category", EventCategory),
    ("severity", Severity),
    ("status", EventStatus),
)

#: 输入非法时的提示上限（避免一次报几百条）。
_MAX_PROBLEMS_REPORTED = 20


class SkillInputError(ValueError):
    """输入非法（调用方缺陷）：**不落表、不写任何输出文件**。

    ``problems`` 为逐条诊断；``str(exc)`` 为 ``输入非法：...；...`` 形式。
    """

    def __init__(self, problems: str | list[str] | tuple[str, ...]) -> None:
        items = (problems,) if isinstance(problems, str) else tuple(str(item) for item in problems)
        self.problems: tuple[str, ...] = items or ("输入非法（未给出原因）",)
        super().__init__("输入非法：" + "；".join(self.problems))


@dataclass(frozen=True)
class SkillInput:
    """校验通过的 skill 输入（班次 + 事件行 + 确定性时钟）。"""

    shift: ShiftRecord
    events: tuple[Mapping[str, Any], ...]
    now: datetime
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        """输入的脱敏摘要（不含绝对路径；便于留痕与断言）。"""
        return {
            "source": self.source,
            "shift_id": self.shift.shift_id,
            "shift_date": self.shift.shift_date.isoformat(),
            "shift_name": self.shift.shift_name,
            "window_start": format_minute(self.shift.start_time),
            "window_end": format_minute(self.shift.end_time),
            "events": len(self.events),
            "now": format_minute(self.now),
        }


def load_input(path: str | Path) -> SkillInput:
    """读取并校验输入文件（本地 JSON，UTF-8）。"""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillInputError(f"输入文件不可读: {target.name}（{exc.strerror or exc}）") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SkillInputError(
            f"输入不是合法 JSON: {target.name}（第 {exc.lineno} 行第 {exc.colno} 列）"
        ) from exc
    return parse_input(payload, source=target.name)


def parse_input(payload: Any, *, source: str = "") -> SkillInput:
    """解析并前置校验输入；任一项不合法即 :class:`SkillInputError`（一次性报全）。"""
    if not isinstance(payload, Mapping):
        raise SkillInputError(f"输入必须是 JSON 对象，收到 {type(payload).__name__}")

    version = payload.get("version", SKILL_VERSION)
    if version != SKILL_VERSION:
        raise SkillInputError(
            f"不支持的输入版本 {version!r}（本 skill 支持 version={SKILL_VERSION}）"
        )

    problems: list[str] = []
    shift_block = payload.get("shift")
    if not isinstance(shift_block, Mapping):
        raise SkillInputError("输入缺少 shift 对象（班次/驻场期定义）")
    shift = _parse_shift(shift_block, problems)

    events_block = payload.get("events")
    if not isinstance(events_block, (list, tuple)):
        raise SkillInputError("输入缺少 events 数组（交接事件行）")
    events = _parse_events(events_block, shift, problems)

    now_raw = payload.get("now")
    if now_raw is None:
        # 确定性时钟：默认取驻场期结束时刻（测试与重放不依赖墙钟）。
        now = shift.end_time if shift is not None else None
    else:
        now = _parse_moment(now_raw, "now", problems)

    if problems or shift is None or now is None:
        shown = problems[:_MAX_PROBLEMS_REPORTED]
        if len(problems) > _MAX_PROBLEMS_REPORTED:
            shown.append(f"……其余 {len(problems) - _MAX_PROBLEMS_REPORTED} 项略")
        raise SkillInputError(shown or ["输入不完整"])
    return SkillInput(shift=shift, events=events, now=now, source=source)


# ---- 内部：班次 ---------------------------------------------------------


def _parse_shift(block: Mapping[str, Any], problems: list[str]) -> ShiftRecord | None:
    missing = [name for name in REQUIRED_SHIFT_FIELDS if is_blank(block.get(name))]
    if missing:
        problems.append("shift 缺少必填字段: " + ", ".join(missing))
        return None

    start = _parse_moment(block.get("start_time"), "shift.start_time", problems)
    end = _parse_moment(block.get("end_time"), "shift.end_time", problems)

    shift_name = block.get("shift_name", DEFAULT_SHIFT_NAME)
    if is_blank(shift_name):
        shift_name = DEFAULT_SHIFT_NAME
    try:
        shift_name_value = coerce_enum(ShiftName, shift_name, field="shift.shift_name").value
    except ContractViolation as exc:
        problems.append(str(exc))
        shift_name_value = DEFAULT_SHIFT_NAME

    handover_from = _parse_identity(block.get("handover_from"), "shift.handover_from", problems)
    handover_to = _parse_identity(block.get("handover_to"), "shift.handover_to", problems)

    # 口径 4：shift_date 取驻场期起始日；显式给出时只允许等于该日。
    shift_date: date | None = start.date() if start is not None else None
    if "shift_date" in block and not is_blank(block.get("shift_date")):
        given = _parse_date(block.get("shift_date"), "shift.shift_date", problems)
        if given is not None and shift_date is not None and given != shift_date:
            problems.append(
                "shift.shift_date 必须等于驻场期起始日 "
                f"{shift_date.isoformat()}（口径：取 start_time 的日期，勿填期内任意一天）；"
                f"收到 {given.isoformat()}"
            )

    critical_standard = _parse_critical_standard(block.get("critical_standard"), problems)

    if start is None or end is None or shift_date is None:
        return None
    if handover_from is None or handover_to is None:
        return None
    try:
        return new_shift(
            shift_id=str(block.get("shift_id")).strip(),
            handover_line=str(block.get("handover_line")).strip(),
            shift_date=shift_date,
            shift_name=shift_name_value,
            start_time=start,
            end_time=end,
            handover_from=handover_from,
            handover_to=handover_to,
            critical_standard=critical_standard,
        )
    except ContractViolation as exc:
        problems.append(f"shift 不成立：{exc}")
        return None


def _parse_critical_standard(raw: Any, problems: list[str]) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_CRITICAL_STANDARD
    if not isinstance(raw, (list, tuple)):
        problems.append("shift.critical_standard 必须是字符串数组")
        return DEFAULT_CRITICAL_STANDARD
    values: list[str] = []
    for item in raw:
        if is_blank(item):
            problems.append("shift.critical_standard 不能含空值")
            continue
        values.append(str(item).strip())
    return tuple(values)


# ---- 内部：事件行 -------------------------------------------------------


def _parse_events(
    block: Any, shift: ShiftRecord | None, problems: list[str]
) -> tuple[Mapping[str, Any], ...]:
    rows: list[Mapping[str, Any]] = []
    for index, item in enumerate(block):
        where = f"events[{index}]"
        if not isinstance(item, Mapping):
            problems.append(f"{where} 必须是对象，收到 {type(item).__name__}")
            continue
        row = {str(key): value for key, value in item.items()}
        event_id = str(row.get("event_id") or "").strip()
        if not event_id:
            problems.append(f"{where} 缺少 event_id（唯一标识必填）")
        if is_blank(row.get("description")):
            # 契约把"描述必填"列为调用方缺陷（四条告警不覆盖该字段）。
            problems.append(f"{where} 缺少 description（事项描述必填）")
        row_shift = row.get("shift_id")
        if shift is not None:
            if is_blank(row_shift):
                row["shift_id"] = shift.shift_id
            elif str(row_shift).strip() != shift.shift_id:
                problems.append(
                    f"{where}.shift_id 与班次不符：{str(row_shift).strip()!r} ≠ {shift.shift_id!r}"
                )
        if "occurred_at" in row and row.get("occurred_at") is not None:
            try:
                parse_occurred_at(row.get("occurred_at"))
            except ContractViolation as exc:
                problems.append(f"{where} {exc}")
        for name, enum_cls in EVENT_ENUM_FIELDS:
            if is_blank(row.get(name)):
                continue  # 空值 → E004 业务告警（不在这里拒）
            try:
                coerce_enum(enum_cls, row.get(name), field=f"{where}.{name}")
            except ContractViolation as exc:
                problems.append(str(exc))
        if row.get("owner") is not None:
            try:
                as_identity(row.get("owner"))
            except ContractViolation as exc:
                problems.append(f"{where}.owner {exc}")
        rows.append(row)
    return tuple(rows)


# ---- 内部：基础取值 -----------------------------------------------------


def _parse_moment(raw: Any, field: str, problems: list[str]) -> datetime | None:
    try:
        parsed = parse_time_text(raw, field=field)
    except ContractViolation as exc:
        problems.append(str(exc))
        return None
    if not parsed.is_minute_precise or parsed.value is None:
        problems.append(f"{field} 必须精确到分（如 2026-03-02 08:00），收到 {raw!r}")
        return None
    return parsed.value


def _parse_date(raw: Any, field: str, problems: list[str]) -> date | None:
    text = str(raw).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        problems.append(f"{field} 必须是 YYYY-MM-DD，收到 {raw!r}")
        return None


def _parse_identity(raw: Any, field: str, problems: list[str]) -> IdentityRef | None:
    try:
        return as_identity(raw)
    except ContractViolation as exc:
        problems.append(f"{field} {exc}")
        return None


__all__ = [
    "DEFAULT_CRITICAL_STANDARD",
    "DEFAULT_SHIFT_NAME",
    "EVENT_ENUM_FIELDS",
    "HISTORICAL_SHIFT_NAMES",
    "REQUIRED_SHIFT_FIELDS",
    "SHANGHAI",
    "SKILL_NAME",
    "SKILL_VERSION",
    "SkillInput",
    "SkillInputError",
    "load_input",
    "parse_input",
]
