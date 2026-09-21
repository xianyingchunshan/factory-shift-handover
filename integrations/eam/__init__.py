"""EAM 只读集成（issue #23：九类自动化①缺陷 + 隐患）。

本包只交付**只读**能力：

- 协议 :class:`~integrations.eam.adapter.EamReadOnlyAdapter`（``list_defects`` /
  ``list_hazards``，入参 = 驻场期窗口，**协议内无任何写方法**）；
- 合成实现 :class:`~integrations.eam.synthetic.SyntheticEamAdapter`（内存夹具，
  ``SYNTH-`` 前缀，离线、可回读拉取留痕）；
- 集中映射表 :mod:`integrations.eam.mapping`（EAM 原始值 → 九类统一字段；
  未知取值不猜 → 留空 + 触发 E004 + 原始值留痕；**待 L4 校准，只改这一处**）。

真实 EAM 连接与读取属 L4，由主控执行；本包不联网、不含表 ID / 账号 / 凭据。
编排（拉取 → 候选行 → 既有四条告警 → 交接事件表，两层幂等）在
:mod:`workflow.eam_pull`。
"""

from __future__ import annotations

from .adapter import (
    KIND_DEFECT,
    KIND_HAZARD,
    KIND_LABELS,
    KINDS,
    READ_METHODS,
    READ_ONLY_NOTE,
    WINDOW_SCOPE_NOTE,
    EamError,
    EamReadError,
    EamReadOnlyAdapter,
    EamRecord,
    EamWindow,
    defect,
    hazard,
    read_only_method_names,
    record_in_window,
    write_like_method_names,
)
from .mapping import (
    DEFECT_SEVERITY_MAP,
    HAZARD_SEVERITY_MAP,
    KIND_TO_CATEGORY,
    MAPPING_TABLES,
    MAPPING_TABLES_VERSION,
    REJECT_BAD_TIME,
    REJECT_MISSING_REF,
    STATUS_MAP,
    CandidateOutcome,
    MappedValue,
    build_candidate,
    derive_event_id,
    map_severity,
    map_status,
    mapping_tables_doc,
)
from .synthetic import (
    ReadCall,
    SyntheticEamAdapter,
    assert_synthetic_eam,
    empty_adapter,
    synth_ref,
)

__all__ = [
    "DEFECT_SEVERITY_MAP",
    "HAZARD_SEVERITY_MAP",
    "KIND_DEFECT",
    "KIND_HAZARD",
    "KIND_LABELS",
    "KIND_TO_CATEGORY",
    "KINDS",
    "MAPPING_TABLES",
    "MAPPING_TABLES_VERSION",
    "READ_METHODS",
    "READ_ONLY_NOTE",
    "REJECT_BAD_TIME",
    "REJECT_MISSING_REF",
    "STATUS_MAP",
    "WINDOW_SCOPE_NOTE",
    "CandidateOutcome",
    "EamError",
    "EamReadError",
    "EamReadOnlyAdapter",
    "EamRecord",
    "EamWindow",
    "MappedValue",
    "ReadCall",
    "SyntheticEamAdapter",
    "assert_synthetic_eam",
    "build_candidate",
    "defect",
    "derive_event_id",
    "empty_adapter",
    "hazard",
    "map_severity",
    "map_status",
    "mapping_tables_doc",
    "read_only_method_names",
    "record_in_window",
    "synth_ref",
    "write_like_method_names",
]
