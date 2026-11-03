"""
redact_cs_replies.py — 流式读 operations_mail_outbox.csv (42,215 行 客服回复)
→ 切出客服回复正文（去掉 quoted history） → 脱敏 → 写 jsonl
→ 同步出 客服回复 vs 32 Intent + region 覆盖报告

输入：  文件库/operations_mail_outbox.csv   （已 .gitignore，不会进 git）
输出：
  ~/Documents/private/iSmart-email-ledger/cs_replies.redacted.jsonl  （项目外）
  eval/runs/cs_reply_coverage.json                                 （项目内，可 commit）

设计与 redact_operations_mail.py 一致：
  - csv 流式读（不要 load 全文件）
  - HTML strip → 切掉 quoted history → 脱敏
  - AUTO: 等同运营 inbox schema，但 outbox 多 26 列；col 23 = category
  - CATEGORY_TO_INTENT / parse_category / resolve_intent 复用
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from agent.template_reply import CATEGORY_TO_INTENT  # noqa: E402
from scripts.redact_operations_mail import (  # noqa: E402
    parse_category,
    resolve_intent,
    redact_text,
    strip_html,
)

DEFAULT_CSV = PROJECT_ROOT / "文件库" / "operations_mail_outbox.csv"
PRIVATE_DIR = Path.home() / "Documents" / "private" / "iSmart-email-ledger"
DEFAULT_JSONL = PRIVATE_DIR / "cs_replies.redacted.jsonl"
DEFAULT_REPORT = PROJECT_ROOT / "eval" / "runs" / "cs_reply_coverage.json"


# ── 切出客服回复正文（去掉 quoted history） ────────────────────

# Outlook "Original Message" 分隔符变体（en / zh / de / fr / es / pt / it）
_QUOTED_HISTORY_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\n[-_]{2,}\s*邮件原件\s*[-_]{2,}", re.I),
    re.compile(r"\n[-_]{2,}\s*Original Message\s*[-_]{2,}", re.I),
    re.compile(r"\n[-_]{2,}\s*Ursprüngliche Nachricht\s*[-_]{2,}", re.I),
    re.compile(r"\n[-_]{2,}\s*Message d['’]origine\s*[-_]{2,}", re.I),
    re.compile(r"\n[-_]{2,}\s*Mensaje original\s*[-_]{2,}", re.I),
    re.compile(r"\n[-_]{2,}\s*Messaggio originale\s*[-_]{2,}", re.I),
    # Outlook "Reply" 分隔符
    re.compile(r"\n+Von\s*:\s", re.I),
    re.compile(r"\n+发件人\s*[：:]\s*", re.I),
    re.compile(r"\n+From\s*:\s", re.I),
    re.compile(r"\n+De\s*:\s", re.I),
    # 邮件签名后直接跟历史（少 <br><br>，靠 "Best regards," / "Mit freundlichen" 等切分）
    re.compile(r"\n+Best regards,\s*\n\s*iSMART Service Team\b", re.I),
    re.compile(r"\n+Mit freundlichen Grüßen\b", re.I),
    re.compile(r"\n+cordiali saluti\b", re.I),
    re.compile(r"\n+cordialement\b", re.I),
    re.compile(r"\n+saludos cordiales\b", re.I),
)


def extract_cs_reply(body_text: str) -> str:
    """从 body 切出客服回复正文（去掉 quoted history）。

    body_text 应该是 strip_html() 之后的纯文本。返回客服实际回复的纯文本
    （包含 "Dear customer,<br><br>..." 开头到 "Best regards,<br>iSMART Service Team" 结束）。
    """
    if not body_text:
        return ""
    # 找到最早的 quoted-history 标记
    cut_at = len(body_text)
    for pat in _QUOTED_HISTORY_MARKERS:
        m = pat.search(body_text)
        if m and m.start() < cut_at:
            cut_at = m.start()
    reply = body_text[:cut_at].strip()
    # 去掉 trailing 多余空行
    reply = re.sub(r"\n{3,}", "\n\n", reply)
    return reply


# ── 客服常用 salutation / 签名模板（用于 sanity check） ─────

_CS_OPENER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*Dear\s+(customer|sir|madam|mr\.?|ms\.?|mrs\.?)\b", re.I),
    re.compile(r"^\s*Hello\b", re.I),
    re.compile(r"^\s*Hi\b", re.I),
    re.compile(r"^\s*Sehr geehrte[r]?\s+(Frau|Herr|Damen und Herren)", re.I),
    re.compile(r"^\s*Bonjour\b", re.I),
    re.compile(r"^\s*Estimad[oa]\s+(cliente|señor|señora)", re.I),
)

_CS_SIGNATURE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\biSMART Service Team\b", re.I),
    re.compile(r"\biSMART Service Team\b", re.I),
    re.compile(r"\bIsmart Customer Service\b", re.I),
    re.compile(r"\bMG Europe (customer service|CS|support)\b", re.I),
    re.compile(r"\bMit freundlichen Grüßen\b", re.I),
)


def looks_like_cs_reply(text: str) -> bool:
    """Sanity check：text 看起来是客服回复吗？"""
    if not text:
        return False
    has_opener = any(p.search(text[:200]) for p in _CS_OPENER_PATTERNS)
    has_signoff = any(p.search(text[-300:]) for p in _CS_SIGNATURE_PATTERNS)
    # 至少要有 salutation 或 sig；只有其一放低阈值
    return has_opener or has_signoff


# ── region 推断（sender_mail TLD 启发式） ───────────────

_TLD_TO_REGION: dict[str, str] = {
    # Europe
    "de": "Europe", "fr": "Europe", "it": "Europe", "es": "Europe",
    "pt": "Europe", "nl": "Europe", "be": "Europe", "at": "Europe",
    "ch": "Europe", "pl": "Europe", "cz": "Europe", "sk": "Europe",
    "hu": "Europe", "ro": "Europe", "bg": "Europe", "gr": "Europe",
    "se": "Europe", "dk": "Europe", "fi": "Europe", "no": "Europe",
    "ie": "Europe", "lu": "Europe", "ee": "Europe", "lv": "Europe",
    "lt": "Europe",
    "tr": "Europe", "ru": "Europe", "by": "Europe",
    # UK
    "uk": "United Kingdom",
    # Oceania
    "au": "Oceania", "nz": "Oceania",
    # Middle East（对照表「中东」；不含土耳其）
    "ae": "Middle East", "sa": "Middle East", "qa": "Middle East",
    "kw": "Middle East", "om": "Middle East", "bh": "Middle East",
    "il": "Middle East", "lb": "Middle East", "jo": "Middle East",
    "iq": "Middle East",
    # Asia（不含 GCC / 以色列 / 俄罗斯）
    "cn": "Asia", "jp": "Asia", "kr": "Asia", "sg": "Asia",
    "in": "Asia", "my": "Asia", "th": "Asia", "ph": "Asia",
    "vn": "Asia", "id": "Asia",
    # Americas
    "us": "North America", "ca": "North America", "mx": "South America",
    "br": "South America", "ar": "South America", "cl": "South America",
    # Africa
    "za": "Africa", "eg": "Africa", "ng": "Africa",
}


def region_from_email(email: str) -> str | None:
    if not email or "@" not in email:
        return None
    tld = email.rsplit(".", 1)[-1].lower().strip()
    return _TLD_TO_REGION.get(tld)


# ── main ──────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--max-rows", type=int, default=None)
    ap.add_argument("--no-redact", action="store_true")
    args = ap.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"csv 不存在: {args.csv}")

    args.jsonl.parent.mkdir(parents=True, exist_ok=True)

    stats = {
        "total": 0,
        "with_body": 0,
        "cs_reply_extracted": 0,
        "cs_reply_passed_sanity": 0,
        "with_category": 0,
        "intent_freq": Counter(),
        "region_freq": Counter(),
        "category_raw_freq": Counter(),
        "cs_opener_freq": Counter(),
        "elapsed_sec": 0.0,
    }

    t0 = time.time()
    with args.csv.open(encoding="utf-8") as f_in, args.jsonl.open("w", encoding="utf-8") as f_out:
        reader = csv.reader(f_in)
        for row in reader:
            stats["total"] += 1
            if args.max_rows and stats["total"] > args.max_rows:
                break
            if len(row) < 24:
                continue
            subject = row[12] or ""
            body_html = row[14] or ""
            sender_mail = row[18] or ""
            recipient_mail = row[21] or ""
            ts_ms = int(row[22]) if (row[22] and row[22].isdigit()) else 0
            category_raw = row[23] or ""

            if body_html:
                stats["with_body"] += 1
            body_text = strip_html(body_html) if body_html else ""
            reply_text = extract_cs_reply(body_text)

            if reply_text:
                stats["cs_reply_extracted"] += 1
                if looks_like_cs_reply(reply_text):
                    stats["cs_reply_passed_sanity"] += 1

            if category_raw:
                stats["with_category"] += 1
                stats["category_raw_freq"][category_raw] += 1

            parsed = parse_category(category_raw)
            intent = resolve_intent(parsed)
            if intent:
                stats["intent_freq"][intent] += 1

            region = region_from_email(sender_mail) or region_from_email(recipient_mail)
            if region:
                stats["region_freq"][region] += 1

            if not args.no_redact and reply_text:
                subject_out = redact_text(subject)
                reply_out = redact_text(reply_text)
                sender_out = redact_text(sender_mail)
            else:
                subject_out = subject
                reply_out = reply_text
                sender_out = sender_mail

            obj = {
                "id":              row[0],
                "ts_ms":           ts_ms,
                "subject":         subject_out,
                "cs_reply":        reply_out,
                "cs_reply_len":    len(reply_out),
                "sender_mail":     sender_out,
                "recipient_mail":  recipient_mail,
                "category_raw":    category_raw,
                "intent_mapped":   intent,
                "region_derived":  region,
            }
            f_out.write(json.dumps(obj, ensure_ascii=False) + "\n")

            if stats["total"] % 5000 == 0:
                rate = stats["total"] / max(0.1, time.time() - t0)
                print(f"  [{stats['total']}]  rate={rate:.0f}/s  "
                      f"cs_extracted={stats['cs_reply_extracted']}  "
                      f"sanity_pass={stats['cs_reply_passed_sanity']}  "
                      f"intent={len(stats['intent_freq'])}/32", flush=True)

    elapsed = time.time() - t0
    stats["elapsed_sec"] = round(elapsed, 1)

    # 报告
    from agent.schemas import Intent  # noqa: F401  (use Literal)
    from typing import get_args
    intent_enum = list(get_args(__import__("agent").schemas.Intent))
    unmapped = [i for i in intent_enum if stats["intent_freq"].get(i, 0) == 0]

    report = {
        "summary": {
            "total_rows":            stats["total"],
            "with_body":             stats["with_body"],
            "cs_reply_extracted":    stats["cs_reply_extracted"],
            "cs_reply_passed_sanity": stats["cs_reply_passed_sanity"],
            "with_category":         stats["with_category"],
            "elapsed_sec":           stats["elapsed_sec"],
            "intents_seen":          len(stats["intent_freq"]),
            "intents_total":         len(intent_enum),
            "unmapped_intents":      unmapped,
        },
        "intent_top":          [{"key": k, "count": v} for k, v in stats["intent_freq"].most_common(32)],
        "region_top":          [{"key": k, "count": v} for k, v in stats["region_freq"].most_common(10)],
        "category_raw_top":    [{"key": k, "count": v} for k, v in stats["category_raw_freq"].most_common(20)],
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=== SUMMARY ===")
    for k, v in report["summary"].items():
        print(f"  {k:24s} {v}")
    print("\n=== INTENT 32-enum 分布 (top 10) ===")
    for it in report["intent_top"][:10]:
        print(f"  {it['count']:6d}  {it['key']}")
    print(f"\n=== region 分布 ===")
    for it in report["region_top"][:8]:
        print(f"  {it['count']:6d}  {it['key']}")
    print(f"\n=== unmapped intents ({len(unmapped)}): {unmapped} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
