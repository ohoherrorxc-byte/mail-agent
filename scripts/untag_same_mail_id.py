#!/usr/bin/env python3
"""揭掉 same_mail_id 误打的关闭标。不 send、不删信、不改正文。

只处理台账里 skipped=duplicate 且 match=same_mail_id 的原信。
表单连点（subject_exact / same_content）不动。

  python3 scripts/untag_same_mail_id.py
  python3 scripts/untag_same_mail_id.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import _env as _env_loader  # noqa: E402
from agent.outlook_drafts import (  # noqa: E402
    CLOSE_INVALID_CATEGORY,
    CLOSE_INVALID_CATEGORY_LEGACY,
    GraphClient,
    LEDGER_DIR,
    _acquire_scan_lock,
    _release_scan_lock,
    get_token,
)

_env_loader.load_env()
LOG = logging.getLogger("untag_same_mail_id")

STRIP = frozenset({CLOSE_INVALID_CATEGORY, CLOSE_INVALID_CATEGORY_LEGACY})


def mail_ids_from_ledgers(paths: list[Path]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            recs = data
        elif isinstance(data, dict):
            recs = data.get("records") or []
        else:
            continue
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            if rec.get("skipped") != "duplicate":
                continue
            if rec.get("category_applied") is False:
                continue
            detail = str(rec.get("skip_reason_detail") or "")
            if not detail.startswith("same_mail_id"):
                continue
            mid = str(rec.get("mail_id") or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            out.append(mid)
    return out


def strip_close_tags(g: GraphClient, mail_id: str, *, dry_run: bool) -> str:
    cats = g.get_categories(mail_id)
    kept = [c for c in cats if c not in STRIP]
    if kept == cats:
        return "noop"
    if dry_run:
        return "would_strip"
    g.set_categories(mail_id, kept)
    return "stripped"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ledger-glob", default="outlook_scan_20260914_*.json")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    paths = sorted(LEDGER_DIR.glob(args.ledger_glob))
    ids = mail_ids_from_ledgers(paths)
    LOG.info("台账 %d 个文件，same_mail_id 去重后 %d 封", len(paths), len(ids))
    if not ids:
        return 0

    tenant_id = os.environ.get("AZURE_AD_TENANT_ID")
    client_id = os.environ.get("AZURE_AD_CLIENT_ID")
    user_id = os.environ.get("MAILBOX_USER", "me")
    if not tenant_id or not client_id:
        print("[ERR] 缺 AZURE_AD_TENANT_ID / AZURE_AD_CLIENT_ID", file=sys.stderr)
        return 2

    lock_fd = _acquire_scan_lock(wait=True)
    if lock_fd is None:
        print("[ERR] 扫信锁被占用", file=sys.stderr)
        return 1
    stats = {"stripped": 0, "would_strip": 0, "noop": 0, "error": 0}
    try:
        token = get_token(tenant_id, client_id)
        g = GraphClient(token, user_id=user_id)
        for mid in ids:
            try:
                status = strip_close_tags(g, mid, dry_run=args.dry_run)
                stats[status] = stats.get(status, 0) + 1
                LOG.info("%s id=%s…", status, mid[:24])
            except Exception as e:
                stats["error"] += 1
                LOG.warning("error id=%s… %s", mid[:24], e)
    finally:
        _release_scan_lock(lock_fd)
    print(json.dumps(stats, ensure_ascii=False))
    return 0 if stats["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
