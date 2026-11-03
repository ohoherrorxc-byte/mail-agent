"""
test_outlook_safety.py — 验证 outlook_drafts.py 的 'no send, no delete' 硬约束。

不连真 Graph，全部在本地伪造 URL 字符串触发守卫。验证 6 类禁止行为全部爆错：
  1. send_mail() 方法直接调用
  2. delete_message() 方法直接调用
  3. move_message() 方法直接调用
  4. mark_as_read() 方法直接调用
  5. _request 收到 DELETE 方法
  6. _request 收到 POST /sendMail
  7. _request 收到 POST /messages/{id}/move
  8. _request 收到 PATCH isRead: true
  9. _request 收到不在白名单的 URL（如 /calendar）

允许的调用必须不报错：
  - GET /users/me/mailFolders/Inbox/messages
  - POST /users/me/messages/{id}/createReply
  - PATCH /users/me/messages/{draftId} body={...}
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让 scripts/eval 都能 import
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from unittest.mock import MagicMock

from agent.outlook_drafts import (
    GraphClient,
    MailboxSafetyError,
    _acquire_scan_lock,
    _html_quote_only,
    _merge_reply_keep_quote,
    _release_scan_lock,
)


def _client() -> GraphClient:
    """造一个不带真 session 的 GraphClient，用 MagicMock 替换 session.request。"""
    c = GraphClient.__new__(GraphClient)
    c.user_id = "me"
    c.session = MagicMock()
    c.session.request.return_value = MagicMock(
        status_code=200,
        text='{"id": "fake-draft-id"}',
        json=lambda: {"id": "fake-draft-id"},
    )
    c._created_draft_ids = set()
    c._queue_cats_ready = False
    return c


def _client_with_draft() -> GraphClient:
    """造一个已 'createReply' 过的客户端 —— PATCH 该 draft id 应被允许。"""
    c = _client()
    c._created_draft_ids.add("fake-draft-id")
    return c


def expect_blocked(label: str, fn):
    try:
        fn()
    except MailboxSafetyError as e:
        print(f"  ✓ {label} → blocked: {e!s:.80}")
        return True
    except Exception as e:
        print(f"  ✗ {label} → wrong exception type: {type(e).__name__}: {e}")
        return False
    print(f"  ✗ {label} → did NOT raise (should have blocked!)")
    return False


def expect_allowed(label: str, fn):
    try:
        fn()
    except MailboxSafetyError as e:
        print(f"  ✗ {label} → blocked but should be allowed: {e}")
        return False
    except Exception:
        # 网络/其他错误无所谓，关键是不要被 MailboxSafetyError 拦
        print(f"  ✓ {label} → allowed (passed guard)")
        return True
    print(f"  ✓ {label} → allowed (passed guard)")
    return True


def main() -> int:
    print("=" * 60)
    print("Outlook Drafts Safety Audit")
    print("=" * 60)
    passed = 0
    total = 0

    cases_blocked = [
        ("send_mail() method",        lambda: _client().send_mail("to@x.com", subject="x")),
        ("delete_message() method",   lambda: _client().delete_message("abc")),
        ("move_message() method",     lambda: _client().move_message("abc", "DeletedItems")),
        ("mark_as_read() method",     lambda: _client().mark_as_read("abc")),
        ("HTTP DELETE method",        lambda: _client()._request("DELETE", "/users/me/messages/abc")),
        ("DELETE original attachment", lambda: _client()._request("DELETE", "/users/me/messages/abc/attachments/att1")),
        ("POST /sendMail",            lambda: _client()._request("POST", "/users/me/sendMail", json_body={})),
        ("POST /messages/x/move",     lambda: _client()._request("POST", "/users/me/messages/abc/move", json_body={"destinationId": "DeletedItems"})),
        ("PATCH isRead=true",         lambda: _client()._request("PATCH", "/users/me/messages/abc", json_body={"isRead": True})),
        ("GET /calendar (off-list)",  lambda: _client()._request("GET", "/users/me/calendar/events")),
        ("POST new mail (no draft)",  lambda: _client()._request("POST", "/users/me/messages", json_body={"subject": "x"})),
        ("PATCH original mail body",  lambda: _client()._request("PATCH", "/users/me/messages/abc", json_body={"body": {"content": "x"}})),
        ("POST attach on original",   lambda: _client()._request("POST", "/users/me/messages/abc/attachments", json_body={"name": "x"})),
        ("POST regular mail folder",  lambda: _client()._request("POST", "/users/me/mailFolders", json_body={"displayName": "junk"})),
    ]
    for label, fn in cases_blocked:
        total += 1
        if expect_blocked(label, fn):
            passed += 1

    cases_allowed = [
        ("GET inbox messages",        lambda: _client()._request("GET", "/users/me/mailFolders/Inbox/messages")),
        ("PATCH original mail categories+isRead", lambda: _client()._request("PATCH", "/users/me/messages/abc", json_body={"categories": ["关闭-无效邮件"], "isRead": True})),
        ("POST createReply",          lambda: _client()._request("POST", "/users/me/messages/abc/createReply", json_body={})),
        ("PATCH own draft body",      lambda: _client_with_draft()._request("PATCH", "/users/me/messages/fake-draft-id", json_body={"body": {"contentType": "text", "content": "hi"}})),
        ("GET single message",        lambda: _client()._request("GET", "/users/me/messages/abc")),
        ("GET other folder",          lambda: _client()._request("GET", "/users/me/mailFolders/SentItems/messages")),
        ("GET Drafts folder messages", lambda: _client()._request("GET", "/users/me/mailFolders/Drafts/messages")),
        ("DELETE copied file on own draft", lambda: _client_with_draft()._request("DELETE", "/users/me/messages/fake-draft-id/attachments/att1")),
        ("GET draft attachments list", lambda: _client_with_draft()._request("GET", "/users/me/messages/fake-draft-id/attachments")),
        ("POST kb pdf on own draft",  lambda: _client_with_draft()._request("POST", "/users/me/messages/fake-draft-id/attachments", json_body={"@odata.type": "#microsoft.graph.fileAttachment", "name": "Digital Key Function.pdf"})),
        ("GET outlook masterCategories", lambda: _client()._request("GET", "/users/me/outlook/masterCategories")),
        ("POST outlook masterCategories", lambda: _client()._request("POST", "/users/me/outlook/masterCategories", json_body={"displayName": "转人工", "color": "preset9"})),
        ("PATCH outlook masterCategory color", lambda: _client()._request("PATCH", "/users/me/outlook/masterCategories/cat-id", json_body={"color": "preset15"})),
        ("POST mail search folder", lambda: _client()._request("POST", "/users/me/mailFolders", json_body={"@odata.type": "#microsoft.graph.mailSearchFolder", "displayName": "转人工"})),
        ("POST search folder as child", lambda: _client()._request("POST", "/users/me/mailFolders/finder/childFolders", json_body={"@odata.type": "#microsoft.graph.mailSearchFolder", "displayName": "转人工"})),
    ]
    for label, fn in cases_allowed:
        total += 1
        if expect_allowed(label, fn):
            passed += 1

    total += 1
    merged = _merge_reply_keep_quote(
        "Dear customer,\nThank you.",
        "<html><body><div id=\"divRplyFwdMsg\">From: A<br>Original</div></body></html>",
    )
    if (
        "Dear customer" in merged
        and "divRplyFwdMsg" in merged
        and "Original" in merged
        and merged.lower().find("dear customer") < merged.lower().find("divrplyfwdmsg")
    ):
        print("  ✓ merge reply keeps quoted original below")
        passed += 1
    else:
        print("  ✗ merge reply did not keep quote")

    total += 1
    plain_quote = (
        "<html><body><div id=\"divRplyFwdMsg\">"
        "__________\nFrom: Shant &lt;a@b.com&gt;\nSent: Monday\n"
        "To: customer.care\nSubject: Issues\n\nDear iSmart service team,\n"
        "Country: Australia\n</div></body></html>"
    )
    merged_plain = _merge_reply_keep_quote("Dear customer,\nThank you.", plain_quote)
    if (
        merged_plain.count("<br>") >= 6
        and "Country: Australia" in merged_plain
        and merged_plain.lower().find("dear customer") < merged_plain.find("divRplyFwdMsg")
    ):
        print("  ✓ plain-text quote keeps line breaks as <br>")
        passed += 1
    else:
        print("  ✗ plain-text quote collapsed")

    total += 1
    html_quote = (
        "<html><body><div id=\"divRplyFwdMsg\">From: A<br>Sent: B<br>"
        "Original line</div></body></html>"
    )
    merged_html = _merge_reply_keep_quote("Hi.", html_quote)
    if merged_html == _merge_reply_keep_quote("Hi.", html_quote) and "From: A<br>Sent: B<br>" in merged_html:
        print("  ✓ HTML quote with <br> left unchanged")
        passed += 1
    else:
        print("  ✗ HTML quote with <br> was rewritten")

    total += 1
    rebuilt = _html_quote_only(
        "<html><body><div>OLD inspection reply</div>"
        "<div id=\"divRplyFwdMsg\">From: A</div></body></html>"
    )
    if "OLD inspection" not in rebuilt and "divRplyFwdMsg" in rebuilt:
        print("  ✓ rebuild keeps quote, drops previous reply")
        passed += 1
    else:
        print("  ✗ rebuild quote strip failed")

    total += 1
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EMAILAGENT_SCAN_LOCK"] = str(Path(tmp) / "scan.lock")
        try:
            fd1 = _acquire_scan_lock(wait=False)
            fd2 = _acquire_scan_lock(wait=False)
            if fd1 is not None and fd2 is None:
                print("  ✓ second scanner cannot take the lock")
                passed += 1
            else:
                print(f"  ✗ scan lock exclusive failed fd1={fd1} fd2={fd2}")
            _release_scan_lock(fd1)
            _release_scan_lock(fd2)
        finally:
            os.environ.pop("EMAILAGENT_SCAN_LOCK", None)

    print("=" * 60)
    print(f"{passed}/{total} cases passed")
    if passed != total:
        print("FAIL")
        return 1
    print("OK — hard constraints enforced")
    return 0


if __name__ == "__main__":
    sys.exit(main())