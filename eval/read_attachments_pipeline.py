"""
read_attachments_pipeline.py — 单封邮件 → 拉附件 → 下载 image → 调 vision → 输出 JSON。

不动 PoC 主流程（outlook_drafts.py / email_agent.py），
不调 sendMail / 不删 / 不改 isRead。
不读原邮件内容（只读附件元数据 + 下载 image/* + 调 vision）。

跑法：
  python3 eval/read_attachments_pipeline.py --message-id "AQMkAG..."
  python3 eval/read_attachments_pipeline.py --message-id "..." --save-dir /tmp/att/
  python3 eval/read_attachments_pipeline.py --message-id "..." --no-vision   # 只下载不调 LLM

输出：
  /tmp/attachments_pipeline/<message_id>.json
  {
    "mail": {...},
    "attachments": [
      {
        "id": "...", "name": "IMG_6079.png", "contentType": "image/png",
        "size_bytes": 4213976,
        "saved_path": "/tmp/att/IMG_6079.png",
        "vision": {
          "model": "gpt-5.4",
          "prompt_tokens": 1649,
          "completion_tokens": 2234,
          "description": "（意大利语 iSMART app 报错界面，错误 59，...）"
        }
      }
    ]
  }
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
from pathlib import Path

ROOT = Path("/Users/uxoxsn/Desktop/AI/hello-agents/Co-creation-projects/uxoxsn-OverseasVehicleEmailAgent")
sys.path.insert(0, str(ROOT))

from agent.outlook_drafts import _env_loader, get_token  # noqa: E402
_env_loader.load_env(verbose=False)

import requests  # noqa: E402


# 调 vision 的最大图片尺寸（字节）。超出就跳过，避免 token 爆炸。
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB


VISION_PROMPT = """\
请用中文详细描述这张手机截图：
1) 这是哪个 app 的哪个页面（顶部标题 / tab 名）？
2) 显示了哪些车型 / 车牌 / 车辆状态信息？
3) 有没有错误 / 报错信息？如果有，**逐字引用完整错误原文**（含括号里错误码）
4) 截图分辨率多大？可见哪些按钮 / 图标？
5) 这张图看起来是 iSMART app 报错界面吗？为什么？
"""


def fetch_mail_attachments(mailbox: str, message_id: str, token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}",
        headers=headers,
        params={"$expand": "attachments"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def download_attachment(mailbox: str, message_id: str, attachment_id: str,
                        token: str, save_dir: Path) -> bytes:
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}"
        f"/attachments/{attachment_id}/$value",
        headers=headers, timeout=60,
    )
    r.raise_for_status()
    save_dir.mkdir(parents=True, exist_ok=True)
    return r.content


def call_vision(image_bytes: bytes, mime: str) -> dict:
    """调 Azure OpenAI gpt-5.4 vision。返回 {description, prompt_tokens, completion_tokens}。"""
    from openai import AzureOpenAI  # 延迟 import，避免没装就 fail
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    api_key = os.environ["AZURE_OPENAI_API_KEY"]
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

    client = AzureOpenAI(
        api_key=api_key, api_version=api_version, azure_endpoint=endpoint,
        timeout=120,
    )
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"

    resp = client.chat.completions.create(
        model=deployment,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        reasoning_effort="low",
        max_completion_tokens=8192,
    )
    text = (resp.choices[0].message.content or "").strip()
    u = getattr(resp, "usage", None)
    out = {
        "model": deployment,
        "description": text,
        "finish_reason": getattr(resp.choices[0], "finish_reason", None),
        "prompt_tokens": getattr(u, "prompt_tokens", 0) if u else 0,
        "completion_tokens": getattr(u, "completion_tokens", 0) if u else 0,
    }
    ptd = getattr(u, "prompt_tokens_details", None) if u else None
    if ptd:
        out["cached_tokens"] = getattr(ptd, "cached_tokens", 0) or 0
    ctd = getattr(u, "completion_tokens_details", None) if u else None
    if ctd:
        out["reasoning_tokens"] = getattr(ctd, "reasoning_tokens", 0) or 0
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--message-id", required=True)
    p.add_argument("--mailbox", help="默认 MAILBOX_USER")
    p.add_argument("--save-dir", default="/tmp/attachments_pipeline")
    p.add_argument("--no-vision", action="store_true", help="只下载不调 LLM")
    p.add_argument("--max-bytes", type=int, default=MAX_IMAGE_BYTES,
                   help=f"超过此字节的 image 跳过（默认 {MAX_IMAGE_BYTES}）")
    p.add_argument("--max-images", type=int, default=99,
                   help="最多 vision 几张图（多图邮件防 token 爆炸）")
    args = p.parse_args()

    mailbox = args.mailbox or os.environ.get("MAILBOX_USER")
    if not mailbox or mailbox == "me":
        print("[ERR] MAILBOX_USER 没设具体 UPN", file=sys.stderr)
        return 2

    token = get_token(os.environ["AZURE_AD_TENANT_ID"], os.environ["AZURE_AD_CLIENT_ID"])
    save_dir = Path(args.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    print(f"mailbox   : {mailbox}")
    print(f"message_id: {args.message_id}")
    print(f"save_dir  : {save_dir}")
    print()

    # ── 1. 拉邮件 + 附件元数据 ─────────────────────────────────────────
    print("─── 1) GET /messages/{id}?$expand=attachments ──")
    msg = fetch_mail_attachments(mailbox, args.message_id, token)
    mail_meta = {
        "id":       msg.get("id"),
        "subject":  msg.get("subject"),
        "sender":   msg.get("sender", {}).get("emailAddress", {}).get("address"),
        "received": msg.get("receivedDateTime"),
        "isRead":   msg.get("isRead"),
    }
    print(f"  subject : {mail_meta['subject']}")
    print(f"  sender  : {mail_meta['sender']}")
    print(f"  received: {mail_meta['received']}")
    atts = msg.get("attachments", [])
    print(f"  附件数: {len(atts)}")
    print()

    result_attachments = []
    vision_count = 0
    for i, att in enumerate(atts, 1):
        name     = att.get("name", "(no name)")
        ctype    = att.get("contentType", "?")
        size     = att.get("size", 0)
        att_id   = att.get("id")
        is_inline = att.get("isInline", False)
        is_image = ctype.startswith("image/")
        print(f"  ── 附件 {i} ──")
        print(f"  name       : {name}")
        print(f"  contentType: {ctype}")
        print(f"  size       : {size} 字节 ({size/1024:.1f} KB)")
        print(f"  isInline   : {is_inline}")

        rec = {
            "id":          att_id,
            "name":        name,
            "contentType": ctype,
            "size_bytes":  size,
            "isInline":    is_inline,
            "is_image":    is_image,
            "saved_path":  None,
            "vision":      None,
        }

        if not is_image:
            print(f"  → 跳过（非 image/* 类型）")
            result_attachments.append(rec)
            print()
            continue

        if size > args.max_bytes:
            print(f"  → 跳过（size {size} > max {args.max_bytes}）")
            rec["skip_reason"] = f"size {size} > max_bytes {args.max_bytes}"
            result_attachments.append(rec)
            print()
            continue

        # ── 2. 下载 ────────────────────────────────────────────────
        print(f"  → 下载 ({ctype}, {size/1024:.1f} KB) ...")
        try:
            content = download_attachment(mailbox, args.message_id, att_id, token, save_dir)
        except Exception as e:
            print(f"  [ERR] download failed: {e}")
            rec["download_error"] = str(e)
            result_attachments.append(rec)
            print()
            continue

        # 用 att['name'] 取最后一段防路径穿越
        safe_name = Path(name).name
        out_path = save_dir / safe_name
        out_path.write_bytes(content)
        rec["saved_path"] = str(out_path)
        rec["downloaded_bytes"] = len(content)
        print(f"  → 已存: {out_path} ({len(content)} 字节)")

        # ── 3. vision（除非 --no-vision）────────────────────────────
        if args.no_vision:
            print(f"  → 跳过 vision（--no-vision）")
        elif vision_count >= args.max_images:
            print(f"  → 跳过 vision（已达 --max-images={args.max_images}）")
            rec["skip_reason"] = f"max_images {args.max_images} reached"
        else:
            print(f"  → 调 gpt-5.4 vision ...")
            try:
                vision = call_vision(content, ctype)
                rec["vision"] = vision
                vision_count += 1
                print(f"     prompt={vision['prompt_tokens']} "
                      f"completion={vision['completion_tokens']} "
                      f"finish={vision['finish_reason']}")
                desc = vision["description"]
                if len(desc) > 500:
                    print(f"     description（前 500 字）: {desc[:500]}...")
                else:
                    print(f"     description: {desc}")
            except Exception as e:
                print(f"  [ERR] vision failed: {e}")
                rec["vision_error"] = str(e)
        print()
        result_attachments.append(rec)

    # ── 4. 写 JSON ─────────────────────────────────────────────────────
    out_json = save_dir / f"{args.message_id}.json"
    payload = {
        "mail":        mail_meta,
        "attachments": result_attachments,
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[wrote] {out_json}")

    # PII redact 检查：附件名里有没有 VIN 露出来？
    suspicious_names = [a["name"] for a in result_attachments
                        if re.search(r"[A-HJ-NPR-Z0-9]{17}", a.get("name", ""))]
    if suspicious_names:
        print(f"\n[WARN] 附件名疑似含 VIN：{suspicious_names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())