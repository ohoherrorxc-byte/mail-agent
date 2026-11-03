#!/usr/bin/env python3
"""Sep 19–24 CST Inbox → drafts with latest KB, allow_rescan + replace."""
from __future__ import annotations

import logging
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import _env as _env_loader

_env_loader.load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("sep19_24")

from agent.outlook_drafts import (  # noqa: E402
    GRAPH_BASE,
    LEDGER_DIR,
    AzureLLMClient,
    GraphClient,
    _DuplicateDetector,
    _acquire_scan_lock,
    _dedup_store_path,
    _print_summary,
    _release_scan_lock,
    _write_out,
    build_router_from_env,
    get_token,
    process_mail,
)
from agent.template_reply import ROOT as TROOT  # noqa: E402
from agent.template_reply import load_xlsx  # noqa: E402


def main() -> int:
    for p in (
        Path.home() / ".cache/emailagent/outlook_drafts.lock",
        Path.home() / ".cache/emailagent/outlook_drafts.pid",
    ):
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            LOG.warning("lock clear: %s", e)

    kb = sorted(
        (
            p
            for p in TROOT.glob("iSMART自动回复知识库_全量模板_*.xlsx")
            if not p.name.startswith("~$")
        ),
        reverse=True,
    )
    templates = load_xlsx()
    sources = Counter(t.get("source") for t in templates)
    LOG.info("KB file picked: %s", kb[0].name if kb else None)
    LOG.info("templates=%d sources=%s", len(templates), dict(sources))
    if not kb:
        LOG.error("no KB xlsx found")
        return 2
    LOG.info("template gate OK — %s", kb[0].name)

    user = os.environ["MAILBOX_USER"]
    g = GraphClient(
        get_token(os.environ["AZURE_AD_TENANT_ID"], os.environ["AZURE_AD_CLIENT_ID"]),
        user_id=user,
    )
    # Sep 19–24 CST = [2026-09-18 16:00Z, 2026-09-24 16:00Z)
    since, until = "2026-09-18T16:00:00Z", "2026-09-24T16:00:00Z"
    filt = f"receivedDateTime ge {since} and receivedDateTime lt {until}"
    params = {
        "$top": "100",
        "$select": "id,subject,from,receivedDateTime,categories",
        "$filter": filt,
        "$orderby": "receivedDateTime asc",
    }
    path = f"/users/{g.user_id}/mailFolders/Inbox/messages"
    ids: list[str] = []
    exclude = {"关闭-无效邮件", "关闭:无效邮件", "关闭:已处理", "关闭-已解决"}
    while path:
        data = g._get(path, params=params)
        for m in data.get("value") or []:
            cats = set(m.get("categories") or [])
            if cats & exclude:
                continue
            if m.get("id"):
                ids.append(m["id"])
        nxt = data.get("@odata.nextLink") or ""
        path = nxt[len(GRAPH_BASE) :] if nxt.startswith(GRAPH_BASE) else ""
        params = None
    LOG.info("Sep19–24 Inbox eligible ids=%d", len(ids))

    llm = AzureLLMClient()
    router = build_router_from_env()
    dedup = _DuplicateDetector(store_path=_dedup_store_path())
    lock_fd = _acquire_scan_lock(wait=True)
    if lock_fd is None:
        print("[ERR] scan lock busy", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = LEDGER_DIR / f"outlook_scan_{stamp}_sep19_24.json"
    records: list[dict] = []
    try:
        for i, mid in enumerate(ids, 1):
            mail = g.get_message_by_id(mid)
            if mail is None:
                records.append({"mail_id": mid, "error": "not_found"})
                continue
            rec = process_mail(
                mail,
                g,
                llm=llm,
                templates=templates,
                allowlist=None,
                dry_run=False,
                use_llm=True,
                router=router,
                dedup=dedup,
                allow_rescan=True,
                replace_existing_draft=True,
            )
            records.append(rec)
            outcome = rec.get("skipped") or (
                "DRAFT" if rec.get("draft_id") else rec.get("error") or "?"
            )
            LOG.info(
                "[%d/%d] %s | %s | tmpl=%s",
                i,
                len(ids),
                str(outcome)[:55],
                (mail.subject or "")[:36],
                rec.get("template_id"),
            )
            if i % 10 == 0:
                _write_out(str(out_path), records)
        _write_out(str(out_path), records)
        latest = LEDGER_DIR / "outlook_scan_latest_sep19_24.json"
        _write_out(str(latest), records)
        _print_summary(records)
        skips = Counter(
            (
                r.get("skipped")
                or ("DRAFT" if r.get("draft_id") else r.get("error") or "other")
            )
            for r in records
        )
        print("OUTCOME", dict(skips))
        print("drafts", sum(1 for r in records if r.get("draft_id")))
        print("KB", kb[0].name)
        print("[wrote]", out_path)
    finally:
        _release_scan_lock(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
