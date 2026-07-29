"""
MockKnowledgeBase — M1 阶段对 mcp-rag 的桩。

不接 Qdrant；只是返回硬编码的"合理"政策 / TSB / 案例片段，
让 KnowledgeRetriever 能跑通端到端。M2 切真 Qdrant 时，
把这层换成对 mcp-rag.search_policy/search_tsb/search_cases 的包装即可。

覆盖范围（M1）：6 大意图各至少 1 条证据，避免"非保修类邮件一律被
retrieval 拒掉"导致 85% escalation rate 掩盖真正的 Triage 准确率。

region 标签约定：与 [agent/schemas.py:30-37](../schemas.py) 的 Region enum
全名一致 ("North America" / "Europe" / "Asia" / "South America" /
"Africa" / "Oceania")。EmailAgent 传 triage.region 直传，所以 chunks
必须用全名才能被 filter 命中（不要写 "NA" 这种缩写）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Evidence:
    doc_id: str
    section: str
    quote: str
    metadata: dict


# M2 RAG 增强：CS 历史回复 chunks
# 文件不在项目内（在 ~/Documents/private/iSmart-email-ledger/）→ 缺则 silent fallback
_CS_CHUNKS_PATH = Path.home() / "Documents" / "private" / "iSmart-email-ledger" / "cs_chunks.jsonl"
_CS_CHUNKS_CACHE: list[Evidence] | None = None


def _load_cs_chunks() -> list[Evidence]:
    """懒加载：首次调 search() 时拉一次。

    文件缺失或解析失败 → 空 list（mock_rag 回到 M1 行为）。
    """
    global _CS_CHUNKS_CACHE
    if _CS_CHUNKS_CACHE is not None:
        return _CS_CHUNKS_CACHE
    chunks: list[Evidence] = []
    if _CS_CHUNKS_PATH.exists():
        try:
            with _CS_CHUNKS_PATH.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    chunks.append(Evidence(
                        doc_id=obj["doc_id"],
                        section=obj.get("section", ""),
                        quote=obj["quote"],
                        metadata=obj.get("metadata", {}),
                    ))
        except Exception:
            chunks = []
    _CS_CHUNKS_CACHE = chunks
    return chunks


# 故意做得很逼真：覆盖 warranty / tsb / case / recall / service / parts /
# complaint / press 各至少 1 条
_POLICY_CHUNKS: List[Evidence] = [
    # ── warranty ────────────────────────────────────────────
    Evidence(
        doc_id="WARRANTY-NA-2024",
        section="US-2024-7.3",
        quote=(
            "Powertrain components (electric drive unit, reduction gear, "
            "power electronics) are covered for 8 years or 100,000 miles, "
            "whichever comes first. Diagnostic at an authorized service "
            "center is required to confirm coverage prior to repair."
        ),
        metadata={"region": "North America", "type": "warranty"},
    ),
    Evidence(
        doc_id="BATTERY-NA-2024",
        section="US-2024-12.1",
        quote=(
            "High-voltage battery modules are warranted against capacity "
            "loss below 70% of nominal for 8 years or 100,000 miles. "
            "Customer complaints of range loss must be documented through "
            "an authorized service center diagnostic before any claim is "
            "approved."
        ),
        metadata={"region": "North America", "type": "warranty"},
    ),
    # ── tsb ─────────────────────────────────────────────────
    Evidence(
        doc_id="TSB-AURORA-EV-2025-04",
        section="TSB-EV-25-04",
        quote=(
            "Some 2023 model year Aurora EV SUVs may report intermittent "
            "regen-braking hesitation in cold-weather operation below "
            "0C. Soak-time diagnostic at dealer is required. Do NOT "
            "attribute to battery degradation without measurement."
        ),
        metadata={"region": "North America", "type": "tsb"},
    ),
    Evidence(
        doc_id="TSB-AURORA-EV-2025-09",
        section="TSB-EV-25-09",
        quote=(
            "Aurora EV charge port door latch may fail to fully close "
            "after extended use. Replace latch assembly under standard "
            "warranty terms; expected repair time 30 minutes."
        ),
        metadata={"region": "North America", "type": "tsb"},
    ),
    Evidence(
        doc_id="TSB-AURORA-EV-2026-01",
        section="TSB-EV-26-01",
        quote=(
            "Some 2024 Aurora EVs may report 'charging fault' after DC fast "
            "charge. Run OTA update to BMS firmware 4.2.1 before scheduling "
            "any service visit."
        ),
        metadata={"region": "North America", "type": "tsb"},
    ),
    # ── case ────────────────────────────────────────────────
    Evidence(
        doc_id="CASE-2025-1187",
        section="case_summary",
        quote=(
            "Similar 2023 Aurora EV customer complaint (range drop ~15% "
            "in two months) resolved by HVAC recirculation software "
            "calibration update at 2025-06. Customer confirmed resolution "
            "after 11 days."
        ),
        metadata={"region": "North America", "type": "case"},
    ),
    # ── recall notice ────────────────────────────────────────
    Evidence(
        doc_id="NHTSA-2024-187",
        section="recall_summary",
        quote=(
            "Recall NHTSA-2024-187: certain 2024 Aurora EV vehicles have a "
            "front passenger airbag inflator that may need inspection. "
            "Affected VIN range is public; refer owner to dealer diagnostic."
        ),
        metadata={"region": "North America", "type": "recall"},
    ),
    # ── service booking ─────────────────────────────────────
    Evidence(
        doc_id="SVC-BOOKING-NA-2025",
        section="appointment_policy",
        quote=(
            "Service appointments may be booked online at the authorized "
            "dealer's service portal, or by calling the dealer directly. "
            "Recall-related inspections are free of charge under all terms."
        ),
        metadata={"region": "North America", "type": "service"},
    ),
    Evidence(
        doc_id="SVC-MAINT-NA-2025",
        section="maintenance_intervals",
        quote=(
            "Aurora EV recommended maintenance intervals: tire rotation "
            "every 7,500 miles; brake fluid replacement every 3 years; "
            "annual multi-point inspection. Owners may combine multiple "
            "services in one visit."
        ),
        metadata={"region": "North America", "type": "service"},
    ),
    # ── parts ───────────────────────────────────────────────
    Evidence(
        doc_id="PARTS-CATALOG-NA-2025",
        section="part_lookup_policy",
        quote=(
            "Genuine Aurora EV parts can be ordered from any authorized "
            "parts department. Owners should provide the 8-character part "
            "SKU from the owner's manual; aftermarket alternatives are "
            "out of OEM warranty but compatible."
        ),
        metadata={"region": "North America", "type": "parts"},
    ),
    Evidence(
        doc_id="ACC-AURORA-EV-2024",
        section="accessory_catalog",
        quote=(
            "Aurora EV accessories include 2-inch tow hitch (rating 5000 "
            "lb / 750 lb tongue), all-weather floor mats, roof rack "
            "(dynamic 165 lb / static 600 lb). All OEM accessories carry "
            "a 3-year accessory warranty."
        ),
        metadata={"region": "North America", "type": "parts"},
    ),
    # ── complaint handling ──────────────────────────────────
    Evidence(
        doc_id="COMPLAINT-POLICY-NA-2024",
        section="complaint_resolution",
        quote=(
            "Customer complaints about dealer conduct, vehicle quality, "
            "or service delays are logged with a Case number. Owners are "
            "entitled to a written response within 5 business days; "
            "dealer service managers must acknowledge any damage claim "
            "within 24 hours."
        ),
        metadata={"region": "North America", "type": "complaint"},
    ),
    # ── press / off-topic ───────────────────────────────────
    Evidence(
        doc_id="MEDIA-ROUTING-2025",
        section="media_routing",
        quote=(
            "All press and PR inquiries must be routed to the OEM press "
            "office (press@example.com). After-sales support does not "
            "respond to interview requests directly."
        ),
        metadata={"region": "North America", "type": "policy"},
    ),

    # ── iSmart App / 平台（NA-flavoured evidence for full-eval bypass）──
    # 来源：iSMART售后服务邮箱回复方法_20260308.xlsx 的 NA 视角翻译。
    # 仅在 M1_BYPASS_RAG_REGION=1 时被非 NA 真样本命中，
    # 用于在 v6 full eval 上验证 retrieval/drafting/compliance 端到端。
    Evidence(
        doc_id="ISMART-APP-BIND-NA-2026",
        section="binding_workflow",
        quote=(
            "iSMART app binding workflow: customer must accept all Privacy "
            "Policy options and confirm correct date, time and time zone on "
            "the vehicle infotainment before scanning the QR code under Add "
            "Vehicle. If the QR code does not display, the vehicle activation "
            "may not be completed; instruct customer to navigate Settings, "
            "General, Activation, Activate. After activation, restart the "
            "infotainment system and try binding again."
        ),
        metadata={"region": "North America", "type": "policy", "intent": "app_binding"},
    ),
    Evidence(
        doc_id="ISMART-APP-VEHICLE-CTRL-NA-2026",
        section="vehicle_status_sync",
        quote=(
            "Vehicle status sync between app and FICM depends on cellular "
            "connectivity at the vehicle location. If status appears stale "
            "or incomplete, customers should verify: vehicle is in cellular "
            "coverage area, Privacy Policy accepted, infotainment time and "
            "date correct. A re-bind may be required if a factory reset was "
            "performed. Diagnostic at an authorized service center is the "
            "fallback when these checks do not resolve the issue."
        ),
        metadata={"region": "North America", "type": "policy", "intent": "app_vehicle_control"},
    ),
    Evidence(
        doc_id="ISMART-APP-DIGITAL-KEY-NA-2026",
        section="digital_key_setup",
        quote=(
            "Digital Key setup requires a Bluetooth-paired primary account, "
            "an active iSMART app session, and vehicle firmware 2.4.0 or "
            "later. If the customer reports keys locked inside the vehicle, "
            "advise calling roadside assistance immediately; do NOT advise "
            "breaking in or attempting improvised entry. Spare key "
            "logistics are the customer's responsibility."
        ),
        metadata={"region": "North America", "type": "policy", "intent": "ismart_digital_key"},
    ),
    Evidence(
        doc_id="ISMART-DATA-PLAN-NA-2026",
        section="data_plan_tiers",
        quote=(
            "iSMART data plan is included for the first 3 years of vehicle "
            "ownership on the small tier, approximately 2 GB per month. After "
            "year 3 the plan reverts to the small tier by default; owners "
            "may upgrade to the medium or large tier through the iSMART app "
            "under Account, Plan, Change. Customers unable to locate this "
            "option should be routed to regional billing support; do not "
            "promise refunds or extensions via this channel."
        ),
        metadata={"region": "North America", "type": "policy", "intent": "ismart_data_plan"},
    ),
    Evidence(
        doc_id="ISMART-ACCOUNT-NA-2026",
        section="secondary_account",
        quote=(
            "iSMART supports one primary account and up to two secondary "
            "accounts per vehicle. Secondary accounts may be authorized "
            "from the primary account under Account, Family. A secondary "
            "account does NOT need to bind the vehicle separately if "
            "invited through the Family flow. Account or password reset "
            "requests should reference the registered email; PIN code "
            "requests should reference the security code shown on the "
            "vehicle infotainment."
        ),
        metadata={"region": "North America", "type": "policy", "intent": "ismart_account_inquiry"},
    ),
    Evidence(
        doc_id="ISMART-FEATURE-NA-2026",
        section="feature_availability",
        quote=(
            "iSMART feature availability varies by model year and region. "
            "Common customer inquiries include changing system language "
            "Settings General Language, viewing trip statistics Account "
            "Trips, and updating the app through the App Store or Play "
            "Store. Trip statistics, FOTA and Digital Key features may not "
            "be available on older model years; consult the model matrix "
            "before promising capability."
        ),
        metadata={"region": "North America", "type": "policy", "intent": "ismart_feature_inquiry"},
    ),
]


class MockKnowledgeBase:
    """M1 的"知识库"。M2 会被 Qdrant + bge-reranker 替换。

    M2.5: 合并加载 [~/Documents/private/iSmart-email-ledger/cs_chunks.jsonl](file://~/Documents/private/iSmart-email-ledger/cs_chunks.jsonl)
    （scripts/build_cs_chunks.py 切片自 outbox.csv 客服回复）。
    文件缺则 silent fallback 到 _POLICY_CHUNKS 单源行为。
    """

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        region: str = "North America",  # 必须用 Region enum 全名；旧 "NA" 缩写会导致 region filter `!=` 永远 True
        doc_type: Optional[str] = None,
    ) -> List[dict]:
        # M1_BYPASS_RAG_REGION=1: 忽略 region 过滤，让非 NA 真样本在
        # full eval 中也能命中 NA-flavoured iSmart evidence。
        # 仅用于 M1 验证 retrieval/drafting/compliance 端到端；
        # 真实生产路径里这个 env var 不应被设置。
        bypass_region = bool(os.environ.get("M1_BYPASS_RAG_REGION"))

        # M2.5: 合并 policy chunks + CS chunks（CS chunks 是 EU/UK/OC
        # 真实案例片段，NA-only 的 policy chunks 在 EU/UK 邮件上 0 hit）。
        all_chunks = _POLICY_CHUNKS + _load_cs_chunks()

        # split by whitespace AND on "_" so triage.intent="app_binding" → ["app", "binding"]
        raw = query.replace("_", " ").split()
        tokens = [t.lower() for t in raw if len(t) > 2]
        scored: List[tuple[int, Evidence]] = []
        for ev in all_chunks:
            if not bypass_region and region and ev.metadata.get("region") != region:
                continue
            if doc_type and ev.metadata.get("type") != doc_type:
                continue
            score = sum(1 for tok in tokens if tok in ev.quote.lower())
            if score > 0:
                scored.append((score, ev))
        # 没有命中 → 返回该 region 内全部 chunk（让 evidence_score 仍然有信息）
        if not scored:
            return [
                vars(ev) for ev in all_chunks
                if bypass_region or not region or ev.metadata.get("region") == region
            ][:top_k]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [vars(ev) for _, ev in scored[:top_k]]
