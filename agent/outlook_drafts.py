"""
outlook_drafts.py — M1.5：从 Outlook 读未读邮件 → template_reply → 在 Drafts 落草稿。

⚠️  硬约束（用户原话）⚠️
  1. **不能主动给客户发送邮件，智能建草稿** —— 永远不调 sendMail；
     只调 createReply（在 Drafts 落新草稿），人工事后审 / 改 / 点 Send。
  2. **不能有任何删除动作** —— 不调 DELETE / 不 move 到 Deleted Items /
     不 markAsRead / 不删任何邮件 / 不删任何草稿。原邮件 isRead 保持原状。
 违反任一条 = 严重安全事故。本模块以代码 guard 强制：见 GraphClient 的
  _FORBIDDEN_OPS 和 guard 方法。

流程：
  1. MSAL device code flow 登录（一次，token 缓存到 ~/.cache/emailagent/graph_token.json）
  2. 拉未读邮件（默认 20 封，可调）—— 不改 isRead
  3. 每封走 template_reply.run_template_reply() —— 1 个 LLM 调用
  4. 用 Graph API 在对应邮件下 createReply draft 并 PATCH body 为模板
  5. Outlook Drafts 文件夹里直接出现草稿，保留线程 / To / Cc / attachments

跳过条件（送人工，不建草稿）：
  - intent 在 xlsx 里没有匹配的模板
  - red_flags 非空（律师 / 媒体 / 安全事件）
  - region != "North America"（M1 守门）
  - urgency == "P0"（无论什么情况都人工）

用法：
  # 真用：读最新 20 封未读、建草稿
  python3 -m agent.outlook_drafts --once

  # Dry run：只读不建草稿
  python3 -m agent.outlook_drafts --once --dry-run

  # 自定义：拉 50 封、只处理 app_binding
  python3 -m agent.outlook_drafts --once --top 50 --intent-allowlist app_binding

认证准备：
  1. Azure Portal → App registrations → New (public client, no redirect URI)
  2. API permissions → Microsoft Graph → Delegated → Mail.ReadWrite
  3. Authentication → "Allow public client flows" = Yes
  4. 把 TENANT_ID / CLIENT_ID 写到 .env（见 .env.example）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# 启动时从 .env / .env.example 自动加载（如果存在）
from . import _env as _env_loader
_env_loader.load_env()

try:
    import msal
except ImportError:
    msal = None  # 仅在 --mock-mail 模式下不需；真连 Graph 时再提示

import requests

from .azure_llm import AzureLLMClient
from .email_agent import region_gate_should_escalate
from .schemas import TriageResult
from .template_reply import (
    load_xlsx,
    run_template_reply,
    fill_placeholders_naive,
    extract_placeholders,
)

LOG = logging.getLogger("outlook_drafts")
ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path.home() / ".cache" / "emailagent"
TOKEN_CACHE_FILE = CACHE_DIR / "graph_token.json"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = ["User.Read", "Mail.ReadWrite"]


class MailboxSafetyError(PermissionError):
    """违反 'no send, no delete' 硬约束时抛出。"""


# ── 硬约束白名单（这些方法以外的 Graph 调用都禁止）───────────────────────
# 检查时按子串匹配；列在这里 = 允许；不列 = 禁止。
# 见 GraphClient._request() 的白名单守卫。
_ALLOWED_GRAPH_PATTERNS: tuple[str, ...] = (
    # 读
    "GET /users/{user_id}/mailFolders/Inbox/messages",   # 读未读
    "GET /users/{user_id}/mailFolders/",                  # 读文件夹
    "GET /users/{user_id}/messages/",                     # 读邮件详情
    # 写 —— 仅在原邮件下建草稿、改新草稿 body
    "POST /users/{user_id}/messages/{mail_id}/createReply",  # 建草稿
    "PATCH /users/{user_id}/messages/{draft_id}",             # 改草稿 body（仅 draft）
    # M1.6: 读附件（用于 vision 集成；只 GET，不下载到 mailbox 之外的位置外泄）
    "GET /users/{user_id}/messages/{mail_id}/attachments/{attachment_id}/$value",
)


# ── 认证 ───────────────────────────────────────────────────────────────

def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_FILE.exists():
        cache.deserialize(TOKEN_CACHE_FILE.read_text())
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # chmod 700 on dir, 600 on file —— 防止 ~/.cache/emailagent 被
        # 同机其它用户 / 共享机器读到 Graph access + refresh token。
        # 重要：必须在 write_text 之后做（创建文件后才能 chmod）。
        try:
            CACHE_DIR.chmod(0o700)
        except OSError:
            pass  # Windows 等不支持的 FS 不报错
        TOKEN_CACHE_FILE.write_text(cache.serialize())
        try:
            TOKEN_CACHE_FILE.chmod(0o600)
        except OSError:
            pass


def get_token_client_credentials(
    tenant_id: str, client_id: str, client_secret: str,
    *, scopes: Optional[list[str]] = None,
) -> str:
    """client_credentials flow：service-to-service，不需 user 登录。

    用 ConfidentialClientApplication + client_secret 拿 token，scope 必须用
    `https://graph.microsoft.com/.default`（Azure AD 会按 app 配的
    Application permissions 全部授权）。
    跟 device_code 的区别：
      - 没有 user 上下文，token 属 app 自己
      - 不能访问 user mailbox（除非 Exchange 里给 service principal 加权限）
      - 不弹同意页、不需要 user 介入
    """
    if msal is None:
        print("[ERR] 需要 msal：pip install msal", file=sys.stderr)
        sys.exit(2)
    if scopes is None:
        scopes = ["https://graph.microsoft.com/.default"]
    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )
    result = app.acquire_token_for_client(scopes=scopes)
    if "access_token" not in result:
        raise RuntimeError(
            f"client_credentials 登录失败: "
            f"{result.get('error_description', result)}"
        )
    return result["access_token"]


def get_token(tenant_id: str, client_id: str) -> str:
    """拿 access token。

    优先级：
      1) 如果环境变量 AZURE_AD_CLIENT_SECRET 非空 → client_credentials flow
         （service principal，不需 user 登录）
      2) 否则 → device code flow（user 登录，token 缓存到 ~/.cache/）
    """
    secret = os.environ.get("AZURE_AD_CLIENT_SECRET", "").strip()
    if secret:
        LOG.info("auth: client_credentials flow (service principal)")
        return get_token_client_credentials(tenant_id, client_id, secret)

    if msal is None:
        print("[ERR] 需要 msal：pip install msal", file=sys.stderr)
        sys.exit(2)
    LOG.info("auth: device code flow (user login)")
    cache = _load_cache()
    app = msal.PublicClientApplication(
        client_id=client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )

    # 优先尝试静默拿
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            return result["access_token"]

    # 走 device code
    flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"device flow 启动失败: {flow}")
    print("=" * 60)
    print(f"打开： {flow['verification_uri']}")
    print(f"输入 code： {flow['user_code']}")
    print("=" * 60)
    try:
        webbrowser.open(flow["verification_uri"])
    except Exception:
        pass
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(f"登录失败: {result.get('error_description', result)}")
    _save_cache(cache)
    return result["access_token"]


# ── Graph 客户端 ────────────────────────────────────────────────────────

@dataclass
class Mail:
    id: str
    subject: str
    sender: str
    received_at: str
    body_text: str
    is_read: bool
    # M1.6: 附件元数据（不下载，只元数据）
    # [{id, name, contentType, size, isInline}]
    # M1.6.3 download_attachment() 才下载 image/*，且受 size 限制
    attachments: list[dict] = field(default_factory=list)


class GraphClient:
    def __init__(self, token: str, user_id: str = "me"):
        self.user_id = user_id
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        # 注册我们 createReply 创建的 draft id —— 只允许 PATCH 这些 id
        # 的 body，防止误改原邮件 / 别人邮件 / 任意邮件
        self._created_draft_ids: set[str] = set()

    # ── HTTP 层守卫：所有出站请求必须命中白名单 ────────────────────────
    def _request(self, method: str, path: str, *, json_body: Optional[dict] = None,
                 params: Optional[dict] = None) -> requests.Response:
        # method 黑名单（最严格守门）
        if method.upper() in ("DELETE",):
            raise MailboxSafetyError(
                f"[hard-constraint] 禁止 DELETE 调用：{method} {path}"
            )
        if method.upper() == "POST" and "/sendMail" in path:
            raise MailboxSafetyError(
                f"[hard-constraint] 禁止 sendMail：{method} {path}。"
                f"只能建草稿（createReply），人工事后点 Send。"
            )
        if method.upper() == "POST" and "/move" in path:
            raise MailboxSafetyError(
                f"[hard-constraint] 禁止 move：{method} {path}"
            )
        if method.upper() == "PATCH" and "isRead" in (json_body or {}):
            raise MailboxSafetyError(
                f"[hard-constraint] 禁止 PATCH isRead：{method} {path}。"
                f"原邮件读未读状态必须保持不变。"
            )
        # URL 子串白名单（把 {user_id} / {mail_id} / {draft_id} 占位符
        # 替换成实际值再匹配；* 是通配符，按段顺序匹配）
        sig = f"{method.upper()} {path}"
        if not any(_pattern_match(sig, p, self.user_id) for p in _ALLOWED_GRAPH_PATTERNS):
            raise MailboxSafetyError(
                f"[hard-constraint] Graph 调用不在白名单：{sig}。"
                f"如需新操作，先 review 是否违反 'no send / no delete' 硬约束。"
            )
        # PATCH messages 必须打在我们自己刚 createReply 的 draft id 上
        # （光看 URL 不够 —— /users/me/messages/{any_id} 同样命中，
        # 但 {any_id} 是原邮件 / 别人邮件就违规）
        if method.upper() == "PATCH" and "/messages/" in path:
            target_id = path.rsplit("/messages/", 1)[-1].strip("/")
            if target_id not in self._created_draft_ids:
                raise MailboxSafetyError(
                    f"[hard-constraint] PATCH messages 仅限本进程 createReply 创建的"
                    f" draft id；{target_id!r} 不在白名单。"
                    f"禁止改原邮件 / 别人邮件 / 任意邮件。"
                )
        return self.session.request(
            method, f"{GRAPH_BASE}{path}",
            json=json_body, params=params,
        )

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        r = self._request("GET", path, params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = self._request("POST", path, json_body=body)
        r.raise_for_status()
        return r.json() if r.text else {}

    def _patch(self, path: str, body: dict) -> None:
        r = self._request("PATCH", path, json_body=body)
        r.raise_for_status()

    # ── 显式 guard：未来误加 send / delete / move 时立刻爆错 ─────────────
    def send_mail(self, *args, **kwargs):  # pragma: no cover
        raise MailboxSafetyError(
            "[hard-constraint] send_mail 被禁止。本 agent 只能建草稿，人工发送。"
        )

    def delete_message(self, *args, **kwargs):  # pragma: no cover
        raise MailboxSafetyError(
            "[hard-constraint] delete_message 被禁止。不删任何邮件 / 草稿。"
        )

    def move_message(self, *args, **kwargs):  # pragma: no cover
        raise MailboxSafetyError(
            "[hard-constraint] move_message 被禁止。不移动邮件到任何文件夹。"
        )

    def mark_as_read(self, *args, **kwargs):  # pragma: no cover
        raise MailboxSafetyError(
            "[hard-constraint] mark_as_read 被禁止。原邮件读未读状态必须保持不变。"
        )

    def list_unread(self, top: int = 20, folder: str = "Inbox") -> list[Mail]:
        """读未读邮件（不改 isRead，不删，不动原邮件）。
        M1.6: 加 $expand=attachments 拿附件元数据（不下载）。"""
        data = self._get(
            f"/users/{self.user_id}/mailFolders/{folder}/messages",
            params={
                "$filter": "isRead eq false",
                "$top":  str(top),
                "$select": "id,subject,sender,receivedDateTime,bodyPreview,body,isRead",
                "$orderby": "receivedDateTime desc",
                "$expand": "attachments",
            },
        )
        out: list[Mail] = []
        for m in data.get("value", []):
            body = m.get("body", {}).get("content", "")
            out.append(Mail(
                id=m["id"],
                subject=m.get("subject", ""),
                sender=(m.get("sender") or {}).get("emailAddress", {}).get("address", ""),
                received_at=m.get("receivedDateTime", ""),
                body_text=_html_to_text(body) if "<" in body else body,
                is_read=m.get("isRead", False),
                attachments=[
                    {
                        "id":          a.get("id"),
                        "name":        a.get("name", ""),
                        "contentType": a.get("contentType", ""),
                        "size":        a.get("size", 0),
                        "isInline":    a.get("isInline", False),
                    }
                    for a in m.get("attachments", [])
                ],
            ))
        return out

    def get_message_by_id(self, mail_id: str) -> Optional[Mail]:
        """按 Graph message ID 取单封邮件（不动原邮件，只读）。
        找不到 / 权限不够返回 None。
        M1.6: 加 $expand=attachments 拿附件元数据。"""
        try:
            m = self._get(
                f"/users/{self.user_id}/messages/{mail_id}",
                params={
                    "$select": "id,subject,sender,receivedDateTime,body,isRead",
                    "$expand": "attachments",
                },
            )
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return None
            raise
        body = m.get("body", {}).get("content", "")
        return Mail(
            id=m["id"],
            subject=m.get("subject", ""),
            sender=(m.get("sender") or {}).get("emailAddress", {}).get("address", ""),
            received_at=m.get("receivedDateTime", ""),
            body_text=_html_to_text(body) if "<" in body else body,
            is_read=m.get("isRead", False),
            attachments=[
                {
                    "id":          a.get("id"),
                    "name":        a.get("name", ""),
                    "contentType": a.get("contentType", ""),
                    "size":        a.get("size", 0),
                    "isInline":    a.get("isInline", False),
                }
                for a in m.get("attachments", [])
            ],
        )

    def create_draft_reply(
        self,
        mail_id: str,
        body_text: str,
        body_html: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> str:
        """在指定邮件下挂草稿，返回草稿 id。
        用 createReply 保留线程 / To / Cc / attachments。
        原邮件本身**完全不动**（不删 / 不读 / 不改 isRead / 不 move）。
        草稿在 Outlook Drafts 文件夹里等人工事后点 Send。

        Args:
          subject: 可选。如果传，用它覆盖 createReply 默认的 'RE: <原主题>'
                  —— 一般用于把原主题里可能含 VIN / email 的部分脱敏后再用。
                  注意：必须保证是脱敏后的版本，code 不再替你 redact。
        """
        # 1) 先 createReply（空 body）—— 在原邮件下新建草稿，原邮件不动
        draft = self._post(
            f"/users/{self.user_id}/messages/{mail_id}/createReply",
            body={},
        )
        draft_id = draft["id"]
        self._created_draft_ids.add(draft_id)   # 注册：之后 PATCH 它才合法
        # 2) PATCH 新草稿 body（必）+ subject（可选）
        content_type = "html" if body_html else "text"
        content = body_html if body_html else body_text
        patch_body = {"body": {"contentType": content_type, "content": content}}
        if subject is not None:
            patch_body["subject"] = subject
        self._patch(
            f"/users/{self.user_id}/messages/{draft_id}",
            body=patch_body,
        )
        return draft_id

    # ── M1.6: 附件下载（仅 image/* + 受大小限制 + 不写 mailbox 之外）──
    def download_attachment(self, mail_id: str, attachment_id: str) -> bytes:
        """下载单个附件到内存（不落盘）。

        守门：
        - 仅 GET，方法 / URL 白名单已在 _request() 守
        - 调用方负责：caller 应先过滤 contentType.startswith('image/')
                       且 size <= MAX_IMAGE_BYTES（防 token 爆炸）
        - 返回 bytes，调用方负责 base64 + 喂 LLM
        - 文件大小上限在 process_mail 里 enforce（避免 vision 烧 token）
        """
        # _request() 守门：方法 GET + URL 白名单匹配
        r = self._request(
            "GET",
            f"/users/{self.user_id}/messages/{mail_id}/attachments/{attachment_id}/$value",
        )
        r.raise_for_status()
        return r.content


def _html_to_text(html: str) -> str:
    """粗略 HTML → 文本（够 triage 用）。"""
    import re
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">"))
    return text.strip()


def _pattern_match(sig: str, pattern: str, user_id: str) -> bool:
    """白名单匹配：把 {user_id}/{mail_id}/{draft_id} 替换成实际值 / 通配符，
    然后按 * 拆分，按段顺序 substring 匹配。"""
    expanded = (pattern
                .replace("{user_id}", user_id)
                .replace("{mail_id}",  "*")
                .replace("{draft_id}", "*"))
    pos = 0
    for part in expanded.split("*"):
        if not part:  # 连续 * 跳过
            continue
        idx = sig.find(part, pos)
        if idx == -1:
            return False
        pos = idx + len(part)
    return True


def _apply_post_filters(mails: list[Mail], args) -> list[Mail]:
    """按 --from-filter / --subject-contains / --intent-allowlist 过滤（mock + 真 Graph 共用）。
    --message-ids 走专门的 fetch 路径，不在这里处理。"""
    out = mails
    if getattr(args, "from_filter", None):
        f = args.from_filter.lower()
        out = [m for m in out if m.sender.lower() == f]
    if getattr(args, "subject_contains", None):
        s = args.subject_contains.lower()
        out = [m for m in out if s in m.subject.lower()]
    return out


# ── PII redact（用户原话：VIN 不要泄漏）───────────────────────────────────
# 复用 scripts/redact_ledger.py 的 redact_text：
#   VIN  [A-HJ-NPR-Z0-9]{17}            → VIN_MASK_<8 hex>
#   email (排除 Outlook CID)            → user_<8 hex>@example.com
#   phone (必须 + 前缀才动)             → PHONE_MASK_<8 hex>
# 调用前 redact 邮件正文（送 LLM 之前）；
# 调用后 redact subject / sender（log 输出 + draft subject）。
#
# 安全约束（M1 拍板）：redact 不可用时 **fail-closed** —— 不静默降级，
# 直接抛 PIIRedactionError，process_mail 捕获后转人工。绝不让客户原始
# 邮件 / VIN / 电话 / 邮箱进 LLM prompt 或写草稿 subject。


class PIIRedactionError(RuntimeError):
    """PII 脱敏不可用或失败时抛出。process_mail 捕获后转人工。"""


def _redact_mail(mail: Mail) -> tuple[Mail, dict]:
    """返回 (redacted_mail, pii_counts)。counts 用于审计。

    Raises:
        PIIRedactionError: redact_text 不可用（ImportError）或 redact 失败。
    """
    try:
        from scripts.redact_ledger import redact_text
    except ImportError as e:
        # fail-closed：宁可停 PII 流程，也别让原文进 LLM
        raise PIIRedactionError(
            "scripts.redact_ledger 不可用，拒绝 redact（fail-closed）。"
            "请把 scripts 加到 PYTHONPATH 或修复 import 路径后再跑。"
        ) from e

    try:
        new_body, body_c = redact_text(mail.body_text)
        new_subject, subj_c = redact_text(mail.subject)
        new_sender, sender_c = redact_text(mail.sender)
    except Exception as e:
        raise PIIRedactionError(
            f"redact_text 执行失败: {type(e).__name__}: {e}"
        ) from e

    counts = {
        "vin":   body_c["vin"]   + subj_c["vin"]   + sender_c["vin"],
        "email": body_c["email"] + subj_c["email"] + sender_c["email"],
        "phone": body_c["phone"] + subj_c["phone"] + sender_c["phone"],
    }
    if any(v > 0 for v in counts.values()):
        LOG.info("[redact] %s — vin=%d email=%d phone=%d",
                 mail.subject[:40], counts["vin"], counts["email"], counts["phone"])
    return (
        Mail(
            id=mail.id, subject=new_subject, sender=new_sender,
            received_at=mail.received_at, body_text=new_body, is_read=mail.is_read,
            # M1.6: attachments 透传（附件元数据本身不含 PII，无须 redact）
            attachments=mail.attachments,
        ),
        counts,
    )


# ── Mailbox 路由（M1.5: 按客户 region 选 mailbox，移植自 .env.email）──────
# 设计原则：与生产 MailUserServiceImpl 一致 —— mailbox 列表从 env 读，
# 按 triage 出的 region 路由；自家 mailbox 互发直接跳过（避免自回自）。
# 留 DB 方案给 M2（mailbox > 5 或需要 round-robin 时再做）。

REGION_MAILBOX = {
    "Europe":            "MAILBOX_UK",      # EU / UK 都先落 UK 信箱
    "North America":     "MAILBOX_DEFAULT",
    "Oceania":           "MAILBOX_AU",
    "Asia":              "MAILBOX_DEFAULT",
    "South America":     "MAILBOX_DEFAULT",
    "Africa":            "MAILBOX_DEFAULT",
}


@dataclass
class MailboxRoute:
    """route() 结果：落哪个 mailbox + 为什么 + 是否 self mail。"""
    mailbox_upn: str | None    # None 表示没匹配上（fallback 也没设）
    source: str                # "region_match" | "default" | "unknown"
    is_self_mail: bool         # 发件人 == 某个自家 mailbox


class MailboxRouter:
    """从 env 构造。按 customer region 选 mailbox，提供 self-mail 跳过判断。

    设计参考生产 MailUserServiceImpl：
      - mailbox 地址从配置读（这里用 env，生产用 Spring @Value）
      - skipMail()：发件人 == 任意自家 mailbox / 命中 skip domain → 跳过
    """

    def __init__(
        self,
        uk: str | None = None,
        au: str | None = None,
        default: str | None = None,
        skip_domains: str | None = None,
    ):
        self.uk = (uk or "").strip() or None
        self.au = (au or "").strip() or None
        self.default = (default or "").strip() or None
        # 自家 mailbox 全集（lowercase，做 self-mail 匹配）
        self._self_mailboxes: set[str] = {
            m.lower() for m in (self.uk, self.au, self.default) if m
        }
        # skip domain 列表（lowercase，子串匹配 sender email）
        self._skip_domains: list[str] = [
            d.strip().lower() for d in (skip_domains or "").split(",")
            if d.strip()
        ]
        # region → mailbox 名（占位符"MAILBOX_UK"等由 route() 替换）
        self._region_keys = dict(REGION_MAILBOX)

    def _resolve(self, key: str) -> str | None:
        if key == "MAILBOX_UK":     return self.uk
        if key == "MAILBOX_AU":     return self.au
        if key == "MAILBOX_DEFAULT": return self.default
        return None

    def route(self, customer_region: str | None) -> MailboxRoute:
        """按 customer region 选 mailbox。region 未知 / 未配置 → fallback default。"""
        key = self._region_keys.get(customer_region or "", "MAILBOX_DEFAULT")
        upn = self._resolve(key)
        # 兜底链：key → default → uk → au（PoC 早期 MAILBOX_DEFAULT 可能没设）
        if not upn:
            upn = self.default or self.uk or self.au
        if not upn:
            return MailboxRoute(mailbox_upn=None, source="unknown", is_self_mail=False)
        # source 判定：region 命中且 region 对应 mailbox 已配置 → region_match
        region_match = (
            customer_region in self._region_keys
            and self._resolve(self._region_keys[customer_region]) is not None
        )
        source = "region_match" if region_match else "default"
        return MailboxRoute(mailbox_upn=upn, source=source, is_self_mail=False)

    def should_skip_sender(self, sender_email: str | None) -> bool:
        """True = 跳过（发件人是自家 mailbox / 系统发件）。"""
        if not sender_email:
            return False
        s = sender_email.strip().lower()
        if s in self._self_mailboxes:
            return True
        for d in self._skip_domains:
            if d and d in s:
                return True
        return False


def build_router_from_env() -> MailboxRouter:
    """从环境变量构造 MailboxRouter（M1.5 默认入口）。"""
    return MailboxRouter(
        uk=os.environ.get("MAILBOX_UK"),
        au=os.environ.get("MAILBOX_AU"),
        default=os.environ.get("MAILBOX_DEFAULT"),
        skip_domains=os.environ.get("SKIP_SENDER_DOMAINS"),
    )


# ── 跳过规则 ──────────────────────────────────────────────────────────

SKIP_INTENTS = set()                # 显式禁止的 intent
DEFAULT_INTENT_ALLOWLIST: Optional[set[str]] = None   # None = 不限


# ── M2.5.4: 重复邮件检测（去重）────────────────────────────────────────
# 同一客户重复追问同一件事 → 只保留 1 条起草，剩下的 tag 关闭:无效邮件
# 设计：
#   - 进程内 LRU 缓存（OrderedDict，上限 500 条，超出 FIFO）
#   - fingerprint = subject normalize（去 Re:/Fwd: 前缀 + lowercase） +
#                    body 前 200 字的 char 3-gram Jaccard
#   - 阈值：默认 0.85；同 sender 命中 → 视为重复
#   - 不调 embedding API（省钱；M1 PoC 阶段够用；阈值可调）
#   - **本轮不调 Graph PATCH**：检测到重复后只 skip 起草 + 写 record，
#     打 Outlook category "关闭:无效邮件" 留给 M2 PATCH guard 放行后做。
#
# 已知局限：
#   - 跨进程不生效（每次 outlook_drafts 启动都重置缓存）
#   - 多语言 / 短邮件容易漏判 / 误判
#   - 真生产应换成 embedding + vector DB；本轮 PoC 先用字符 n-gram 跑通

import re as _re
from collections import OrderedDict

DEDUP_NGRAM_SIZE      = 3
DEDUP_JACCARD_THRESHOLD = 0.70    # body jaccard 阈值（中文客服"重发"语序略变
                                    # 但 70%+ 文本相同算重复）
DEDUP_SUBJ_SUBSTR_MIN   = 8        # 主题 normalize 后互相包含的最小长度
DEDUP_BODY_CHARS       = 200
DEDUP_CACHE_MAX        = 500

# 主题前缀：去掉这些再算 fingerprint（"Re: App not working" 跟原主题算相似）
_RE_FWD_RE = _re.compile(r"^\s*(?:re|fwd|fw)\s*:\s*", _re.IGNORECASE)


def _dedup_fingerprint(mail: Mail) -> tuple[str, frozenset[str]]:
    """mail → (normalized_subject, body 3-gram 集合)。"""
    subj = _RE_FWD_RE.sub("", mail.subject or "").strip().lower()
    body = (mail.body_text or "")[:DEDUP_BODY_CHARS].lower()
    ngrams: set[str] = set()
    if len(body) >= DEDUP_NGRAM_SIZE:
        for i in range(len(body) - DEDUP_NGRAM_SIZE + 1):
            ngrams.add(body[i:i + DEDUP_NGRAM_SIZE])
    return subj, frozenset(ngrams)


def _dedup_jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


class _DuplicateDetector:
    """进程内 LRU：同 sender + 主题相似 + body jaccard > 阈值 → 视为重复。

    命中时返回 dict {duplicate_of: mail_id, score: float}；
    不命中返回 None 并把当前邮件指纹入缓存。
    """

    def __init__(self, threshold: float = DEDUP_JACCARD_THRESHOLD,
                 max_size: int = DEDUP_CACHE_MAX):
        self.threshold = threshold
        self.max_size  = max_size
        # 按 sender 分桶，避免不同客户被误判重复
        self._buckets: dict[str, "OrderedDict[str, tuple[str, frozenset[str]]]"] = {}

    def check_and_record(self, mail: Mail) -> Optional[dict]:
        subj, ngrams = _dedup_fingerprint(mail)
        sender_key = (mail.sender or "").lower()
        bucket = self._buckets.get(sender_key)
        if bucket is None:
            bucket = OrderedDict()
            self._buckets[sender_key] = bucket
        for existing_id, (existing_subj, existing_ngrams) in bucket.items():
            # 主题 normalize 后相等 → 100% 算重复
            if subj and subj == existing_subj:
                bucket.move_to_end(existing_id)
                return {"duplicate_of": existing_id, "score": 1.0,
                        "match": "subject_exact"}
            # 主题互相包含（≥ 8 字符子串）—— 客户加 Re:/Fwd: 时常见
            if (subj and existing_subj and len(subj) >= DEDUP_SUBJ_SUBSTR_MIN
                    and len(existing_subj) >= DEDUP_SUBJ_SUBSTR_MIN
                    and (subj in existing_subj or existing_subj in subj)):
                score = _dedup_jaccard(ngrams, existing_ngrams)
                if score >= self.threshold * 0.6:   # 主题高度相关时 body 阈值放宽
                    bucket.move_to_end(existing_id)
                    return {"duplicate_of": existing_id, "score": score,
                            "match": "subject_contains"}
            # 主题不相似 → body jaccard > 阈值
            score = _dedup_jaccard(ngrams, existing_ngrams)
            if score >= self.threshold:
                bucket.move_to_end(existing_id)
                return {"duplicate_of": existing_id, "score": score,
                        "match": "jaccard"}
        # 不重复 → 入缓存
        bucket[mail.id] = (subj, ngrams)
        while len(bucket) > self.max_size:
            bucket.popitem(last=False)
        return None


def should_skip(mail: Mail, triage: dict, tmpl: Optional[dict],
                allowlist: Optional[set[str]],
                region: str = "North America") -> Optional[str]:
    """返回 None 表示可以建草稿；返回 str 表示跳过原因。

    region gate 逻辑复用 email_agent.region_gate_should_escalate()，
    改 region 守门只需改 email_agent.py 一处，outlook_drafts.py 自动同步。

    M2.5: 多了 auto_reply_level 一道守门——
      no_auto_reply           → 跳 "auto_reply_level=no_auto_reply"（人工介入）
      auto_draft_review       → 不跳，但 process_mail 会给 draft subject 加 [REVIEW]
      其它（safe / needs_info / and_close） → 落草稿
    """
    if not tmpl:
        return "no template match"
    if triage.get("red_flags"):
        return f"red_flag={triage['red_flags']}"
    if triage.get("urgency") == "P0":
        return "P0 urgency"
    # M2.5: auto_reply_level 守门
    arl = triage.get("auto_reply_level") or tmpl.get("level") or "auto_draft_safe"
    if arl == "no_auto_reply":
        return "auto_reply_level=no_auto_reply"
    # 共享 region gate：用 TriageResult 包装字典，跑同一份判定
    if region:
        try:
            triage_obj = TriageResult(
                intent=triage.get("intent", "other"),
                urgency=triage.get("urgency", "P3"),
                region=triage.get("region") or region,
                language=triage.get("language", "en"),
                requires_recall_check=triage.get("requires_recall_check", False),
                requires_warranty_lookup=triage.get("requires_warranty_lookup", False),
                red_flags=triage.get("red_flags") or [],
            )
        except Exception:
            # 字典缺字段或非法值 —— 保守按"非 NA"处理，转人工
            return f"region={triage.get('region')} (triage shape invalid)"
        if triage_obj.region != region and region_gate_should_escalate(triage_obj):
            return f"region={triage.get('region')} (M1 only NA)"
    if allowlist is not None and triage.get("intent") not in allowlist:
        return f"intent={triage.get('intent')} not in allowlist"
    return None


# ── M1.6: vision 抽取附件截图描述 ─────────────────────────────────────
VISION_MAX_IMAGES_PER_MAIL = 3
VISION_MAX_IMAGE_BYTES      = 4 * 1024 * 1024   # 4 MB

_VISION_EXTRACT_PROMPT = """\
请详细描述这张手机/电脑截图，作为客户邮件 triage 的辅助上下文。

