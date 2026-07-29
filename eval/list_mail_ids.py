"""
list_mail_ids.py — 列出 mailbox 里最近 N 封邮件的 id + subject + sender。

不调 LLM，不 triage，只列邮件。给人工选邮件 ID 用。

跑法：
  python3 eval/list_mail_ids.py                          # 默认 MAILBOX_USER
  python3 eval/list_mail_ids.py --top 10                 # 拉 10 封
  python3 eval/list_mail_ids.py --folder Inbox           # Inbox
  python3 eval/list_mail_ids.py --folder "Junk Email"    # 垃圾邮件
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


def redact(s: str) -> str:
    s = re.sub(r"[A-HJ-NPR-Z0-9]{17}", "VIN_MASK", s)
    s = re.sub(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", "user@example.com",
               s, flags=re.IGNORECASE)
    s = re.sub(r"\+\d[\d\s-]{6,}", "PHONE_MASK", s)
    return s


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mailbox", help="mailbox UPN (默认 MAILBOX_USER)")
    p.add_argument("--folder", default="Inbox")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--filter-isread", choices=["true", "false", "all"],
                   default="false", help="isRead 过滤（默认只看未读）")
    args = p.parse_args()

    mailbox = args.mailbox or os.environ.get("MAILBOX_USER")
    if not mailbox or mailbox == "me":
        print("[ERR] MAILBOX_USER 没设具体 UPN", file=sys.stderr)
        return 2

    tenant_id = os.environ.get("AZURE_AD_TENANT_ID")
    client_id = os.environ.get("AZURE_AD_CLIENT_ID")
    if not tenant_id or not client_id:
        print("[ERR] AZURE_AD_TENANT_ID / AZURE_AD_CLIENT_ID 未配置")
        return 2

    token = get_token(tenant_id, client_id)
    headers = {"Authorization": f"Bearer {token}"}

    # 1) 拿 folder id
    r = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/{args.folder}",
        headers=headers, timeout=10,
    )
    if r.status_code != 200:
        print(f"[ERR] folder {r.status_code}: {r.text[:200]}")
        return 1
    folder = r.json()
    print(f"mailbox:   {mailbox}")
    print(f"folder:    {args.folder} (id={folder.get('id')[:20]}...)")
    print(f"total:     {folder.get('totalItemCount')}")
    print(f"unread:    {folder.get('unreadItemCount')}")
    print()

    # 2) 拉邮件列表
    params = {
        "$top": args.top,
        "$select": "id,subject,sender,receivedDateTime,isRead",
        "$orderby": "receivedDateTime desc",
    }
    if args.filter_isread != "all":
        params["$filter"] = f"isRead eq {args.filter_isread}"

    r = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/{args.folder}/messages",
        headers=headers, params=params, timeout=15,
    )
    if r.status_code != 200:
        print(f"[ERR] messages {r.status_code}: {r.text[:200]}")
        return 1

    msgs = r.json().get("value", [])
    print(f"=== 最近 {len(msgs)} 封邮件（{args.folder}, "
          f"isRead={args.filter_isread}）===")
    for i, m in enumerate(msgs, 1):
        sender = m.get("sender", {}).get("emailAddress", {}).get("address", "")
        print(f"\n[{i}] id={m.get('id')}")
        print(f"    subj    = {redact(m.get('subject', ''))[:80]}")
        print(f"    sender  = {redact(sender)}")
        print(f"    recv    = {m.get('receivedDateTime')}")
        print(f"    isRead  = {m.get('isRead')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
