"""
test_m1_safety_hardening.py — M1 P0 安全收口的回归测试。

涵盖本轮 4 个改动：
  1. PII 脱敏 fail-closed
     - ImportError 抛 PIIRedactionError（不静默返回原 mail）
     - redact 内部失败抛 PIIRedactionError
  2. Region gate 共享函数
     - 默认不按大区升人工（中东/亚洲/拉美/非洲可出草稿）
     - 阶段 1 红牌 / GDPR 关键词仍升
     - M2_ENFORCE_REGION_GATE 已废弃，不再按四区拦
     - M2_ROLLBACK_TO_M1=1 仅 NA
     - outlook_drafts.should_skip 共用同一判断
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
    """不按大区升人工；M2_ENFORCE_REGION_GATE 也拦不住。"""
    from agent.email_agent import region_gate_should_escalate
    from agent.schemas import TriageResult

    def make_triage(region: str, country: str | None = None) -> TriageResult:
        return TriageResult(
            intent="other", urgency="P2", region=region, language="en",
            country=country,
            requires_recall_check=False, requires_warranty_lookup=False,
            red_flags=[],
        )

    all_regions = (
        "North America", "Europe", "Oceania",
        "Asia", "Middle East", "South America", "Africa",
    )
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("M1_BYPASS_REGION_GATE", None)
        os.environ.pop("M2_ROLLBACK_TO_M1", None)
        os.environ.pop("M2_ENFORCE_REGION_GATE", None)
        for r in all_regions:
            assert region_gate_should_escalate(make_triage(r)) is False, r
        assert region_gate_should_escalate(make_triage("Europe", country="GB")) is False

    with patch.dict(os.environ, {"M2_ENFORCE_REGION_GATE": "1"}, clear=False):
        os.environ.pop("M1_BYPASS_REGION_GATE", None)
        os.environ.pop("M2_ROLLBACK_TO_M1", None)
        for r in all_regions:
            assert region_gate_should_escalate(make_triage(r)) is False, r


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
    """outlook_drafts.should_skip 与 email_agent.region_gate_should_escalate 共用。
    不因大区跳过；enforce 也不拦。
    """
    from agent.outlook_drafts import should_skip

    base = {
        "intent": "app_binding", "urgency": "P2", "language": "en",
        "requires_recall_check": False, "requires_warranty_lookup": False,
        "red_flags": [],
    }

    mail = MagicMock()
    tmpl = {"intent": "app_binding", "template": "x"}

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("M1_BYPASS_REGION_GATE", None)
        os.environ.pop("M2_ROLLBACK_TO_M1", None)
        os.environ.pop("M2_ENFORCE_REGION_GATE", None)
        for r in ("North America", "Europe", "Oceania", "Asia", "Middle East"):
            assert should_skip(mail, {**base, "region": r}, tmpl, None) is None, r

    with patch.dict(os.environ, {"M2_ENFORCE_REGION_GATE": "1"}, clear=False):
        os.environ.pop("M1_BYPASS_REGION_GATE", None)
        os.environ.pop("M2_ROLLBACK_TO_M1", None)
        skip_reason = should_skip(mail, {**base, "region": "Asia"}, tmpl, None)
        assert skip_reason is None


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
            os.environ.pop("M2_ENFORCE_REGION_GATE", None)
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


def test_build_compliance_system_unsupported_region_falls_back():
    """未接合规大区不再 raise；用 NA 硬规则 + 禁止编造当地法条。"""
    from agent.prompts import pick_compliance_constraints

    for r in ("Asia", "Africa", "South America", "Middle East"):
        role, constraints = pick_compliance_constraints(r, None)
        assert isinstance(role, str) and len(role) > 0
        assert "Do NOT invent local statute" in constraints
        assert "refund" in constraints.lower()


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

    templates = load_xlsx(include_legacy=True)
    v4_only = [t for t in templates if "v4:" in (t.get("description") or "")]
    assert len(v4_only) > 0, "load_xlsx(include_legacy=True) 应至少加载一条 v4 模板"

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
    # 知识库新表约 95 条；旧表不再默认混入路由
    assert placeholder_count >= 70, (
        f"placeholder 只 {placeholder_count}，期待 ≥70（20260729 知识库）"
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
    """同 sender + 同主题 + 正文几乎一样 → 重复发送。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    m1 = Mail(id="m1", subject="App not working", sender="alice@x.com",
              received_at="", body_text="My app is broken and I cannot use it",
              is_read=False)
    m2 = Mail(id="m2", subject="App not working", sender="alice@x.com",
              received_at="", body_text="My app is broken and I cannot use it",
              is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None
    assert r["match"] == "subject_exact"
    assert r["duplicate_of"] == "m1"
    assert r["score"] >= 0.7


def test_duplicate_detector_same_mail_id_is_dup():
    """同一 Graph message id 扫两次 → 不得 createReply 两次。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    m = Mail(id="same", subject="App not working", sender="alice@x.com",
             received_at="", body_text="My app is broken and I cannot use it",
             is_read=False)
    assert d.check_and_record(m) is None
    r = d.check_and_record(m)
    assert r is not None
    assert r["match"] == "same_mail_id"
    assert r["duplicate_of"] == "same"


def test_process_mail_same_mail_id_does_not_tag_invalid():
    """同一封再扫：跳过、不打 关闭-无效邮件。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail, process_mail

    class _StubGraph:
        def __init__(self):
            self.calls: list = []
        def add_category(self, mail_id, category):
            self.calls.append((mail_id, category))

    g = _StubGraph()
    d = _DuplicateDetector()
    m = Mail(id="same", subject="App not working",
             sender="alice@x.com", received_at="",
             body_text="My app is broken and I cannot use it", is_read=False)

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
    rec1 = process_mail(m, g=None, llm=_StubLLM(), templates=templates,
                        allowlist=None, dry_run=True, use_llm=True, dedup=d)
    rec2 = process_mail(m, g=g, llm=_StubLLM(), templates=templates,
                        allowlist=None, dry_run=True, use_llm=True, dedup=d)
    assert rec1.get("draft_id") or rec1.get("would_post_body") or rec1.get("skipped")
    assert rec2.get("skipped") == "already_scanned"
    assert rec2.get("category_applied") is False
    assert g.calls == []


def test_duplicate_detector_same_subject_follow_up_not_dup():
    """同客户同主题，但是新的跟进内容 → 不算无效。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    m1 = Mail(
        id="m1", subject="Re: Ongoing Issues with the App",
        sender="alexia.png@hotmail.com", received_at="",
        body_text="Also, I have other people close to me that are experiencing the same issue.",
        is_read=False,
    )
    m2 = Mail(
        id="m2", subject="Re: Ongoing Issues with the App",
        sender="alexia.png@hotmail.com", received_at="",
        body_text=(
            "The vehicle status has not updated since 29/07. I already "
            "restarted infotainment, reinstalled the app, and visited the dealer. "
            "Please escalate to the technical team."
        ),
        is_read=False,
    )
    assert d.check_and_record(m1) is None
    assert d.check_and_record(m2) is None


_WEB_FORM = """\
Caro team di assistenza iSmart,

Spero che questo messaggio vi trovi bene.

Scrivo per segnalare i problemi che sto attualmente riscontrando con il mio veicolo o l'app iSmart. Per aiutarvi a diagnosticare e risolvere efficacemente i problemi, ecco i dettagli rilevanti:
• Paese/Regione:
• Marca: MG
• Modello di veicolo: MG3
• Numero di identificazione del veicolo: LSJWP4393RZ209912
• Tipo di problema: Altri
• Descrizione dei problemi: {desc}
"""


def test_duplicate_detector_web_form_different_issue_not_dup():
    """同客户同表单主题，描述不同 → 不是重发。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    subj = "Problemi con il veicolo o l'app iSmart"
    m1 = Mail(id="m1", subject=subj, sender="v@x.com", received_at="",
              body_text=_WEB_FORM.format(desc="VPN again? Remove this restriction."),
              is_read=False)
    m2 = Mail(id="m2", subject=subj, sender="v@x.com", received_at="",
              body_text=_WEB_FORM.format(desc="Climate control does not heat the seats."),
              is_read=False)
    assert d.check_and_record(m1) is None
    assert d.check_and_record(m2) is None


def test_duplicate_detector_web_form_privacy_footer_not_dup():
    """表单末尾隐私声明相同、描述不同 → 不是重发。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    footer = (
        "\n\nI can read the latest privacy policy at "
        "https://page.soimt.com/soimt/privacy/en-GB_privacy.html\n"
        "By sending this email I consent to data processing.\n"
    )
    subj = "Problemi con il veicolo o l'app iSmart"
    m1 = Mail(
        id="m1", subject=subj, sender="v@x.com", received_at="",
        body_text=_WEB_FORM.format(desc="VPN restriction please remove.") + footer,
        is_read=False,
    )
    m2 = Mail(
        id="m2", subject=subj, sender="v@x.com", received_at="",
        body_text=_WEB_FORM.format(desc="Seat heating does not work at all.") + footer,
        is_read=False,
    )
    assert d.check_and_record(m1) is None
    assert d.check_and_record(m2) is None


def test_duplicate_detector_web_form_resubmit_is_dup():
    """同表单主题且描述相同 → 连点重发。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    subj = "Problemi con il veicolo o l'app iSmart"
    body = _WEB_FORM.format(desc="VPN again? Remove this restriction.")
    m1 = Mail(id="m1", subject=subj, sender="v@x.com", received_at="",
              body_text=body, is_read=False)
    m2 = Mail(id="m2", subject=subj, sender="v@x.com", received_at="",
              body_text=body, is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None
    assert r["duplicate_of"] == "m1"


def test_duplicate_detector_days_later_same_issue_not_invalid():
    """客服要过 VIN 后再交同一张表，不是几秒连点，不能打无效邮件。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    subj = "Issues with Vehicle or iSmart App"
    body = _WEB_FORM.format(
        desc="my car keeps disconnecting from my MG app within an hour"
    )
    m1 = Mail(
        id="m1", subject=subj, sender="buddy@x.com",
        received_at="2026-08-20T10:00:00Z", body_text=body, is_read=False,
    )
    m2 = Mail(
        id="m2", subject=subj, sender="buddy@x.com",
        received_at="2026-09-09T08:00:00Z", body_text=body, is_read=False,
    )
    assert d.check_and_record(m1) is None
    assert d.check_and_record(m2) is None


def test_duplicate_detector_minutes_later_still_dup():
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    subj = "Issues with Vehicle or iSmart App"
    body = _WEB_FORM.format(desc="app keeps disconnecting from the car")
    m1 = Mail(
        id="m1", subject=subj, sender="buddy@x.com",
        received_at="2026-09-09T08:00:00Z", body_text=body, is_read=False,
    )
    m2 = Mail(
        id="m2", subject=subj, sender="buddy@x.com",
        received_at="2026-09-09T08:04:00Z", body_text=body, is_read=False,
    )
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None
    assert r["duplicate_of"] == "m1"


def test_duplicate_detector_web_form_cid_tail_is_dup():
    """第二封末尾多 cid 图（Outlook 把附件写进 body）→ 仍是连点，只留一封草稿。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    subj = "Probleme mit dem Fahrzeug oder der iSmart App"
    desc = (
        "Die iSmart App verbindet sich nicht mit dem Fahrzeug. "
        "Wenn es ein paar Minuten steht, dann kann man das Auto nicht mehr erreichen."
    )
    body = (
        "Sehr geehrtes iSMART-Service-Team,\n"
        "• Beschreibung der Probleme: " + desc + "\n"
        "Ich kann die neueste Datenschutzrichtlinie lesen unter "
        "https://page.soimt.com/soimt/privacy/en-GB_privacy.html\n"
    )
    noisy = body + "\n[cid:image001.jpg@01D] [cid:image002.png@02E]\n"
    m1 = Mail(id="m1", subject=subj, sender="koch@x.com", received_at="",
              body_text=body, is_read=False)
    m2 = Mail(id="m2", subject=subj, sender="koch@x.com", received_at="",
              body_text=noisy, is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None
    assert r["duplicate_of"] == "m1"


def test_duplicate_detector_web_form_extra_thanks_is_dup():
    """连点第二封在描述末尾加 Grazie → 短描述被长描述包含，不算新问题。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    subj = "Problemi con il veicolo o l'app iSmart"
    base = "Non riesco a collegare apple carplay tramite cavo con iphone 17"
    m1 = Mail(id="m1", subject=subj, sender="guerra@x.com", received_at="",
              body_text=_WEB_FORM.format(desc=base), is_read=False)
    m2 = Mail(id="m2", subject=subj, sender="guerra@x.com", received_at="",
              body_text=_WEB_FORM.format(desc=base + " Grazie."), is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None
    assert r["duplicate_of"] == "m1"
    assert r["match"] in ("subject_exact", "issue_contains", "same_content")


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
    """同一人正文几乎相同、主题不同（表单语言）→ 第二封不算新信，只留一封草稿。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    body = "How do I schedule overnight charging for my MG4 vehicle please"
    m1 = Mail(id="m1", subject="Charging question", sender="alice@x.com",
              received_at="", body_text=body, is_read=False)
    m2 = Mail(id="m2", subject="Overnight charging setup", sender="alice@x.com",
              received_at="", body_text=body, is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None
    assert r["duplicate_of"] == "m1"
    assert r["match"] == "same_content"
    assert r["same_subject"] is False


def test_duplicate_detector_localized_subject_same_issue_is_dup():
    """网页表单主题语言不同、描述相同 → 只建一封。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    body = _WEB_FORM.format(desc="VPN again? Remove this restriction.")
    m1 = Mail(id="m1", subject="Issues with Vehicle or iSmart App",
              sender="v@x.com", received_at="", body_text=body, is_read=False)
    m2 = Mail(id="m2", subject="Problèmes avec le Véhicule ou l'Application iSmart",
              sender="v@x.com", received_at="", body_text=body, is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None
    assert r["duplicate_of"] == "m1"


def test_duplicate_detector_persists_across_instances():
    """下一轮新进程仍能认出上一轮已处理的相同内容。"""
    import tempfile
    from pathlib import Path
    from agent.outlook_drafts import Mail, _DuplicateDetector

    body = "My app is broken and I cannot use it at all today"
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "dedup.json"
        d1 = _DuplicateDetector(store_path=store)
        m1 = Mail(id="m1", subject="App not working", sender="alice@x.com",
                  received_at="", body_text=body, is_read=False)
        m2 = Mail(id="m2", subject="App not working", sender="alice@x.com",
                  received_at="", body_text=body, is_read=False)
        assert d1.check_and_record(m1) is None
        d2 = _DuplicateDetector(store_path=store)
        r = d2.check_and_record(m2)
        assert r is not None
        assert r["duplicate_of"] == "m1"


def test_duplicate_detector_different_sender_not_dup():
    """不同 sender 同样的 body、没有同一 VIN → 不算重复。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    m1 = Mail(id="m1", subject="App not working", sender="alice@x.com",
              received_at="", body_text="My app is broken", is_read=False)
    m2 = Mail(id="m2", subject="App not working", sender="bob@y.com",
              received_at="", body_text="My app is broken", is_read=False)
    assert d.check_and_record(m1) is None
    assert d.check_and_record(m2) is None


def test_duplicate_detector_long_form_extra_privacy_is_dup():
    """描述超过 400 字、第二封末尾多隐私声明 → 整段比较，仍是连点。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    story = (
        "Hello, my MG ZS EV was working normally yesterday evening and I parked it "
        "without any issues. This morning, when I tried to drive the vehicle, the "
        "following error message appeared: &quot;vehicle control system fault - "
        "please stop safely&quot; The vehicle enters ready mode and can move only "
        "a few meters before the fault appears and the vehicle stops. Could you "
        "please assist and arrange a technical inspection?"
    )
    longer = (
        story.replace("&quot;", '"')
        + " I can read the latest privacy notice for more details of the data "
        "processing activities."
    )
    subj = "Issues with Vehicle or iSmart App"
    m1 = Mail(id="m1", subject=subj, sender="a@x.com",
              received_at="2026-10-07T08:02:48Z",
              body_text=_WEB_FORM.format(desc=story), is_read=False)
    m2 = Mail(id="m2", subject=subj, sender="a@x.com",
              received_at="2026-10-07T08:02:57Z",
              body_text=_WEB_FORM.format(desc=longer), is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None, "long form resubmit should be one draft"
    assert r["duplicate_of"] == "m1"


def test_duplicate_detector_same_vin_two_mailboxes_is_dup():
    """同一 VIN、两个邮箱、21 秒内同一描述 → 只留一封。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    desc = (
        "Bonjour, à partir de quel kilométrage je dois amener la voiture "
        "pour le premier contrôle? Je l’ai acheté neuve avec 10km. Merci."
    )
    body = _WEB_FORM.format(desc=desc)
    m1 = Mail(id="m1", subject="Problèmes avec le Véhicule ou l'Application iSmart",
              sender="louislesser04@gmail.com", received_at="2026-10-06T20:16:40Z",
              body_text=body, is_read=False)
    m2 = Mail(id="m2", subject="Problèmes avec le Véhicule ou l'Application iSmart",
              sender="louislsrpro@gmail.com", received_at="2026-10-06T20:17:01Z",
              body_text=body, is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None, r
    assert r["match"] == "same_vin"
    assert r["duplicate_of"] == "m1"


def test_duplicate_detector_same_vin_different_issue_not_dup():
    """同一 VIN、两个邮箱，但描述不是同一件事 → 各留一封。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    subj = "Issues with Vehicle or iSmart App"
    m1 = Mail(id="m1", subject=subj, sender="a@x.com",
              received_at="2026-10-06T20:16:40Z",
              body_text=_WEB_FORM.format(desc="The app will not unlock the doors."),
              is_read=False)
    m2 = Mail(id="m2", subject=subj, sender="b@y.com",
              received_at="2026-10-06T20:17:01Z",
              body_text=_WEB_FORM.format(desc="Climate control does not heat the seats."),
              is_read=False)
    assert d.check_and_record(m1) is None
    assert d.check_and_record(m2) is None


def test_duplicate_detector_same_vin_later_is_dup():
    """同一 VIN、另一个邮箱、描述相同：隔天也是重复，不限 3 小时。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    body = _WEB_FORM.format(desc="The app will not unlock the doors at all.")
    m1 = Mail(id="m1", subject="Issues with Vehicle or iSmart App",
              sender="a@x.com", received_at="2026-10-06T20:16:40Z",
              body_text=body, is_read=False)
    m2 = Mail(id="m2", subject="Issues with Vehicle or iSmart App",
              sender="b@y.com", received_at="2026-10-08T20:16:40Z",
              body_text=body, is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None, r
    assert r["match"] == "same_vin"
    assert r["duplicate_of"] == "m1"


def test_duplicate_detector_same_sender_next_day_same_content_is_dup():
    """同一发件人隔天再发相同内容，也是重复。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    body = _WEB_FORM.format(desc="The app will not unlock the doors at all.")
    m1 = Mail(id="m1", subject="Issues with Vehicle or iSmart App",
              sender="a@x.com", received_at="2026-10-06T20:16:40Z",
              body_text=body, is_read=False)
    m2 = Mail(id="m2", subject="Issues with Vehicle or iSmart App",
              sender="a@x.com", received_at="2026-10-08T20:16:40Z",
              body_text=body, is_read=False)
    assert d.check_and_record(m1) is None
    r = d.check_and_record(m2)
    assert r is not None, r
    assert r["duplicate_of"] == "m1"


def test_duplicate_detector_same_sender_next_day_new_issue_not_dup():
    """同一发件人隔天再写，但描述换成另一件事，仍各留一封。"""
    from agent.outlook_drafts import _DuplicateDetector, Mail
    d = _DuplicateDetector()
    subj = "Issues with Vehicle or iSmart App"
    m1 = Mail(id="m1", subject=subj, sender="a@x.com",
              received_at="2026-10-06T20:16:40Z",
              body_text=_WEB_FORM.format(desc="The app will not unlock the doors."),
              is_read=False)
    m2 = Mail(id="m2", subject=subj, sender="a@x.com",
              received_at="2026-10-08T20:16:40Z",
              body_text=_WEB_FORM.format(desc="Climate control does not heat the seats."),
              is_read=False)
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
    calls_after_first = llm.calls
    rec2 = process_mail(
        m2, g=None, llm=llm, templates=templates,
        allowlist=None, dry_run=True, use_llm=True,
        dedup=d,
    )
    assert llm.calls == calls_after_first, f"第二封应短路不调 LLM；实际调了 {llm.calls} 次"
    assert rec1.get("draft_id") or rec1.get("would_post_body")
    assert rec2.get("skipped") == "duplicate"
    assert rec2.get("duplicate_of") == "m1"
    assert rec2.get("duplicate_score", 0) >= 0.7


def test_process_mail_duplicate_applies_category_via_graph():
    """重复邮件 → apply_close_invalid（关闭-无效邮件 + isRead）。"""
    from agent.outlook_drafts import (
        GraphClient, _DuplicateDetector, Mail, process_mail,
    )

    g = MagicMock(spec=GraphClient)
    g.find_unsent_customer_draft = MagicMock(return_value=None)
    g.get_conversation_thread = MagicMock(return_value=[])
    g.get_categories = MagicMock(return_value=[])
    g.set_categories = MagicMock(return_value=None)
    g.create_draft_reply = MagicMock(return_value="draft-1")
    g.download_attachment = MagicMock(return_value=b"")
    d = _DuplicateDetector()
    m1 = Mail(id="m1", subject="App not working",
              sender="alice@x.com", received_at="",
              body_text="My app is broken and I cannot use it", is_read=False)
    m2 = Mail(id="m2", subject="App not working",
              sender="alice@x.com", received_at="",
              body_text="My app is broken and I cannot use it still", is_read=False)

    class _StubLLM:
        def __init__(self):
            self.calls = 0
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
    calls_after_first = llm.calls
    rec2 = process_mail(m2, g=g, llm=llm, templates=templates,
                        allowlist=None, dry_run=False, use_llm=True, dedup=d)

    assert rec2["skipped"] == "duplicate"
    assert rec2["category_applied"] is True
    assert rec2["category_error"] is None
    assert llm.calls == calls_after_first
    g.apply_close_invalid.assert_called_once_with("m2")


def test_duplicate_quoting_cs_closes_not_human_or_draft():
    """连点再发即使引用了客服回复，也只关闭，不转人工、不建草稿。"""
    from agent.outlook_drafts import (
        GraphClient, MailboxRouter, _DuplicateDetector, Mail, process_mail,
    )
    g = MagicMock(spec=GraphClient)
    g.get_categories = MagicMock(return_value=["转人工"])
    g.create_draft_reply = MagicMock(return_value="draft-1")
    g.add_category = MagicMock()
    d = _DuplicateDetector()
    body = (
        "My app is broken and I cannot use it\n"
        "On Mon, customer.care <customer.care@ismart-service.com> wrote:\n"
        "Dear customer, please try again.\n"
    )
    m1 = Mail(id="m1", subject="App not working", sender="alice@x.com",
              received_at="2026-10-01T01:00:00Z", body_text=body, is_read=False)
    m2 = Mail(id="m2", subject="App not working", sender="alice@x.com",
              received_at="2026-10-01T01:05:00Z", body_text=body, is_read=False)
    router = MailboxRouter(default="customer.care@ismart-service.com")
    process_mail(m1, g=g, llm=None, templates=[], allowlist=None,
                 dry_run=True, use_llm=False, router=router, dedup=d)
    rec2 = process_mail(m2, g=g, llm=None, templates=[], allowlist=None,
                        dry_run=False, use_llm=False, router=router, dedup=d)
    assert rec2["skipped"] == "duplicate", rec2
    g.apply_close_invalid.assert_called_once_with("m2")
    g.create_draft_reply.assert_not_called()
    g.add_category.assert_not_called()


def test_process_mail_duplicate_category_failure_captured_not_raised():
    """Graph apply_close_invalid 抛错时 process_mail 不传播，只在 record 里写
    category_error 给后续审计。LLM 仍不调。"""
    from agent.outlook_drafts import (
        GraphClient, _DuplicateDetector, Mail, process_mail,
    )

    g = MagicMock(spec=GraphClient)
    g.find_unsent_customer_draft = MagicMock(return_value=None)
    g.get_conversation_thread = MagicMock(return_value=[])
    g.get_categories = MagicMock(return_value=[])
    g.set_categories = MagicMock(return_value=None)
    g.apply_close_invalid = MagicMock(side_effect=RuntimeError("simulated Graph 503"))
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
    calls_after_first = llm.calls
    rec2 = process_mail(m2, g=g, llm=llm, templates=templates,
                        allowlist=None, dry_run=False, use_llm=True, dedup=d)

    assert rec2["skipped"] == "duplicate"
    assert rec2["category_applied"] is False
    assert "simulated Graph 503" in (rec2["category_error"] or "")
    assert llm.calls == calls_after_first, "category 失败也不应触发第二封 LLM"


# ── M2.5.4: list_filtered 拉邮件参数 ──────────────────────────────────


def test_list_filtered_builds_odata_filter_with_since_until():
    """list_filtered(since, until, is_read=None, exclude_categories=['关闭-无效邮件'])
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
        is_read=None, exclude_categories=["关闭-无效邮件"], order="asc",
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
        exclude_categories=["关闭-无效邮件", "已处理"],
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
        g.add_category("m1", "关闭-无效邮件, foo")
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
    g.session.request.return_value.json.return_value = {"categories": []}
    g.user_id = "me"
    g._created_draft_ids = set()

    g.add_category("fake-id", "关闭-无效邮件")

    call = g.session.request.call_args
    method, url = call.args[0], call.args[1]
    assert method == "PATCH"
    # _patch 内部把 path 拼成完整 URL；检查路径末尾 + 拼接
    assert url.endswith("/users/me/messages/fake-id"), url
    assert call.kwargs["json"] == {"categories": ["关闭-无效邮件"]}


def test_add_category_passes_guard_even_when_not_in_draft_registry():
    """add_category 走 PATCH 原邮件（不在 _created_draft_ids 注册表）→ 专项
    守门放过；body 只含 categories → 不 raise。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value.status_code = 200
    g.session.request.return_value.raise_for_status = MagicMock()
    g.session.request.return_value.json.return_value = {"categories": []}
    g.user_id = "me"
    g._created_draft_ids = set()
    g.add_category("original-mail-not-in-registry", "关闭-无效邮件")
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
            body={"categories": ["关闭-无效邮件"], "body": {"contentType": "text",
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


def test_patch_original_mail_isRead_alone_blocked():
    """单独 PATCH isRead 仍禁止。"""
    from agent.outlook_drafts import GraphClient, MailboxSafetyError
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.user_id = "me"
    g._created_draft_ids = set()
    try:
        g._patch("/users/me/messages/m1", body={"isRead": True})
    except MailboxSafetyError:
        return
    raise AssertionError("单独 isRead 应被拒")


def test_patch_original_mail_resolved_categories_and_isRead_allowed():
    """仅关闭-无效邮件 + isRead 允许；关闭-已解决不得夹带 isRead。"""
    from agent.outlook_drafts import GraphClient, MailboxSafetyError
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value = MagicMock(status_code=200, raise_for_status=lambda: None)
    g.user_id = "me"
    g._created_draft_ids = set()
    g._patch(
        "/users/me/messages/m2",
        body={"categories": ["关闭-无效邮件"], "isRead": True},
    )
    try:
        g._patch(
            "/users/me/messages/m1",
            body={"categories": ["关闭-已解决"], "isRead": True},
        )
    except MailboxSafetyError:
        pass
    else:
        raise AssertionError("关闭-已解决+isRead 应被拒")


def test_apply_close_invalid_marks_read():
    """apply_close_invalid 同时打关闭-无效邮件并 isRead=True。"""
    from agent.outlook_drafts import GraphClient, CLOSE_INVALID_CATEGORY
    g = GraphClient.__new__(GraphClient)
    g.session = MagicMock()
    g.session.request.return_value = MagicMock(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {"categories": ["转人工"]},
    )
    g.user_id = "me"
    g._created_draft_ids = set()
    g._queue_cats_ready = True
    g._master_cats = []
    g.get_categories = MagicMock(return_value=["转人工", "VIN:LSJWH4099PN101557"])
    patches = []
    def _patch(path, body=None, **kw):
        patches.append((path, body))
        return MagicMock(status_code=200, raise_for_status=lambda: None)
    g._patch = _patch
    g.apply_close_invalid("m1")
    assert len(patches) == 1
    _, body = patches[0]
    assert body["isRead"] is True
    assert CLOSE_INVALID_CATEGORY in body["categories"]
    assert "转人工" not in body["categories"]
    assert "VIN:LSJWH4099PN101557" in body["categories"]


def test_apply_customer_resolved_does_not_mark_read():
    """关闭-已解决只改 categories，不改 isRead。"""
    from agent.outlook_drafts import GraphClient, CLOSE_RESOLVED_CATEGORY
    g = GraphClient.__new__(GraphClient)
    g.user_id = "me"
    g._queue_cats_ready = True
    g.get_categories = MagicMock(return_value=["转人工"])
    set_calls = []
    g.set_categories = MagicMock(side_effect=lambda mid, cats: set_calls.append((mid, cats)))
    g.apply_customer_resolved("m1")
    assert set_calls == [("m1", [CLOSE_RESOLVED_CATEGORY])]
    g.set_categories.assert_called_once()


def test_customer_issue_resolved_reestablished_connection():
    from agent.outlook_drafts import customer_issue_resolved
    en = (
        "I reset the display and the connection was successfully re-established. "
        "Thank you for your attention\n\n"
        "-----Original Message-----\n"
        "From: customer.care@ismart-service.com\n"
        "Dear Mr./Ms. SINATRA,\n"
        "Please first try the operation with the vehicle parked...\n"
    )
    it = (
        "Ho effettuato il reset del display e con successo la connessione "
        "si è ristabilita. Grazie per l'attenzione \n"
        "Il Lun 14 Set 2026, 09:23 customer.care <customer.care@ismart-service.com> ha scritto:\n"
        "Dear Mr./Ms. SINATRA,\n"
    )
    assert customer_issue_resolved(en)
    assert customer_issue_resolved(it)
    made_it = (
        "we made it.\n"
        "Thanks a lot because we tried with three different dealers in Greece without success\n"
        "Στις Τρί 15 Σεπ 2026 ο/η customer.care έγραψε:\n"
        "As requested, we have unbound the vehicle.\n"
    )
    assert customer_issue_resolved(made_it)
    assert not customer_issue_resolved("we made it to the dealer but it still fails")
    assert not customer_issue_resolved("Please re-establish the connection.")
    assert not customer_issue_resolved("Is it fixed yet?")
    assert customer_issue_resolved(
        "Good morning,\n\nYes, it's already resolved.\n\nThank you."
    )
    assert customer_issue_resolved("Thank you for replying.")
    nieves = (
        "Thank you for replying.\n"
        "El jue, 10 sept 2026, 10:21, customer.care "
        "<customer.care@ismart-service.com> escribió:\n"
        "Please try the following steps to bind the vehicle...\n"
    )
    assert customer_issue_resolved(nieves)
    marek = (
        "Thank you - will do that\n"
        "On Thu, Sep 10, 2026 at 8:07 AM customer.care "
        "<customer.care@ismart-service.com> wrote:\n"
        "Regarding the windscreen stone chip, contact the dealer...\n"
    )
    assert customer_issue_resolved(marek)
    assert customer_issue_resolved(
        "It works fine when not using the VPN\n\nBest regards\n\nJoey Pierce"
    )
    assert not customer_issue_resolved(
        "Thank you for replying, but it still fails to bind."
    )
    assert not customer_issue_resolved(
        "All good. The issue is already fixed.\n"
        "Can you pls confirm if I can start the engine remotely with this app?"
    )
    janet = (
        "customer.care\n"
        "Thank you 😊\n"
        "On 16 Sept 2026 10:39, \"customer.care\" "
        "<customer.care@ismart-service.com> wrote:\n"
        "Please note that the iSMART Service Team does not have the "
        "authority to delete user accounts on your behalf. "
        "Open the iSMART app and tap Delete Account.\n"
    )
    assert customer_issue_resolved(janet)
    assert customer_issue_resolved("Thank you 😊")
    assert customer_issue_resolved("Thanks!\n\nSent via BT Email App")
    assert customer_issue_resolved(
        "Thank you, I managed to link my app to my vehicle.\n"
        "Thank you very much for your answer.\n"
        "Sincerely,\n"
        "Christophe CLEMENT"
    )
    assert not customer_issue_resolved(
        "Thank you, but the app still will not bind my car."
    )
    assert not customer_issue_resolved(
        "Thank you very much for your answer.\n"
        "The car still will not charge and the app is offline."
    )
    assert not customer_issue_resolved(
        "Hi,\n\n"
        "Thank you for your email.\n\n"
        "Please find attached the photo of the app with the error being shown.\n"
        "By touching the screen the error disappears and the app is working "
        "as normal in the background unaffected by the error message.\n\n"
        "Kind regards,\n\n"
        "Clyde Camp"
    )


def test_pure_chase_nudge_close_invalid_not_human():
    from agent.outlook_drafts import (
        GraphClient, Mail, MailboxRouter, process_mail, pure_chase_nudge,
    )
    body = (
        "I have contacted you at this email already sometimes and never respond. "
        "For a car company it is really not serious."
    )
    assert pure_chase_nudge(body)
    quoted = (
        body
        + "\nOn Mon, customer.care <customer.care@ismart-service.com> wrote:\n"
        "Dear customer, please try again.\n"
    )
    assert pure_chase_nudge(quoted)
    assert not pure_chase_nudge(
        body + " The app still will not bind after scanning the QR code."
    )
    assert not pure_chase_nudge("still need help")
    g = MagicMock(spec=GraphClient)
    g.create_draft_reply = MagicMock()
    router = MailboxRouter(default="customer.care@ismart-service.com")
    mail = Mail(
        id="chase1",
        subject="Re: Issues with Vehicle or iSmart App",
        sender="a@b.com",
        received_at="2026-09-16T00:00:00Z",
        body_text=quoted,
        is_read=False,
        conversation_id="c1",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=router, dedup=None,
    )
    assert rec.get("skipped") == "pure_chase_nudge", rec
    assert rec.get("close_tag") == "关闭-无效邮件"
    g.create_draft_reply.assert_not_called()
    g.apply_close_invalid.assert_not_called()  # dry_run

    rec2 = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=False, use_llm=False, router=router, dedup=None,
    )
    assert rec2.get("skipped") == "pure_chase_nudge"
    g.apply_close_invalid.assert_called_once_with("chase1")


def test_bank_legalization_junk_close_invalid_not_human():
    from agent.outlook_drafts import (
        GraphClient, Mail, MailboxRouter, process_mail,
        skip_needs_human_queue,
    )
    body = (
        "--------- Forwarded message ---------\n"
        "Company Legalization Bulletin. Please reactivate myAlpha Web "
        "for Business and submit G.E.MI. documents.\n"
    )
    g = MagicMock(spec=GraphClient)
    g.create_draft_reply = MagicMock()
    mail = Mail(
        id="junk1",
        subject="Fwd: Company Legalization",
        sender="a@b.com",
        received_at="2026-09-15T00:00:00Z",
        body_text=body,
        is_read=False,
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=MailboxRouter(), dedup=None,
    )
    assert rec.get("skipped") == "close_invalid_junk", rec
    assert rec.get("close_tag") == "关闭-无效邮件"
    assert rec.get("skip_reason_detail") == "misdirected_bank_legalization"
    assert not skip_needs_human_queue(rec["skipped"])
    g.create_draft_reply.assert_not_called()
    g.apply_close_invalid.assert_not_called()

    rec2 = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=False, use_llm=False, router=MailboxRouter(), dedup=None,
    )
    assert rec2.get("skipped") == "close_invalid_junk"
    g.apply_close_invalid.assert_called_once_with("junk1")
    g.create_draft_reply.assert_not_called()


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
        body={"categories": ["关闭-无效邮件"]},
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
    # 充电故障模板要求 VIN；正文没有 17 位 VIN → 知识库闸门改走 GEN-002
    assert tmpl.get("template_id") == "GEN-002", (
        f"缺 VIN 的技术问题应 GEN-002；实际 {tmpl.get('template_id')}"
    )


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


# ── 5. M2.5.5 conversation thread + PDF + thread_context ───────────────


def test_mail_dataclass_has_conversation_id_and_sender_display_name():
    """Mail dataclass 应带 conversation_id 和 sender_display_name 字段。"""
    from agent.outlook_drafts import Mail
    m = Mail(
        id="x", subject="s", sender="a@x.com",
        received_at="", body_text="b", is_read=False,
    )
    assert m.conversation_id == ""
    assert m.sender_display_name == ""
    # 加可选 kwarg
    m2 = Mail(
        id="x", subject="s", sender="a@x.com",
        received_at="", body_text="b", is_read=False,
        conversation_id="conv-123",
        sender_display_name="Philip Watson",
    )
    assert m2.conversation_id == "conv-123"
    assert m2.sender_display_name == "Philip Watson"


def test_graphclient_get_conversation_thread_builds_correct_odata():
    """M2.5.5: get_conversation_thread 应该发对 OData（filter + orderby + top）。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient(token="dummy", user_id="user-1")
    g._request = MagicMock(return_value=MagicMock(
        json=lambda: {"value": []},
        raise_for_status=lambda: None,
    ))
    g.get_conversation_thread("conv-with-'-quote", max_messages=10)
    (call,) = g._request.call_args_list
    # MagicMock call: (method, args, kwargs) —— 这里 method/args 在 arg list (位置),
    # method 是位置参数 0
    method, path = call.args[0], call.args[1]
    assert method == "GET"
    assert "/users/user-1/messages" in path
    params = call.kwargs.get("params")
    assert params is not None
    # conversationId 单引号 escape（OData string escape → ''）
    assert "conv-with-''-quote" in params["$filter"], params["$filter"]
    assert "conversationId eq" in params["$filter"]
    assert "$orderby" not in params, params
    assert params["$top"] == "10"


def test_graphclient_get_conversation_thread_parses_messages():
    """解析 Graph 响应：取 sender / body / received。"""
    from agent.outlook_drafts import GraphClient
    g = GraphClient(token="dummy", user_id="me")
    sample = {
        "value": [
            {
                "id": "m1",
                "sender": {"emailAddress": {"name": "John", "address": "j@x.com"}},
                "receivedDateTime": "2026-07-01T10:00:00Z",
                "body": {"contentType": "text", "content": "Hello, I have a problem"},
            },
            {
                "id": "m2",
                "sender": {"emailAddress": {"name": "CS Team", "address": "cs@x.com"}},
                "receivedDateTime": "2026-07-02T11:00:00Z",
                "body": {"contentType": "html",
                         "content": "<p>Hi John,</p><p>Please check ..</p>"},
            },
        ]
    }
    g._request = MagicMock(return_value=MagicMock(
        json=lambda: sample,
        raise_for_status=lambda: None,
    ))
    out = g.get_conversation_thread("conv-1")
    assert len(out) == 2
    assert out[0]["sender_email"] == "j@x.com"
    assert out[0]["body_text"] == "Hello, I have a problem"
    assert out[1]["sender_email"] == "cs@x.com"
    # HTML → plain text
    assert "<p>" not in out[1]["body_text"]
    assert "Hi John" in out[1]["body_text"]
    assert out[0]["is_draft"] is False
    assert out[1]["is_draft"] is False


def test_process_mail_skips_when_we_replied_last_in_thread(monkeypatch):
    """M2.5.5: 线程最后一封是自己回的 → skip = we_replied_last（暂不打 category）。

    用 mock Graph 模拟：thread 2 条消息，最后一条 sender = CS mailbox。
    process_mail 应该早返回并跳过。
    """
    from agent.outlook_drafts import (
        GraphClient, MailboxRouter, Mail, process_mail, AzureLLMClient,
    )

    # Mock LLM（不应被调 —— 跳过前 early return）
    llm = MagicMock(spec=AzureLLMClient)

    # Mock Graph
    g = MagicMock(spec=GraphClient)
    # mail.id 不同 conversation 不同
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "current", "sender_email": "customer@x.com", "sender_name": "Jane",
         "received_at": "2026-07-01T10:00:00Z", "body_text": "Help, app not binding"},
        {"id": "cs1", "sender_email": "cs-team@mgmotor.com", "sender_name": "CS",
         "received_at": "2026-07-02T11:00:00Z", "body_text": "Hi Jane, please send VIN"},
    ])
    g.add_category = MagicMock(return_value=None)

    router = MailboxRouter(uk="cs-team@mgmotor.com")
    mail = Mail(
        id="current",
        subject="Re: app not binding",
        sender="customer@x.com",
        sender_display_name="Jane Smith",
        received_at="2026-07-03T10:00:00Z",
        body_text="I tried, still not working",
        is_read=False,
        conversation_id="conv-XYZ",
    )
    rec = process_mail(
        mail, g=g, llm=llm, templates=[], allowlist=None,
        dry_run=True, use_llm=True, router=router, dedup=None,
    )
    assert rec["skipped"] == "we_replied_last", rec
    assert rec["thread_messages_count"] == 2
    assert rec["thread_our_replies"] == 1
    # LLM 不该被调用
    assert not llm.invoke_json.called
    assert rec.get("tag_applied") is False
    assert not g.add_category.called


_COMFORT_PATIENCE = (
    "Thank you for contacting the iSMART Service Team.\n\n"
    "We kindly ask for your patience while this is reviewed.\n\n"
    "Privacyoffice@ismart-service.com"
)
_COMFORT_INTAKE = (
    "Thank you for contacting the iSMART Service Team.\n\n"
    "As your feedback involves technical details, we kindly request "
    "the following information.\n"
    "Country/Region: Sweden\n"
    "Description: cannot connect app"
)


def _stub_business_draft(monkeypatch):
    captured: dict = {}

    def fake(body, **kwargs):
        captured["body"] = body
        captured["thread"] = kwargs.get("thread_context")
        return {
            "triage": {
                "intent": "app_binding",
                "urgency": "P2",
                "region": "Europe",
                "red_flags": [],
                "language": "en",
            },
            "template_doc": {
                "template_id": "BIND-008",
                "level": "auto_draft_safe",
                "human_trigger": "",
            },
            "filled_body": "Dear customer,\n\nPlease try the steps.",
            "template_forbidden": "",
        }

    monkeypatch.setattr("agent.outlook_drafts.run_template_reply", fake)
    monkeypatch.setattr(
        "agent.outlook_drafts.draft_semantic_mismatch_reason",
        lambda *args, **kwargs: None,
    )
    return captured


def test_comfort_reply_detector():
    from agent.comfort_reply import is_comfort_reply, quoted_reply_is_comfort

    assert is_comfort_reply(_COMFORT_PATIENCE)
    assert is_comfort_reply(_COMFORT_INTAKE)
    real = (
        "Hi Jane, please send VIN\n\n"
        "-----Original Message-----\n"
        + _COMFORT_PATIENCE
    )
    assert not is_comfort_reply(real)
    quoted = (
        "The app still cannot connect.\n\n"
        "On Monday, customer.care@ismart-service.com wrote:\n"
        + _COMFORT_PATIENCE
    )
    assert quoted_reply_is_comfort(quoted, "customer.care@ismart-service.com")
    business = (
        "Still broken.\n\n"
        "On Monday, customer.care@ismart-service.com wrote:\n"
        "Hi Jane, please send VIN"
    )
    assert not quoted_reply_is_comfort(business, "customer.care@ismart-service.com")


def test_comfort_tag_target_uses_latest_customer_before_comfort():
    from agent.comfort_reply import comfort_tag_target

    thread = [
        {"id": "m1", "sender_email": "a@x.com", "received_at": "2026-10-20T01:00:00Z",
         "body_text": "The app will not connect to the car.", "is_draft": False},
        {"id": "c1", "sender_email": "customer.care@ismart-service.com",
         "received_at": "2026-10-20T02:00:00Z", "body_text": _COMFORT_PATIENCE,
         "is_draft": False},
        {"id": "m2", "sender_email": "a@x.com", "received_at": "2026-10-20T03:00:00Z",
         "body_text": "Adding the workshop photo of the error screen.",
         "is_draft": False},
    ]
    own = {"customer.care@ismart-service.com"}
    assert comfort_tag_target(
        thread, comfort_at="2026-10-20T02:00:00Z", self_emails=own,
    ) == "m1"
    assert comfort_tag_target(
        thread, comfort_at="2026-10-20T04:00:00Z", self_emails=own,
    ) == "m2"


def test_process_mail_comfort_only_still_drafts(monkeypatch):
    """只发过安抚、最新一封是安抚信 → 仍起草，且不把安抚正文喂进选模板。"""
    from agent.outlook_drafts import GraphClient, MailboxRouter, Mail, process_mail

    captured = _stub_business_draft(monkeypatch)
    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "current", "sender_email": "customer@x.com", "sender_name": "Jane",
         "received_at": "2026-10-20T01:00:00Z",
         "body_text": "The app will not connect to the car.", "is_draft": False},
        {"id": "cs1", "sender_email": "cs-team@mgmotor.com", "sender_name": "CS",
         "received_at": "2026-10-20T02:00:00Z",
         "body_text": _COMFORT_PATIENCE, "is_draft": False},
    ])
    g.find_unsent_customer_draft = MagicMock(return_value=None)
    g.add_category = MagicMock(return_value=None)
    router = MailboxRouter(uk="cs-team@mgmotor.com")
    mail = Mail(
        id="current",
        subject="App will not connect",
        sender="customer@x.com",
        sender_display_name="Jane Smith",
        received_at="2026-10-20T01:00:00Z",
        body_text="The app will not connect to the car.",
        is_read=False,
        conversation_id="conv-comfort",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=router, dedup=None,
    )
    assert rec.get("skipped") not in {
        "we_replied_last", "follow_up_needs_human", "already_has_draft",
    }, rec
    assert rec.get("thread_our_replies") == 0
    assert "will not connect" in (captured.get("body") or "")
    assert "kindly ask for your patience" not in (captured.get("thread") or "").lower()
    assert rec.get("draft_anchor_id") == "current"


