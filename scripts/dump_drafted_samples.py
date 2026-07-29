"""Re-run the 10 drafted samples from v9 eval and dump draft subject + body
for manual qualitative review.

Usage:
  python3 scripts/dump_drafted_samples.py \
      --samples eval/samples_real_seed.json \
      --v9-report eval/runs/v9_real_full.json \
      --out eval/runs/v9_drafts_for_review.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# load .env before importing agent (AzureLLMClient needs env vars at import)
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.example", override=False)
except ImportError:
    pass

from agent.email_agent import EmailAgent, NeedsHumanEscalation  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--samples", required=True, type=Path)
    p.add_argument("--v9-report", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()

    # Load drafted sample IDs from v9 report
    report = json.load(open(args.v9_report))
    drafted_ids = {o["sample_id"] for o in report["outcomes"] if not o["escalated"]}
    print(f"[drafts] {len(drafted_ids)} drafted samples to re-run: {sorted(drafted_ids)}")

    samples = json.load(open(args.samples))
    samples = [s for s in samples if s["id"] in drafted_ids]
    print(f"[drafts] {len(samples)} matched in samples file")

    agent = EmailAgent()

    results = []
    t_total = time.time()
    for i, s in enumerate(samples, 1):
        t0 = time.time()
        try:
            res = agent.handle_email(s["email"], sender_hint=s.get("meta", {}).get("sender_hash"))
            draft = res.draft
            results.append({
                "sample_id": s["id"],
                "truth": s.get("truth"),
                "triage_pred": res.triage.model_dump() if hasattr(res.triage, "model_dump") else res.triage.__dict__,
                "draft_subject": getattr(draft, "subject_md", "") or getattr(draft, "subject", ""),
                "draft_body": getattr(draft, "body_md", "") or getattr(draft, "body", ""),
                "compliance_passed": res.compliance.verdict.pass_ if hasattr(res, "compliance") else None,
                "latency_ms": int((time.time() - t0) * 1000),
            })
            print(f"[drafts] [{i}/{len(samples)}] {s['id']} OK  latency={int((time.time()-t0)*1000)}ms")
        except NeedsHumanEscalation as e:
            results.append({
                "sample_id": s["id"],
                "escalated": True,
                "reason": str(e),
                "latency_ms": int((time.time() - t0) * 1000),
            })
            print(f"[drafts] [{i}/{len(samples)}] {s['id']} ESCALATED: {str(e)[:120]}")
        except Exception as e:
            results.append({
                "sample_id": s["id"],
                "error": f"{type(e).__name__}: {e}",
                "latency_ms": int((time.time() - t0) * 1000),
            })
            print(f"[drafts] [{i}/{len(samples)}] {s['id']} ERROR: {type(e).__name__}: {e}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"[drafts] wrote {args.out}")
    print(f"[drafts] total {time.time()-t_total:.1f}s")


if __name__ == "__main__":
    main()