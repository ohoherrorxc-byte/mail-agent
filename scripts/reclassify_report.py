"""Generate the AUTO-vs-LLM report from cs_replies.llm_labeled.jsonl.
Re-runnable; useful for interim reports while the full run is still going."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PRIVATE_DIR = Path.home() / "Documents" / "private" / "iSmart-email-ledger"
DEFAULT_INPUT = PRIVATE_DIR / "cs_replies.llm_labeled.jsonl"
DEFAULT_REPORT = PROJECT_ROOT / "eval" / "runs" / "cs_reply_llm_vs_auto.json"


def main():
    if not DEFAULT_INPUT.exists():
        print(f"{DEFAULT_INPUT} not found; nothing to report")
        return 1

    auto_intents: list[str] = []
    llm_intents: list[str] = []
    confidence_counter: Counter = Counter()

    with DEFAULT_INPUT.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("llm_intent") and rec.get("intent_mapped"):
                auto_intents.append(rec["intent_mapped"])
                llm_intents.append(rec["llm_intent"])
                confidence_counter[rec.get("llm_confidence", "?")] += 1

    n = len(auto_intents)
    if n == 0:
        print(f"no records with both intent_mapped and llm_intent in {DEFAULT_INPUT}")
        return 1

    cm: dict[tuple[str, str], int] = Counter()
    for a, l in zip(auto_intents, llm_intents):
        cm[(a, l)] += 1

    matches = sum(c for (a, l), c in cm.items() if a == l)
    accuracy = matches / n

    by_auto: dict[str, Counter] = {}
    for (a, l), c in cm.items():
        by_auto.setdefault(a, Counter())[l] += c

    auto_perf = []
    for a, c in by_auto.items():
        n_a = sum(c.values())
        match_a = c.get(a, 0)
        acc_a = match_a / n_a
        alts = [(l, cnt) for l, cnt in c.most_common() if l != a]
        top_alt = alts[0] if alts else None
        auto_perf.append({
            "auto_intent": a,
            "n": n_a,
            "matches": match_a,
            "accuracy": round(acc_a, 3),
            "disagreement_rate": round(1 - acc_a, 3),
            "top_llm_alt": top_alt[0] if top_alt else None,
            "top_llm_alt_count": top_alt[1] if top_alt else 0,
        })
    auto_perf.sort(key=lambda x: (-x["disagreement_rate"], -x["n"]))
    worst_auto = [a for a in auto_perf if a["n"] >= 5 and a["disagreement_rate"] >= 0.5]

    llm_dist = Counter(llm_intents)
    auto_dist = Counter(auto_intents)

    DEFAULT_REPORT.write_text(json.dumps({
        "summary": {
            "n_compared": n,
            "auto_llm_matches": matches,
            "auto_llm_disagreement_rate": round(1 - accuracy, 4),
            "note": "preliminary — full run still in progress",
        },
        "llm_confidence_distribution": dict(confidence_counter),
        "llm_intent_distribution": dict(llm_dist.most_common()),
        "auto_intent_distribution": dict(auto_dist.most_common()),
        "auto_intent_performance": auto_perf,
        "worst_auto_intents": worst_auto,
        "interpretation": (
            "auto_llm_disagreement_rate 高 → AUTO heuristic 错得多。\n"
            "worst_auto_intents 列出 AUTO 错得最离谱的（accuracy<50%, n≥5）；\n"
            "建议：要么修 CATEGORY_TO_INTENT heuristic，要么直接用 llm_intent 覆盖 intent_mapped。\n"
            "LLM confidence='low' 的样本建议人工 review。"
        ),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"report → {DEFAULT_REPORT}")
    print(f"n_compared={n}  matches={matches}  disagreement={1-accuracy:.2%}")
    print(f"top-5 worst AUTO intents:")
    for a in worst_auto[:5]:
        print(f"  {a['auto_intent']:>26s}  n={a['n']:>4d}  acc={a['accuracy']:.0%}  →  LLM alt: {a['top_llm_alt']} ({a['top_llm_alt_count']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())