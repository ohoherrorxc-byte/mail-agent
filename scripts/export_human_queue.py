#!/usr/bin/env python3
"""把 Inbox 里打了「转人工」的邮件导出到台账目录 xlsx。

只读 Graph，不建草稿、不 send、不改 isRead、不删信。

  python3 scripts/export_human_queue.py

默认写：
  ~/Documents/private/iSmart-email-ledger/转人工_YYYYMMDD_HHMM.xlsx
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.outlook_drafts import (  # noqa: E402
    GRAPH_BASE,
    QUEUE_HUMAN_CATEGORY,
    GraphClient,
    get_token,
    _html_to_text,
)

# Excel 单格上限 32767
_XLSX_CELL_MAX = 32767
from scripts.redact_ledger import redact_text  # noqa: E402

LEDGER = Path.home() / "Documents/private/iSmart-email-ledger"


def _next_path(link: str) -> str:
    url = link or ""
    if url.startswith(GRAPH_BASE):
        return url[len(GRAPH_BASE):]
    marker = "/v1.0"
    i = url.find(marker)
    return url[i + len(marker):] if i >= 0 else url


def _addr(msg: dict) -> str:
    ea = ((msg.get("from") or msg.get("sender") or {}).get("emailAddress") or {})
    name = (ea.get("name") or "").strip()
    addr = (ea.get("address") or "").strip()
    if name and addr:
        return f"{name} <{addr}>"
    return addr or name


def list_human_queue(g: GraphClient, *, folder: str = "Inbox") -> list[dict]:
    cat = QUEUE_HUMAN_CATEGORY.replace("'", "''")
    path = f"/users/{g.user_id}/mailFolders/{folder}/messages"
    params: dict[str, str] | None = {
        "$top": "80",
        "$select": (
            "id,subject,from,sender,receivedDateTime,isRead,"
            "categories,bodyPreview,webLink,conversationId"
        ),
        "$orderby": "receivedDateTime desc",
        "$filter": f"categories/any(c:c eq '{cat}')",
    }
    g.session.headers["ConsistencyLevel"] = "eventual"
    out: list[dict] = []
    while path:
        r = g._request("GET", path, params=params)
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("value") or [])
        nxt = data.get("@odata.nextLink") or ""
        if not nxt:
            break
        path = _next_path(nxt)
        if path.startswith("http"):
            path = path.split(GRAPH_BASE, 1)[-1]
        params = None
    g.session.headers.pop("ConsistencyLevel", None)
    for i, m in enumerate(out, 1):
        mid = m.get("id")
        if not mid:
            continue
        m["body_text"] = _fetch_body_text(g, mid) or (m.get("bodyPreview") or "")
        if i % 25 == 0:
            print(f"  正文 {i}/{len(out)}", flush=True)
    return out


def _fetch_body_text(g: GraphClient, mail_id: str) -> str:
    """只拉 body，不 expand 附件，避免超大 JSON 截断。"""
    try:
        data = g._get(
            f"/users/{g.user_id}/messages/{mail_id}",
            params={"$select": "id,body,bodyPreview"},
        )
    except Exception:
        return ""
    html = (data.get("body") or {}).get("content") or ""
    if not html:
        return data.get("bodyPreview") or ""
    text = _html_to_text(html) if "<" in html else html
    return text.strip()


def write_xlsx(rows: list[dict], dest: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = QUEUE_HUMAN_CATEGORY
    ws.append([
        "receivedDateTime", "from", "subject", "isRead",
        "categories", "body", "webLink", "id",
    ])
    for m in rows:
        body, _ = redact_text(m.get("body_text") or m.get("bodyPreview") or "")
        if len(body) > _XLSX_CELL_MAX:
            body = body[: _XLSX_CELL_MAX - 20] + "\n[truncated]"
        cats = "; ".join(m.get("categories") or [])
        ws.append([
            m.get("receivedDateTime") or "",
            _addr(m),
            m.get("subject") or "",
            bool(m.get("isRead")),
            cats,
            body,
            m.get("webLink") or "",
            m.get("id") or "",
        ])
    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)


def main() -> int:
    p = argparse.ArgumentParser(description="导出 Inbox「转人工」分类")
    p.add_argument("--folder", default="Inbox")
    p.add_argument("--out", help="xlsx 路径，默认写台账目录")
    args = p.parse_args()
    _env_loader = __import__("agent._env", fromlist=["load_env"])
    _env_loader.load_env(verbose=False)
    g = GraphClient(
        get_token(os.environ["AZURE_AD_TENANT_ID"], os.environ["AZURE_AD_CLIENT_ID"]),
        user_id=os.environ.get("MAILBOX_USER", "me"),
    )
    rows = list_human_queue(g, folder=args.folder)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M")
    dest = Path(args.out) if args.out else LEDGER / f"转人工_{stamp}.xlsx"
    write_xlsx(rows, dest)
    dest.chmod(0o600)
    print(f"{len(rows)} 封 → {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
