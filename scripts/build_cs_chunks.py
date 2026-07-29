"""
build_cs_chunks.py — 把 42k 客服回复 jsonl 切成 RAG chunks

输入：  ~/Documents/private/iSmart-email-ledger/cs_replies.redacted.jsonl  （项目外，.gitignore）
输出：  ~/Documents/private/iSmart-email-ledger/cs_chunks.jsonl          （项目外，可进 mock_rag.py）

chunk 设计：
  doc_id    = "cs-{original_id}"
  section   = category_raw（如 "App-绑车解绑相关"）
  quote     = 截取前 ~700 字符的客服回复正文（中文/德/法/意 等多语都保留）
  metadata  = {region, type="cs_reply", intent, category_raw, src_lang}

分层 + 上限：
  - 高频 intent（>1000 样本）→ 每个 region 桶取 60 条
  - 中频 intent（200-1000）   → 每个 region 桶取 30 条
  - 低频 intent（<200）       → 每个 region 桶取 10 条
  - 全局上限 3000 条（避免撑爆 prompt cache；按 (intent × region) 桶优先）

理由：mock_rag.search() 现在只对 NA 有 14 条；这个 corpus 一进，
非 NA 真样本在 full eval 里也能命中 3-5 条真实案例片段 → RAG 全面可用。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PRIVATE_DIR = Path.home() / "Documents" / "private" / "iSmart-email-ledger"
DEFAULT_IN = PRIVATE_DIR / "cs_replies.redacted.jsonl"
DEFAULT_OUT = PRIVATE_DIR / "cs_chunks.jsonl"

QUOTE_MAX_CHARS = 700
GLOBAL_CHUNK_CAP = 3000

# region × intent 分桶配额
def bucket_quota(intent_count: int) -> int:
    if intent_count >= 1000:
        return 60
    if intent_count >= 200:
        return 30
    return 10


def make_quote(text: str) -> str:
    """截取前 ~N 字符；保留段落边界。"""
    if not text:
        return ""
    if len(text) <= QUOTE_MAX_CHARS:
        return text
    # 在最大长度附近找最近的句子结束
    cut = text[:QUOTE_MAX_CHARS]
    for sep in [". ", "。 ", "! ", "? "]:
        idx = cut.rfind(sep)
        if idx > QUOTE_MAX_CHARS * 0.6:
            return cut[:idx + 1]
    return cut  # 没有句子边界，硬截


def main() -> int:
    in_path = DEFAULT_IN
    out_path = DEFAULT_OUT
    if len(sys.argv) > 1:
        in_path = Path(sys.argv[1])
    if len(sys.argv) > 2:
        out_path = Path(sys.argv[2])

    if not in_path.exists():
        raise SystemExit(f"input not found: {in_path}")

    print(f"in:  {in_path}")
    print(f"out: {out_path}")

    # Pass 1: stream in, bucketize by (intent, region)
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    intent_count: Counter = Counter()
    region_count: Counter = Counter()

    n_total = n_picked = 0
    n_no_intent = n_no_region = 0

    with in_path.open() as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_total += 1
            intent = row.get("intent_mapped")
            region = row.get("region_derived")
            reply = row.get("cs_reply", "")
            cat_raw = row.get("category_raw", "")
            if not intent:
                n_no_intent += 1
                continue
            if not region:
                n_no_region += 1
                continue
            if len(reply) < 200:
                continue
            intent_count[intent] += 1
            region_count[region] += 1
            buckets[(intent, region)].append({
                "id": row.get("id", ""),
                "cs_reply": reply,
                "category_raw": cat_raw,
                "ts_ms": row.get("ts_ms", 0),
            })

    # Pass 2: 按配额选 chunk
    chunks: list[dict] = []
    total_picked = 0
    capped = False
    # 按 (intent_count desc) 顺序排，先服务高频 intent
    sorted_intents = sorted(intent_count, key=lambda k: -intent_count[k])
    for intent in sorted_intents:
        if capped:
            break
        quota = bucket_quota(intent_count[intent])
        for region in ("Europe", "United Kingdom", "Oceania", "North America"):
            if capped:
                break
            bucket = buckets.get((intent, region), [])
            if not bucket:
                continue
            # 按 cs_reply 长度 proxy：取长度适中的（中位 ±少量），倾向于信息密度
            bucket.sort(key=lambda r: -min(len(r["cs_reply"]), 3000))  # 太长的 thread 截短后信息密度高
            picks = bucket[:quota]
            for r in picks:
                if total_picked >= GLOBAL_CHUNK_CAP:
                    capped = True
                    break
                chunks.append({
                    "doc_id": f"cs-{r['id']}",
                    "section": r["category_raw"] or intent,
                    "quote": make_quote(r["cs_reply"]),
                    "metadata": {
                        "region": region,
                        "type": "cs_reply",
                        "intent": intent,
                        "category_raw": r["category_raw"],
                    },
                })
                total_picked += 1
                n_picked += 1

    # Write jsonl (not pretty — this is consumed by mock_rag.py)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    # 报告
    by_region = Counter(c["metadata"]["region"] for c in chunks)
    by_intent = Counter(c["metadata"]["intent"] for c in chunks)
    by_lang: Counter = Counter()
    for c in chunks:
        # 启发式：quote 前 50 字符判断
        q = c["quote"][:60].lower()
        for kw, lang in [
            (r"\bthe customer\b", "en"), (r"der kunde", "de"), (r"le client", "fr"),
            (r"il cliente", "it"), (r"el cliente", "es"), (r"geachte", "nl"),
        ]:
            if __import__("re").search(kw, q):
                by_lang[lang] += 1
                break
        else:
            by_lang["other"] += 1

    print()
    print(f"=== SUMMARY ===")
    print(f"  total rows read      {n_total}")
    print(f"  filtered (no intent) {n_no_intent}")
    print(f"  filtered (no region) {n_no_region}")
    print(f"  chunks written       {n_picked}  cap={GLOBAL_CHUNK_CAP}")
    print()
    print(f"=== by region ===")
    for k, v in by_region.most_common():
        print(f"  {v:5d}  {k}")
    print()
    print(f"=== by intent (top 15) ===")
    for k, v in by_intent.most_common(15):
        print(f"  {v:5d}  {k}")
    print(f"  ... and {len(by_intent)} unique intents")
    print()
    print(f"=== language heuristic ===")
    for k, v in by_lang.most_common():
        print(f"  {v:5d}  {k}")
    print()
    print(f"wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
