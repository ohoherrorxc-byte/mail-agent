"""
M1 PoC 阶段所有跨 Agent 传递的结构化数据的 Pydantic schema。

约定：
- 所有 model 都用 BaseModel（不绑 ORM，方便发到 MCP 工具 / 工作台）
- 所有 enum 字段用 `Literal[...]` 锁定，防止 gpt-5.4 在 strict json_schema 下吐非法值
- 字段名一律 snake_case，方便后续进 Postgres / Qdrant metadata
"""

from __future__ import annotations

from typing import List, Literal, Optional, Dict, Any
from pydantic import BaseModel, Field, ConfigDict


# ── 3.2 意图分类 ──────────────────────────────────────────────────────────

# 来源：iSMART售后服务邮箱回复方法_20260308.xlsx 真实流量 77 条模板
# 按"回复模式 + 工单路由"聚类，32 值。M1 不强求 exhaustivity，但每类
# 都对应客服知识库里的标准模板段落，方便 drafting 直接套。
# 真值样本见 eval/samples_real_seed.json；改 enum 必须三同步
# （schemas.py + prompts.py TRIAGE_SYSTEM rubric + 真值）。
# FICM-软件更新 在 xlsx 里有分类，但 schemas 没有 ficm_update；
# template_reply.py:49 把它映射到 app_update（注释覆盖 "FICM 升级失败"）。
Intent = Literal[
    # ── App 侧（高频） ──
    "app_vehicle_control",     # App-车控车态: stale data / error 47/59 / 车辆无响应
    "app_account",             # App-账号管理: login 失败 / session expired
    "app_binding",             # App-绑车解绑: QR 不显示 / 二手车 / 强制解绑
    "app_update",              # App Store / Play Store / FICM 升级失败
    "app_messages",            # App-消息中心相关: 重复告警
    "app_suggestion",          # App-优化建议

    # ── iSMART 平台（按子主题细拆） ──
    "ismart_data_plan",        # SIM 容量 / 免费周期
    "ismart_remote_control",   # AC / 车窗 / 充电功率 远程
    "ismart_digital_key",      # 数字钥匙 / Keyless Start
    "ismart_fota",             # FOTA / OTA / 未来支持
    "ismart_connectivity",     # 无线 CPAA / EV Trip Planner / e-call / 区域扩展
    "ismart_account_inquiry",  # 主副账号 / 密码重置 / PIN / 副账号创建
    "ismart_owner_change",     # 卖车 / 新车主过户
    "ismart_region_support",   # X 国支不支持
    "ismart_privacy",          # 拒绝隐私协议 → 核心功能关闭
    "ismart_user_manual",      # 找不到车主手册
    "ismart_feature_inquiry",  # 其余功能咨询 (free usage / 远程 AC / trip planner / how-to)

    # ── FICM ──
    "ficm_activation",
    "ficm_cpaa",
    "ficm_map",

    # ── TSP ──
    "tsp_sensitive_data",      # GPS / 实时位置 / 车辆密码

    # ── ADAS（拆 4 类，安全相关默认 P1） ──
    "adas_acc",
    "adas_aeb",
    "adas_lka",
    "adas_tja",

    # ── 硬件 / 非 iSMART ──
    "hardware_warranty",       # 保修 / 零件 / IPK 灯 / 车身控制
    "charging",                # 充电失败
    "delivery_handover",       # 交车培训
    "vehicle_incompatible",    # 车型不支持

    # ── 投诉 / 生命周期 ──
    "complaint",               # 含 PR / 律师 / 媒体
    "follow_up_resolved",      # 询问解决 / 已解决
    "other",                   # fallback
]
Urgency = Literal["P0", "P1", "P2", "P3"]

