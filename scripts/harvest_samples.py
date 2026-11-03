#!/usr/bin/env python3
"""
harvest_samples.py — M2.6: 从 Outlook mailbox 拉历史邮件 → 脱敏 → 落候选池。

数据源（不再是 ~/Documents/private/iSmart-email-ledger/ 那份历史 CSV）：
  Microsoft Graph API —— mailbox 就是真业务数据的事实源。
  拉 Inbox 一个 mailbox 一次；多 mailbox 需要按 MAILBOX_USER 重跑。

安全契约（CLAUDE.md §"PII 脱敏"）：
  1. 调用 agent.outlook_drafts._redact_mail() 走 scripts/redact_ledger.redact_text()
     —— VIN [A-HJ-NPR-Z0-9]{17} / email / phone (要求 + 前缀) 三种 PII 必 redact。
  2. redact 失败 (ImportError 等) 走 fail-closed：原邮件不写候选池，
     LOG.warning 记录 mail_id 供人工 audit。
  3. 输出文件 chmod 600 —— 即便已 redact 也加一道（防意外残留）。
  4. 输出 JSONL 必须 gitignored —— 已在 .gitignore 加 eval/candidates*.jsonl。

硬约束继承（outlook_drafts._request 白名单）：
  - 仅 GET /users/{user_id}/mailFolders/.../messages
  - 不动 isRead / 不删 / 不 move / 不 sendMail
  - 通过 GraphClient._request() 走，所以这些约束自动生效

下游用法：
  scripts/sample_ledger.py    --candidates eval/candidates.jsonl --out eval/_samples_to_label.json
  scripts/label_review.py     --pool eval/_samples_to_label.json     (人工标注 CLI)
  → 标完 append 到 eval/samples_real_seed.json
  → eval/run_eval.py --triage-only 评 ≥200 条
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

# 让 scripts/harvest_samples.py 能 import agent.outlook_drafts
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import _env as _env_loader  # noqa: E402
_env_loader.load_env()

from agent.outlook_drafts import (  # noqa: E402
    GRAPH_BASE,
    GraphClient,
    Mail,
    _html_to_text,
    _redact_mail,
    get_token,
)

LOG = logging.getLogger("harvest_samples")


# ── 翻页 ─────────────────────────────────────────────────────────────

def _message_to_mail(m: dict) -> Mail:
    """把 Graph API 单封 message dict → Mail dataclass。

    复制 outlook_drafts.list_filtered() 的解析逻辑（避免修改 outlook_drafts.py
    引入 _iter_messages 改动）。这样 list_filtered 仍是单次调用版本（兼容
    现有 eval / process_mail），harvest_samples 走自己的翻页 loop。

    字段 nullable 防御：Graph 偶尔返回 subject=null / sender=null 等。
    .get(key, "") 不会把 None 转空串；显式 (x or "") 兜底。
    """
    body_html = (m.get("body") or {}).get("content", "") or ""
    _sndr = m.get("sender") or {}
    _sndr_addr = _sndr.get("emailAddress") or {}
    return Mail(
        id=m["id"],
        subject=(m.get("subject") or ""),
        sender=(_sndr_addr.get("address") or ""),
        received_at=(m.get("receivedDateTime") or ""),
        body_text=_html_to_text(body_html) if "<" in body_html else body_html,
        is_read=bool(m.get("isRead", False)),
        attachments=[
            {
                "id":          (a.get("id") or ""),
                "name":        (a.get("name") or ""),
                "contentType": (a.get("contentType") or ""),
                "size":        a.get("size", 0) or 0,
                "isInline":    bool(a.get("isInline", False)),
            }
            for a in (m.get("attachments") or [])
        ],
        sender_display_name=(_sndr_addr.get("name") or ""),
        conversation_id=(m.get("conversationId") or ""),
    )


def iter_all_messages(
    gc: GraphClient,
    *,
    folder: str = "Inbox",
    since: Optional[str] = None,
    until: Optional[str] = None,
    is_read: Optional[bool] = None,
    exclude_categories: Optional[list[str]] = None,
    page_size: int = 100,
    no_attachments: bool = False,
    attachments_meta: bool = False,
    order: str = "asc",
) -> Iterator[Mail]:
    """Generator: 翻页拉所有符合条件的 Mail。

    Args:
      since / until: ISO 8601 ("2026-02-01T00:00:00Z")
      is_read:      True/False 限定；None = 不限
      exclude_categories: 跳过带这些 category 的邮件
      page_size:    每页条数（Graph 上限 100）

    Yields:
      Mail dataclass（**未 redact**—— caller 走 _redact_mail）

    Pagination:
      第一次调用带 $filter + $top + $select + $orderby
      后续 page 走 @odata.nextLink（已是完整 URL 含 $skiptoken）→
      path 切到 nextLink、params=None（nextLink 自带 query string）
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

    initial_params: dict[str, str] = {
        "$top": str(page_size),
        "$select": "id,subject,sender,receivedDateTime,bodyPreview,body,isRead,categories,conversationId",
        "$orderby": f"receivedDateTime {order}",
    }
    if attachments_meta:
        # 轻量 expand：只取 id/name/contentType/size/isInline，不下载 binary
        initial_params["$expand"] = (
            "attachments($select=id,name,contentType,size,isInline)"
        )
    elif not no_attachments:
        # 默认展开 attachments；某些大附件邮件会让 Graph 返回体被截断 →
        # 触发 JSONDecodeError。--no-attachments 时只取 has_attachments 计数。
        initial_params["$expand"] = "attachments"
    if parts:
        initial_params["$filter"] = " and ".join(parts)

    path = f"/users/{gc.user_id}/mailFolders/{folder}/messages"
    params: Optional[dict[str, str]] = initial_params
    total = 0

    while path:
        r = gc._request("GET", path, params=params)
        r.raise_for_status()
        try:
            data = r.json()
        except Exception as e:
            head = (r.text or "")[:200]
            LOG.warning(
                "[harvest] JSON parse failed (%s); status=%s body_head=%r",
                e, r.status_code, head,
            )
            # 截断多半是大附件邮件；用轻量 $select 重拉这一页
            if not no_attachments:
                retry_params = dict(initial_params)
                retry_params["$select"] = (
                    "id,subject,sender,receivedDateTime,bodyPreview,"
                    "isRead,categories,conversationId,hasAttachments"
                )
                retry_params.pop("$expand", None)
                retry_params["$skip"] = str(total)
                r2 = gc._request("GET", path.split("?")[0],
                                 params=retry_params)
                r2.raise_for_status()
                data = r2.json()
                LOG.info("[harvest] 退到无 attachments 模式，已 %d 条", total)
            else:
                raise
        for m in data.get("value", []):
            yield _message_to_mail(m)
            total += 1
        # 翻页：nextLink 是绝对 URL（含 GRAPH_BASE + $skiptoken）
        next_link = data.get("@odata.nextLink", "")
        if next_link.startswith(GRAPH_BASE):
            path = next_link[len(GRAPH_BASE):]
            params = None  # nextLink 已带 query string
        else:
            path = ""
            params = None

    LOG.info("[harvest] %s: %d messages fetched", gc.user_id, total)


