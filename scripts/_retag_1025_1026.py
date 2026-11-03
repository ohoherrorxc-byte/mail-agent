"""2026-10-25、26（北京时间）只重打「问题:」。不建草稿，不改 isRead。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import _env as _env_loader
from agent.azure_llm import AzureLLMClient
from agent.foreign_draft_sweep import ensure_trust_store
from agent.mail_tags import human_issue_display_names, push_fact_tags
from agent.outlook_drafts import (
    GraphClient,
    _acquire_scan_lock,
    _html_to_text,
    _release_scan_lock,
    get_token,
)
import os

SINCE = "2026-10-24T16:00:00Z"
UNTIL = "2026-10-26T16:00:00Z"
STATUS = ROOT / "eval" / "runs" / "retag_1025_1026.json"
SKIP_FOLDERS = ("sentitems", "drafts", "deleteditems", "junkemail")


class RetryLlm:
    def __init__(self, inner: AzureLLMClient) -> None:
        self.inner = inner

    def invoke_json(self, **kwargs):
        last: Exception | None = None
        for attempt in range(3):
            try:
                return self.inner.invoke_json(**kwargs)
            except Exception as exc:
                last = exc
                time.sleep(min(20, 2 ** attempt))
        assert last is not None
        raise last


def _get_json(g: GraphClient, path: str, params: dict | None = None) -> dict:
    last_status = 0
    for attempt in range(6):
        resp = g._request("GET", path, params=params)
        last_status = resp.status_code
        if resp.status_code in (429, 502, 503, 504):
            time.sleep(min(60, 2 ** attempt))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"graph_get status={last_status}")


def _next_path(link: str) -> str:
    parsed = urlparse(link)
    path = parsed.path
    marker = "/v1.0"
    if marker in path:
        path = path.split(marker, 1)[1]
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def _folder_ids(g: GraphClient) -> set[str]:
    found: set[str] = set()
    for name in SKIP_FOLDERS:
        try:
            data = _get_json(
                g,
                f"/users/{g.user_id}/mailFolders/{name}",
                {"$select": "id"},
            )
        except Exception:
            continue
        fid = str(data.get("id") or "")
        if fid:
            found.add(fid)
    return found


def _list_messages(g: GraphClient, skip_ids: set[str]) -> list[dict]:
    path = f"/users/{g.user_id}/messages"
    params: dict | None = {
        "$filter": f"receivedDateTime ge {SINCE} and receivedDateTime lt {UNTIL}",
        "$select": "id,parentFolderId,categories,receivedDateTime",
        "$orderby": "receivedDateTime asc",
        "$top": "100",
    }
    rows: list[dict] = []
    while path:
        data = _get_json(g, path, params)
        params = None
        for item in data.get("value") or []:
            if str(item.get("parentFolderId") or "") in skip_ids:
                continue
            mid = str(item.get("id") or "")
            if not mid:
                continue
            cats = [str(c) for c in (item.get("categories") or []) if str(c).strip()]
            rows.append({"id": mid, "categories": cats})
        link = data.get("@odata.nextLink")
        path = _next_path(link) if link else ""
    return rows


def _body_text(g: GraphClient, mail_id: str) -> str:
    data = _get_json(
        g,
        f"/users/{g.user_id}/messages/{mail_id}",
        {"$select": "body"},
    )
    body = data.get("body") or {}
    content = body.get("content") or ""
    if str(body.get("contentType") or "").lower() == "html":
        return _html_to_text(content)
    return content


def _issue_names(categories: list[str]) -> list[str]:
    return [c for c in categories if c.startswith("问题:")]


def _write(stats: dict) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    _env_loader.load_env()
    ensure_trust_store()
    lock_fd = _acquire_scan_lock(wait=False)
    if lock_fd is None:
        print("LOCK_BUSY")
        return 1
    stats = {
        "window": "2026-10-25..2026-10-26 Asia/Shanghai",
        "seen": 0,
        "changed": 0,
        "same": 0,
        "red": 0,
        "semantic": 0,
        "err": 0,
        "transitions": {},
    }
    try:
        tenant = os.environ["AZURE_AD_TENANT_ID"]
        client = os.environ["AZURE_AD_CLIENT_ID"]
        user = os.environ.get("MAILBOX_USER", "me")
        token = get_token(tenant, client)
        g = GraphClient(token, user_id=user)
        llm = RetryLlm(AzureLLMClient())
        skip_ids = _folder_ids(g)
        rows = _list_messages(g, skip_ids)
        stats["listed"] = len(rows)
        _write(stats)
        print(f"LISTED {len(rows)}", flush=True)
        for row in rows:
            stats["seen"] += 1
            before = _issue_names(row["categories"])
            reds = human_issue_display_names(row["categories"])
            body = ""
            tag_llm = None
            if reds:
                stats["red"] += 1
            else:
                stats["semantic"] += 1
                try:
                    body = _body_text(g, row["id"])
                except Exception:
                    stats["err"] += 1
                    continue
                tag_llm = llm
            rec = {"applied": False}
            for _attempt in range(3):
                rec = push_fact_tags(g, row["id"], body, llm=tag_llm)
                if not rec.get("error"):
                    break
                time.sleep(2)
            if rec.get("error"):
                stats["err"] += 1
            elif rec.get("applied"):
                stats["changed"] += 1
                after = _issue_names(rec.get("categories") or [])
                key = " | ".join(before) + " => " + (" | ".join(after) if after else "(去掉)")
                bucket = stats["transitions"]
                bucket[key] = bucket.get(key, 0) + 1
            else:
                stats["same"] += 1
            if stats["seen"] % 20 == 0:
                _write(stats)
                print(
                    f"PROGRESS seen={stats['seen']} changed={stats['changed']} "
                    f"same={stats['same']} err={stats['err']}",
                    flush=True,
                )
        _write(stats)
        print(
            f"DONE seen={stats['seen']} changed={stats['changed']} "
            f"same={stats['same']} red={stats['red']} semantic={stats['semantic']} "
            f"err={stats['err']}",
            flush=True,
        )
        return 0
    finally:
        _release_scan_lock(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