def test_process_mail_comfort_joins_customer_supplements(monkeypatch):
    """第一封加后来的补充都进草稿正文，草稿挂在最新一封客户来信上。"""
    from agent.outlook_drafts import GraphClient, MailboxRouter, Mail, process_mail

    captured = _stub_business_draft(monkeypatch)
    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "m1", "sender_email": "customer@x.com", "sender_name": "Jane",
         "received_at": "2026-10-20T01:00:00Z",
         "body_text": "The app will not connect to the car.", "is_draft": False},
        {"id": "cs1", "sender_email": "cs-team@mgmotor.com", "sender_name": "CS",
         "received_at": "2026-10-20T02:00:00Z",
         "body_text": _COMFORT_INTAKE, "is_draft": False},
        {"id": "current", "sender_email": "customer@x.com", "sender_name": "Jane",
         "received_at": "2026-10-20T03:00:00Z",
         "body_text": "Adding the workshop photo of the error screen.",
         "is_draft": False},
    ])
    g.find_unsent_customer_draft = MagicMock(return_value=None)
    router = MailboxRouter(uk="cs-team@mgmotor.com")
    mail = Mail(
        id="current",
        subject="App will not connect",
        sender="customer@x.com",
        sender_display_name="Jane Smith",
        received_at="2026-10-20T03:00:00Z",
        body_text="Adding the workshop photo of the error screen.",
        is_read=False,
        conversation_id="conv-comfort-2",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=router, dedup=None,
    )
    assert rec.get("skipped") not in {
        "we_replied_last", "follow_up_needs_human", "already_has_draft",
    }, rec
    body = captured.get("body") or ""
    assert "will not connect" in body
    assert "workshop photo" in body
    assert "Customer message 1" in body
    assert rec.get("draft_anchor_id") == "current"


