"""One-shot bridge: _samples_to_label.json → eval-compatible samples JSON.

Why needed:
  eval/run_eval.py 期望 {id, email, truth:{...}, meta, expected_cited_doc_ids}
  sample_ledger.py 输出的是 {sample_id, body_excerpt, labels:{intent_truth,...}, ...}
  字段名 / 嵌套 / 字段缺失都对不上；本脚本做一次 transform。

注意：sample_ledger 输出 body_excerpt 是 truncated (per-bucket sample)
      eval 需要完整 email body 才能 triage；本脚本只是给 demo 用，真实流
      程应该是从 candidates.jsonl 重新读完整 body_text_redacted。
"""
import json
import sys
from pathlib import Path

def main(pool_path: str = "eval/_samples_to_label.json",
         out_path: str = "eval/_samples_eval_demo.json") -> int:
    pool = json.loads(Path(pool_path).read_text(encoding="utf-8"))
    if not isinstance(pool, list):
        print(f"[ERR] {pool_path} 不是 list", file=sys.stderr)
        return 2

    out = []
    for r in pool:
        labels = r.get("labels") or {}
        email_text = (
            f"Subject: {r.get('subject_redacted', '')}\n"
            f"From: {r.get('sender_display_name') or r.get('sender_redacted', '')}\n"
            f"Received: {r.get('received_at', '')}\n\n"
            f"{r.get('body_excerpt', '')}"
        )
        out.append({
            "id":   r["sample_id"],
            "email": email_text,
            "truth": {
                "intent":      labels.get("intent_truth"),
                "sub_intent":  labels.get("sub_intent_truth"),  # M2.5 新增
                "urgency":     labels.get("urgency_truth"),
                "region":      labels.get("region_truth"),
                "language":    "en",
                "country":     labels.get("country_truth") or "",
                "requires_recall_check": False,
                "requires_warranty_lookup": False,
                "red_flags": [],
            },
            "meta": {
                "notes": f"harvest v1; inferred={r.get('inferred_intent')}/"
                          f"{r.get('inferred_sub_intent')}; "
                          f"source={r.get('inferred_source')}",
                "source": "harvest_samples.py demo",
                "received_at": r.get("received_at"),
                "mailbox":     r.get("mailbox"),
            },
            "expected_cited_doc_ids": [],
        })

    Path(out_path).write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[bridge] transformed {len(out)} records → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]) if len(sys.argv) > 1 else main())