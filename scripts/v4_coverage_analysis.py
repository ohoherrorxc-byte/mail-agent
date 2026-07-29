"""
v4_coverage_analysis.py — 比较现有 77 xlsx 模板 vs 33k CS 回复的覆盖差距

输入：
  - iSMART售后服务邮箱回复方法_20260308.xlsx   (现网 77 条)
  - ~/Documents/private/iSmart-email-ledger/cs_replies.redacted.jsonl   (42k CS 回复)

输出：
  - eval/runs/v4_template_coverage_gap.json
  含每 Intent 的：
    n_xlsx_templates, n_cs_replies, gap=top cs_replies - existing templates,
    top_sub_scenarios=[{category_raw, count, sample_subjects}],
    sample_replies=[{cs_reply_index, snippet, region_derived}]

为下面 cluster + 起草 v4 模板准备数据。

Usage:
  python3 scripts/v4_coverage_analysis.py [--top-n 50]
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

from agent.template_reply import load_xlsx  # noqa: E402

PRIVATE_DIR = Path.home() / "Documents" / "private" / "iSmart-email-ledger"
DEFAULT_JSONL = PRIVATE_DIR / "cs_replies.redacted.jsonl"
DEFAULT_REPORT = PROJECT_ROOT / "eval" / "runs" / "v4_template_coverage_gap.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--top-n", type=int, default=10,
                    help="每个 Intent 列出 top N sub-scenarios / sample replies")
    args = ap.parse_args()

    # Load existing 77 xlsx templates
    templates = load_xlsx()
    by_intent_count = Counter(t["intent"] for t in templates)
    print(f"loaded {len(templates)} templates from xlsx (v2/v3 + 20260308)")
    print(f"covered intents: {len(by_intent_count)}")

    # Stream CS replies
    by_intent_cat: dict[tuple[str, str], Counter] = defaultdict(Counter)
    by_intent_subjects: dict[tuple[str, str], list[str]] = defaultdict(list)
    by_intent_sample_reply: dict[tuple[str, str], dict] = {}
    intent_total = Counter()
    region_by_intent = defaultdict(Counter)

    n_read = 0
    n_used = 0
    with args.jsonl.open() as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_read += 1
            intent = row.get("intent_mapped")
            cat_raw = (row.get("category_raw") or "").strip()
            reply = row.get("cs_reply") or ""
            subject = row.get("subject") or ""
            region = row.get("region_derived")
            if not intent or len(reply) < 200:
                continue
            n_used += 1
            intent_total[intent] += 1
            region_by_intent[intent][region or "?"] += 1
            if cat_raw:
                by_intent_cat[(intent, cat_raw)][cat_raw] += 1
                subjs = by_intent_subjects[(intent, cat_raw)]
                if len(subjs) < 5:
                    subjs.append(subject[:80])
                if (intent, cat_raw) not in by_intent_sample_reply:
                    by_intent_sample_reply[(intent, cat_raw)] = {
                        "subject": subject,
                        "reply_snippet": reply[:600],
                        "region": region,
                    }

    # Compute gap analysis
    intent_report = {}
    for intent in sorted(set(list(by_intent_count) + list(intent_total))):
        n_xlsx = by_intent_count.get(intent, 0)
        n_cs = intent_total.get(intent, 0)
        # sub-scenarios = distinct category_raw for this intent
        sub_buckets = [
            ((i, c), cnt)
            for (i, c), cnt in by_intent_cat.items()
            if i == intent
        ]
        sub_buckets.sort(key=lambda x: -sum(x[1].values()))
        top_subs = []
        for (i, c), cnt in sub_buckets[:args.top_n]:
            top_subs.append({
                "category_raw": c,
                "count": sum(cnt.values()),
                "sample_subjects": by_intent_subjects.get((i, c), [])[:3],
                "sample_reply": by_intent_sample_reply.get((i, c), {}),
                "regions": dict(region_by_intent[intent]),
            })
        intent_report[intent] = {
            "n_xlsx_templates": n_xlsx,
            "n_cs_replies":     n_cs,
            "gap":              max(0, n_cs // 50 - n_xlsx),  # 50:1 reduction ratio target
            "top_sub_scenarios": top_subs,
        }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "summary": {
            "n_xlsx_templates": len(templates),
            "n_cs_replies_used": n_used,
            "intents_total": len(intent_total),
            "intents_with_gap": sum(1 for r in intent_report.values() if r["gap"] > 0),
        },
        "by_intent": intent_report,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # Print summary table
    print()
    print(f"{'Intent':<32s}  {'xlsx':>5s}  {'cs':>6s}  {'gap':>5s}  top sub-scenarios")
    print("-" * 100)
    for intent in sorted(intent_report, key=lambda k: -intent_report[k]["n_cs_replies"]):
        r = intent_report[intent]
        subs = ", ".join(s["category_raw"] for s in r["top_sub_scenarios"][:3])[:40]
        print(f"  {intent:<32s}  {r['n_xlsx_templates']:>5d}  {r['n_cs_replies']:>6d}  {r['gap']:>5d}  {subs}")
    print(f"\nwritten: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
