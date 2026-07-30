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


# ── M2.5.1: xlsx 第 11 列「禁止/注意表述」端到端 ─────────────────────────

def test_xlsx_forbidden_field_loaded_into_template_doc():
    """M2.5.1: load_xlsx() 应把 xlsx 第 11 列读进 row['forbidden']。
    94/95 行非空。
    """
    from agent.template_reply import load_xlsx
    templates = load_xlsx()
    assert templates, "load_xlsx() 应至少加载一条模板"

    # 至少有一些模板带 forbidden 字段
    with_forbidden = [t for t in templates if (t.get("forbidden") or "").strip()]
    assert len(with_forbidden) >= 50, (
        f"应至少 50 条模板带 forbidden 字段；实际 {len(with_forbidden)}"
    )

    # forbidden 字段是中文「不要 X」自由文本，不是空字符串
    sample = with_forbidden[0]
    fb = sample["forbidden"]
    assert "不要" in fb or "不能" in fb or "不可" in fb, (
        f"forbidden 字段应该是中文「不要 X」自由文本；got: {fb!r}"
    )


def test_template_body_never_contains_forbidden_phrase():
    """M2.5.1: 模板作者写模板时已经避开了 forbidden 短语（93/93 验证）——
    这是禁则字段的语义保证：禁则是给模板作者的边界提示，不是运行时校验信号。
    """
    from agent.template_reply import load_xlsx

    templates = load_xlsx()
    hit_count = 0
    for t in templates:
        fb = (t.get("forbidden") or "").strip()
        body = t.get("template", "")
        if not fb or not body:
            continue
        # 简单去否定词子串匹配：检查 body 是否含 forbidden 里「不要 X」抽出 X 的子串
        # 实际抽词算法复杂，这里用宽口径检查：body 不应含「要求客户」「密码」「承诺」
        # 等明显禁则触发词
        trigger_phrases = [
            "please send your password", "please provide your password",
            "we will refund", "we will replace", "by tomorrow",
            "within 24 hours", "within 48 hours",
            "your warranty is valid", "your warranty is expired",
            "this is not iSMART",
        ]
        for phrase in trigger_phrases:
            if phrase.lower() in body.lower():
                hit_count += 1
                print(f"  WARNING: tmpl={t.get('template_id')} contains '{phrase}'")
                break
    assert hit_count == 0, (
        f"模板正文不应含已知禁则触发短语；发现 {hit_count} 条"
    )


def test_run_template_reply_returns_template_forbidden():
    """M2.5.1: run_template_reply() 返回 dict 含 template_forbidden (str)。
    forbidden_hit 已移除 —— 模板正文是 xlsx 写死的，运行时不会触发禁则。
    """
    from agent.template_reply import run_template_reply

    result = run_template_reply(
        "I cannot bind my car to the app. The QR code does not show.",
        use_llm=False,        # 走 _keyword_classify → 不调 Azure
        templates=None,        # 触发 load_xlsx() 默认路径
    )

    assert "template_forbidden" in result, (
        "run_template_reply 返回值应含 template_forbidden"
    )
    assert "forbidden_hit" not in result, (
        "forbidden_hit 已移除（post-filter 不做运行时校验）"
    )
    assert isinstance(result["template_forbidden"], str)


def test_email_agent_loads_template_forbidden_by_intent():
    """M2.5.1: EmailAgent._load_template_forbidden(intent) 应按 intent 挑模板。
    不调 Azure LLM（不构造完整 handle_email 链路）。
    """
    from agent.email_agent import EmailAgent

    # 用一个 stub llm（不调 Azure）
    class _StubLLM:
        pass

    agent = EmailAgent(llm=_StubLLM())  # type: ignore[arg-type]

    # 找一个真有模板的 intent（xlsx 第 11 列 94/95 非空）
    fb = agent._load_template_forbidden("app_binding")
    if fb is None:
        # app_binding 在 v2/v4 旧表才有；如果被新表覆盖后无 forbidden，跳过
        import pytest
        pytest.skip("app_binding intent 当前无 forbidden 字段（xlsx 覆盖差异）")

    assert isinstance(fb, str) and fb.strip(), (
        f"_load_template_forbidden 应返回非空字符串；got {fb!r}"
    )
    assert "不要" in fb or "不能" in fb or "不可" in fb, (
        f"forbidden 原文应是中文「不要 X」风格；got {fb!r}"
    )


