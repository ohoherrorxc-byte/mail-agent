"""
show_mail_content.py — 列出最近 N 封邮件的完整内容（subject / sender / body）。

不调 LLM，只读 + 脱敏。给人工核对邮件内容用。

跑法：
  python3 eval/show_mail_content.py                          # 拉 3 封最新未读
  python3 eval/show_mail_content.py --top 5                   # 拉 5 封
  python3 eval/show_mail_content.py --filter-isread all       # 不限已读未读
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.outlook_drafts import _env_loader
_env_loader.load_env(verbose=False)

import requests

from agent.outlook_drafts import get_token


# 脱敏规则（跟 redact_ledger.py 一致）
def redact(s: str) -> str:
    if not s:
        return s
    s = re.sub(r"[A-HJ-NPR-Z0-9]{17}", "VIN_MASK", s)
    s = re.sub(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", "user@example.com",
               s, flags=re.IGNORECASE)
    s = re.sub(r"\+\d[\d\s-]{6,}", "PHONE_MASK", s)
    return s


def html_to_text(html: str) -> str:
    """粗略 HTML → 文本"""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">"))
    return text.strip()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mailbox")
    p.add_argument("--folder", default="Inbox")
    p.add_argument("--top", type=int, default=3)
    p.add_argument("--filter-isread", choices=["true", "false", "all"],
                   default="false")
    args = p.parse_args()

    mailbox = args.mailbox or os.environ.get("MAILBOX_USER")
    if not mailbox or mailbox == "me":
        print("[ERR] MAILBOX_USER 没设具体 UPN", file=sys.stderr)
        return 2

    token = get_token(
        os.environ["AZURE_AD_TENANT_ID"],
        os.environ["AZURE_AD_CLIENT_ID"],
    )
    headers = {"Authorization": f"Bearer {token}"}

    params = {
        "$top": args.top,
        "$select": "id,subject,sender,receivedDateTime,isRead,body,bodyPreview",
        "$orderby": "receivedDateTime desc",
    }
    if args.filter_isread != "all":
        params["$filter"] = f"isRead eq {args.filter_isread}"

    r = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/{args.folder}/messages",
        headers=headers, params=params, timeout=15,
    )
    if r.status_code != 200:
        print(f"[ERR] {r.status_code}: {r.text[:200]}")
        return 1

    msgs = r.json().get("value", [])
    print(f"=== {mailbox} / {args.folder} / {len(msgs)} 封 / "
          f"isRead={args.filter_isread} ===\n")
    for i, m in enumerate(msgs, 1):
        sender = m.get("sender", {}).get("emailAddress", {}).get("address", "")
        body = m.get("body", {})
        content = body.get("content", "")
        if body.get("contentType") == "html":
            content = html_to_text(content)
        elif not content:
            content = m.get("bodyPreview", "")
        print(f"━━━━━━ Mail {i} ━━━━━━")
        print(f"id       : {m.get('id')}")
        print(f"subject  : {m.get('subject', '')}")
        print(f"sender   : {sender}")
        print(f"received : {m.get('receivedDateTime')}")
        print(f"isRead   : {m.get('isRead')}")
        print(f"body type: {body.get('contentType', 'text')}")
        print()
        print("─── body (脱敏后) ───")
        print(redact(content))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