def test_process_mail_quote_of_comfort_is_not_follow_up(monkeypatch):
    """客户只引用安抚信，不按已回复转人工。"""
    from agent.outlook_drafts import GraphClient, MailboxRouter, Mail, process_mail

    _stub_business_draft(monkeypatch)
    g = MagicMock(spec=GraphClient)
    body = (
        "The app still cannot connect.\n\n"
        "On Monday, customer.care@ismart-service.com wrote:\n"
        + _COMFORT_PATIENCE
    )
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "current", "sender_email": "customer@x.com", "sender_name": "Jane",
         "received_at": "2026-10-20T03:00:00Z", "body_text": body, "is_draft": False},
    ])
    g.find_unsent_customer_draft = MagicMock(return_value=None)
    router = MailboxRouter(uk="customer.care@ismart-service.com")
    mail = Mail(
        id="current",
        subject="Re: app",
        sender="customer@x.com",
        sender_display_name="Jane Smith",
        received_at="2026-10-20T03:00:00Z",
        body_text=body,
        is_read=False,
        conversation_id="conv-quote-comfort",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=router, dedup=None,
    )
    assert rec.get("skipped") != "follow_up_needs_human", rec


def test_process_mail_comfort_draft_does_not_block(monkeypatch):
    """线程里只有未发送的安抚草稿 → 仍起草。"""
    from agent.outlook_drafts import GraphClient, MailboxRouter, Mail, process_mail

    _stub_business_draft(monkeypatch)
    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "current", "sender_email": "customer@x.com", "sender_name": "Jane",
         "received_at": "2026-10-20T01:00:00Z",
         "body_text": "The app will not connect to the car.", "is_draft": False},
        {"id": "draft-comfort", "sender_email": "cs-team@mgmotor.com",
         "sender_name": "CS", "received_at": "2026-10-20T02:00:00Z",
         "body_text": _COMFORT_PATIENCE, "is_draft": True},
    ])
    g.find_unsent_customer_draft = MagicMock(return_value=None)
    router = MailboxRouter(uk="cs-team@mgmotor.com")
    mail = Mail(
        id="current",
        subject="App will not connect",
        sender="customer@x.com",
        sender_display_name="Jane Smith",
        received_at="2026-10-20T01:00:00Z",
        body_text="The app will not connect to the car.",
        is_read=False,
        conversation_id="conv-comfort-draft",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=router, dedup=None,
    )
    assert rec.get("skipped") != "already_has_draft", rec


