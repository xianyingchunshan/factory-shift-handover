# T01 公共契约（冻结候选）

> 状态：**候选冻结**。主控审查合并后才视为冻结；消费者不得把候选当已冻结依赖（issue #6）。
> 仅依赖 Python 标准库；不联网；不调用任何外部系统。

> T03 **增补**（issue #12，2026-09-21 主控拍板口径）：新增 `successor.py`
> （接班人前置询问 + 换人变更），**不改**下列五类契约的字段、方法与既有语义。
> 换人规则：新接班人须解析为唯一可信身份（`AUTH_REQUIRED` 拒绝解析不到/不唯一），
> `archived` 终态与 `blocked`（告警未清）拒绝变更（`ALARM_NOT_CLEARED`），
> 变更显式留痕（谁/何时/从谁改到谁）且旧配置快照进 `snapshot_history`。
> 待办作废状态列属**工作流辅助表**（见 `integrations/aitable/tables.py`），不是契约列。

## 五类契约

| 契约 | 位置 | 关键不变量 |
|---|---|---|
| 班次 `ShiftRecord` | `shift.py` | 幂等键=(交接线,日期,班次)；`draft→submitted→confirmed→archived`，`blocked`=告警未清；配置快照建立时锁定，变更须显式确认 |
| 交接事件 `HandoverEvent` | `events.py` | 九类统一字段；`occurred_at` 必填精确到分；`category=major` 与 `status=transferred` 强制 `critical` 且不可降 |
| 完整性告警 `CompletenessAlarm` | `alarms.py` | 四条校验；告警带 `created_at/cleared_at`；未清告警禁止提交与出清单 |
| 输出清单 `HandoverReport` | `report.py` | 五段固定顺序；关键级置顶加粗；流水严格时间序；只单发交班人+接班人两人 |
| 确认回写 `Confirmation` | `confirmation.py` | `pending→confirmed→written_back`；无回读不得 confirmed；`unknown` 只回查不重放 |

`enums.py` 放九类/重要级/状态等枚举与中文别名；`identity.py` 放身份引用
（`dingtalk|eam|ehr`）；`timebase.py` 放时间口径（Asia/Shanghai 用固定 +08:00 表示，
Windows 无需 tzdata）；`aitable_mapping.py` 放钉钉AI表格字段映射（不写死表 ID）；
`successor.py`（T03 增补）放接班人变更：唯一身份解析、态约束、变更留痕账本。

## 错误码最小集

`E001`（发生时间缺失，**拒收不落表**）、`E002`（时间不完整）、`E003`（时间越界）、
`E004`（关键字段缺失：类别/重要级/状态）、`AUTH_REQUIRED`、`DUPLICATE_SHIFT`、
`WRITE_UNKNOWN`、`NOT_CONFIRMED`、`ALARM_NOT_CLEARED`。

约定：跨模块业务拒绝用 `ContractError`（`code` 必须属于最小集）；
调用方传参/用法非法用 `ContractViolation`（`ValueError` 子类，不占业务错误码）。

## 失败边界（按规则拒绝或告警）

| 输入 | 处置 |
|---|---|
| `occurred_at` 为空 | E001：拒收，**不落表** |
| 只有日期没有时:分 | E002：落表并告警，待补全（`occurred_at=None`） |
| 时间越出班次闭区间 | E003：落表并告警，待复核 |
| 类别/重要级/状态为空 | E004：落表并告警（每缺一字段一条） |
| 同班同线换 `shift_id` 重复建单 | `DUPLICATE_SHIFT`，不建第二单 |
| 同一 `shift_id` 重复提交 | 幂等 no-op，不双写 |
| 未清告警就要出清单/提交 | `ALARM_NOT_CLEARED`，班次置 `blocked` |
| 缺身份或操作者与待办指向的人不符 | `AUTH_REQUIRED`（错人不放行） |
| 无回读就标 `confirmed`/`verified` | `WRITE_UNKNOWN` |
| 未确认先回写 | `NOT_CONFIRMED` |
| 受理不明（`unknown`）时尝试重放 | `WRITE_UNKNOWN`：只回查不重放 |

## 合成测试

```
python -B -m unittest discover -s tests/contracts -p "test_*.py" -v
```

测试数据一律 `SYNTH-` 前缀，非真实人员、群、表或业务数据。
