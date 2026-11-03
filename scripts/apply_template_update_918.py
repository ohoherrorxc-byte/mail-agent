#!/usr/bin/env python3
"""把《2026-9-18模版更新.xlsx》写入全量知识库 20260918。不调接口。

更新表带「修改日期及状态」。SVC-003 在更新表是 colour code，全量漆面行仍用 SVC-003，
色号写入 SVC-004。删除行保留在表里并打删除标记，加载时跳过且不再被旧表补回。
"""
from __future__ import annotations

from copy import copy
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DST_KB = ROOT / "iSMART自动回复知识库_全量模板_20260918.xlsx"
UPDATE = ROOT / "2026-9-18模版更新.xlsx"

ID_REMAP = {"SVC-003": "SVC-004"}
AFTER_TID = {
    "ACC-009": "ACC-008",
    "STATUS-013": "STATUS-012",
    "FICM-PRIV-003": "FICM-PRIV-002",
    "SVC-004": "SVC-003",
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
    hits = []
    for r in range(2, ws.max_row + 1):
        if str(ws.cell(r, 1).value or "").strip() == tid:
            hits.append(r)
    return hits[0] if hits else None


def _upsert(ws, *, tid: str, values: list) -> None:
    row = _find_id_row(ws, tid)
    if row is None:
        anchor_tid = AFTER_TID.get(tid)
        anchor = (_find_id_row(ws, anchor_tid) if anchor_tid else None) or ws.max_row
        ws.insert_rows(anchor + 1)
        row = anchor + 1
        for c in range(1, 15):
            _copy_style(ws.cell(anchor, c), ws.cell(row, c))
    for i, v in enumerate(values, 1):
        ws.cell(row, i).value = v


def _norm(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _dedupe_svc003(ws) -> None:
    """全量若有两条 SVC-003（色号 + 漆面），色号改挂 SVC-004。"""
    paint = None
    colour = None
    for r in range(2, ws.max_row + 1):
        if str(ws.cell(r, 1).value or "").strip() != "SVC-003":
            continue
        scene = str(ws.cell(r, 5).value or "")
        if "colour" in scene.lower() or "color code" in scene.lower() or "色号" in scene:
            colour = r
        elif "车漆" in scene or "paint" in scene.lower():
            paint = r
    if colour and paint and colour != paint:
        if _find_id_row(ws, "SVC-004") is None:
            ws.cell(colour, 1).value = "SVC-004"
            print("  split duplicate SVC-003 colour → SVC-004")
        else:
            ws.delete_rows(colour)
            print("  drop duplicate SVC-003 colour (SVC-004 already exists)")


def main() -> None:
    wb = openpyxl.load_workbook(DST_KB)
    ws = wb["自动回复知识库"]
    _dedupe_svc003(ws)
    upd = openpyxl.load_workbook(UPDATE, data_only=True).active
    n = 0
    for r in range(2, upd.max_row + 1):
        src_id = _norm(upd.cell(r, 1).value)
        if not src_id:
            continue
        tid = ID_REMAP.get(src_id, src_id)
        values = [_norm(upd.cell(r, c).value) for c in range(1, 15)]
        values[0] = tid
        _upsert(ws, tid=tid, values=values)
        n += 1
        print(f"  {src_id} → {tid}  {values[1] or ''}")
    wb.save(DST_KB)
    print("wrote", DST_KB.name, "rows_from_update", n)


if __name__ == "__main__":
    main()
