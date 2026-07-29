"""
test_m1_safety_hardening.py — M1 P0 安全收口的回归测试。

涵盖本轮 4 个改动：
  1. PII 脱敏 fail-closed
     - ImportError 抛 PIIRedactionError（不静默返回原 mail）
     - redact 内部失败抛 PIIRedactionError
  2. Region gate 共享函数
     - email_agent.region_gate_should_escalate 在 NA / Europe / Asia /
       South America / Africa / Oceania 一致
     - bypass env var 可绕过
     - outlook_drafts.should_skip 与 email_agent 同 region 时返回 None
  3. mock_rag 默认 region 必须用全名 "North America"
     - 不传 region 调用 search() 仍能命中 NA chunks
     - 显式传 "NA" 缩写时 0 命中（按 filter `!=` 设计）
  4. process_mail 捕获 PIIRedactionError 转人工
     - redact 抛异常时，process_mail 返回 dict 含 "skipped=pii_redact_failed"
     - 不调 LLM（mock 检测 invoke_json 是否被调）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── 1. PII 脱敏 fail-closed ────────────────────────────────────────────

def test_pii_redact_raises_on_import_error():
    """scripts.redact_ledger 不可用时，_redact_mail 必须抛 PIIRedactionError。"""
    from agent.outlook_drafts import _redact_mail, Mail, PIIRedactionError
    mail = Mail(id="x", subject="s", sender="a@b.com",
                received_at="2026-01-01", body_text="VIN 1HGBH41JXMN109186",
                is_read=False, attachments=[])

    # 把 scripts 路径藏起来，让 import 失败
    with patch.dict(sys.modules, {"scripts.redact_ledger": None}):
        try:
            raised = False
            try:
                _redact_mail(mail)
            except PIIRedactionError as e:
                raised = True
                assert "fail-closed" in str(e).lower() or "不可用" in str(e)
            assert raised, "ImportError 时必须抛 PIIRedactionError，不能降级返回原 mail"
        finally:
            sys.modules.pop("scripts.redact_ledger", None)


def test_pii_redact_raises_when_redact_text_throws():
    """redact_text 抛异常时，_redact_mail 必须抛 PIIRedactionError，绝不返回原 mail。"""
    from agent.outlook_drafts import _redact_mail, Mail, PIIRedactionError

    mail = Mail(id="x", subject="s", sender="a@b.com",
                received_at="2026-01-01", body_text="hello", is_read=False,
                attachments=[])

    fake_module = MagicMock()
    fake_module.redact_text.side_effect = RuntimeError("downstream fail")
    with patch.dict(sys.modules, {"scripts.redact_ledger": fake_module}):
        try:
            raised = False
            try:
                _redact_mail(mail)
            except PIIRedactionError as e:
                raised = True
                assert "downstream fail" in str(e)
            assert raised, "redact_text 抛异常时必须抛 PIIRedactionError"
        finally:
            sys.modules.pop("scripts.redact_ledger", None)


def test_process_mail_routes_to_human_when_redact_fails():
    """process_mail 在 redact 不可用时返回 skipped=pii_redact_failed，不调 LLM。"""
    from agent.outlook_drafts import process_mail, Mail, GraphClient

    mail = Mail(id="x", subject="s", sender="customer@example.com",
                received_at="2026-01-01", body_text="VIN 1HGBH41JXMN109186",
                is_read=False, attachments=[])

    llm = MagicMock()
    g = MagicMock(spec=GraphClient)
    # self-mail 跳过的 domain 必须不含 example.com
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("M1_BYPASS_REGION_GATE", None)
        with patch.dict(sys.modules, {"scripts.redact_ledger": None}):
            try:
                result = process_mail(
                    mail, g, llm=llm, templates=[], allowlist=None,
                    dry_run=True, use_llm=True, router=None,
                )
            finally:
                sys.modules.pop("scripts.redact_ledger", None)

    assert result["skipped"] == "pii_redact_failed", result
    assert "fail-closed" in result["skip_reason_detail"].lower() or "不可用" in result["skip_reason_detail"]
    # 关键断言：LLM 不能被调用
    assert not llm.invoke_json.called, (
        f"PII redact 失败时 LLM 不应被调用，但 invoke_json 被调: "
        f"{llm.invoke_json.call_args_list}"
    )


# ── 2. Region gate 共享函数 ────────────────────────────────────────────

def test_region_gate_escalate_6_regions():
    """M2 region_gate_should_escalate 走细路由：
    M2 supported regions (NA / Europe / UK / Oceania) → 不升；
    Asia / South America / Africa → 升（未接入合规规则）。
    """
    from agent.email_agent import region_gate_should_escalate
    from agent.schemas import TriageResult

    def make_triage(region: str, country: str | None = None) -> TriageResult:
        return TriageResult(
            intent="other", urgency="P2", region=region, language="en",
            country=country,
            requires_recall_check=False, requires_warranty_lookup=False,
            red_flags=[],
        )

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("M1_BYPASS_REGION_GATE", None)
        os.environ.pop("M2_ROLLBACK_TO_M1", None)
        # M2 放行集合
        for r in ("North America", "Europe", "Oceania"):
            assert region_gate_should_escalate(make_triage(r)) is False, r
        # 通过 country=GB 走 UK
        assert region_gate_should_escalate(make_triage("Europe", country="GB")) is False
        assert region_gate_should_escalate(make_triage("Europe", country="United Kingdom")) is False
        # 仍升人工
        for r in ("Asia", "South America", "Africa"):
            assert region_gate_should_escalate(make_triage(r)) is True, r


def test_region_gate_bypass_env_var():
    """M1_BYPASS_REGION_GATE=1 时非 NA 仍放行。"""
    from agent.email_agent import region_gate_should_escalate
    from agent.schemas import TriageResult

    triage = TriageResult(
        intent="other", urgency="P2", region="Europe", language="en",
        requires_recall_check=False, requires_warranty_lookup=False,
        red_flags=[],
    )
    with patch.dict(os.environ, {"M1_BYPASS_REGION_GATE": "1"}):
        assert region_gate_should_escalate(triage) is False


def test_outlook_should_skip_uses_shared_region_gate():
    """outlook_drafts.should_skip 与 email_agent.region_gate_should_escalate
    共享判断；M2 supported region → 都不跳；未支持 region → 都跳。
    """
    from agent.outlook_drafts import should_skip
    from agent.schemas import TriageResult

    base = {
        "intent": "app_binding", "urgency": "P2", "language": "en",
        "requires_recall_check": False, "requires_warranty_lookup": False,
        "red_flags": [],
    }

    mail = MagicMock()  # 不需要真实 Mail
    tmpl = {"intent": "app_binding", "template": "x"}

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("M1_BYPASS_REGION_GATE", None)
        os.environ.pop("M2_ROLLBACK_TO_M1", None)
        # M2 放行
        for r in ("North America", "Europe", "Oceania"):
            assert should_skip(mail, {**base, "region": r}, tmpl, None) is None, r
        # 未支持 region 跳
        skip_reason = should_skip(mail, {**base, "region": "Asia"}, tmpl, None)
        assert skip_reason is not None
        assert "region" in skip_reason.lower() and "asia" in skip_reason.lower()


# ── 3. mock_rag 默认 region 全名 ───────────────────────────────────────

def test_mock_rag_default_region_uses_full_name():
    """MockKnowledgeBase.search() 不传 region 时，默认必须是 "North America"。"""
    from agent.tools.mock_rag import MockKnowledgeBase

    kb = MockKnowledgeBase()
    import inspect
    sig = inspect.signature(kb.search)
    assert sig.parameters["region"].default == "North America", (
        f"mock_rag 默认 region 应为 'North America'，实际 {sig.parameters['region'].default!r}"
    )


def test_mock_rag_default_query_hits_na_chunks():
    """不显式传 region 时，搜 "binding" 仍能命中 NA chunks。"""
    from agent.tools.mock_rag import MockKnowledgeBase

    kb = MockKnowledgeBase()
    hits = kb.search("binding QR not displaying")
    # 命中应包含 NA 标记的 chunk
    na_hits = [h for h in hits if h["metadata"].get("region") == "North America"]
    assert len(na_hits) > 0, f"默认 region 下 0 命中 NA chunks，hits={hits}"


def test_mock_rag_m25_loads_cs_chunks_for_eu_uk_oc():
    """M2.5: CS chunks 加载后，EU/UK/OC region 应能命中真实客服回复。"""
    from agent.tools.mock_rag import MockKnowledgeBase, _load_cs_chunks, _CS_CHUNKS_PATH

    cs = _load_cs_chunks()
    if not _CS_CHUNKS_PATH.exists():
        # 缺 cs_chunks.jsonl 是 silent fallback；测试不强依赖
        import pytest
        pytest.skip("cs_chunks.jsonl 不存在，跳过（build_cs_chunks.py 生成）")
    assert len(cs) > 0, "CS chunks 应该有数"

    # 至少有 Europe / United Kingdom / Oceania 三区
    regions = {c.metadata.get("region") for c in cs}
    for region in ("Europe", "United Kingdom", "Oceania"):
        assert region in regions, f"CS chunks 缺 region={region}; 实际 regions={regions}"

    # 真检索：搜 "binding" 在 Europe region 必须能命中 app_binding 案例
    kb = MockKnowledgeBase()
    hits = kb.search("bind QR code previous owner", region="Europe", top_k=5)
    assert len(hits) > 0, "Europe 搜 binding 应返回 ≥1 hit"
    cs_hits = [h for h in hits if h["metadata"].get("type") == "cs_reply"]
    assert len(cs_hits) > 0, (
        f"Europe 搜 binding 应返回 ≥1 条 CS 类型 chunk；hits={[(h['doc_id'], h['metadata'].get('type')) for h in hits]}"
    )


# ── 4. M2: region gate 两阶段 + EU/UK/OC 合规 picker ────────────────


def test_m2_region_gate_red_flag_escalates_anywhere():
    """M2 阶段 1：red_flags 命中 → 任意 region 都升人工。"""
    from agent.email_agent import region_gate_should_escalate
    from agent.schemas import TriageResult

    for region in ("North America", "Europe", "Oceania"):
        triage = TriageResult(
            intent="complaint", urgency="P0", region=region, language="en",
            red_flags=["lawyer_letter"],
            requires_recall_check=False, requires_warranty_lookup=False,
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("M2_ROLLBACK_TO_M1", None)
            assert region_gate_should_escalate(triage) is True, (
                f"region={region} 红牌应升人工"
            )


def test_m2_region_gate_gdpr_body_keyword_escalates():
    """M2 阶段 1：body 命中 GDPR 删除/数据保护请求 → 升人工（M2 supported region 也拦）。"""
    from agent.email_agent import region_gate_should_escalate
    from agent.schemas import TriageResult

    bodies = [
        "I would like to exercise my right to be forgotten under GDPR.",
        "Please treat this as a data deletion request for my account.",
        "This is a GDPR subject access request.",
        "Contact: dpo@mycompany.com — formal supervisory authority complaint.",
        "I am concerned about cross-border transfer of my telemetry data.",
    ]
    for body in bodies:
        triage = TriageResult(
            intent="ismart_privacy", urgency="P2", region="Europe", language="en",
            red_flags=[],
            requires_recall_check=False, requires_warranty_lookup=False,
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("M2_ROLLBACK_TO_M1", None)
            assert region_gate_should_escalate(triage, email_body=body) is True, (
                f"body 应触发阶段 1 升人工: {body!r}"
            )


def test_m2_rollback_env_var_restores_na_only():
    """M2_ROLLBACK_TO_M1=1 应急回滚：仅 NA 放行。"""
    from agent.email_agent import region_gate_should_escalate
    from agent.schemas import TriageResult

    def make_triage(region: str) -> TriageResult:
        return TriageResult(
            intent="other", urgency="P2", region=region, language="en",
            requires_recall_check=False, requires_warranty_lookup=False, red_flags=[],
        )

    with patch.dict(os.environ, {"M2_ROLLBACK_TO_M1": "1"}, clear=False):
        assert region_gate_should_escalate(make_triage("North America")) is False
        assert region_gate_should_escalate(make_triage("Europe")) is True
        assert region_gate_should_escalate(make_triage("Oceania")) is True


def test_pick_compliance_constraints_per_region():
    """pick_compliance_constraints / build_compliance_system 给出 region 特定规则。"""
    from agent.prompts import build_compliance_system, pick_compliance_constraints
    from agent.schemas import TriageResult

    cases = [
        # (region,    country,   marker_present_in_role_or_constraints)
        ("North America",  None, "North America"),
        ("Europe",         "FR",  "GDPR"),
        ("Europe",         "GB",  "UK GDPR"),
        ("Europe",         "DE",  "GDPR"),
        ("Oceania",        "AU",  "Privacy Act 1988"),
    ]
    for region, country, marker in cases:
        triage = TriageResult(
            intent="app_binding", urgency="P2", region=region, language="en",
            country=country,
            requires_recall_check=False, requires_warranty_lookup=False, red_flags=[],
        )
        sys_prompt = build_compliance_system(region, country)
        assert marker in sys_prompt, (
            f"region={region} country={country} system prompt 应含 {marker!r}\n"
            f"前 500 字: {sys_prompt[:500]}"
        )
        # 也试 pick_compliance_constraints 不抛
        role, _ = pick_compliance_constraints(region, country)
        assert isinstance(role, str) and len(role) > 0


def test_build_compliance_system_rejects_unm2_region():
    """M2 picker 不支持 Asia/Africa/South America → raise。"""
    from agent.prompts import pick_compliance_constraints

    for r in ("Asia", "Africa", "South America"):
        with pytest.raises(ValueError, match="not in M2 picker"):
            pick_compliance_constraints(r, None)


def test_m2_compliance_step_uses_region_picker():
    """_compliance_step 调 build_compliance_system → system prompt 含 region 规则。"""
    from unittest.mock import MagicMock, patch
    from agent.email_agent import EmailAgent
    from agent.schemas import DraftReply, TriageResult, ComplianceResult, ComplianceVerdict

    # 用一个假 LLM client，只调 invoke_json 一次并从 system 抓 region 用的规则
    seen_system_prompt = []

    class FakeLLM:
        def invoke_json(self, *, schema, messages, reasoning_effort, role,
                        max_completion_tokens=None):
            seen_system_prompt.append(messages[0]["content"])
            # 返一个 minimal ComplianceResult（用 alias "pass" 序列化名）
            return ComplianceResult.model_validate({
                "verdict": {"pass": True, "violations": []},
                "sla_commitment": "3 business days",
                "human_review_required": False,
                "suggested_reply_adjustments": [],
            }), MagicMock(model="fake", usage={}, reasoning_effort=reasoning_effort)

    agent = EmailAgent(llm=FakeLLM())
    draft = DraftReply(
        subject="Re: hi",
        body_md="hello",
        cited_clause_ids=["x"],
        next_step="ok",
    )
    for region, country, marker in [
        ("North America",  None, "North America"),
        ("Europe",         "DE",  "GDPR"),
        ("Europe",         "GB",  "UK GDPR"),
        ("Oceania",        "AU",  "Privacy Act 1988"),
    ]:
        seen_system_prompt.clear()
        triage = TriageResult(
            intent="app_binding", urgency="P2", region=region, language="en",
            country=country,
            requires_recall_check=False, requires_warranty_lookup=False, red_flags=[],
        )
        agent._compliance_step(draft, triage)
        assert marker in seen_system_prompt[0], (
            f"region={region} country={country} → system prompt 缺 {marker!r}"
        )


# ── 4.5 M2.5: v4 templates 自动接入 ────────────────────────────


def test_v4_templates_load_via_template_reply():
    """M2.5: v4 xlsx 自动被 template_reply.load_xlsx() 加载（glob reverse-sort），
    同 intent + 同 template 文本由 v4 优先保留。
    """
    from pathlib import Path
    from agent.template_reply import load_xlsx

    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    v4_path = PROJECT_ROOT / "iSMART售后服务邮箱回复方法_v4_templates.xlsx"
    if not v4_path.exists():
        import pytest
        pytest.skip("v4 xlsx 还没生成；跑 scripts/v4_draft_templates.py 后再跑这条")

    templates = load_xlsx()
    v4_only = [t for t in templates if "v4:" in (t.get("description") or "")]
    assert len(v4_only) > 0, "load_xlsx 应至少加载一条 v4 模板"

    # v4 模板至少覆盖一个之前 0 模板的 Intent（ismart_connectivity）
    intent_set = {t["intent"] for t in v4_only}
    assert "ismart_connectivity" in intent_set, (
        f"v4 模板应覆盖 ismart_connectivity（之前 0 模板）；实际 intent={intent_set}"
    )

    # v4 模板的 Category 必须能 route 到 32 Intent enum
    from agent.template_reply import CATEGORY_TO_INTENT
    missing_route = []
    for t in v4_only:
        cat = t["category"]
        if not CATEGORY_TO_INTENT.get(cat):
            missing_route.append(cat)
    assert not missing_route, (
        f"v4 模板里有 Category 没在 CATEGORY_TO_INTENT 里：{missing_route}"
    )


# ── 5. entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