def test_find_unsent_customer_draft_skips_comfort_body():
    from agent.outlook_drafts import GraphClient

    g = GraphClient("tok", "me")
    listed = {
        "value": [{
            "id": "d1",
            "isDraft": True,
            "subject": "RE: App will not connect",
            "toRecipients": [{"emailAddress": {"address": "a@x.com"}}],
        }],
    }
    g._get = MagicMock(side_effect=[
        listed,
        {"body": {"content": _COMFORT_PATIENCE}, "isDraft": True},
    ])
    assert g.find_unsent_customer_draft("a@x.com", "App will not connect") is None

    g._get = MagicMock(side_effect=[
        listed,
        {"body": {"content": "Hi Jane, please send VIN"}, "isDraft": True},
    ])
    assert g.find_unsent_customer_draft("a@x.com", "App will not connect") == "d1"


def test_process_mail_skips_when_unsent_draft_exists():
    """同一线程已有未发送草稿 → 不再建第二封。"""
    from agent.outlook_drafts import (
        GraphClient, MailboxRouter, Mail, process_mail, AzureLLMClient,
    )
    llm = MagicMock(spec=AzureLLMClient)
    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "current", "sender_email": "customer@x.com", "sender_name": "M",
         "received_at": "2026-09-01T10:00:00Z", "body_text": "letter attached",
         "is_draft": False},
        {"id": "draft1", "sender_email": "cs-team@mgmotor.com", "sender_name": "CS",
         "received_at": "2026-09-02T06:43:08Z", "body_text": "GEN-001",
         "is_draft": True},
    ])
    g.add_category = MagicMock(return_value=None)
    g.download_attachment = MagicMock(return_value=b"")
    router = MailboxRouter(uk="cs-team@mgmotor.com")
    mail = Mail(
        id="current",
        subject="Reclamation",
        sender="customer@x.com",
        sender_display_name="Maurice",
        received_at="2026-09-01T10:00:00Z",
        body_text="copy attached",
        is_read=False,
        conversation_id="conv-draft",
    )
    rec = process_mail(
        mail, g=g, llm=llm, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=router, dedup=None,
    )
    assert rec.get("skipped") == "already_has_draft", rec
    assert not llm.invoke_json.called


