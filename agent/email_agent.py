"""
EmailAgent — M1 PoC 的主循环。

流水线（与开发指南 §3 / §3.7 对齐）：

    email_body
        │
        ▼
    ┌─────────────┐
    │   Triage    │  reasoning_effort="low"     → TriageResult
    └─────┬───────┘
          │
          ▼
    ┌─────────────┐
    │   Context   │  拉客户/车辆/保修/召回 (本地 mock)
    └─────┬───────┘
          │
          ▼
    ┌─────────────┐
    │  Retrieval  │  reasoning_effort="high"    → RetrievalResult
    └─────┬───────┘      (此处 evidence_score<0.4 → 退一步，记 NEEDS_ESCALATION)
          │
          ▼
    ┌─────────────┐
    │   Drafting  │  reasoning_effort="high"    → DraftReply
    └─────┬───────┘
          │
          ▼
    ┌─────────────┐
    │ Compliance  │  reasoning_effort="high"    → ComplianceResult
    └─────┬───────┘
          │ pass=False
          ▼
    ┌─────────────┐    max 2 次，否则 escalate
    │ Reflection  │
    └─────┬───────┘
          │
          ▼
    DraftReply  ──>  送人工工作台（不做自动 send）

设计原则：

- 流水线每一步都是一个独立 LLM 调用；失败重试只重做最后一步或回退一步。
- 任何步骤出现"已无法自动解决"信号（red_flag / 证据为零 / 合规不通过且重写耗尽）
  → 抛 NeedsHumanEscalation，外层脚本转人工工作台。
- 审计日志：每次 LLM 调用都留下 role + reasoning_effort + usage（含 cached_tokens），
  后续进审计 / 成本看板。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .azure_llm import AzureLLMClient, LLMCall, ReasoningEffort, EmptyCompletionError
from .prompts import (
    TRIAGE_SYSTEM,
    RETRIEVER_SYSTEM,
    DRAFTING_SYSTEM,
    COMPLIANCE_SYSTEM,
    check_forbidden_phrasings,
    REFLECTION_SYSTEM,
    _resolve_region_key,
    build_compliance_system,
)
from .template_reply import (
    load_xlsx as _load_xlsx_templates,
    pick_template as _pick_template,
    _check_template_forbidden,
    europe_service_subscription_needs_human,
    ismart_plan_or_3g_needs_human,
    fraud_force_unbind_needs_human,
)
from .schemas import (
    ComplianceResult,
    DraftReply,
    RetrievalResult,
    TriageResult,
)
from .tools import MockCustomerCRM, MockKnowledgeBase

# M2.6: keyword pre-judge intent，给 triage 阶段当 fallback/补盲
_KW_INDEX: Optional[dict] = None
_KW_CURATED: Optional[dict[str, list[str]]] = None
# M2.6.1: curated intents 是 keyword 唯一可信的源（模板库没有 → keyword 命中
# 一定是真实客户痛点）。这些 intent 的 medium conf 也允许 override。
_KEYWORD_OVERRIDE_MIN_CONF = "high"   # 通用：只 high 才覆盖（防扳错）
_KEYWORD_OVERRIDE_CURATED_INTENTS: frozenset[str] = frozenset({
    "ismart_region_support",
    "ismart_connectivity",
    "ismart_feature_inquiry",
    "ismart_owner_change",
    "ismart_fota",
    "complaint",
    "app_suggestion",
})


def load_keyword_resources() -> tuple[dict, Optional[dict]]:
    """懒加载 keyword index + curated 短语补丁。"""
    global _KW_INDEX, _KW_CURATED
    if _KW_INDEX is None:
        from scripts.intent_keyword_classifier import _load_index
        _KW_INDEX = _load_index()
        _curated_p = Path(__file__).resolve().parent.parent / "eval" / "_intent_keyword_curated.json"
        _KW_CURATED = json.loads(_curated_p.read_text(encoding="utf-8")) if _curated_p.exists() else None
    return _KW_INDEX, _KW_CURATED


def classify_intent(
    email_body: str,
    subject: str = "",
    *,
    kw_index: Optional[dict] = None,
    curated: Optional[dict[str, list[str]]] = None,
) -> dict:
    """M2.6 hybrid: keyword pre-judge intent（替代 LLM 或与 LLM 投票）。

    Returns:
      {
        "kw_top":           dict | None,  # keyword top-1 候选 (含 template_id/score/intent)
        "kw_conf":          "high"|"medium"|"low"|"none",
        "kw_candidates":    [top-K ...],
        "should_override":  bool,         # high conf + intent != other → True
        "override_intent":  str | None,   # keyword 推荐的（"should_override" 时有效）
      }

    规则：
      - confidence="high" + intent != "other" → should_override=True
      - 否则 → 不覆盖（保留 LLM 的 triage）
    """
    if kw_index is None or curated is None:
        kw_index, curated = load_keyword_resources()
    from scripts.intent_keyword_classifier import classify as _kw_classify
    result = _kw_classify(email_body, subject, kw_index, curated=curated)
    top = result.get("top")
    conf = result.get("confidence", "none")
    # Override rules (M2.6.1):
    #   - default: high conf + intent != "other" → override
    #   - curated intent bypass: curated 的短语是我们手填的，模板库不会有
    #     这些客户的痛点措辞 → medium conf + intent 在 curated 集合里 → 也允许 override
    if top is None or top.get("intent") == "other":
        should_override = False
    elif conf == _KEYWORD_OVERRIDE_MIN_CONF:
        should_override = True
    elif (
        conf == "medium"
        and top.get("intent") in _KEYWORD_OVERRIDE_CURATED_INTENTS
    ):
        should_override = True
    else:
        should_override = False
    return {
        "kw_top":          top,
        "kw_conf":         conf,
        "kw_candidates":   result.get("candidates", []),
        "should_override": should_override,
        "override_intent": top["intent"] if should_override else None,
    }


# M1 历史常量：仅 NA 放行；保留供 M2_ROLLBACK_TO_M1 应急回退使用。
SUPPORTED_REGIONS_M1: frozenset[str] = frozenset({"North America"})


# 任何 region 都触发升人工的红牌信号（CLAUDE.md 红牌规则）
_HARD_ESCALATION_RED_FLAGS: frozenset[str] = frozenset({
    "lawyer_letter",
    "litigation_threat",
    "media_cc",
    "social_media_threat",
    "safety_incident",
})

# 阶段 1 body 关键词：客户主动行使法规权利（GDPR / UK GDPR 等删除/访问请求）。
# 这种 case 任何 region 都需人工审核——不能让机器人走模板回复。
_HARD_ESCALATION_BODY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bright\s+to\s+(be\s+forgotten|erasure|rectification|data\s+portability|object)\b", re.I),
    re.compile(r"\b(data|deletion|erasure)\s+request\b", re.I),
    re.compile(r"\b(gdpr|uk\s+gdpr)\b", re.I),
    re.compile(r"\b(data\s+protection\s+officer|dpo)\b", re.I),
    re.compile(r"\bsupervisory\s+authority\b", re.I),
    re.compile(r"\bcross[- ]border\s+(data\s+)?transfer\b", re.I),
    re.compile(r"\bsubject\s+access\s+request\b", re.I),
)


def region_gate_should_escalate(
    triage: TriageResult,
    *,
    email_body: str | None = None,
    bypass_env: str = "M1_BYPASS_REGION_GATE",
    rollback_env: str = "M2_ROLLBACK_TO_M1",
    enforce_region_env: str = "M2_ENFORCE_REGION_GATE",
) -> bool:
    """Region gate 共享判断：返回 True 表示需要升人工。

    默认（2026-09-17 拍板「模版区域限制全部打开」）：
      阶段 1（硬敏感词）：red_flags 命中或 body 命中法规请求关键词
        → 任意 region 都升人工。
      阶段 2（按大区拦）：关闭。中东 / 亚洲 / 拉美 / 非洲与四区一样可走自动草稿。
        M2_ENFORCE_REGION_GATE 不再恢复四区拦。

    应急开关：
      rollback_env（M2_ROLLBACK_TO_M1=1）→ 仅 NA 放行。
      bypass_env（M1_BYPASS_REGION_GATE=1）→ 兼容旧 eval；阶段 2 已关时无效果。

    唯一出口策略，被 email_agent.py 主循环与 outlook_drafts.py should_skip
    共用。改守门逻辑只改这里。
    """
    # 阶段 1: 硬敏感词
    if triage.red_flags and any(rf in _HARD_ESCALATION_RED_FLAGS for rf in triage.red_flags):
        return True
    if email_body:
        for pat in _HARD_ESCALATION_BODY_PATTERNS:
            if pat.search(email_body):
                return True

    # 应急回滚：仅 NA 放行
    if os.environ.get(rollback_env):
        return triage.region not in SUPPORTED_REGIONS_M1

    # 全区可走自动草稿。REG-001 仅在客户明确说不支持时命中，且为 no_auto_reply。
    # enforce_region_env 已废弃。
    _ = (bypass_env, enforce_region_env)
    return False


# gpt-5 推理模式 + strict json_schema 下，为 drafting / compliance
# 留够 reasoning + JSON 输出的余量。如果命中 length，调一次退档重试。
_RETRY_DEGRADE: dict[str, str] = {"high": "medium", "medium": "low"}

LOG = logging.getLogger(__name__)


class NeedsHumanEscalation(RuntimeError):
    """EmailAgent 已无法自动解决；外层应当路由到人工工作台。"""

    def __init__(self, reason: str, partial: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.partial = partial or {}


@dataclass
class EmailAgentResult:
    """
    跑完流水线后给工作台 / 审计用的全量结构。

    `draft` 一律是要送到人工工作台审核的草稿；M1 不主动 send。
    """

    triage: TriageResult
    retrieval: RetrievalResult
    draft: DraftReply
    compliance: ComplianceResult
    audit: dict = field(default_factory=dict)


class EmailAgent:
    def __init__(
        self,
        llm: Optional[AzureLLMClient] = None,
        crm: Optional[MockCustomerCRM] = None,
        rag: Optional[MockKnowledgeBase] = None,
        max_rewrite_attempts: int = 2,
        *,
        use_keyword_hybrid: bool = False,
        kw_index: Optional[dict] = None,
        kw_curated: Optional[dict] = None,
    ) -> None:
        self.llm = llm or AzureLLMClient()
        self.crm = crm or MockCustomerCRM()
        self.rag = rag or MockKnowledgeBase()
        self.max_rewrite_attempts = max_rewrite_attempts
        # M2.6: keyword 先判 intent + 高分覆盖 LLM
        self.use_keyword_hybrid = use_keyword_hybrid
        if use_keyword_hybrid:
            self._kw_index = kw_index if kw_index is not None else load_keyword_resources()[0]
            self._kw_curated = kw_curated if kw_curated is not None else load_keyword_resources()[1]

    # ── 对外接口 ───────────────────────────────────────────────────

    def handle_email(
        self,
        email_body: str,
        *,
        sender_hint: str | None = None,
    ) -> EmailAgentResult:
        """端到端跑一封客户邮件。失败时抛 NeedsHumanEscalation。"""
        t0 = time.time()
        audit_calls: list[dict] = []
        kw_override_info: dict = {}

        # 1) Triage ─────────────────────────────────────────────
        triage, c = self.llm.invoke_json(
            schema=TriageResult,
            messages=[
                {"role": "system", "content": TRIAGE_SYSTEM},
                {"role": "user",   "content": email_body},
            ],
            reasoning_effort="low",
            role="triage",
        )
        audit_calls.append(_as_audit(c))

        from .region_catalog import apply_to_triage
        triage = apply_to_triage(triage, email_body)

        # M2.6 hybrid: keyword 后判，高分覆盖 LLM 的 intent
        if self.use_keyword_hybrid:
            kw_res = classify_intent(
                email_body, "",
                kw_index=self._kw_index, curated=self._kw_curated,
            )
            kw_override_info = {
                "kw_top":           kw_res["kw_top"],
                "kw_conf":          kw_res["kw_conf"],
                "kw_should_override": kw_res["should_override"],
                "kw_overridden":    False,
                "kw_override_intent": None,
            }
            if (
                kw_res["should_override"]
                and triage.intent != kw_res["override_intent"]
            ):
                prev_intent = triage.intent
                triage.intent = kw_res["override_intent"]
                kw_override_info["kw_overridden"]      = True
                kw_override_info["kw_override_intent"] = kw_res["override_intent"]
                kw_override_info["kw_prev_intent"]     = prev_intent
                LOG.info(
                    "hybrid override: kw=%s (conf=%s) overrode llm=%s",
                    kw_res["override_intent"], kw_res["kw_conf"], prev_intent,
                )

        # 红牌一步升人工
        if triage.red_flags:
            raise NeedsHumanEscalation(
                f"red_flags detected: {triage.red_flags}",
                partial={"triage": triage.model_dump()},
            )

        # region gate — 共享逻辑见 region_gate_should_escalate()，outlook_drafts.py 共用。
        # 只拦阶段 1 硬敏感词；不按大区升人工。
        if region_gate_should_escalate(triage, email_body=email_body):
            region_key = _resolve_region_key(triage.region, triage.country)
            raise NeedsHumanEscalation(
                f"region {triage.region} (resolved={region_key}) → human regional team",
                partial={"triage": triage.model_dump()},
            )

        if ismart_plan_or_3g_needs_human(email_body):
            raise NeedsHumanEscalation(
                "3G / iSMART data plan (retired template) → human",
                partial={"triage": triage.model_dump()},
            )

        if fraud_force_unbind_needs_human(email_body):
            raise NeedsHumanEscalation(
                "fraud/unauthorized force-unbind → human",
                partial={"triage": triage.model_dump()},
            )

        if europe_service_subscription_needs_human(email_body):
            raise NeedsHumanEscalation(
                "Europe iSMART/connected service subscription → human",
                partial={"triage": triage.model_dump()},
            )

        # 2) Context ────────────────────────────────────────────
        context = self._build_context(email_body, sender_hint, triage)

        # 3) Retrieval ──────────────────────────────────────────
        query = _build_retrieval_query(email_body, triage, context)
        evidence = self.rag.search(query, region=triage.region)
        retrieval, c = self._retrieval_step(
            email_body=email_body,
            triage=triage,
            context=context,
            evidence=evidence,
        )
        audit_calls.append(_as_audit(c))

        # M2.5: weak-evidence retry once with hint before escalating
        if retrieval.evidence_score < 0.5 or not retrieval.cited_clauses:
            retry_retrieval, c_retry = self._retrieval_step(
                email_body=email_body,
                triage=triage,
                context=context,
                evidence=evidence,
                retry_hint=(
                    "Previous retrieval was too conservative. At least one "
                    "of the chunks above likely addresses the customer's "
                    "question. Cite the most relevant one(s), even partial "
                    "matches. Lower the evidence_score bar to 0.3 if the "
                    "chunk is tangentially relevant."
                ),
            )
            audit_calls.append(_as_audit(c_retry))
            # accept retry if it improved (cited more clauses) or scored better
            if (
                len(retry_retrieval.cited_clauses) > len(retrieval.cited_clauses)
                or retry_retrieval.evidence_score > retrieval.evidence_score
            ) and retry_retrieval.cited_clauses:
                retrieval = retry_retrieval

        if retrieval.evidence_score < 0.4 or not retrieval.cited_clauses:
            # 证据薄弱，直接升人工，不浪费草稿阶段
            raise NeedsHumanEscalation(
                "retrieval evidence too weak (score<0.4 or empty)",
                partial={
                    "triage": triage.model_dump(),
                    "retrieval": retrieval.model_dump(),
                },
            )

        # 4) Drafting ── 5) Compliance ── 6) Reflection 循环 ─────
        draft: DraftReply | None = None
        compliance: ComplianceResult | None = None
        rewrite_attempts = 0

        # M2.5.1: 取模板自带的「禁止/注意表述」（xlsx 第 11 列）。
        # 模板加载复用 template_reply.load_xlsx()——同一份 xlsx，避免 schema 分叉。
        # M2.7: 同时取 template_body，用于 LLM drafting 阶段处理
        # 【xxx】 inline 占位符（按 case 动态填充）。
        template_meta = self._load_template_meta(triage.intent, email_body=email_body)
        template_forbidden = template_meta["forbidden"]
        template_body = template_meta["body"]
        if template_meta.get("level"):
            triage.auto_reply_level = template_meta["level"]  # type: ignore[assignment]
        if template_meta.get("intent") and template_meta["intent"] != triage.intent:
            triage.intent = template_meta["intent"]  # type: ignore[assignment]

        while True:
            # 草稿
            draft, c = self._drafting_step(
                email_body=email_body,
                triage=triage,
                context=context,
                retrieval=retrieval,
                user_feedback=(
                    compliance.suggested_reply_adjustments
                    if compliance is not None else None
                ),
                template_forbidden=template_forbidden,
                template_body=template_body,
            )
            audit_calls.append(_as_audit(c))

            # M1.7: 个性化称谓 "Dear customer," → "Dear Mr./Ms. {Last},"
            # 在 LLM 起草后、合规前做。LLM prompt 不动（cache 命中稳定），
            # 仅替换 body_md 开头一次，拿不到名字时不动。
            # sender_hint 在 handle_email 入参里就传进来；fallback 到
            # email_body 里抽邮箱对应的显示名（如 "From: John Smith"）。
            if draft and draft.body_md:
                from .greeting import personalize_greeting
                sender_for_greeting = sender_hint or _extract_email_display_name(email_body) or ""
                draft.body_md = personalize_greeting(draft.body_md, sender_for_greeting)
                # subject 也常以 "Re:" 开头不带称谓，无需处理

            # 合规
            compliance, c = self._compliance_step(draft, triage)
            audit_calls.append(_as_audit(c))

            if compliance.verdict.pass_:
                break  # 通过

            if rewrite_attempts >= self.max_rewrite_attempts:
                raise NeedsHumanEscalation(
                    f"compliance failed after {rewrite_attempts} rewrites; "
                    f"violations={compliance.verdict.violations}",
                    partial={
                        "triage": triage.model_dump(),
                        "retrieval": retrieval.model_dump(),
                        "draft": draft.model_dump(),
                        "compliance": compliance.model_dump(),
                    },
                )

            rewrite_attempts += 1
            LOG.info("compliance failed → reflection round %d", rewrite_attempts)

        # 7) 完成 ─ 不发送
        elapsed = round((time.time() - t0) * 1000)
        LOG.info("EmailAgent finished in %d ms", elapsed)
        return EmailAgentResult(
            triage=triage,
            retrieval=retrieval,
            draft=draft,
            compliance=compliance,
            audit={
                "calls": audit_calls,
                "rewrite_attempts": rewrite_attempts,
                "latency_ms": elapsed,
                "template_forbidden": template_forbidden,   # M2.5.1: 哪条禁则原文进了 prompt
                **({"kw_hybrid": kw_override_info} if kw_override_info else {}),
            },
        )

    # ── 内部 step ───────────────────────────────────────────────

    def _load_template_forbidden(self, intent: str) -> str | None:
        """M2.5.1: 按 intent 挑第一条匹配模板的 xlsx「禁止/注意表述」原文。
        模板库懒加载（首次调用才读 xlsx；后续命中缓存）。
        返回 None 表示该 intent 无模板 / 模板无禁则。

        M2.7: 改为 _load_template_meta() 的薄包装，向后兼容。
        """
        return self._load_template_meta(intent)["forbidden"]

    def _load_template_meta(self, intent: str, email_body: str = "") -> dict:
        """按邮件正文 + intent 挑知识库行（与 Outlook 快路径同一套 pick_template）。"""
        if not hasattr(self, "_templates_cache"):
            self._templates_cache = _load_xlsx_templates()  # type: ignore[attr-defined]
        templates = self._templates_cache  # type: ignore[attr-defined]
        row = _pick_template(intent, templates, body_text=email_body) if templates else None
        if not row:
            return {
                "forbidden": None, "body": None, "template_id": None,
                "scenario": None, "level": None, "intent": None,
            }
        return {
            "forbidden":    (row.get("forbidden") or "").strip() or None,
            "body":         row.get("template") or None,
            "template_id":  row.get("template_id") or None,
            "scenario":     row.get("scenario") or None,
            "level":        row.get("level") or None,
            "intent":       row.get("intent") or None,
        }

    def _build_context(
        self,
        email_body: str,
        sender_hint: str | None,
        triage: TriageResult,
    ) -> dict:
        vin = _extract_vin(email_body)
        email = sender_hint or _extract_email(email_body) or ""

        customer = self.crm.lookup_customer(email) if email else None
        vehicle = self.crm.get_vehicle(vin) if vin else None

        warranty = {}
        if vin and vehicle:
            for comp in ("powertrain", "battery"):
                w = self.crm.get_warranty_status(vin, comp)
                if w:
                    warranty[comp] = w

        service_history = self.crm.get_service_history(vin) if vin else []
        recalls = self.crm.get_open_recalls(vin) if vin else []

        return {
            "customer": customer,
            "vehicle": vehicle,
            "warranty": warranty,
            "service_history": service_history,
            "recalls": recalls,
        }

    def _retrieval_step(
        self,
        *,
        email_body: str,
        triage: TriageResult,
        context: dict,
        evidence: list,
        retry_hint: str | None = None,
    ) -> tuple[RetrievalResult, LLMCall]:
        user_payload = {
            "email":     email_body,
            "triage":    triage.model_dump(),
            "context":   context,
            "evidence":  evidence,
        }
        if retry_hint:
            user_payload["retry_hint"] = retry_hint
        user_msg = json.dumps(user_payload, ensure_ascii=False, default=str)
        return self._call_with_degrade(
            schema=RetrievalResult,
            messages=[
                {"role": "system", "content": RETRIEVER_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            role="retriever",
            initial_reasoning="high",
            max_completion_tokens=8192,
        )

    def _drafting_step(
        self,
        *,
        email_body: str,
        triage: TriageResult,
        context: dict,
        retrieval: RetrievalResult,
        user_feedback: list[str] | None,
        template_forbidden: str | None = None,   # M2.5.1: xlsx 第 11 列「禁止/注意表述」
        template_body: str | None = None,        # M2.7: 模板正文（含【xxx】时用于 LLM 动态填充）
    ) -> tuple[DraftReply, LLMCall]:
        payload: dict = {
            "email":         email_body,
            "triage":        triage.model_dump(),
            "context":       context,
            "retrieval":     retrieval.model_dump(),
            "rewrite_notes": user_feedback or [],
        }
        if template_forbidden:
            # 把模板自带的禁则原文喂给 LLM；与 FORBIDDEN_PHRASINGS（F1-F8）互补
            payload["template_forbidden"] = template_forbidden
        # M2.7: 仅在含【】内 inline placeholder 时才喂模板正文给 LLM。
        # 没有时 LLM 直接基于 triage + retrieval 重写即可，避免模板 body 干扰。
        if template_body and "【" in template_body:
            payload["template_body"] = template_body
        prompt = json.dumps(payload, ensure_ascii=False, default=str)
        return self._call_with_degrade(
            schema=DraftReply,
            messages=[
                {"role": "system", "content": DRAFTING_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            role="drafting",
            initial_reasoning="high",
            max_completion_tokens=8192,
        )

    def _compliance_step(
        self,
        draft: DraftReply,
        triage: TriageResult,
    ) -> tuple[ComplianceResult, LLMCall]:
        # ── 三层 post-check ─────────────────────────────────────────
        #
        # 1. LLM compliance 调用（region-specific 系统提示，prompt 强约束）
        #    → ComplianceResult 含 verdict.pass / violations
        #
        # 2. F1-F8 regex post-check（check_forbidden_phrasings，代码写死的 8 条
        #    通用禁则）。命中 → verdict.pass_=False + violations + 触发 reflection loop。
        #
        # 3. template_forbidden post-check（_check_template_forbidden，从 xlsx
        #    第 11 列「禁止/注意表述」抽出动作短语，在 draft.body_md 里查子串）。
        #    Best-effort：xlsx forbidden 是中文，LLM draft 必为英文（CLAUDE.md
        #    硬约束），所以这条防线**仅防中文 hallucination / 中英 prompt injection**，
        #    不替代 F1-F8。命中 → verdict.pass_=False + violations + 触发 reflection loop。
        #
        # 完整防御链：模板作者写模板避开禁则 → LLM drafting 在 prompt 看到禁则 →
        # LLM compliance 在 system prompt 看到 F1-F8 → F1-F8 regex 复检 →
        # template_forbidden 复检 → 反射循环改写 → 不通过就 escalate。
        #
        # M2: 按 (region, country) 选合规规则——NA/EU/UK/OC 各一套；
        # Asia/South America/Africa 已在 region gate 阶段被拦下，到不了这里。
        system = build_compliance_system(triage.region, triage.country)
        prompt = json.dumps({
            "draft":   draft.model_dump(),
            "triage":  triage.model_dump(),
        }, ensure_ascii=False, default=str)
        compliance, c = self._call_with_degrade(
            schema=ComplianceResult,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt},
            ],
            role="compliance",
            initial_reasoning="high",
            max_completion_tokens=8192,
        )
        # M2.5: 8 条禁止表述 post-check (re_check; 即便 LLM 通过也再扫一遍)。
        # 命中 → violations 追加 + verdict.pass_=False → reflection loop 触发改写。
        forbidden_hits = check_forbidden_phrasings(draft.body_md)
        if forbidden_hits:
            compliance.verdict.pass_ = False
            compliance.verdict.violations = list(compliance.verdict.violations) + [
                f"forbidden_phrase: {h}" for h in forbidden_hits
            ]
            compliance.suggested_reply_adjustments = list(
                compliance.suggested_reply_adjustments
            ) + [
                f"Remove / rewrite per forbidden-phrasing rule {h.split(':')[0]} "
                f"(see 禁止表述 sheet in iSMART自动回复知识库_全量模板_20260729.xlsx)."
                for h in forbidden_hits
            ]
        # M2.5.1: xlsx 第 11 列 template_forbidden post-check。
        # 模板作者写模板时避开了 forbidden 短语（93/93 验证），但 LLM 起草正文不一定守 prompt。
        # 这里对 draft.body_md 做 post-check：抽 forbidden 动作短语 → body_md 是否命中 →
        # 命中就 verdict.pass_=False + 触发 reflection loop 改写。
        template_forbidden = self._load_template_forbidden(triage.intent)
        if template_forbidden:
            tf_hit = _check_template_forbidden(draft.body_md, template_forbidden)
            if tf_hit:
                compliance.verdict.pass_ = False
                compliance.verdict.violations = list(compliance.verdict.violations) + [
                    f"template_forbidden: {tf_hit}"
                ]
                compliance.suggested_reply_adjustments = list(
                    compliance.suggested_reply_adjustments
                ) + [
                    f"LLM draft 违反了模板专属禁则「{template_forbidden}」（触发了「{tf_hit}」）；"
                    f"改写时按禁则原文要求处理。",
                ]
        return compliance, c

    def _call_with_degrade(
        self,
        *,
        schema: type,
        messages: list[dict],
        role: str,
        initial_reasoning: ReasoningEffort,
        max_completion_tokens: int,
    ) -> tuple:
        """
        对 gpt-5 reasoning 系列做"length 截断自动退档"。

        第一调用按 initial_reasoning；若碰到 EmptyCompletionError 且
        finish_reason=='length' 且 reasoning 还能退一档（high→medium→low），
        自动再调用一次。
        """
        try:
            return self.llm.invoke_json(
                schema=schema, messages=messages,
                reasoning_effort=initial_reasoning,
                max_completion_tokens=max_completion_tokens,
                role=role,
            )
        except EmptyCompletionError as e:
            new_reasoning = _RETRY_DEGRADE.get(e.reasoning_effort)
            if new_reasoning is None:
                raise
            LOG.warning(
                "[%s] length-truncated at reasoning=%s; retrying with %s",
                role, e.reasoning_effort, new_reasoning,
            )
            # 也把 max_completion_tokens 拉高一档，给 reasoning 留空间
            return self.llm.invoke_json(
                schema=schema, messages=messages,
                reasoning_effort=new_reasoning,
                max_completion_tokens=max(max_completion_tokens, 12288),
                role=role,
            )


# ── helpers ────────────────────────────────────────────────────────────

_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _extract_vin(text: str) -> Optional[str]:
    m = _VIN_RE.search(text)
    return m.group(0) if m else None


def _extract_email(text: str) -> Optional[str]:
    m = _EMAIL_RE.search(text)
    return m.group(0) if m else None


def _extract_email_display_name(text: str) -> Optional[str]:
    """从 email_body 里 "From: Name <email@...>" / "Name <email@...>" 抽显示名。
    失败返回 None（与 greeting.extract_display_name 配合用）。
    """
    from .greeting import extract_display_name
    # 找 "Name <email@...>" 形式；可带 From: 前缀；group 1 是纯显示名
    m = re.search(
        r"(?:from\s*:)?\s*([^\n<>\"][^<>\n]*?)\s*<[^<>\n]+@[^<>\n]+>",
        text, re.I,
    )
    if m:
        return extract_display_name(m.group(1).strip())
    return None


def _build_retrieval_query(
    email_body: str,
    triage: TriageResult,
    context: dict,
) -> str:
    parts = [triage.intent, triage.region]
    vehicle = (context.get("vehicle") or {}).get("model")
    if vehicle:
        parts.append(vehicle)
    # 去 HTML + 取短些的 snippet 给 mock RAG（HTML 里的 fontfamily/CSS 噪音会稀释 token 命中）
    from html.parser import HTMLParser as _HP
    class _Strip(_HP):
        def __init__(self): super().__init__(convert_charrefs=True); self.parts=[]
        def handle_data(self, d): self.parts.append(d)
    s = _Strip()
    try: s.feed(email_body)
    except Exception: pass
    plain = re.sub(r"\s+", " ", "".join(s.parts)).strip()
    parts.append(plain[:300])
    return " ".join(parts)


def _as_audit(c: LLMCall) -> dict:
    return {
        "role":              c.role,
        "model":             c.model,
        "reasoning_effort":  c.reasoning_effort,
        "prompt_tokens":     c.usage.prompt_tokens,
        "completion_tokens": c.usage.completion_tokens,
        "cached_tokens":     c.usage.cached_tokens,
        "reasoning_tokens":  c.usage.reasoning_tokens,
        "cache_hit_rate":    round(c.usage.cache_hit_rate, 3),
    }
