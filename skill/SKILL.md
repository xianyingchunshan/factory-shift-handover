# SKILL：厂级交接班 skill（输入 → 过程 → 输出）

把已冻结的三段能力（**输入校验 → 钉钉AI表格读写 → 交接班清单**）封装成**可独立运行**的一条命令：
给定一份输入 JSON，跑完三步，产出人能看懂的清单与机器可读的结果。

- 事实来源：`SPEC.md` §0（交付形态 = 可运行 skill）、Issue #19（本卡）。
- 实现：`skill/config.py`（输入契约）、`skill/pipeline.py`（过程与输出）、`skill/run.py`（入口）。
- 全程**离线**：表格读写走合成适配器（`integrations/aitable/synthetic.py` 内存实现），
  **不联网、不接真实钉钉/AI表格、无第三方依赖、不读环境变量或凭据**。
- 全部示例/输出数据为虚构合成值（`SYNTH-` 前缀），不含真实人名、真实群/表 ID、个人绝对路径。

## 1. 范围

**做**：给定输入 → 结构前置校验 → 合成 AI 表格读写（班次表 / 交接事件表 / 留痕表 / 待办表）
→ 告警闸门 → 交接班清单 + 结果 JSON + 状态快照。

**明确不做（范围外）**：

- 机器人 / 群关键词监听 / 任何外部触发与部署配置——由使用方自理（SPEC §0 拍板，原 Probe-02 已取消）。
- 真实钉钉 / AI 表格读写、真实待办创建与回写——属 **L4**，由主控在隔离环境执行。
- 班次防重键（同班同线判重）的跨单改造——另有卡片处理；本 skill 仅沿用既有 `DUPLICATE_SHIFT` 语义。
- 告警的人工处置动作（补全时间 / 越界复核 / 补关键字段）——属**人工停止点**，不在输入契约内（见 §6）。

## 2. 快速开始

仓库根目录，Python 3.11+，无依赖：

```text
python -m skill.run --input skill/examples/stay_period_handover.json --out <输出目录>
python -m skill.run --input skill/examples/stay_period_blocked.json  --out <输出目录>   # 告警拦截示例（退出码 2）
python -m skill.run --resume <上次输出目录>/state.json --out <输出目录>                 # 重启/重复拉取重放（只读）
python -m skill                                                                    # 等价入口（--help 看参数）
```

示例目录 `skill/examples/`：`stay_period_handover.json`（驻场期合规单，出清单）、
`stay_period_blocked.json`（E001–E004 各一条，被闸门拦下）。

## 3. 输入契约

输入是一个 JSON 对象（`version` 可选，当前 `1`）：

```json
{
  "version": 1,
  "shift": {
    "shift_id": "SYNTH-SHIFT-STAY-0001",
    "handover_line": "SYNTH-LINE-A",
    "shift_name": "stay_period",
    "start_time": "2026-03-02 08:00",
    "end_time": "2026-04-01 08:00",
    "handover_from": {"source": "dingtalk", "user_id": "SYNTH-uid-from", "display_name": "SYNTH-交班人A"},
    "handover_to":   {"source": "eam",      "user_id": "SYNTH-uid-to",   "display_name": "SYNTH-接班人B"},
    "critical_standard": ["重大事项", "移交接班人"]
  },
  "now": "2026-04-01 09:00",
  "events": [
    {
      "event_id": "SYNTH-STAY-EVT-0001",
      "category": "runtime",
      "description": "SYNTH 运行工况记录",
      "occurred_at": "2026-03-02 08:10",
      "severity": "normal",
      "status": "done",
      "ref_no": "SYNTH-REF-001",
      "note": ""
    }
  ]
}
```

### shift（必填块）

| 字段 | 必填 | 说明 |
|---|---|---|
| `shift_id` | 是 | 稳定唯一标识（幂等键的一部分） |
| `handover_line` | 是 | 交接线（配置决定，非人员自由填写） |
| `shift_name` | 否 | **默认 `stay_period`（驻场期）**；`early/middle/late/custom` 保留给**历史数据与例外**，照旧可读、口径不变 |
| `start_time` / `end_time` | 是 | **驻场期边界**，精确到分，可长达数十天；不带偏移按 Asia/Shanghai 解释 |
| `handover_from` / `handover_to` | 是 | 身份引用 `{source, user_id, display_name?}`；`source ∈ dingtalk/eam/ehr`，`user_id` 为非空字符串 |
| `critical_standard` | 否 | 关键级强制标准，默认 `["重大事项", "移交接班人"]` |
| `shift_date` | 否 | **只允许等于驻场期起始日**（= `start_time` 的日期）；给其它的日期即输入非法（口径 4） |