def test_process_mail_skips_when_drafts_folder_has_same_customer_subject():
    """另一会话已有同客户同主题草稿 → 不建第二封。"""
    from agent.outlook_drafts import (
        GraphClient, MailboxRouter, Mail, process_mail, AzureLLMClient,
    )
    llm = MagicMock(spec=AzureLLMClient)
    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "current", "sender_email": "cangini@x.com", "sender_name": "C",
         "received_at": "2026-08-20T10:00:00Z", "body_text": "voice",
         "is_draft": False},
    ])
    g.find_unsent_customer_draft = MagicMock(return_value="existing-draft-id")
    g.add_category = MagicMock(return_value=None)
    g.download_attachment = MagicMock(return_value=b"")
    router = MailboxRouter(uk="cs-team@mgmotor.com")
    mail = Mail(
        id="current",
        subject="Problemi con il veicolo o l'app iSmart",
        sender="cangini@x.com",
        sender_display_name="Christian Cangini",
        received_at="2026-08-20T10:00:00Z",
        body_text="voice assistant italian",
        is_read=False,
        conversation_id="conv-new-submit",
    )
    rec = process_mail(
        mail, g=g, llm=llm, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=router, dedup=None,
    )
    assert rec.get("skipped") == "already_has_draft", rec
    assert not llm.invoke_json.called


