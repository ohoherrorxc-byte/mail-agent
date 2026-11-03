"""
outlook_drafts.py — M1.5：从 Outlook 读未读邮件 → template_reply → 在 Drafts 落草稿。

⚠️  硬约束（用户原话）⚠️
  1. **不能主动给客户发送邮件，智能建草稿** —— 永远不调 sendMail；
     只调 createReply（在 Drafts 落新草稿），人工事后审 / 改 / 点 Send。
  2. **不删邮件 / 不删草稿 / 不 move / 不 markAsRead。** 唯一例外：
     createReply 有时会把客户文件拷到回复草稿上，发出去会寄回去；
     只允许 DELETE **本进程刚建的草稿**上的非 inline 附件，原信附件不动。
 违反任一条 = 严重安全事故。本模块以代码 guard 强制：见 GraphClient 的
  _FORBIDDEN_OPS 和 guard 方法。

流程：
  1. MSAL device code flow 登录（一次，token 缓存到 ~/.cache/emailagent/graph_token.json）
  2. 拉未读邮件（默认 20 封，可调）—— 不改 isRead
  3. 每封走 template_reply.run_template_reply() —— Triage + 选 template_id
  4. 用 Graph API 在对应邮件下 createReply draft 并 PATCH body 为模板
  5. Outlook Drafts 里出现草稿，保留线程 / To / Cc；客户文件留在原信，
     不把文件挂在回复上。

跳过条件（送人工，不建草稿）：
  - intent 在 xlsx 里没有匹配的模板
  - red_flags 非空（律师 / 媒体 / 安全事件）
  - region != "North America"（M1 守门）
  - 客服已发出至少一封、客户再跟进且未结案 → follow_up_needs_human
  - 客户已进店/经销商处理过仍未解决 → dealer_unresolved_needs_human
  - 要解除 VPN 限制 → APP-VPN-001（关 VPN 再试），禁止套 NET-001
  - 选行与最新主诉语义不符 → semantic_mismatch（错桶才拦；多问里对不上的额外问不整封转人工）

用法：
  # 真用：读最新 20 封未读、建草稿
  python3 -m agent.outlook_drafts --once --top 20
  # 定时扫（分钟），结果写台账目录；Ctrl-C 或 --stop 结束
  python3 -m agent.outlook_drafts --every 15 --top 20
  python3 -m agent.outlook_drafts --stop

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
import base64
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
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
from .mail_tags import (
    extract_vin,
    push_fact_tags,
    thread_msgs_to_context,
    issue_l2_from_template,
)
from .comfort_reply import (
    customer_case_text,
    is_comfort_reply,
    latest_customer_id,
    quoted_reply_is_comfort,
)
from .template_reply import (
    load_xlsx,
    run_template_reply,
    fill_placeholders_naive,
    extract_placeholders,
    human_trigger_should_escalate,
    _latest_customer_turn,
    _form_issue_description,
    _webform_score_text,
    _is_vpn_restriction,
    draft_semantic_mismatch_reason,
    AMERICAS_GRAY_TEMPLATE_ID,
    parked_false_awake_needs_human,
    europe_service_subscription_needs_human,
    ismart_plan_or_3g_needs_human,
    fraud_force_unbind_needs_human,
)

LOG = logging.getLogger("outlook_drafts")
ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path.home() / ".cache" / "emailagent"
TOKEN_CACHE_FILE = CACHE_DIR / "graph_token.json"
SCAN_PID_FILE = CACHE_DIR / "outlook_drafts.pid"
SCAN_LOCK_FILE = CACHE_DIR / "outlook_drafts.lock"
DEDUP_STORE_FILE = CACHE_DIR / "dedup_fingerprints.json"
LEDGER_DIR = Path.home() / "Documents" / "private" / "iSmart-email-ledger"
REPLY_ATTACH_DIR = ROOT / "回复附件模版"

# Outlook 关闭类：打标用连字符，与知识库 L2「关闭-无效邮件」一致。
# 旧标「关闭:无效邮件」仍排除，避免已打过的信再进扫描。
CLOSE_INVALID_CATEGORY = "关闭-无效邮件"
CLOSE_INVALID_CATEGORY_LEGACY = "关闭:无效邮件"
CLOSE_RESOLVED_CATEGORY = "关闭-已解决"
# 原信不 move。坐席用 Outlook 分类筛选，或自建搜索文件夹。
QUEUE_HUMAN_CATEGORY = "转人工"
# 问题类型芯片（AI「问题:」前缀）：preset8=Purple（淡紫）
ISSUE_CATEGORY_COLOR = "preset8"
# 人工光杆 L2：保持红，AI 不得改色
HUMAN_ISSUE_CATEGORY_COLOR = "preset15"  # DarkRed
# 历史 AUTO: 标签：保持灰
AUTO_CATEGORY_COLOR = "preset12"  # Gray
QUEUE_SECOND_REPLY_CATEGORY = "待二次回复"
_HUMAN_SKIP_EXACT = frozenset({
    "follow_up_needs_human",
    "dealer_unresolved_needs_human",
    "parked_false_awake_needs_human",
    "europe_service_subscription_needs_human",
    "ismart_plan_or_3g_needs_human",
    "fraud_force_unbind_needs_human",
    "pii_redact_failed",
    "semantic_mismatch",
    "no template match",
    "human_trigger",
    "P0 urgency",
})
_SECOND_REPLY_TEMPLATE_IDS = frozenset({
    "GEN-001", "GEN-002", "GEN-004", "FICM-PRIV-002",
})

# 知识库「下一步动作」要贴 PDF 的行 → 回复附件模版/ 文件名。
# 条目从第 2 行起算：第 68 行 = APP-DIGITAL KEY-001（工作表第 69 行）。
# 工作表第 67 行 APP-GUIDE-002 是副账号 8 步，不挂数字钥匙 PDF。
_TEMPLATE_KB_PDFS: dict[str, tuple[str, ...]] = {
    "APP-DIGITAL KEY-001": ("Digital Key Function.pdf",),
    "BIND-003": ("Bind vehicle guidance for iSmart App (1).pdf",),
    "APP-GUIDE-001": ("Bind vehicle guidance for iSmart App (1).pdf",),
}


def skip_needs_human_queue(reason: Optional[str]) -> bool:
    """不建草稿、要坐席处理的 skip → 打「转人工」。重复/已有草稿/我方已回 不算。"""
    if not reason:
        return False
    if reason in _HUMAN_SKIP_EXACT:
        return True
    if reason.startswith("red_flag="):
        return True
    if reason.startswith("region="):
        return True
    if reason.startswith("auto_reply_level=no_auto_reply"):
        return True
    return False


def draft_needs_second_reply_queue(record: dict) -> bool:
    """首封已落草稿、还要等客户补材料或再回一轮 → 打「待二次回复」。"""
    tid = str(record.get("template_id") or "")
    if tid in _SECOND_REPLY_TEMPLATE_IDS:
        return True
    level = str(record.get("auto_reply_level") or record.get("template_level") or "")
    return level == "auto_draft_needs_info" or "补信息" in level


def stamp_queue_category(
    g: Optional["GraphClient"],
    mail_id: str,
    category: str,
    *,
    dry_run: bool,
) -> bool:
    if dry_run or g is None or not mail_id:
        return False
    try:
        g.ensure_queue_master_categories()
        g.add_category(mail_id, category)
        return True
    except Exception as e:
        LOG.warning("[QUEUE-TAG-FAIL] %s id=%s… %s", category, mail_id[:24], e)
        return False


def unstamp_queue_category(
    g: Optional["GraphClient"],
    mail_id: str,
    category: str,
    *,
    dry_run: bool,
) -> bool:
    """草稿已落后，从原信摘掉「转人工」。"""
    if dry_run or g is None or not mail_id:
        return False
    try:
        return bool(g.remove_category(mail_id, category))
    except Exception as e:
        LOG.warning("[QUEUE-UNTAG-FAIL] %s id=%s… %s", category, mail_id[:24], e)
        return False


def kb_reply_pdf_paths(template_id: str, next_step: str = "") -> list[Path]:
    """知识库指定的教程 PDF。只允许 回复附件模版/ 下的文件。"""
    names: list[str] = []
    seen: set[str] = set()
    for part in (template_id or "").split("+"):
        for name in _TEMPLATE_KB_PDFS.get(part.strip()) or ():
            if name not in seen:
                seen.add(name)
                names.append(name)
    if not names and re.search(r"贴.?pdf|pdf作为附件", next_step or "", re.I):
        if "digital key" in (next_step or "").lower() or "数字钥匙" in (next_step or ""):
            names = ["Digital Key Function.pdf"]
    root = REPLY_ATTACH_DIR.resolve()
    out: list[Path] = []
    for name in names:
        raw = Path(name)
        if raw.is_absolute() or ".." in raw.parts:
            continue
        path = (REPLY_ATTACH_DIR / raw.name).resolve()
        if path.parent != root or not path.is_file():
            LOG.warning("[draft-attach] missing kb pdf %s", path)
            continue
        out.append(path)
    return out

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
    # M2.6: 同上但 OData alternate 形式（Graph @odata.nextLink pagination 用此格式）
    "GET /users/{user_id}/mailFolders('Inbox')/messages",
    "GET /users/{user_id}/mailFolders/",                  # 读文件夹
    "GET /users/{user_id}/messages/",                     # 读邮件详情
    # M2.5.5: 拉整段 conversation thread（不限 folder，Inbox + SentItems 都有）。
    # 白名单段顺序匹配："GET /users/{user_id}/messages" 是 prefix，
    # 比上面那条更长 / 更特化（带 `{mail_id}` 不带 `{mail_id}` 都能命中），
    # 放前面保证优先匹配（pattern 顺序无关，any() 都中）。
    "GET /users/{user_id}/messages",                      # 拉线程全部消息
    # 写 —— 仅在原邮件下建草稿、改新草稿 body
    "POST /users/{user_id}/messages/{mail_id}/createReply",  # 建草稿
    "PATCH /users/{user_id}/messages/{draft_id}",             # 改草稿 body（仅 draft，走 _created_draft_ids 注册表）
    # M2.5.4: 打 Outlook category（用于去重后给原邮件标 "关闭-无效邮件"）
    # PATCH 原邮件 body `{"categories": [...]}`。**专项守门**：body 只允许
    # `categories` key；其它 key（body / isRead / subject / importance ...）
    # → 拒绝（即便在白名单内也不放行）。注册表不约束此路径 —— 走专项检测。
    "PATCH /users/{user_id}/messages/{mail_id}",
    # M1.6: 读原邮件附件（vision / PDF / docx 抽文本）。不往回复草稿上挂文件。
    "GET /users/{user_id}/messages/{mail_id}/attachments/{attachment_id}/$value",
    "GET /users/{user_id}/messages/{draft_id}/attachments",
    # 只卸本进程 createReply 草稿上被拷来的客户文件，见 _request DELETE 守门。
    "DELETE /users/{user_id}/messages/{draft_id}/attachments/{attachment_id}",
    # 知识库指定的教程 PDF（回复附件模版/）挂到本进程草稿；客户原信文件仍先剥离。
    "POST /users/{user_id}/messages/{draft_id}/attachments",
    # 主分类必须先写入，Outlook 搜索栏「转人工」芯片才能命中。
    "GET /users/{user_id}/outlook/masterCategories",
    "POST /users/{user_id}/outlook/masterCategories",
    "PATCH /users/{user_id}/outlook/masterCategories/",
    # 搜索文件夹：Outlook 顶栏搜「转人工」当关键字会空；左侧文件夹才稳。
    "POST /users/{user_id}/mailFolders",
    "POST /users/{user_id}/mailFolders/*/childFolders",
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
    # M2.5.4 fix: Graph 解出的发件人原始显示名（"Mr. Watson"）—
    # `sender` 字段经过 PII redact 后只剩 `user_xxx@example.com`，
    # 没显示名；这个字段保留原始显示名供 greeting 个性化使用。
    sender_display_name: str = ""
    # M2.5.5: Graph conversationId（线程 ID）—— 用来拉整段会话历史判断
    # "我们是否已经在线程里回过"，以及作为 LLM drafting 的上下文。
    # 空字符串表示这条邮件没法关联到线程（罕见，fallback 用 body quote 检测）。
    conversation_id: str = ""
    categories: list[str] = field(default_factory=list)


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
        self._queue_cats_ready = False
        self._master_cats: Optional[list[dict]] = None

    # ── HTTP 层守卫：所有出站请求必须命中白名单 ────────────────────────
    def _request(self, method: str, path: str, *, json_body: Optional[dict] = None,
                 params: Optional[dict] = None) -> requests.Response:
        # method 黑名单（最严格守门）
        if method.upper() in ("DELETE",):
            if not self._own_draft_file_attachment_delete(path):
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
        if (
            method.upper() == "POST"
            and "/mailFolders" in path
            and (
                path.rstrip("/").endswith("/mailFolders")
                or path.rstrip("/").endswith("/childFolders")
            )
            and (json_body or {}).get("@odata.type")
            != "#microsoft.graph.mailSearchFolder"
        ):
            raise MailboxSafetyError(
                f"[hard-constraint] POST mailFolders 只允许建搜索文件夹：{method} {path}"
            )
        # URL 子串白名单（把 {user_id} / {mail_id} / {draft_id} 占位符
        # 替换成实际值再匹配；* 是通配符，按段顺序匹配）
        sig = f"{method.upper()} {path}"
        if not any(_pattern_match(sig, p, self.user_id) for p in _ALLOWED_GRAPH_PATTERNS):
            raise MailboxSafetyError(
                f"[hard-constraint] Graph 调用不在白名单：{sig}。"
                f"如需新操作，先 review 是否违反 'no send / no delete' 硬约束。"
            )
        # PATCH 分两路：
        #   A) PATCH /messages/{draft_id} → target_id 必须在我们刚 createReply
        #      的 draft id 注册表里（老规则）
        #   B) PATCH /messages/{mail_id}（target_id 不在注册表）→ 走专项守门：
        #      body 只允许 `categories`；仅「关闭-无效邮件」路径可同时 `isRead`。
        if method.upper() == "PATCH" and "/messages/" in path:
            target_id = path.rsplit("/messages/", 1)[-1].strip("/")
            if target_id in self._created_draft_ids:
                pass
            else:
                body_keys = set((json_body or {}).keys())
                if body_keys not in ({"categories"}, {"categories", "isRead"}):
                    extra = body_keys - {"categories", "isRead"}
                    raise MailboxSafetyError(
                        f"[hard-constraint] PATCH 原邮件仅允许 categories；"
                        f"仅关闭-无效邮件时可同时 isRead。"
                        f"body keys={sorted(body_keys)} extra={sorted(extra)}。"
                    )
                if body_keys == {"isRead"}:
                    raise MailboxSafetyError(
                        f"[hard-constraint] 禁止单独 PATCH isRead：{method} {path}"
                    )
                # categories+isRead：必须含「关闭-无效邮件」，禁止借关单已解决等改阅读状态
                if "isRead" in body_keys:
                    cats = (json_body or {}).get("categories") or []
                    if CLOSE_INVALID_CATEGORY not in cats:
                        raise MailboxSafetyError(
                            f"[hard-constraint] 仅打「{CLOSE_INVALID_CATEGORY}」时可 "
                            f"categories+isRead；其它标签不得改阅读状态。"
                        )
        if method.upper() == "POST" and re.search(
            r"/messages/[^/]+/attachments/?$", path or "",
        ):
            if not self._own_draft_attachment_write(path):
                raise MailboxSafetyError(
                    f"[hard-constraint] 禁止往非本进程草稿挂附件：{method} {path}"
                )
        resp = self.session.request(
            method, f"{GRAPH_BASE}{path}",
            json=json_body, params=params,
        )
        if resp.status_code == 401:
            LOG.warning("Graph 401，刷新 token 后重试一次")
            token = get_token(
                os.environ["AZURE_AD_TENANT_ID"],
                os.environ["AZURE_AD_CLIENT_ID"],
            )
            self.session.headers["Authorization"] = f"Bearer {token}"
            resp = self.session.request(
                method, f"{GRAPH_BASE}{path}",
                json=json_body, params=params,
            )
        return resp

    def _own_draft_attachment_write(self, path: str) -> bool:
        """仅：POST /messages/{本进程草稿 id}/attachments。"""
        from urllib.parse import unquote
        m = re.search(r"/messages/([^/]+)/attachments/?$", path or "")
        if not m:
            return False
        return unquote(m.group(1)) in self._created_draft_ids

    def _own_draft_file_attachment_delete(self, path: str) -> bool:
        """仅：DELETE /messages/{本进程草稿 id}/attachments/{附件 id}。"""
        from urllib.parse import unquote
        if "/attachments/" not in path or "/$value" in path:
            return False
        m = re.search(
            r"/messages/([^/]+)/attachments/([^/]+)/?$",
            path or "",
        )
        if not m:
            return False
        draft_id = unquote(m.group(1))
        return draft_id in self._created_draft_ids

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        r = self._request("GET", path, params=params)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError as e:
            raise RuntimeError(
                f"Graph 返回非完整 JSON（{path} status={r.status_code} "
                f"bytes={len(r.content or b'')}）: {e}"
            ) from e

    def _post(self, path: str, body: dict) -> dict:
        r = self._request("POST", path, json_body=body)
        r.raise_for_status()
        return r.json() if r.text else {}

    def _patch(self, path: str, body: dict) -> None:
        r = self._request("PATCH", path, json_body=body)
        r.raise_for_status()

    def _delete(self, path: str) -> None:
        r = self._request("DELETE", path)
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
        M1.6: 加 $expand=attachments 拿附件元数据（不下载）。

        M2.5.4: 保留原方法签名作为薄封装，调 list_filtered() 传 isRead_eq=False。
        """
        return self.list_filtered(top=top, folder=folder, is_read=False)

    def list_filtered(
        self,
        top: int = 20,
        folder: str = "Inbox",
        *,
        since: Optional[str] = None,        # ISO 8601, e.g. "2026-07-01T00:00:00Z"
        until: Optional[str] = None,        # ISO 8601, e.g. "2026-07-31T00:00:00Z"
        is_read: Optional[bool] = None,     # True/False 限定；None = 不限
        exclude_categories: Optional[list[str]] = None,
                                            # 排除带这些 category 的（如 ["关闭-无效邮件"]）
        order: str = "desc",                # "asc" / "desc"
        include_attachments: bool = True,   # 大批量只打标签时关掉
    ) -> list[Mail]:
        """读邮件（不限读未读），支持日期范围 + category 排除 + 排序方向。

        M2.5.4 引入：补 list_unread() 不能覆盖的场景——历史邮件 backlog 起草
        （--since / --until）+ 排除已关闭邮件（--exclude-category）。
        """
        parts: list[str] = []
        if since:
            parts.append(f"receivedDateTime ge {since}")
        if until:
            parts.append(f"receivedDateTime lt {until}")
        if is_read is True:
            parts.append("isRead eq true")
        elif is_read is False:
            parts.append("isRead eq false")
        # category 排除：not categories/any(c: c eq 'X')
        for cat in (exclude_categories or []):
            cat_esc = cat.replace("'", "''")  # OData string escape
            parts.append(f"not categories/any(c: c eq '{cat_esc}')")
        params: dict[str, str] = {
            "$top":  str(top),
            "$select": "id,subject,sender,receivedDateTime,isRead,categories,conversationId",
            "$orderby": f"receivedDateTime {order}",
        }
        if parts:
            params["$filter"] = " and ".join(parts)
        data = self._get(
            f"/users/{self.user_id}/mailFolders/{folder}/messages",
            params=params,
        )
        out: list[Mail] = []
        for m in data.get("value") or []:
            mid = m.get("id")
            if not mid:
                continue
            full = self.get_message_by_id(mid)
            if full is None:
                LOG.warning("[list] 无法读取邮件 id=%s，跳过", str(mid)[:24])
                continue
            cats = [c for c in (m.get("categories") or []) if str(c).strip()]
            if cats:
                full.categories = cats
            out.append(full)
        return out

    def find_unsent_customer_draft(
        self, customer_email: str, subject: str,
    ) -> Optional[str]:
        """Drafts 里是否已有写给这位客户、同一主题的未发送回复。

        网页表单连点会生成两个 conversationId，线程级 already_has_draft
        看不见另一封。只读 Drafts，不删。命中则返回 draft id。
        """
        addr = _dedup_sender_key(customer_email)
        want = _RE_FWD_RE.sub("", subject or "").strip().lower()
        if not addr or not want:
            return None
        try:
            data = self._get(
                f"/users/{self.user_id}/mailFolders/Drafts/messages",
                params={
                    "$top": "50",
                    "$select": "id,subject,toRecipients,createdDateTime,isDraft",
                    "$orderby": "createdDateTime desc",
                },
            )
        except Exception as e:
            LOG.warning("[draft-dedup] list Drafts failed: %s", e)
            return None
        for m in data.get("value") or []:
            if not m.get("isDraft", True):
                continue
            tos = []
            for rec in m.get("toRecipients") or []:
                ea = (rec.get("emailAddress") or {}).get("address") or ""
                if ea:
                    tos.append(ea.lower())
            if addr not in tos:
                continue
            got = _RE_FWD_RE.sub("", m.get("subject") or "").strip().lower()
            if not _dedup_same_subject(want, got):
                continue
            mid = str(m.get("id") or "")
            if not mid:
                continue
            if self._draft_body_is_comfort(mid):
                continue
            return mid
        return None

    def _draft_body_is_comfort(self, draft_id: str) -> bool:
        """未发送的安抚草稿不算业务草稿，不挡后面的回复稿。"""
        try:
            data = self._get(
                f"/users/{self.user_id}/messages/{draft_id}",
                params={"$select": "body,isDraft"},
            )
        except Exception as e:
            LOG.warning("[draft-dedup] read draft body failed: %s", e)
            return False
        content = ((data.get("body") or {}).get("content")) or ""
        text = _html_to_text(content) if "<" in content else content
        return is_comfort_reply(text)

    def get_message_by_id(self, mail_id: str) -> Optional[Mail]:
        """按 Graph message ID 取单封邮件（不动原邮件，只读）。
        找不到 / 权限不够返回 None。
        M1.6: 加 $expand=attachments 拿附件元数据。
        M2.5.4 fix: 解 sender.emailAddress.name 显示名供 greeting 用。
        M2.5.5: 加 conversationId $select 拿线程 ID。"""
        select = {
            "$select": "id,subject,sender,receivedDateTime,body,isRead,conversationId",
        }
        _read_fail = (
            RuntimeError, requests.exceptions.ContentDecodingError,
        )
        try:
            m = self._get(
                f"/users/{self.user_id}/messages/{mail_id}",
                params={**select, "$expand": "attachments"},
            )
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return None
            raise
        except _read_fail as e:
            LOG.warning("[list] 带附件元数据读失败，改不 expand：%s", e)
            try:
                m = self._get(
                    f"/users/{self.user_id}/messages/{mail_id}",
                    params=select,
                )
            except requests.HTTPError as e2:
                if e2.response.status_code == 404:
                    return None
                LOG.warning("[list] 跳过无法读完整 JSON 的邮件 id=%s… %s", mail_id[:24], e2)
                return None
            except _read_fail as e2:
                LOG.warning("[list] 跳过无法读完整 JSON 的邮件 id=%s… %s", mail_id[:24], e2)
                return None
        body = m.get("body", {}).get("content", "")
        _sndr = m.get("sender") or {}
        _sndr_addr = _sndr.get("emailAddress") or {}
        return Mail(
            id=m["id"],
            subject=m.get("subject") or "",
            sender=_sndr_addr.get("address") or "",
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
            sender_display_name=_sndr_addr.get("name", ""),
            conversation_id=m.get("conversationId", "") or "",
        )

    def create_draft_reply(
        self,
        mail_id: str,
        body_text: str,
        body_html: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> str:
        """在指定邮件下挂草稿，返回草稿 id。
        用 createReply 保留线程 / To / Cc。客户文件若被 Graph 拷到草稿上，
        立刻从**本草稿**卸掉（原信不动）。
        原邮件本身**完全不动**（不删邮件 / 不改 isRead / 不 move）。
        """
        # 1) 先 createReply（空 body）—— 在原邮件下新建草稿，原邮件不动
        draft = self._post(
            f"/users/{self.user_id}/messages/{mail_id}/createReply",
            body={},
        )
        draft_id = draft["id"]
        self._created_draft_ids.add(draft_id)   # 注册：之后 PATCH / 卸附件才合法
        self._strip_copied_file_attachments(draft_id)
        quoted_html = ((draft.get("body") or {}).get("content") or "")
        if not str(quoted_html).strip():
            got = self._get(
                f"/users/{self.user_id}/messages/{draft_id}",
                params={"$select": "body"},
            )
            quoted_html = ((got.get("body") or {}).get("content") or "")
        quoted_html = _preserve_quote_linebreaks(quoted_html)
        if body_html:
            content = body_html
            if quoted_html.strip():
                m = re.search(r"(<body[^>]*>)", quoted_html, flags=re.IGNORECASE)
                if m:
                    content = quoted_html[:m.end()] + body_html + quoted_html[m.end():]
                else:
                    content = body_html + quoted_html
        else:
            content = _merge_reply_keep_quote(body_text, quoted_html)
        content_type = "html"
        patch_body = {"body": {"contentType": content_type, "content": content}}
        if subject is not None:
            patch_body["subject"] = subject
        self._patch(
            f"/users/{self.user_id}/messages/{draft_id}",
            body=patch_body,
        )
        return draft_id

    def rewrite_draft_reply(
        self,
        draft_id: str,
        body_text: str,
        body_html: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> str:
        """改写已有未发送回复草稿正文。不 createReply、不删草稿、不动原信。"""
        got = self._get(
            f"/users/{self.user_id}/messages/{draft_id}",
            params={"$select": "id,isDraft,body"},
        )
        if not got.get("isDraft"):
            raise MailboxSafetyError(
                "[hard-constraint] rewrite_draft_reply 只能改未发送草稿"
            )
        self._created_draft_ids.add(draft_id)
        quoted_html = _html_quote_only(
            ((got.get("body") or {}).get("content") or "")
        )
        quoted_html = _preserve_quote_linebreaks(quoted_html)
        if body_html:
            content = body_html
            if quoted_html.strip():
                m = re.search(r"(<body[^>]*>)", quoted_html, flags=re.IGNORECASE)
                if m:
                    content = quoted_html[:m.end()] + body_html + quoted_html[m.end():]
                else:
                    content = body_html + quoted_html
        else:
            content = _merge_reply_keep_quote(body_text, quoted_html)
        patch_body = {"body": {"contentType": "html", "content": content}}
        if subject is not None:
            patch_body["subject"] = subject
        self._patch(
            f"/users/{self.user_id}/messages/{draft_id}",
            body=patch_body,
        )
        return draft_id

    def _strip_copied_file_attachments(self, draft_id: str) -> int:
        """卸掉 createReply 拷到本草稿上的非 inline 文件。原信不碰。"""
        if draft_id not in self._created_draft_ids:
            raise MailboxSafetyError(
                "[hard-constraint] 只能卸本进程 createReply 草稿上的附件"
            )
        got = self._get(
            f"/users/{self.user_id}/messages/{draft_id}/attachments",
            params={"$select": "id,name,isInline,size,contentType"},
        )
        removed = 0
        for att in got.get("value") or []:
            if att.get("isInline"):
                continue
            att_id = att.get("id")
            if not att_id:
                continue
            name = att.get("name") or "?"
            try:
                self._delete(
                    f"/users/{self.user_id}/messages/{draft_id}/attachments/{att_id}"
                )
                removed += 1
                LOG.info("[draft-attach] stripped copied file %s from draft", name)
            except Exception as e:
                LOG.warning("[draft-attach] strip failed %s: %s", name, e)
        return removed

    def attach_kb_pdfs(self, draft_id: str, paths: list[Path]) -> list[str]:
        """把知识库教程 PDF 挂到本进程草稿。客户文件不在这里。"""
        if draft_id not in self._created_draft_ids:
            raise MailboxSafetyError(
                "[hard-constraint] 只能给本进程草稿挂知识库附件"
            )
        attached: list[str] = []
        root = REPLY_ATTACH_DIR.resolve()
        for path in paths:
            resolved = path.resolve()
            if resolved.parent != root or not resolved.is_file():
                continue
            payload = {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": resolved.name,
                "contentType": "application/pdf",
                "contentBytes": base64.b64encode(resolved.read_bytes()).decode("ascii"),
            }
            self._post(
                f"/users/{self.user_id}/messages/{draft_id}/attachments",
                payload,
            )
            attached.append(resolved.name)
            LOG.info("[draft-attach] kb pdf %s → draft", resolved.name)
        return attached

    def add_category(self, mail_id: str, category: str) -> None:
        """给指定邮件打 Outlook category 标签（不删 / 不动原邮件）。

        用途（M2.5.4）：去重后给重复邮件标 "关闭-无效邮件"，人工 review
        queue 不用再看一眼。

        Graph API：
          PATCH /users/{user_id}/messages/{mail_id}
          body: {"categories": ["关闭-无效邮件", ...已有标签]}
        先 GET 当前 categories，再追加，避免盖掉 VIN / 品牌 / 问题类型。

        守门（专项）：
          - HTTP 层：PATCH 路径在 _ALLOWED_GRAPH_PATTERNS 白名单
          - 字段层：_request() 检查 body 只含 `categories` key；其它（body /
            subject / isRead / importance / flag ...）一律 raise
          - 不动 draft 注册表：draft PATCH 仍走 _created_draft_ids 注册表
            约束；这条路径**不**约束 draft id
          - category 参数不接受空字符串 / 含 `,`（避免 Graph 把它当多标签解析错位）

        Raises:
          ValueError: category 为空 / 含非法字符
          MailboxSafetyError: Graph 调用被 _request() 守卫拦下
        """
        category = (category or "").strip()
        if not category:
            raise ValueError("category 不能为空")
        if "," in category:
            raise ValueError(
                f"category 含逗号 (会触发 Graph 集合解析错位)：{category!r}"
            )
        existing = self.get_categories(mail_id)
        if category not in existing:
            existing = [*existing, category]
        self.set_categories(mail_id, existing)

    def remove_category(self, mail_id: str, category: str) -> bool:
        """从原信分类里去掉一个标签（只 PATCH categories）。没有该标签则 no-op。"""
        category = (category or "").strip()
        if not category or "," in category:
            raise ValueError(f"category 非法：{category!r}")
        existing = self.get_categories(mail_id)
        if category not in existing:
            return False
        self.set_categories(mail_id, [c for c in existing if c != category])
        return True

    def get_categories(self, mail_id: str) -> list[str]:
        m = self._get(
            f"/users/{self.user_id}/messages/{mail_id}",
            params={"$select": "id,categories"},
        )
        return [c for c in (m.get("categories") or []) if str(c).strip()]

    def set_categories(self, mail_id: str, categories: list[str]) -> None:
        """整表替换 categories（Graph 语义）。仅 categories 字段。"""
        cleaned: list[str] = []
        for c in categories:
            c = (c or "").strip()
            if not c:
                continue
            if "," in c:
                raise ValueError(f"category 含逗号：{c!r}")
            if len(c) > 255:
                raise ValueError(f"category 超长：{c[:40]!r}")
            cleaned.append(c)
        self._patch(
            f"/users/{self.user_id}/messages/{mail_id}",
            body={"categories": cleaned},
        )

    def apply_customer_resolved(self, mail_id: str) -> None:
        """客户说已解决：打「关闭-已解决」、去掉「转人工」。不改 isRead、不建草稿、不 send。"""
        self.ensure_queue_master_categories()
        existing = self.get_categories(mail_id)
        cats = [c for c in existing if c != QUEUE_HUMAN_CATEGORY]
        if CLOSE_RESOLVED_CATEGORY not in cats:
            cats.append(CLOSE_RESOLVED_CATEGORY)
        self.set_categories(mail_id, cats)

    def apply_close_invalid(self, mail_id: str) -> None:
        """无效关单：打「关闭-无效邮件」、去掉「转人工」、标已读。不建草稿、不 send。

        全库唯一允许改 isRead 的路径——其它标签 / 草稿流程一律不动阅读状态。
        """
        self.ensure_queue_master_categories()
        existing = self.get_categories(mail_id)
        cats = [c for c in existing if c != QUEUE_HUMAN_CATEGORY]
        if CLOSE_INVALID_CATEGORY not in cats:
            cats.append(CLOSE_INVALID_CATEGORY)
        self._patch(
            f"/users/{self.user_id}/messages/{mail_id}",
            body={"categories": cats, "isRead": True},
        )

    def ensure_queue_master_categories(self) -> None:
        """把「转人工」写进邮箱主分类，否则 Outlook 搜索栏按分类筛是空的。"""
        if self._queue_cats_ready:
            return
        data = self._get(f"/users/{self.user_id}/outlook/masterCategories")
        names = {
            str(c.get("displayName") or "")
            for c in (data.get("value") or [])
        }
        if QUEUE_HUMAN_CATEGORY not in names:
            self._post(
                f"/users/{self.user_id}/outlook/masterCategories",
                {
                    "displayName": QUEUE_HUMAN_CATEGORY,
                    "color": "preset9",
                },
            )
        if CLOSE_RESOLVED_CATEGORY not in names:
            self._post(
                f"/users/{self.user_id}/outlook/masterCategories",
                {
                    "displayName": CLOSE_RESOLVED_CATEGORY,
                    "color": "preset4",
                },
            )
        self._queue_cats_ready = True

    def ensure_issue_master_category(self, display_name: str) -> None:
        """仅 AI「问题:…」芯片用淡紫。人工光杆 / AUTO: 不改色。"""
        name = (display_name or "").strip()
        if not name or name.startswith("AUTO:"):
            return
        if not name.startswith("问题:"):
            # 误传光杆名时，只给「问题:」主分类上色，绝不改光杆红标。
            from .mail_tags import ISSUE_PREFIX, canonicalize_issue_l2
            bare = canonicalize_issue_l2(name) or name
            if not bare:
                return
            name = bare if bare.startswith(ISSUE_PREFIX) else f"{ISSUE_PREFIX}{bare}"
        if self._master_cats is None:
            data = self._get(f"/users/{self.user_id}/outlook/masterCategories")
            self._master_cats = list(data.get("value") or [])
        by_name = {
            str(c.get("displayName") or ""): c for c in self._master_cats
        }
        rec = by_name.get(name)
        if rec is None:
            created = self._post(
                f"/users/{self.user_id}/outlook/masterCategories",
                {"displayName": name, "color": ISSUE_CATEGORY_COLOR},
            )
            if isinstance(created, dict) and created.get("id"):
                self._master_cats.append(created)
            else:
                self._master_cats.append(
                    {"displayName": name, "color": ISSUE_CATEGORY_COLOR}
                )
            return
        if rec.get("color") != ISSUE_CATEGORY_COLOR and rec.get("id"):
            self._patch(
                f"/users/{self.user_id}/outlook/masterCategories/{rec['id']}",
                body={"color": ISSUE_CATEGORY_COLOR},
            )
            rec["color"] = ISSUE_CATEGORY_COLOR

    def ensure_auto_category_gray(self, display_name: str) -> None:
        """历史 AUTO: 标签保持灰色。"""
        name = (display_name or "").strip()
        if not name.startswith("AUTO:"):
            return
        if self._master_cats is None:
            data = self._get(f"/users/{self.user_id}/outlook/masterCategories")
            self._master_cats = list(data.get("value") or [])
        by_name = {
            str(c.get("displayName") or ""): c for c in self._master_cats
        }
        rec = by_name.get(name)
        if rec is None:
            created = self._post(
                f"/users/{self.user_id}/outlook/masterCategories",
                {"displayName": name, "color": AUTO_CATEGORY_COLOR},
            )
            if isinstance(created, dict) and created.get("id"):
                self._master_cats.append(created)
            else:
                self._master_cats.append(
                    {"displayName": name, "color": AUTO_CATEGORY_COLOR}
                )
            return
        if rec.get("color") != AUTO_CATEGORY_COLOR and rec.get("id"):
            self._patch(
                f"/users/{self.user_id}/outlook/masterCategories/{rec['id']}",
                body={"color": AUTO_CATEGORY_COLOR},
            )
            rec["color"] = AUTO_CATEGORY_COLOR

    def ensure_human_issue_category_red(self, display_name: str) -> None:
        """人工光杆 L2 恢复/保持红色；绝不改成紫。"""
        name = (display_name or "").strip()
        if not name or name.startswith((
            "AUTO:", "问题:", "VIN:", "Brand:", "Region:", "品牌:", "关闭", "转人工",
        )):
            return
        if self._master_cats is None:
            data = self._get(f"/users/{self.user_id}/outlook/masterCategories")
            self._master_cats = list(data.get("value") or [])
        by_name = {
            str(c.get("displayName") or ""): c for c in self._master_cats
        }
        rec = by_name.get(name)
        if rec is None or not rec.get("id"):
            return
        if rec.get("color") != HUMAN_ISSUE_CATEGORY_COLOR:
            self._patch(
                f"/users/{self.user_id}/outlook/masterCategories/{rec['id']}",
                body={"color": HUMAN_ISSUE_CATEGORY_COLOR},
            )
            rec["color"] = HUMAN_ISSUE_CATEGORY_COLOR

    def ensure_issue_colors_for_categories(self, categories: list[str] | None) -> None:
        """AI「问题:」→ 淡紫；AUTO: → 灰；人工光杆 → 不改色。"""
        for raw in categories or []:
            name = (raw or "").strip()
            if not name:
                continue
            if name.startswith("AUTO:"):
                try:
                    self.ensure_auto_category_gray(name)
                except Exception:
                    pass
                continue
            if name.startswith("问题:"):
                try:
                    self.ensure_issue_master_category(name)
                except Exception:
                    pass
                continue
            # 光杆 L2 等：留给人工红标，AI 不改色

    def ensure_human_search_folder(self) -> None:
        """建在 Outlook「搜索文件夹」下（与 America/Europe 一组）。根上那份客户端不显示。"""
        finder = self._get(
            f"/users/{self.user_id}/mailFolders/searchfolders",
            params={"$select": "id"},
        )
        finder_id = finder.get("id")
        if not finder_id:
            return
        kids = self._get(
            f"/users/{self.user_id}/mailFolders/{finder_id}/childFolders",
            params={"$select": "id,displayName"},
        )
        for folder in kids.get("value") or []:
            if folder.get("displayName") == QUEUE_HUMAN_CATEGORY:
                return
        inbox = self._get(
            f"/users/{self.user_id}/mailFolders/Inbox",
            params={"$select": "id"},
        )
        inbox_id = inbox.get("id")
        if not inbox_id:
            return
        self._post(
            f"/users/{self.user_id}/mailFolders/{finder_id}/childFolders",
            {
                "@odata.type": "#microsoft.graph.mailSearchFolder",
                "displayName": QUEUE_HUMAN_CATEGORY,
                "includeNestedFolders": False,
                "sourceFolderIds": [inbox_id],
                "filterQuery": (
                    f"categories/any(c:c eq '{QUEUE_HUMAN_CATEGORY}')"
                ),
            },
        )

    def get_conversation_thread(
        self,
        conversation_id: str,
        *,
            max_messages: int = 30,
        max_body_chars: int = 8000,
    ) -> list[dict]:
        """拉整段会话线程（按 receivedDateTime 升序）。

        用途（M2.5.5）：
          - 在 process_mail 主循环里分析线程最后几封：
            * 已有未发送回复草稿 → skip "already_has_draft"
            * 最后一封是不是我们自己发出的 → skip "we_replied_last"
            * 已有客服发出回复、客户再跟进 → skip "follow_up_needs_human"
          - 同时把倒数几封消息正文喂给 LLM 当 thread_context，让
            drafting 接得上客户之前的上下文（"我试了你说的 #3 还是
            不行" → 我们知道 #3 是什么）。

        失败 / 0 messages → 返回 []（caller 走 fallback 路径 body quote
        检测 + 跳过 thread context）。

        Args:
            conversation_id: Graph conversationId（mail.conversation_id）。
            max_messages: 最多拉 N 条（M2.5.5: 默认 30，覆盖绝大部分；
                50+ 一般是营销转发链，不增加辨识度）。
            max_body_chars: 每条 body 截断长度。默认 8000，避免长表单末尾 VIN 被切掉。

        Returns:
            list[dict]，每个形如：
              {
                "id":           "<graph message id>",
                "sender_email": "<sender email>",
                "sender_name":  "<sender display name>",
                "received_at":  "<ISO 8601>",
                "body_text":    "<plain text, 截断到 max_body_chars>",
              }

        Raises:
            requests.HTTPError: Graph 调用失败（caller 应当 catch 走 fallback）。
        """
        if not conversation_id:
            return []
        # OData string escape：cid 里的单引号 → 双单引号
        cid_esc = conversation_id.replace("'", "''")
        # 不能 $filter=conversationId 同时 $orderby=receivedDateTime：
        # Graph 要求 orderby 字段出现在 filter 里，否则 400。时间在本地排。
        params: dict[str, str] = {
            "$filter": f"conversationId eq '{cid_esc}'",
            "$top": str(max_messages),
            "$select": "id,sender,receivedDateTime,body,bodyPreview,isDraft",
        }
        data = self._get(
            f"/users/{self.user_id}/messages",
            params=params,
        )
        out: list[dict] = []
        for m in data.get("value", []):
            snd = m.get("sender") or {}
            addr = snd.get("emailAddress") or {}
            body_html = (m.get("body") or {}).get("content", "")
            body_text = _html_to_text(body_html) if "<" in body_html else body_html
            out.append({
                "id":           m.get("id", ""),
                "sender_email": addr.get("address", ""),
                "sender_name":  addr.get("name", ""),
                "received_at":  m.get("receivedDateTime", ""),
                "body_text":    (body_text or "")[:max_body_chars],
                "is_draft":     bool(m.get("isDraft")),
            })
        out.sort(key=lambda x: x.get("received_at") or "")
        return out

    # ── M1.6: 附件下载（仅 image/* + 受大小限制 + 不写 mailbox 之外）──
    def download_attachment(self, mail_id: str, attachment_id: str) -> bytes:
        """下载单个附件到内存（不落盘）。仅 GET；大小由调用方 / vision 失败时跳过。"""
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


def _plain_text_to_html(text: str) -> str:
    """英文草稿正文 → 可插入 Outlook HTML 的一段。"""
    import html as html_lib
    escaped = html_lib.escape(text or "")
    escaped = escaped.replace("\r\n", "\n").replace("\n", "<br>\n")
    return (
        '<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt;">'
        f"{escaped}</div>"
    )


def _html_quote_only(draft_html: str) -> str:
    """已有草稿里只留 Outlook 引用块，去掉上一版回复正文。"""
    html = draft_html or ""
    m = re.search(r'<div[^>]*\bid=["\']divRplyFwdMsg["\']', html, re.I)
    if not m:
        return html
    head = html[:m.start()]
    body_open = re.search(r"(<body[^>]*>)", head, flags=re.IGNORECASE)
    if body_open:
        return html[:body_open.end()] + html[m.start():]
    return html[m.start():]


def _preserve_quote_linebreaks(quoted: str) -> str:
    """Graph 引用若是纯文本塞进 HTML，换行会被浏览器吃掉，变成一整段。

    已有 <br> 的 HTML 引用不动。只有「标签之间排版换行」的 HTML 也不动。
    引用块内部仍靠 \\n 分段时，把这些换行改成 <br>。
    """
    if not quoted:
        return quoted
    if re.search(r"<br\s*/?>", quoted, flags=re.IGNORECASE):
        return quoted
    collapsed = re.sub(r">\s*\n\s*<", "><", quoted.replace("\r\n", "\n"))
    if "\n" not in collapsed:
        return quoted
    return collapsed.replace("\n", "<br>\n")


def _merge_reply_keep_quote(reply_text: str, draft_html: str) -> str:
    """把模板回复插到 createReply 草稿 HTML 的 <body> 开头，保留下方引用。

    Graph createReply 会带上原信 HTML 引用；若 PATCH 整段覆盖，Outlook 草稿
    里就只剩模板、看不到上下文。
    """
    reply_html = _plain_text_to_html(reply_text)
    quoted = _preserve_quote_linebreaks(draft_html or "")
    if not quoted.strip():
        return f"<html><body>{reply_html}</body></html>"
    m = re.search(r"(<body[^>]*>)", quoted, flags=re.IGNORECASE)
    if m:
        i = m.end()
        return quoted[:i] + reply_html + "<br>\n" + quoted[i:]
    return f"<html><body>{reply_html}<br>\n{quoted}</body></html>"


def _pattern_match(sig: str, pattern: str, user_id: str) -> bool:
    """白名单匹配：把 {user_id}/{mail_id}/{draft_id} 替换成实际值 / 通配符，
    然后按 * 拆分，按段顺序 substring 匹配。"""
    expanded = (pattern
                .replace("{user_id}", user_id)
                .replace("{mail_id}",  "*")
                .replace("{draft_id}", "*")
                .replace("{attachment_id}", "*"))
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
        new_body, body_c = redact_text(mail.body_text or "")
        new_subject, subj_c = redact_text(mail.subject or "")
        new_sender, sender_c = redact_text(mail.sender or "")
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
                 (new_subject or "")[:40], counts["vin"], counts["email"], counts["phone"])
    return (
        Mail(
            id=mail.id, subject=new_subject, sender=new_sender,
            received_at=mail.received_at, body_text=new_body, is_read=mail.is_read,
            # M1.6: attachments 透传（附件元数据本身不含 PII，无须 redact）
            attachments=mail.attachments,
            # M2.5.4 fix: sender_display_name 是 Graph 解出的原始显示名，
            # 走 PII redact 也只是名字不是 email/phone，不脱敏 —— 透传即可
            sender_display_name=mail.sender_display_name,
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
    "Middle East":       "MAILBOX_DEFAULT",
    "South America":     "MAILBOX_DEFAULT",
    "Africa":            "MAILBOX_DEFAULT",
}


@dataclass
class MailboxRoute:
    """route() 结果：落哪个 mailbox + 为什么 + 是否 self mail。"""
    mailbox_upn: str | None    # None 表示没匹配上（fallback 也没设）
    source: str                # "region_match" | "default" | "unknown"
    is_self_mail: bool         # 发件人 == 某个自家 mailbox


KNOWN_CS_MAILBOXES = frozenset({
    "customer.care@ismart-service.com",
})

_REPLY_QUOTE_MARKERS = (
    "wrote:",
    "写道:",
    "写道：",
    "escribió:",
    "a écrit:",
    "ha scritto:",
    "schrieb:",
    "skrev:",
    "skrev en e-post",
    "έγραψε:",
    "έγραψε：",
)


def cs_mailbox_set(router: Optional["MailboxRouter"] = None) -> set[str]:
    """自家客服发件地址：env 配置 + 固定 customer.care（MAILBOX_USER 常是 GUID）。"""
    out = {m.lower() for m in KNOWN_CS_MAILBOXES}
    if router is not None:
        out |= {m.lower() for m in router._self_mailboxes if m and "@" in m}
    return out


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
        user_upn: str | None = None,    # M2.5.5: 当前处理 mailbox (MAILBOX_USER)
    ):
        self.uk = (uk or "").strip() or None
        self.au = (au or "").strip() or None
        self.default = (default or "").strip() or None
        # M2.5.5: 把"我们正在用"的 mailbox 也纳入 self_mailboxes 集合。
        # 原因：MAILBOX_USER 是 Graph 读取 / 写草稿用的 mailbox 地址 —— 即
        # 这个 PoC 跑在哪台 mailbox。它本身也是"自家" mailbox（任何
        # `customer.care@...` 写来的邮件都是 CS 自己 / 自动转发）。
        self.user_upn = (user_upn or "").strip() or None
        # 自家 mailbox 全集（lowercase，做 self-mail 匹配）
        self._self_mailboxes: set[str] = {
            m.lower() for m in (self.uk, self.au, self.default, self.user_upn) if m
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

    def find_already_replied_to(
        self, body_text: str,
    ) -> Optional[str]:
        """检测邮件正文里是否带有自家 mailbox 的"wrote: / 写道:"引用。

        用途（M2.5.5）：客户把 CS 之前的回复 forward 回 inbox / 用 Reply 引用
        上一封时，邮件正文会出现 `<our-mailbox> wrote:` 或
        `<our-mailbox> 写道:` 标准 reply quote —— 这表示 CS 在这封邮件之前
        已经回过同一线程，再起草是重复劳动，应该 skip。

        Why 在 process_mail 主循环里做（而不是 Graph conversation API）：
        Graph 查 conversationId 要为每封多走一次 API（N+1 ~200ms/封），
        且 Graph 的 `conversationId` 在转发 / 跨 mailbox 场景下不一定能
        拼回原始线程。body keyword 检查 0 额外 API 调用，覆盖 90%+ 标准
        email client 引用格式。

        Args:
            body_text: 邮件**原文**（必须在 _redact_mail 之前调用，否则
                       邮箱已被 hash 过无法匹配）。

        Returns:
            命中时返回 own mailbox 地址（audit 用），无命中返回 None。

        注意：quoted reply prefix 的 mailbox 地址是 **CS 上一封回复里**的邮箱
        —— 它**必然**在 self_mailboxes 里。误判风险（客户在正文里碰巧提到
        CS 邮箱而无回复意图）极低：标准 reply client 会把 quote 嵌在「On ...,
        xxx wrote:」或「xxx 写道:」模板里，单纯提到邮箱不会同时带这些
        关键字。
        """
        if not body_text:
            return None
        mailboxes = cs_mailbox_set(self)
        if not mailboxes:
            return None
        body_lower = body_text.lower()
        for mbox in mailboxes:
            m = mbox.strip().lower()
            if not m:
                continue
            for marker in _REPLY_QUOTE_MARKERS:
                patterns = [
                    f"<{m}> {marker}",
                    f"<{m}>\n{marker}",
                    f"{m} {marker}",
                    f"<{m}> {marker.rstrip(':')}",  # Gmail 希腊文 <addr> έγραψε:
                ]
                for p in patterns:
                    if p.lower() in body_lower:
                        return mbox
        return None


def build_router_from_env() -> MailboxRouter:
    """从环境变量构造 MailboxRouter（M1.5 默认入口）。

    M2.5.5: 多读 `MAILBOX_USER` 作为 user_upn —— 即当前 PoC 跑的 mailbox
            (会纳入 self_mailboxes 集合)。
    """
    return MailboxRouter(
        uk=os.environ.get("MAILBOX_UK"),
        au=os.environ.get("MAILBOX_AU"),
        default=os.environ.get("MAILBOX_DEFAULT"),
        skip_domains=os.environ.get("SKIP_SENDER_DOMAINS"),
        user_upn=os.environ.get("MAILBOX_USER"),
    )


# ── 跳过规则 ──────────────────────────────────────────────────────────

SKIP_INTENTS = set()                # 显式禁止的 intent
DEFAULT_INTENT_ALLOWLIST: Optional[set[str]] = None   # None = 不限


# 客户已进店/车间处理过仍未解决 → 不要再套「请去 function inspection」。
# 必须同时有「场所」和「已经去过」；「打算去店里」的首询仍可起草。
_DEALER_VENUE_RE = re.compile(
    r"(?:werkstatt|autohaus|workshop|dealerships?|\bdealers?\b|"
    r"\bgarages?\b|\btaller(?:es)?\b|concessionnaires?|\bofficina\b|"
    r"service\s+cent(?:re|er)s?|authori[sz]ed\s+(?:dealer|repairer)|"
    r"officina\s+autorizzata|servicio\s+t[eé]cnico|"
    r"修理厂|经销商|授权店)",
    re.I,
)
_DEALER_ALREADY_RE = re.compile(
    r"(?:war\s+schon|schon\s+(?:zweimal|mehrfach|einmal|in)|"
    r"bereits|"
    r"zweimal|zwei\s*mal|"
    r"already\s+(?:went|been|took|visited|had|at)|"
    r"was\s+(?:already\s+)?(?:at|in)\s+(?:the\s+)?(?:workshop|dealer|garage|werkstatt)|"
    r"twice\s+(?:already\s+)?(?:at|to|in)|"
    r"been\s+(?:to|at)\s+(?:the\s+)?(?:workshop|dealer|garage|werkstatt)|"
    r"dos\s+veces|deux\s+fois|"
    r"die\s+haben\s+alles|"
    r"they\s+(?:have\s+)?(?:already\s+)?(?:checked|inspected|replaced)|"
    r"neue\s+software|"
    r"software\s+(?:oft\s+)?(?:gespielt|geflasht)|"
    r"non\s+[eèé']?\s*riuscita|no\s+me\s+dan\s+soluci[oó]n|"
    r"did\s+not\s+(?:fix|solve|repair)|couldn'?t\s+fix)",
    re.I,
)


def dealer_unresolved_needs_human(body_text: str) -> bool:
    """最新客户段：已进店/车间处理过 → True（转人工，不建草稿）。"""
    latest = _latest_customer_turn(body_text or "")
    blob = latest.casefold()
    if not blob:
        return False
    return bool(_DEALER_VENUE_RE.search(blob) and _DEALER_ALREADY_RE.search(blob))


_CUSTOMER_RESOLVED_RE = re.compile(
    r"("
    r"\b(it'?s|is|has been|have been|was)\s+((now|already)\s+)?(solved|resolved|fixed)\b"
    r"|already\s+(solved|resolved|fixed)"
    r"|(problem|issue|fault)\s+(is\s+)?((now|already)\s+)?(solved|resolved|fixed)"
    r"|working\s+(again|now|fine|properly)"
    r"|it\s+works\s+(fine|again|now|properly|well)"
    r"|works\s+fine\b"
    r"|no\s+longer\s+(an?\s+)?(issue|problem)"
    r"|all\s+good\s+now"
    r"|sorted\s+(now|it out)"
    r"|issue\s+is\s+sorted"
    r"|已解决|已经好了|问题好了|没问题了"
    r"|problem(?:a)?\s+(ist\s+)?gel[oö]st"
    r"|funktioniert\s+wieder"
    r"|probl[eè]me\s+r[eé]solu"
    r"|[eè]\s+risolto|\brisolto\b"
    r"|ya\s+(est[aá]\s+)?resuelto|ya\s+funciona"
    r"|problema\s+resolvido"
    r"|successfully\s+(re-?established|restored|reconnected)"
    r"|(connection|display|app)\s+(was|is|has been)\s+(successfully\s+)?(re-?established|restored)"
    r"|back\s+to\s+normal"
    r"|si\s+[eè]\s+ristabilit"
    r"|con\s+successo.{0,80}ristabilit"
    r"|wiederhergestellt"
    r"|se\s+ha\s+restablecido|se\s+restableci[oó]"
    r"|connex?ion\s+a\s+[eé]t[eé]\s+r[eé]tabl"
    r"|we\s+made\s+it\b(?!\s+to\b)"
    r"|\bit\s+worked\b"
    r")",
    re.I,
)
_CUSTOMER_NOT_RESOLVED_RE = re.compile(
    r"("
    r"not\s+(yet\s+)?(solved|resolved|fixed)"
    r"|still\s+(not|broken|failing|disconnected)"
    r"|is\s+it\s+(fixed|resolved|solved)"
    r"|has\s+(it|this)\s+been\s+(fixed|resolved)"
    r"|any\s+update"
    r"|还没|未解决|仍未|仍然不行"
    r"|nicht\s+gel[oö]st|immer\s+noch"
    r"|pas\s+(encore\s+)?r[eé]solu"
    r")",
    re.I,
)


_CUSTOMER_ACK_CLOSE_RE = re.compile(
    r"("
    r"thank you for (your )?(reply|replying)"
    r"|thanks for (the |your )?(reply|replying)"
    r"|thank you\s*[-–]\s*will do"
    r"|\bwill do (that|so)\b"
    r"|i('ll| will) do (that|so)"
    r"|gracias por .{0,24}respuesta"
    r"|merci pour .{0,24}r[eé]ponse"
    r"|grazie per .{0,24}risposta"
    r")",
    re.I,
)

# 客服回后客户说了 Thank you / Thanks，同一段没有新问题 → 关闭-已解决。不限字数。
_CUSTOMER_PURE_THANKS_HEAD_RE = re.compile(
    r"(?is)^\s*"
    r"(dear\s+[^,\n]{1,40},\s*)?"
    r"(many\s+|best\s+|a\s+big\s+)?"
    r"(thanks|thank\s+you|merci|gracias|grazie|danke|谢谢|非常感谢)\b"
)
# 谢谢后面还在报故障、或补了报错截图。不算已解决。
_CUSTOMER_ONGOING_FAULT_RE = re.compile(
    r"("
    r"(?<!no )(?<!without )\berror(?!\s+is\s+(?:now\s+|already\s+)?(?:fixed|gone|resolved|solved))\b"
    r"|please find attached"
    r"|attached (?:the |a )?(?:photo|picture|image|screenshot)"
    r"|screenshot|screen\s*shot"
    r"|photo of the (?:app|error|screen|issue|problem)"
    r")",
    re.I,
)


def _is_pure_thanks_close(latest: str) -> bool:
    """最新客户段说了谢谢，且没有新故障、新请求。落款和名字不算新问题。"""
    t = (latest or "").strip()
    if not t:
        return False
    t = re.sub(
        r"(?is)\n?\s*(sent\s+via|sent\s+from|get\s+outlook).*$",
        "",
        t,
    ).strip()
    t = re.sub(
        r"(?is)\n?\s*(best|kind|warm)\s+regards[,.]?\s*.*$",
        "",
        t,
    ).strip()
    t = re.sub(
        r"(?is)\n?\s*(sincerely|yours\s+sincerely|cordialement|saludos|cordiali\s+saluti)\b.*$",
        "",
        t,
    ).strip()
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    while lines and not _CUSTOMER_PURE_THANKS_HEAD_RE.search(lines[0]):
        if len(lines[0]) > 40:
            break
        lines.pop(0)
    t = "\n".join(lines).strip()
    if not t or not _CUSTOMER_PURE_THANKS_HEAD_RE.search(t):
        return False
    if _CUSTOMER_NOT_RESOLVED_RE.search(t) or _CUSTOMER_NEW_ASK_RE.search(t):
        return False
    if _CHASE_SUBSTANCE_RE.search(t) or _CUSTOMER_ONGOING_FAULT_RE.search(t):
        return False
    return True


_CUSTOMER_NEW_ASK_RE = re.compile(
    r"("
    r"\b(can you|could you|would you|can i|could i)\b"
    r"|pls confirm|please confirm"
    r"|is there any (way|chance|option)"
    r"|how (can|do|to)\b"
    r")",
    re.I,
)


def customer_issue_resolved(body_text: str) -> bool:
    """最新客户段：已修好、会照做，或说了谢谢且没有新问题。"""
    latest = _latest_customer_turn(body_text or "")
    if not latest.strip():
        return False
    if _CUSTOMER_NOT_RESOLVED_RE.search(latest):
        return False
    if _CUSTOMER_NEW_ASK_RE.search(latest):
        return False
    if _CUSTOMER_RESOLVED_RE.search(latest):
        return True
    if _is_pure_thanks_close(latest):
        return True
    if not _CUSTOMER_ACK_CLOSE_RE.search(latest):
        return False
    blob = f"{_form_issue_description(latest)}\n{_webform_score_text(latest)}"
    if _CHASE_SUBSTANCE_RE.search(blob):
        return False
    return True


_CHASE_NUDGE_RE = re.compile(
    r"("
    r"never\s+respond"
    r"|no\s+(one\s+)?respond"
    r"|without\s+(any\s+)?response"
    r"|no\s+reply"
    r"|don'?t\s+(ever\s+)?(reply|respond|answer)"
    r"|didn'?t\s+(reply|respond|answer)"
    r"|do\s+not\s+(reply|respond|answer)"
    r"|ignoring\s+my\s+(e-?mails?|messages?)"
    r"|contacted\s+you.{0,48}(already|several|many|sometimes)"
    r"|wrote\s+(to\s+)?you.{0,48}(already|several|times|sometimes)"
    r"|not\s+serious"
    r"|jamais\s+(de\s+)?r[eé]ponse|ne\s+r[eé]pondez\s+jamais"
    r"|nunca\s+responden|sin\s+respuesta"
    r"|non\s+rispondete|nessuna\s+risposta"
    r"|keine\s+antwort|antworten\s+nie"
    r"|一直不回|从不回复|没有回复|一直没人回"
    r")",
    re.I | re.S,
)
_CHASE_SUBSTANCE_RE = re.compile(
    r"("
    r"\bbind|\bunbind|desvincul|qr\s*code|password|mot\s+de\s+passe|"
    r"carplay|android\s+auto|fota|\bota\b|t-?box|charg|battery|"
    r"disconnect|offline|not\s+working|doesn'?t\s+work|"
    r"still\s+need\s+help|app\s+will\s+not|cannot\s+log|"
    r"解绑|绑车|密码|充电|连不上|无法|"
    r"VIN_MASK_|\b[A-HJ-NPR-Z0-9]{17}\b"
    r")",
    re.I,
)


def pure_chase_nudge(body_text: str) -> bool:
    """最新一段只在催「为什么不回」，没有新的车/App 问题。"""
    latest = _latest_customer_turn(body_text or "")
    desc = _form_issue_description(latest)
    blob = f"{desc}\n{_webform_score_text(latest)}"
    if not _CHASE_NUDGE_RE.search(blob):
        return False
    if _CHASE_SUBSTANCE_RE.search(blob):
        return False
    return True


def vpn_restriction_needs_human(body_text: str) -> bool:
    """已有 APP-VPN-001，不再因 VPN 限制跳过起草。"""
    return False


# ── M2.5.4: 重复邮件检测（去重）────────────────────────────────────────
# 同客户 + 最新一轮问题正文几乎一样（重复发送 / 表单连点）
# → skip 起草，并 tag 关闭-无效邮件。主题语言不同（Issues with Vehicle /
# Problèmes…）只要内容相同也只留一封草稿。
# 同一 Graph message id 再扫一遍（same_mail_id）只跳过，不当无效邮件。
# 同客户来回沟通（最新一段内容不同）不算重复，即使主题相同。
# 设计：
#   - 进程内 LRU（OrderedDict，上限 500）
#   - 正文：最新客户段（去掉引用和隐私页脚）char 3-gram Jaccard ≥ 0.70
#   - 不调 embedding API

import html as _html
import re as _re
from collections import OrderedDict

DEDUP_NGRAM_SIZE      = 3
DEDUP_JACCARD_THRESHOLD = 0.70    # body jaccard 阈值（中文客服"重发"语序略变
                                    # 但 70%+ 文本相同算重复）
DEDUP_SUBJ_SUBSTR_MIN   = 8        # 主题 normalize 后互相包含的最小长度
DEDUP_BODY_CHARS       = 400    # 无表单描述行时：切页脚后取末尾（旧 160 会被 cid 图挤掉真描述）
DEDUP_ISSUE_CONTAIN_MIN = 24    # 描述互相包含（连点多写了一句 Grazie）最少字数


_DEDUP_PRIVACY_MARKERS = (
    "https://page.soimt.com",
    "privacy policy",
    "privacy notice",
    "notice de confidentialité",
    "datenschutzrichtlinie",
    "datenschutzhinweis",
    "datenschutz",
    "informativa sulla privacy",
    "politique de confidentialité",
    "política de privacidad",
    "política de privacidade",
    "privacyverklaring",
    "πολιτική απορρήτου",
)


_CID_NOISE_RE = _re.compile(
    r"\[cid:[^\]]+\]|cid:[^\s<>]+|\[image:?[^\]]*\]|"
    r"\[cid_image[^\]]*\]|<img\b[^>]*>",
    _re.I,
)

# 网页表单「问题描述」行（各语言）。只比这一行，不要拿整封末 160 字
# （连点第二封常在末尾多 cid 图 / Grazie，Jaccard 掉到阈值下仍建第二封草稿）。
_FORM_DESC_LABEL_RE = _re.compile(
    r"(?:"
    r"descrizione dei problemi|"
    r"beschreibung der probleme|"
    r"descripci[oó]n de (?:los )?problemas|"
    r"description des probl[eè]mes|"
    r"descri[cç][aã]o dos problemas|"
    r"beschrijving van de problemen|"
    r"beskrivning av problem(?:en)?|"
    r"opis problem(?:u|[óo]w)|"
    r"popis probl[eé]m(?:u|ů)|"
    r"описание проблем|"
    r"תיאור הבעיות|"
    r"detailed description of the issue|"
    r"description of (?:the )?(?:issues|problems)"
    r")\s*:\s*",
    _re.I,
)


def _dedup_strip_cid_noise(text: str) -> str:
    return _CID_NOISE_RE.sub(" ", text or "")


def _dedup_norm_issue(text: str) -> str:
    raw = _html.unescape(text or "")
    raw = (
        raw.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )
    return _re.sub(r"\s+", " ", raw.casefold()).strip()


def _dedup_grams(chunk: str) -> frozenset[str]:
    ngrams: set[str] = set()
    if len(chunk) >= DEDUP_NGRAM_SIZE:
        for i in range(len(chunk) - DEDUP_NGRAM_SIZE + 1):
            ngrams.add(chunk[i:i + DEDUP_NGRAM_SIZE])
    return frozenset(ngrams)


def _dedup_cut_privacy(text: str) -> str:
    if not text.strip():
        return text.strip()
    low = text.casefold()
    cut = len(text)
    for marker in _DEDUP_PRIVACY_MARKERS:
        i = low.find(marker)
        if i >= 40:
            cut = min(cut, i)
    return text[:cut].strip()


def _dedup_form_description(body_text: str) -> str:
    """抽出表单「描述」字段；没有该行则空串。"""
    blob = _dedup_cut_privacy(_dedup_strip_cid_noise(body_text or ""))
    m = _FORM_DESC_LABEL_RE.search(blob)
    if not m:
        return ""
    rest = blob[m.end():]
    # 下一枚项目符号 / 隐私段之前
    stop = _re.search(r"\n\s*[•·]\s+\S", rest)
    if stop:
        rest = rest[: stop.start()]
    return _dedup_cut_privacy(rest)


def _dedup_issue_text(body_text: str) -> str:
    """去重用的问题正文：优先表单描述行，否则最新客户段去页脚、去 cid。"""
    desc = _dedup_form_description(body_text or "")
    if len(_dedup_norm_issue(desc)) >= 8:
        return desc.strip()
    latest = _latest_customer_turn(body_text or "")
    latest = _dedup_strip_cid_noise(latest)
    return _dedup_cut_privacy(latest)


def _dedup_issue_contained(a: str, b: str) -> bool:
    """连点重发：第二封在原文末尾加了一句，短的那份仍整段出现在长的里。"""
    na, nb = _dedup_norm_issue(a), _dedup_norm_issue(b)
    if not na or not nb:
        return False
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) < DEDUP_ISSUE_CONTAIN_MIN:
        return False
    return shorter in longer


DEDUP_CACHE_MAX        = 500

# 主题前缀：去掉这些再算 fingerprint（"Re: App not working" 跟原主题算相似）
_RE_FWD_RE = _re.compile(r"^\s*(?:re|fwd|fw)\s*:\s*", _re.IGNORECASE)


def _dedup_sender_key(sender: str) -> str:
    s = (sender or "").strip().lower()
    m = _re.search(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", s)
    return m.group(0) if m else s


def _dedup_store_path() -> Path:
    override = os.environ.get("EMAILAGENT_DEDUP_STORE")
    if override:
        return Path(override)
    return DEDUP_STORE_FILE


def _dedup_fingerprint(mail: Mail) -> tuple[str, frozenset[str], str]:
    """mail → (normalized_subject, 问题段 3-gram, 规范化问题文本)。

    网页表单比整段「描述」（不截末尾 400 字，否则长描述窗口错开、相似度掉到阈值下）。
    没有描述行才用切页脚后的末尾。cid 图不参与。
    """
    subj = _RE_FWD_RE.sub("", mail.subject or "").strip().lower()
    desc = _dedup_form_description(mail.body_text or "")
    if len(_dedup_norm_issue(desc)) >= 8:
        chunk = _dedup_norm_issue(desc)
    else:
        latest = _dedup_issue_text(mail.body_text or "")
        chunk = _dedup_norm_issue(latest)
        if len(chunk) > DEDUP_BODY_CHARS:
            chunk = chunk[-DEDUP_BODY_CHARS:]
    return subj, _dedup_grams(chunk), chunk


def _dedup_same_subject(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if (
        len(a) >= DEDUP_SUBJ_SUBSTR_MIN
        and len(b) >= DEDUP_SUBJ_SUBSTR_MIN
        and (a in b or b in a)
    ):
        return True
    return False


def _dedup_jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


class _DuplicateDetector:
    """同一发件人，或同 VIN 的另一个邮箱，问题正文几乎一样 → 只建一封草稿。不限间隔。

    命中返回 {duplicate_of, score, match, same_subject}；否则入缓存并返回 None。
    store_path 非空时写入磁盘，下一轮 / 另一进程仍能打 关闭-无效邮件。
    """

    def __init__(self, threshold: float = DEDUP_JACCARD_THRESHOLD,
                 max_size: int = DEDUP_CACHE_MAX,
                 store_path: Path | None = None):
        self.threshold = threshold
        self.max_size  = max_size
        self.store_path = store_path
        self._buckets: dict[str, "OrderedDict[str, tuple]"] = {}
        self._by_vin: dict[str, list[tuple[str, str]]] = {}
        if store_path is not None:
            self._load()

    def _load(self) -> None:
        path = self.store_path
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        senders = payload.get("senders") if isinstance(payload, dict) else None
        if not isinstance(senders, dict):
            return
        for sender, rows in senders.items():
            if not isinstance(rows, list):
                continue
            bucket: OrderedDict[str, tuple[str, frozenset[str], str, str]] = OrderedDict()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                mid = str(row.get("id") or "")
                subj = str(row.get("subj") or "")
                grams = row.get("ngrams") or []
                issue = str(row.get("issue") or "")
                received = str(row.get("received") or "")
                if not mid or not isinstance(grams, list):
                    continue
                vin = str(row.get("vin") or "")
                bucket[mid] = (
                    subj, frozenset(str(g) for g in grams if g), issue, received, vin,
                )
                if len(bucket) >= self.max_size:
                    break
            if bucket:
                sender_key = str(sender).lower()
                self._buckets[sender_key] = bucket
                for mid, stored in bucket.items():
                    self._index_vin(sender_key, mid, stored[4])

    def _save(self) -> None:
        path = self.store_path
        if path is None:
            return
        senders: dict[str, list[dict]] = {}
        for sender, bucket in self._buckets.items():
            senders[sender] = [
                {
                    "id": mid, "subj": subj, "ngrams": list(ngrams),
                    "issue": issue, "received": received, "vin": vin,
                }
                for mid, (subj, ngrams, issue, received, vin) in bucket.items()
            ]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"senders": senders}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)

    def _index_vin(self, sender_key: str, mail_id: str, vin: str) -> None:
        if not vin:
            return
        rows = self._by_vin.setdefault(vin, [])
        pair = (sender_key, mail_id)
        if pair not in rows:
            rows.append(pair)

    def _unindex_vin(self, sender_key: str, mail_id: str, vin: str) -> None:
        if not vin:
            return
        rows = self._by_vin.get(vin) or []
        self._by_vin[vin] = [p for p in rows if p != (sender_key, mail_id)]

    def _content_hit(
        self,
        mail: Mail,
        subj: str,
        ngrams: frozenset[str],
        issue: str,
        existing_id: str,
        stored: tuple,
        *,
        cross_vin: bool,
    ) -> Optional[dict]:
        existing_subj = stored[0]
        existing_issue = _dedup_norm_issue(stored[2])
        score = _dedup_jaccard(ngrams, _dedup_grams(existing_issue))
        contained = _dedup_issue_contained(issue, existing_issue)
        if score < self.threshold and not contained:
            return None
        same_subj = _dedup_same_subject(subj, existing_subj)
        if cross_vin:
            match = "same_vin"
        elif contained and score < self.threshold:
            match = "issue_contains"
        elif same_subj:
            match = "subject_exact" if subj == existing_subj else "subject_contains"
        else:
            match = "same_content"
        return {
            "duplicate_of": existing_id,
            "score": score,
            "match": match,
            "same_subject": same_subj,
        }

    def check_and_record(self, mail: Mail) -> Optional[dict]:
        subj, ngrams, issue = _dedup_fingerprint(mail)
        vin = extract_vin(mail.body_text or "") or ""
        sender_key = _dedup_sender_key(mail.sender)
        if not sender_key:
            return None
        bucket = self._buckets.get(sender_key)
        if bucket is None:
            bucket = OrderedDict()
            self._buckets[sender_key] = bucket
        for existing_id, stored in bucket.items():
            if existing_id == mail.id:
                # 同一封信再扫：禁止建第二封草稿，也不是「无效邮件」。
                return {
                    "duplicate_of": existing_id,
                    "score": 1.0,
                    "match": "same_mail_id",
                    "same_subject": True,
                }
            hit = self._content_hit(
                mail, subj, ngrams, issue, existing_id, stored, cross_vin=False,
            )
            if hit is not None:
                bucket.move_to_end(existing_id)
                return hit
        if vin:
            for other_sender, other_id in list(self._by_vin.get(vin) or []):
                if other_sender == sender_key or other_id == mail.id:
                    continue
                other_bucket = self._buckets.get(other_sender)
                if not other_bucket or other_id not in other_bucket:
                    continue
                hit = self._content_hit(
                    mail, subj, ngrams, issue, other_id, other_bucket[other_id],
                    cross_vin=True,
                )
                if hit is not None:
                    return hit
        bucket[mail.id] = (subj, ngrams, issue, mail.received_at or "", vin)
        self._index_vin(sender_key, mail.id, vin)
        while len(bucket) > self.max_size:
            old_id, old = bucket.popitem(last=False)
            self._unindex_vin(sender_key, old_id, old[4] if len(old) > 4 else "")
        self._save()
        return None


def should_skip(mail: Mail, triage: dict, tmpl: Optional[dict],
                allowlist: Optional[set[str]],
                region: str = "North America") -> Optional[str]:
    """返回 None 表示可以建草稿；返回 str 表示跳过原因。

    region gate 逻辑复用 email_agent.region_gate_should_escalate()，
    改 region 守门只需改 email_agent.py 一处，outlook_drafts.py 自动同步。

    M2.5: 多了 auto_reply_level 一道守门——
      no_auto_reply           → 跳 "auto_reply_level=no_auto_reply"（人工介入）
      auto_draft_review       → 不跳，照常落草稿（标题不加 [REVIEW]）
      其它（safe / needs_info / and_close） → 落草稿
    """
    if not tmpl:
        return "no template match"
    americas_fixed = str((tmpl or {}).get("template_id") or "") == AMERICAS_GRAY_TEMPLATE_ID
    if triage.get("red_flags"):
        return f"red_flag={triage['red_flags']}"
    if triage.get("urgency") == "P0" and not americas_fixed:
        return "P0 urgency"
    # M2.5: auto_reply_level 守门 —— 以命中模板行为准（xlsx 自动化策略）
    arl = (tmpl or {}).get("level") or triage.get("auto_reply_level") or "auto_draft_safe"
    if arl == "no_auto_reply" and not americas_fixed:
        note = (tmpl or {}).get("level_note") or ""
        if note:
            return f"auto_reply_level=no_auto_reply: {note}"
        return "auto_reply_level=no_auto_reply"
    if tmpl and not americas_fixed and human_trigger_should_escalate(
        tmpl.get("human_trigger") or "",
        getattr(mail, "body_text", "") or "",
        triage.get("urgency") or "P3",
    ):
        return "human_trigger"
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
        if region_gate_should_escalate(triage_obj):
            return f"region={triage.get('region')} (region gate)"
    if (
        allowlist is not None
        and not americas_fixed
        and triage.get("intent") not in allowlist
    ):
        return f"intent={triage.get('intent')} not in allowlist"
    return None


# ── M1.6: vision 抽取附件截图描述 ─────────────────────────────────────
# 不按张数 / 体积预跳过；下不下来或模型拒图再记日志往下走。

_VISION_EXTRACT_PROMPT = """\
请详细描述这张客户附件图片，作为邮件 triage 的辅助上下文。
附件可能是：iSMART/车机截图、纸质信件扫描、挂号信复印件、保修函、发票。

要求：
1. 中文回答。
2. 若是截图：描述 app / 页面 / 车辆信息 / 错误原文（含错误码）/ 按钮。
3. 若是信件或扫描件：逐字摘录诉求、日期、VIN、车型、经销商、投诉事项；不要概括掉关键事实。
4. 不要编造；看不清的写"不清晰"。
5. 输出必须严格匹配 JSON schema。非 app 截图时 app_name 填 "none"，is_ismart_app=false。
"""


def _extract_image_descriptions(
    mail: Mail, g: GraphClient, llm: AzureLLMClient,
    raw_sink: list[str] | None = None,
) -> list[str]:
    """遍历 mail.attachments 里所有 image/*：下载 → vision。
    不设张数/体积上限。失败单张跳过，返回已成功的描述。
    """
    from .schemas import ImageDescription  # 延迟 import

    out: list[str] = []
    if not mail.attachments:
        return out

    for att in mail.attachments:
        ctype = att.get("contentType", "")
        if not ctype.startswith("image/"):
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
            line = (
                f"[{tag}] app={desc.app_name}{extra}; {desc.description}"
            )
            if raw_sink is not None:
                raw_sink.append(line)
            out.append(_redact_extracted_text(line))
            LOG.info("[vision] %s → ismart=%s err=%s",
                     tag, desc.is_ismart_app, desc.error_code or "—")
        except Exception as e:
            LOG.warning("[vision] LLM failed %s: %s", att.get("name"), e)
            continue
    return out


# ── M2.5.5: PDF 文本提取 ────────────────────────────────────────────────
# image/* 走 vision；application/pdf 走 pypdf 抽文本。不按体积/页数预跳过。
# 扫描型 PDF 抽出来是空 → 标注无法提取。pypdf 不可用 → 空 list。

def _extract_pdf_descriptions(
    mail: Mail, g: GraphClient,
    raw_sink: list[str] | None = None,
) -> list[str]:
    """遍历 mail.attachments 里的 PDF：下载 → 抽全部可解析页。失败单份跳过。"""
    out: list[str] = []
    if not mail.attachments:
        return out
    # 延迟 import：pypdf 不在 requirements 里时也能跑（fallback 到无视 PDF）
    try:
        import pypdf
    except ImportError:
        LOG.warning("[pdf] pypdf 不可用，PDF 附件将跳过")
        return out

    for att in mail.attachments:
        ctype = (att.get("contentType") or "").lower()
        name = (att.get("name") or "").strip()
        if ctype.split(";")[0].strip() not in {"application/pdf", "application/x-pdf"}:
            if not name.lower().endswith(".pdf"):
                continue
        att_id = att.get("id")
        if not att_id:
            continue
        try:
            data = g.download_attachment(mail.id, att_id)
        except Exception as e:
            LOG.warning("[pdf] download failed %s: %s",
                        att.get("name"), e)
            continue
        try:
            import io
            reader = pypdf.PdfReader(io.BytesIO(data))
            n_pages = len(reader.pages)
            chunks: list[str] = []
            for i in range(n_pages):
                try:
                    text = reader.pages[i].extract_text() or ""
                    text = text.strip()
                    if text:
                        chunks.append(f"--- 第 {i+1} 页 ---\n{text}")
                except Exception as e:
                    LOG.warning("[pdf] page %d extract failed: %s", i+1, e)
                    continue
            if not chunks:
                # 0 文本：扫描型 / 加密 / 全图片 PDF
                tag = att.get("name", "?")
                out.append(
                    f"[{tag}] (PDF 共 {n_pages} 页，无可抽取文本，"
                    f"可能为扫描型 / 加密 / 图片型 PDF)"
                )
                LOG.info("[pdf] %s → %d pages, no extractable text",
                         tag, n_pages)
                continue
            full_text_raw = "\n".join(chunks)
            if raw_sink is not None:
                raw_sink.append(full_text_raw)
            full_text = _redact_extracted_text(full_text_raw)
            tag = att.get("name", "?")
            out.append(
                f"[{tag}] (PDF 共 {n_pages} 页)\n{full_text}"
            )
            LOG.info("[pdf] %s → %d pages, extracted %d chars",
                     tag, n_pages, len(full_text))
        except Exception as e:
            LOG.warning("[pdf] parse failed %s: %s", att.get("name"), e)
            continue
    return out


# ── Word / 挂号信扫描件：docx 抽文本 ──────────────────────────────────
# Graph 常见 contentType：
#   application/vnd.openxmlformats-officedocument.wordprocessingml.document
# 客户把挂号信「副本」附成 .docx 时，旧逻辑只读 image/* 和 PDF → 0 抽取
# （Réclamation / courrier MG France.docx）。不按体积预跳过。
_DOCX_CONTENT_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-word.document.macroenabled.12",
})


