#!/usr/bin/env python3
"""
enrich_xlsx_scenarios.py — 离线预处理：把 iSMART 模板 xlsx 的「问题场景」翻译成英文 + 扩展关键词。

为什么需要这个脚本：
  M2.5.5 root cause：pick_template() 在同 intent 多候选下选错模板，因为只用
  硬编码正则 + body token overlap，完全没用 xlsx 第 4 列「问题场景」/ 第 5 列
  「触发关键词」里人工事先分的 96 条 scenario。

修复方案（用户拍板）：
  1) 一次性离线脚本：把每条 scenario 从中文翻英文，再用 LLM 从英文 scenario
     描述里扩 8-15 个关键词（existing 列只 5-7 条，扩出来更全）
  2) 结果落到 agent/xlsx_scenarios_enriched.json（git 跟踪；启动期 load 一次）
  3) runtime pick_template 用 (existing + extracted) 关键词 + en_scenario token
     打分选最高分 → 不调 LLM，prompt cache 100% 命中

运行：
  python3 scripts/enrich_xlsx_scenarios.py             # 默认；xlsx 比 JSON 新才跑
  python3 scripts/enrich_xlsx_scenarios.py --force     # 无条件重跑
  python3 scripts/enrich_xlsx_scenarios.py --check     # CI 用：xlsx/JSON 不一致 exit 1
  python3 scripts/enrich_xlsx_scenarios.py --limit 5   # 只跑前 5 条（debug）

输出：agent/xlsx_scenarios_enriched.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 允许直接 python3 scripts/enrich_xlsx_scenarios.py
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import BaseModel


# ── 1. 读 xlsx ─────────────────────────────────────────────────────────

# xlsx 文件名约定（与 template_reply.py 同步：日期最新的全量表）
_kb = sorted(
    (
        p for p in ROOT.glob("iSMART自动回复知识库_全量模板_*.xlsx")
        if not p.name.startswith("~$")
    ),
    reverse=True,
)
PRIMARY_XLSX = _kb[0] if _kb else ROOT / "iSMART自动回复知识库_全量模板_20260914.xlsx"
LEGACY_XLSX  = ROOT / "iSMART售后服务邮箱回复方法_20260308.xlsx"

# 输出
OUTPUT_JSON  = ROOT / "agent" / "xlsx_scenarios_enriched.json"


def _load_rows_from_xlsx(xlsx_path: Path) -> list[dict]:
    """从新表 schema 读 (template_id, l2_category, scenario_zh, keywords_existing) 四列。
    旧表 (3 列 schema) 没有 scenario 列 → 直接跳过（不在 enriched JSON 里）。
    """
    if not xlsx_path.exists():
        return []
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = (
        wb["自动回复知识库"]
        if "自动回复知识库" in wb.sheetnames
        else wb.active
    )
    head = ws.cell(row=1, column=1).value
    if str(head or "").strip() != "模板ID":
        # 旧表 → 跳过
        return []
    headers = [str(ws.cell(1, c).value or "").strip() for c in range(1, 16)]
    offset = 1 if headers[1:2] == ["修改日期及状态"] else 0

    def col(*names, default: int) -> int:
        for name in names:
            if name in headers:
                return headers.index(name)
        return default

    i_id = col("模板ID", default=0)
    i_st = col("修改日期及状态", default=-1)
    i_l2 = col("二级问题分类", default=2 + offset)
    i_sc = col("问题场景", default=3 + offset)
    i_kw = col("触发关键词", default=4 + offset)
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        def g(i):
            if i < 0 or i >= len(row):
                return ""
            v = row[i]
            return (v or "").strip() if isinstance(v, str) else str(v or "").strip()
        tmpl_id = g(i_id)
        if not tmpl_id:
            continue
        if i_st >= 0 and "删除" in g(i_st):
            continue
        rows.append({
            "template_id":       tmpl_id,
            "l2_category":       g(i_l2),
            "scenario_zh":       g(i_sc),
            "keywords_existing": g(i_kw),
        })
    return rows


# ── 2. LLM 翻译 + 扩词 ─────────────────────────────────────────────────

class _Enrichment(BaseModel):
    en_scenario: str
    keywords_extracted: list[str]


_SYSTEM = """You are a translation + keyword extraction specialist for iSmart customer service email routing.

Input: a Chinese scenario description (1-3 sentences) describing when a customer email template should be applied.

