"""T06 skill 入口（issue #19）：输入 → 过程 → 输出，一条命令跑通。

用法（仓库根目录，Python 3.11+，无第三方依赖、**不联网**）：

    python -m skill.run --input skill/examples/stay_period_handover.json --out <输出目录>
    python -m skill.run --input <输入.json> --out <目录> [--quiet]
    python -m skill.run --resume <上次输出目录>/state.json --out <目录>   # 重启重放（只读）

退出码：``0`` 出清单；``2`` 告警未清（不出清单，班次 blocked）；``3`` 输入非法（不落表、不写文件）。
完整契约（输入/输出、配置项、告警语义、失败处置、未覆盖边界）见同目录 ``SKILL.md``。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 支持 `python skill/run.py` 直接执行
    sys.path.insert(0, str(ROOT))

from contracts.errors import COMPLETENESS_RULES  # noqa: E402
from integrations.aitable.cells import shift_from_fields  # noqa: E402
from integrations.aitable.tables import SHIFT_TABLE  # noqa: E402

from skill.config import SkillInputError, load_input  # noqa: E402
from skill.pipeline import (  # noqa: E402
    EXIT_INPUT_ERROR,
    MODE_RESUME,
    OUTCOME_CHECKLIST,
    run,
    run_resume,
    shift_label,
)


def _force_utf8_stdio() -> None:
    """Windows 控制台默认编码（cp1252/GBK）打印中文会崩，统一改 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m skill.run",
        description="厂级交接 skill：输入校验 → 合成 AI 表格读写 → 交接班清单（离线）",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="输入 JSON 路径（输入契约见 SKILL.md）")
    source.add_argument("--resume", help="从上次输出的 state.json 重放（只读，不重写表格）")
    parser.add_argument("--out", required=True, help="输出目录（checklist.txt / result.json / state.json）")
    parser.add_argument("--quiet", action="store_true", help="只打印结论行，不打印清单正文")
    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        if args.resume:
            result, spec_line = _run_resume(args.resume)
        else:
            spec = load_input(args.input)
            spec_line = (
                f"输入校验通过：{spec.source or '(内存输入)'}（事件 {len(spec.events)} 条；"
                f"班次名 {shift_label(spec.shift)}）"
            )
            result = run(spec)
    except SkillInputError as exc:
        print(f"[skill] 输入非法，未落表、未写出任何文件：{exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    print(f"[skill] {spec_line}")
    print(f"[skill] {_stage_line(result)}")
    written = result.write(args.out)
    print(f"[skill] {_output_line(result)}")
    print(f"[skill] 已写：{', '.join(path.name for path in written)} → {Path(args.out)}")
    if result.outcome == OUTCOME_CHECKLIST and not args.quiet:
        print()
        print(result.checklist_text, end="")
    return result.exit_code


def _run_resume(snapshot_path: str):
    """读 state.json 快照 → 取班次表行（业务键不变）→ 只读重放。"""
    path = Path(snapshot_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SkillInputError(f"快照文件不可读: {path.name}（{exc.strerror or exc}）") from exc
    except json.JSONDecodeError as exc:
        raise SkillInputError(f"快照不是合法 JSON: {path.name}（第 {exc.lineno} 行）") from exc
    if not isinstance(payload, Mapping):
        raise SkillInputError("快照必须是 JSON 对象（state.json）")
    rows = payload.get(SHIFT_TABLE)
    if not isinstance(rows, list) or not rows:
        raise SkillInputError(f"快照里没有班次表（{SHIFT_TABLE}）行，无法重放")
    first = rows[0]
    fields: Any = first.get("fields") if isinstance(first, Mapping) else None
    if not isinstance(fields, Mapping):
        raise SkillInputError("快照班次行缺少 fields，无法重放")
    shift = shift_from_fields(fields)
    result = run_resume(
        payload, shift_id=shift.shift_id, now=shift.end_time, source=path.name
    )
    return result, (
        f"重放模式（{MODE_RESUME}）：{path.name} → 班次 {shift.shift_id}"
        f"（班次名 {shift_label(shift)}；不重写表格、不重复创建待办）"
    )


def _stage_line(result) -> str:
    count = result.tables()
    base = (
        f"过程（合成适配器，无网络）：班次 {result.shift.shift_id} 在库；"
        f"交接事件表 {count['events']} 行；待办 {count['todos']} 行"
    )
    if result.mode == MODE_RESUME:
        return base + f"；恢复期问题 {len(result.restore_issues)} 项"
    intake = result.to_dict()["intake"] or {}
    return base + (
        f"；落表 {intake.get('stored', 0)} / 拒收 {intake.get('rejected', 0)}"
        f" / 重复 {intake.get('duplicate', 0)}"
    )


def _output_line(result) -> str:
    if result.outcome == OUTCOME_CHECKLIST:
        return (
            f"输出：{result.title}（清单 {len(result.checklist_lines)} 行）"
            f"；事件数 {result.to_dict()['summary'].get('total')}"
        )
    counts = result.to_dict()["alarms"]["counts_by_rule"]
    detail = " ".join(
        f"{rule}×{counts.get(rule, 0)}" for rule in COMPLETENESS_RULES if counts.get(rule)
    )
    return (
        f"输出：告警未清（{detail}）→ 不出清单（班次 {result.shift.status}）；"
        "只写 result.json / state.json"
    )


if __name__ == "__main__":
    sys.exit(main())