def _attachment_filename(att: dict) -> str:
    return (att.get("name") or "").strip()


def _attachment_ctype(att: dict) -> str:
    return (att.get("contentType") or "").split(";")[0].strip().lower()


def _looks_like_docx(att: dict) -> bool:
    name = _attachment_filename(att).lower()
    ctype = _attachment_ctype(att)
    if ctype in _DOCX_CONTENT_TYPES:
        return True
    if name.endswith(".docx"):
        return True
    return False


def _docx_to_text(data: bytes) -> str:
    """不依赖 python-docx：OOXML 是 zip，读 word/document.xml 的 w:t。"""
    import io
    import zipfile
    from xml.etree import ElementTree as ET

    w_t = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
    w_p = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    paras: list[str] = []
    for p in root.iter(w_p):
        line = "".join((t.text or "") for t in p.iter(w_t)).strip()
        if line:
            paras.append(line)
    return "\n".join(paras)


def _redact_extracted_text(text: str) -> str:
    if not text:
        return text
    try:
        from scripts.redact_ledger import redact_text
        redacted, _ = redact_text(text)
        return redacted
    except Exception as e:
        LOG.warning("[attach] redact extracted text failed: %s", e)
        return text


def _extract_document_descriptions(
    mail: Mail, g: GraphClient,
    raw_sink: list[str] | None = None,
) -> list[str]:
    """遍历附件，抽 .docx 正文（挂号信 / 正式函）。非 docx 跳过。"""
    out: list[str] = []
    if not mail.attachments:
        return out
    for att in mail.attachments:
        if att.get("isInline"):
            continue
        name = _attachment_filename(att) or "?"
        ctype = _attachment_ctype(att)
        if ctype == "application/msword" or name.lower().endswith(".doc"):
            if not name.lower().endswith(".docx"):
                out.append(
                    f"[{name}] (旧版 .doc，无法抽取正文；请人工打开附件)"
                )
                continue
        if not _looks_like_docx(att):
            continue
        att_id = att.get("id")
        if not att_id:
            continue
        try:
            data = g.download_attachment(mail.id, att_id)
        except Exception as e:
            LOG.warning("[docx] download failed %s: %s", name, e)
            continue
        try:
            raw = _docx_to_text(data)
        except Exception as e:
            LOG.warning("[docx] parse failed %s: %s", name, e)
            out.append(f"[{name}] (docx 解析失败: {e})")
            continue
        if raw_sink is not None and (raw or "").strip():
            raw_sink.append(raw)
        raw = _redact_extracted_text(raw)
        if not (raw or "").strip():
            out.append(f"[{name}] (Word 无可抽取文本，可能是扫描图嵌在文档里)")
            LOG.info("[docx] %s → empty text", name)
            continue
        out.append(f"[{name}] (Word 信件正文)\n{raw}")
        LOG.info("[docx] %s → extracted %d chars", name, len(raw))
    return out


