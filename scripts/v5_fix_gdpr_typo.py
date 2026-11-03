"""
v5_fix_gdpr_typo.py — 修复 v5 模板里 GDPR 段落的 {customer_email} you 拼接错误

根因：原始 cs_replies 里 14,314 条都有同一个客服邮件模板 typo：
    "...complaint on Personal Data Rights to the following mail,
        user_0d6e0cab@example.com you!"
  （客服把"Thank"漏了，把 GDPR 邮箱塞在了 "Thank you" 中间）

v4 clean_reply() 把 user_xxx@example.com 一律打成 {customer_email} 占位符，
结果变成 "...following mail,{customer_email} you!" —— 模板里出现 23 次。

修复策略（v5 层定向 patch）：
  1. {customer_email} you!                                  → customer.care@ismart-service.com. Thank you!
  2. {customer_email} you for choosing MG!                  → customer.care@ismart-service.com. Thank you for choosing MG!
  3. {customer_email} you for your cooperation and understanding → customer.care@ismart-service.com. Thank you for your cooperation and understanding.
  4. {customer_email} response to your feedback              → customer.care@ismart-service.com. Thank you for your response to your feedback,
  5. {customer_email} can try the following                  → customer.care@ismart-service.com. You can try the following
  6. {customer_email} you once again                         → customer.care@ismart-service.com. Thank you once again
  7. "{customer_email}"                                      → "customer.care@ismart-service.com"   （quoted header 残留）

为什么不在 v4 clean_reply() 层改：那是数据清洗脚本，会影响 v2 / v3 / v4 全部模板。
v5 的目标只是补 4 个 intent 的草稿空缺，本地 patch 即可。
"""
from __future__ import annotations

import sys
import re
from pathlib import Path
from openpyxl import load_workbook

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_XLSX = PROJECT_ROOT / "iSMART售后服务邮箱回复方法_v5_templates.xlsx"

# GDPR 客服邮箱（从原 user_0d6e0cab@example.com 解出 — 这是 iSmart 客服团队邮箱，
# 不是客户邮箱，所以可以直接回填，不需要 redact）
CS_TEAM_EMAIL = "customer.care@ismart-service.com"

# 修复 mapping（顺序敏感：从最长到最短，避免短匹配吞长匹配）
FIXES: list[tuple[str, str]] = [
    # 1. you for your cooperation and understanding. （最长）
    (
        "{customer_email} you for your cooperation and understanding",
        f"{CS_TEAM_EMAIL}. Thank you for your cooperation and understanding",
    ),
    # 2. you for choosing MG!
    (
        "{customer_email} you for choosing MG!",
        f"{CS_TEAM_EMAIL}. Thank you for choosing MG!",
    ),
    # 3. response to your feedback
    (
        "{customer_email} response to your feedback",
        f"{CS_TEAM_EMAIL}. Thank you for your response to your feedback",
    ),
    # 4. can try the following
    (
        "{customer_email} can try the following",
        f"{CS_TEAM_EMAIL}. You can try the following",
    ),
    # 5. you once again
    (
        "{customer_email} you once again",
        f"{CS_TEAM_EMAIL}. Thank you once again",
    ),
    # 6. you!  （最常见的 20 处）
    (
        "{customer_email} you",
        f"{CS_TEAM_EMAIL}. Thank you",
    ),
    # 7. quoted header 残留（quoted email 被夹在 Cc: 里）
    (
        "'{customer_email}'",
        f"'{CS_TEAM_EMAIL}'",
    ),
    (
        '"{customer_email}"',
        f'"{CS_TEAM_EMAIL}"',
    ),
]


def patch_template(body: str) -> tuple[str, list[str]]:
    """对单条模板做 7 类替换，返回 (patched_body, 触发的修复列表)。"""
    fixes_applied = []
    for old, new in FIXES:
        if old in body:
            count = body.count(old)
            body = body.replace(old, new)
            fixes_applied.append(f"{old!r} × {count}")
    return body, fixes_applied


def main() -> int:
    if not DEFAULT_XLSX.exists():
        print(f"[ERR] {DEFAULT_XLSX} not found")
        return 1

    wb = load_workbook(DEFAULT_XLSX, data_only=True)
    ws = wb.active
    # legacy 3 列 schema: A=Category B=Template C=Description
    n_patched_rows = 0
    total_fixes = 0
    summary = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        cat, tmpl, desc = (row[0] or ""), (row[1] or ""), (row[2] or "")
        if not tmpl or "v5:" not in desc:
            continue
        new_tmpl, fixes = patch_template(tmpl)
        if fixes:
            # 写回
            # iter_rows 是只读的，要用 ws.cell() 写
            row_idx = ws.max_row  # placeholder, actual find below
            # 找到行号
            for r_idx, r in enumerate(ws.iter_rows(min_row=2, values_only=False), start=2):
                if r[1].value == tmpl:
                    r[1].value = new_tmpl
                    n_patched_rows += 1
                    total_fixes += len(fixes)
                    summary.append((cat[:30], fixes))
                    break
    wb.save(DEFAULT_XLSX)

    print(f"[patch] wrote {n_patched_rows} rows, {total_fixes} fixes total")
    print("\n=== per-template summary ===")
    for cat, fixes in summary[:10]:
        print(f"  {cat}: {fixes}")
    if len(summary) > 10:
        print(f"  ... +{len(summary)-10} more")

    # 验证：load_xlsx 后看 {customer_email} 残留
    print("\n=== verification: reload + check residue ===")
    from agent.template_reply import load_xlsx
    templates = load_xlsx()
    v5_only = [t for t in templates if 'v5:' in (t.get('description') or '')]
    residue = [t for t in v5_only if '{customer_email}' in t['template']]
    print(f"  v5 templates with {{customer_email}} residue: {len(residue)}")
    if residue:
        for t in residue[:3]:
            idx = t['template'].find('{customer_email}')
            print(f"    [{t['intent']}]  ...{t['template'][max(0,idx-30):idx+60]}...")

    # 验证：sample 1 条看拼接是否修好
    print("\n=== sample patched template ===")
    for t in v5_only:
        if t['intent'] == 'ismart_account_inquiry':
            tail = t['template'][-200:]
            if 'Thank you' in tail:
                print(t['template'][-400:])
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())