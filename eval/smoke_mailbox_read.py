"""
smoke_mailbox_read.py — read-only 验证：service principal 能不能读指定 mailbox。

只读不写：不删 / 不改 isRead / 不动原邮件。如果能读，会打印 1 封邮件的
subject / sender / received_at（脱敏后）。如果不能读，会打印错误码 + 建议。

跑法：
  python3 eval/smoke_mailbox_read.py                    # 默认读 MAILBOX_UK
  python3 eval/smoke_mailbox_read.py --mailbox ...@x   # 指定 mailbox
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.outlook_drafts import _env_loader
_env_loader.load_env(verbose=True)

import requests

from agent.outlook_drafts import get_token


def redact(s: str) -> str:
    """粗略 PII 脱敏（VIN / email / phone），保证 log 不漏。"""
    import re
    s = re.sub(r"[A-HJ-NPR-Z0-9]{17}", "VIN_MASK", s)
    s = re.sub(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", "user@example.com", s, flags=re.IGNORECASE)
    s = re.sub(r"\+\d[\d\s-]{6,}", "PHONE_MASK", s)
    return s


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mailbox", help="mailbox UPN (默认 = MAILBOX_UK)")
    p.add_argument("--folder", default="Inbox")
    p.add_argument("--top", type=int, default=1)
    args = p.parse_args()

    mailbox = args.mailbox or os.environ.get("MAILBOX_UK")
    if not mailbox:
        print("[ERR] 没指定 mailbox", file=sys.stderr)
        return 2

    tenant_id = os.environ.get("AZURE_AD_TENANT_ID")
    client_id = os.environ.get("AZURE_AD_CLIENT_ID")
    if not tenant_id or not client_id:
        print("[ERR] AZURE_AD_TENANT_ID / AZURE_AD_CLIENT_ID 未配置")
        return 2

    print(f"mailbox: {mailbox}")
    token = get_token(tenant_id, client_id)
    print(f"[OK] 拿到 token（{len(token)} 字符）")

    # 1) 拿 folder id（Inbox 的 ID 在每个 mailbox 里不一样）
    print(f"\n[1] GET /users/{mailbox}/mailFolders/{args.folder} ...")
    r = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/{args.folder}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    print(f"    {r.status_code} {r.reason}")
    if r.status_code != 200:
        print(f"    {r.text[:300]}")
        return 1
    folder = r.json()
    print(f"    folder id={folder.get('id')[:20]}...  "
          f"totalItemCount={folder.get('totalItemCount')}  "
          f"unreadItemCount={folder.get('unreadItemCount')}")

    # 2) 拉前 N 封邮件
    print(f"\n[2] GET /users/{mailbox}/mailFolders/{args.folder}/messages?$top={args.top} ...")
    r = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/{args.folder}/messages",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "$top": args.top,
            "$select": "id,subject,sender,receivedDateTime,isRead,bodyPreview",
        },
        timeout=10,
    )
    print(f"    {r.status_code} {r.reason}")
    if r.status_code != 200:
        print(f"    {r.text[:300]}")
        return 1
    msgs = r.json().get("value", [])
    print(f"    拿到 {len(msgs)} 封邮件")
    for m in msgs:
        sender = m.get("sender", {}).get("emailAddress", {}).get("address", "")
        print(f"    - id={m.get('id')[:20]}...")
        print(f"      subject    = {redact(m.get('subject', ''))[:80]}")
        print(f"      sender     = {redact(sender)}")
        print(f"      received   = {m.get('receivedDateTime')}")
        print(f"      isRead     = {m.get('isRead')}")
        print(f"      bodyPreview= {redact(m.get('bodyPreview', ''))[:120]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
