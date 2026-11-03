"""
test_greeting.py — 草稿称谓个性化 "Dear customer," → "Dear Mr./Ms. {Name}," 的回归。

覆盖：
  1. extract_display_name 各种 sender 形式
  2. split_first_last 各种显示名（含 honorifics / 单 token / "Last, First"）
  3. build_greeting 6 大场景
  4. personalize_greeting 替换 / 降级 / 边界
  5. _extract_email_display_name helper
  6. 端到端：template_reply.run_template_reply 走 mock LLM 验证 filled_body
  7. 端到端：EmailAgent 起草后 draft.body_md 替换
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── 1. extract_display_name ─────────────────────────────────────────

def test_extract_display_name_with_email_brackets():
    from agent.greeting import extract_display_name
    assert extract_display_name("John Smith <john@example.com>") == "John Smith"


def test_extract_display_name_with_quoted_brackets():
    from agent.greeting import extract_display_name
    assert extract_display_name('"John Smith" <john@example.com>') == "John Smith"


def test_extract_display_name_bare_email_returns_none():
    from agent.greeting import extract_display_name
    assert extract_display_name("john@example.com") is None


def test_extract_display_name_just_brackets_returns_none():
    from agent.greeting import extract_display_name
    assert extract_display_name("<john@example.com>") is None


def test_extract_display_name_plain_name():
    from agent.greeting import extract_display_name
    assert extract_display_name("John Smith") == "John Smith"


def test_extract_display_name_empty_returns_none():
    from agent.greeting import extract_display_name
    assert extract_display_name("") is None


# ── 2. split_first_last ─────────────────────────────────────────────

def test_split_first_last_two_tokens():
    from agent.greeting import split_first_last
    assert split_first_last("John Smith") == ("John", "Smith")


def test_split_first_last_with_honorific():
    from agent.greeting import split_first_last
    # honorifics 不算 first/last
    assert split_first_last("Mr. John Smith") == ("John", "Smith")
    assert split_first_last("Dr Maria Garcia") == ("Maria", "Garcia")


def test_split_first_last_three_tokens_takes_first_and_last():
    from agent.greeting import split_first_last
    # "Maria Garcia Lopez" → first=Maria, last=Lopez（中间名省略）
    assert split_first_last("Maria Garcia Lopez") == ("Maria", "Lopez")


def test_split_first_last_single_token_is_first():
    from agent.greeting import split_first_last
    # 单 token（如 Madonna）→ first=Token, last=None
    assert split_first_last("Madonna") == ("Madonna", None)


def test_split_first_last_last_comma_first():
    from agent.greeting import split_first_last
    assert split_first_last("Smith, John") == ("John", "Smith")


def test_split_first_last_empty():
    from agent.greeting import split_first_last
    assert split_first_last("") == (None, None)


# ── 3. build_greeting —— 6 大场景 ──────────────────────────────────

def test_build_greeting_with_last_name():
    from agent.greeting import build_greeting
    assert build_greeting("John Smith <john@example.com>") == "Dear Mr./Ms. Smith,"


def test_build_greeting_with_single_token_uses_first():
    from agent.greeting import build_greeting
    # 单 token（如 Madonna / Cher）→ 用 token 当 first
    assert build_greeting("Madonna <m@example.com>") == "Dear Mr./Ms. Madonna,"


def test_build_greeting_with_placeholder_email_returns_default():
    from agent.greeting import build_greeting
    # 邮箱本地部分为 customer 占位词 → 默认
    assert build_greeting("customer@example.com") == "Dear customer,"
    assert build_greeting("noreply@example.com") == "Dear customer,"
    assert build_greeting("admin@example.com") == "Dear customer,"


def test_build_greeting_no_display_name_returns_default():
    from agent.greeting import build_greeting
    assert build_greeting("") == "Dear customer,"
    assert build_greeting("john@example.com") == "Dear customer,"


def test_build_greeting_strips_honorific():
    from agent.greeting import build_greeting
    # honorific 不计入姓名
    assert build_greeting("Mr. John Smith <j@a.com>") == "Dear Mr./Ms. Smith,"


def test_build_greeting_unisex_no_gender_inference():
    """用户拍板：统一用 Mr./Ms.，不区分性别。"""
    from agent.greeting import build_greeting
    # 男女都返回同样的 Mr./Ms.
    assert build_greeting("John Smith <j@a.com>") == "Dear Mr./Ms. Smith,"
    assert build_greeting("Mary Johnson <m@a.com>") == "Dear Mr./Ms. Johnson,"
    assert build_greeting("Alex Taylor <a@a.com>") == "Dear Mr./Ms. Taylor,"


# ── 4. personalize_greeting 替换 / 降级 ────────────────────────────

def test_personalize_replaces_dear_customer():
    from agent.greeting import personalize_greeting
    body = "Dear customer,\n\nThank you for your email."
    out = personalize_greeting(body, "John Smith <john@example.com>")
    assert out.startswith("Dear Mr./Ms. Smith,")
    assert "Thank you for your email." in out


def test_personalize_replaces_dear_customer_capital_c():
    from agent.greeting import personalize_greeting
    body = "Dear Customer,\n\nThanks."
    out = personalize_greeting(body, "Mary Johnson <m@a.com>")
    assert out.startswith("Dear Mr./Ms. Johnson,")


def test_personalize_replaces_hi_customer():
    from agent.greeting import personalize_greeting
    body = "Hi customer,\n\nPlease find attached."
    out = personalize_greeting(body, "Alex Lee <a@a.com>")
    assert out.startswith("Dear Mr./Ms. Lee,")


def test_personalize_leaves_default_when_no_name():
    from agent.greeting import personalize_greeting
    body = "Dear customer,\n\nBody."
    # 纯邮箱无显示名 → 不动
    assert personalize_greeting(body, "john@example.com") == body
    # 占位词 first → 不动
    assert personalize_greeting(body, "customer@example.com") == body


def test_personalize_email_as_display_name_not_mr_ms():
    """Graph 显示名经常是邮箱本身，不能写成 Dear Mr./Ms. tcharles@sky.com。"""
    from agent.greeting import personalize_greeting
    body = "{{salutation}},\n\nThank you for your email."
    out = personalize_greeting(
        body, "tcharles@sky.com",
        display_name="tcharles@sky.com",
    )
    assert out.startswith("Dear customer,"), out
    assert "@" not in out.split("\n", 1)[0]


def test_personalize_leaves_alone_when_no_default_greeting():
    """草稿不以 "Dear customer" 开头时，不强行插入称谓。"""
    from agent.greeting import personalize_greeting
    body = "Hello, your issue has been resolved."
    out = personalize_greeting(body, "John Smith <j@a.com>")
    assert out == body


def test_personalize_only_replaces_first_occurrence():
    """正文里出现 "Dear customer" 不应被替换。"""
    from agent.greeting import personalize_greeting
    body = "Dear customer,\n\nDear customer service team, please escalate."
    out = personalize_greeting(body, "John Smith <j@a.com>")
    # 只有开头被替换；正文里的 "Dear customer service team" 保留
    assert out.startswith("Dear Mr./Ms. Smith,\n\nDear customer service team")


# ── 5. _extract_email_display_name helper ──────────────────────────

def test_extract_email_display_name_from_header():
    from agent.email_agent import _extract_email_display_name
    assert _extract_email_display_name(
        "From: John Smith <john@example.com>\n\nBody"
    ) == "John Smith"


def test_extract_email_display_name_bare():
    from agent.email_agent import _extract_email_display_name
    assert _extract_email_display_name("Mary Johnson <m@a.com> wrote:") == "Mary Johnson"


def test_extract_email_display_name_no_match():
    from agent.email_agent import _extract_email_display_name
    assert _extract_email_display_name("Hello world") is None
    assert _extract_email_display_name("From: support@example.com") is None


# ── 6. 端到端：template_reply 路径 ────────────────────────────────

def test_template_reply_substitutes_greeting():
    """run_template_reply 在传入 sender 时，把 "Dear customer," 替换成发件人称谓。"""
    from agent.template_reply import run_template_reply
    from agent.schemas import TriageResult

    # 不用 LLM：use_llm=False → 用关键词分类
    body = "I cannot bind my car, the QR code is not showing on the app"
    out = run_template_reply(
        body,
        use_llm=False,
        sender="John Smith <john@example.com>",
    )
    # app_binding 关键词分类会命中；模板是 "Dear customer,..." 开头的
    assert out["filled_body"].startswith("Dear Mr./Ms. Smith,"), (
        f"filled_body 开头应被替换，实测: {out['filled_body'][:60]!r}"
    )


def test_template_reply_keeps_default_when_no_sender():
    """M2.5.4 fix: 不传 sender 时也要把 {{salutation}}, 占位符替换成
    "Dear customer,"（fallback） —— 避免 placeholder 字面量泄漏。

    之前的行为是"不动模板"（传给 personalize_greeting 时会被早返回），
    实际上让 {{salutation}}, 漏到 Outlook Drafts —— 这是 bug。
    """
    from agent.template_reply import run_template_reply
    body = "I cannot bind my car, the QR code is not showing on the app"
    out = run_template_reply(body, use_llm=False, sender=None)
    # 占位符 {{salutation}}, 必须被替换；fallback 是 "Dear customer,"
    assert out["filled_body"].startswith("Dear customer,"), (
        f"无 sender 时占位符应被 fallback 替换，实测: {out['filled_body'][:60]!r}"
    )
    # 字面 placeholder 不应出现
    assert "{{salutation}}" not in out["filled_body"], (
        f"占位符不应泄漏，实测: {out['filled_body'][:120]!r}"
    )


# ── 7. 端到端：EmailAgent 起草路径 ─────────────────────────────────

def test_email_agent_draft_substitutes_greeting():
    """EmailAgent 起草后，draft.body_md 开头应被替换成 "Dear Mr./Ms. {Last},"。"""
    from agent.email_agent import EmailAgent
    from agent.azure_llm import LLMCall
    from agent.schemas import DraftReply, TriageResult, RetrievalResult, ComplianceResult, ImageDescription

    # Mock LLM：triage 返 app_binding；drafting 返 "Dear customer,..."
    class _MockLLM:
        def invoke_json(self, *, schema, messages, reasoning_effort="medium",
                        max_completion_tokens=4096, role="generic"):
            # 第一次调用：triage → app_binding / P2 / North America
            if role == "triage":
                from agent.schemas import TriageResult
                return TriageResult(
                    intent="app_binding", urgency="P2", region="North America",
                    language="en", requires_recall_check=False,
                    requires_warranty_lookup=False, red_flags=[],
                ), LLMCall(role="triage", model="gpt-5.4", reasoning_effort="low")
            # 第二次：retrieval
            if role == "retriever":
                from agent.schemas import CitedClause
                return RetrievalResult(
                    summary="customer cannot bind car",
                    cited_clauses=[CitedClause(
                        doc_id="na-policy-01",
                        section="NA-2026-1.1",
                        quote="binding via QR code is the standard process.",
                    )],
                    evidence_score=0.9,
                ), LLMCall(role="retriever", model="gpt-5.4", reasoning_effort="low")
            # 第三次：drafting → "Dear customer,..."
            if role == "drafting":
                return DraftReply(
                    subject="Re: cannot bind",
                    body_md="Dear customer,\n\nThank you for your email. We will help.",
                    cited_clause_ids=["na-policy-01"],
                    next_step="Please re-scan the QR code.",
                ), LLMCall(role="drafting", model="gpt-5.4", reasoning_effort="low")
            # 第四次：compliance
            if role == "compliance":
                from agent.schemas import ComplianceResult, ComplianceVerdict
                return ComplianceResult(
                    verdict=ComplianceVerdict(**{"pass": True, "violations": []}),
                    suggested_reply_adjustments=[],
                    sla_commitment="",
                    human_review_required=False,
                ), LLMCall(role="compliance", model="gpt-5.4", reasoning_effort="low")
            raise RuntimeError(f"unexpected role: {role}")

    agent = EmailAgent(llm=_MockLLM())
    # email_body 头部包含 From: 行；sender_hint 也传 → 优先用 sender_hint
    body = "From: John Smith <john@example.com>\n\nI cannot bind my car."
    res = agent.handle_email(body, sender_hint="John Smith <john@example.com>")
    assert res.draft.body_md.startswith("Dear Mr./Ms. Smith,"), (
        f"EmailAgent 起草后开头应被替换，实测: {res.draft.body_md[:60]!r}"
    )


def test_email_agent_draft_keeps_default_when_no_sender_info():
    """email_body / sender_hint 都没有显示名时，保持 "Dear customer,"。"""
    from agent.email_agent import EmailAgent
    from agent.azure_llm import LLMCall
    from agent.schemas import (
        DraftReply, TriageResult, RetrievalResult, ComplianceResult,
        ComplianceVerdict,
    )

    class _MockLLM:
        def invoke_json(self, *, schema, messages, reasoning_effort="medium",
                        max_completion_tokens=4096, role="generic"):
            if role == "triage":
                return TriageResult(
                    intent="app_binding", urgency="P2", region="North America",
                    language="en", requires_recall_check=False,
                    requires_warranty_lookup=False, red_flags=[],
                ), LLMCall(role="triage", model="gpt-5.4", reasoning_effort="low")
            if role == "retriever":
                from agent.schemas import CitedClause
                return RetrievalResult(
                    summary="binding issue",
                    cited_clauses=[CitedClause(
                        doc_id="na-policy-01",
                        section="NA-2026-1.1",
                        quote="QR binding standard.",
                    )],
                    evidence_score=0.9,
                ), LLMCall(role="retriever", model="gpt-5.4", reasoning_effort="low")
            if role == "drafting":
                return DraftReply(
                    subject="Re: binding",
                    body_md="Dear customer,\n\nWe will help.",
                    cited_clause_ids=["na-policy-01"],
                    next_step="re-scan QR",
                ), LLMCall(role="drafting", model="gpt-5.4", reasoning_effort="low")
            if role == "compliance":
                return ComplianceResult(
                    verdict=ComplianceVerdict(**{"pass": True, "violations": []}),
                    suggested_reply_adjustments=[],
                    sla_commitment="",
                    human_review_required=False,
                ), LLMCall(role="compliance", model="gpt-5.4", reasoning_effort="low")
            raise RuntimeError(f"unexpected role: {role}")

    agent = EmailAgent(llm=_MockLLM())
    body = "I cannot bind my car, no name visible in body"  # 无 From: 头
    res = agent.handle_email(body, sender_hint=None)
    assert res.draft.body_md.startswith("Dear customer,"), (
        f"无 sender 信息时保留默认，实测: {res.draft.body_md[:60]!r}"
    )


# ── 8. 入口 ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