Output JSON:
  - en_scenario: 1-2 sentence English description preserving all semantic distinctions.
    Use consistent vocabulary: "customer", "VIN", "vehicle", "bind", "app", "infotainment",
    "QR", "screenshot", "video", "troubleshooting".
    Translate car terms precisely: 车辆→vehicle, 绑车→bind vehicle, 解绑→unbind,
    二手→used, 扫码→scan QR, 后台→backend, 排查→troubleshooting.
  - keywords_extracted: 8-15 short English keyword phrases a customer's email body
    might contain when it falls into this scenario. Include both single-word and
    multi-word phrases. Examples: "no details", "VIN missing", "scanned QR",
    "no error code", "cannot bind", "is this your account", "step by step".
    Phrases must be LOWERCASE. Avoid generic stopwords like "the", "a", "is".
    Reuse the scenario's semantic content — don't invent scenarios outside it."""


def _call_llm_enrich(scenario_zh: str, existing_kws: str,
                     *, llm, role: str = "triage") -> _Enrichment:
    user_content = (
        f"Chinese scenario: {scenario_zh}\n\n"
        f"Existing manual keywords (for reference, do NOT copy verbatim): {existing_kws or '(empty)'}"
    )
    result, _ = llm.invoke_json(
        schema=_Enrichment,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user",   "content": user_content},
        ],
        reasoning_effort="low",
        role=role,
    )
    return result


# ── 3. en_scenario 抽 token (兜底信号) ─────────────────────────────────

# 中文 stopwords — 不抽进 scenario_en_tokens
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "at", "by", "for", "with", "from", "as",
    "and", "or", "but", "so", "if", "then", "than",
    "this", "that", "these", "those", "it", "its",
    "they", "their", "them", "we", "our", "us",
    "you", "your", "i", "my", "me", "he", "she", "his", "her",
    "has", "have", "had", "do", "does", "did", "done",
    "will", "would", "should", "could", "may", "might", "can",
    "not", "no", "nor", "neither",
    "very", "too", "also", "just", "only",
}
_TOKEN_RE = re.compile(r"[a-z][a-z\-]{2,}")  # 至少 3 字符的英文 token


def _extract_scenario_tokens(en_scenario: str, top_n: int = 12) -> list[str]:
    """从英文 scenario 抽 top_n 个信息量 token (按频次, 排除 stopword)。"""
    if not en_scenario:
        return []
    tokens = _TOKEN_RE.findall(en_scenario.lower())
    counts: dict[str, int] = {}
    for t in tokens:
        if t in _STOPWORDS:
            continue
        counts[t] = counts.get(t, 0) + 1
    # 按频次降序
    sorted_toks = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return [tok for tok, _ in sorted_toks[:top_n]]


# ── 4. 主流程 ──────────────────────────────────────────────────────────

def _merge_keywords(existing: str, extracted: list[str]) -> list[str]:
    """existing (CSV string) + extracted → 去重保序的小写列表。"""
    out: list[str] = []
    seen: set[str] = set()
    if existing:
        for k in existing.split(","):
            k2 = k.strip().lower()
            if k2 and k2 not in seen:
                seen.add(k2)
                out.append(k2)
    for k in extracted:
        k2 = k.strip().lower()
        if k2 and k2 not in seen:
            seen.add(k2)
            out.append(k2)
    return out


def _resolve_model_name(llm) -> str:
    """从 llm 对象抽 deployment / model 名（审计用）。"""
    for attr in ("deployment", "deployment_name", "model", "model_name"):
        v = getattr(llm, attr, None)
        if v:
            return str(v)
    return "unknown"


def _run_enrichment(force: bool, limit: int | None,
                    output_json: Path) -> int:
    # 收集所有行（多 xlsx 文件 — 与 template_reply.load_xlsx 同源）
    all_rows: list[dict] = []
    seen_ids: set[str] = set()
    for path in [PRIMARY_XLSX, LEGACY_XLSX]:
        for row in _load_rows_from_xlsx(path):
            if row["template_id"] in seen_ids:
                continue
            seen_ids.add(row["template_id"])
            all_rows.append(row)

    if not all_rows:
        print(f"[ERR] 没找到任何新表 schema 的 xlsx 行（{PRIMARY_XLSX}？）",
              file=sys.stderr)
        return 1

    # limit
    if limit:
        all_rows = all_rows[:limit]
        print(f"[debug] limit={limit} → 只跑前 {limit} 条")

    # 初始化 LLM
    try:
        from agent.azure_llm import AzureLLMClient
        llm = AzureLLMClient()
        model_name = _resolve_model_name(llm)
    except Exception as e:
        print(f"[ERR] AzureLLMClient 初始化失败: {type(e).__name__}: {e}", file=sys.stderr)
        print("       请确认 .env 里有 AZURE_OPENAI_ENDPOINT / API_KEY / DEPLOYMENT",
              file=sys.stderr)
        return 1

    enriched: list[dict] = []
    skipped: list[tuple[str, str]] = []  # (template_id, reason)
    started = time.time()

    for i, row in enumerate(all_rows, 1):
        tmpl_id = row["template_id"]
        scenario_zh = row["scenario_zh"]
        keywords_existing = row["keywords_existing"]
        print(f"[{i}/{len(all_rows)}] {tmpl_id} ...", end=" ", flush=True)
        t0 = time.time()
        try:
            res = _call_llm_enrich(scenario_zh, keywords_existing, llm=llm)
            extracted = res.keywords_extracted
            en_scenario = res.en_scenario
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            print(f"SKIP ({reason})")
            skipped.append((tmpl_id, reason))
            continue
        elapsed = time.time() - t0
        keywords_all = _merge_keywords(keywords_existing, extracted)
        scenario_en_tokens = _extract_scenario_tokens(en_scenario)
        print(f"OK ({elapsed:.1f}s, "
              f"{len(extracted)} extracted, "
              f"{len(keywords_all)} total, "
              f"{len(scenario_en_tokens)} scenario_tokens)")
        enriched.append({
            "template_id":        tmpl_id,
            "l2_category":        row["l2_category"],
            "scenario_zh":        scenario_zh,
            "scenario_en":        en_scenario,
            "keywords_existing":  [k.strip() for k in keywords_existing.split(",") if k.strip()],
            "keywords_extracted": extracted,
            "keywords_all":       keywords_all,
            "scenario_en_tokens": scenario_en_tokens,
        })

    elapsed_total = time.time() - started
    print()
    print(f"[done] {len(enriched)}/{len(all_rows)} enriched in {elapsed_total:.1f}s")
    if skipped:
        print(f"[warn] {len(skipped)} skipped:")
        for tid, reason in skipped:
            print(f"  - {tid}: {reason}")

    # 写 JSON
    payload = {
        "version":      datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "model":        model_name,
        "source_xlsx":  PRIMARY_XLSX.name,
        "enriched_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "templates":    enriched,
        "skipped":      [{"template_id": t, "reason": r} for t, r in skipped],
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[wrote] {output_json} ({output_json.stat().st_size:,} bytes)")
    return 0


def _check(output_json: Path, primary_xlsx: Path) -> int:
    """CI 模式：xlsx mtime > JSON mtime 或 template_id 数不一致 → exit 1。"""
    if not output_json.exists():
        print(f"[FAIL] {output_json} 不存在；先跑 enrich_xlsx_scenarios.py 生成",
              file=sys.stderr)
        return 1
    if not primary_xlsx.exists():
        print(f"[FAIL] {primary_xlsx} 不存在", file=sys.stderr)
        return 1

    xlsx_mtime  = primary_xlsx.stat().st_mtime
    json_mtime  = output_json.stat().st_mtime
    if xlsx_mtime > json_mtime:
        print(f"[FAIL] {primary_xlsx.name} 比 {output_json.name} 新；"
              f"需要重新 enrich", file=sys.stderr)
        return 1

    payload = json.loads(output_json.read_text(encoding="utf-8"))
    xlsx_ids = {
        str(r.get("template_id") or "")
        for r in _load_rows_from_xlsx(primary_xlsx)
        if r.get("template_id")
    }
    json_ids = {
        str(t.get("template_id") or "")
        for t in payload.get("templates") or []
        if t.get("template_id")
    }
    missing = sorted(xlsx_ids - json_ids)
    if missing:
        print(f"[FAIL] xlsx 有但 enrich JSON 没有：{missing[:12]}",
              file=sys.stderr)
        return 1

    print(f"[OK] {len(xlsx_ids)} xlsx ids covered by enrich JSON "
          f"({len(json_ids)} json rows), xlsx ≤ JSON mtime")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--force", action="store_true",
                        help="无条件重跑（默认仅 xlsx 比 JSON 新才跑）")
    parser.add_argument("--check", action="store_true",
                        help="CI 模式：xlsx/JSON 不一致 exit 1")
    parser.add_argument("--limit", type=int, default=None,
                        help="只跑前 N 条（debug）")
    parser.add_argument("--output", type=Path, default=OUTPUT_JSON,
                        help="输出 JSON 路径")
    args = parser.parse_args()

    if args.check:
        return _check(args.output, PRIMARY_XLSX)

    if not args.force and args.output.exists() and PRIMARY_XLSX.exists():
        if PRIMARY_XLSX.stat().st_mtime <= args.output.stat().st_mtime:
            print(f"[skip] {PRIMARY_XLSX.name} 不比 JSON 新；用 --force 重跑")
            return 0

    return _run_enrichment(args.force, args.limit, args.output)


if __name__ == "__main__":
    sys.exit(main())