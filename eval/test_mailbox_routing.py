"""
test_mailbox_routing.py — 验证 MailboxRouter + self-mail 跳过。

不连真 Graph，纯单测 + mock-mail 跑：
  Part A. MailboxRouter 单元测试（region → mailbox）
  Part B. should_skip_sender（自家 mailbox / skip domain / 大小写 / 空值）
  Part C. build_router_from_env（从 env 构造）
  Part D. 集成：在 mock_mail 里塞一封 self-mail，验证不进 triage / 不建草稿
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# 让 agent/ scripts/ 可 import
sys.path.insert(0, str(ROOT))

from agent.outlook_drafts import (
    MailboxRouter,
    build_router_from_env,
)


# ── Part A: region → mailbox 路由 ─────────────────────────────────────

def test_route_uk_to_uk_mailbox():
    r = MailboxRouter(uk="UK@example.com", au="AU@example.com",
                      default="DEF@example.com")
    route = r.route("Europe")
    assert route.mailbox_upn == "UK@example.com", f"got {route.mailbox_upn}"
    assert route.source == "region_match"


def test_route_oceania_to_au_mailbox():
    r = MailboxRouter(uk="UK@example.com", au="AU@example.com",
                      default="DEF@example.com")
    route = r.route("Oceania")
    assert route.mailbox_upn == "AU@example.com", f"got {route.mailbox_upn}"
    assert route.source == "region_match"


def test_route_na_to_default():
    r = MailboxRouter(uk="UK@example.com", au="AU@example.com",
                      default="DEF@example.com")
    route = r.route("North America")
    assert route.mailbox_upn == "DEF@example.com", f"got {route.mailbox_upn}"
    assert route.source == "region_match"


def test_route_unknown_region_falls_back_to_default():
    r = MailboxRouter(uk="UK@example.com", au="AU@example.com",
                      default="DEF@example.com")
    route = r.route("Atlantis")
    assert route.mailbox_upn == "DEF@example.com"
    assert route.source == "default"


def test_route_empty_region_falls_back_to_default():
    r = MailboxRouter(uk="UK@example.com", au="AU@example.com",
                      default="DEF@example.com")
    route = r.route("")
    assert route.mailbox_upn == "DEF@example.com"
    assert route.source == "default"


def test_route_returns_none_when_nothing_configured():
    r = MailboxRouter()
    route = r.route("Europe")
    assert route.mailbox_upn is None
    assert route.source == "unknown"


def test_route_falls_back_to_uk_when_default_missing():
    """M1 早期可能没设 MAILBOX_DEFAULT：fallback 应该是 UK 而不是 None。"""
    r = MailboxRouter(uk="UK@example.com", au=None, default=None)
    route = r.route("Asia")
    assert route.mailbox_upn == "UK@example.com"


# ── Part B: should_skip_sender ─────────────────────────────────────────

def test_skip_sender_exact_match_uk():
    r = MailboxRouter(uk="UK@example.com", au="AU@example.com",
                      default="DEF@example.com")
    assert r.should_skip_sender("UK@example.com") is True


def test_skip_sender_case_insensitive_exact_match():
    r = MailboxRouter(uk="Customer.service@mg.co.uk",
                      au="customercare@mgmotor.com.au",
                      default="x@x.com")
    # 生产配置的大小写：UK 大写 C，AU 小写 c
    assert r.should_skip_sender("customer.service@mg.co.uk") is True
    assert r.should_skip_sender("CUSTOMERCARE@MGMOTOR.COM.AU") is True


def test_skip_sender_real_customer_not_skipped():
    r = MailboxRouter(uk="UK@example.com", au="AU@example.com",
                      default="DEF@example.com",
                      skip_domains="mg.co.uk,mgmotor.com.au")
    assert r.should_skip_sender("cristian@cargomuntenia.ro") is False
    assert r.should_skip_sender("real.customer@gmail.com") is False


def test_skip_sender_domain_substring_match():
    r = MailboxRouter(uk="UK@example.com", au="AU@example.com",
                      default="DEF@example.com",
                      skip_domains="mg.co.uk,mgmotor.com.au")
    assert r.should_skip_sender("anyone@mg.co.uk") is True
    assert r.should_skip_sender("customercare@mgmotor.com.au") is True
    # domain 子串也要命中
    assert r.should_skip_sender("sub.mg.co.uk.hosted.com") is True


def test_skip_sender_empty_safe():
    r = MailboxRouter(uk="UK@example.com", au="AU@example.com",
                      default="DEF@example.com",
                      skip_domains="mg.co.uk")
    assert r.should_skip_sender("") is False
    assert r.should_skip_sender(None) is False


def test_skip_sender_with_no_domains_configured():
    """没配 skip_domains 时只检查自家 mailbox 全集。"""
    r = MailboxRouter(uk="UK@example.com", au="AU@example.com",
                      default="DEF@example.com")
    assert r.should_skip_sender("UK@example.com") is True
    # 任何其它邮箱都不跳过
    assert r.should_skip_sender("random@mg.co.uk") is False


# ── Part B+: M2.5.5 find_already_replied_to (CS 已回过线程检测) ─────────

def test_find_already_replied_to_english_wrote():
    """英文标准 reply quote：`<our_mbox> wrote:` → 命中。"""
    r = MailboxRouter(uk="customer.care@ismart-service.com")
    body = (
        "Hi,\n\n"
        "Thanks for forwarding. As discussed below.\n\n"
        "On Mon, Jul 28, 2026 at 10:30 AM, Customer Care "
        "<customer.care@ismart-service.com> wrote:\n"
        "> Hi, please help me...\n"
    )
    hit = r.find_already_replied_to(body)
    assert hit == "customer.care@ismart-service.com", hit


def test_find_already_replied_to_chinese_xiedao():
    """中文「写道:」reply quote → 命中。"""
    r = MailboxRouter(uk="customer.care@ismart-service.com")
    body = (
        "您好，\n\n"
        "请参考下方客服回复。\n\n"
        "在 2026年7月28日 10:30，Customer Care "
        "<customer.care@ismart-service.com> 写道:\n"
        "> 您好，请问有什么可以帮您？\n"
    )
    hit = r.find_already_replied_to(body)
    assert hit == "customer.care@ismart-service.com", hit


def test_find_already_replied_to_no_quote():
    """正文里只提到自家邮箱（无 reply quote）→ 不命中。"""
    r = MailboxRouter(uk="customer.care@ismart-service.com")
    body = "Please contact us at customer.care@ismart-service.com for help."
    assert r.find_already_replied_to(body) is None


def test_find_already_replied_to_empty_body():
    r = MailboxRouter(uk="customer.care@ismart-service.com")
    assert r.find_already_replied_to("") is None
    assert r.find_already_replied_to(None) is None


def test_find_already_replied_to_no_self_mailboxes():
    """没配自家 mailbox → 永远不命中（防御性）。"""
    r = MailboxRouter()  # 全空
    body = "On Mon, Customer Care <customer.care@ismart-service.com> wrote:\n> ..."
    assert r.find_already_replied_to(body) is None


def test_user_upn_added_to_self_mailboxes():
    """M2.5.5: MAILBOX_USER 也要纳入 self_mailboxes（防止漏配 MAILBOX_DEFAULT 时漏判）。"""
    r = MailboxRouter(user_upn="customer.care@ismart-service.com")
    # 应该把 user_upn 当 own mailbox
    assert r.should_skip_sender("customer.care@ismart-service.com") is True
    # reply quote 检测也应命中
    body = "On Mon, Customer Care <customer.care@ismart-service.com> wrote:\n> ..."
    assert r.find_already_replied_to(body) == "customer.care@ismart-service.com"


# ── Part C: build_router_from_env ─────────────────────────────────────

def test_build_router_from_env_reads_all():
    saved = {k: os.environ.pop(k, None) for k in (
        "MAILBOX_UK", "MAILBOX_AU", "MAILBOX_DEFAULT", "SKIP_SENDER_DOMAINS"
    )}
    try:
        os.environ["MAILBOX_UK"] = "UK@x.com"
        os.environ["MAILBOX_AU"] = "AU@x.com"
        os.environ["MAILBOX_DEFAULT"] = "DEF@x.com"
        os.environ["SKIP_SENDER_DOMAINS"] = "mg.co.uk, mgmotor.com.au"
        r = build_router_from_env()
        assert r.uk == "UK@x.com"
        assert r.au == "AU@x.com"
        assert r.default == "DEF@x.com"
        assert r._skip_domains == ["mg.co.uk", "mgmotor.com.au"]
        assert r.route("Oceania").mailbox_upn == "AU@x.com"
        assert r.should_skip_sender("anyone@mg.co.uk") is True
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_build_router_from_env_with_missing_vars():
    """env 完全没设时应该安全构造（所有字段 None）。"""
    saved = {k: os.environ.pop(k, None) for k in (
        "MAILBOX_UK", "MAILBOX_AU", "MAILBOX_DEFAULT", "SKIP_SENDER_DOMAINS"
    )}
    try:
        r = build_router_from_env()
        assert r.uk is None
        assert r.au is None
        assert r.default is None
        assert r._skip_domains == []
        # 没配置时 route 应该返回 unknown
        assert r.route("Europe").source == "unknown"
        # 没配置时也不误跳过
        assert r.should_skip_sender("anyone@mg.co.uk") is False
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── Part D: 集成测试（mock-mail 含 self-mail）─────────────────────────

def _run_mock_with_router(mock_mails: list[dict],
                          env: dict | None = None) -> dict:
    """用 --mock-mail 跑一遍，返回 records。env 覆盖 MAILBOX_* 等。"""
    mock_path = Path("/tmp/_mailbox_mock.json")
    mock_path.write_text(json.dumps(mock_mails), encoding="utf-8")
    out_path = Path("/tmp/_mailbox_out.json")
    out_path.write_text("{}")
    full_env = os.environ.copy()
    full_env["MAILBOX_UK"]            = env.get("MAILBOX_UK", "UK@example.com") if env else "UK@example.com"
    full_env["MAILBOX_AU"]            = env.get("MAILBOX_AU", "AU@example.com") if env else "AU@example.com"
    full_env["MAILBOX_DEFAULT"]       = env.get("MAILBOX_DEFAULT", "DEF@example.com") if env else "DEF@example.com"
    full_env["SKIP_SENDER_DOMAINS"]   = env.get("SKIP_SENDER_DOMAINS", "mg.co.uk,mgmotor.com.au") if env else "mg.co.uk,mgmotor.com.au"
    subprocess.check_output(
        ["python3", "-m", "agent.outlook_drafts",
         "--mock-mail", str(mock_path),
         "--no-llm",
         "--out", str(out_path)],
        cwd=str(ROOT), stderr=subprocess.STDOUT, env=full_env,
    )
    return json.loads(out_path.read_text())


def test_integration_self_mail_skipped_without_llm_call():
    """self-mail 在 redact/LLM 之前就被跳过 → record.skipped == 'self_mail'。

    注：第一封真客户的 sender 会被 redact 成 user_<hash>@example.com，
    所以不能用原始邮箱查找，改用 records[0] 定位。
    """
    mock_mails = [
        {"subject": "Customer asking question",
         "sender":  "real.customer@gmail.com",
         "body_text": "How do I bind the app?"},
        {"subject": "Internal reply draft",
         "sender":  "Customer.service@mg.co.uk",   # UK 自家 mailbox 发件
         "body_text": "I already replied to the customer"},
    ]
    result = _run_mock_with_router(mock_mails)
    records = result.get("records", [])
    assert len(records) == 2, f"expected 2 records, got {len(records)}: {records}"

    # 第一封：真客户 → redact 后被 triage；应有 target_mailbox
    real = records[0]
    assert real.get("skipped") in (None, "no template match"), \
        f"real customer shouldn't be self_mail skipped, got {real}"
    assert "target_mailbox" in real, f"missing target_mailbox: {real}"

    # 第二封：UK mailbox 自家 → 必须 self_mail skip（原始 sender 保留）
    self_ = records[1]
    assert self_.get("skipped") == "self_mail", \
        f"UK mailbox send should be self_mail skipped, got {self_}"
    assert self_.get("sender") == "Customer.service@mg.co.uk", \
        f"self-mail record should keep original sender, got {self_.get('sender')}"


def test_integration_target_mailbox_recorded_for_all_mails():
    """所有非 self-mail 的 record 都应该有 target_mailbox + mailbox_route_source。

    注：--no-llm 模式下关键词分类器固定返回 region='North America'，
    所以 mock 模式下 target_mailbox 总是 MAILBOX_DEFAULT。
    真连 Graph + LLM triage 后会按 region 路由（Unit 测试 A1/A2/A3 已覆盖）。
    这里验证 record 字段被正确写入 + source 字段正确。
    """
    mock_mails = [
        {"subject": "Customer asking question",
         "sender":  "real.customer@gmail.com",
         "body_text": "How do I bind the app?"},
        {"subject": "Another question",
         "sender":  "another@example.com",
         "body_text": "Data plan question"},
    ]
    result = _run_mock_with_router(mock_mails)
    records = result.get("records", [])

    for r in records:
        assert "target_mailbox" in r, f"missing target_mailbox: {r}"
        assert "mailbox_route_source" in r, f"missing mailbox_route_source: {r}"
        # --no-llm 模式下 region 固定 NA → 应该走 default mailbox
        assert r["target_mailbox"] == "DEF@example.com", \
            f"keyword mode should fall back to default, got {r['target_mailbox']}"
        assert r["mailbox_route_source"] == "region_match", \
            f"NA region should be region_match, got {r['mailbox_route_source']}"


def test_integration_no_real_drafts_created():
    """mock 模式下不应有任何 draft_id（草稿只在真 Graph 模式创建）。"""
    mock_mails = [
        {"subject": "Should not create real draft",
         "sender":  "test@example.com",
         "body_text": "Bind the app please"},
    ]
    result = _run_mock_with_router(mock_mails)
    for r in result.get("records", []):
        assert "draft_id" not in r, \
            f"mock mode must not produce draft_id, got {r}"


# ── runner ────────────────────────────────────────────────────────────

TESTS = [
    # Part A
    ("A1 route Europe → UK mailbox",              test_route_uk_to_uk_mailbox),
    ("A2 route Oceania → AU mailbox",             test_route_oceania_to_au_mailbox),
    ("A3 route North America → default",          test_route_na_to_default),
    ("A4 route unknown region → default",         test_route_unknown_region_falls_back_to_default),
    ("A5 route empty region → default",           test_route_empty_region_falls_back_to_default),
    ("A6 route no config → unknown",              test_route_returns_none_when_nothing_configured),
    ("A7 fallback to UK when default missing",    test_route_falls_back_to_uk_when_default_missing),
    # Part B
    ("B1 skip sender exact match (UK)",           test_skip_sender_exact_match_uk),
    ("B2 skip sender case-insensitive",           test_skip_sender_case_insensitive_exact_match),
    ("B3 real customer not skipped",              test_skip_sender_real_customer_not_skipped),
    ("B4 skip sender domain substring",           test_skip_sender_domain_substring_match),
    ("B5 skip sender empty safe",                 test_skip_sender_empty_safe),
    ("B6 skip with no domains configured",        test_skip_sender_with_no_domains_configured),
    # Part B+: M2.5.5 find_already_replied_to
    ("B+1 wrote: English reply quote detected",   test_find_already_replied_to_english_wrote),
    ("B+2 写道: Chinese reply quote detected",     test_find_already_replied_to_chinese_xiedao),
    ("B+3 plain email mention not detected",      test_find_already_replied_to_no_quote),
    ("B+4 empty body safe",                        test_find_already_replied_to_empty_body),
    ("B+5 no self mailboxes = no detection",       test_find_already_replied_to_no_self_mailboxes),
    ("B+6 user_upn falls into self_mailboxes",     test_user_upn_added_to_self_mailboxes),
    # Part C
    ("C1 build_router_from_env reads all",        test_build_router_from_env_reads_all),
    ("C2 build_router_from_env missing safe",     test_build_router_from_env_with_missing_vars),
    # Part D
    ("D1 integration self-mail skipped",          test_integration_self_mail_skipped_without_llm_call),
    ("D2 integration target mailbox recorded",    test_integration_target_mailbox_recorded_for_all_mails),
    ("D3 integration no real drafts in mock mode",test_integration_no_real_drafts_created),
]


def main() -> int:
    print("=" * 70)
    print("Mailbox Routing + Self-mail Skip Tests")
    print("=" * 70)
    passed = 0
    total = len(TESTS)
    for label, fn in TESTS:
        try:
            fn()
            passed += 1
            print(f"  ✓ {label}")
        except Exception as e:
            print(f"  ✗ {label}")
            print(f"      {type(e).__name__}: {e}")
    print("=" * 70)
    print(f"{passed}/{total} cases passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