# ── 系统通知 / 撤回报告：不起草 ────────────────────────────────────────
# Graph 会把 Outlook「邮件撤回」结果写成 inbox 未读：发件人是
# Office365Reports@microsoft.com，主题带「撤回报告」。那不是客户邮件，
# 套 GEN-001 等于给微软系统信要 VIN。
_SYSTEM_SKIP_SENDERS = frozenset({
    "office365reports@microsoft.com",
    "noreply@microsoft.com",
    "mailer-daemon@microsoft.com",
})
_SYSTEM_SKIP_SUBJECT_RE = re.compile(
    r"(邮件撤回|撤回报告|message\s*recall|undeliverable(?:\s+mail)?|"
    r"delivery\s+status\s+notification)",
    re.I,
)


def system_notification_skip_reason(mail: Mail) -> Optional[str]:
    """系统信 / 撤回报告 → 跳过原因；客户邮件返回 None。"""
    sender = (mail.sender or "").strip().lower()
    if "<" in sender and ">" in sender:
        m = re.search(r"<([^>]+)>", sender)
        if m:
            sender = m.group(1).strip().lower()
    if sender in _SYSTEM_SKIP_SENDERS:
        return f"system_sender={sender}"
    subj = mail.subject or ""
    if _SYSTEM_SKIP_SUBJECT_RE.search(subj):
        return "recall_or_dsn_report"
    return None


