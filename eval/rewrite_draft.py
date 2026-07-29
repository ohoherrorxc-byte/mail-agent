"""
rewrite_draft.py — B 方案 demo：
拿 Mail 2 现有草稿正文（用错模板：GDPR 通知型）+ 客户完整 thread + 已知诊断
→ 调 gpt-5.4 改写为"基于已知诊断的 follow-up 回复"（按客户语言：意大利语）
→ 用 Graph API createReply 在 Drafts 落新草稿

**遵守 CLAUDE.md 硬约束**：
- 不删旧草稿（旧草稿留在 Drafts，用户人工决定怎么处理）
- 不调 sendMail
- 不动原邮件 isRead

跑法：
  PYTHONPATH=. python3 eval/rewrite_draft.py
"""
from __future__ import annotations

import os
import sys
import re
import json
from pathlib import Path

ROOT = Path("/Users/uxoxsn/Desktop/AI/hello-agents/Co-creation-projects/uxoxsn-OverseasVehicleEmailAgent")
sys.path.insert(0, str(ROOT))

from agent.outlook_drafts import _env_loader, get_token  # noqa: E402
_env_loader.load_env(verbose=False)

import requests  # noqa: E402

from openai import AzureOpenAI  # noqa: E402
from pydantic import BaseModel  # noqa: E402


# ── 客户诊断（从 Mail 2 thread + vision 抽取，硬编码为已知事实）──────────
CUSTOMER_DIAGNOSIS = {
    "country":      "Italia",
    "brand":        "MG",
    "model":        "MG3",
    "vin":          "VIN_MASK（已 redact）",
    "device":       "iPhone 16 Pro",
    "os":           "iOS 26.5.2",
    "app_version":  "iSMART V3.1.5",
    "error_code":   "59 (原文: 'The request failed. Check the condition of the vehicle and try again. (59)')",
    "issue_summary": "客户报告 iSMART app 无法开锁 / 关锁（lock/unlock failed），错误码 59，并附了意大利语报错界面截图。",
    "screenshot":   "已附（IMG_6079.png，意大利语 UI，错误信息 'La richiesta non è riuscita. Controllare lo stato del veicolo e riprovare. (59)'）",
    "thread_position": "follow-up（thread 第 3 封：7/15 客户首封 → 7/23 客服问诊 → 7/23 客户补诊断）",
    "customer_lang":  "客户用意大利语，但草稿固定英文（en-US，CLAUDE.md 守则 5）",
}


# ── LLM 改写 prompt ──────────────────────────────────────────────────────
REWRITE_SYSTEM = """\
你是 iSmart 海外售后邮件改写助手，专做"问诊 / GDPR 通知型模板" → \
"基于已知诊断的 follow-up 回复"改写。

你的输出必须遵守 6 条守则（来自 CLAUDE.md 用户原话硬约束 + 红线）：

1. **不编造技术原因** —— 错误码 59 / 47 / iSMART app 故障的具体技术原因你不一定知道，\
不要说"是因为 XX 模块 / 蓝牙 / 4G 信号 / BMS" 等具体技术原因。

2. **不给具体操作步骤** —— 不要说"请卸载重装 app"、"请重启手机"、"请检查网络"、\
"请打开 XX 设置"等具体步骤。你不知道哪种有效。

3. **不重复问已提供信息** —— Country / Brand / Model / VIN / Device / OS / App version / \
Error code / 截图 客户都已提供，不要再要。

4. **不向客户承诺时间 / SLA** —— 不能写 "1-2 business days"、"within 24 hours"、\
"X hours / days to resolve" 等具体时间承诺。可以写 "we will follow up"、\
"our team will review and get back to you"、"we are looking into this" \
这类无时间锚的话术。

5. **回复语言固定为英文（en-US）** —— 不论客户用哪种语言（it / es / fr / de / ja / zh）\
写邮件，草稿一律用英文。客户语言字段保留但不参与回复语言选择。

6. **保留模板签名** —— "Best regards, ismart-service team" 不改。

你可以说的（事实安全的话术）：

- We have received your diagnostic information (country / model / device / error code / screenshot)
- Your case has been escalated to our technical support team
- We will follow up / Our team is looking into this
- For urgent situations, contact your local MG service / roadside assistance
- Keep closing signature "Best regards, ismart-service team"

**回复语言**：en-US（固定英文，不跟客户语言）

**长度**：≤ 原模板长度。

**输出**：仅邮件正文（不含 subject，不含 meta 解释），保持抬头 "Dear customer," 段 + 签名段。
"""

REWRITE_USER_TEMPLATE = """\
## 客户邮件 thread（已 redact VIN/email/phone）

{customer_thread}

## 客户已提供诊断信息

{customer_diag}

## 原模板填好的回复（不要直接发 — 这是 GDPR 通知型，不适合 follow-up）

{draft_old}

## 改写输出（意大利语邮件正文）
"""


