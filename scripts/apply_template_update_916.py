#!/usr/bin/env python3
"""把《2026-9-16模版更新.xlsx》写入全量知识库 20260916。不调接口。

SVC-003 在全量里已是漆面/配件，更新表同 ID 的 colour code 写入 SVC-004。
在已有 20260916 上增量合入，不从 20260914 覆盖掉上一批行。
"""
from __future__ import annotations

import shutil
from copy import copy
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DST_KB = ROOT / "iSMART自动回复知识库_全量模板_20260916.xlsx"
SRC_KB = DST_KB if DST_KB.exists() else ROOT / "iSMART自动回复知识库_全量模板_20260914.xlsx"
UPDATE = ROOT / "2026-9-16模版更新.xlsx"

# 更新表 ID → 全量 ID（避免覆盖已占用语义）
ID_REMAP = {"SVC-003": "SVC-004"}
AFTER_TID = {
    "ACC-007": "ACC-005",
    "ACC-008": "ACC-007",
    "STATUS-012": "STATUS-007",
    "DK-003": "DK-002",
    "BIND-008": "BIND-007",
    "DK-004": "DK-003",
    "MAP-005": "MAP-004",
    "SVC-004": "SVC-003",
    "APP-NOTIFICATION-001": "STATUS-009",
    "NON-009": "NON-008",
}


def _copy_style(src, dst) -> None:
    if src.has_style:
        dst.font = copy(src.font)
        dst.border = copy(src.border)
        dst.fill = copy(src.fill)
        dst.number_format = src.number_format
        dst.protection = copy(src.protection)
        dst.alignment = copy(src.alignment)


def _find_id_row(ws, tid: str) -> int | None:
    for r in range(2, ws.max_row + 1):
        if str(ws.cell(r, 1).value or "").strip() == tid:
            return r
    return None


def _upsert(ws, *, tid: str, values: list) -> None:
    row = _find_id_row(ws, tid)
    if row is None:
        anchor_tid = AFTER_TID.get(tid)
        anchor = (_find_id_row(ws, anchor_tid) if anchor_tid else None) or ws.max_row
        ws.insert_rows(anchor + 1)
        row = anchor + 1
        for c in range(1, 14):
            _copy_style(ws.cell(anchor, c), ws.cell(row, c))
    for i, v in enumerate(values, 1):
        ws.cell(row, i).value = v


def _norm(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def main() -> None:
    if SRC_KB.resolve() != DST_KB.resolve():
        shutil.copy2(SRC_KB, DST_KB)
    wb = openpyxl.load_workbook(DST_KB)
    ws = wb["自动回复知识库"]
    upd = openpyxl.load_workbook(UPDATE, data_only=True).active
    n = 0
    for r in range(2, upd.max_row + 1):
        src_id = _norm(upd.cell(r, 1).value)
        if not src_id:
            continue
        tid = ID_REMAP.get(src_id, src_id)
        values = [
            tid,
            _norm(upd.cell(r, 3).value),
            _norm(upd.cell(r, 4).value),
            _norm(upd.cell(r, 5).value),
            _norm(upd.cell(r, 6).value),
            _norm(upd.cell(r, 7).value),
            _norm(upd.cell(r, 8).value),
            _norm(upd.cell(r, 9).value),
            _norm(upd.cell(r, 10).value),
            _norm(upd.cell(r, 11).value),
            _norm(upd.cell(r, 12).value),
            _norm(upd.cell(r, 13).value),
            _norm(upd.cell(r, 14).value),
        ]
        _upsert(ws, tid=tid, values=values)
        n += 1
        print(f"  {src_id} → {tid}")
    wb.save(DST_KB)
    print("wrote", DST_KB.name, "rows_from_update", n)


if __name__ == "__main__":
    main()