# 银行公证 / 公司合法化等误投到售后箱：不起草，打 关闭-无效邮件。
_BANK_LEGALIZATION_JUNK_RE = re.compile(
    r"("
    r"company\s+legalization|"
    r"legalization\s+bulletin|"
    r"legalizations?\s+ops|"
    r"myalpha\s+web|"
    r"\bg\.e\.?\s*m\.?i\.?|"
    r"articles of association|"
    r"statute\s+codification|"
    r"general certificate of\s+g"
    r")",
    re.I,
)
_CUSTOMER_VEHICLE_OR_APP_RE = re.compile(
    r"("
    r"\bismart\b|\bi-?smart\b|\bmg\s*(app|4|5|zs|hs|3)\b|"
    r"carplay|android\s+auto|infotainment|"
    r"\bbind|\bunbind|t-?box|fota|\bvin\b|"
    r"iSMART|绑车|解绑|车机"
    r")",
    re.I,
)


# 合作方自动回执 / 推广：不起草，打 关闭-无效邮件。正文里还有车/App 主诉则放过。
_MARKETING_AUTOREPLY_RE = re.compile(
    r"("
    r"partnership\s+(opportunity|team)|"
    r"explore\s+collaboration|"
    r"unsubscribe\b.{0,80}manage\s+preferences|"
    r"we(?:'|’)ll\s+connect\s+soon"
    r")",
    re.I | re.S,
)


