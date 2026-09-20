"""外部系统集成层（本卡只交付钉钉AI表格的**合成适配器**）。

约定：

- 业务层只依赖抽象口 :class:`~integrations.aitable.adapter.AitableAdapter`，
  真实实现与合成实现可互换；仓库内不提供任何真实网络实现。
- 真实表格读写属 L4，由主控在隔离测试 Base 上执行（issue #9 范围说明）。
- 本层不含任何表 ID / 账号 / 凭据；表名与列名取自
  :mod:`contracts.aitable_mapping`。
"""