def test_extract_action_phrases_strips_negation():
    """M2.5.1: _extract_action_phrases() 应剥掉「不要/不能/不可」等否定词前缀。
    """
    from agent.template_reply import _extract_action_phrases

    cases = [
        ("不要要求客户提供任何密码",          ["要求客户提供任何密码"]),
        ("不要用车牌替代 VIN 做最终判断",     ["用车牌替代 VIN 做最终判断"]),
        ("不能承诺具体解决日期",              ["承诺具体解决日期"]),
        ("不要 X；also 不要 Y",              ["also Y"]),
        ("", []),
    ]
    for raw, expected in cases:
        out = _extract_action_phrases(raw)
        assert out == expected, f"raw={raw!r} → expected {expected} got {out}"


def test_compliance_step_checks_template_forbidden_violation():
    """M2.5.1: _compliance_step() 对 LLM 起草正文做 template_forbidden post-check。

    已知设计局限：
      - xlsx forbidden 是中文「不要 X」格式
      - LLM draft body 必为英文（CLAUDE.md 硬约束）
      - 子串匹配无法捕获英文译法
      - post-check 仅对**中文 hallucination** / **中英混合 prompt injection** 生效

    本测试断言这条局限：用真实场景（中文禁则 + 英文 draft），post-check 应**不**命中。
    防止未来误把这条防线当成强保证。
    """
    from dataclasses import dataclass
    from agent.email_agent import EmailAgent
    from agent.azure_llm import LLMCall
    from agent.schemas import (
        TriageResult, RetrievalResult, DraftReply, ComplianceResult, CitedClause,
    )

    @dataclass
    class Usage:
        prompt_tokens: int = 0; completion_tokens: int = 0
        cached_tokens: int = 0; reasoning_tokens: int = 0
        @property
        def cache_hit_rate(self): return 0.5

    class StubLLM:
        def invoke_json(self, *, schema, messages, reasoning_effort, role, **kwargs):
            if role == "triage":
                r = TriageResult(intent="app_binding", urgency="P2",
                    region="North America", language="en",
                    requires_recall_check=False, requires_warranty_lookup=False,
                    red_flags=[], auto_reply_level="auto_draft_safe",
                    required_info_group=[])
            elif role == "retriever":
                r = RetrievalResult(summary="x", evidence_score=0.8,
                    cited_clauses=[CitedClause(doc_id="d1", section="s",
                        quote="contact authorized service center")])
            elif role == "drafting":
                # 英文 draft 触发禁则的中文译法 —— post-check 应**不**命中
                r = DraftReply(subject="Re: bind", body_md=(
                    "Dear customer,\n\n"
                    "We will help. Please ask the customer to provide their password "
                    "and we will reset it for you.\n\nBest regards"
                ), cited_clause_ids=[], next_step="share password")
            elif role == "compliance":
                r = ComplianceResult(sla_commitment="",
                    verdict={"pass": True, "violations": []},
                    suggested_reply_adjustments=[], human_review_required=False)
            return (r, LLMCall(role=role, model="stub",
                               reasoning_effort=reasoning_effort, usage=Usage()))

    agent = EmailAgent(llm=StubLLM(), max_rewrite_attempts=0)
    # app_binding 的 forbidden = 「不要要求客户提供任何密码」（中文）
    # LLM draft 是英文 → post-check 不命中 → 整体通过
    result = agent.handle_email("I cannot bind my car.")
    # 应**不**被 escalate；handle_email 正常返回
    viols = result.compliance.verdict.violations
    tf_violations = [v for v in viols if "template_forbidden" in v]
    assert not tf_violations, (
        f"中英不匹配场景下 post-check 不应命中；got violations={tf_violations}"
    )


def test_compliance_step_catches_chinese_hallucination():
    """M2.5.1: 当 LLM 在 draft 里 hallucinate 出中文禁则短语时（极低概率场景，
    主要是 prompt injection / 模型异常），post-check 应能捕获。
    """
    from dataclasses import dataclass
    from agent.email_agent import EmailAgent, NeedsHumanEscalation
    from agent.azure_llm import LLMCall
    from agent.schemas import (
        TriageResult, RetrievalResult, DraftReply, ComplianceResult, CitedClause,
    )

    @dataclass
    class Usage:
        prompt_tokens: int = 0; completion_tokens: int = 0
        cached_tokens: int = 0; reasoning_tokens: int = 0
        @property
        def cache_hit_rate(self): return 0.5

    class StubLLM:
        def invoke_json(self, *, schema, messages, reasoning_effort, role, **kwargs):
            if role == "triage":
                r = TriageResult(intent="app_binding", urgency="P2",
                    region="North America", language="en",
                    requires_recall_check=False, requires_warranty_lookup=False,
                    red_flags=[], auto_reply_level="auto_draft_safe",
                    required_info_group=[])
            elif role == "retriever":
                r = RetrievalResult(summary="x", evidence_score=0.8,
                    cited_clauses=[CitedClause(doc_id="d1", section="s",
                        quote="contact authorized service center")])
            elif role == "drafting":
                # draft 里塞中文禁则触发短语 —— 模拟 LLM hallucination / prompt injection
                r = DraftReply(subject="Re: bind", body_md=(
                    "Dear customer,\n\n我们已记录。我们需要您要求客户提供任何密码 "
                    "才能继续处理。\n\nBest regards"
                ), cited_clause_ids=[], next_step="share info")
            elif role == "compliance":
                # compliance LLM 直接 pass —— 靠 template_forbidden post-check 捕获
                r = ComplianceResult(sla_commitment="",
                    verdict={"pass": True, "violations": []},
                    suggested_reply_adjustments=[], human_review_required=False)
            return (r, LLMCall(role=role, model="stub",
                               reasoning_effort=reasoning_effort, usage=Usage()))

    agent = EmailAgent(llm=StubLLM(), max_rewrite_attempts=0)
    try:
        result = agent.handle_email("I cannot bind my car.")
    except NeedsHumanEscalation as e:
        # max_rewrite_attempts=0 → 第一次 fail 直接 escalate
        assert "template_forbidden" in str(e), (
            f"escalate reason 应含 template_forbidden；got: {e}"
        )
        return

    pytest.fail(f"中文禁则触发应被 escalate；但 handle_email 返回了 {result}")