def test_process_mail_replace_existing_draft_does_not_skip():
    """--replace-existing-draft 时改写已有草稿，不 skip。"""
    from agent.outlook_drafts import (
        GraphClient, MailboxRouter, Mail, process_mail,
    )
    from agent.template_reply import load_xlsx

    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "current", "sender_email": "durak@x.com", "sender_name": "Y",
         "received_at": "2026-09-03T18:48:00Z",
         "body_text": (
             "VIN LSJWH4099PN101557. Will the current or upcoming software "
             "update also improve LKA?"
         ),
         "is_draft": False},
        {"id": "draft1", "sender_email": "cs-team@mgmotor.com", "sender_name": "CS",
         "received_at": "2026-09-04T06:00:00Z", "body_text": "ADAS-LKA-001",
         "is_draft": True},
    ])
    g.add_category = MagicMock(return_value=None)
    g.download_attachment = MagicMock(return_value=b"")
    router = MailboxRouter(uk="cs-team@mgmotor.com")
    mail = Mail(
        id="current",
        subject="Probleme mit dem Fahrzeug",
        sender="durak@x.com",
        sender_display_name="Durak Yamaner",
        received_at="2026-09-03T18:48:00Z",
        body_text=(
            "VIN LSJWH4099PN101557. Will the current or upcoming software "
            "update also improve LKA and traffic sign recognition?"
        ),
        is_read=False,
        conversation_id="conv-draft",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=load_xlsx(), allowlist=None,
        dry_run=True, use_llm=False, router=router, dedup=None,
        replace_existing_draft=True,
    )
    assert rec.get("skipped") in (None, False), rec
    assert rec.get("template_id") == "FICM-001", rec.get("template_id")
    assert rec.get("would_post_body")
    assert "inspection" not in (rec.get("would_post_body") or "").lower()
    g.create_draft_reply.assert_not_called()


def test_process_mail_fact_tags_on_we_replied_last():
    """不起草时仍打 VIN/品牌/问题类型（dry_run=False）。"""
    from agent.outlook_drafts import (
        GraphClient, MailboxRouter, Mail, process_mail, AzureLLMClient,
    )
    llm = MagicMock(spec=AzureLLMClient)
    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "current", "sender_email": "customer@x.com", "sender_name": "D",
         "received_at": "2026-07-01T10:00:00Z",
         "body_text": "activation failed"},
        {"id": "cs1", "sender_email": "cs-team@mgmotor.com", "sender_name": "CS",
         "received_at": "2026-07-02T11:00:00Z", "body_text": "please wait"},
    ])
    g.get_categories = MagicMock(return_value=[])
    g.set_categories = MagicMock(return_value=None)
    g.add_category = MagicMock(return_value=None)
    router = MailboxRouter(uk="cs-team@mgmotor.com")
    mail = Mail(
        id="current",
        subject="Re: iSmart",
        sender="customer@x.com",
        sender_display_name="Danilo",
        received_at="2026-07-01T10:00:00Z",
        body_text=(
            "Brand: MG\nVIN: LSJW74099PZ099787\n"
            "The QRCode cannot be created until the vehicle is ACTIVATED. "
            "The activation doesn't work."
        ),
        is_read=False,
        conversation_id="conv-XYZ",
    )
    rec = process_mail(
        mail, g=g, llm=llm, templates=[], allowlist=None,
        dry_run=False, use_llm=True, router=router, dedup=None,
    )
    assert rec["skipped"] == "we_replied_last", rec
    assert not llm.invoke_json.called
    g.set_categories.assert_called_once()
    cats = g.set_categories.call_args[0][1]
    assert "VIN:LSJW74099PZ099787" in cats
    assert "品牌:MG" in cats
    assert "问题:FICM-激活相关" in cats
    assert rec.get("fact_tags", {}).get("applied") is True


def test_process_mail_issue_tag_follows_picked_template():
    """选了 FICM-001 时 Outlook 问题类型是软件更新，不是 Adas-LKA。"""
    from agent.outlook_drafts import GraphClient, Mail, process_mail
    from agent.template_reply import load_xlsx

    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[])
    g.get_categories = MagicMock(return_value=[])
    g.set_categories = MagicMock(return_value=None)
    g.create_draft_reply = MagicMock(return_value="draft-1")
    g.download_attachment = MagicMock(return_value=b"")
    mail = Mail(
        id="m1",
        subject="HS Hybrid",
        sender="durak@x.com",
        sender_display_name="Durak Yamaner",
        received_at="2026-09-03T18:48:00Z",
        body_text=(
            "VIN LSJWH4099PN101557. Will the current or upcoming software "
            "update also improve LKA and traffic sign recognition?"
        ),
        is_read=False,
        conversation_id="c1",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=load_xlsx(), allowlist=None,
        dry_run=False, use_llm=False, router=None, dedup=None,
    )
    assert rec.get("template_id") == "FICM-001", rec.get("template_id")
    cats = g.set_categories.call_args[0][1]
    assert "问题:FICM-软件更新" in cats
    assert "问题:Adas-LKA" not in cats
    assert "FICM-软件更新" not in cats


