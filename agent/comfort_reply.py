"""安抚信识别。

两套已发出的安抚正文不算客服业务回复：不挡首封草稿，也不算跟进转人工。
只认回复区开头，引用块里出现同样的句子不算。
"""
from __future__ import annotations

import re

COMFORT_CATEGORY = "已安抚"

_OPENING = "thank you for contacting the ismart service team"
_PATIENCE = "we kindly ask for your patience"
_INTAKE = (
    "as your feedback involves technical details, "
    "we kindly request the following information"
)

# 回复区结束、引用开始。只切这些，避免把安抚信正文里的普通句子切掉。
_QUOTE_CUTS = (
    re.compile(r"(?im)^[ \t]*-{5,}\s*original message\s*-{5,}"),
    re.compile(r"(?im)^[ \t]*发件人\s*[:：]"),
    re.compile(r"(?im)^[ \t]*from\s*:"),
    re.compile(r"(?im)^[ \t]*on .+wrote\s*:"),
    re.compile(r"(?i)\bwrote\s*:"),
    re.compile(r"(?i)写道\s*[:：]"),
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold()).strip()


def reply_head(text: str) -> str:
    """引用块之前的回复区。"""
    raw = text or ""
    cut_at = len(raw)
    for pat in _QUOTE_CUTS:
        matched = pat.search(raw)
        if matched and matched.start() < cut_at:
            cut_at = matched.start()
    return raw[:cut_at]


def is_comfort_reply(text: str) -> bool:
    """回复区是安抚信（耐心等待，或要技术资料）才算。"""
    head = _norm(reply_head(text))
    if _OPENING not in head:
        return False
    return _PATIENCE in head or _INTAKE in head


def quoted_reply_is_comfort(body: str, mailbox: str) -> bool:
    """客户引用的那一段是安抚信，而不是业务回复。"""
    raw = body or ""
    mbox = (mailbox or "").strip()
    if not raw or not mbox:
        return False
    idx = raw.casefold().find(mbox.casefold())
    if idx < 0:
        return False
    rest = raw[idx + len(mbox):]
    marker = re.search(r"(?i)wrote\s*:|写道\s*[:：]|έγραψε\s*:", rest)
    if not marker:
        return False
    return is_comfort_reply(rest[marker.end():])


def _customer_turns(
    thread_msgs: list[dict] | None,
    self_emails: set[str],
    current=None,
) -> list[dict]:
    own = {e.casefold() for e in self_emails if e}
    turns: list[dict] = []
    seen: set[str] = set()
    for msg in thread_msgs or []:
        if msg.get("is_draft"):
            continue
        sender = (msg.get("sender_email") or "").casefold()
        if sender in own:
            continue
        if is_comfort_reply(msg.get("body_text") or ""):
            continue
        text = (msg.get("body_text") or "").strip()
        if not text:
            continue
        mid = str(msg.get("id") or "")
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        words = reply_head(text).strip() or text
        turns.append({
            "id": mid,
            "received_at": msg.get("received_at") or "",
            "body_text": words,
        })
    if current is not None:
        cid = str(getattr(current, "id", "") or "")
        ctext = (getattr(current, "body_text", "") or "").strip()
        if cid not in seen and ctext and not is_comfort_reply(ctext):
            words = reply_head(ctext).strip() or ctext
            turns.append({
                "id": cid,
                "received_at": getattr(current, "received_at", "") or "",
                "body_text": words,
            })
    turns.sort(key=lambda m: m.get("received_at") or "")
    return turns


def customer_case_text(
    thread_msgs: list[dict] | None,
    self_emails: set[str],
    current=None,
) -> str:
    """两封及以上客户来信拼成一段，供选模板。只有一封时返回空，沿用原正文。"""
    turns = _customer_turns(thread_msgs, self_emails, current)
    if len(turns) < 2:
        return ""
    parts = [
        f"Customer message {i}:\n{m['body_text'].strip()}"
        for i, m in enumerate(turns, 1)
        if (m.get("body_text") or "").strip()
    ]
    if len(parts) < 2:
        return ""
    return "\n\n".join(parts)


def latest_customer_id(
    thread_msgs: list[dict] | None,
    self_emails: set[str],
    fallback: str,
) -> str:
    turns = _customer_turns(thread_msgs, self_emails, None)
    if not turns:
        return fallback
    return str(turns[-1].get("id") or "") or fallback


def comfort_tag_target(
    thread_msgs: list[dict] | None,
    *,
    comfort_at: str,
    self_emails: set[str],
) -> str | None:
    """安抚信之前最近的一封客户来信。没有更早的，就用线程里任一封客户来信。"""
    turns = _customer_turns(thread_msgs, self_emails, None)
    if not turns:
        return None
    cutoff = comfort_at or "9999"
    before = [m for m in turns if (m.get("received_at") or "") <= cutoff]
    pool = before or turns
    mid = str(pool[-1].get("id") or "")
    return mid or None
