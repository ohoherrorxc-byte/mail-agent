"""补 2026-11-11 至 2026-11-19（北京时间）的三件事。

1. 会话里发过安抚、客户来信还没有「已安抚」→ 打在最早一封客户来信上。
2. 同一会话里的来回，或对上没有会话号的回信主题，被打了「关闭-无效邮件」→ 去掉这个标。
3. 还没有业务草稿、客服也还没有业务回复的 → 起草。不 send，不删信。

已解决、误转发、系统信、另一条会话里的连点，保持关闭。模板对不上的仍不建草稿。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import _env as _env_loader  # noqa: E402
from agent.azure_llm import AzureLLMClient  # noqa: E402
from agent.comfort_reply import (  # noqa: E402
    COMFORT_CATEGORY,
    comfort_tag_target,
    is_comfort_reply,
)
from agent.comfort_tag_sweep import _ensure_category  # noqa: E402
from agent.foreign_draft_sweep import ensure_trust_store  # noqa: E402
from agent.mail_tags import extract_vin  # noqa: E402
from agent.outlook_drafts import (  # noqa: E402
    CLOSE_INVALID_CATEGORY,
    CLOSE_INVALID_CATEGORY_LEGACY,
    CLOSE_RESOLVED_CATEGORY,
    LEDGER_DIR,
    GraphClient,
    _DuplicateDetector,
    _acquire_scan_lock,
    _dedup_fingerprint,
    _dedup_sender_key,
    _dedup_store_path,
    _html_to_text,
    _release_scan_lock,
    build_router_from_env,
    cs_mailbox_set,
    get_token,
    misdirected_junk_skip_reason,
    process_mail,
    should_reopen_invalid_close,
    system_notification_skip_reason,
)
from agent.template_reply import load_xlsx  # noqa: E402

LOG = logging.getLogger("backfill_nov11_19")

# 北京时间 11 月 11 日 0 点到 11 月 20 日 0 点。
SINCE = "2026-11-10T16:00:00Z"
UNTIL = "2026-11-19T16:00:00Z"


def _sender(item: dict) -> tuple[str, str]:
    addr = ((item.get("sender") or {}).get("emailAddress") or {})
    return str(addr.get("address") or ""), str(addr.get("name") or "")


def _to_mail(item: dict):
    from agent.outlook_drafts import Mail

    body = (item.get("body") or {}).get("content") or ""
    text = _html_to_text(body) if "<" in body else body
    sender, name = _sender(item)
    return Mail(
        id=str(item.get("id") or ""),
        subject=item.get("subject") or "",
        sender=sender,
        sender_display_name=name,
        received_at=item.get("receivedDateTime") or "",
        body_text=text,
        is_read=bool(item.get("isRead")),
        conversation_id=item.get("conversationId") or "",
        categories=[c for c in (item.get("categories") or []) if str(c).strip()],
    )


def list_window(g: GraphClient) -> list:
    url = (
        f"https://graph.microsoft.com/v1.0/users/{g.user_id}"
        f"/mailFolders/Inbox/messages"
    )
    params: dict[str, str] | None = {
        "$filter": f"receivedDateTime ge {SINCE} and receivedDateTime lt {UNTIL}",
        "$orderby": "receivedDateTime asc",
        "$top": "40",
        "$select": (
            "id,subject,sender,receivedDateTime,isRead,categories,"
            "conversationId,body"
        ),
    }
    out = []
    pages = 0
    while url and pages < 80:
        pages += 1
        resp = g.session.get(url, params=params, timeout=90)
        resp.raise_for_status()
        data = resp.json()
        params = None
        for item in data.get("value") or []:
            mail = _to_mail(item)
            if mail.id:
                out.append(mail)
        url = data.get("@odata.nextLink")
        LOG.info("listed %d", len(out))
    return out


def _pin_seen(dedup: _DuplicateDetector, mail) -> None:
    """重开的信先记入指纹，起草时按「同一封再扫」走，不会被旧规则再次关掉。"""
    key = _dedup_sender_key(mail.sender)
    if not key:
        return
    if mail.id in (dedup._buckets.get(key) or {}):
        return
    subj, ngrams, issue = _dedup_fingerprint(mail)
    vin = extract_vin(mail.body_text or "") or ""
    dedup._remember(key, mail, subj, ngrams, issue, vin)


def _business_state(thread: list[dict], self_emails: set[str]) -> tuple[bool, bool]:
    """返回 (有业务草稿, 有已发出的业务回复)。安抚不算。"""
    own = {e.casefold() for e in self_emails if e}
    draft = False
    replied = False
    for msg in thread:
        body = msg.get("body_text") or ""
        if msg.get("is_draft"):
            if not is_comfort_reply(body):
                draft = True
            continue
        sender = (msg.get("sender_email") or "").casefold()
        if sender in own and not is_comfort_reply(body):
            replied = True
    return draft, replied


def main() -> int:
    _env_loader.load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    ensure_trust_store()
    import os
    tenant = os.environ.get("AZURE_AD_TENANT_ID")
    client = os.environ.get("AZURE_AD_CLIENT_ID")
    user = os.environ.get("MAILBOX_USER", "me")
    if not tenant or not client:
        print("缺 AZURE_AD_TENANT_ID / AZURE_AD_CLIENT_ID", file=sys.stderr)
        return 2
    lock = _acquire_scan_lock(wait=False)
    if lock is None:
        print("扫信进程还占着锁，先停掉再补", file=sys.stderr)
        return 1
    try:
        token = get_token(tenant, client)
        g = GraphClient(token, user)
        router = build_router_from_env()
        self_emails = cs_mailbox_set(router)
        dedup = _DuplicateDetector(store_path=_dedup_store_path())
        templates = load_xlsx()
        llm = AzureLLMClient()
        try:
            _ensure_category(g)
        except (requests.HTTPError, requests.RequestException) as exc:
            LOG.warning("ensure 已安抚 failed: %s", exc)

        mails = list_window(g)
        threads: dict[str, list[dict]] = {}

        def thread_of(mail) -> list[dict]:
            cid = (mail.conversation_id or "").strip()
            if not cid:
                return []
            if cid not in threads:
                try:
                    threads[cid] = g.get_conversation_thread(cid)
                except (requests.HTTPError, requests.RequestException) as exc:
                    LOG.warning("thread failed %s: %s", cid[:16], exc)
                    threads[cid] = []
            return threads[cid]

        stats = {
            "listed": len(mails),
            "comfort_tagged": 0,
            "comfort_already": 0,
            "reopened": 0,
            "kept_closed": 0,
            "drafted": 0,
            "draft_skipped": 0,
            "errors": 0,
        }
        records: list[dict] = []
        tagged: set[str] = set()
        for i, mail in enumerate(mails, 1):
            if (mail.sender or "").casefold() in self_emails:
                continue
            thread = thread_of(mail)
            rec: dict = {
                "mail_id": mail.id,
                "subject": mail.subject,
                "sender": mail.sender,
                "received_at": mail.received_at,
            }
            ours = [
                m for m in thread
                if not m.get("is_draft")
                and (m.get("sender_email") or "").casefold() in self_emails
                and is_comfort_reply(m.get("body_text") or "")
            ]
            if ours:
                comfort_at = min(m.get("received_at") or "" for m in ours)
                target = comfort_tag_target(
                    thread, comfort_at=comfort_at, self_emails=self_emails,
                )
                if target and target not in tagged:
                    tagged.add(target)
                    try:
                        cats = g.get_categories(target)
                    except (requests.HTTPError, requests.RequestException) as exc:
                        stats["errors"] += 1
                        rec["comfort_error"] = str(exc)
                        cats = []
                    if COMFORT_CATEGORY in cats:
                        stats["comfort_already"] += 1
                    else:
                        try:
                            g.add_category(target, COMFORT_CATEGORY)
                            stats["comfort_tagged"] += 1
                            rec["comfort_tagged"] = target
                        except (requests.HTTPError, requests.RequestException, ValueError) as exc:
                            stats["errors"] += 1
                            rec["comfort_error"] = str(exc)

            closed = CLOSE_INVALID_CATEGORY in mail.categories or (
                CLOSE_INVALID_CATEGORY_LEGACY in mail.categories
            )
            if closed and should_reopen_invalid_close(mail, thread, self_emails, dedup):
                try:
                    g.remove_category(mail.id, CLOSE_INVALID_CATEGORY)
                    if CLOSE_INVALID_CATEGORY_LEGACY in mail.categories:
                        g.remove_category(mail.id, CLOSE_INVALID_CATEGORY_LEGACY)
                    mail.categories = [
                        c for c in mail.categories
                        if c not in {CLOSE_INVALID_CATEGORY, CLOSE_INVALID_CATEGORY_LEGACY}
                    ]
                    stats["reopened"] += 1
                    rec["reopened"] = True
                except (requests.HTTPError, requests.RequestException, ValueError) as exc:
                    stats["errors"] += 1
                    rec["reopen_error"] = str(exc)
            elif closed:
                stats["kept_closed"] += 1
                rec["kept_closed"] = True

            still_closed = (
                CLOSE_INVALID_CATEGORY in mail.categories
                or CLOSE_INVALID_CATEGORY_LEGACY in mail.categories
            )
            resolved = CLOSE_RESOLVED_CATEGORY in mail.categories
            junk = bool(
                system_notification_skip_reason(mail)
                or misdirected_junk_skip_reason(mail)
            )
            has_draft, has_reply = _business_state(thread, self_emails)
            if still_closed or resolved or junk or has_draft or has_reply:
                rec["draft"] = "skip"
                rec["draft_why"] = (
                    "closed" if still_closed else
                    "resolved" if resolved else
                    "junk" if junk else
                    "has_draft" if has_draft else
                    "staff_replied"
                )
            else:
                try:
                    full = g.get_message_by_id(mail.id) or mail
                    full.categories = mail.categories
                    if rec.get("reopened"):
                        _pin_seen(dedup, full)
                    result = process_mail(
                        full, g, llm=llm, templates=templates,
                        allowlist=None, dry_run=False, use_llm=True,
                        router=router, dedup=dedup, allow_rescan=True,
                    )
                    rec["draft"] = result.get("skipped") or "drafted"
                    rec["template_id"] = result.get("template_id")
                    rec["draft_id"] = result.get("draft_id")
                    if result.get("draft_id"):
                        stats["drafted"] += 1
                    else:
                        stats["draft_skipped"] += 1
                        rec["skip_reason_detail"] = result.get("skip_reason_detail")
                except Exception as exc:
                    stats["errors"] += 1
                    rec["draft"] = "error"
                    rec["draft_error"] = f"{type(exc).__name__}: {exc}"
                    LOG.exception("draft failed %s", mail.subject[:40])
            records.append(rec)
            if i % 20 == 0:
                LOG.info("progress %d/%d %s", i, len(mails), stats)

        LOG.info("done %s", stats)
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        out = LEDGER_DIR / "backfill_20261111_19.json"
        out.write_text(
            json.dumps({"stats": stats, "records": records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    finally:
        _release_scan_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
