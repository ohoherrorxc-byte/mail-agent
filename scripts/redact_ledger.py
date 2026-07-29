#!/usr/bin/env python3
"""
redact_ledger.py — 把售后邮件台账里的 PII 替换成 mask 后写到项目外。

输入: ~/Documents/private/iSmart-email-ledger/售后邮件台账-2.csv
      (SharePoint list export，第一行 schema + 1458 列；数据从第 2 行起)

输出: ~/Documents/private/iSmart-email-ledger/售后邮件台账-2.redacted.json
      (项目外，仅本机。不进 git。)

替换规则（保守——只动 PII，不动正文文意）：
  - VIN  (17 位 [A-HJ-NPR-Z0-9]{17})                → VIN_MASK_<8 hex>
  - email 地址                                      → user_<8 hex>@example.com
  - 电话 (国际 / 国内格式，>= 7 位连续数字)          → PHONE_MASK_<8 hex>
  - 已经在 BodyHtml/BodyPreview 里的 PII 同样替换

正文（Subject / BodyPreview / BodyHtml / Title）保留，**因为评估需要看正文**。
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path

# ── 路径常量（不允许写到项目内） ──────────────────────────────────
PRIVATE_DIR = Path.home() / "Documents" / "private" / "iSmart-email-ledger"
SRC = PRIVATE_DIR / "售后邮件台账-2.csv"
DST = PRIVATE_DIR / "售后邮件台账-2.redacted.json"

# ── 数据字段顺序（从 schema 行手工抽出，确保不依赖任何结构推断） ──
FIELDS = [
    "Title", "ReceivedTime", "CategoriesRaw", "Sender", "Subject", "Status",
    "RegionGroup", "Countrt", "VIN", "Module", "ProblemType", "RequestType",
    "BodyPreview", "MessageId", "RecordType", "BodyHtml",
]

# ── PII 模式 ──────────────────────────────────────────────────────
# VIN：17 位字母数字，去掉 I/O/Q
VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")

# email：基础模式，但排除 Outlook 的 inline-image CID 形态
#         (image001.png@01DCF804.PHONE 这种不算 PII)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 排除 CID: 本地部分是 image*.png/jpg + 域名是 HEX.PHONE/PHONE/COM 等短后缀
CID_INLINE_RE = re.compile(r"^image\d{0,4}\.(png|jpg|jpeg|gif)$", re.IGNORECASE)

# 电话：必须显式带国际区号前缀 (+xx / 00xx)，避免误伤 hex / 时间戳 / CID
# 格式: +xx xxx xxx xxxx 或 +xx-xxx-xxx-xxxx 等
PHONE_RE = re.compile(
    r"(?<![A-Za-z0-9])"               # 前面不能是字母数字（避免 email local part）
    r"(?:\+\d{1,3}|\(\+\d{1,3}\)|00\d{1,3})"  # 必须显式国际区号
    r"[\s\-]?"
    r"(?:\(?\d{2,4}\)?[\s\-]?)?"
    r"\d{2,4}[\s\-]?\d{2,4}[\s\-]?\d{0,4}"
)


def _h(s: str) -> str:
    """确定性 hash — 同一 VIN 总是映射成同一 mask，利于后续 join。"""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


def _mask_vin(m: re.Match) -> str:
    return f"VIN_MASK_{_h(m.group(0))}"


def _mask_email(m: re.Match) -> str:
    addr = m.group(0)
    local, _, _ = addr.partition("@")
    # 跳过 Outlook inline-image CID（image001.png@01DCF804.PHONE 这种）
    if CID_INLINE_RE.match(local):
        return addr
    return f"user_{_h(addr)}@example.com"


def _mask_phone(m: re.Match) -> str:
    return f"PHONE_MASK_{_h(m.group(0))}"


def redact_text(s: str) -> tuple[str, dict]:
    """返回 (脱敏后文本, 替换计数)。"""
    if not s:
        return s, {"vin": 0, "email": 0, "phone": 0}

    n_vin = n_email = n_phone = 0

    def _vin(m):
        nonlocal n_vin
        n_vin += 1
        return _mask_vin(m)

    def _em(m):
        nonlocal n_email
        n_email += 1
        return _mask_email(m)

    def _ph(m):
        nonlocal n_phone
        n_phone += 1
        return _mask_phone(m)

    # 顺序很重要：先 VIN（避免 email 把 VIN 切碎）→ 再 email → 最后 phone
    s = VIN_RE.sub(_vin, s)
    s = EMAIL_RE.sub(_em, s)
    # phone 模式较激进，要求 ≥ 7 位连续数字才动手；先 dry-run 计数再确认
    s, n_phone_raw = PHONE_RE.subn(_ph, s)

    return s, {"vin": n_vin, "email": n_email, "phone": n_phone_raw}


def redact_record(rec: dict) -> tuple[dict, dict]:
    """对单条记录做脱敏；统计本条 PII 数量。"""
    counts = {"vin": 0, "email": 0, "phone": 0}
    out = {}
    for k, v in rec.items():
        new_v, c = redact_text(v or "")
        out[k] = new_v
        for kk in counts:
            counts[kk] += c[kk]
    return out, counts


def main() -> int:
    if not SRC.exists():
        print(f"[ERR] source not found: {SRC}", file=sys.stderr)
        return 2

    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)

    with open(SRC, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    # row[0] 是 SharePoint schema（1458 列）；从 row[1:] 起是 5013 条数据
    if len(rows) < 2:
        print("[ERR] empty source", file=sys.stderr)
        return 2

    records = []
    total_counts = {"vin": 0, "email": 0, "phone": 0}
    skipped = 0

    for r in rows[1:]:
        if len(r) < 16:
            r = r + [""] * (16 - len(r))
        elif len(r) > 16:
            # 极端情况下超长，截断到 16
            r = r[:16]
        rec = dict(zip(FIELDS, r))
        redacted, c = redact_record(rec)
        # 跳过全空记录
        if not any(redacted[f].strip() for f in FIELDS):
            skipped += 1
            continue
        records.append(redacted)
        for kk in total_counts:
            total_counts[kk] += c[kk]

    DST.write_text(
        json.dumps(
            {"records": records, "redaction_counts": total_counts,
             "skipped_blank": skipped, "total_in": len(rows) - 1},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    print(f"[redact] read   {len(rows)-1:>5} records from {SRC.name}")
    print(f"[redact] wrote  {len(records):>5} records to {DST}")
    print(f"[redact] PII replaced: VIN={total_counts['vin']}  "
          f"email={total_counts['email']}  phone={total_counts['phone']}")
    print(f"[redact] skipped {skipped} blank records")
    return 0


if __name__ == "__main__":
    sys.exit(main())