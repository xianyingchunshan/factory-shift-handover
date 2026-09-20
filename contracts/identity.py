"""T01 公共契约：可信身份引用 identity_ref。

身份源不唯一（Probe-01 三源实测成立）：``dingtalk | eam | ehr``。
仓库内不出现真实人员、真实账号；测试数据一律 ``SYNTH-`` 前缀合成值。

规则：

- 缺身份 -> :data:`~contracts.errors.AUTH_REQUIRED`（不是告警，是拒绝）。
- 待办确认必须"操作者 = 待办指向的人"（错人不放行）。
- 仓库/PR 不得出现真实姓名或真实人员 ID，``display_name`` 只作展示用途。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .enums import IdentitySource, coerce_enum, is_blank
from .errors import AUTH_REQUIRED, ContractError, ContractViolation


@dataclass(frozen=True)
class IdentityRef:
    """一条可信身份引用（不落真实姓名，display_name 仅展示）。"""

    source: str
    user_id: str
    display_name: str = ""

    def __post_init__(self) -> None:
        source = coerce_enum(IdentitySource, self.source, field="identity.source")
        if is_blank(self.user_id):
            raise ContractViolation("identity.user_id 不能为空")
        object.__setattr__(self, "source", source.value)
        object.__setattr__(self, "user_id", str(self.user_id).strip())
        object.__setattr__(self, "display_name", str(self.display_name or "").strip())

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "user_id": self.user_id,
            "display_name": self.display_name,
        }

    def same_person(self, other: "IdentityRef | None") -> bool:
        """按 (身份源, user_id) 判断是否同一人；显示名不参与判定。"""
        if other is None:
            return False
        return (self.source, self.user_id) == (other.source, other.user_id)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "IdentityRef":
        """从表格/接口原始字典构造；缺字段即拒绝。"""
        if not isinstance(data, Mapping):
            raise ContractViolation(f"identity 需要映射，收到 {type(data).__name__}")
        missing = [k for k in ("source", "user_id") if is_blank(data.get(k))]
        if missing:
            raise ContractViolation(f"identity 缺少字段: {', '.join(missing)}")
        return cls(
            source=data["source"],
            user_id=data["user_id"],
            display_name=data.get("display_name", ""),
        )


def as_identity(value: Any) -> IdentityRef:
    """IdentityRef / 映射 / None 归一化；None 与空映射抛 :class:`ContractViolation`。"""
    if isinstance(value, IdentityRef):
        return value
    if isinstance(value, Mapping):
        return IdentityRef.from_mapping(value)
    raise ContractViolation(f"identity 需要 IdentityRef 或映射，收到 {type(value).__name__}")


def require_identity(value: Any, *, field: str = "identity") -> IdentityRef:
    """缺身份一律 :data:`AUTH_REQUIRED`。确认与回写路径必须走这里。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ContractError(
            AUTH_REQUIRED,
            f"{field} 缺失：确认与回写必须带可信身份，缺输入不可代填",
            detail={"field": field},
        )
    try:
        return as_identity(value)
    except ContractViolation as exc:
        raise ContractError(
            AUTH_REQUIRED,
            f"{field} 非法：{exc}",
            detail={"field": field, "raw_type": type(value).__name__},
        ) from exc


def require_operator(
    expected: IdentityRef | None,
    actual: Any,
    *,
    field: str = "confirmed_by",
) -> IdentityRef:
    """要求实际操作者与待办指向的人一致；错人不放行。"""
    operator = require_identity(actual, field=field)
    if expected is None:
        raise ContractError(
            AUTH_REQUIRED,
            f"{field} 无法核验：缺少待办指向人",
            detail={"field": field},
        )
    if not expected.same_person(operator):
        raise ContractError(
            AUTH_REQUIRED,
            f"{field} 与待办指向的人不一致：错人不放行",
            detail={
                "field": field,
                "expected_source": expected.source,
                "actual_source": operator.source,
            },
        )
    return operator


def with_display_name(ref: IdentityRef, display_name: str) -> IdentityRef:
    """展示名可更新，身份键不可改。"""
    return replace(ref, display_name=display_name)


__all__ = [
    "IdentityRef",
    "as_identity",
    "require_identity",
    "require_operator",
    "with_display_name",
]