# M2.5 知识库 (iSMART自动回复知识库_全量模板_20260729.xlsx) 引入的 5 档自动回复分级。
# 全部对应"自动生成草稿 → 人工后审 → Send"的工位流程，**不是直接 send**。
#   auto_draft_safe         → 可自动回复（默认路径，createReply 落草稿）
#   auto_draft_needs_info   → 可自动回复-补信息（草稿先发，回信再补；
#                              M1.5 简化：仍走 createReply，但草稿正文会
#                              提示"please provide X"，人工 review 时一眼看出）
#   auto_draft_review       → 可自动回复-需人工复核（建草稿 + 标题加 [REVIEW]）
#   no_auto_reply           → 不可自动回复（任何带"接 TCMP / 人工强制解绑 /
#                              法规风险"等 → 走 NeedsHumanEscalation 升人工）
#   auto_draft_and_close    → 可自动回复并关闭（建草稿后归档，对应 follow_up_resolved）
#
# 注意：auto_reply_level 与 red_flags / urgency 严格正交。
# 一封 ADAS 红牌邮件 urgency=P1，但 red_flag=safety_incident 命中 → 走 hard escalation
# 而非 auto_draft_review。auto_reply_level 仅控制"能不能落草稿"，不能绕过 red_flag gate。
AutoReplyLevel = Literal[
    "auto_draft_safe",
    "auto_draft_needs_info",
    "auto_draft_review",
    "no_auto_reply",
    "auto_draft_and_close",
]

# 必填信息组（来自 必填信息字典 sheet）：草稿生成时 LLM 提示哪些字段需
# 求客户补齐；模板占位符填不上的字段会落在"please provide"提示里。
# 取值是 必填信息字典 第 1 列（一级信息组）的归一化形式 —— M2.5 共 7 组：
#   "basic_vehicle"      → Country/Region; Brand; Vehicle model; full 17-char VIN
#   "app_tech"           → Mobile phone model/system; iSMART App version; screenshot
#   "binding"            → VIN; iSMART account; proof of ownership; driving licence
#   "avn_activation"     → VIN; activation process video; vehicle time/date/region
#   "location_map"       → VIN; navigation location video; weather location
#   "charging_diff"      → VIN; App screenshot; dashboard photo at same time
#   "non_ismart"         → VIN; dashboard warning photo; symptom video (if safe)
#   空 list 表示模板不需要额外补信息。
RequiredInfoGroup = Literal[
    "basic_vehicle",
    "app_tech",
    "binding",
    "avn_activation",
    "location_map",
    "charging_diff",
    "non_ismart",
]
# iSmart 销售区 enum（6 区）—— 来源：业务拍板 2026-07-21。
# 不再细分子区域（UK 归 Europe，GCC 归 Asia，AU&NZ 归 Oceania）。
# 如要改请同步改 agent/prompts.py 的 TRIAGE_SYSTEM region 描述段及 eval 真值。
Region = Literal[
    "Europe",         # 英国 / 法 / 德 / 意 / 西 / 葡 / 希 / 塞 / 瑞 / 丹 / 挪 / 爱 / 波 / 土耳其 / 以色列
    "Asia",           # 沙特 / 阿联酋 / 卡塔尔 / 印度 / 巴基斯坦 / 菲律宾 / 越南 / 泰国 / 印尼 / 马来西亚 / 新加坡 / 俄罗斯 / 哈萨克斯坦
    "South America",  # 墨西哥 / 巴西 / 阿根廷 / 智利 / Jamaica / Trinidad & Tobago
    "Africa",         # 埃及 / 南非 / 尼日利亚 等
    "Oceania",        # 澳大利亚 / 新西兰
    "North America",  # US / Canada（iSmart 不卖，PoC 阶段遇到应 escalate）
]


class TriageResult(BaseModel):
    """IntentTriageAgent 的唯一输出。"""

    model_config = ConfigDict(extra="forbid")

    intent: Intent = Field(..., description="邮件落入的售后意图类别")
    urgency: Urgency = Field(..., description="紧急度；P0=律师函/安全事故/P1=保修失效风险")
    region: Region = Field(..., description="地区法规适用；用于后续选 ComplianceChecker 规则库")
    language: str = Field(..., description="ISO-639-1 简码，如 'en' / 'es' / 'fr' / 'ar' / 'pt'")
    country: Optional[str] = Field(
        default=None,
        description=(
            "ISO 3166-1 alpha-2 / 区域英文名（如 'GB' / 'FR' / 'United Kingdom' / 'Germany'）。"
            "用于 region=Europe 时区分 UK GDPR vs EU GDPR；None 时按 region 默认路由。"
            "M2 加；M1 旧样本此字段可为空，向后兼容。"
        ),
    )
    requires_recall_check: bool = Field(..., description="是否需要先查 mcp-crm.get_open_recalls")
    requires_warranty_lookup: bool = Field(..., description="是否需要先查 mcp-crm.get_warranty_status")
    red_flags: List[str] = Field(
        default_factory=list,
        description="红牌信号：律师函暗示 / 媒体抄送 / 安全事故 / 已升级媒体发声",
    )
    # ── M2.5: 知识库新增字段 ──
    auto_reply_level: AutoReplyLevel = Field(
        default="auto_draft_safe",
        description=(
            "5 档自动回复分级；控制 createReply 是否落草稿、标题是否加 [REVIEW]、"
            "或直接走 NeedsHumanEscalation。来源：iSMART自动回复知识库_全量模板_20260729.xlsx"
            " 的 自动回复等级 列。"
        ),
    )
    required_info_group: List[RequiredInfoGroup] = Field(
        default_factory=list,
        description=(
            "必填信息组（来自 必填信息字典 sheet 的一级信息组）。草稿生成时 LLM 会在"
            " 缺失字段处用 'please provide X' 提示人工 review 时补。空 list 表示模板"
            " 不需要补信息。"
        ),
    )


