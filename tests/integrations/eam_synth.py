"""合成测试数据工厂（integrations/eam 测试用；非测试模块，不被 discover 收集）。

**所有数据均为虚构**：编号/描述/人员/交接线一律 ``SYNTH-`` 前缀，不含真实姓名、
真实群/表 ID、真实业务数据，也不含个人绝对路径（AGENTS.md）。时间用 Asia/Shanghai
固定偏移，不依赖真实当前时间，也不发任何网络请求。

夹具覆盖四类边界：

- **正常**：等级/状态都在集中映射表内；
- **未知**：等级/状态不在表里（不猜 → 留空 + E004 + 原始值留痕）；
- **时间**：缺失（E001 拒收）、只有日期（E002）、同日历日但晚于窗口结束（E003）；
- **不可映射**：编号为空、发现时间写法无法解析（显式报出，不静默丢）。
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from contracts.identity import IdentityRef  # noqa: E402
from contracts.shift import ShiftRecord, new_shift  # noqa: E402
from contracts.timebase import SHANGHAI  # noqa: E402

from integrations.eam.adapter import (  # noqa: E402
    KIND_DEFECT,
    KIND_HAZARD,
    EamRecord,
    EamWindow,
    defect,
    hazard,
)
from integrations.eam.synthetic import SyntheticEamAdapter, synth_ref  # noqa: E402

#: 驻场期（T04 厂级口径）：一次轮换一张单，30 天、跨月、跨零点相连日。
SHIFT_ID = "SYNTH-SHIFT-STAY-0001"
LINE = "SYNTH-LINE-A"
FIRST_DAY = date(2026, 3, 2)
LAST_DAY = date(2026, 4, 1)
START = datetime(2026, 3, 2, 8, 0, tzinfo=SHANGHAI)
END = datetime(2026, 4, 1, 8, 0, tzinfo=SHANGHAI)

#: 下一个驻场期（窗口不重叠：端点相接不算重叠，T07 G2 口径）。
NEXT_SHIFT_ID = "SYNTH-SHIFT-STAY-0002"
NEXT_FIRST_DAY = date(2026, 4, 2)
NEXT_START = datetime(2026, 4, 2, 8, 0, tzinfo=SHANGHAI)
NEXT_END = datetime(2026, 5, 1, 8, 0, tzinfo=SHANGHAI)

FROM = IdentityRef(source="dingtalk", user_id="SYNTH-uid-from", display_name="SYNTH-交班人A")
TO = IdentityRef(source="eam", user_id="SYNTH-uid-to", display_name="SYNTH-接班人B")

#: 合成编号（编号本身也是虚构值）。
D1 = synth_ref(1, KIND_DEFECT)
D2 = synth_ref(2, KIND_DEFECT)
D3 = synth_ref(3, KIND_DEFECT)
D4 = synth_ref(4, KIND_DEFECT)
D5 = synth_ref(5, KIND_DEFECT)
H1 = synth_ref(1, KIND_HAZARD)
H2 = synth_ref(2, KIND_HAZARD)
H3 = synth_ref(3, KIND_HAZARD)
H4 = synth_ref(4, KIND_HAZARD)

#: 跨驻场期重新提出的未消缺缺陷（两个驻场期各一条，同一编号）。
OPEN_REF = synth_ref(90, KIND_DEFECT)
OUT_OF_DAY_RANGE = synth_ref(21, KIND_DEFECT)
LAST_DAY_LATE = synth_ref(20, KIND_DEFECT)
NO_REF = ""
BAD_TIME_REF = synth_ref(31, KIND_DEFECT)


def make_shift(**overrides: Any) -> ShiftRecord:
    """默认一个合规的驻场期班次。"""
    params: dict[str, Any] = dict(
        shift_id=SHIFT_ID,
        handover_line=LINE,
        shift_date=FIRST_DAY,
        shift_name="stay_period",
        start_time=START,
        end_time=END,
        handover_from=FROM,
        handover_to=TO,
        critical_standard=("重大事项", "移交接班人"),
    )
    params.update(overrides)
    return new_shift(**params)


def make_next_shift(**overrides: Any) -> ShiftRecord:
    """下一个驻场期班次（窗口与默认驻场期不重叠）。"""
    params: dict[str, Any] = dict(
        shift_id=NEXT_SHIFT_ID,
        handover_line=LINE,
        shift_date=NEXT_FIRST_DAY,
        shift_name="stay_period",
        start_time=NEXT_START,
        end_time=NEXT_END,
        handover_from=FROM,
        handover_to=TO,
        critical_standard=("重大事项", "移交接班人"),
    )
    params.update(overrides)
    return new_shift(**params)


def make_window(**overrides: Any) -> EamWindow:
    """默认窗口 = 默认驻场期边界。"""
    return EamWindow.from_shift(make_shift(**overrides))


def defects() -> tuple[EamRecord, ...]:
    """五条缺陷：2 条正常、1 条等级+状态未知、1 条缺发现时间、1 条只有日期。"""
    return (
        defect(D1, "SYNTH 风机齿轮箱温度偏高", "2026-03-05 09:30", severity="危急", status="处理中"),
        defect(D2, "SYNTH 箱变渗油", "2026-03-20 14:05", severity="一般", status="已消缺"),
        defect(D3, "SYNTH 逆变器通讯中断", "2026-03-11 08:12", severity="特急", status="已关闭"),
        defect(D4, "SYNTH 集电线路避雷器异常", None, severity="严重", status="待处理"),
        defect(D5, "SYNTH SVG 模块告警", "2026-03-10", severity="一般", status="处理中"),
    )


def hazards() -> tuple[EamRecord, ...]:
    """四条隐患：A/B/C 级各覆盖；含首日跨零点时刻（期内）。"""
    return (
        hazard(H1, "SYNTH 升压站消防通道占用", "2026-03-08 10:15", severity="A级", status="处理中"),
        hazard(H2, "SYNTH 备品备件库房堆放不规范", "2026-03-25 16:40", severity="C级", status="已验收"),
        hazard(H3, "SYNTH 箱变围栏缺失", "2026-03-02 23:30", severity="B", status="待处理"),
        hazard(H4, "SYNTH 安全工器具超期", "2026-03-18 09:00", severity="C级", status="已移交"),
    )


def boundary_defects() -> tuple[EamRecord, ...]:
    """边界缺陷：末日晚于窗口结束（同一日历日 → 应触发 E003）、下一天（应被窗口滤掉）。"""
    return (
        defect(LAST_DAY_LATE, "SYNTH 末日晚间缺陷", "2026-04-01 12:00", severity="一般", status="处理中"),
        defect(OUT_OF_DAY_RANGE, "SYNTH 下个窗口缺陷", "2026-04-02 09:00", severity="一般", status="处理中"),
    )


def unmappable_defects() -> tuple[EamRecord, ...]:
    """不可映射：编号为空（无法派生 event_id）、发现时间写法无法解析。"""
    return (
        defect(NO_REF, "SYNTH 缺编号缺陷", "2026-03-12 10:00", severity="一般", status="处理中"),
        defect(BAD_TIME_REF, "SYNTH 时间写法异常缺陷", "2026/03/12", severity="一般", status="处理中"),
    )


def open_defect(shift_id: str, discovered_at: str) -> EamRecord:
    """未消缺缺陷（同一编号用于跨驻场期重新提出的用例）。"""
    return defect(
        OPEN_REF,
        "SYNTH 未消缺缺陷（跨驻场期跟踪）",
        discovered_at,
        severity="一般",
        status="处理中",
        note="SYNTH 夹具：本驻场期未消缺",
    )


def make_adapter(
    *,
    defect_records: Iterable[EamRecord] | None = None,
    hazard_records: Iterable[EamRecord] | None = None,
) -> SyntheticEamAdapter:
    """默认夹具 = 五条缺陷 + 四条隐患（都在默认驻场期内可拉到）。"""
    return SyntheticEamAdapter(
        defects=defects() if defect_records is None else defect_records,
        hazards=hazards() if hazard_records is None else hazard_records,
    )


def expected_fetched() -> int:
    return len(defects()) + len(hazards())


def fixed_clock(moment: datetime = datetime(2026, 4, 1, 9, 0, tzinfo=SHANGHAI)):
    """固定时钟（不用墙钟）。"""
    return lambda: moment


__all__ = [
    "BAD_TIME_REF",
    "D1",
    "D2",
    "D3",
    "D4",
    "D5",
    "END",
    "FIRST_DAY",
    "FROM",
    "H1",
    "H2",
    "H3",
    "H4",
    "LAST_DAY",
    "LAST_DAY_LATE",
    "LINE",
    "NEXT_END",
    "NEXT_FIRST_DAY",
    "NEXT_SHIFT_ID",
    "NEXT_START",
    "NO_REF",
    "OPEN_REF",
    "OUT_OF_DAY_RANGE",
    "SHIFT_ID",
    "START",
    "TO",
    "boundary_defects",
    "defects",
    "expected_fetched",
    "fixed_clock",
    "hazards",
    "make_adapter",
    "make_next_shift",
    "make_shift",
    "make_window",
    "open_defect",
    "unmappable_defects",
]