def misdirected_junk_skip_reason(mail: Mail) -> Optional[str]:
    """银行合法化 / 合作推广等与车辆售后无关的误投信。有车/App 主诉则放过。"""
    blob = f"{mail.subject or ''}\n{mail.body_text or ''}"
    if _CUSTOMER_VEHICLE_OR_APP_RE.search(blob):
        return None
    if _BANK_LEGALIZATION_JUNK_RE.search(blob):
        return "misdirected_bank_legalization"
    if _MARKETING_AUTOREPLY_RE.search(blob):
        return "marketing_partnership_autoreply"
    return None


def _extract_file_attachments(
    mail: Mail, g: GraphClient, raw_sink: list[str],
) -> tuple[list[str], list[str]]:
    """PDF + Word → LLM 用脱敏文本；raw_sink 收未脱敏正文供分类打标。"""
    pdf_descs: list[str] = []
    doc_descs: list[str] = []
    if not mail.attachments:
        return pdf_descs, doc_descs
    try:
        pdf_descs = _extract_pdf_descriptions(mail, g, raw_sink=raw_sink)
    except Exception as e:
        LOG.warning("[pdf] extract failed (fallback to text-only): %s", e)
    try:
        doc_descs = _extract_document_descriptions(mail, g, raw_sink=raw_sink)
    except Exception as e:
        LOG.warning("[docx] extract failed (fallback to text-only): %s", e)
    return pdf_descs, doc_descs