### events（必填数组）

每行一条事项，字段与冻结契约一致：`event_id`（必填）、`category`、`description`（必填，四条告警不覆盖该字段）、
`occurred_at`（可为 `null` → 走 E001 拒收路径；只给日期 → 走 E002 告警路径）、`severity`、`status`、
`owner`（可选身份引用）、`ref_no`、`note`。行内 `shift_id` 可省略（默认取班次），
给了就必须与班次一致。类别可用规范值或中文标签（如 `major` / `重大事项`）。

### now（可选）

确定性时钟（出清单时刻、告警留痕时间），**默认 = `end_time`**。同一输入两次运行产出逐字一致，
不使用墙钟。

### 前置校验（本 skill 只拦「调用方缺陷」）

结构不符、必填留痕缺失（`shift_id` / `event_id` / `description` / 时间边界）、枚举取值不认识、
时间不可解析或精度不足（未到分）、`shift_id` / `shift_date` 与班次不符、`version` 不支持
→ 报**全部问题**后以退出码 `3` 结束，**不落表、不写任何输出文件**。

**不在这里拦**：`occurred_at` 为空 / 只有日期 / 越界、`category`/`severity`/`status` 为空
——这些是 E001–E004 业务告警路径，按 §5 处理。

## 4. 输出契约

输出目录（不存在则创建）：

| 文件 | 何时产出 | 内容 |
|---|---|---|
| `checklist.txt` | **仅出清单时**（退出码 0） | 第 1 行 = 清单标题（口径 2），随后是契约五段：重要事项 / 事件流水 / 未完移交 / 完整性校验 / 确认区 |
| `result.json` | 总是 | 确定性结果载荷（下表） |
| `state.json` | 总是 | 六张表的原始快照，可原样喂给 `--resume` 重放 |

`result.json` 关键字段：`skill`、`mode`（`fresh`/`resume`）、`source`、`outcome`（`checklist`/`blocked`）、
`exit_code`、`shift`（含 `title` 口径同源的 `shift_name_label` / 起止 / 覆盖日历日 / `status`）、`title`、
`intake`（逐条落表结果 + 告警）、`alarms`（`counts_by_rule` / `open_count` / 全部留痕）、
`tables`（各表行数）、`summary`（事件数 / 完整数 / 时间完整率 / 告警数 / 关键级 / 未完移交 / 确认单元）、
`blocked`（被拦时的未清告警与拒收留痕）、`protocol`（随卡口径逐条列出）。
`json.dumps(..., sort_keys=True)` 输出，便于逐字 diff。

### 退出码

| 码 | 含义 | 产出 |
|---|---|---|
| `0` | 出清单（复验通过 → 提交 → 清单生成） | `checklist.txt` + `result.json` + `state.json` |
| `2` | **告警未清**：不出清单（班次置 `blocked`） | `result.json` + `state.json` |
| `3` | **输入非法**：不落表、不写文件 | 无（诊断打 stderr） |

## 5. 告警语义（E001–E004 取舍）

| 规则 | 触发 | 本 skill 的取舍 |
|---|---|---|
| `E001` 时间缺失 | `occurred_at` 缺失 / 空 | **拒收且不落表**；事件表无此行，证据写输入留痕表；未清则禁提交禁出清单 |
| `E002` 时间不完整 | 只有日期没有时:分 | **落表 + 告警**；原始时间文本原样落列，不丢输入；须人工补全时才清 |
| `E003` 时间越界 | 时间不在 `[start_time, end_time]` 内 | **落表 + 告警**；边界 = **驻场期整体区间**，跨零点相连日历日均属期内，**不按单日切分**（T04 冻结口径） |
| `E004` 关键字段缺失 | `category` / `severity` / `status` 为空 | **落表 + 告警**；交班提交前全表复验时统一暴露 |

闸门与升级：

