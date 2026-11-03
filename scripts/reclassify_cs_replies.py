"""
reclassify_cs_replies.py — 用 gpt-5.4 全量重分类 42k CS 回复的 Intent

输入：~/Documents/private/iSmart-email-ledger/cs_replies.redacted.jsonl
      （42,215 条；每条已有 AUTO intent_mapped + region_derived）
输出：
  - ~/Documents/private/iSmart-email-ledger/cs_replies.llm_labeled.jsonl
    （原 jsonl 每行 + llm_intent / llm_confidence / llm_reason）
  - eval/runs/cs_reply_llm_vs_auto.json
    （AUTO vs LLM 准确率 + 32×32 confusion matrix）

策略：
- Batch 10 records per LLM call → ~4222 calls total
- reasoning_effort="low"（分类任务，无须深度推理）
- Cut cs_reply at closing markers（去 quoted history 节省 token）；上限 1500 chars
- Azure prompt cache：system prompt（rubric）固定 → 首次后 cache hit 应 >50%
- Resume support：output 已存在 + 含 llm_intent 字段 → 跳过

为什么不直接复用 v4_draft_templates.clean_reply()？
- clean_reply() 会 reject len<200 + 非 salutation 开头 → 42k 会被砍掉大半
- reclassification 要尽量覆盖所有 → 只 truncate、不 reject

Usage:
  python3 scripts/reclassify_cs_replies.py --batch-size 10 --max-records 100     # 干跑
  python3 scripts/reclassify_cs_replies.py --batch-size 10                       # 全量
  python3 scripts/reclassify_cs_replies.py --resume                              # 跳过已处理
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 加载 env（项目里实际文件名是 env / env.example，不是 .env / .env.example）
from dotenv import load_dotenv  # noqa: E402
for p in (PROJECT_ROOT / "env.example", PROJECT_ROOT / "env"):
    if p.exists():
        load_dotenv(p, override=(p.name == "env"))

from agent.azure_llm import AzureLLMClient, EmptyCompletionError  # noqa: E402
from agent.schemas import Intent  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

PRIVATE_DIR = Path.home() / "Documents" / "private" / "iSmart-email-ledger"
DEFAULT_INPUT = PRIVATE_DIR / "cs_replies.redacted.jsonl"
DEFAULT_OUTPUT = PRIVATE_DIR / "cs_replies.llm_labeled.jsonl"
DEFAULT_REPORT = PROJECT_ROOT / "eval" / "runs" / "cs_reply_llm_vs_auto.json"

LOG = logging.getLogger("reclassify_cs_replies")


# ── 切 quoted history（from v4_draft_templates） ───────────────────────

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


def cut_for_labeling(text: str, max_chars: int = 1500) -> str:
    """Cut at closing/quoted marker, truncate to max_chars. Never reject."""
    if not text:
        return ""
    m = _CLOSING_RE.search(text)
    if m:
        text = text[: m.end()]
    else:
        m2 = _QUOTED_RE.search(text)
        if m2:
            text = text[: m2.start()]
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(".", 1)[0] + "." if "." in text[:max_chars] else text[:max_chars]
    return text


# ── Pydantic schemas ──────────────────────────────────────────────────

Confidence = str  # Literal["high","medium","low"]


class LabeledRecord(BaseModel):
    """LLM 对单条 CS reply 的分类结果。"""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="原 jsonl 记录的 id 字段")
    intent: Intent = Field(..., description="32 值 Intent enum")
    confidence: Confidence = Field(..., description="high/medium/low")
    reason: str = Field(..., description="一句话解释为什么是这个 intent")


class BatchLabelResult(BaseModel):
    """LLM 返回的整批结果。"""
    model_config = ConfigDict(extra="forbid")

    labels: list[LabeledRecord] = Field(..., description="长度 = 输入 records 数")


# ── System prompt（精简版 rubric） ─────────────────────────────────────

# 注意：故意去掉 ROLE_AND_LAWS_NA + COMPLIANCE_CONSTRAINTS 全段，
# 这段对分类无用、且会大幅增加 input token（不被 prompt cache 吃光预算）。
# 同样的精简版可作 cache-friendly system prefix。

RECLASSIFY_SYSTEM = """\
You are an intent classifier for an iSmart after-sales customer-service
email corpus. Your ONLY job is to read a batch of CS reply texts and
assign ONE of the 32 iSmart after-sales intents to each. Do not invent
new intents. Do not explain outside the JSON.

