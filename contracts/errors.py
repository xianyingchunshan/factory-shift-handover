"""T01 公共契约：错误码最小集（issue #6 §7）。

使用规则（后续模块只依赖这一处定义，不私造错误码）：

- **跨模块业务拒绝** 一律用 :class:`ContractError`，``code`` 必须取自
  :data:`ERROR_CODES`（最小集，共 9 个）。
- **调用方传参/用法非法**（本地缺陷，例如未知枚举取值、越界参数）用
  :class:`ContractViolation`，它是 ``ValueError`` 子类，**不占用**业务错误码。

语义（issue #6 §3/§5/§7）：

===============  ====================================================
错误码            含义
===============  ====================================================
E001             发生时间缺失 → **拒收，不落表**（不是"告警后保留"）
E002             时间不完整（有日期无时:分）→ 告警要求补全
E003             发生时间越出班次区间 → 告警复核
E004             关键字段缺失（类别/重要级/状态）→ 告警
AUTH_REQUIRED    缺少可信身份，或确认操作者与待办指向的人不一致
DUPLICATE_SHIFT  同班同线（同幂等键）重复建班次，不建第二单
WRITE_UNKNOWN    外部写入/回读结果不明 → **只回查，不重放**
NOT_CONFIRMED    未确认先回写（状态机 pending → confirmed → written_back）
ALARM_NOT_CLEARED 存在未清除告警 → 禁止提交、禁止生成清单
===============  ====================================================
"""

from __future__ import annotations

from typing import Any, Mapping

E001 = "E001"
E002 = "E002"
E003 = "E003"
E004 = "E004"
AUTH_REQUIRED = "AUTH_REQUIRED"
DUPLICATE_SHIFT = "DUPLICATE_SHIFT"
WRITE_UNKNOWN = "WRITE_UNKNOWN"
NOT_CONFIRMED = "NOT_CONFIRMED"
ALARM_NOT_CLEARED = "ALARM_NOT_CLEARED"

#: 错误码最小集（顺序即契约文档顺序）。
ERROR_CODES: tuple[str, ...] = (
    E001,
    E002,
    E003,
    E004,
    AUTH_REQUIRED,
    DUPLICATE_SHIFT,
    WRITE_UNKNOWN,
    NOT_CONFIRMED,
    ALARM_NOT_CLEARED,
)

ERROR_CODE_LABELS: Mapping[str, str] = {
    E001: "发生时间缺失（拒收，不落表）",
    E002: "时间不完整（有日期无时:分，告警补全）",
    E003: "时间越出班次区间（告警复核）",
    E004: "关键字段缺失（类别/重要级/状态）",
    AUTH_REQUIRED: "缺少可信身份或操作者与待办不符",
    DUPLICATE_SHIFT: "同班同线重复建班次",
    WRITE_UNKNOWN: "外部写入/回读结果不明（只回查不重放）",
    NOT_CONFIRMED: "未确认先回写",
    ALARM_NOT_CLEARED: "存在未清除告警，禁止生成清单或提交",
}

#: 四条完整性校验中"拒收"的码（唯一一条不落表的规则）。
REJECTING_CODES: frozenset[str] = frozenset({E001})

#: 四条完整性校验中"落表 + 告警"的码。
ALARM_CODES: frozenset[str] = frozenset({E002, E003, E004})

#: 四条完整性校验全部规则码。
COMPLETENESS_RULES: tuple[str, ...] = (E001, E002, E003, E004)


class ContractError(Exception):
    """跨模块业务拒绝。``code`` 必须来自 :data:`ERROR_CODES`。"""

    def __init__(
        self,
        code: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(
                f"未登记的错误码 {code!r}；最小集为 {', '.join(ERROR_CODES)}"
            )
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.detail: dict[str, Any] = dict(detail or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "label": ERROR_CODE_LABELS[self.code],
            "detail": dict(self.detail),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"ContractError(code={self.code!r}, message={self.message!r})"


class ContractViolation(ValueError):
    """调用方参数或用法非法（本地缺陷），不占用业务错误码。

    例：未知枚举取值、负计数、把"已回写"的单据再次确认、缺少必填描述等。
    业务侧需要处置的拒绝应改用 :class:`ContractError`。
    """


def is_known_code(code: str) -> bool:
    """该字符串是否属于错误码最小集。"""
    return code in ERROR_CODES


def describe_codes() -> dict[str, str]:
    """返回错误码到中文含义的副本，供文档与清单渲染使用。"""
    return dict(ERROR_CODE_LABELS)


__all__ = [
    "ALARM_CODES",
    "ALARM_NOT_CLEARED",
    "AUTH_REQUIRED",
    "COMPLETENESS_RULES",
    "ContractError",
    "ContractViolation",
    "DUPLICATE_SHIFT",
    "E001",
    "E002",
    "E003",
    "E004",
    "ERROR_CODES",
    "ERROR_CODE_LABELS",
    "NOT_CONFIRMED",
    "REJECTING_CODES",
    "WRITE_UNKNOWN",
    "describe_codes",
    "is_known_code",
]