def _facts_extra(raw_sink: list[str]) -> str | None:
    blob = "\n".join(x for x in raw_sink if (x or "").strip())
    return blob or None


def _conversation_vin_note(
    orig_body: str,
    attach_raw: list[str],
    thread_msgs: list[dict],
) -> str:
    """从当前正文 + 附件 + 全线程（未截成 300 字）抽 VIN，脱敏后写成一行证据。

    追问很短、VIN 在更早长信后半段时，闸门仍能看见 VIN_MASK_。
    """
    from .mail_tags import extract_vin

    parts = [orig_body or "", *attach_raw]
    for m in thread_msgs or []:
        if m.get("is_draft"):
            continue
        parts.append(m.get("body_text") or "")
    vin = extract_vin("\n".join(parts))
    if not vin:
        return ""
    return _redact_extracted_text(
        f"VIN {vin} appears earlier in this conversation."
    )


def _pack_thread_context(
    thread_msgs: list[dict],
    *,
    vin_note: str = "",
    max_msgs: int = 8,
    chars_per_msg: int = 2000,
) -> str:
    lines: list[str] = []
    if vin_note.strip():
        lines.append(vin_note.strip())
    if not thread_msgs:
        return "\n".join(lines)
    for m in thread_msgs[-max_msgs:]:
        if m.get("is_draft"):
            continue
        who = m.get("sender_name") or m.get("sender_email") or "?"
        when = (m.get("received_at") or "")[:19]
        body_clip = (m.get("body_text") or "").replace("\n", " ")[:chars_per_msg]
        lines.append(f"[{when}] {who}: {body_clip}")
    return "\n".join(lines)


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
    replace_existing_draft: bool = False,
    allow_rescan: bool = False,
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

    orig_body = mail.body_text or ""

    sys_skip = system_notification_skip_reason(mail)
    if sys_skip:
        LOG.info("[SKIP] %s — %s", mail.subject[:50], sys_skip)
        tag_applied = False
        tag_err: Optional[str] = None
        if g is not None and not dry_run:
            try:
                g.apply_close_invalid(mail.id)
                tag_applied = True
            except Exception as e:
                tag_err = f"{type(e).__name__}: {e}"
                LOG.warning("[TAG-FAIL] %s → %s", mail.subject[:50], tag_err)
        return {
            "mail_id":            mail.id,
            "subject":            mail.subject,
            "sender":             mail.sender,
            "skipped":            "system_notification",
            "skip_reason_detail": sys_skip,
            "tag_applied":        tag_applied,
            "tag_error":          tag_err,
            "close_tag":          CLOSE_INVALID_CATEGORY,
        }

    junk_skip = misdirected_junk_skip_reason(mail)
    if junk_skip:
        LOG.info("[CLOSE-INVALID junk] %s — %s", mail.subject[:50], junk_skip)
        tag_applied = False
        if g is not None and not dry_run:
            try:
                g.apply_close_invalid(mail.id)
                tag_applied = True
            except Exception as e:
                LOG.warning("[TAG-FAIL] %s → %s", mail.subject[:50], e)
        return {
            "mail_id":            mail.id,
            "subject":            mail.subject,
            "sender":             mail.sender,
            "skipped":            "close_invalid_junk",
            "skip_reason_detail": junk_skip,
            "queue_untag":        QUEUE_HUMAN_CATEGORY,
            "close_tag":          CLOSE_INVALID_CATEGORY,
            "tag_applied":        tag_applied,
        }

    if customer_issue_resolved(orig_body):
        LOG.info("[CLOSE-RESOLVED] %s", mail.subject[:50])
        if g is not None and not dry_run:
            g.apply_customer_resolved(mail.id)
        return {
            "mail_id":            mail.id,
            "subject":            mail.subject,
            "sender":             mail.sender,
            "skipped":            "customer_resolved",
            "skip_reason_detail": "customer says the issue is resolved",
            "queue_untag":        QUEUE_HUMAN_CATEGORY,
            "close_tag":          CLOSE_RESOLVED_CATEGORY,
        }

    if pure_chase_nudge(orig_body):
        LOG.info("[CLOSE-INVALID chase] %s", mail.subject[:50])
        tag_applied = False
        if g is not None and not dry_run:
            try:
                g.apply_close_invalid(mail.id)
                tag_applied = True
            except Exception as e:
                LOG.warning("[TAG-FAIL] %s → %s", mail.subject[:50], e)
        return {
            "mail_id":            mail.id,
            "subject":            mail.subject,
            "sender":             mail.sender,
            "skipped":            "pure_chase_nudge",
            "skip_reason_detail": "chase/no-reply complaint with no new issue",
            "queue_untag":        QUEUE_HUMAN_CATEGORY,
            "close_tag":          CLOSE_INVALID_CATEGORY,
            "tag_applied":        tag_applied,
        }

    # 重复（连点 / 同文再发）先关闭。关单摘掉转人工，不建草稿。
    # 必须在「引用客服回复 → 转人工」之前，否则第二封会进人工队列。
    if dedup is not None:
        dup = dedup.check_and_record(mail)
        if dup is not None:
            is_rescan = dup.get("match") == "same_mail_id"
            if is_rescan and allow_rescan:
                LOG.info("[RESCAN] %s — same mail id, continue (message-ids)",
                         mail.subject[:50])
            else:
                skip_kind = "already_scanned" if is_rescan else "duplicate"
                LOG.info("[SKIP→%s] %s — %s (of %s, score=%.2f)",
                         skip_kind, mail.subject[:50], dup["match"],
                         dup["duplicate_of"], dup["score"])
                fact_tags = {}
                if not dry_run and not is_rescan:
                    raw_bits: list[str] = []
                    _extract_file_attachments(mail, g, raw_bits)
                    fact_tags = push_fact_tags(
                        g, mail.id, orig_body,
                        extra_text=_facts_extra(raw_bits),
                        classify_issue=False,
                    )
                category_applied = False
                category_error: Optional[str] = None
                # 同一 message id 再扫 ≠ 表单连点，不打关闭-无效邮件。
                # 内容重复：关闭-无效邮件 + 已读，摘掉转人工，不建草稿。
                if g is not None and not is_rescan and not dry_run:
                    try:
                        g.apply_close_invalid(mail.id)
                        category_applied = True
                        LOG.info("[TAG] %s → %r + isRead",
                                 mail.subject[:50], CLOSE_INVALID_CATEGORY)
                    except Exception as e:
                        category_error = f"{type(e).__name__}: {e}"
                        LOG.warning("[TAG-FAIL] %s → %s",
                                    mail.subject[:50], category_error)
                return {
                    "mail_id":            mail.id,
                    "subject":            mail.subject,
                    "sender":             mail.sender,
                    "skipped":            skip_kind,
                    "skip_reason_detail": (
                        f"{dup['match']} (score={dup['score']:.2f}) of "
                        f"mail_id={dup['duplicate_of']}"
                    ),
                    "duplicate_of":       dup["duplicate_of"],
                    "duplicate_score":    dup["score"],
                    "category_applied":   category_applied,
                    "category_error":     category_error,
                    "queue_untag":        QUEUE_HUMAN_CATEGORY,
                    "close_tag":          CLOSE_INVALID_CATEGORY if not is_rescan else None,
                    "fact_tags":          fact_tags,
                }

    # ── 0.5. M2.5.5: CS 已回复线程检测（正文里 own_mbox wrote: / 写道:）──
    # 客户把 CS 之前回复 forward 回 inbox 时，邮件正文会出现自家 mailbox 的
    # reply quote → 这封不该再建草稿。必须**在 _redact_mail 之前**做（邮箱
    # 被 redact 后匹配不到）。暂不打 Outlook category（关闭:已处理 先不用）。
    from .region_catalog import americas_form_country
    americas_country = americas_form_country(orig_body)

    hit_mbox = (router or MailboxRouter()).find_already_replied_to(mail.body_text)
    if hit_mbox and quoted_reply_is_comfort(mail.body_text, hit_mbox):
        # 客户只是把安抚信引回来，还没有业务回复。
        hit_mbox = None
    if hit_mbox:
        if customer_issue_resolved(orig_body):
            LOG.info("[CLOSE-RESOLVED after CS] %s — body quotes CS but latest is thanks/done",
                     mail.subject[:50])
            if g is not None and not dry_run:
                g.apply_customer_resolved(mail.id)
            return {
                "mail_id":            mail.id,
                "subject":            mail.subject,
                "sender":             mail.sender,
                "skipped":            "customer_resolved",
                "skip_reason_detail": (
                    f"body quotes CS mailbox {hit_mbox!r}, but customer "
                    "thanks / says done — close resolved"
                ),
                "matched_self_mbox":  hit_mbox,
                "queue_untag":        QUEUE_HUMAN_CATEGORY,
                "close_tag":          CLOSE_RESOLVED_CATEGORY,
            }
        if not americas_country:
            LOG.info("[SKIP→already_handled] %s — body quotes CS mailbox: %s",
                     mail.subject[:50], hit_mbox)
            fact_tags = {}
            if not dry_run:
                raw_bits: list[str] = []
                _extract_file_attachments(mail, g, raw_bits)
                fact_tags = push_fact_tags(
                    g, mail.id, orig_body,
                    extra_text=_facts_extra(raw_bits),
                    llm=llm if use_llm else None,
                )
            queued = stamp_queue_category(
                g, mail.id, QUEUE_HUMAN_CATEGORY, dry_run=dry_run,
            )
            return {
                "mail_id":            mail.id,
                "subject":            mail.subject,
                "sender":             mail.sender,
                "skipped":            "follow_up_needs_human",
                "skip_reason_detail": f"body quotes CS mailbox {hit_mbox!r}; further turns go to human",
                "matched_self_mbox":  hit_mbox,
                "tag_applied":        False,
                "queue_tag":          QUEUE_HUMAN_CATEGORY if queued else None,
                "fact_tags":          fact_tags,
            }

    if not americas_country and parked_false_awake_needs_human(orig_body):
        LOG.info("[SKIP→parked_false_awake] %s", mail.subject[:50])
        fact_tags = {}
        if not dry_run:
            raw_bits: list[str] = []
            _extract_file_attachments(mail, g, raw_bits)
            fact_tags = push_fact_tags(
                g, mail.id, orig_body,
                extra_text=_facts_extra(raw_bits),
                llm=llm if use_llm else None,
            )
        queued = stamp_queue_category(
            g, mail.id, QUEUE_HUMAN_CATEGORY, dry_run=dry_run,
        )
        return {
            "mail_id":            mail.id,
            "subject":            mail.subject,
            "sender":             mail.sender,
            "skipped":            "parked_false_awake_needs_human",
            "skip_reason_detail": (
                "parked vehicle still shown as on/ignition plus 12V drain "
                "or accessibility; do not send STATUS-001 intake list"
            ),
            "tag_applied":        False,
            "queue_tag":          QUEUE_HUMAN_CATEGORY if queued else None,
            "fact_tags":          fact_tags,
        }

    if not americas_country and ismart_plan_or_3g_needs_human(orig_body):
        LOG.info("[SKIP→ismart_plan_or_3g] %s", mail.subject[:50])
        fact_tags = {}
        if not dry_run:
            raw_bits: list[str] = []
            _extract_file_attachments(mail, g, raw_bits)
            fact_tags = push_fact_tags(
                g, mail.id, orig_body,
                extra_text=_facts_extra(raw_bits),
                llm=llm if use_llm else None,
            )
        queued = stamp_queue_category(
            g, mail.id, QUEUE_HUMAN_CATEGORY, dry_run=dry_run,
        )
        return {
            "mail_id":            mail.id,
            "subject":            mail.subject,
            "sender":             mail.sender,
            "skipped":            "ismart_plan_or_3g_needs_human",
            "skip_reason_detail": (
                "3G / iSMART data plan row retired; do not auto-draft "
                "or use NET-001 hotspot/month-start"
            ),
            "tag_applied":        False,
            "queue_tag":          QUEUE_HUMAN_CATEGORY if queued else None,
            "fact_tags":          fact_tags,
        }

    if not americas_country and fraud_force_unbind_needs_human(orig_body):
        LOG.info("[SKIP→fraud_force_unbind] %s", mail.subject[:50])
        fact_tags = {}
        if not dry_run:
            raw_bits: list[str] = []
            _extract_file_attachments(mail, g, raw_bits)
            fact_tags = push_fact_tags(
                g, mail.id, orig_body,
                extra_text=_facts_extra(raw_bits),
                llm=llm if use_llm else None,
            )
        queued = stamp_queue_category(
            g, mail.id, QUEUE_HUMAN_CATEGORY, dry_run=dry_run,
        )
        return {
            "mail_id":            mail.id,
            "subject":            mail.subject,
            "sender":             mail.sender,
            "skipped":            "fraud_force_unbind_needs_human",
            "skip_reason_detail": (
                "fraud/unauthorized account on vehicle; force-unbind "
                "needs backend/human — do not auto-draft GEN-001/BIND-001"
            ),
            "tag_applied":        False,
            "queue_tag":          QUEUE_HUMAN_CATEGORY if queued else None,
            "fact_tags":          fact_tags,
        }

    if not americas_country and europe_service_subscription_needs_human(orig_body):
        LOG.info("[SKIP→europe_service_subscription] %s", mail.subject[:50])
        fact_tags = {}
        if not dry_run:
            raw_bits: list[str] = []
            _extract_file_attachments(mail, g, raw_bits)
            fact_tags = push_fact_tags(
                g, mail.id, orig_body,
                extra_text=_facts_extra(raw_bits),
                llm=llm if use_llm else None,
            )
        queued = stamp_queue_category(
            g, mail.id, QUEUE_HUMAN_CATEGORY, dry_run=dry_run,
        )
        return {
            "mail_id":            mail.id,
            "subject":            mail.subject,
            "sender":             mail.sender,
            "skipped":            "europe_service_subscription_needs_human",
            "skip_reason_detail": (
                "Europe connected/iSMART service subscription; "
                "do not auto-draft"
            ),
            "tag_applied":        False,
            "queue_tag":          QUEUE_HUMAN_CATEGORY if queued else None,
            "fact_tags":          fact_tags,
        }

    if not americas_country and dealer_unresolved_needs_human(orig_body):
        LOG.info("[SKIP→dealer_unresolved] %s", mail.subject[:50])
        fact_tags = {}
        if not dry_run:
            raw_bits: list[str] = []
            _extract_file_attachments(mail, g, raw_bits)
            fact_tags = push_fact_tags(
                g, mail.id, orig_body,
                extra_text=_facts_extra(raw_bits),
                llm=llm if use_llm else None,
            )
        queued = stamp_queue_category(
            g, mail.id, QUEUE_HUMAN_CATEGORY, dry_run=dry_run,
        )
        return {
            "mail_id":            mail.id,
            "subject":            mail.subject,
            "sender":             mail.sender,
            "skipped":            "dealer_unresolved_needs_human",
            "skip_reason_detail": (
                "customer already visited workshop/dealer and issue persists; "
                "do not send inspection template"
            ),
            "tag_applied":        False,
            "queue_tag":          QUEUE_HUMAN_CATEGORY if queued else None,
            "fact_tags":          fact_tags,
        }

    # ── M2.5.5: Graph conversation thread 上下文分析 ─────────────────────
    # 拉整段会话线程：
    #   1) 线程最后一封是不是我们自己回的 → skip "we_replied_last"
    #   2) 客服已发出至少一封、问题未结 → skip "follow_up_needs_human"
    #   3) 把最近 N 封消息正文作为 thread_context 喂给 LLM（仅首轮 inbound）
    #
    # 与 body keyword 检测对比：keyword 只看"这封邮件正文里的 reply
    # quote"，漏判概率高（不同 email client quote 格式各异）。Graph
    # conversationId 是权威 —— 一次 API 调换完整线程分析。
    #
    # 失败模式（rate limit / 401 / network）→ 静默 fallback，不阻塞：
    #   - 跳过 skip 检查，走 body quote 检测已经做了
    #   - thread_context 为空，LLM 退化到只用当前邮件正文
    thread_msgs: list[dict] = []
    thread_skipped_reason: Optional[str] = None
    thread_skip_detail: Optional[str] = None
    thread_our_reply_count: int = 0  # 业务回复次数；安抚信不算
    reuse_draft_id: Optional[str] = None
    comfort_only = False
    reply_to_id = mail.id
    if g is not None and mail.conversation_id:
        try:
            thread_msgs = g.get_conversation_thread(mail.conversation_id)
        except Exception as e:
            LOG.warning("[thread] fetch failed for %s: %s",
                        mail.conversation_id[:20], e)
            thread_msgs = []
        if thread_msgs:
            self_emails = cs_mailbox_set(router)
            # 排除当前这封（thread 里也包含自己；只看"我们之前的"回复）
            earlier = [
                m for m in thread_msgs
                if m["id"] != mail.id and not m.get("is_draft")
            ]
            our_earlier = [
                m for m in earlier
                if (m.get("sender_email") or "").lower() in self_emails
            ]
            real_earlier = [
                m for m in our_earlier
                if not is_comfort_reply(m.get("body_text") or "")
            ]
            thread_our_reply_count = len(real_earlier)
            comfort_only = bool(our_earlier) and not real_earlier
            # 未发送的安抚草稿不算业务草稿。
            existing_drafts = [
                m for m in thread_msgs
                if m.get("is_draft") and m.get("id") != mail.id
                and not is_comfort_reply(m.get("body_text") or "")
            ]
            if existing_drafts:
                if replace_existing_draft:
                    reuse_draft_id = str(existing_drafts[0].get("id") or "")
                    if not reuse_draft_id:
                        thread_skipped_reason = "already_has_draft"
                        thread_skip_detail = (
                            f"thread already has {len(existing_drafts)} unsent draft(s)"
                        )
                    else:
                        LOG.info(
                            "[rebuild] rewrite existing draft %s for %s",
                            reuse_draft_id[:24], mail.subject[:50],
                        )
                else:
                    thread_skipped_reason = "already_has_draft"
                    thread_skip_detail = (
                        f"thread already has {len(existing_drafts)} unsent draft(s)"
                    )
            else:
                sent_thread = [m for m in thread_msgs if not m.get("is_draft")]
                newest = sent_thread[-1] if sent_thread else None
                newest_us = bool(
                    newest
                    and (newest.get("sender_email") or "").lower() in self_emails
                )
                newest_comfort = bool(
                    newest_us and is_comfort_reply((newest or {}).get("body_text") or "")
                )
                if newest_us and not newest_comfort:
                    thread_skipped_reason = "we_replied_last"
                    thread_skip_detail = (
                        f"thread newest sender ({newest['sender_email']}) is us, "
                        f"received at {newest['received_at']}"
                    )
                elif thread_our_reply_count >= 1 and not pure_chase_nudge(orig_body):
                    if customer_issue_resolved(orig_body):
                        thread_skipped_reason = "customer_resolved"
                        thread_skip_detail = (
                            f"CS already sent {thread_our_reply_count} reply(ies); "
                            "customer thanks / says done — close resolved"
                        )
                    elif not americas_country:
                        thread_skipped_reason = "follow_up_needs_human"
                        thread_skip_detail = (
                            f"CS already sent {thread_our_reply_count} reply(ies) in thread; "
                            "unresolved follow-up goes to human"
                        )
            if comfort_only and not thread_skipped_reason:
                reply_to_id = latest_customer_id(
                    thread_msgs, self_emails, mail.id,
                )

    if (
        g is not None
        and not thread_skipped_reason
        and not replace_existing_draft
    ):
        other = g.find_unsent_customer_draft(mail.sender, mail.subject)
        if isinstance(other, str) and other:
            thread_skipped_reason = "already_has_draft"
            thread_skip_detail = (
                f"Drafts already has unsent reply for same customer+subject "
                f"({other[:24]})"
            )

    # 附件文本先抽（Word/PDF 不依赖 LLM）：分类打标需要未脱敏 VIN。
    attach_raw: list[str] = []
    pdf_descs, doc_descs = _extract_file_attachments(mail, g, attach_raw)

    fact_tags: dict = {}
    if thread_skipped_reason:
        if thread_skipped_reason == "customer_resolved":
            if g is not None and not dry_run:
                g.apply_customer_resolved(mail.id)
                fact_tags = push_fact_tags(
                    g, mail.id, orig_body,
                    thread_context=thread_msgs_to_context(thread_msgs) or None,
                    extra_text=_facts_extra(attach_raw),
                    llm=llm if use_llm else None,
                )
            LOG.info(
                "[CLOSE-RESOLVED] %s — %s",
                mail.subject[:50], thread_skip_detail,
            )
            return {
                "mail_id":              mail.id,
                "subject":              mail.subject,
                "sender":               mail.sender,
                "skipped":              "customer_resolved",
                "skip_reason_detail":   thread_skip_detail,
                "thread_messages_count": len(thread_msgs),
                "thread_our_replies":   thread_our_reply_count,
                "queue_untag":          QUEUE_HUMAN_CATEGORY,
                "close_tag":            CLOSE_RESOLVED_CATEGORY,
                "fact_tags":            fact_tags,
            }
        if not dry_run:
            fact_tags = push_fact_tags(
                g, mail.id, orig_body,
                thread_context=thread_msgs_to_context(thread_msgs) or None,
                extra_text=_facts_extra(attach_raw),
                llm=llm if use_llm else None,
            )
        LOG.info(
            "[SKIP→%s] %s — %s",
            thread_skipped_reason, mail.subject[:50], thread_skip_detail,
        )
        queued = False
        if skip_needs_human_queue(thread_skipped_reason):
            queued = stamp_queue_category(
                g, mail.id, QUEUE_HUMAN_CATEGORY, dry_run=dry_run,
            )
        return {
            "mail_id":              mail.id,
            "subject":              mail.subject,
            "sender":               mail.sender,
            "skipped":              thread_skipped_reason,
            "skip_reason_detail":   thread_skip_detail,
            "thread_messages_count": len(thread_msgs),
            "thread_our_replies":   thread_our_reply_count,
            "tag_applied":          False,
            "queue_tag":            QUEUE_HUMAN_CATEGORY if queued else None,
            "fact_tags":            fact_tags,
        }

    # 只发过安抚时，把客户各封来信拼进正文再脱敏。关单仍看当前这一封。
    if comfort_only and thread_msgs:
        joined = customer_case_text(
            thread_msgs, cs_mailbox_set(router), mail,
        )
        if joined:
            mail.body_text = joined

    # ── PII redact：邮件正文 / subject / sender 一律先脱敏 ────────────────
    # 脱敏后才送 LLM、进 log、写 draft subject。
    # fail-closed：redact 不可用或失败 → 转人工，绝不让原文进 LLM。
    try:
        mail, pii_counts = _redact_mail(mail)
    except PIIRedactionError as e:
        LOG.error("[SKIP→human] %s — PII redact failed: %s",
                  mail.subject[:50], e)
        queued = stamp_queue_category(
            g, mail.id, QUEUE_HUMAN_CATEGORY, dry_run=dry_run,
        )
        return {
            "mail_id":   mail.id,
            "subject":   mail.subject,   # 未 redact 的 subject 不写草稿，不进 LLM
            "sender":    mail.sender,
            "skipped":   "pii_redact_failed",
            "skip_reason_detail": str(e),
            "queue_tag": QUEUE_HUMAN_CATEGORY if queued else None,
        }

    # ── 附件抽取：image → vision（可 OCR VIN）────────────────────────
    image_descs: list[str] = []
    if mail.attachments:
        if use_llm:
            try:
                image_descs = _extract_image_descriptions(
                    mail, g, llm, raw_sink=attach_raw,
                )
            except Exception as e:
                LOG.warning("[vision] extract failed (fallback to text-only): %s", e)
    attachment_contexts = image_descs + pdf_descs + doc_descs

    # 线程全文抽 VIN（不依赖 300 字摘要）+ 最近几封加长摘要喂模型和闸门。
    vin_note = _conversation_vin_note(orig_body, attach_raw, thread_msgs)
    context_msgs = thread_msgs
    if comfort_only:
        context_msgs = [
            m for m in thread_msgs
            if not is_comfort_reply(m.get("body_text") or "")
        ]
    thread_context_str = _pack_thread_context(context_msgs, vin_note=vin_note)

    result = run_template_reply(
        mail.body_text,
        use_llm=use_llm,
        llm=llm,
        templates=templates,
        image_descriptions=attachment_contexts,
        sender=mail.sender,    # M1.7: 个性化 "Dear customer," → "Dear Mr./Ms. {Last},"
        sender_display_name=mail.sender_display_name,   # M2.5.4 fix: 优先用 Graph 原始显示名
        thread_context=thread_context_str or None,    # M2.5.5: 线程上下文
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
        "image_descriptions_count": len(image_descs),
        "pdf_descriptions_count":   len(pdf_descs),
        "document_descriptions_count": len(doc_descs),
        # M2.5.5: 线程上下文统计（audit 看 LLM 是否拿到 thread info）
        "thread_messages_count":  len(thread_msgs),
        "thread_our_replies":     thread_our_reply_count,
        "thread_context_chars":   len(thread_context_str),
        "fact_tags":  fact_tags,
        "skipped":    skip,
        # M2.5.1: xlsx 第 11 列「禁止/注意表述」原文。模板正文是 xlsx 写死的，
        # 不会触发 forbidden 短语（93/93 模板正文 0 命中）；此字段仅供审计 /
        # 给 CS 看这个模板的边界，**不做运行时校验**。
        "template_forbidden": result.get("template_forbidden", ""),
    }
    if comfort_only:
        record["draft_anchor_id"] = reply_to_id
    # M2.5: 把 auto_reply_level / required_info_group 也写到 record（审计 + 报告用）
    record["auto_reply_level"]   = (
        (result.get("template_doc") or {}).get("level")
        or result["triage"].get("auto_reply_level", "auto_draft_safe")
    )
    record["required_info_group"] = result["triage"].get("required_info_group", [])
    record["cs_next_step"] = result.get("cs_next_step") or ""
    record["cs_human_trigger"] = result.get("cs_human_trigger") or ""
    record["cs_level_note"] = result.get("cs_level_note") or ""
    if result.get("template_doc"):
        td = result["template_doc"]
        if td.get("template_id"):
            record["template_id"] = td["template_id"]
        if td.get("level"):
            record["template_level"] = td["level"]
        if td.get("level_note"):
            record["level_note"] = td["level_note"]
        if td.get("required_info"):
            record["template_required_info"] = td["required_info"]

    def _stamp_facts(*, with_issue_l2: bool) -> None:
        if dry_run:
            return
        l2 = issue_l2_from_template(result.get("template_doc")) if with_issue_l2 else None
        record["fact_tags"] = push_fact_tags(
            g, mail.id, orig_body,
            thread_context=thread_msgs_to_context(thread_msgs) or None,
            extra_text=_facts_extra(attach_raw),
            issue_l2=l2,
            llm=llm if use_llm else None,
        )

    # ── mailbox 路由（M1.5）：按 triage region 选落哪个 mailbox ──────────
    # 即使 dry_run / skip 也记录：运维排查"为什么这封落到那个 mailbox"用
    if router is not None:
        route = router.route(result["triage"].get("region"))
        record["target_mailbox"] = route.mailbox_upn
        record["mailbox_route_source"] = route.source

    if skip:
        _stamp_facts(with_issue_l2=False)
        if skip_needs_human_queue(skip):
            queued = stamp_queue_category(
                g, mail.id, QUEUE_HUMAN_CATEGORY, dry_run=dry_run,
            )
            record["queue_tag"] = QUEUE_HUMAN_CATEGORY if queued else None
        LOG.info("[SKIP] %s — %s", mail.subject[:50], skip)
        return record

    td = result.get("template_doc") or {}
    semantic_hit = draft_semantic_mismatch_reason(
        mail.body_text,
        str(td.get("template_id") or record.get("template_id") or ""),
        result.get("filled_body") or "",
        llm=llm if use_llm else None,
        use_llm=use_llm,
        scenario=str(td.get("scenario_en") or td.get("scenario") or ""),
    )
    if semantic_hit:
        _stamp_facts(with_issue_l2=False)
        record["skipped"] = "semantic_mismatch"
        record["skip_reason_detail"] = semantic_hit
        queued = stamp_queue_category(
            g, mail.id, QUEUE_HUMAN_CATEGORY, dry_run=dry_run,
        )
        record["queue_tag"] = QUEUE_HUMAN_CATEGORY if queued else None
        LOG.info("[SKIP] %s — semantic_mismatch: %s", mail.subject[:50], semantic_hit)
        return record

    also_human = bool((result.get("template_doc") or {}).get("also_queue_human"))
    if dry_run:
        _stamp_facts(with_issue_l2=True)
        record["would_post_body"] = result["filled_body"]
        if also_human:
            record["queue_tag"] = QUEUE_HUMAN_CATEGORY
        LOG.info("[DRY]  would create draft: %s (mailbox=%s, level=%s)",
                 mail.subject[:50], record.get("target_mailbox"),
                 record.get("auto_reply_level"))
        return record

    # 用脱敏后的 subject 作 draft subject（防 Drafts 列表里看到真 VIN）
    draft_subject = f"RE: {mail.subject}"
    if reuse_draft_id:
        draft_id = g.rewrite_draft_reply(
            reuse_draft_id, result["filled_body"], subject=draft_subject,
        )
        record["draft_rebuilt"] = True
    else:
        draft_id = g.create_draft_reply(
            reply_to_id or mail.id, result["filled_body"], subject=draft_subject,
        )
    record["draft_id"] = draft_id
    _stamp_facts(with_issue_l2=True)
    td = result.get("template_doc") or {}
    pdfs = kb_reply_pdf_paths(td.get("template_id") or "", td.get("next_step") or "")
    if pdfs:
        try:
            record["kb_attachments"] = g.attach_kb_pdfs(draft_id, pdfs)
        except Exception as e:
            LOG.warning("[draft-attach] kb pdf failed: %s", e)
            record["kb_attachments_error"] = f"{type(e).__name__}: {e}"
    LOG.info("[DRAFT] %s → Drafts/%s (mailbox=%s, vision=%d, level=%s)",
             mail.subject[:50], draft_id, record.get("target_mailbox"),
             len(image_descs), record["auto_reply_level"])
    if also_human:
        queued = stamp_queue_category(
            g, mail.id, QUEUE_HUMAN_CATEGORY, dry_run=False,
        )
        record["queue_tag"] = QUEUE_HUMAN_CATEGORY if queued else None
    elif unstamp_queue_category(
        g, mail.id, QUEUE_HUMAN_CATEGORY, dry_run=False,
    ):
        record["queue_untag"] = QUEUE_HUMAN_CATEGORY
    return record