def fetch_mail_thread(mailbox: str, message_id: str, token: str) -> dict:
    """拉邮件完整 body + thread 历史（如果有 conversationId）"""
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}",
        headers=headers,
        params={"$select": "subject,sender,receivedDateTime,body,bodyPreview,conversationId"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def fetch_draft_body(mailbox: str, draft_id: str, token: str) -> str:
    """拉现有草稿正文（HTML → 文本）"""
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{draft_id}",
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()
    body_html = r.json().get("body", {}).get("content", "")
    text = re.sub(r"<br\s*/?>", "\n", body_html, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&nbsp;", " ").replace("&amp;", "&").strip()


def rewrite_with_llm(customer_thread: str, customer_diag: dict, draft_old: str) -> str:
    """调 gpt-5.4 改写"""
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    api_key = os.environ["AZURE_OPENAI_API_KEY"]
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

    client = AzureOpenAI(
        api_key=api_key, api_version=api_version, azure_endpoint=endpoint,
        timeout=180,
    )

    diag_lines = "\n".join(f"- {k}: {v}" for k, v in customer_diag.items())
    prompt = REWRITE_USER_TEMPLATE.format(
        customer_thread=customer_thread,
        customer_diag=diag_lines,
        draft_old=draft_old,
    )

    print("─── 调 gpt-5.4 改写 (reasoning=low, max_tokens=8192) ───")
    resp = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": REWRITE_SYSTEM},
            {"role": "user",   "content": prompt},
        ],
        reasoning_effort="low",
        max_completion_tokens=8192,
    )
    text = (resp.choices[0].message.content or "").strip()
    u = getattr(resp, "usage", None)
    if u:
        print(f"usage.prompt={u.prompt_tokens} completion={u.completion_tokens}")
        ptd = getattr(u, "prompt_tokens_details", None)
        if ptd:
            print(f"usage.cached={ptd.cached_tokens}")
    return text


def html_to_body_html(plain_text: str) -> str:
    """把改写后的纯文本包成最小可用的 HTML body（Outlook 草稿需要 HTML）"""
    paras = [p.strip() for p in plain_text.split("\n\n") if p.strip()]
    html = "".join(
        "<p style=\"margin:0 0 12px 0;font-family:Calibri,sans-serif;font-size:11pt\">"
        + p.replace("\n", "<br/>") + "</p>"
        for p in paras
    )
    return f"<html><body>{html}</body></html>"


def create_draft(mailbox: str, message_id: str, body_html: str, subject: str, token: str) -> str:
    """createReply 落新草稿，返回新 draft_id。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
        },
        "comment": "",   # 不传 comment → 不预填 reply
    }
    r = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}/createReply",
        headers=headers, json=payload, timeout=30,
    )
    print(f"[createReply] status={r.status_code}")
    if r.status_code not in (200, 201):
        print(f"[createReply] body: {r.text[:400]}")
        raise RuntimeError(f"createReply failed: {r.status_code}")
    return r.json().get("id")


def main() -> int:
    # ── 1. 准备 inputs ────────────────────────────────────────────────
    mailbox = "customer.care@ismart-service.com"
    target_mail_id = (
        "AQMkAGY3ZDZjZmVkLTRiM2ItNGZjYS04ZjYxLWU0NmQxM2FjNTI5NwBGAAADGwln3grRz0m"
        "WW3GQGF0esQcAU00SOLVVf0CK78DM4WwhtAAAAgEMAAAAU00SOLVVf0CK78DM4WwhtAAB"
        "KLxJPAAAAA=="
    )
    old_draft_id = (
        "AQMkAGY3ZDZjZmVkLTRiM2ItNGZjYS04ZjYxLWU0NmQxM2FjNTI5NwBGAAADGwln3grRz0m"
        "WW3GQGF0esQcAU00SOLVVf0CK78DM4WwhtAAAAgEPAAAAU00SOLVVf0CK78DM4WwhtAAB"
        "KLyy9gAAAA=="
    )

    token = get_token(
        os.environ["AZURE_AD_TENANT_ID"],
        os.environ["AZURE_AD_CLIENT_ID"],
    )

    print("─── 1) 拉 Mail 2 thread ──")
    mail = fetch_mail_thread(mailbox, target_mail_id, token)
    print(f"  subject : {mail.get('subject')}")
    print(f"  sender  : {mail.get('sender', {}).get('emailAddress', {}).get('address')}")
    thread_text = mail.get("bodyPreview") or ""

    # bodyPreview 太短，加 body 完整
    body_html = mail.get("body", {}).get("content", "")
    body_text = re.sub(r"<br\s*/?>", "\n", body_html, flags=re.I)
    body_text = re.sub(r"</p>", "\n\n", body_text, flags=re.I)
    body_text = re.sub(r"<[^>]+>", "", body_text)
    thread_full = (body_text or thread_text).replace("&nbsp;", " ").strip()

    print(f"  thread length: {len(thread_full)} chars")
    print()

    print("─── 2) 拉现有草稿正文 ──")
    draft_old = fetch_draft_body(mailbox, old_draft_id, token)
    print(f"  draft length: {len(draft_old)} chars")
    print(f"  --- 原模板填好正文（前 200 字） ---")
    print(f"  {draft_old[:200]}...")
    print()

    # ── 2. LLM 改写 ──────────────────────────────────────────────────
    rewritten = rewrite_with_llm(thread_full, CUSTOMER_DIAGNOSIS, draft_old)
    print()
    print("=" * 60)
    print("改写后正文（意大利语）")
    print("=" * 60)
    print(rewritten)
    print("=" * 60)
    print()

    # ── 3. 落新草稿 ──────────────────────────────────────────────────
    print("─── 3) 落新草稿到 Drafts ──")
    subject = "RE: " + mail.get("subject", "").lstrip("Re: ").lstrip("re: ").lstrip()
    # subject: "RE: Problemi con il veicolo o l'app iSMART"
    body_html_new = html_to_body_html(rewritten)
    new_draft_id = create_draft(mailbox, target_mail_id, body_html_new, subject, token)
    print(f"  new draft_id: {new_draft_id}")
    print()
    print("旧草稿保留在 Drafts（user 决定怎么处理）。")
    print(f"  旧: {old_draft_id}")
    print(f"  新: {new_draft_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())