"""每天给安抚信对应的客户来信打「已安抚」。

不改已读，不发送，不删除，不挡业务草稿。
已有这个标签的不再改。

  python3 -m agent.comfort_tag_sweep
  python3 -m agent.comfort_tag_sweep --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import requests

from . import _env as _env_loader
from .comfort_reply import (
    COMFORT_CATEGORY,
    comfort_tag_target,
    is_comfort_reply,
)
from .foreign_draft_sweep import ensure_trust_store
from .outlook_drafts import (
    GraphClient,
    build_router_from_env,
    cs_mailbox_set,
    get_token,
)

LOG = logging.getLogger("comfort_tag_sweep")

_SEARCHES = (
    "we kindly ask for your patience",
    "As your feedback involves technical details",
)
_FOLDERS = ("SentItems", "Drafts")
_PAGE_CAP = 40


def _sender_address(item: dict) -> str:
    snd = item.get("sender") or {}
    addr = (snd.get("emailAddress") or {}).get("address") or ""
    return str(addr).strip()


def _body_text(item: dict) -> str:
    body = item.get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html" or "<" in content:
        from .outlook_drafts import _html_to_text
        return _html_to_text(content)
    return content


def _search(g: GraphClient, folder: str, query: str):
    url = (
        f"https://graph.microsoft.com/v1.0/users/{g.user_id}"
        f"/mailFolders/{folder}/messages"
    )
    params: dict[str, str] | None = {
        "$search": f'"{query}"',
        "$top": "50",
        "$select": (
            "id,conversationId,receivedDateTime,isDraft,sender,body,categories"
        ),
    }
    headers = {"ConsistencyLevel": "eventual"}
    pages = 0
    while url and pages < _PAGE_CAP:
        pages += 1
        resp = g.session.get(url, headers=headers, params=params, timeout=90)
        resp.raise_for_status()
        data = resp.json()
        params = None
        for item in data.get("value") or []:
            yield item
        url = data.get("@odata.nextLink")


def _ensure_category(g: GraphClient) -> None:
    data = g._get(f"/users/{g.user_id}/outlook/masterCategories")
    names = {
        str(c.get("displayName") or "")
        for c in (data.get("value") or [])
    }
    if COMFORT_CATEGORY in names:
        return
    g._post(
        f"/users/{g.user_id}/outlook/masterCategories",
        {"displayName": COMFORT_CATEGORY, "color": "preset3"},
    )


def tag_once(g: GraphClient, *, dry_run: bool = False) -> dict[str, int]:
    """扫发件箱和草稿箱，给安抚之前的客户来信打标签。"""
    self_emails = cs_mailbox_set(build_router_from_env())
    if not dry_run:
        _ensure_category(g)
    stats = {
        "seen": 0,
        "tagged": 0,
        "already": 0,
        "no_target": 0,
        "not_comfort": 0,
        "errors": 0,
    }
    seen_ids: set[str] = set()
    for folder in _FOLDERS:
        for query in _SEARCHES:
            try:
                items = list(_search(g, folder, query))
            except (requests.HTTPError, requests.RequestException) as exc:
                stats["errors"] += 1
                LOG.warning("search %s failed: %s", folder, exc)
                continue
            for item in items:
                mid = str(item.get("id") or "")
                if not mid or mid in seen_ids:
                    continue
                seen_ids.add(mid)
                stats["seen"] += 1
                sender = _sender_address(item).casefold()
                if sender not in self_emails and not item.get("isDraft"):
                    stats["not_comfort"] += 1
                    continue
                if not is_comfort_reply(_body_text(item)):
                    stats["not_comfort"] += 1
                    continue
                cid = item.get("conversationId") or ""
                if not cid:
                    stats["no_target"] += 1
                    continue
                try:
                    thread = g.get_conversation_thread(cid)
                except (requests.HTTPError, requests.RequestException) as exc:
                    stats["errors"] += 1
                    LOG.warning("thread fetch failed: %s", exc)
                    continue
                target = comfort_tag_target(
                    thread,
                    comfort_at=str(item.get("receivedDateTime") or ""),
                    self_emails=self_emails,
                )
                if not target or target == mid:
                    stats["no_target"] += 1
                    continue
                try:
                    existing = g.get_categories(target)
                except (requests.HTTPError, requests.RequestException) as exc:
                    stats["errors"] += 1
                    LOG.warning("categories read failed: %s", exc)
                    continue
                if COMFORT_CATEGORY in existing:
                    stats["already"] += 1
                    continue
                if dry_run:
                    stats["tagged"] += 1
                    continue
                try:
                    g.add_category(target, COMFORT_CATEGORY)
                except (requests.HTTPError, requests.RequestException, ValueError) as exc:
                    stats["errors"] += 1
                    LOG.warning("tag failed: %s", exc)
                    continue
                stats["tagged"] += 1
    LOG.info(
        "comfort tags: seen=%d tagged=%d already=%d no_target=%d not_comfort=%d errors=%d",
        stats["seen"], stats["tagged"], stats["already"],
        stats["no_target"], stats["not_comfort"], stats["errors"],
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    _env_loader.load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description="给安抚信对应的客户来信打已安抚")
    p.add_argument("--dry-run", action="store_true", help="只计数，不打标签")
    args = p.parse_args(argv)
    tenant = os.environ.get("AZURE_AD_TENANT_ID")
    client = os.environ.get("AZURE_AD_CLIENT_ID")
    user = os.environ.get("MAILBOX_USER", "me")
    if not tenant or not client:
        print("缺 AZURE_AD_TENANT_ID / AZURE_AD_CLIENT_ID", file=sys.stderr)
        return 2
    ensure_trust_store()
    last: Exception | None = None
    token = ""
    for attempt in range(3):
        try:
            token = get_token(tenant, client)
            break
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as exc:
            last = exc
            LOG.warning("登录失败，15 秒后重试（%d/3）", attempt + 1)
            import time
            time.sleep(15)
    else:
        LOG.error("登录失败: %s", last)
        return 3
    g = GraphClient(token, user)
    tag_once(g, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