def main() -> int:
    p = argparse.ArgumentParser(
        description="Outlook → template_reply → 在 Drafts 落草稿",
    )
    p.add_argument("--once", action="store_true", help="跑一轮（默认行为）")
    p.add_argument(
        "--every",
        type=float,
        metavar="MINUTES",
        help="每隔 N 分钟再扫一轮（同一进程内去重仍有效）。"
             "结果默认写 ~/Documents/private/iSmart-email-ledger/。"
             "不 send、不删信。Ctrl-C 或 --stop 结束。",
    )
    p.add_argument(
        "--stop",
        action="store_true",
        help="停止本机正在跑的 --every 扫信进程。",
    )
    p.add_argument("--top", type=int, default=20, help="读最新 N 封未读")
    p.add_argument("--folder", default="Inbox", help="邮箱文件夹名")
    p.add_argument("--dry-run", action="store_true", help="只读不建草稿")
    # M2.5.4: 历史 backlog 起草支持（日期范围 / 已读 / 已关闭排除 / 升序）
    p.add_argument(
        "--since",
        help="ISO 8601 下界（含），e.g. 2026-07-01T00:00:00Z",
    )
    p.add_argument(
        "--until",
        help="ISO 8601 上界（不含），e.g. 2026-07-31T00:00:00Z",
    )
    p.add_argument(
        "--include-read",
        action="store_true",
        help="把已读邮件也纳入（默认只看未读）",
    )
    p.add_argument(
        "--exclude-category",
        action="append",
        default=[],
        help="排除带这些 category 的邮件（可重复），默认排除 "
             "'关闭-无效邮件'（及旧标 '关闭:无效邮件'）和 '关闭:已处理'",
    )
    p.add_argument(
        "--order",
        choices=["asc", "desc"], default="desc",
        help="按 receivedDateTime 排序方向；默认 desc（最新在前）",
    )
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
        "--replace-existing-draft",
        action="store_true",
        help="线程已有未发送草稿时改写该草稿，不新建第二封。"
             "必须配合 --message-ids；不删草稿、不 send。",
    )
    p.add_argument(
        "--from-filter",
        help="只处理 sender 等于这个邮箱的邮件（精确匹配，大小写不敏感）",
    )
    p.add_argument(
        "--subject-contains",
        help="只处理 subject 包含这个子串的邮件（大小写不敏感）",
    )
    p.add_argument(
        "--out",
        help="处理结果写 JSON。省略时真连 Outlook 默认写台账目录 "
             "outlook_scan_YYYYMMDD_HHMMSS.json",
    )
    p.add_argument(
        "--no-ledger",
        action="store_true",
        help="不自动写台账目录（仍可用 --out 指定路径）",
    )
    args = p.parse_args()
    if args.stop:
        return _stop_running_scan()
    if args.replace_existing_draft and not args.message_ids:
        p.error("--replace-existing-draft requires --message-ids")
    if args.every is not None:
        if args.every <= 0:
            p.error("--every 必须 > 0（分钟）")
        if args.mock_mail:
            p.error("--every 只用于真连 Outlook，不能和 --mock-mail 一起用")
        if args.message_ids:
            p.error("--every 不能和 --message-ids 一起用")

    templates = load_xlsx()
    allowlist = (
        set(args.intent_allowlist.split(",")) if args.intent_allowlist else None
    )
    # ── Mailbox 路由（M1.5）：从 env 读 mailbox + skip domain ─────────────
    router = build_router_from_env()
    # ── M2.5.4: 去重 detector（磁盘 LRU，跨轮次仍能打 关闭-无效邮件）──
    dedup = _DuplicateDetector(store_path=_dedup_store_path())
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

    lock_fd = _acquire_scan_lock(wait=True)
    if lock_fd is None:
        print(
            "[ERR] 已有扫信进程在跑。再开一个会给同一封信建两封草稿。"
            "先 python3 -m agent.outlook_drafts --stop 并等它结束。",
            file=sys.stderr,
        )
        return 1

    token = get_token(tenant_id, client_id)
    g = GraphClient(token, user_id=user_id)
    llm = AzureLLMClient()

    stop = {"flag": False}

    def _on_stop(signum, _frame):
        LOG.info("收到信号 %s，本轮结束后停止", signum)
        stop["flag"] = True

    if args.every is not None:
        _write_pid()
        signal.signal(signal.SIGTERM, _on_stop)
        signal.signal(signal.SIGINT, _on_stop)

    round_index = 0
    try:
        while True:
            round_index += 1
            if args.every is not None:
                LOG.info("扫信第 %d 轮 top=%d", round_index, args.top)
                token = get_token(tenant_id, client_id)
                g = GraphClient(token, user_id=user_id)
            try:
                records = _process_graph_mails(
                    args, g=g, llm=llm, templates=templates,
                    allowlist=allowlist, router=router, dedup=dedup,
                )
            except Exception:
                LOG.exception("本轮扫信失败，将在下一轮重试（不 send）")
                records = []
            _print_summary(records)
            out_path = _resolve_scan_out_path(args, round_index=round_index)
            if out_path:
                _write_out(out_path, records)
                out_p = Path(out_path)
                if out_p.parent == LEDGER_DIR:
                    latest = LEDGER_DIR / "outlook_scan_latest.json"
                    try:
                        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(out_path, latest)
                    except OSError as e:
                        LOG.warning("无法更新 outlook_scan_latest.json: %s", e)
            if args.every is None or stop["flag"]:
                break
            wait_s = max(args.every, 0.1) * 60
            LOG.info("坐席请只审 Drafts。%s 分钟后下一轮（Ctrl-C / --stop 结束）", args.every)
            slept = 0.0
            while slept < wait_s and not stop["flag"]:
                time.sleep(min(5.0, wait_s - slept))
                slept += 5.0
            if stop["flag"]:
                break
    finally:
        if args.every is not None:
            _clear_pid()
        _release_scan_lock(lock_fd)
    return 0