- **未清告警 → 禁止提交、禁止出清单**（`ALARM_NOT_CLEARED`）：`recheck()` 复验失败即返回退出码 2，
  班次状态回写为 `blocked`，清单文件不产出。
- 关键级强制升级：第 5 类（重大事项）默认关键级；状态 =「移交接班人」自动升级关键级，**不可降**。
- 告警是**业务信号**，本 skill **不代替人工清告警**（人工停止点）：交班人补全 / 复核后重新提交输入，
  重新跑一遍即可（合成的库是进程内的，重跑不会产生第二行）。

## 6. 失败处置

| 情形 | 处置 |
|---|---|
| 输入非法（§3） | 退出码 3；stderr 打印逐条诊断；**不落表、不写文件** |
| 告警未清 | 退出码 2；`result.json.blocked` 列出未清告警与拒收留痕；**不出清单** |
| 同一输入内重复 `event_id` | 第二次起为 `duplicate`：不双写、不报错，`intake.outcomes` 可见 |
| 同班同线重复建单（库内已有） | 契约 `DUPLICATE_SHIFT`：不为新单建第二行（错误码来自冻结契约） |
| 落表受理不明 | 只回查不重放（契约 `WRITE_UNKNOWN` 语义）；合成适配器提供故障注入用于反向验证 |
| 进程重启 / 重复拉取 | `--resume state.json`：从快照重建，**不重写表格、不重放外部写入、不重复创建待办**；恢复期不一致只记 `restore_issues`，不自动补写 |
| 未知异常 | 直接抛出（非零退出），不吞异常、不伪造成功 |

## 7. 配置项

本 skill **没有独立配置文件、没有隐藏状态**，一次运行的全部输入都显式给在命令行与输入 JSON 里：

| 参数 | 必填 | 说明 |
|---|---|---|
| `--input PATH` | 与 `--resume` 二选一 | 输入 JSON（§3） |
| `--resume PATH` | 与 `--input` 二选一 | 上次输出的 `state.json`；重放模式（只读） |
| `--out DIR` | 是 | 输出目录（§4） |
| `--quiet` | 否 | 只打印结论行，不打印清单正文 |

不读环境变量、不读凭据、不访问网络；表格目标固定为合成适配器（接非合成适配器直接断言失败）。

## 8. 未覆盖边界

- **真实外部系统**：真实 AI 表格 / 待办 / 消息读写与送达回读（L4，由主控执行）。
- **机器人 / 关键词触发 / 部署**：使用方自理；本 skill 只提供可被调用的命令与输入输出契约。
- **人工停止点**：授权、人员配置、交班填写、接班确认不代填；清单确认区的待办只按契约生成，
  「完成回写」状态机（`pending → confirmed → written_back`）由既有工作流承载，不在本 skill 输入契约内。
- **换人 / 路由跟随**：接班人变更与旧待办显式作废重发属 T03 能力，本 skill 不暴露。
- **判重键**：同班同线跨单判重键另有卡片处理；当前 `shift_date` 固定取驻场期起始日（口径 4）。
- **多人会签、跨班统计、历史检索、群发、移动端**：SPEC §8 明示延期。
- **九类自动化拉取**（EAM/两票/气象等）：T05 及后续卡片范围。

## 9. 验证与测试位置

测试放在 `tests/` **根目录**（`tests/test_skill_*.py`），而不是新建 `tests/skill/` 子目录：

- CI（`.github/workflows/checks.yml`）逐目录显式运行 `tests/`、`tests/contracts`、`tests/workflow`、
  `tests/integrations`，**新增子目录不会被自动运行**（`unittest discover` 不递归无 `__init__.py` 的子目录）；
- 仓库守卫 `tests/test_repo_structure.py::test_ci_runs_every_test_subdir` 要求「每个含 `test_*.py` 的
  `tests/` 子目录都必须被 CI 显式运行」，而本卡**不改 CI**（范围硬约束）——因此把新增测试放根目录，
  由 CI 的 repo guard 步骤一并发现运行。

本地（仓库根目录，逐字可复现）：

```text
python -B -m unittest discover -s tests -p "test_*.py" -v      # 含 skill 测试与仓库守卫
python -B scripts/repo_checks.py                               # 仓库保守检查
```