# ── 4. M2.5.4: salutation 变量位 + 进程内 dedup ──────────────────────────


def test_normalize_greeting_placeholder_replaces_dear_customer():
    """模板正文 'Dear Customer,' 运行时 normalize 成 '{{salutation}},'。"""
    from agent.template_reply import _normalize_greeting_placeholder
    assert _normalize_greeting_placeholder("Dear Customer,\nFoo") == "{{salutation}},\nFoo"
    assert _normalize_greeting_placeholder("Dear customer, rest") == "{{salutation}}, rest"
    assert _normalize_greeting_placeholder("Hi Customer,\nFoo") == "{{salutation}},\nFoo"
    assert _normalize_greeting_placeholder("Hello customer,\nX") == "{{salutation}},\nX"
    # 不在开头 → 不动
    assert _normalize_greeting_placeholder("Foo Dear Customer,") == "Foo Dear Customer,"
    # 前面只允许空行 / 空白
    assert _normalize_greeting_placeholder("  Dear Customer,\nX") == "  {{salutation}},\nX"
    # 空字符串不动
    assert _normalize_greeting_placeholder("") == ""
    # 标题里的 "Dear Customer service team"（合法短语）不动
    assert _normalize_greeting_placeholder(
        "Dear Customer service team, hello"
    ) == "Dear Customer service team, hello"


def test_load_xlsx_templates_have_salutation_placeholder():
    """load_xlsx 加载的所有新表 + 旧表 模板正文里没有字面 'Dear Customer,'
    字面开头 —— 都被 normalize 成 '{{salutation}},'。"""
    from agent.template_reply import load_xlsx
    templates = load_xlsx()
    literal_count = 0
    placeholder_count = 0
    other_count = 0
    for t in templates:
        body = t["template"]
        head = body[:50].lower()
        if head.startswith("dear customer,") or head.startswith("hi customer,") or \
           head.startswith("hello customer,"):
            literal_count += 1
        elif body.startswith("{{salutation}},"):
            placeholder_count += 1
        else:
            other_count += 1
    assert literal_count == 0, (
        f"有 {literal_count} 条模板正文仍是字面 'Dear Customer,'："
        "normalize 漏了"
    )
    # 大多数模板应该被 normalize（v4 表 1 条 "Hi Team," 不命中是预期）
    assert placeholder_count >= 200, (
        f"placeholder 只 {placeholder_count}，期待 ≥200（来自 264+ 模板）"
    )


def test_personalize_greeting_matches_salutation_placeholder():
    """personalize_greeting 识别 {{salutation}}, 占位符并替换。"""
    from agent.greeting import personalize_greeting
    body = "{{salutation}},\n\nThanks for your email.\n\nBest,\nTeam"
    out = personalize_greeting(body, "John Smith <john@x.com>")
    assert out.startswith("Dear Mr./Ms. Smith,"), out
    # 后面正文不动
    assert "Thanks for your email." in out
    assert "Best,\nTeam" in out


