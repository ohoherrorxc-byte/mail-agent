"""
v5_draft_templates.py — 补 4 个 0 模板 intent 的草稿空缺

目标：ismart_account_inquiry / ismart_owner_change / ismart_digital_key / ismart_data_plan
    （v16 triage 验证后这 4 个 intent 在 148 样本里有 16 条，模板库 0 命中，
    full pipeline 会用相邻 intent 的模板（app_binding 32 条等）兜底，业务匹配差。）

输入：  cs_replies.llm_labeled.jsonl（用 llm_intent + llm_confidence='high' 过滤）
        + scripts/v4_draft_templates.py 的 clean_reply() 清洗规则
输出：  iSMART售后服务邮箱回复方法_v5_templates.xlsx（legacy 3 列 schema）
        + eval/runs/v5_template_audit.json

策略（与 v4 类似但简化）：
1. 4 个目标 intent，llm_confidence='high' 且 reply_len ≥ 300
2. 按 category_raw 桶（CS 自带分类），取 top 5 sub_scenarios by 桶内回复数
3. 每个 sub_scenario 取 5 条中等长度回复（按距 1200 char 的绝对距离排序）
4. 复用 v4 clean_reply() 清洗（closing 切 / quoted history 切 / PII 占位 / 长度校验）
5. 写 legacy 3 列 xlsx → 让 load_xlsx() glob 'v5_*.xlsx' 自动 pickup

依赖：在 agent/template_reply.py 加 4 个新 category → intent 映射
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

from scripts.v4_draft_templates import clean_reply  # noqa: E402

PRIVATE_DIR = Path.home() / "Documents" / "private" / "iSmart-email-ledger"
DEFAULT_JSONL = PRIVATE_DIR / "cs_replies.llm_labeled.jsonl"
DEFAULT_XLSX = PROJECT_ROOT / "iSMART售后服务邮箱回复方法_v5_templates.xlsx"
DEFAULT_AUDIT = PROJECT_ROOT / "eval" / "runs" / "v5_template_audit.json"

# 4 个目标 intent
PRIORITY_INTENTS = (
    "ismart_account_inquiry",
    "ismart_owner_change",
    "ismart_digital_key",
    "ismart_data_plan",
)

# 显式 v5 新 category — 在 CATEGORY_TO_INTENT / L2_TO_INTENT_EXTRA 里登记
# 让 load_xlsx() 能直接路由到 4 个目标 intent（不依赖 category_raw 推断）
INTENT_TO_V5_CATEGORY: dict[str, str] = {
    "ismart_account_inquiry": "iSMART-账号查询",
    "ismart_owner_change":    "iSMART-车主变更",
    "ismart_digital_key":     "iSMART-数字钥匙",
    "ismart_data_plan":       "iSMART-流量套餐",
}

# 每 intent 限取的 sub_scenarios 数
SUBSCENARIOS_PER_INTENT = 5
# 每 sub_scenario 取的代表 CS 回复数
PICKS_PER_SUBSCENARIO = 5


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    ap.add_argument("--out-xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    ap.add_argument("--min-confidence", choices=["high", "medium", "low"], default="high")
    args = ap.parse_args()

    # ── 1. 读 llm_labeled.jsonl，按 llm_intent 桶分类（不按 category_raw） ──
    # 因为原始 category_raw 全都映射到相邻 intent，要让模板落到目标 intent，
    # 必须用 v5 显式新 category（iSMART-账号查询 等）。
    buckets: dict[str, list[dict]] = defaultdict(list)
    n_seen = 0
    n_kept = 0
    with args.jsonl.open() as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_seen += 1
            intent = row.get("llm_intent")
            if intent not in PRIORITY_INTENTS:
                continue
            if args.min_confidence == "high" and row.get("llm_confidence") != "high":
                continue
            reply = row.get("cs_reply") or ""
            if len(reply) < 300:
                continue
            buckets[intent].append({
                "id":         row.get("id"),
                "reply":      reply,
                "region":     row.get("region_derived"),
                "subject":    row.get("subject") or "",
                "raw_category": (row.get("category_raw") or "").strip(),
            })
            n_kept += 1
    print(f"[v5] scanned {n_seen} rows, kept {n_kept} for {len(buckets)} target intents")

    # ── 2. 每 intent 内按"原始 category_raw"二次分桶，给每个 v5 模板标 raw_category 作说明 ──
    # 但 xlsx Category 列写 v5 显式新 category，让 load_xlsx 直接路由
    templates: list[dict] = []
    audit_rows: list[dict] = []

    for intent in PRIORITY_INTENTS:
        rows = buckets.get(intent, [])
        if not rows:
            print(f"\n[v5] {intent}: 0 replies")
            continue
        # 按原始 category 分桶（仅用于 sub_scenario 来源说明，不影响 routing）
        sub_counter: Counter = Counter(r["raw_category"] or "(uncategorized)" for r in rows)
        top_subs = sub_counter.most_common(SUBSCENARIOS_PER_INTENT)
        print(f"\n### {intent}: {len(rows)} replies, {len(sub_counter)} sub_scenarios")

        # 每 sub_scenario 抽 PICKS_PER_SUBSCENARIO 条；不同 sub_scenario 的池合并去重抽取
        seen_ids: set[str] = set()
        picks: list[dict] = []
        for sub_cat, _n in top_subs:
            pool = [r for r in rows if (r["raw_category"] or "(uncategorized)") == sub_cat]
            # 按长度距 1200 排序
            pool.sort(key=lambda r: abs(len(r["reply"]) - 1200))
            for r in pool:
                if r["id"] in seen_ids:
                    continue
                picks.append(r)
                seen_ids.add(r["id"])
                if sum(1 for p in picks if p["raw_category"] == sub_cat) >= PICKS_PER_SUBSCENARIO:
                    break
            if len(picks) >= SUBSCENARIOS_PER_INTENT * PICKS_PER_SUBSCENARIO:
                break

        # 实际清洗入 xlsx
        n_clean = 0
        for pick in picks:
            cleaned = clean_reply(pick["reply"])
            if not cleaned:
                continue
            n_clean += 1
            templates.append({
                # M2.7: 显式 v5 新 category（iSMART-账号查询 等），不再用 raw_category
                "Category":    INTENT_TO_V5_CATEGORY[intent],
                "Template":    cleaned,
                "Description": (
                    f"v5: source=cs-{pick['id']}; region={pick['region']}; "
                    f"intent={intent}; raw_category={pick['raw_category'][:50]}"
                ),
            })
            audit_rows.append({
                "intent":      intent,
                "category":    INTENT_TO_V5_CATEGORY[intent],
                "raw_category": pick["raw_category"],
                "source_id":   pick["id"],
                "region":      pick["region"],
                "cleaned_len": len(cleaned),
            })
        print(f"  picks={len(picks)} cleaned={n_clean}")

    # ── 4. 写 xlsx（legacy 3 列 schema，与 v4 同） ──
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "v5_templates"
    header = ["Category", "Template", "Description"]
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True)

    # Category 清洗：去 AUTO: 前缀 + 关闭-xxx + Region: token
    def _clean_category(cat: str) -> str:
        tokens = [t.strip() for t in re.split(r"[,/]", cat) if t.strip()]
        kept: list[str] = []
        for t in tokens:
            if t.startswith("AUTO:"):
                t = t[5:].strip()
            if t.startswith("关闭-") or t.startswith("Region:") or t.startswith("Negative"):
                continue
            kept.append(t)
        return " / ".join(kept) or cat

    for t in templates:
        cleaned_cat = _clean_category(t["Category"])
        t["Category"] = cleaned_cat
        ws.append([t["Category"], t["Template"], t["Description"]])

    args.out_xlsx.parent.mkdir(parents=True, exist_ok=True)
    wb.save(args.out_xlsx)
    print(f"\n[v5] wrote {len(templates)} templates → {args.out_xlsx}")

    # ── 5. 写 audit ──
    by_intent_counter: dict[str, int] = Counter(t["Category"] for t in templates)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps({
        "n_templates":        len(templates),
        "n_unique_categories": len(by_intent_counter),
        "n_unique_intents":   len(set(a["intent"] for a in audit_rows)),
        "by_intent":          dict(Counter(a["intent"] for a in audit_rows)),
        "by_category":        dict(by_intent_counter),
        "samples":            audit_rows[:30],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[v5] audit → {args.audit}")

    # ── 6. 验证 load_xlsx() 能 pick up ──
    from agent.template_reply import load_xlsx, CATEGORY_TO_INTENT
    all_t = load_xlsx()
    v5_only = [t for t in all_t if "v5:" in (t.get("description") or "")]
    print(f"\n[v5] load_xlsx(): total={len(all_t)}, v5={len(v5_only)}")

    # 关键检查：4 个目标 intent 是否都进入 rows
    intent_counts = Counter(t["intent"] for t in all_t)
    for intent in PRIORITY_INTENTS:
        print(f"  {intent:30s}  total templates after load: {intent_counts[intent]}")

    # 警告：如果 category 不在 CATEGORY_TO_INTENT 里，load 不到
    cats_in_xlsx = set(t["Category"] for t in templates)
    cats_mapped = {c for c in cats_in_xlsx if c in CATEGORY_TO_INTENT}
    cats_unmapped = cats_in_xlsx - cats_mapped
    if cats_unmapped:
        print(f"\n[v5] WARN: {len(cats_unmapped)} categories NOT in CATEGORY_TO_INTENT (would not load):")
        for c in sorted(cats_unmapped)[:10]:
            print(f"  - {c!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())