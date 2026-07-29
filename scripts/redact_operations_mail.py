"""
redact_operations_mail.py — 流式读 operations_mail.xls (53,558 行)
→ strip HTML → 脱敏 VIN/email/phone → 写到 ~/Documents/private/iSmart-email-ledger/
→ 同步出 AUTO: token → 32 Intent enum 对账报告

输入：  文件库/operations_mail.xls    （已 .gitignore，不会进 git）
输出：
  ~/Documents/private/iSmart-email-ledger/operations_mail.redacted.jsonl  （项目外，不进 git）
  eval/runs/auto_intent_coverage.json                                       （项目内，可 commit）

设计：
  - xlrd on_demand=True 逐行读 row_slice()，内存峰值 < 200MB
  - jsonl 流式写，每行一个 {id, ts, sender_mail, subject, content, category, region, auto_token, ...}
  - redact_text 复用了 scripts/redact_ledger.py 的规则（VIN / email / phone）
  - AUTO: token → 32 Intent 用 agent.template_reply.CATEGORY_TO_INTENT 现成映射表
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import xlrd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from agent.template_reply import CATEGORY_TO_INTENT  # noqa: E402

DEFAULT_XLS = PROJECT_ROOT / "文件库" / "operations_mail.xls"
PRIVATE_DIR = Path.home() / "Documents" / "private" / "iSmart-email-ledger"
DEFAULT_JSONL = PRIVATE_DIR / "operations_mail.redacted.jsonl"
DEFAULT_REPORT = PROJECT_ROOT / "eval" / "runs" / "auto_intent_coverage.json"


# ── HTML stripper ───────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(html: str) -> str:
    """去 HTML tags；保留文本节点。多用于邮件正文。"""
    s = _HTMLStripper()
    try:
        s.feed(html)
    except Exception:
        return html  # fallback: 原样
    out = "".join(s.parts)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n[ \t]+", "\n", out)
    return out.strip()


# ── PII 模式（复用于 scripts/redact_ledger.py） ──────────────────────

VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Outlook CID: image001.png@01DCF804.PHONE 不算 PII
CID_INLINE_RE = re.compile(r"^image\d{0,4}\.(png|jpg|jpeg|gif)$", re.I)
# 电话：必须显式国际区号前缀 (+xx / 00xx)
PHONE_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:\+\d{1,3}|\(\+\d{1,3}\)|00\d{1,3})"
    r"[\s\-]?"
    r"(?:\(?\d{2,4}\)?[\s\-]?)?"
    r"\d{2,4}[\s\-]?\d{2,4}[\s\-]?\d{0,4}"
)


def _hash8(value: str, salt: str) -> str:
    return hashlib.sha256((salt + "|" + value).encode()).hexdigest()[:8]


def redact_text(text: str, salt: str = "iSmart") -> str:
    """VIN / email / phone → mask。email 排除 Outlook CID 形态。"""
    if not text:
        return text
    text = VIN_RE.sub(lambda m: f"VIN_MASK_{_hash8(m.group(), salt)}", text)

    def _email_sub(m: re.Match) -> str:
        local = m.group().split("@")[0]
        if CID_INLINE_RE.match(local):
            return m.group()  # Outlook inline-image CID
        return f"user_{_hash8(m.group(), salt)}@example.com"

    text = EMAIL_RE.sub(_email_sub, text)
    text = PHONE_RE.sub(lambda m: f"PHONE_MASK_{_hash8(m.group(), salt)}", text)
    return text


# ── category 解析 ───────────────────────────────────────────────────

def parse_category(cat: str) -> dict[str, object]:
    """把 'AUTO:App-车控车态相关,Region:Europe,App-绑车解绑相关,关闭-已解决,Negative'
    拆成结构化字段。"""
    if not cat:
        return {}
    parts = [p.strip() for p in re.split(r"[,/]", cat) if p.strip()]
    auto_tok = None
    region_tok = None
    biz_toks: list[str] = []
    status_toks: list[str] = []
    negative = False
    for p in parts:
        if p.startswith("AUTO:"):
            auto_tok = p[5:].strip()
        elif p.startswith("Region:"):
            region_tok = p.split(":", 1)[1].strip()
        elif p == "Negative":
            negative = True
            # 把 "Negative" 也作为 token 加进 status_toks；
            # CATEGORY_TO_INTENT["Negative"] = "complaint" → 让 parse_category
            # 出去的 token 走 mapping 表，不靠特判。
            status_toks.append("Negative")
        elif p.startswith("关闭-"):
            status_toks.append(p)
        else:
            biz_toks.append(p)
    return {
        "auto": auto_tok,
        "region": region_tok,
        "biz": biz_toks,
        "status": status_toks,
        "negative": negative,
    }


def map_auto_to_intent(auto_tok: str | None) -> str | None:
    """AUTO: token → 32 Intent enum (用现成 CATEGORY_TO_INTENT 映射表)。"""
    if not auto_tok:
        return None
    return CATEGORY_TO_INTENT.get(auto_tok)


def resolve_intent(parsed: dict) -> str | None:
    """复合 token resolution：按优先级在 CATEGORY_TO_INTENT 里查 32 Intent。

    优先级：
      1. auto (AUTO: 标签，业务专家的 LLM 分类)
      2. biz (人工业务标签，如 "App-车控车态相关")
      3. status (status_toks：Negative → complaint, 关闭-已解决 → follow_up_resolved)
    """
    for key in [parsed.get("auto"), *parsed.get("biz", []), *parsed.get("status", [])]:
        if key:
            m = CATEGORY_TO_INTENT.get(key)
            if m:
                return m
    return None


# ── xls 流式读 ──────────────────────────────────────────────────────

# column index → 字段名（来自 sheet 0 header）
COL_MAP = {
    0:  "id",
    10: "received_time",
    11: "subject_enc",
    12: "subject",
    13: "content_enc",
    14: "content",
    17: "recipient",
    19: "sender",
    21: "sender_mail",
    22: "reply_time",
    24: "category",
}


def excel_serial_to_iso(serial: float) -> str | None:
    """Excel 日期序列号 → ISO 字符串。失败返回 None。"""
    try:
        # Excel 1900-based epoch with 1900 leap-year bug
        from datetime import datetime, timedelta
        epoch = datetime(1899, 12, 30)
        return (epoch + timedelta(days=float(serial))).isoformat()
    except Exception:
        return None


def stream_redact(xls_path: Path, jsonl_path: Path, *,
                  max_rows: int | None = None,
                  progress_every: int = 5000,
                  redact: bool = True,
                  salt: str = "iSmart") -> dict:
    """流式读 xls → 脱敏 → 写 jsonl。返回报告需要的统计。"""
    wb = xlrd.open_workbook(str(xls_path), formatting_info=False, on_demand=True)
    ws = wb.sheet_by_index(0)
    nrows = ws.nrows if max_rows is None else min(max_rows, ws.nrows)
    ncols = ws.ncols

    # 验证 header
    header = {c: ws.cell_value(0, c) for c in range(ncols)}
    expected = {0: "id", 10: "received_time", 12: "subject", 14: "content",
                19: "sender", 21: "sender_mail", 24: "category"}
    for c, exp in expected.items():
        if header.get(c) != exp:
            raise ValueError(f"xls schema 异常: col {c} = {header.get(c)!r}, expected {exp!r}")

    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    stats = {
        "total": 0,
        "with_content": 0,
        "with_subject": 0,
        "with_category": 0,
        "with_auto_token": 0,
        "with_region_token": 0,
        "auto_token_freq": Counter(),
        "auto_token_unmapped": Counter(),
        "intent_freq": Counter(),
        "region_freq": Counter(),
        "biz_freq": Counter(),
        "status_freq": Counter(),
        "negative_count": 0,
        "html_count": 0,
        "elapsed_sec": 0.0,
        "elapsed_per_row_ms": 0.0,
    }

    t0 = time.time()
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in range(1, nrows):
            row = ws.row_slice(r, start_colx=0, end_colx=ncols)
            # row 是 Cell 列表
            def _get(c: int):
                return row[c].value if c < len(row) else ""

            raw = {
                "id":            _get(0),
                "received_time": _get(10),
                "subject":       _get(12) or "",
                "content":       _get(14) or "",
                "sender":        _get(19) or "",
                "sender_mail":   _get(21) or "",
                "category":      _get(24) or "",
                "reply_time":    _get(22) or 0.0,
            }

            content = str(raw["content"])
            subject = str(raw["subject"])
            category = str(raw["category"])
            sender = str(raw["sender"])
            sender_mail = str(raw["sender_mail"])

            has_html = bool(re.search(r"<[a-z]+[^>]*>", content, re.I))
            if has_html:
                stats["html_count"] += 1
                content = strip_html(content)
            if subject:
                stats["with_subject"] += 1
            if content:
                stats["with_content"] += 1
            if category:
                stats["with_category"] += 1

            parsed = parse_category(category)
            auto_tok = parsed.get("auto") if isinstance(parsed, dict) else None
            region_tok = parsed.get("region") if isinstance(parsed, dict) else None
            biz_toks = parsed.get("biz", []) if isinstance(parsed, dict) else []
            status_toks = parsed.get("status", []) if isinstance(parsed, dict) else []
            negative = parsed.get("negative", False) if isinstance(parsed, dict) else False

            intent = resolve_intent(parsed)
            if auto_tok:
                stats["with_auto_token"] += 1
                stats["auto_token_freq"][auto_tok] += 1
                # AUTO 标签查不到映射时记入 unmapped（只统计 AUTO 路径）
                if not CATEGORY_TO_INTENT.get(auto_tok):
                    stats["auto_token_unmapped"][auto_tok] += 1
            if intent:
                stats["intent_freq"][intent] += 1
            if region_tok:
                stats["with_region_token"] += 1
                stats["region_freq"][region_tok] += 1
            for t in biz_toks:
                stats["biz_freq"][t] += 1
            for t in status_toks:
                stats["status_freq"][t] += 1
            if negative:
                stats["negative_count"] += 1

            # 脱敏（如果关掉，写原文便于人工 spot-check）
            if redact:
                subject_out = redact_text(subject, salt)
                content_out = redact_text(content, salt)
                sender_out = redact_text(sender, salt)
                sender_mail_out = redact_text(sender_mail, salt)
            else:
                subject_out = subject
                content_out = content
                sender_out = sender
                sender_mail_out = sender_mail

            ts_iso = excel_serial_to_iso(raw["received_time"]) if raw["received_time"] else None

            obj = {
                "id":            str(raw["id"]),
                "ts":            ts_iso,
                "sender":        sender_out,
                "sender_mail":   sender_mail_out,
                "subject":       subject_out,
                "content":       content_out,
                "category_raw":  category,
                "auto_token":    auto_tok,
                "region":        region_tok,
                "biz_tokens":    biz_toks,
                "status_tokens": status_toks,
                "negative":      negative,
                "intent_mapped": intent,
                "reply_time":    float(raw["reply_time"]) if raw["reply_time"] else 0.0,
            }
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            stats["total"] += 1

            if stats["total"] % progress_every == 0:
                elapsed = time.time() - t0
                rate = stats["total"] / max(0.1, elapsed)
                eta = (nrows - stats["total"]) / max(0.1, rate)
                print(f"  [{stats['total']}/{nrows-1}]  rate={rate:.0f}/s  ETA={eta:.0f}s", flush=True)

    elapsed = time.time() - t0
    stats["elapsed_sec"] = round(elapsed, 1)
    stats["elapsed_per_row_ms"] = round(elapsed / max(1, stats["total"]) * 1000, 2)
    return stats


def build_report(stats: dict) -> dict:
    """把 Counter 转成 top-N 列表，写 JSON。"""
    def _top(d: Counter, n: int = 30) -> list[dict]:
        return [{"key": k, "count": v} for k, v in d.most_common(n)]

    return {
        "summary": {
            "total_rows":        stats["total"],
            "with_content":      stats["with_content"],
            "with_subject":      stats["with_subject"],
            "with_category":     stats["with_category"],
            "with_auto_token":   stats["with_auto_token"],
            "with_region_token": stats["with_region_token"],
            "html_count":        stats["html_count"],
            "negative_count":    stats["negative_count"],
            "elapsed_sec":       stats["elapsed_sec"],
            "elapsed_per_row_ms": stats["elapsed_per_row_ms"],
        },
        "auto_token_top":       _top(stats["auto_token_freq"], 30),
        "auto_token_unmapped":  _top(stats["auto_token_unmapped"], 30),
        "intent_top":           _top(stats["intent_freq"], 32),
        "region_top":           _top(stats["region_freq"], 30),
        "biz_top":              _top(stats["biz_freq"], 30),
        "status_top":           _top(stats["status_freq"], 20),
        "unmapped_intents":     [i for i in __import__("agent").schemas.Intent.__args__
                                 if stats["intent_freq"].get(i, 0) == 0],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--xls", type=Path, default=DEFAULT_XLS)
    ap.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--max-rows", type=int, default=None,
                    help="最多读 N 行（调试用）")
    ap.add_argument("--no-redact", action="store_true",
                    help="不脱敏，写原文（仅 debug；生产不要用）")
    args = ap.parse_args()

    if not args.xls.exists():
        raise SystemExit(f"xls 不存在: {args.xls}")

    print(f"xls:   {args.xls} ({args.xls.stat().st_size // 1024 // 1024} MB)")
    print(f"jsonl: {args.jsonl}")
    print(f"report: {args.report}")
    print(f"redact: {not args.no_redact}")
    if args.max_rows:
        print(f"max-rows: {args.max_rows} (debug)")
    print()

    stats = stream_redact(
        args.xls, args.jsonl,
        max_rows=args.max_rows,
        redact=not args.no_redact,
    )
    report = build_report(stats)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"=== SUMMARY ===")
    for k, v in report["summary"].items():
        print(f"  {k:24s} {v}")
    print(f"\n=== INTENT 32-enum 分布 (top 15) ===")
    for it in report["intent_top"][:15]:
        print(f"  {it['count']:6d}  {it['key']}")
    print(f"\n=== AUTO: token 未映射 (top 10) ===")
    for it in report["auto_token_unmapped"][:10]:
        print(f"  {it['count']:6d}  {it['key']}")
    print(f"\n=== Region top 10 ===")
    for it in report["region_top"][:10]:
        print(f"  {it['count']:6d}  {it['key']}")
    print(f"\n=== 32 Intent 中未出现的: {len(report['unmapped_intents'])} ===")
    print(f"  {report['unmapped_intents']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())