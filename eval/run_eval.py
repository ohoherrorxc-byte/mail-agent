#!/usr/bin/env python3
"""
run_eval.py — M1 PoC 的批量评测脚本。

加载 eval/samples_seed.json（手写真标签）→ 串 EmailAgent → 对每条计算：
  - intent accuracy / urgency accuracy / region accuracy / language accuracy
  - retrieval recall（cited doc_ids 是否命中 truth）
  - compliance pass rate（first-try 是否通过）
  - 每步骤 token 用量 + cache hit + latency
  - escalation rate（触发 NeedsHumanEscalation 的占比）

产出：
  - JSON 报告（细到每条样本）
  - stdout 摘要表（按意图/紧急度切片）

用法：
    python eval/run_eval.py
    python eval/run_eval.py --samples eval/samples_seed.json --out eval/runs/run_xxx.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.example", override=False)
except ImportError:
    pass

from agent import AzureLLMClient, EmailAgent                # noqa: E402
from agent.email_agent import NeedsHumanEscalation          # noqa: E402
from agent.prompts import TRIAGE_SYSTEM                     # noqa: E402
from agent.schemas import TriageResult                      # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING"),
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
LOG = logging.getLogger("eval")


# ── 单条样本结果 ─────────────────────────────────────────────

@dataclass
class SampleOutcome:
    sample_id: str
    intent_truth: str
    urgency_truth: str
    region_truth: Optional[str] = None

    # triage
    intent_pred: Optional[str] = None
    urgency_pred: Optional[str] = None
    region_pred: Optional[str] = None
    language_pred: Optional[str] = None
    requires_recall_pred: Optional[bool] = None
    requires_warranty_pred: Optional[bool] = None
    # M2.5: 知识库新增字段
    auto_reply_level_pred: Optional[str] = None
    required_info_group_pred: Optional[list[str]] = None

    # retrieval
    cited_doc_ids: list[str] | None = None
    cited_doc_ids_truth: list[str] = None
    retrieval_recall: float = 0.0
    evidence_score: Optional[float] = None

    # compliance
    compliance_passed_first_try: Optional[bool] = None
    rewrite_attempts: int = 0

    # flow
    escalated: bool = False
    escalate_reason: str = ""

    # cost
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_cached_tokens: int = 0

    # latency
    latency_ms: int = 0

    # per-step breakdown for debugging
    audit: dict = None


# ── 评测主循环 ───────────────────────────────────────────────

def run_eval(samples: list[dict], agent: EmailAgent,
             triage_only: bool = False) -> list[SampleOutcome]:
    outcomes: list[SampleOutcome] = []

    for i, s in enumerate(samples, 1):
        sample_id = s["id"]

        # samples_real_seed.json 里可能用 truth=None 标记"抽不到真描述"
        # 的样本（chain 邮件、非模板格式等），跳过 eval
        if s.get("truth") is None:
            LOG.warning("[%d/%d] SKIP %s (truth=null)", i, len(samples), sample_id)
            continue
        truth_t = s["truth"]
        truth_cited = s.get("expected_cited_doc_ids", []) or []

        LOG.warning("[%d/%d] %s", i, len(samples), sample_id)
        out = SampleOutcome(
            sample_id=sample_id,
            intent_truth=truth_t["intent"],
            urgency_truth=truth_t["urgency"],
            region_truth=truth_t.get("region"),
            cited_doc_ids_truth=truth_cited,
        )

        t0 = time.time()
        # triage_only 模式：直接调底层 LLM 跑 Triage，跳过 region gate /
        # retrieval / drafting / compliance ——用于在 M1 还只覆盖 NA 合规
        # 规则时评估 Triage 准确率，不让 NA 规则误审 EU/UK 客户。
        if triage_only:
            try:
                triage, _c = agent.llm.invoke_json(
                    schema=TriageResult,
                    messages=[
                        {"role": "system", "content": TRIAGE_SYSTEM},
                        {"role": "user",   "content": s["email"]},
                    ],
                    reasoning_effort="low",
                    role="triage",
                )
                out.intent_pred            = triage.intent
                out.urgency_pred           = triage.urgency
                out.region_pred            = triage.region
                out.language_pred          = triage.language
                out.requires_recall_pred   = triage.requires_recall_check
                out.requires_warranty_pred = triage.requires_warranty_lookup
                out.auto_reply_level_pred  = triage.auto_reply_level
                out.required_info_group_pred = list(triage.required_info_group or [])
                out.compliance_passed_first_try = None  # 没跑到 compliance
                # red_flags 真值表对照
                out.escalated = bool(triage.red_flags)
                out.escalate_reason = f"triage_only: red_flags={triage.red_flags}"
                out.audit = {"triage_only": True, "calls": [_as_audit(_c)]}
                _accumulate_tokens(out, _c)
                out.latency_ms = int((time.time() - t0) * 1000)
                outcomes.append(out)
                continue
            except Exception as e:
                LOG.exception("[%s] triage_only failed: %s", sample_id, e)
                out.escalate_reason = f"unexpected:{type(e).__name__}"
                out.latency_ms = int((time.time() - t0) * 1000)
                outcomes.append(out)
                continue
        try:
            res = agent.handle_email(s["email"])
        except NeedsHumanEscalation as e:
            out.escalated = True
            out.escalate_reason = e.reason
            out.audit = (e.partial or {})
            # Triage 即使在 retrieval/drafting 步骤 escalate 时也已经跑完，
            # 把它从 partial 拿出来，否则 85% 样本的 Triage 准确率都看不见。
            partial = e.partial or {}
            triage_obj = partial.get("triage")
            if isinstance(triage_obj, dict):
                out.intent_pred            = triage_obj.get("intent")
                out.urgency_pred           = triage_obj.get("urgency")
                out.region_pred            = triage_obj.get("region")
                out.language_pred          = triage_obj.get("language")
                out.requires_recall_pred   = triage_obj.get("requires_recall_check")
                out.requires_warranty_pred = triage_obj.get("requires_warranty_lookup")
                out.auto_reply_level_pred  = triage_obj.get("auto_reply_level")
                out.required_info_group_pred = triage_obj.get("required_info_group") or []
            # retrieval 的 cited_clauses 同样可能在 partial 里
            retrieval_obj = partial.get("retrieval")
            if isinstance(retrieval_obj, dict):
                out.evidence_score = retrieval_obj.get("evidence_score")
                clauses = retrieval_obj.get("cited_clauses") or []
                if isinstance(clauses, list) and clauses:
                    out.cited_doc_ids = clauses
        except Exception as e:
            LOG.exception("[%s] unexpected: %s", sample_id, e)
            out.escalated = True
            out.escalate_reason = f"unexpected:{type(e).__name__}"
        else:
            out.intent_pred           = res.triage.intent
            out.urgency_pred          = res.triage.urgency
            out.region_pred           = res.triage.region
            out.language_pred         = res.triage.language
            out.requires_recall_pred  = res.triage.requires_recall_check
            out.requires_warranty_pred= res.triage.requires_warranty_lookup
            out.auto_reply_level_pred = res.triage.auto_reply_level
            out.required_info_group_pred = list(res.triage.required_info_group or [])

            out.cited_doc_ids   = res.retrieval.cited_clauses  # 可能在 escalated False 时为 []
            cited = {c["doc_id"] if isinstance(c, dict) else c.doc_id
                     for c in res.retrieval.cited_clauses}
            truth_set = set(truth_cited)
            out.retrieval_recall = (
                len(cited & truth_set) / len(truth_set) if truth_set else 1.0
            )
            out.evidence_score  = res.retrieval.evidence_score

            out.compliance_passed_first_try = res.compliance.verdict.pass_
            out.rewrite_attempts = res.audit.get("rewrite_attempts", 0)
            out.audit = res.audit

        out.latency_ms = int((time.time() - t0) * 1000)

        # token 统计从 audit 里抽
        if isinstance(out.audit, dict) and "calls" in out.audit:
            for call in out.audit["calls"]:
                out.total_prompt_tokens     += call.get("prompt_tokens", 0)
                out.total_completion_tokens += call.get("completion_tokens", 0)
                out.total_reasoning_tokens  += call.get("reasoning_tokens", 0)
                out.total_cached_tokens     += call.get("cached_tokens", 0)

        outcomes.append(out)

    return outcomes


# ── 聚合指标 ────────────────────────────────────────────────

def aggregate(outcomes: list[SampleOutcome]) -> dict:
    n = len(outcomes)
    if n == 0:
        return {"n": 0}

    def safe_ratio(num: int, den: int) -> float:
        return round(num / den, 4) if den else 0.0

    escalated = [o for o in outcomes if o.escalated]
    not_escalated = [o for o in outcomes if not o.escalated]

    # intent / urgency accuracy 只在 not_escalated 上算——上升样本是路由正确，
    # 算"miss"是把对的当错的。但 intent_breakdown 会按真意图切片。
    intent_hits = sum(o.intent_pred == o.intent_truth for o in not_escalated)
    urgency_hits = sum(o.urgency_pred == o.urgency_truth for o in not_escalated)
    # region accuracy：用真值 region 而不是固定 "NA"（samples_real_seed.json 多数是 EU/AP）
    region_hits = sum(o.region_pred == o.region_truth for o in not_escalated if o.region_pred)
    compliance_ok = sum(1 for o in not_escalated
                        if o.compliance_passed_first_try is True)

    # routing accuracy：上升样本里包含 red_flag 的算 "routing correct"
    routing_correct = 0
    for o in outcomes:
        if o.escalated:
            # 仅在真标签也有 red_flag 时算正确上升
            truth_red_flags = (o.audit or {}).get("red_flags_truth")
            # 注：truth.red_flags 不在 outcomes 上携带；这里用一种保守方式——
            # 上升＋最终 hit 红牌的样本都算 "routing-correct"。
            if "red_flags detected" in o.escalate_reason:
                routing_correct += 1

    # 按意图切片
    by_intent = defaultdict(list)
    for o in outcomes:
        by_intent[o.intent_truth].append(o)

    intent_breakdown = {}
    for intent, items in by_intent.items():
        # 在该意图子集里，intent_pred == truth 视为 hit
        hits = sum(o.intent_pred == intent for o in items if not o.escalated)
        escalated_count = sum(1 for o in items if o.escalated)
        n_total = len(items)
        intent_breakdown[intent] = {
            "n":                  n_total,
            "n_drafted":          n_total - escalated_count,
            "n_escalated":        escalated_count,
            "triage_accuracy_on_drafted": round(hits / (n_total - escalated_count), 4)
                                                 if (n_total - escalated_count) else None,
        }

    # 按紧急度切片
    by_urgency = defaultdict(list)
    for o in outcomes:
        by_urgency[o.urgency_truth].append(o)
    urgency_breakdown = {}
    for u, items in by_urgency.items():
        hits = sum(o.urgency_pred == u for o in items if not o.escalated)
        escalated_count = sum(1 for o in items if o.escalated)
        n_total = len(items)
        urgency_breakdown[u] = {
            "n":                              n_total,
            "n_drafted":                      n_total - escalated_count,
            "n_escalated":                    escalated_count,
            "urgency_accuracy_on_drafted":   round(hits / (n_total - escalated_count), 4)
                                                 if (n_total - escalated_count) else None,
        }

    # 按 region 切片 — 衡量 region gate 的精度：
    #   真值=NA → 期望 drafted；
    #   真值≠NA → 期望 escalated（被 gate 拦下）。
    by_region = defaultdict(list)
    for o in outcomes:
        by_region[o.region_truth or "NA"].append(o)
    region_breakdown = {}
    for r, items in by_region.items():
        escalated_count = sum(1 for o in items if o.escalated)
        drafted_count   = len(items) - escalated_count
        # gate accuracy: 真值=NA 应 drafted；真值≠NA 应 escalated
        if r == "NA":
            gate_correct = drafted_count
        else:
            gate_correct = escalated_count
        region_breakdown[r] = {
            "n":              len(items),
            "n_drafted":      drafted_count,
            "n_escalated":    escalated_count,
            "region_gate_acc": round(gate_correct / len(items), 4),
        }

    retrieval_recalls = [o.retrieval_recall for o in outcomes
                         if o.cited_doc_ids is not None and o.cited_doc_ids_truth]
    mean_recall = round(statistics.mean(retrieval_recalls), 4) if retrieval_recalls else 0.0

    prompt_tokens     = [o.total_prompt_tokens     for o in outcomes]
    completion_tokens = [o.total_completion_tokens for o in outcomes]
    reasoning_tokens  = [o.total_reasoning_tokens  for o in outcomes]
    cached_tokens     = [o.total_cached_tokens     for o in outcomes]
    latencies         = [o.latency_ms              for o in outcomes]

    return {
        "n_samples":                  n,
        "n_escalated":                len(escalated),
        "n_drafted":                  len(not_escalated),
        # 只在 drafted 样本上算 triage 准确率
        "intent_accuracy_on_drafted": safe_ratio(intent_hits, len(not_escalated)),
        "urgency_accuracy_on_drafted": safe_ratio(urgency_hits, len(not_escalated)),
        "region_accuracy_on_drafted": safe_ratio(region_hits, len(not_escalated)),
        "retrieval_recall_mean":      mean_recall,
        "compliance_first_try_rate":  safe_ratio(compliance_ok, len(not_escalated)),
        "escalation_correct_rate":    safe_ratio(routing_correct, len(escalated)),
        "escalation_rate":            safe_ratio(len(escalated), n),
        "intent_breakdown":           intent_breakdown,
        "urgency_breakdown":          urgency_breakdown,
        "region_breakdown":           region_breakdown,
        "cost": {
            "prompt_tokens_total":     sum(prompt_tokens),
            "completion_tokens_total": sum(completion_tokens),
            "reasoning_tokens_total":  sum(reasoning_tokens),
            "cached_tokens_total":     sum(cached_tokens),
            "mean_prompt_per_email":   round(statistics.mean(prompt_tokens), 1),
            "mean_completion_per_email": round(statistics.mean(completion_tokens), 1),
            "mean_reasoning_per_email": round(statistics.mean(reasoning_tokens), 1),
            "cache_hit_rate_overall":  safe_ratio(sum(cached_tokens), sum(prompt_tokens) or 1),
        },
        "latency": {
            "mean_ms":         round(statistics.mean(latencies), 1),
            "median_ms":       round(statistics.median(latencies), 1),
            "p95_ms":          round(_percentile(latencies, 0.95), 1),
            "max_ms":          max(latencies),
        },
    }


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(p * (len(xs) - 1))))
    return xs[k]


def _as_audit(c) -> dict:
    """从 LLMCall 抽出 audit dict（与 email_agent._as_audit 同形）"""
    return {
        "role":              c.role,
        "model":             c.model,
        "reasoning_effort":  c.reasoning_effort,
        "prompt_tokens":     c.usage.prompt_tokens,
        "completion_tokens": c.usage.completion_tokens,
        "cached_tokens":     c.usage.cached_tokens,
        "reasoning_tokens":  c.usage.reasoning_tokens,
        "cache_hit_rate":    round(c.usage.cache_hit_rate, 3),
    }


def _accumulate_tokens(out: SampleOutcome, c) -> None:
    out.total_prompt_tokens     += c.usage.prompt_tokens
    out.total_completion_tokens += c.usage.completion_tokens
    out.total_reasoning_tokens  += c.usage.reasoning_tokens
    out.total_cached_tokens     += c.usage.cached_tokens


# ── 输出 ────────────────────────────────────────────────────

def print_summary(metrics: dict) -> None:
    print()
    print("=" * 70)
    print(f"EVAL SUMMARY  (n={metrics['n_samples']}, drafted={metrics['n_drafted']}, "
          f"escalated={metrics['n_escalated']})")
    print("=" * 70)
    print(f"  intent accuracy (drafted)         "
          f"{metrics['intent_accuracy_on_drafted']*100:>6.2f}%")
    print(f"  urgency accuracy (drafted)        "
          f"{metrics['urgency_accuracy_on_drafted']*100:>6.2f}%")
    print(f"  retrieval recall (mean)           "
          f"{metrics['retrieval_recall_mean']*100:>6.2f}%")
    print(f"  compliance 1st-try rate (drafted) "
          f"{metrics['compliance_first_try_rate']*100:>6.2f}%")
    print(f"  escalation rate                   "
          f"{metrics['escalation_rate']*100:>6.2f}%")
    if metrics['n_escalated']:
        print(f"  escalation routing-correct rate   "
              f"{metrics['escalation_correct_rate']*100:>6.2f}%")
    print()
    print("  by intent:")
    for k, v in sorted(metrics["intent_breakdown"].items()):
        acc = v["triage_accuracy_on_drafted"]
        acc_str = f"{acc*100:>5.1f}%" if acc is not None else "  n/a"
        print(f"    {k:<22}  n={v['n']:<2}  drafted={v['n_drafted']:<2}  "
              f"escalated={v['n_escalated']:<2}  acc={acc_str}")
    print()
    print("  by urgency:")
    for k, v in sorted(metrics["urgency_breakdown"].items()):
        acc = v["urgency_accuracy_on_drafted"]
        acc_str = f"{acc*100:>5.1f}%" if acc is not None else "  n/a"
        print(f"    {k:<22}  n={v['n']:<2}  drafted={v['n_drafted']:<2}  "
              f"escalated={v['n_escalated']:<2}  acc={acc_str}")
    print()
    # 注意：region_breakdown 不在 summary 里打。
    # 原因 — region_pred 是模型判的，region_truth 是我手工标的（粗分），
    # 两边的 enum 不一致（模型 5 区销售口径 vs 我 UK/AP/AU/EU/NA 粗分）。
    # 在 iSmart 没有官方销售区定义前，gate_acc 没锚点，会误导。
    # 完整 region_pred / region_truth 仍在 outcomes JSON 里供后续接 iSmart
    # 业务系统后做对齐。
    c = metrics["cost"]
    print(f"  cost per email (mean):  prompt={c['mean_prompt_per_email']:.0f} "
          f"completion={c['mean_completion_per_email']:.0f} "
          f"reasoning={c['mean_reasoning_per_email']:.0f} "
          f"cache_hit={c['cache_hit_rate_overall']*100:.1f}%")
    print()
    l = metrics["latency"]
    print(f"  latency:  mean={l['mean_ms']:.0f}ms  median={l['median_ms']:.0f}ms  "
          f"p95={l['p95_ms']:.0f}ms  max={l['max_ms']:.0f}ms")
    print()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--samples", default=str(ROOT / "eval" / "samples_seed.json"))
    p.add_argument("--out",     default=None,
                   help="写 JSON 报告到这里；默认不写")
    p.add_argument("--limit",   type=int, default=None,
                   help="只跑前 N 条；调试用")
    p.add_argument("--triage-only", action="store_true",
                   help="只跑 Triage 步骤就停；用于在 M1 还没扩合规规则前"
                        "评估 Triage 准确率，不让 NA 规则误审 EU/UK 客户")
    args = p.parse_args()

    with open(args.samples, encoding="utf-8") as f:
        samples = json.load(f)
    if args.limit:
        samples = samples[:args.limit]
    print(f"[eval] {len(samples)} samples")

    try:
        client = AzureLLMClient()
    except KeyError as e:
        print(f"[ERR] missing env {e}", file=sys.stderr)
        return 3

    agent = EmailAgent(llm=client)
    outcomes = run_eval(samples, agent, triage_only=args.triage_only)
    metrics = aggregate(outcomes)
    if args.triage_only:
        print("[eval] TRIAGE-ONLY mode: skipping region gate / retrieval / drafting / compliance")
    print_summary(metrics)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "metrics": metrics,
            "outcomes": [o.__dict__ for o in outcomes],
        }
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"[eval] wrote report to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