Source taxonomy (from iSMART售后服务邮箱回复方法_20260308.xlsx):

App-side (vehicle / account / binding):
  app_vehicle_control — App shows stale data, error 47/59, vehicle not
                        responding to App commands
  app_account         — login failure / session expired / password reset
                        failed (an actual fault, not a how-to)
  app_binding         — cannot bind / QR not displayed / second-hand
                        owner / forced unbind failed
  app_update          — App Store / Play Store / FICM software update
                        failure
  app_messages        — repeated / excessive alert notifications
  app_suggestion      — customer optimisation suggestion

iSMART platform (function-level):
  ismart_data_plan      — SIM card capacity / free usage cycle
  ismart_remote_control — remote AC / window / charging power questions
  ismart_digital_key    — Digital Key / Keyless Start — fault or how-to
  ismart_fota           — FOTA / OTA availability, "will X be supported"
  ismart_connectivity   — wireless CarPlay/Android Auto, EV Trip Planner,
                          E-call, area-code coverage
  ismart_account_inquiry— main vs secondary account, password reset how-to,
                          PIN / security code, secondary account creation
  ismart_owner_change   — sold car / new owner handover
  ismart_region_support — "is X country supported"
  ismart_privacy        — declined privacy agreement → core features off
  ismart_user_manual    — cannot locate digital owner's manual
  ismart_feature_inquiry— OTHER feature consultations not covered above

FICM (in-vehicle infotainment):
  ficm_activation — unclear or missing activation guidance
  ficm_cpaa       — unstable CarPlay / Android Auto
  ficm_map        — map upgrade failure / not available

TSP:
  tsp_sensitive_data — GPS / real-time location / vehicle password
                       disclosure requests

ADAS (safety-related):
  adas_acc, adas_aeb, adas_lka, adas_tja

Hardware / non-iSMART:
  hardware_warranty    — warranty claim / part inquiry / IPK warning light
                         / powertrain / body control
  charging             — charging session failure
  delivery_handover    — pre-sales / delivery onboarding complaint
  vehicle_incompatible — iSMART not compatible with their model/year

Lifecycle:
  complaint            — explicit PR / lawyer / media escalation
  follow_up_resolved   — "did my issue get fixed" / closed / resolved
  other                — fallback when none of the above fits

Decision rules:
- Pick the SINGLE best intent; do NOT output multiple.
- The CS reply is a SUPPORT AGENT response (it starts with "Dear customer,"
  or similar). Classify by WHAT the customer's question was about, not what
  the agent said.
- "We need to collect VIN/country/model" boilerplate at the start of many
  replies is NOT diagnostic — read the rest to determine intent.