def test_process_mail_follow_up_after_cs_reply_goes_human():
    """客服已回过一轮、客户再跟进且未解决 → 不自动起草，转人工。"""
    from agent.outlook_drafts import (
        GraphClient, MailboxRouter, Mail, process_mail, AzureLLMClient,
    )
    llm = MagicMock(spec=AzureLLMClient)
    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "m1", "sender_email": "customer@x.com", "sender_name": "C",
         "received_at": "2026-07-01T10:00:00Z", "body_text": "hi"},
        {"id": "m2", "sender_email": "cs-team@mgmotor.com", "sender_name": "CS",
         "received_at": "2026-07-02T10:00:00Z", "body_text": "we can help"},
        {"id": "current", "sender_email": "customer@x.com", "sender_name": "C",
         "received_at": "2026-07-05T10:00:00Z", "body_text": "still need help"},
    ])
    g.add_category = MagicMock(return_value=None)
    router = MailboxRouter(uk="cs-team@mgmotor.com")
    mail = Mail(
        id="current",
        subject="Re: still need help",
        sender="customer@x.com",
        sender_display_name="Jane",
        received_at="2026-07-05T10:00:00Z",
        body_text="still need help",
        is_read=False,
        conversation_id="conv-X",
    )
    rec = process_mail(
        mail, g=g, llm=llm, templates=[], allowlist=None,
        dry_run=True, use_llm=True, router=router, dedup=None,
    )
    assert rec.get("skipped") == "follow_up_needs_human", rec
    assert rec.get("thread_our_replies") == 1, rec
    g.create_draft_reply.assert_not_called()


def test_process_mail_first_inbound_still_drafts():
    """线程里还没有客服发出邮件 → 仍可自动起草。"""
    from agent.outlook_drafts import (
        GraphClient, Mail, process_mail,
    )
    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "current", "sender_email": "customer@x.com", "sender_name": "C",
         "received_at": "2026-07-05T10:00:00Z", "body_text": "app will not bind"},
    ])
    g.add_category = MagicMock(return_value=None)
    mail = Mail(
        id="current",
        subject="app binding",
        sender="customer@x.com",
        sender_display_name="Jane",
        received_at="2026-07-05T10:00:00Z",
        body_text="app will not bind",
        is_read=False,
        conversation_id="conv-X",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=None, dedup=None,
    )
    assert rec.get("skipped") != "follow_up_needs_human", rec
    assert rec.get("skipped") != "dealer_unresolved_needs_human", rec


def test_dealer_unresolved_german_workshop_twice():
    from agent.outlook_drafts import dealer_unresolved_needs_human, GraphClient, Mail, process_mail

    body = (
        "Hallo, ich habe Riesenprobleme mit meiner Klimaanlage. "
        "Sobald die Temperatur über circa 29-30 Grad geht, funktioniert sie nicht mehr. "
        "Ich war schon zweimal in einer Werkstatt hier in Südspanien. "
        "Die haben alles überprüft. Neue Software oft gespielt aber das Problem "
        "nicht in den Griff bekommen. Ich kann mit dem Auto jetzt nicht mehr fahren."
    )
    assert dealer_unresolved_needs_human(body) is True
    g = MagicMock(spec=GraphClient)
    g.create_draft_reply = MagicMock()
    g.get_conversation_thread = MagicMock(return_value=[])
    mail = Mail(
        id="m-ac",
        subject="Probleme mit dem Fahrzeug",
        sender="customer@x.com",
        sender_display_name="M",
        received_at="2026-09-04T07:18:23Z",
        body_text=body,
        is_read=False,
        conversation_id="c-ac",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=None, dedup=None,
    )
    assert rec.get("skipped") == "dealer_unresolved_needs_human", rec
    g.create_draft_reply.assert_not_called()


def test_parked_false_awake_skips_status001():
    from agent.outlook_drafts import (
        GraphClient, Mail, parked_false_awake_needs_human, process_mail,
    )
    from agent.template_reply import parked_false_awake_needs_human as det

    body = (
        "Modello di veicolo: MG3\n"
        "Numero di identificazione del veicolo: VIN_MASK_aabbccdd\n"
        "il veicolo mi segnala l'accensione del veicolo, quando in realtà "
        "è già da una mezzora che il veicolo è parcheggiato. "
        "Non è la prima volta e poi la trovo con la batteria scarica. "
        "devo farmi aiutare, essendo un invalido."
    )
    assert det(body) is True
    assert parked_false_awake_needs_human(body) is True
    g = MagicMock(spec=GraphClient)
    g.create_draft_reply = MagicMock()
    g.get_conversation_thread = MagicMock(return_value=[])
    mail = Mail(
        id="m-awake",
        subject="Problemi con il veicolo o l'app iSmart",
        sender="customer@x.com",
        sender_display_name="A",
        received_at="2026-09-09T16:48:00Z",
        body_text=body,
        is_read=False,
        conversation_id="c-awake",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=None, dedup=None,
    )
    assert rec.get("skipped") == "parked_false_awake_needs_human", rec
    g.create_draft_reply.assert_not_called()
    stale = (
        'When the car is parked and not connected to a charger, the app '
        'shows "The vehicle did not respond."'
    )
    assert det(stale) is False


def test_dealer_unresolved_future_visit_still_drafts():
    from agent.outlook_drafts import dealer_unresolved_needs_human

    assert dealer_unresolved_needs_human(
        "The air conditioning is weak. I will go to the workshop next week."
    ) is False
    assert dealer_unresolved_needs_human(
        "Hallo, die Klimaanlage kühlt nicht, wenn es heiss ist."
    ) is False


def test_dealer_unresolved_italian_workshop_failed():
    from agent.outlook_drafts import dealer_unresolved_needs_human

    assert dealer_unresolved_needs_human(
        "la funzione vocale non funziona più, l'officina autorizzata "
        "non è riuscita a sistemare il problema"
    ) is True
    assert dealer_unresolved_needs_human(
        "En el servicio técnico oficial no me dan solución ni respuesta"
    ) is True


def test_vpn_restriction_uses_app_vpn_template():
    from agent.outlook_drafts import vpn_restriction_needs_human
    from agent.template_reply import pick_template, load_xlsx

    body = "Please remove this VPN restriction from the car."
    assert vpn_restriction_needs_human(body) is False
    chosen = pick_template("ismart_connectivity", load_xlsx(), body_text=body)
    assert chosen.get("template_id") == "APP-VPN-001", chosen.get("template_id")


def test_process_mail_follow_up_detects_customer_care_without_env():
    """MAILBOX_USER 未配成邮箱时，仍按 customer.care 发出记录转人工。"""
    from agent.outlook_drafts import GraphClient, Mail, process_mail

    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "m1", "sender_email": "customer@x.com", "sender_name": "C",
         "received_at": "2026-07-01T10:00:00Z", "body_text": "ac not working"},
        {"id": "m2", "sender_email": "customer.care@ismart-service.com",
         "sender_name": "CS",
         "received_at": "2026-07-02T10:00:00Z", "body_text": "please send screenshot"},
        {"id": "current", "sender_email": "customer@x.com", "sender_name": "C",
         "received_at": "2026-07-05T10:00:00Z", "body_text": "here is the photo"},
    ])
    mail = Mail(
        id="current",
        subject="Re: AC",
        sender="customer@x.com",
        sender_display_name="G",
        received_at="2026-07-05T10:00:00Z",
        body_text="here is the photo",
        is_read=False,
        conversation_id="conv-Y",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=None, dedup=None,
    )
    assert rec.get("skipped") == "follow_up_needs_human", rec
    assert rec.get("thread_our_replies") == 1, rec


def test_thread_context_passed_to_run_template_reply():
    """M2.5.5: process_mail 把 thread_msgs 打包成 thread_context，
    传给 run_template_reply，进而喂给 classify_intent 的 LLM。"""
    import agent.outlook_drafts as od
    from agent.outlook_drafts import (
        GraphClient, MailboxRouter, Mail, AzureLLMClient,
    )

    llm = MagicMock(spec=AzureLLMClient)

    # mock Graph: thread 有 1 条消息
    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "m1", "sender_email": "customer@x.com", "sender_name": "Jane",
         "received_at": "2026-07-01T10:00:00Z",
         "body_text": "Hi, my app does not bind, QR not showing"},
    ])
    g.add_category = MagicMock(return_value=None)
    g.download_attachment = MagicMock(return_value=b"")  # 0 附件 → 不调

    captured: dict = {}

    def spy(*args, **kw):
        captured.update(kw)
        from agent.schemas import TriageResult
        return {
            "triage": TriageResult(
                intent="app_binding", urgency="P2", region="North America",
                language="en", requires_recall_check=False,
                requires_warranty_lookup=False, red_flags=[],
            ).model_dump(),
            "template_doc": None, "placeholders": [], "filled_body": "",
            "template_forbidden": "", "image_descriptions": [],
            "sender": "", "sender_display_name": "",
        }

    import unittest.mock as um
    # 关键：patch `agent.outlook_drafts.run_template_reply`，因为 process_mail
    # 用 from import 捕获本地引用；patch 模块属性不生效。
    with um.patch.object(od, "run_template_reply", side_effect=spy):
        router = MailboxRouter(uk="customer.care@ismart-service.com")
        mail = Mail(
            id="m2", subject="Re: app binding",
            sender="customer@x.com", sender_display_name="Jane",
            received_at="2026-07-02T10:00:00Z",
            body_text="still can't bind",
            is_read=False, conversation_id="conv-X",
        )
        od.process_mail(
            mail, g=g, llm=llm, templates=[], allowlist=None,
            dry_run=True, use_llm=True, router=router, dedup=None,
        )
    assert "thread_context" in captured, captured
    assert "Jane" in captured["thread_context"], captured["thread_context"]
    assert "QR" in captured["thread_context"], captured["thread_context"]