# ── 命令行 ────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="从 Outlook mailbox 拉候选邮件 → 脱敏 → JSONL。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--mailbox", default=os.environ.get("MAILBOX_USER", "me"),
        help="Mailbox UPN 或 'me'。多个 mailbox 需要分别跑。默认读 MAILBOX_USER env。",
    )
    ap.add_argument(
        "--folder", default="Inbox",
        help="Mail folder；默认 Inbox。其它常见：JunkEmail / Archive。",
    )
    ap.add_argument(
        "--since", default=None,
        help="ISO 8601，例如 2026-02-01T00:00:00Z。默认 = 180 天前。",
    )
    ap.add_argument(
        "--until", default=None,
        help="ISO 8601；默认 = now。",
    )
    ap.add_argument(
        "--is-read", choices=["true", "false", "any"], default="any",
        help="限定读 / 未读 / 不限。默认 any（拉历史全量）。",
    )
    ap.add_argument(
        "--exclude-categories", default="关闭-无效邮件,关闭:无效邮件",
        help="逗号分隔；命中 category 的邮件跳过。空字符串 = 不过滤。",
    )
    ap.add_argument(
        "--page-size", type=int, default=100,
        help="每页大小（Graph 上限 100）。",
    )
    ap.add_argument(
        "--max-messages", type=int, default=0,
        help="硬上限条数；0 = 不限（拉完为止）。",
    )
    ap.add_argument(
        "--limit-mb", type=int, default=500,
        help="输出文件大小上限（MB）；超过即停。0 = 不限。",
    )
    ap.add_argument(
        "--output", default="eval/candidates.jsonl",
        help="输出 JSONL 路径；默认 eval/candidates.jsonl (gitignored)。",
    )
    ap.add_argument(
        "--append", action="store_true",
        help="追加到现有文件（多 mailbox 合并到一个 JSONL 时用）。",
    )
    ap.add_argument(
        "--verbose", "-v", action="store_true",
        help="DEBUG 日志。",
    )
    ap.add_argument(
        "--no-attachments", action="store_true",
        help="不要 $expand=attachments；附件多的邮件会让 Graph 返回体被截断 → JSON 解析失败。"
             "harvest 只用 has_attachments/attachment_count/attachment_names 即可，"
             "二进制不进 LLM。",
    )
    ap.add_argument(
        "--attachments-meta", action="store_true",
        help="$expand=attachments($select=id,name,contentType,size,isInline) — "
             "只取附件元数据，不下载 binary。比 --no-attachments 多了附件 "
             "name/contentType，但比默认 $expand=attachments 安全（不下载内容）。",
    )
    ap.add_argument(
        "--order", choices=["asc", "desc"], default="asc",
        help="receivedDateTime 排序方向。默认 asc（旧→新）；要看最近流量用 desc。",
    )
    return ap.parse_args(argv)