def _process_graph_mails(
    args,
    *,
    g: GraphClient,
    llm,
    templates: list[dict],
    allowlist,
    router,
    dedup,
) -> list[dict]:
    """拉 inbox / 指定 id，逐封 process_mail。不 send。"""
    try:
        g.ensure_queue_master_categories()
        g.ensure_human_search_folder()
    except Exception as e:
        LOG.warning("[queue-folder] %s", e)
    if args.message_ids:
        ids = [s.strip() for s in args.message_ids.split(",") if s.strip()]
        LOG.info("按 --message-ids 取 %d 封指定邮件 ...", len(ids))
        mails = []
        for mid in ids:
            try:
                m = g.get_message_by_id(mid)
            except Exception as e:
                LOG.warning("[WARN] 读邮件失败，跳过 id=%s… %s", mid[:24], e)
                continue
            if m is None:
                LOG.warning("[WARN] 找不到邮件 id=%s（已跳过）", mid)
            else:
                mails.append(m)
    else:
        exclude_cats = list(args.exclude_category)
        if not exclude_cats:
            exclude_cats = [
                CLOSE_INVALID_CATEGORY,
                CLOSE_INVALID_CATEGORY_LEGACY,
                "关闭:已处理",
                CLOSE_RESOLVED_CATEGORY,
                QUEUE_HUMAN_CATEGORY,
            ]
        is_read = False if not args.include_read else None
        LOG.info(
            "拉 %s/%s 邮件 top=%d since=%s until=%s is_read=%s exclude=%s order=%s ...",
            args.folder, "全部" if is_read is None else ("未读" if is_read is False else "已读"),
            args.top,
            args.since or "(无)", args.until or "(无)",
            "all" if is_read is None else is_read,
            exclude_cats, args.order,
        )
        mails = g.list_filtered(
            top=args.top, folder=args.folder,
            since=args.since, until=args.until,
            is_read=is_read,
            exclude_categories=exclude_cats,
            order=args.order,
        )
        before = len(mails)
        mails = _apply_post_filters(mails, args)
        if before != len(mails):
            LOG.info("过滤后 %d → %d 封", before, len(mails))
    LOG.info("拿到 %d 封", len(mails))
    seen_ids: set[str] = set()
    uniq: list = []
    for m in mails:
        if m.id in seen_ids:
            continue
        seen_ids.add(m.id)
        uniq.append(m)
    if len(uniq) != len(mails):
        LOG.info("去掉重复 message id %d → %d", len(mails), len(uniq))
    mails = uniq

    records = []
    for m in mails:
        try:
            rec = process_mail(
                m, g, llm=llm, templates=templates,
                allowlist=allowlist, dry_run=args.dry_run,
                use_llm=not args.no_llm,
                router=router,
                dedup=dedup,
                replace_existing_draft=args.replace_existing_draft,
                allow_rescan=bool(args.message_ids),
            )
            records.append(rec)
        except Exception as e:
            LOG.exception("[ERR] 处理邮件失败: %s", m.subject)
            records.append({
                "mail_id": m.id, "subject": m.subject, "sender": m.sender,
                "error":   str(e),
            })
    return records


def _ledger_scan_path() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return LEDGER_DIR / f"outlook_scan_{ts}.json"


def _resolve_scan_out_path(args, *, round_index: int = 1) -> Optional[str]:
    if args.no_ledger and not args.out:
        return None
    stamped = _ledger_scan_path()
    if not args.out:
        return str(stamped)
    p = Path(args.out).expanduser()
    if p.suffix.lower() == ".json":
        if args.every is None:
            return str(p)
        return str(p.with_name(f"{p.stem}_{round_index:04d}{p.suffix}"))
    return str(p / stamped.name)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _scan_lock_path() -> Path:
    override = os.environ.get("EMAILAGENT_SCAN_LOCK")
    if override:
        return Path(override)
    return SCAN_LOCK_FILE


def _acquire_scan_lock(*, wait: bool = True) -> int | None:
    """进程互斥：两个扫信同时跑会给同一封未读建两封草稿。"""
    path = _scan_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    flags = fcntl.LOCK_EX
    if not wait:
        flags |= fcntl.LOCK_NB
    try:
        if wait:
            LOG.info("获取扫信锁（已有进程会先等它本轮结束）")
        fcntl.flock(fd, flags)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _release_scan_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _stop_running_scan() -> int:
    if not SCAN_PID_FILE.exists():
        print("没有在跑的扫信进程")
        return 0
    raw = SCAN_PID_FILE.read_text(encoding="utf-8").strip()
    try:
        pid = int(raw)
    except ValueError:
        SCAN_PID_FILE.unlink(missing_ok=True)
        print("pid 文件损坏，已删除")
        return 1
    if pid == os.getpid():
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"已请求停止扫信 pid={pid}（不 send、不删信）")
    except ProcessLookupError:
        print(f"进程 {pid} 已不在")
        SCAN_PID_FILE.unlink(missing_ok=True)
        return 0
    except PermissionError as e:
        print(f"无法停止 pid={pid}: {e}", file=sys.stderr)
        return 1
    # SIGTERM 只让旧进程「本轮结束后停」。立刻再开一轮会双开建两封草稿。
    deadline = time.time() + 45 * 60
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(1.0)
    if _pid_alive(pid):
        print(
            f"进程 {pid} 仍在跑（本轮未结束）。不要再开第二个扫信进程。",
            file=sys.stderr,
        )
        return 1
    SCAN_PID_FILE.unlink(missing_ok=True)
    print(f"扫信 pid={pid} 已退出")
    return 0


def _write_pid() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SCAN_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    try:
        SCAN_PID_FILE.chmod(0o600)
    except OSError:
        pass


def _clear_pid() -> None:
    try:
        if SCAN_PID_FILE.exists() and SCAN_PID_FILE.read_text().strip() == str(os.getpid()):
            SCAN_PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


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
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"[wrote] {out_path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())