# ── 3.4 知识检索 ──────────────────────────────────────────────────────────

class CitedClause(BaseModel):
    """RAG 召回的每条证据。doc_id 必须能在 Qdrant/Qdrant-BM25 命中。"""
    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(..., description="政策 / TSB / 案例库里的稳定 ID")
    section: str = Field(..., description="小节标题或编号，例如 'EU-2024-7.3'")
    quote: str = Field(..., description="原文片段；不要让模型改写")


class RetrievalResult(BaseModel):
    """KnowledgeRetriever 的唯一输出。"""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., description="一段话总结检索到的政策/案例对本邮件的适用")
    cited_clauses: List[CitedClause] = Field(
        default_factory=list,
        description="每条结论必须挂一节证据；空数组会被 ComplianceChecker 直接打回",
    )
    evidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="自评证据强度；<0.6 建议退回 Step 4 重新检索或转人工",
    )


# ── 3.6 合规检查 ──────────────────────────────────────────────────────────

class ComplianceVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pass_: bool = Field(..., alias="pass", description="是否通过；False → 退回 DraftingAgent")
    violations: List[str] = Field(
        default_factory=list,
        description="具体违规项；如 '承诺词: refund', 'PII 回显: 信用卡尾号'",
    )


class ComplianceResult(BaseModel):
    """ComplianceCheckerAgent 的唯一输出。"""

    model_config = ConfigDict(extra="forbid")

    verdict: ComplianceVerdict
    sla_commitment: str = Field(..., description="本封邮件对外承诺的响应 SLA")
    human_review_required: bool = Field(
        ...,
        description="是否必须人工复核；P0 / 含承诺词 / 含 PII 时为 True",
    )
    suggested_reply_adjustments: List[str] = Field(
        default_factory=list,
        description="若不通过，列出需要改写/删除的句子",
    )


# ── 3.5 草稿 ──────────────────────────────────────────────────────────────

class DraftReply(BaseModel):
    """DraftingAgent / Reflection 重写后的输出（供人审 + 进审计库）。"""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(..., description="邮件主题，Re: 开头优先")
    body_md: str = Field(..., description="邮件正文（Markdown 渲染后给客户）")
    cited_clause_ids: List[str] = Field(
        default_factory=list,
        description="正文里引用到的政策/TSB 编号；空数组 → 不允许送审",
    )
    next_step: str = Field(..., description="结尾的清晰下一步动作 + SLA 承诺")


# ── M1.6: vision 输出 ─────────────────────────────────────────────────────

class ImageDescription(BaseModel):
    """vision LLM 对单张客户附件截图的结构化描述。
    作为 triage 的辅助上下文（不进 Pydantic strict 必填太多字段，保持精简）。"""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(..., description="中文详细描述截图内容（app / 页面 / 错误 / 按钮）")
    app_name: str = Field(..., description="识别出的 app 名（如 iSMART、Settings、Chrome 等）")
    error_text: Optional[str] = Field(
        None,
        description="截图里**逐字引用**的错误信息原文；含括号里错误码；没有就 null",
    )
    error_code: Optional[str] = Field(
        None,
        description="错误码（如 '59' / '47'）；没有就 null",
    )
    is_ismart_app: bool = Field(..., description="看起来是不是 iSMART app 截图")