- If the reply is clearly a follow-up confirmation ("has my issue been
  resolved?", "any update?"), use follow_up_resolved.
- confidence:
    high   — clear single intent from the text
    medium — likely but ambiguous (multiple plausible intents)
    low    — text too short / unclear / multilingual
- reason: ONE short clause (<= 15 words) explaining the intent choice.

Output schema: one JSON object with field `labels` = list of {id, intent,
confidence, reason}, in the SAME ORDER as the input.
"""


def build_user_prompt(records: list[dict]) -> str:
    """Build user message with list of {id, subject, body}. JSON for stability."""
    payload = []
    for r in records:
        body = cut_for_labeling(r["cs_reply"])
        payload.append({
            "id": r["id"],
            "subject": (r.get("subject") or "")[:120],
            "cs_reply": body,
        })
    return "Classify each CS reply. Return labels in input order.\n\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


# ── 主流程 ─────────────────────────────────────────────────────────────

def stream_records(path: Path):
    with path.open() as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_done_ids(path: Path) -> set[str]:
    """Resume support: 读已写入 output 的 id，跳过。"""
    done: set[str] = set()
    if not path.exists():
        return done
    with path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("llm_intent"):
                    done.add(rec["id"])
            except json.JSONDecodeError:
                continue
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--max-records", type=int, default=0,
                    help="0 = 全量；>0 = 只跑前 N 条（干跑用）")
    ap.add_argument("--max-completion-tokens", type=int, default=1500,
                    help="每批 10 条 labels × ~50 tokens = 500，留 3x buffer")
    ap.add_argument("--resume", action="store_true", help="跳过已写入 output 的 id")
    ap.add_argument("--reasoning-effort", default="low", choices=["low", "medium", "high"])
    ap.add_argument("--report-every", type=int, default=100,
                    help="每 N 条打印一次进度")
    ap.add_argument("--workers", type=int, default=1,
                    help="并行 worker 数（Azure rate limit 一般 60-200 RPM，"
                         "默认 1 串行稳妥；>=4 可加速但可能 429）")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    done_ids = load_done_ids(args.output) if args.resume else set()
    LOG.info("resume: %d records already done", len(done_ids))

    records = []
    for rec in stream_records(args.input):
        if rec["id"] in done_ids:
            continue
        if not (rec.get("cs_reply") or "").strip():
            continue
        records.append(rec)
        if args.max_records and len(records) >= args.max_records:
            break

    LOG.info("to process: %d records (batch_size=%d → ~%d calls, workers=%d)",
             len(records), args.batch_size,
             (len(records) + args.batch_size - 1) // args.batch_size,
             args.workers)

    args.output.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    if args.workers <= 1:
        total, errors, cache_hits, prompt_tokens_total, completion_tokens_total = \
            _run_serial(records, args)
    else:
        total, errors, cache_hits, prompt_tokens_total, completion_tokens_total = \
            _run_parallel(records, args)

    LOG.info("DONE. total=%d errors=%d elapsed=%.1fs rate=%.1f rec/s",
             total, errors, time.time() - t0, total / (time.time() - t0) if (time.time() - t0) else 0)

    # 生成报告
    return _write_report(args.output, args.report, total, errors,
                          cache_hits, prompt_tokens_total, completion_tokens_total)


def _run_serial(records, args):
    """串行执行（默认）。"""
    client = AzureLLMClient()
    out_f = args.output.open("a", encoding="utf-8")
    total = errors = cache_hits = prompt_tokens_total = completion_tokens_total = 0
    t0 = time.time()
    try:
        for batch_start in range(0, len(records), args.batch_size):
            batch = records[batch_start: batch_start + args.batch_size]
            total, errors, cache_hits, prompt_tokens_total, completion_tokens_total = \
                _process_one_batch(
                    client, batch, args, out_f,
                    total, errors, cache_hits, prompt_tokens_total, completion_tokens_total,
                    t0, len(records),
                )
            out_f.flush()
    finally:
        out_f.close()
    return total, errors, cache_hits, prompt_tokens_total, completion_tokens_total


def _run_parallel(records, args):
    """并行执行（线程池）。每个 worker 自己一份 AzureLLMClient（线程安全）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    batches = [
        records[i: i + args.batch_size]
        for i in range(0, len(records), args.batch_size)
    ]

    out_f = args.output.open("a", encoding="utf-8")
    total = errors = cache_hits = prompt_tokens_total = completion_tokens_total = 0
    t0 = time.time()

    def _one(batch):
        client = AzureLLMClient()
        # 局部计数（线程安全）
        return _process_one_batch(
            client, batch, args, out_f,
            0, 0, 0, 0, 0,
            t0, len(records),
        )

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_one, b): i for i, b in enumerate(batches)}
            for fut in as_completed(futs):
                bt, be, bch, bpt, bct = fut.result()
                total += bt
                errors += be
                cache_hits += bch
                prompt_tokens_total += bpt
                completion_tokens_total += bct
                out_f.flush()
    finally:
        out_f.close()
    return total, errors, cache_hits, prompt_tokens_total, completion_tokens_total


