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

from agent.outlook_drafts import GraphClient, MailboxSafetyError


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
        ("POST /sendMail",            lambda: _client()._request("POST", "/users/me/sendMail", json_body={})),
        ("POST /messages/x/move",     lambda: _client()._request("POST", "/users/me/messages/abc/move", json_body={"destinationId": "DeletedItems"})),
        ("PATCH isRead=true",         lambda: _client()._request("PATCH", "/users/me/messages/abc", json_body={"isRead": True})),
        ("GET /calendar (off-list)",  lambda: _client()._request("GET", "/users/me/calendar/events")),
        ("POST new mail (no draft)",  lambda: _client()._request("POST", "/users/me/messages", json_body={"subject": "x"})),
        ("PATCH original mail body",  lambda: _client()._request("PATCH", "/users/me/messages/abc", json_body={"body": {"content": "x"}})),
    ]
    for label, fn in cases_blocked:
        total += 1
        if expect_blocked(label, fn):
            passed += 1

    cases_allowed = [
        ("GET inbox messages",        lambda: _client()._request("GET", "/users/me/mailFolders/Inbox/messages")),
        ("POST createReply",          lambda: _client()._request("POST", "/users/me/messages/abc/createReply", json_body={})),
        ("PATCH own draft body",      lambda: _client_with_draft()._request("PATCH", "/users/me/messages/fake-draft-id", json_body={"body": {"contentType": "text", "content": "hi"}})),
        ("GET single message",        lambda: _client()._request("GET", "/users/me/messages/abc")),
        ("GET other folder",          lambda: _client()._request("GET", "/users/me/mailFolders/SentItems/messages")),
    ]
    for label, fn in cases_allowed:
        total += 1
        if expect_allowed(label, fn):
            passed += 1

    print("=" * 60)
    print(f"{passed}/{total} cases passed")
    if passed != total:
        print("FAIL")
        return 1
    print("OK — hard constraints enforced")
    return 0


if __name__ == "__main__":
    sys.exit(main())