def test_personalize_greeting_falls_back_when_no_name():
    """M2.5.4 fix: 拿不到名字也要替换占位符（之前早返回导致 placeholder 漏网）。

    任何 _GREETING_PATTERNS 命中（包括 {{salutation}},）都必须替换成最终
    greeting（fallback 也是 "Dear customer,"）—— 绝不让 placeholder 字面量
    泄漏到 Outlook Drafts。
    """
    from agent.greeting import personalize_greeting
    body = "{{salutation}},\n\nFoo"
    # 纯邮箱无显示名 → fallback "Dear customer,"
    out = personalize_greeting(body, "noreply@x.com")
    assert out.startswith("Dear customer,"), out
    assert "Foo" in out
    # 占位词 first name → fallback
    out2 = personalize_greeting(body, "Customer <c@x.com>")
    assert out2.startswith("Dear customer,"), out2
    # 没 sender + 没 display_name → fallback
    out3 = personalize_greeting(body, "")
    assert out3.startswith("Dear customer,"), out3


def test_personalize_greeting_uses_display_name_over_sender():
    """M2.5.4 fix: display_name 优先 —— 即使 sender 是 redacted 后的
    `user_xxx@example.com`（无显示名可抽），也能拿到原始姓名做个性化。"""
    from agent.greeting import personalize_greeting
    body = "{{salutation}},\n\nFoo"
    # display_name 优先
    out = personalize_greeting(body, "user_abc12345@example.com",
                               display_name="Philip Watson")
    assert out.startswith("Dear Mr./Ms. Watson,"), out


# ── dedup detector ──────────────────────────────────────────────────────


def test_duplicate_detector_first_mail_not_duplicate():
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    m = Mail(id="m1", subject="App not working",
             sender="alice@x.com", received_at="",
             body_text="My app is broken", is_read=False)
    assert d.check_and_record(m) is None