# ── main ──────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # 默认 since = 180 天前（半年 backlog；可在 M2.6 跑长一些如 365）
    if not args.since:
        since_dt = datetime.now(timezone.utc) - timedelta(days=180)
        args.since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── 认证 ───────────────────────────────────────────────────────
    tenant = os.environ.get("AZURE_AD_TENANT_ID", "").strip()
    client_id = os.environ.get("AZURE_AD_CLIENT_ID", "").strip()
    if not tenant or not client_id:
        print(
            "[ERR] AZURE_AD_TENANT_ID / AZURE_AD_CLIENT_ID 未设置。"
            "从 .env.example 复制一份到 .env 并填好。",
            file=sys.stderr,
        )
        return 2
    LOG.info("[harvest] auth: tenant=%s client=%s ...", tenant[:8], client_id[:8])
    token = get_token(tenant, client_id)
    gc = GraphClient(token, user_id=args.mailbox)

    # ── 输出文件 ──────────────────────────────────────────────────
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if (args.append and out_path.exists()) else "w"

    exclude_categories = (
        [c.strip() for c in args.exclude_categories.split(",") if c.strip()]
        if args.exclude_categories else []
    )
    is_read_map = {"true": True, "false": False, "any": None}
    is_read = is_read_map[args.is_read]

    n_fetched = 0
    n_redacted_ok = 0
    n_dropped_redact_fail = 0
    pii_total = {"vin": 0, "email": 0, "phone": 0}

    try:
        out_f = out_path.open(mode, encoding="utf-8")
    except OSError as e:
        print(f"[ERR] 打开输出文件失败: {e}", file=sys.stderr)
        return 2
    try:
        # chmod path (not file handle — TextIOWrapper 没 chmod 方法)
        try:
            out_path.chmod(0o600)
        except OSError:
            pass  # Windows FS 不支持

        for mail in iter_all_messages(
            gc,
            folder=args.folder,
            since=args.since,
            until=args.until,
            is_read=is_read,
            exclude_categories=exclude_categories,
            page_size=args.page_size,
            no_attachments=args.no_attachments,
            attachments_meta=args.attachments_meta,
            order=args.order,
        ):
            n_fetched += 1

            # ── redact（fail-closed）────────────────────────────
            try:
                redacted, counts = _redact_mail(mail)
            except Exception as e:
                LOG.warning(
                    "[harvest] redact 失败, 跳过 mail_id=%s subject=%r err=%s: %s",
                    mail.id[:24] if mail.id else "<no-id>",
                    (mail.subject or "")[:40],
                    type(e).__name__,
                    e,
                )
                n_dropped_redact_fail += 1
                continue

            n_redacted_ok += 1
            for k in pii_total:
                pii_total[k] += counts[k]

            # ── 写 JSONL ───────────────────────────────────
            line = {
                "graph_message_id":       redacted.id,
                "mailbox":                args.mailbox,
                "received_at":            redacted.received_at,
                "subject_redacted":       redacted.subject,
                "sender_redacted":        redacted.sender,
                "body_text_redacted":     redacted.body_text,
                # 显示名非 PII（不是 email/phone），透传供 greeting 个性化参考
                "sender_display_name":    redacted.sender_display_name,
                "is_read":                redacted.is_read,
                "conversation_id":        redacted.conversation_id,
                "has_attachments":        len(redacted.attachments) > 0,
                "attachment_count":       len(redacted.attachments),
                "attachment_names":       [a["name"] for a in redacted.attachments],
                # 完整附件详情（id/name/contentType/size/isInline）— vision pipeline 用
                "attachments":            redacted.attachments,
                "redaction_counts":       counts,
                "harvested_at":           datetime.now(timezone.utc).isoformat(),
                "harvested_via":          "harvest_samples.py@v1",
            }
            out_f.write(json.dumps(line, ensure_ascii=False) + "\n")

            # ── 进度 / 限制 ───────────────────────────────
            if n_redacted_ok % 50 == 0:
                LOG.info(
                    "[harvest] progress: fetched=%d ok=%d drop=%d pii(vin/email/phone)=%d/%d/%d",
                    n_fetched, n_redacted_ok, n_dropped_redact_fail,
                    pii_total["vin"], pii_total["email"], pii_total["phone"],
                )
            if args.max_messages and n_redacted_ok >= args.max_messages:
                LOG.info("[harvest] 达 --max-messages=%d，停", args.max_messages)
                break
            if args.limit_mb and out_path.stat().st_size > args.limit_mb * 1024 * 1024:
                LOG.info("[harvest] 达 --limit-mb=%d MB，停", args.limit_mb)
                break
    finally:
        out_f.close()

    # ── 报告 ────────────────────────────────────────────────────
    size_mb = out_path.stat().st_size / 1024 / 1024
    print()
    print("=== harvest_samples summary ===")
    print(f"mailbox:               {args.mailbox}")
    print(f"folder:                {args.folder}")
    print(f"since -> until:        {args.since}  ->  {args.until or 'now'}")
    print(f"is_read filter:        {args.is_read}")
    print(f"exclude_categories:    {exclude_categories}")
    print(f"---")
    print(f"fetched:               {n_fetched}")
    print(f"redacted (written):    {n_redacted_ok}")
    print(f"dropped (redact fail): {n_dropped_redact_fail}")
    print(f"---")
    print(f"PII masked total:      vin={pii_total['vin']}  email={pii_total['email']}  phone={pii_total['phone']}")
    print(f"---")
    print(f"output:                {out_path}  ({size_mb:.2f} MB, mode={mode})")
    print(f"next step:             python3 scripts/sample_ledger.py --candidates {out_path} "
          f"--out eval/_samples_to_label.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())