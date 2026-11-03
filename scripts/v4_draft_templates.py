"""
v4_draft_templates.py — 从 CS 回复语料抽取高质量 v4 模板

输入：  cs_replies.redacted.jsonl + v4_template_coverage_gap.json
输出：  iSMART售后服务邮箱回复方法_v4_templates.xlsx
        + eval/runs/v4_template_audit.json

策略：
1. 读 coverage gap 报告，挑 gap > 0 的 Intent
2. 每个 Intent 取 top 3 sub_scenarios（by 客服回复 count）
3. 每个 sub_scenario 选 3-5 个代表 CS 回复（中位数长度）+ 重新切割 quoted history
4. 清洗：去重、去掉 salutation/签名/PII、保留核心诊断或操作步骤
5. 写 v4 xlsx（schema 同 20260308.xlsx：Category | Template | Description）
   - Category 沿用原 category_raw（如 "FICM-网络相关"）→ 让 CATEGORY_TO_INTENT 自动路由
   - Template 含 placeholders ({vehicle_model}, {dealer_name}, {feature})
   - Description 标 "v4: source=cs-NNNNN; sub_scenario=FICM-网络相关; n_supporting_replies=20"
6. 无 LLM 调用（pure data 转换；CS 回复已是英文，可直接套）

v4 模板会自动被 [agent/template_reply.py:load_xlsx()] 加载（glob "v4_*.xlsx"），
    同 intent + 同 template 文本去重时 v4 优先（同 v2/v3 优先级逻辑）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.template_reply import CATEGORY_TO_INTENT  # noqa: E402

PRIVATE_DIR = Path.home() / "Documents" / "private" / "iSmart-email-ledger"
DEFAULT_JSONL = PRIVATE_DIR / "cs_replies.redacted.jsonl"
DEFAULT_GAP_REPORT = PROJECT_ROOT / "eval" / "runs" / "v4_template_coverage_gap.json"
DEFAULT_XLSX = PROJECT_ROOT / "iSMART售后服务邮箱回复方法_v4_templates.xlsx"
DEFAULT_AUDIT = PROJECT_ROOT / "eval" / "runs" / "v4_template_audit.json"

# 想重点扩的 Intent 集合
PRIORITY_INTENTS = [
    "ismart_connectivity",      # 0 templates
    "ismart_user_manual",       # 1 template
    "ismart_privacy",           # 1 template
    "ismart_region_support",    # 3 templates
    "vehicle_incompatible",     # 1 template
    "app_vehicle_control",      # 6 templates
    "app_binding",              # 9 templates
    "app_account",              # 7 templates
    "hardware_warranty",        # 7 templates
]
# 每个 sub_scenario 取的代表 CS 回复数
PICKS_PER_SUBSCENARIO = 4
# 每个 Intent 限制的 sub_scenarios 数
SUBSCENARIOS_PER_INTENT = 4

# 客服 reply 切割 / 清洗规则 ─────────────────────────────────────

# 任意 closing 边界（保留到 closing 为止）
_CLOSING_MARKERS = (
    r"Best regards,?\s*\n?\s*[Ii]?[Ss]mart[- ]service team",
    r"Best regards,?\s*\n?\s*iSMART Service Team",
    r"Best regards,?\s*\n?\s*[Ii]smart Customer Service",
    r"Best regards,?\s*\n?\s*MG Europe (customer service|CS|support)",
    r"Mit freundlichen Grüßen",
    r"cordiali saluti",
    r"cordialement",
    r"saludos cordiales",
    r"kind regards",
    r"Yours sincerely",
    r"Yours faithfully",
)
_CLOSING_RE = re.compile("|".join(_CLOSING_MARKERS), re.I)

# 上游 quoted-history markers（必须出现在 closing 前 → 不属于 reply）
_QUOTED_MARKERS = (
    r"发件人\s*[：:]\s*",
    r"From\s*:\s",
    r"Von\s*:\s*",
    r"De\s*:\s*",
    r"----+\s*邮件原件\s*----+",
    r"----+\s*Original Message\s*----+",
    r"----+\s*Ursprüngliche Nachricht\s*----+",
)
_QUOTED_RE = re.compile("|".join(_QUOTED_MARKERS), re.I | re.S)

# 去 PII 残留（VIN_MASK_xxx / PHONE_MASK_xxx / user_xxx@example.com 已在 redact 里做了；这里只去其它）
_PII_PATTERNS = (
    (re.compile(r"\bVIN_MASK_[a-f0-9]{8}\b"), "{vin}"),
    (re.compile(r"\bPHONE_MASK_[a-f0-9]{8}\b"), "{phone}"),
    (re.compile(r"\buser_[a-f0-9]{8}@example\.com\b"), "{customer_email}"),
    # 数字长串（车辆 / 订单号）
    (re.compile(r"\b\d{8,}\b"), "{ticket_id}"),
)


def clean_reply(text: str) -> str | None:
    """清洗 CS reply → 可用作 v4 模板。

    Returns None if text doesn't look clean enough to template.
    """
    if not text:
        return None

    # 1. 在 closing marker 处切
    m = _CLOSING_RE.search(text)
    if m:
        text = text[: m.end()]
    else:
        # 没有 closing → 太短或不像 reply
        # 但有些 reply 没显式 closing 但仍有内容 → 切到 quoted history
        m2 = _QUOTED_RE.search(text)
        if m2:
            text = text[: m2.start()]
        else:
            # 完全没有 boundary → 保留整段
            pass

    # 2. 去空白 edge
    text = text.strip()
    if len(text) < 200:
        return None
    if len(text) > 2000:
        # 太长可能是 thread 切错的；截到 2000
        text = text[:2000].rsplit(".", 1)[0] + "."

    # 3. PII → placeholders
    for pat, repl in _PII_PATTERNS:
        text = pat.sub(repl, text)

    # 4. 多余空白
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 5. 如果原 reply 是英文，确保清洗后还是英文主体（heuristic）
    ascii_alpha = sum(1 for c in text if c.isascii() and c.isalpha())
    total_alpha = sum(1 for c in text if c.isalpha())
    if total_alpha > 100 and ascii_alpha / total_alpha < 0.6:
        # 外语比例太高（>40%）→ 不太能直接用 → 仅当绝大多数英文可保留
        # 仍保留，万一整段是真外语也能用
        pass

    # 6. 丢弃文本未真正"开头是 CS reply"的脏数据
    # 标准开头：Dear / Hello / Hi / Greetings
    head = text[:60].strip()
    if not re.match(r"^(Dear\s+\w+|Hello\b|Hi\b|Hi\s+there|Greetings|Good\s+(morning|day|afternoon))",
                    head, re.I):
        # 不以 salutation 开头 → 可能是 quoted history 切错或内部转发
        return None

    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    ap.add_argument("--gap-report", type=Path, default=DEFAULT_GAP_REPORT)
    ap.add_argument("--out-xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    args = ap.parse_args()

    gap = json.load(open(args.gap_report))
    intents_to_expand = [i for i in PRIORITY_INTENTS
                         if i in gap["by_intent"] and gap["by_intent"][i]["n_cs_replies"] >= 100]
    print(f"priority intents to expand ({len(intents_to_expand)}):")
    for i in intents_to_expand:
        r = gap["by_intent"][i]
        print(f"  {i:30s}  xlsx={r['n_xlsx_templates']:>2d}  cs={r['n_cs_replies']:>5d}  gap={r['gap']}")

    # Streamp CS replies → bucket by (intent, category_raw)
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with args.jsonl.open() as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            intent = row.get("intent_mapped")
            cat = (row.get("category_raw") or "").strip()
            reply = row.get("cs_reply") or ""
            if not intent or not cat or len(reply) < 300:
                continue
            if intent not in intents_to_expand:
                continue
            buckets[(intent, cat)].append({
                "subject": row.get("subject") or "",
                "reply": reply,
                "region": row.get("region_derived"),
                "id": row.get("id"),
            })

    # 每个 (intent, cat) 取 top PICKS_PER_SUBSCENARIO 条
    # 中位数长度 / 信息密度 proxy
    templates: list[dict] = []
    audit_rows = []
    for intent in intents_to_expand:
        sub_buckets = [
            ((i, c), rows) for (i, c), rows in buckets.items() if i == intent
        ]
        # 每个 category_raw 元素内，取 top PICKS_PER_SUBSCENARIO 条回复
        sub_buckets.sort(key=lambda x: -len(x[1]))
        for (i, c), rows in sub_buckets[:SUBSCENARIOS_PER_INTENT]:
            # 按回复长度排序（中等长度优先，太长是 thread 切错，太短可能不完整）
            rows.sort(key=lambda r: abs(len(r["reply"]) - 1200))
            picks = rows[:PICKS_PER_SUBSCENARIO]
            for pick in picks:
                cleaned = clean_reply(pick["reply"])
                if not cleaned:
                    continue
                templates.append({
                    "Category":    c,
                    "Template":    cleaned,
                    "Description": (
                        f"v4: source=cs-{pick['id']}; region={pick['region']}; "
                        f"intent={intent}; supporting_replies={len(rows)}"
                    ),
                })
                audit_rows.append({
                    "intent":      intent,
                    "category":    c,
                    "source_id":   pick["id"],
                    "region":      pick["region"],
                    "cleaned_len": len(cleaned),
                })

    # 写 xlsx
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "v4_templates"
    header = ["Category", "Template", "Description"]
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True)

    # Category 清洗：去 AUTO: 前缀 + 关闭-xxx status + 多个 category 用 / 连
    # 让 xlsx loader 的 CATEGORY_TO_INTENT 直查命中
    def _clean_category(cat: str) -> str:
        tokens = [t.strip() for t in re.split(r"[,/]", cat) if t.strip()]
        kept: list[str] = []
        for t in tokens:
            if t.startswith("AUTO:"):
                t = t[5:].strip()
            if t.startswith("关闭-") or t.startswith("Region:") or t.startswith("Negative"):
                continue
            kept.append(t)
        # 用 / 连接（xlsx loader 自己的 split('/') 会再次展开）
        return " / ".join(kept) or cat

    for t in templates:
        cleaned_cat = _clean_category(t["Category"])
        t["Category"] = cleaned_cat
        ws.append([t["Category"], t["Template"], t["Description"]])

    args.out_xlsx.parent.mkdir(parents=True, exist_ok=True)
    wb.save(args.out_xlsx)
    print(f"\nwrote {len(templates)} v4 templates → {args.out_xlsx}")

    # 写 audit
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps({
        "n_templates": len(templates),
        "by_intent": dict(Counter(t["Category"] for t in templates)),
        "intent_mapping_check": {
            t["Category"]: CATEGORY_TO_INTENT.get(t["Category"], "MISSING")
            for t in templates[:30]   # sample
        },
        "samples": audit_rows[:20],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"audit → {args.audit}")

    # 验证：reload via template_reply.load_xlsx
    from agent.template_reply import load_xlsx
    all_t = load_xlsx()
    v4_only = [t for t in all_t if "v4:" in (t.get("description") or "")]
    print(f"\nverifying via load_xlsx(): total={len(all_t)} templates, v4={len(v4_only)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