def test_duplicate_detector_exact_subject_match():
    """同 sender + 同主题 → 100% 算重复。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    m1 = Mail(id="m1", subject="App not working", sender="alice@x.com",
              received_at="", body_text="My app is broken", is_read=False)
    m2 = Mail(id="m2", subject="App not working", sender="alice@x.com",
              received_at="", body_text="Completely different content", is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None
    assert r["match"] == "subject_exact"
    assert r["duplicate_of"] == "m1"
    assert r["score"] == 1.0


def test_duplicate_detector_subject_contains_with_re_prefix():
    """Re:/Fwd: 前缀 + 同主题主干 → 主题包含 + body jaccard 命中。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    m1 = Mail(id="m1", subject="App not working", sender="alice@x.com",
              received_at="",
              body_text="My app is broken and I cannot use it at all", is_read=False)
    m2 = Mail(id="m2", subject="Re: App not working", sender="alice@x.com",
              received_at="",
              body_text="My app is broken and I cannot use it at all because it crashes",
              is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None
    assert r["match"] in ("subject_exact", "subject_contains")
    assert r["duplicate_of"] == "m1"


def test_duplicate_detector_jaccard_only():
    """主题完全不同但 body 高度相似 → jaccard 命中。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    m1 = Mail(id="m1", subject="Charging question", sender="alice@x.com",
              received_at="",
              body_text="How do I schedule overnight charging for my MG4 vehicle",
              is_read=False)
    m2 = Mail(id="m2", subject="Overnight charging setup", sender="alice@x.com",
              received_at="",
              body_text="How do I schedule overnight charging for my MG4 vehicle please",
              is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None
    assert r["match"] == "jaccard"
    assert r["score"] >= 0.7


def test_duplicate_detector_different_sender_not_dup():
    """不同 sender 同样的 body → 不算重复（每个 sender 独立 bucket）。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    m1 = Mail(id="m1", subject="App not working", sender="alice@x.com",
              received_at="", body_text="My app is broken", is_read=False)
    m2 = Mail(id="m2", subject="App not working", sender="bob@y.com",
              received_at="", body_text="My app is broken", is_read=False)
    assert d.check_and_record(m1) is None
    assert d.check_and_record(m2) is None


def test_duplicate_detector_unrelated_mail_not_dup():
    """主题 + body 都不相关 → 不命中。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    m1 = Mail(id="m1", subject="App not working", sender="alice@x.com",
              received_at="", body_text="My app is broken", is_read=False)
    m2 = Mail(id="m2", subject="Cannot bind my car", sender="alice@x.com",
              received_at="",
              body_text="The QR code is missing on the infotainment screen",
              is_read=False)
    assert d.check_and_record(m1) is None
    assert d.check_and_record(m2) is None


def test_process_mail_skips_duplicate_before_llm():
    """process_mail 收到 dedup detector → 第二封同主题邮件走 skipped='duplicate'
    且不调 LLM（mock LLM 计数器验证）。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail, process_mail

    d = _DuplicateDetector()
    m1 = Mail(id="m1", subject="App not working",
              sender="alice@x.com", received_at="",
              body_text="My app is broken and I cannot use it", is_read=False)
    m2 = Mail(id="m2", subject="App not working",
              sender="alice@x.com", received_at="",
              body_text="My app is broken and I cannot use it still", is_read=False)

    # 第一个 mock：调一次 LLM，drafter 返回非空
    class _StubLLM:
        def __init__(self): self.calls = 0
        def invoke_json(self, *args, **kwargs):
            self.calls += 1
            from agent.schemas import TriageResult
            return TriageResult(
                intent="app_vehicle_control", urgency="P2",
                region="North America", language="en",
                requires_recall_check=False,
                requires_warranty_lookup=False, red_flags=[],
            ), None

    llm = _StubLLM()
    templates = [{"intent": "app_vehicle_control",
                  "template": "{{salutation}},\n\nThanks.\n\nBest,\niSMART Service Team",
                  "category": "App-车控车态相关", "level": "auto_draft_safe",
                  "required_info": [], "next_step": "", "human_trigger": "",
                  "forbidden": "", "source": "test"}]

    rec1 = process_mail(
        m1, g=None, llm=llm, templates=templates,
        allowlist=None, dry_run=True, use_llm=True,
        dedup=d,
    )
    rec2 = process_mail(
        m2, g=None, llm=llm, templates=templates,
        allowlist=None, dry_run=True, use_llm=True,
        dedup=d,
    )
    assert llm.calls == 1, f"第二封应短路不调 LLM；实际调了 {llm.calls} 次"
    assert rec1.get("draft_id") or rec1.get("would_post_body")
    assert rec2.get("skipped") == "duplicate"
    assert rec2.get("duplicate_of") == "m1"
    assert rec2.get("duplicate_score", 0) >= 0.7


def test_process_mail_duplicate_applies_category_via_graph():
    """重复邮件 + 真 Graph 客户端 → 调 add_category('关闭:无效邮件')，
    且原邮件不动（isRead 仍 False、不删、不移动）。"""
    from unittest.mock import MagicMock
    from agent.outlook_drafts import (
        _DuplicateDetector, Mail, process_mail, GraphClient,
    )

    # 伪造 GraphClient：只记录调用的 _post URL + body
    class _StubGraph:
        def __init__(self):
            self.calls: list[tuple[str, dict]] = []
        def add_category(self, mail_id, category):
            self.calls.append((
                f"/users/me/messages/{mail_id}/categories",
                {"categories": [category]},
            ))

    g = _StubGraph()
    d = _DuplicateDetector()
    m1 = Mail(id="m1", subject="App not working",
              sender="alice@x.com", received_at="",
              body_text="My app is broken and I cannot use it", is_read=False)
    m2 = Mail(id="m2", subject="App not working",
              sender="alice@x.com", received_at="",
              body_text="My app is broken and I cannot use it still", is_read=False)

    class _StubLLM:
        def invoke_json(self, *args, **kwargs):
            from agent.schemas import TriageResult
            return TriageResult(
                intent="app_vehicle_control", urgency="P2",
                region="North America", language="en",
                requires_recall_check=False,
                requires_warranty_lookup=False, red_flags=[],
            ), None

    templates = [{"intent": "app_vehicle_control",
                  "template": "{{salutation}},\n\nThanks.",
                  "category": "App-车控车态相关", "level": "auto_draft_safe",
                  "required_info": [], "next_step": "", "human_trigger": "",
                  "forbidden": "", "source": "test"}]

    # 第一封走完整流程（dry_run，没真 Graph 调用）
    process_mail(m1, g=g, llm=_StubLLM(), templates=templates,
                 allowlist=None, dry_run=True, use_llm=True, dedup=d)
    # 第二封：重复 → 应调 add_category('关闭:无效邮件')
    rec2 = process_mail(m2, g=g, llm=_StubLLM(), templates=templates,
                        allowlist=None, dry_run=True, use_llm=True, dedup=d)

    assert rec2["skipped"] == "duplicate"
    assert rec2["category_applied"] is True
    assert rec2["category_error"] is None
    assert len(g.calls) == 1
    url, body = g.calls[0]
    assert url == "/users/me/messages/m2/categories"
    assert body == {"categories": ["关闭:无效邮件"]}


def test_process_mail_duplicate_category_failure_captured_not_raised():
    """Graph add_category 抛错时 process_mail 不传播，只在 record 里写
    category_error 给后续审计。LLM 仍不调。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail, process_mail

    class _FailingGraph:
        def add_category(self, mail_id, category):
            raise RuntimeError("simulated Graph 503")

    g = _FailingGraph()
    d = _DuplicateDetector()
    m1 = Mail(id="m1", subject="App not working",
              sender="alice@x.com", received_at="",
              body_text="My app is broken and I cannot use it", is_read=False)
    m2 = Mail(id="m2", subject="App not working",
              sender="alice@x.com", received_at="",
              body_text="My app is broken and I cannot use it still", is_read=False)

    class _StubLLM:
        def __init__(self): self.calls = 0
        def invoke_json(self, *args, **kwargs):
            self.calls += 1
            from agent.schemas import TriageResult
            return TriageResult(
                intent="app_vehicle_control", urgency="P2",
                region="North America", language="en",
                requires_recall_check=False,
                requires_warranty_lookup=False, red_flags=[],
            ), None

    llm = _StubLLM()
    templates = [{"intent": "app_vehicle_control",
                  "template": "{{salutation}},\n\nThanks.",
                  "category": "App-车控车态相关", "level": "auto_draft_safe",
                  "required_info": [], "next_step": "", "human_trigger": "",
                  "forbidden": "", "source": "test"}]

    process_mail(m1, g=g, llm=llm, templates=templates,
                 allowlist=None, dry_run=True, use_llm=True, dedup=d)
    rec2 = process_mail(m2, g=g, llm=llm, templates=templates,
                        allowlist=None, dry_run=True, use_llm=True, dedup=d)

    assert rec2["skipped"] == "duplicate"
    assert rec2["category_applied"] is False
    assert "simulated Graph 503" in (rec2["category_error"] or "")
    assert llm.calls == 1, "category 失败也不应触发第二封 LLM"


# ── M2.5.4: list_filtered 拉邮件参数 ──────────────────────────────────


def test_list_filtered_builds_odata_filter_with_since_until():
    """list_filtered(since, until, is_read=None, exclude_categories=['关闭:无效邮件'])
    应构造完整 OData $filter。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value.status_code = 200
    g.session.request.return_value.text = '{"value": []}'
    g.session.request.return_value.raise_for_status = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()

    g.list_filtered(
        top=20, since="2026-07-01T00:00:00Z", until="2026-07-31T00:00:00Z",
        is_read=None, exclude_categories=["关闭:无效邮件"], order="asc",
    )

    # 验证构造的 $filter 包含所有条件（在 params 里，不在 path 里）
    call = g.session.request.call_args
    params = call.kwargs.get("params") or {}
    filt = params.get("$filter", "")
    orderby = params.get("$orderby", "")
    assert "receivedDateTime ge 2026-07-01" in filt
    assert "receivedDateTime lt 2026-07-31" in filt
    assert "categories/any" in filt
    assert "关闭" in filt
    assert orderby == "receivedDateTime asc"


def test_list_filtered_is_read_true_keeps_only_read():
    """is_read=True → $filter 含 'isRead eq true'。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value.status_code = 200
    g.session.request.return_value.text = '{"value": []}'
    g.session.request.return_value.raise_for_status = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()

    g.list_filtered(top=10, is_read=True, order="desc")

    call = g.session.request.call_args
    params = call.kwargs.get("params") or {}
    assert "isRead eq true" in params.get("$filter", "")


def test_list_filtered_is_read_false_keeps_only_unread():
    """is_read=False → $filter 含 'isRead eq false'（同旧 list_unread 行为）。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value.status_code = 200
    g.session.request.return_value.text = '{"value": []}'
    g.session.request.return_value.raise_for_status = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()

    g.list_filtered(top=10, is_read=False, order="desc")

    call = g.session.request.call_args
    params = call.kwargs.get("params") or {}
    assert "isRead eq false" in params.get("$filter", "")


def test_list_filtered_exclude_categories_multiple():
    """多个 exclude_categories → 多个 not categories/any(c: c eq ...) 子句 AND 起来。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value.status_code = 200
    g.session.request.return_value.text = '{"value": []}'
    g.session.request.return_value.raise_for_status = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()

    g.list_filtered(
        top=10, is_read=False,
        exclude_categories=["关闭:无效邮件", "已处理"],
        order="asc",
    )
    call = g.session.request.call_args
    params = call.kwargs.get("params") or {}
    filt = params.get("$filter", "")
    assert "关闭" in filt
    assert "已处理" in filt
    assert filt.count(" and ") >= 2, f"应至少 2 个 and；got: {filt}"


def test_list_unread_delegates_to_list_filtered():
    """list_unread() 默认 is_read=False，向后兼容。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value.status_code = 200
    g.session.request.return_value.text = '{"value": []}'
    g.session.request.return_value.raise_for_status = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()

    g.list_unread(top=10)

    call = g.session.request.call_args
    params = call.kwargs.get("params") or {}
    filt = params.get("$filter", "")
    orderby = params.get("$orderby", "")
    assert "isRead eq false" in filt
    assert orderby == "receivedDateTime desc"


# ── add_category 安全 guard（M2.5.4 PATCH 原邮件 + 专项守门）─────────


def test_add_category_rejects_empty_string():
    """category 为空 → ValueError（不允许空 category 创建空标签）。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value.status_code = 200
    g.session.request.return_value.raise_for_status = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()
    try:
        g.add_category("m1", "")
    except ValueError as e:
        assert "不能为空" in str(e)
        return
    raise AssertionError("空 category 应 raise ValueError")


def test_add_category_rejects_comma_separated():
    """category 含 `,` → ValueError（避免 Graph 集合解析错位）。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value.status_code = 200
    g.session.request.return_value.raise_for_status = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()
    try:
        g.add_category("m1", "关闭:无效邮件, foo")
    except ValueError as e:
        assert "逗号" in str(e)
        return
    raise AssertionError("含逗号 category 应 raise ValueError")


def test_add_category_uses_patch_not_post():
    """add_category 走 PATCH 原邮件（不是 POST /categories，那个 405）。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value.status_code = 200
    g.session.request.return_value.raise_for_status = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()

    g.add_category("fake-id", "关闭:无效邮件")

    call = g.session.request.call_args
    method, url = call.args[0], call.args[1]
    assert method == "PATCH"
    # _patch 内部把 path 拼成完整 URL；检查路径末尾 + 拼接
    assert url.endswith("/users/me/messages/fake-id"), url
    assert call.kwargs["json"] == {"categories": ["关闭:无效邮件"]}


def test_add_category_passes_guard_even_when_not_in_draft_registry():
    """add_category 走 PATCH 原邮件（不在 _created_draft_ids 注册表）→ 专项
    守门放过；body 只含 categories → 不 raise。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value.status_code = 200
    g.session.request.return_value.raise_for_status = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()
    g.add_category("original-mail-not-in-registry", "关闭:无效邮件")
    # 不应 raise


# ── PATCH 原邮件专项守门（专项检测 categories-only body）─────────────


def test_patch_original_mail_with_body_field_blocked():
    """PATCH 原邮件 + body 含 'body' key → 拒绝（不光是 isRead；所有其它 key 都拒）。"""
    from agent.outlook_drafts import GraphClient, MailboxSafetyError
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()

    try:
        g._patch(
            "/users/me/messages/original-mail-id",
            body={"categories": ["关闭:无效邮件"], "body": {"contentType": "text",
                                                            "content": "hi"}},
        )
    except MailboxSafetyError as e:
        assert "categories 字段" in str(e) or "不允许" in str(e)
        return
    raise AssertionError("PATCH 原邮件带 body 字段应被专项守门拦下")


def test_patch_original_mail_with_subject_blocked():
    """PATCH 原邮件 + body 含 'subject' → 拒绝。"""
    from agent.outlook_drafts import GraphClient, MailboxSafetyError
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()
    try:
        g._patch(
            "/users/me/messages/m1",
            body={"categories": ["x"], "subject": "new subj"},
        )
    except MailboxSafetyError:
        return
    raise AssertionError("subject 也应被拒")


def test_patch_original_mail_with_importance_blocked():
    """PATCH 原邮件 + body 含 'importance' → 拒绝。"""
    from agent.outlook_drafts import GraphClient, MailboxSafetyError
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()
    try:
        g._patch(
            "/users/me/messages/m1",
            body={"categories": ["x"], "importance": "high"},
        )
    except MailboxSafetyError:
        return
    raise AssertionError("importance 也应被拒")


def test_patch_original_mail_with_isRead_blocked():
    """PATCH 原邮件 + body 含 'isRead' → 拒绝（已有老规则 + 专项规则双重拦截）。"""
    from agent.outlook_drafts import GraphClient, MailboxSafetyError
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()
    try:
        g._patch(
            "/users/me/messages/m1",
            body={"categories": ["x"], "isRead": True},
        )
    except MailboxSafetyError:
        return
    raise AssertionError("isRead 应被拒")


def test_patch_original_mail_with_flag_blocked():
    """PATCH 原邮件 + body 含 'flag' → 拒绝（flag 改 read state 是常见泄露口）。"""
    from agent.outlook_drafts import GraphClient, MailboxSafetyError
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()
    try:
        g._patch(
            "/users/me/messages/m1",
            body={"categories": ["x"], "flag": {"flagStatus": "flagged"}},
        )
    except MailboxSafetyError:
        return
    raise AssertionError("flag 也应被拒")


def test_patch_original_mail_categories_only_allowed():
    """PATCH 原邮件 + body 只有 categories → 放行。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value.status_code = 200
    g.session.request.return_value.raise_for_status = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()

    g._patch(
        "/users/me/messages/m1",
        body={"categories": ["关闭:无效邮件"]},
    )
    # 不 raise


def test_patch_draft_still_uses_draft_registry():
    """PATCH draft 路径仍走 _created_draft_ids 注册表 —— 不被专项守门吞。"""
    from agent.outlook_drafts import GraphClient, MailboxSafetyError
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = {"my-draft-id"}   # 只这一个 draft 在注册表

    # 1) 注册表内的 draft id → 放行（不需 body 形状校验）
    g.session.request.return_value.status_code = 200
    g.session.request.return_value.raise_for_status = MagicMock()
    g._patch(
        "/users/me/messages/my-draft-id",
        body={"body": {"contentType": "text", "content": "hi"}},
    )
    # 不 raise

    # 2) 不在注册表的 id → raise（老逻辑）
    g2 = GraphClient.__new__(GraphClient)
    g2.session = MagicMock()
    g2.user_id = "me"
    g2._created_draft_ids = set()
    try:
        g2._patch(
            "/users/me/messages/some-other-id",
            body={"body": {"contentType": "text", "content": "hi"}},
        )
    except MailboxSafetyError:
        return
    raise AssertionError("非注册表 draft id 应被老 guard 拦")


# ── M2.5.5: CHG-SCHED-001 新模板 + pick_template body overlap ───────────


def test_chg_sched_001_loaded_with_correct_intent():
    """xlsx 里的 CHG-SCHED-001 加载后 intent=charging, salutation 已 normalize,
    forbidden 字段保留（供 _compliance_step post-check 用）。"""
    from agent.template_reply import load_xlsx
    templates = load_xlsx()
    chg_sched = [t for t in templates if t.get("template_id") == "CHG-SCHED-001"]
    assert len(chg_sched) == 1, f"CHG-SCHED-001 应 1 条；实际 {len(chg_sched)}"
    t = chg_sched[0]
    assert t["intent"] == "charging", f"intent 应 charging；实际 {t['intent']}"
    assert t["template"].startswith("{{salutation}},"), (
        f"salutation 应被 normalize 成 {{salutation}},；实际开头 {t['template'][:50]!r}"
    )
    assert "iSMART Service Team" in t["template"]
    assert "scheduling" in t["template"].lower()
    assert t["forbidden"], "forbidden 字段不应为空"


def test_pick_template_charging_picks_scheduling_template_by_overlap():
    """charging intent + 邮件 body 含 schedule / off-peak / timer → pick_template
    应按 body token 重叠分选中 CHG-SCHED-001 而非充电失败模板 CHG-001。"""
    from agent.template_reply import load_xlsx, pick_template, classify_intent
    templates = load_xlsx()
    email_body = (
        "Hello\nHow can I charge my car during off-peak hours from 1 a.m. to 6 a.m.\n"
        "I don't know how to program it, can I put\n"
        "On my electrical outlet a clock so that it starts charging at 1 am"
    )
    tmpl = pick_template("charging", templates, body_text=email_body)
    assert tmpl is not None
    assert tmpl.get("template_id") == "CHG-SCHED-001", (
        f"应选 CHG-SCHED-001；实际选 {tmpl.get('template_id')} ({tmpl['category']})"
    )


def test_pick_template_charging_falls_back_when_no_overlap():
    """charging intent + 邮件 body 不含 schedule / timer → 默认第一条候选
    （CHG-001 充电失败）。验证 overlap 评分在无命中时正常降级。"""
    from agent.template_reply import load_xlsx, pick_template
    templates = load_xlsx()
    email_body = "My car stopped charging at 80%. Range reduced after charge."
    tmpl = pick_template("charging", templates, body_text=email_body)
    assert tmpl is not None
    # 不强制是哪条（取决于 token 重叠），但必须是 charging intent
    assert tmpl["intent"] == "charging"


def test_template_body_token_overlap_scores_chg_sched_highest():
    """CHG-SCHED-001 的 body token 重叠分应显著高于其他 charging 候选。"""
    from agent.template_reply import load_xlsx, _template_body_token_overlap
    templates = load_xlsx()
    candidates = [t for t in templates if t["intent"] == "charging"]
    email_body = (
        "How can I charge my car during off-peak hours from 1 a.m. to 6 a.m.\n"
        "I want to set a charging schedule."
    )
    scores = {
        t.get("template_id"): _template_body_token_overlap(t, email_body)
        for t in candidates
    }
    sched_score = scores.get("CHG-SCHED-001", 0)
    other_max = max(
        (s for tid, s in scores.items() if tid != "CHG-SCHED-001"),
        default=0,
    )
    assert sched_score > other_max, (
        f"CHG-SCHED-001 应比其它候选分数高；got {scores}"
    )
    assert sched_score >= 4, f"CHG-SCHED-001 score 应 ≥ 4；got {sched_score}"


# ── 5. entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