def _process_one_batch(client, batch, args, out_f,
                       total, errors, cache_hits,
                       prompt_tokens_total, completion_tokens_total,
                       t0, total_records):
    """跑一个 batch；累计 token 计数；返回新累计值。"""
    user_msg = build_user_prompt(batch)
    try:
        result, call = client.invoke_json(
            schema=BatchLabelResult,
            messages=[
                {"role": "system", "content": RECLASSIFY_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            reasoning_effort=args.reasoning_effort,  # type: ignore[arg-type]
            max_completion_tokens=args.max_completion_tokens,
            role="reclassify",
        )
    except Exception as e:
        errors += 1
        LOG.error("batch failed: %s; ids=%s", e, [r["id"] for r in batch])
        if errors > 50:
            LOG.error("too many errors")
        return total, errors, cache_hits, prompt_tokens_total, completion_tokens_total

    cache_hits += call.usage.cached_tokens
    prompt_tokens_total += call.usage.prompt_tokens
    completion_tokens_total += call.usage.completion_tokens

    labels_by_id = {lbl.id: lbl for lbl in result.labels}
    for rec in batch:
        lbl = labels_by_id.get(rec["id"])
        if lbl is None:
            rec_out = dict(rec)
            rec_out["llm_intent"] = None
            rec_out["llm_confidence"] = "low"
            rec_out["llm_reason"] = "missing_in_response"
            errors += 1
        else:
            rec_out = dict(rec)
            rec_out["llm_intent"] = lbl.intent
            rec_out["llm_confidence"] = lbl.confidence
            rec_out["llm_reason"] = lbl.reason
        out_f.write(json.dumps(rec_out, ensure_ascii=False) + "\n")
        total += 1

    if total % args.report_every < args.batch_size:
        elapsed = time.time() - t0
        rate = total / elapsed if elapsed else 0
        cache_hit_rate = cache_hits / prompt_tokens_total if prompt_tokens_total else 0
        LOG.info(
            "progress: %d/%d (%.1f rec/s) errors=%d "
            "prompt=%.1fk cached=%.1f%% completion=%.1fk cost=$%.3f",
            total, total_records, rate, errors,
            prompt_tokens_total / 1000, 100 * cache_hit_rate,
            completion_tokens_total / 1000,
            _estimate_cost(prompt_tokens_total, completion_tokens_total),
        )
    return total, errors, cache_hits, prompt_tokens_total, completion_tokens_total


def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """gpt-5.4 pricing: rough $3/M input, $15/M output（待确认）"""
    return prompt_tokens * 3.0 / 1_000_000 + completion_tokens * 15.0 / 1_000_000


def _write_report(output: Path, report_path: Path, total: int, errors: int,
                  cache_hits: int, prompt_tokens_total: int,
                  completion_tokens_total: int) -> int:
    """AUTO vs LLM 报告：重点是 AUTO 哪里错（heuristic 不靠谱的地方）。"""
    if not output.exists():
        LOG.warning("output not exists, skip report")
        return 0

    auto_intents: list[str] = []
    llm_intents: list[str] = []
    confidence_counter: Counter = Counter()
    reason_counter: Counter = Counter()

    with output.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("llm_intent") and rec.get("intent_mapped"):
                auto_intents.append(rec["intent_mapped"])
                llm_intents.append(rec["llm_intent"])
                confidence_counter[rec.get("llm_confidence", "?")] += 1
            if rec.get("llm_reason"):
                reason_counter[rec["llm_reason"][:60]] += 1

    n = len(auto_intents)
    if n == 0:
        LOG.warning("no labeled records to compare")
        return 0

    # Confusion matrix (AUTO, LLM)
    cm: dict[tuple[str, str], int] = Counter()
    for a, l in zip(auto_intents, llm_intents):
        cm[(a, l)] += 1

    matches = sum(c for (a, l), c in cm.items() if a == l)
    accuracy = matches / n
    disagreement_rate = 1 - accuracy

    # AUTO 每个 intent 的表现：matches, disagreement_rate, top_llm_alt
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

    llm_dist: Counter = Counter(llm_intents)
    auto_dist: Counter = Counter(auto_intents)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        "summary": {
            "total_processed": total,
            "errors": errors,
            "n_compared": n,
            "auto_llm_matches": matches,
            "auto_llm_disagreement_rate": round(disagreement_rate, 4),
            "cost_usd": round(_estimate_cost(prompt_tokens_total, completion_tokens_total), 2),
            "prompt_tokens_total": prompt_tokens_total,
            "completion_tokens_total": completion_tokens_total,
            "cache_hit_tokens": cache_hits,
            "cache_hit_rate": round(cache_hits / prompt_tokens_total, 3) if prompt_tokens_total else 0,
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
    LOG.info("report → %s (n=%d AUTO-LLM disagreement=%.2f%%)",
             report_path, n, 100 * disagreement_rate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())