要求：
1. 中文回答。
2. 描述 app 名 / 页面 / 显示的车辆信息 / 错误信息（**逐字引用原文 + 括号里错误码**）/
   可见按钮图标 / 是不是 iSMART app 截图。
3. 不要编造内容；看不清的写"不清晰"。
4. 输出必须严格匹配 JSON schema。
"""


def _extract_image_descriptions(
    mail: Mail, g: GraphClient, llm: AzureLLMClient,
) -> list[str]:
    """遍历 mail.attachments，过滤 image/* + 大小限制 → download → vision 解析。
    返回 list[str]，每个形如 "[IMG_6079.png] 中文描述 ..."。
    失败 / 0 附件 → 返回空 list。
    """
    from .schemas import ImageDescription  # 延迟 import

    out: list[str] = []
    if not mail.attachments:
        return out

    for att in mail.attachments:
        if len(out) >= VISION_MAX_IMAGES_PER_MAIL:
            LOG.info("[vision] 已达 %d 张上限，跳过 %s",
                     VISION_MAX_IMAGES_PER_MAIL, att.get("name"))
            break
        ctype = att.get("contentType", "")
        if not ctype.startswith("image/"):
            continue
        size = att.get("size", 0)
        if size > VISION_MAX_IMAGE_BYTES:
            LOG.info("[vision] %s size=%d > %d 跳过",
                     att.get("name"), size, VISION_MAX_IMAGE_BYTES)
            continue
        att_id = att.get("id")
        if not att_id:
            continue

        try:
            data = g.download_attachment(mail.id, att_id)
        except Exception as e:
            LOG.warning("[vision] download failed %s: %s", att.get("name"), e)
            continue

        try:
            desc, _call = llm.invoke_json_with_images(
                schema=ImageDescription,
                text=_VISION_EXTRACT_PROMPT,
                image_bytes_list=[data],
                image_mime=ctype,
                role="vision_triage",
            )
            tag = att.get("name", "?")
            extra = ""
            if desc.error_text:
                extra = f" [错误: {desc.error_text}]"
            if desc.error_code:
                extra += f" [code: {desc.error_code}]"
            out.append(
                f"[{tag}] app={desc.app_name}{extra}; {desc.description}"
            )
            LOG.info("[vision] %s → ismart=%s err=%s",
                     tag, desc.is_ismart_app, desc.error_code or "—")
        except Exception as e:
            LOG.warning("[vision] LLM failed %s: %s", att.get("name"), e)
            continue
    return out


# ── 主流程 ─────────────────────────────────────────────────────────────

def process_mail(
    mail: Mail,
    g: GraphClient,
    *,
    llm: AzureLLMClient,
    templates: list[dict],
    allowlist: Optional[set[str]],
    dry_run: bool,
    use_llm: bool = True,
    router: Optional[MailboxRouter] = None,
    dedup: Optional[_DuplicateDetector] = None,    # M2.5.4: 进程内去重
) -> dict:
    # ── 0. self-mail 跳过（最便宜，先做：省 LLM + redact）─────────────────
    # 移植自生产 MailUserServiceImpl.skipMail()：发件人是自家 mailbox /
    # 系统发件 → 跳过。必须在 redact 之前做（跳过逻辑需要真 sender）。
    if router is not None and router.should_skip_sender(mail.sender):
        LOG.info("[SKIP] %s — self_mail (sender=%s)",
                 mail.subject[:50], mail.sender)
        return {
            "mail_id":   mail.id,
            "subject":   mail.subject,
            "sender":    mail.sender,
            "skipped":   "self_mail",
            "skip_reason_detail": "sender matches own mailbox or skip domain",
        }

    # ── M2.5.4: 重复邮件检测（去重）──────────────────────────────────────
    # 同一客户重复追问同一件事 → 只保留 1 条起草，剩下的 tag 关闭:无效邮件。
    # 本轮只 skip 起草 + 写 record；打 category 留给 M2 PATCH guard 放行后做。
    # detector 是进程内单例（main() 里 new 一次 + 透传给 process_mail）。
    if dedup is not None:
        dup = dedup.check_and_record(mail)
        if dup is not None:
            LOG.info("[SKIP→duplicate] %s — %s (of %s, score=%.2f)",
                     mail.subject[:50], dup["match"],
                     dup["duplicate_of"], dup["score"])
            return {
                "mail_id":            mail.id,
                "subject":            mail.subject,
                "sender":             mail.sender,
                "skipped":            "duplicate",
                "skip_reason_detail": (
                    f"{dup['match']} (score={dup['score']:.2f}) of "
                    f"mail_id={dup['duplicate_of']}"
                ),
                "duplicate_of":       dup["duplicate_of"],
                "duplicate_score":    dup["score"],
            }

    # ── PII redact：邮件正文 / subject / sender 一律先脱敏 ────────────────
    # 脱敏后才送 LLM、进 log、写 draft subject。
    # fail-closed：redact 不可用或失败 → 转人工，绝不让原文进 LLM。
    try:
        mail, pii_counts = _redact_mail(mail)
    except PIIRedactionError as e:
        LOG.error("[SKIP→human] %s — PII redact failed: %s",
                  mail.subject[:50], e)
        return {
            "mail_id":   mail.id,
            "subject":   mail.subject,   # 未 redact 的 subject 不写草稿，不进 LLM
            "sender":    mail.sender,
            "skipped":   "pii_redact_failed",
            "skip_reason_detail": str(e),
        }

    # ── M1.6: vision 抽取（attachments → image descriptions）─────────────
    # 在 triage 之前调；vision 失败不阻塞，fallback 到纯文本 triage
    image_descs: list[str] = []
    if use_llm and mail.attachments:
        try:
            image_descs = _extract_image_descriptions(mail, g, llm)
        except Exception as e:
            LOG.warning("[vision] extract failed (fallback to text-only): %s", e)

    result = run_template_reply(
        mail.body_text,
        use_llm=use_llm,
        llm=llm,
        templates=templates,
        image_descriptions=image_descs,
        sender=mail.sender,    # M1.7: 个性化 "Dear customer," → "Dear Mr./Ms. {Last},"
    )
    skip = should_skip(mail, result["triage"], result["template_doc"], allowlist)
    record = {
        "mail_id":    mail.id,
        "subject":    mail.subject,
        "sender":     mail.sender,
        "intent":     result["triage"]["intent"],
        "urgency":    result["triage"]["urgency"],
        "region":     result["triage"]["region"],
        "red_flags":  result["triage"]["red_flags"],
        "pii_redacted": pii_counts,
        "image_descriptions_count": len(image_descs),   # M1.6: 报告用
        "skipped":    skip,
        # M2.5.1: xlsx 第 11 列「禁止/注意表述」原文。模板正文是 xlsx 写死的，
        # 不会触发 forbidden 短语（93/93 模板正文 0 命中）；此字段仅供审计 /
        # 给 CS 看这个模板的边界，**不做运行时校验**。
        "template_forbidden": result.get("template_forbidden", ""),
    }
    # M2.5: 把 auto_reply_level / required_info_group 也写到 record（审计 + 报告用）
    record["auto_reply_level"]   = result["triage"].get(
        "auto_reply_level", "auto_draft_safe"
    )
    record["required_info_group"] = result["triage"].get("required_info_group", [])
    # 模板自带的 level（从 xlsx load 进来；如果 triage 没输出，用模板的）
    if result.get("template_doc"):
        td = result["template_doc"]
        if td.get("level"):
            record["template_level"] = td["level"]
        if td.get("required_info"):
            record["template_required_info"] = td["required_info"]
    # ── mailbox 路由（M1.5）：按 triage region 选落哪个 mailbox ──────────
    # 即使 dry_run / skip 也记录：运维排查"为什么这封落到那个 mailbox"用
    if router is not None:
        route = router.route(result["triage"].get("region"))
        record["target_mailbox"] = route.mailbox_upn
        record["mailbox_route_source"] = route.source

    if skip:
        LOG.info("[SKIP] %s — %s", mail.subject[:50], skip)
        return record

    if dry_run:
        record["would_post_body"] = result["filled_body"]
        LOG.info("[DRY]  would create draft: %s (mailbox=%s, level=%s)",
                 mail.subject[:50], record.get("target_mailbox"),
                 record.get("auto_reply_level"))
        return record

    # 用脱敏后的 subject 作 draft subject（防 Drafts 列表里看到真 VIN）
    # M2.5: auto_draft_review → 标题加 [REVIEW] 前缀让人工优先看
    draft_subject = f"RE: {mail.subject}"
    if record["auto_reply_level"] == "auto_draft_review":
        draft_subject = f"[REVIEW] {draft_subject}"
    draft_id = g.create_draft_reply(mail.id, result["filled_body"], subject=draft_subject)
    record["draft_id"] = draft_id
    LOG.info("[DRAFT] %s → Drafts/%s (mailbox=%s, vision=%d, level=%s)",
             mail.subject[:50], draft_id, record.get("target_mailbox"),
             len(image_descs), record["auto_reply_level"])
    return record


def main() -> int:
    p = argparse.ArgumentParser(
        description="Outlook → template_reply → 在 Drafts 落草稿",
    )
    p.add_argument("--once", action="store_true", help="跑一轮（默认行为）")
    p.add_argument("--top", type=int, default=20, help="读最新 N 封未读")
    p.add_argument("--folder", default="Inbox", help="邮箱文件夹名")
    p.add_argument("--dry-run", action="store_true", help="只读不建草稿")
    p.add_argument("--no-llm", action="store_true", help="用关键词分类，跳过 Azure OpenAI（smoke test 用）")
    p.add_argument(
        "--intent-allowlist",
        help="逗号分隔 intent 白名单；默认全部",
    )
    p.add_argument(
        "--mock-mail",
        help="本地 JSON 文件（含 [{subject,sender,body_text}]），跳过 Graph 认证，"
             "用于离线 smoke test 模板匹配 + 跳过规则",
    )
    p.add_argument(
        "--message-ids",
        help="逗号分隔 Graph message ID；只处理这些邮件，跳过 inbox 列表。"
             "适合预先在 Outlook UI 选好测试邮件再跑。",
    )
    p.add_argument(
        "--from-filter",
        help="只处理 sender 等于这个邮箱的邮件（精确匹配，大小写不敏感）",
    )
    p.add_argument(
        "--subject-contains",
        help="只处理 subject 包含这个子串的邮件（大小写不敏感）",
    )
    p.add_argument("--out", help="处理结果写 JSON 文件")
    args = p.parse_args()

    templates = load_xlsx()
    allowlist = (
        set(args.intent_allowlist.split(",")) if args.intent_allowlist else None
    )
    # ── Mailbox 路由（M1.5）：从 env 读 mailbox + skip domain ─────────────
    router = build_router_from_env()
    # ── M2.5.4: 进程内去重 detector（LRU）────────────────────────────────
    dedup = _DuplicateDetector()
    LOG.info("mailbox router: uk=%s au=%s default=%s skip_domains=%s",
             router.uk, router.au, router.default, router._skip_domains)

    # ── 分支 1：mock mail（不需要 Graph 认证）───────────────────
    if args.mock_mail:
        llm = AzureLLMClient() if not args.no_llm else None
        mock_mails = json.loads(Path(args.mock_mail).read_text(encoding="utf-8"))
        mails = [
            Mail(
                id=f"mock-{i}",
                subject=m["subject"],
                sender=m.get("sender", ""),
                received_at=m.get("received_at", ""),
                body_text=m["body_text"],
                is_read=False,
            )
            for i, m in enumerate(mock_mails)
        ]
        mails = _apply_post_filters(mails, args)
        if not mails:
            LOG.warning("过滤后 mock 邮件 0 封 —— 检查 --from-filter / --subject-contains")
        records: list[dict] = []
        for mail in mails:
            try:
                rec = process_mail(
                    mail, g=None, llm=llm, templates=templates,
                    allowlist=allowlist, dry_run=True,
                    use_llm=not args.no_llm,
                    router=router,
                    dedup=dedup,
                )
                records.append(rec)
            except Exception as e:
                LOG.exception("[ERR] mock 处理失败: %s", mail.subject)
                records.append({
                    "mail_id": mail.id, "subject": mail.subject,
                    "sender":  mail.sender, "error": str(e),
                })
        _print_summary(records)
        if args.out:
            _write_out(args.out, records)
        return 0

    # ── 分支 2：真连 Outlook ─────────────────────────────────────
    tenant_id = os.environ.get("AZURE_AD_TENANT_ID")
    client_id = os.environ.get("AZURE_AD_CLIENT_ID")
    user_id   = os.environ.get("MAILBOX_USER", "me")
    if not tenant_id or not client_id:
        print(
            "[ERR] 缺 AZURE_AD_TENANT_ID / AZURE_AD_CLIENT_ID。"
            "见 .env.example；离线先跑 --mock-mail 试模板匹配",
            file=sys.stderr,
        )
        return 2

    token = get_token(tenant_id, client_id)
    g = GraphClient(token, user_id=user_id)
    llm = AzureLLMClient()

    # ── 选邮件：3 种方式（优先级：--message-ids > --from/--subject > 默认 inbox）──
    if args.message_ids:
        ids = [s.strip() for s in args.message_ids.split(",") if s.strip()]
        LOG.info("按 --message-ids 取 %d 封指定邮件 ...", len(ids))
        mails = []
        for mid in ids:
            m = g.get_message_by_id(mid)
            if m is None:
                LOG.warning("[WARN] 找不到邮件 id=%s（已跳过）", mid)
            else:
                mails.append(m)
    else:
        LOG.info("拉 %s/%s 未读邮件 ...", args.folder, args.top)
        mails = g.list_unread(top=args.top, folder=args.folder)
        before = len(mails)
        mails = _apply_post_filters(mails, args)
        if before != len(mails):
            LOG.info("过滤后 %d → %d 封", before, len(mails))
    LOG.info("拿到 %d 封", len(mails))

    records = []
    for m in mails:
        try:
            rec = process_mail(
                m, g, llm=llm, templates=templates,
                allowlist=allowlist, dry_run=args.dry_run,
                router=router,
                dedup=dedup,
            )
            records.append(rec)
        except Exception as e:
            LOG.exception("[ERR] 处理邮件失败: %s", m.subject)
            records.append({
                "mail_id": m.id, "subject": m.subject, "sender": m.sender,
                "error":   str(e),
            })

    _print_summary(records)
    if args.out:
        _write_out(args.out, records)
    return 0


def _print_summary(records: list[dict]) -> None:
    drafted = sum(1 for r in records if r.get("draft_id"))
    skipped = sum(1 for r in records if r.get("skipped"))
    errors  = sum(1 for r in records if r.get("error"))
    dry     = sum(1 for r in records if r.get("would_post_body") and not r.get("draft_id"))
    print("=" * 60)
    print(f"total={len(records)} drafted={drafted} dry={dry} "
          f"skipped={skipped} errors={errors}")
    print("=" * 60)
    for r in records:
        if r.get("draft_id"):
            tag = "DRAFT"
        elif r.get("skipped"):
            tag = "SKIP "
        elif r.get("error"):
            tag = "ERR  "
        else:
            tag = "DRY  "
        print(f"  [{tag}] {r.get('intent','?'):30s}  {r['subject'][:60]}")


def _write_out(out_path: str, records: list[dict]) -> None:
    summary = {
        "total":    len(records),
        "drafted":  sum(1 for r in records if r.get("draft_id")),
        "skipped":  sum(1 for r in records if r.get("skipped")),
        "errors":   sum(1 for r in records if r.get("error")),
        "records":  records,
    }
    Path(out_path).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"[wrote] {out_path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())