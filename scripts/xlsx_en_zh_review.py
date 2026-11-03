#!/usr/bin/env python3
"""把审稿表「初始诉求 / 初次AI回复 / 人工修改」做成中英对照列。

默认读 Downloads 里的表，写出同目录 `*_中英对照.xlsx`，不覆盖原文件、不进 git。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import _env as _env_loader  # noqa: E402
from agent.azure_llm import AzureLLMClient  # noqa: E402

_env_loader.load_env()

SYSTEM = (
    "你把售后邮件审稿材料译成简体中文，给内部对照用。"
    "只翻译，不改事实、不补未写的步骤、不删隐私声明。"
    "保留 VIN、iSMART、App、MG、kWh、CarPlay、FOTA 等专有名词。"
    "空字符串保持空。不要解释。"
)


class RowZh(BaseModel):
    appeal_zh: str = Field(description="初始诉求中文")
    ai_zh: str = Field(description="初次AI回复中文")
    human_zh: str = Field(description="人工修改中文")


def _s(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def translate_row(llm: AzureLLMClient, appeal: str, ai: str, human: str) -> RowZh:
    if not appeal and not ai and not human:
        return RowZh(appeal_zh="", ai_zh="", human_zh="")
    obj, _ = llm.invoke_json(
        schema=RowZh,
        messages=[
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    "【初始诉求】\n"
                    f"{appeal}\n\n【初次AI回复】\n{ai}\n\n【人工修改】\n{human}"
                ),
            },
        ],
        reasoning_effort="low",
        max_completion_tokens=8192,
        role="review_zh",
    )
    return obj


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--in",
        dest="src",
        default=str(Path.home() / "Downloads" / "AI回复修改 (2).xlsx"),
    )
    p.add_argument("--out", default="")
    args = p.parse_args()
    src = Path(args.src)
    out = Path(args.out) if args.out else src.with_name(src.stem + "_中英对照.xlsx")

    ws_in = load_workbook(src, data_only=True).active
    rows = []
    for i, row in enumerate(ws_in.iter_rows(min_row=2, max_col=5, values_only=True), 2):
        _, appeal, ai, human, note = row
        if not any([appeal, ai, human, note]):
            continue
        rows.append((i, _s(appeal), _s(ai), _s(human), _s(note)))

    llm = AzureLLMClient()
    wb = Workbook()
    ws = wb.active
    ws.title = "中英对照"
    headers = [
        "#",
        "初始诉求 EN",
        "初始诉求 ZH",
        "初次AI回复 EN",
        "初次AI回复 ZH",
        "人工修改 EN",
        "人工修改 ZH",
        "备注",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")

    for n, (src_row, appeal, ai, human, note) in enumerate(rows, 1):
        print(f"[{n}/{len(rows)}] source_row={src_row}", flush=True)
        zh = translate_row(llm, appeal, ai, human)
        ws.append(
            [
                n,
                appeal,
                zh.appeal_zh,
                ai,
                zh.ai_zh,
                human,
                zh.human_zh,
                note,
            ]
        )
        ws.row_dimensions[ws.max_row].height = 90

    for col, width in enumerate([6, 42, 42, 42, 42, 42, 42, 28], 1):
        ws.column_dimensions[get_column_letter(col)].width = width
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for cell in row:
            cell.alignment = wrap
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:H{ws.max_row}"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(f"wrote {out} rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