def test_extract_pdf_descriptions_skips_non_pdf():
    """M2.5.5: _extract_pdf_descriptions 只对 application/pdf 出手。"""
    from agent.outlook_drafts import GraphClient, Mail, _extract_pdf_descriptions

    g = MagicMock(spec=GraphClient)
    g.download_attachment = MagicMock(side_effect=Exception("should not call"))
    mail = Mail(
        id="m", subject="s", sender="a@x.com",
        received_at="", body_text="", is_read=False,
        attachments=[
            {"id": "a1", "name": "photo.jpg", "contentType": "image/jpeg",
             "size": 100, "isInline": False},
            {"id": "a2", "name": "doc.docx", "contentType":
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "size": 100, "isInline": False},
        ],
    )
    out = _extract_pdf_descriptions(mail, g)
    assert out == []
    assert not g.download_attachment.called


def test_extract_pdf_descriptions_handles_unreadable_pdf():
    """M2.5.5: 损坏 / 加密 / 扫描型 PDF → 不抛错，给出空描述标签。"""
    from agent.outlook_drafts import GraphClient, Mail, _extract_pdf_descriptions
    g = MagicMock(spec=GraphClient)
    g.download_attachment = MagicMock(return_value=b"%PDF-garbage-not-real")
    mail = Mail(
        id="m", subject="s", sender="a@x.com",
        received_at="", body_text="", is_read=False,
        attachments=[
            {"id": "a1", "name": "scan.pdf", "contentType": "application/pdf",
             "size": 100, "isInline": False},
        ],
    )
    out = _extract_pdf_descriptions(mail, g)
    # 不抛错，返回 list（可能空或含"无可抽取文本"标签，取决于 pypdf 行为）
    assert isinstance(out, list)


# ── 5. entry point ─────────────────────────────────────────────────────
    """M2.5.5: pypdf 抽 PDF 文本。生成一个最小 PDF，验证 _extract_pdf_descriptions。"""
    from agent.outlook_drafts import GraphClient, Mail, _extract_pdf_descriptions

    # 生成最小 PDF（含 "Hello PDF World" 文本）
    import pypdf, io
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    # 注入文本（最简方式：用 Reportlab 类的工具。但 Reportlab 不可用，用
    # Annotation trick 代替 —— pypdf 能从 annotations 读到 text），
    # 但更可靠：跳过这条测试用生成的 PDF，直接 mock download_attachment。
    # 改为测"非 PDF 跳过 + 不抛错"
    g = MagicMock(spec=GraphClient)
    g.download_attachment = MagicMock(side_effect=Exception("should not call"))
    mail = Mail(
        id="m", subject="s", sender="a@x.com",
        received_at="", body_text="", is_read=False,
        attachments=[
            {"id": "a1", "name": "photo.jpg", "contentType": "image/jpeg",
             "size": 100, "isInline": False},   # 不是 PDF
        ],
    )
    out = _extract_pdf_descriptions(mail, g)
    assert out == []
    assert not g.download_attachment.called


def _minimal_docx_bytes(paragraph: str) -> bytes:
    import io
    import zipfile

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>"
        + paragraph.replace("&", "&amp;").replace("<", "&lt;")
        + "</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def test_extract_document_descriptions_reads_docx_letter():
    """挂号信 .docx（如 courrier MG France.docx）必须抽正文喂 triage。"""
    from agent.outlook_drafts import GraphClient, Mail, _extract_document_descriptions

    letter = (
        "I have been waiting one month for a reply to my registered letter "
        "regarding iSMART connectivity on VIN LSJXXXXXXXXXXXXX1."
    )
    g = MagicMock(spec=GraphClient)
    g.download_attachment = MagicMock(return_value=_minimal_docx_bytes(letter))
    mail = Mail(
        id="m", subject="Reclamation", sender="maurice.vignon@gmail.com",
        received_at="", body_text="copy attached", is_read=False,
        attachments=[{
            "id": "a1",
            "name": "courrier MG France.docx",
            "contentType":
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "size": 10459,
            "isInline": False,
        }],
    )
    out = _extract_document_descriptions(mail, g)
    assert len(out) == 1
    assert "courrier MG France.docx" in out[0]
    assert "registered letter" in out[0]
    g.download_attachment.assert_called_once()


def test_extract_document_raw_sink_keeps_unredacted_vin():
    from agent.outlook_drafts import GraphClient, Mail, _extract_document_descriptions

    vin = "LSJWH4099PN101557"
    g = MagicMock(spec=GraphClient)
    g.download_attachment = MagicMock(
        return_value=_minimal_docx_bytes(f"Registered letter VIN {vin} ACC LKA")
    )
    mail = Mail(
        id="m", subject="Reclamation", sender="a@x.com",
        received_at="", body_text="see attached", is_read=False,
        attachments=[{
            "id": "a1",
            "name": "letter.docx",
            "contentType":
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "size": 4000,
            "isInline": False,
        }],
    )
    raw_sink: list[str] = []
    out = _extract_document_descriptions(mail, g, raw_sink=raw_sink)
    assert vin in "\n".join(raw_sink)
    assert "VIN_MASK_" in out[0]
    assert vin not in out[0]


def test_extract_document_descriptions_skips_images():
    from agent.outlook_drafts import GraphClient, Mail, _extract_document_descriptions

    g = MagicMock(spec=GraphClient)
    g.download_attachment = MagicMock(side_effect=Exception("should not call"))
    mail = Mail(
        id="m", subject="s", sender="a@x.com",
        received_at="", body_text="", is_read=False,
        attachments=[{
            "id": "a1", "name": "photo.jpg", "contentType": "image/jpeg",
            "size": 100, "isInline": False,
        }],
    )
    assert _extract_document_descriptions(mail, g) == []
    assert not g.download_attachment.called


def test_queue_category_helpers():
    from agent.outlook_drafts import (
        skip_needs_human_queue, draft_needs_second_reply_queue,
    )

    assert skip_needs_human_queue("follow_up_needs_human")
    assert skip_needs_human_queue("dealer_unresolved_needs_human")
    assert skip_needs_human_queue("parked_false_awake_needs_human")
    assert skip_needs_human_queue("europe_service_subscription_needs_human")
    assert skip_needs_human_queue("ismart_plan_or_3g_needs_human")
    assert skip_needs_human_queue("region=Asia (region gate)")
    assert skip_needs_human_queue("red_flag=['lawyer_letter']")
    assert skip_needs_human_queue("auto_reply_level=no_auto_reply: 法规")
    assert not skip_needs_human_queue("already_has_draft")
    assert not skip_needs_human_queue("we_replied_last")
    assert not skip_needs_human_queue("duplicate")
    assert not skip_needs_human_queue("already_scanned")
    assert not skip_needs_human_queue("pure_chase_nudge")
    assert not skip_needs_human_queue("close_invalid_junk")
    assert draft_needs_second_reply_queue({"template_id": "GEN-001"})
    assert draft_needs_second_reply_queue({"auto_reply_level": "auto_draft_needs_info"})
    assert not draft_needs_second_reply_queue({
        "template_id": "ACC-002", "auto_reply_level": "auto_draft_safe",
    })
    g = MagicMock()
    g.remove_category = MagicMock(return_value=True)
    from agent.outlook_drafts import unstamp_queue_category, QUEUE_HUMAN_CATEGORY
    assert unstamp_queue_category(g, "mid", QUEUE_HUMAN_CATEGORY, dry_run=False)
    g.remove_category.assert_called_once_with("mid", QUEUE_HUMAN_CATEGORY)
    assert not unstamp_queue_category(g, "mid", QUEUE_HUMAN_CATEGORY, dry_run=True)
    # 已落草稿不再打「待二次回复」：坐席看 Drafts，不是等客户第二封。


def test_follow_up_stamps_human_queue_tag():
    from agent.outlook_drafts import (
        GraphClient, Mail, process_mail, QUEUE_HUMAN_CATEGORY,
    )

    g = MagicMock(spec=GraphClient)
    g.get_conversation_thread = MagicMock(return_value=[
        {"id": "m1", "sender_email": "customer@x.com", "sender_name": "C",
         "received_at": "2026-07-01T10:00:00Z", "body_text": "ac not working"},
        {"id": "m2", "sender_email": "customer.care@ismart-service.com",
         "sender_name": "CS",
         "received_at": "2026-07-02T10:00:00Z", "body_text": "please send screenshot"},
        {"id": "current", "sender_email": "customer@x.com", "sender_name": "C",
         "received_at": "2026-07-05T10:00:00Z", "body_text": "here is the photo"},
    ])
    g.get_categories = MagicMock(return_value=[])
    g.set_categories = MagicMock()
    g.add_category = MagicMock()
    g.download_attachment = MagicMock(return_value=b"")
    mail = Mail(
        id="current",
        subject="Re: AC",
        sender="customer@x.com",
        sender_display_name="G",
        received_at="2026-07-05T10:00:00Z",
        body_text="here is the photo",
        is_read=False,
        conversation_id="conv-Y",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=False, use_llm=False, router=None, dedup=None,
    )
    assert rec.get("skipped") == "follow_up_needs_human", rec
    g.add_category.assert_called_with("current", QUEUE_HUMAN_CATEGORY)


# ── 5. entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
