"""
read_attachments.py — 列出并下载指定邮件的附件。

跑法：
  python3 eval/read_attachments.py --message-id "AQMkAG..."
  python3 eval/read_attachments.py --message-id "AQMkAG..." --save-dir /tmp/attachments/
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.outlook_drafts import _env_loader
_env_loader.load_env(verbose=False)

import requests

from agent.outlook_drafts import get_token


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--message-id", required=True)
    p.add_argument("--mailbox", help="mailbox UPN (默认 MAILBOX_USER)")
    p.add_argument("--save-dir", default="/tmp/emailagent_attachments")
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

    # 1) 列附件（用 $expand attachments）
    print(f"[1] 列 {mailbox} / messages/{args.message_id} 的附件 ...")
    r = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{args.message_id}",
        headers=headers,
        params={"$expand": "attachments"},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"[ERR] {r.status_code}: {r.text[:300]}")
        return 1
    msg = r.json()
    atts = msg.get("attachments", [])
    print(f"    邮件 subject: {msg.get('subject', '')[:60]}")
    print(f"    附件数: {len(atts)}")
    if not atts:
        print("    （没附件）")
        return 0

    # 2) 逐个附件打印元数据 + 下载
    save_dir = Path(args.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    print(f"    下载到: {save_dir}")
    print()

    for i, att in enumerate(atts, 1):
        name = att.get("name", "(no name)")
        ctype = att.get("contentType", "?")
        size = att.get("size", 0)
        att_id = att.get("id")
        is_inline = att.get("isInline", False)
        print(f"  ── 附件 {i} ──")
        print(f"  name       : {name}")
        print(f"  contentType: {ctype}")
        print(f"  size       : {size} 字节 ({size/1024:.1f} KB)")
        print(f"  isInline   : {is_inline}")
        print(f"  id         : {att_id}")

        # 3) 下载（用 /$value 端点）
        dl_url = (
            f"https://graph.microsoft.com/v1.0/users/{mailbox}"
            f"/messages/{args.message_id}/attachments/{att_id}/$value"
        )
        r = requests.get(dl_url, headers=headers, timeout=30)
        if r.status_code != 200:
            print(f"  download  : [ERR] {r.status_code}: {r.text[:200]}")
            continue
        content = r.content
        # 防路径穿越：用 att['name'] 但 strip ../ 和绝对路径前缀
        safe_name = Path(name).name  # 取最后一段
        out_path = save_dir / safe_name
        out_path.write_bytes(content)
        print(f"  download  : OK → {out_path} ({len(content)} 字节)")
        # 简单判断类型
        if ctype.startswith("image/"):
            print(f"               ← 图片，可视化/OCR 处理")
        elif "pdf" in ctype:
            print(f"               ← PDF，文本提取处理")
        else:
            print(f"               ← 其它类型")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
