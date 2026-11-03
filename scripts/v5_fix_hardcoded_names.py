"""
v5_fix_hardcoded_names.py — 修复 v5 模板里两类污染

A. 硬编码人名残留（PII + 模板污染）：
   - "Hi Ben，..." / "Dear Steve,..." 这类把客户 first name 写死在模板里的
   - 应替换为 "{{salutation}}," 让 personalize_greeting() 来填
   - 已知 2 处：
     1. ismart_digital_key: "Hi Ben，..." → "{{salutation}},..."
     2. ismart_data_plan:   "Dear Steve,..." → "{{salutation}},..."

B. 全角符号混入（CJK 标点）：
   - 全角分号 ；、全角逗号 ，、全角句号 。
   - 主要在 GDPR 段 "...etc.)；Your personal data..."
   - 已知 6 条 v5 模板出现
   - 应统一替换为半角 ASCII

为什么 patch 在 v5 层而不是 v4 clean_reply()：
- v4 是数据清洗脚本，影响 v2/v3/v4 全部模板，改动面广
- v5 定向 patch 只动 201 条新模板，安全
"""
from __future__ import annotations

import sys
import re
from pathlib import Path
from openpyxl import load_workbook

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_XLSX = PROJECT_ROOT / "iSMART售后服务邮箱回复方法_v5_templates.xlsx"


# A. 硬编码人名 → {{salutation}},
HARDCODED_NAMES: dict[str, str] = {
    # intent: [(old_pattern, new_pattern), ...]
    # 严格按"硬编码名 + 全角逗号"或"硬编码名 + 半角逗号"匹配
    "Hi Ben，":            "{{salutation}},",   # 全角逗号
    "Hi Ben,":             "{{salutation}},",
    "Dear Steve,":         "{{salutation}},",   # 半角逗号
    "Hi Steve,":           "{{salutation}},",   # 防御性
    "Dear Ben,":           "{{salutation}},",   # 防御性
}

# B. 全角符号 → 半角符号
CJK_PUNCT_FIXES: dict[str, str] = {
    "；":  ";",    # 全角分号
    "，":  ",",    # 全角逗号
    "：":  ":",    # 全角冒号
    "（":  "(",    # 全角左括号
    "）":  ")",    # 全角右括号
    "。":  ".",    # 全角句号
    "！":  "!",    # 全角感叹号
    "？":  "?",    # 全角问号
    "“":  '"',    # 全角左引号
    "”":  '"',    # 全角右引号
    "　":  " ",    # 全角空格
}


def fix_template(body: str) -> tuple[str, list[str]]:
    fixes = []

    # A. 硬编码人名
    for old, new in HARDCODED_NAMES.items():
        if old in body:
            n = body.count(old)
            body = body.replace(old, new)
            fixes.append(f"name: {old!r} → {new!r} × {n}")

    # B. 全角符号
    for cjk, asc in CJK_PUNCT_FIXES.items():
        if cjk in body:
            n = body.count(cjk)
            body = body.replace(cjk, asc)
            fixes.append(f"punct: {cjk!r} → {asc!r} × {n}")

    return body, fixes


def main() -> int:
    if not DEFAULT_XLSX.exists():
        print(f"[ERR] {DEFAULT_XLSX} not found")
        return 1

    wb = load_workbook(DEFAULT_XLSX, data_only=True)
    ws = wb.active

    n_patched = 0
    total_fixes = 0
    rows_to_write: list[tuple[int, str]] = []

    for r_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=False), start=2):
        cat = row[0].value or ""
        tmpl = row[1].value or ""
        desc = row[2].value or ""
        if not tmpl or "v5:" not in desc:
            continue
        new_tmpl, fixes = fix_template(tmpl)
        if fixes:
            rows_to_write.append((r_idx, new_tmpl))
            n_patched += 1
            total_fixes += len(fixes)
            print(f"  [row {r_idx}] {cat[:30]:30s} → {fixes}")

    # 写回
    for r_idx, new_tmpl in rows_to_write:
        ws.cell(row=r_idx, column=2).value = new_tmpl
    wb.save(DEFAULT_XLSX)
    print(f"\n[patch] wrote {n_patched} rows, {total_fixes} fixes total → {DEFAULT_XLSX.name}")

    # 验证
    print("\n=== verification ===")
    from agent.template_reply import load_xlsx
    templates = load_xlsx()
    v5_only = [t for t in templates if 'v5:' in (t.get('description') or '')]

    # 1. 硬编码人名残留
    NAME_RE = re.compile(r"\b(?:Hi|Dear|Hello|Hey)\s+([A-Z][a-z]{1,15})\b")
    name_residue = []
    for t in v5_only:
        m = NAME_RE.search(t['template'])
        if m and m.group(1).lower() not in {'customer','team','sir','madam','there'}:
            name_residue.append((t['intent'], m.group(1), t['template'][:60]))
    print(f"  hardcoded name residue: {len(name_residue)}")
    for r in name_residue:
        print(f"    {r}")

    # 2. CJK 残留
    CJK_RE = re.compile(r'[一-鿿　-〿＀-￯]')
    cjk_residue = [t for t in v5_only if CJK_RE.search(t['template'])]
    print(f"  CJK residue: {len(cjk_residue)}")
    for t in cjk_residue[:3]:
        idx = CJK_RE.search(t['template']).start()
        print(f"    [{t['intent']}]  ...{t['template'][max(0,idx-30):idx+30]!r}...")

    # 3. 模板开头全部应是 {{salutation}}, 或 Dear customer,
    non_salutation = []
    for t in v5_only:
        head = t['template'].lstrip()[:30]
        if not head.startswith('{{salutation}}') and not head.startswith('Dear customer'):
            non_salutation.append((t['intent'], head))
    print(f"  templates NOT starting with {{{{salutation}}}} or 'Dear customer,': {len(non_salutation)}")
    for r in non_salutation:
        print(f"    {r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())