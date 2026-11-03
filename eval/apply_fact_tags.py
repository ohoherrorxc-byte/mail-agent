"""给 Inbox 最近一段时间的邮件打 VIN / 品牌 / 已知问题类型标签。

不建草稿、不发送、不删信。已有同类标签正确则保留，不对再补一条。
无对应信息不改这一维。

  python3 eval/apply_fact_tags.py --since 2026-08-02T00:00:00Z
  python3 eval/apply_fact_tags.py --since 2026-09-10T16:00:00Z --until 2026-09-12T16:00:00Z
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.outlook_drafts import GraphClient, _env_loader, get_token
from agent.mail_tags import push_fact_tags


def _list_window(
    g: GraphClient,
    since: str,
    until: str | None,
    *,
    page_size: int = 80,
) -> list:
    """Graph $filter 窗口 + 用 oldest received_at 往更早翻页（until 游标）。"""
    out = []
    seen: set[str] = set()
    page_until = until
    while True:
        batch = g.list_filtered(
            top=page_size,
            folder="Inbox",
            since=since,
            until=page_until,
            is_read=None,
            exclude_categories=[],
            order="desc",
            include_attachments=False,
        )
        if not batch:
            break
        fresh = 0
        for m in batch:
            if m.id in seen:
                continue
            seen.add(m.id)
            out.append(m)
            fresh += 1
        print(f"  listed +{fresh} (total {len(out)}) oldest={batch[-1].received_at}",
              flush=True)
        if len(batch) < page_size or fresh == 0:
            break
        page_until = batch[-1].received_at
        time.sleep(0.15)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since", help="ISO 8601 下界（含），默认 30 天前")
    p.add_argument("--until", help="ISO 8601 上界（不含）")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--out",
        default=str(Path.home() / "Documents/private/iSmart-email-ledger/outlook_fact_tags_month.json"),
    )
    args = p.parse_args()

    _env_loader.load_env(verbose=False)
    since = args.since
    if not since:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
            "%Y-%m-%dT00:00:00Z"
        )
    until = args.until
    print(f"since={since} until={until} dry_run={args.dry_run}", flush=True)

    g = GraphClient(
        get_token(os.environ["AZURE_AD_TENANT_ID"], os.environ["AZURE_AD_CLIENT_ID"]),
        user_id=os.environ["MAILBOX_USER"],
    )
    mails = _list_window(g, since, until)
    print(f"inbox window: {len(mails)}", flush=True)

    counts: Counter[str] = Counter()
    patched_n = 0
    errors = []
    samples = []

    for i, mail in enumerate(mails, 1):
        rec = push_fact_tags(g, mail.id, mail.body_text or "")
        actions = rec.get("actions") or []
        for a in actions:
            counts[f"{a.get('dim')}:{a.get('kind')}"] += 1
        if rec.get("error"):
            counts["error"] += 1
            errors.append({
                "subject": (mail.subject or "")[:80],
                "err": rec.get("error"),
            })
        elif rec.get("applied"):
            patched_n += 1
            counts["patched"] += 1
        else:
            counts["unchanged"] += 1
        if i <= 15 or (rec.get("applied") and len(samples) < 20):
            samples.append({
                "subject": (mail.subject or "")[:80],
                "received_at": mail.received_at,
                "actions": [(a.get("dim"), a.get("kind"), a.get("value")) for a in actions],
                "patched": bool(rec.get("applied")),
            })
        if i % 50 == 0:
            print(f"  processed {i}/{len(mails)} patched={patched_n}", flush=True)
        time.sleep(0.05)

    summary = {
        "since": since,
        "until": until,
        "total": len(mails),
        "patched": patched_n,
        "dry_run": args.dry_run,
        "counts": dict(counts),
        "errors": errors[:30],
        "samples": samples,
    }
    out = Path(args.out)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 50)
    print(json.dumps(summary["counts"], ensure_ascii=False, indent=2))
    print(f"total={len(mails)} patched={patched_n} errors={len(errors)}")
    print(f"wrote {out}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
