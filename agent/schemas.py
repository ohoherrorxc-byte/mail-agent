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

# 来源（两层）：
# L1 — iSMART售后服务邮箱回复方法_20260308.xlsx（77 条模板）按"回复模式 + 工单
#      路由"聚类 → 32 值（M1 PoC 沿用）。M2.5 补盲区到 38 值。
# L2 — iSMART自动回复知识库_全量模板_20260729.xlsx（97 条模板）的 67 个 二级问题
#      分 类（"App-绑车解绑相关" / "FICM-蓝牙相关" 等）→ SubIntent 字段。
#
# Intent 是路由层（哪个团队接手）；SubIntent 是模板层（哪段话术）。
# 改 enum 必须三同步：
#   1) schemas.py（这里）
#   2) agent/prompts.py:TRIAGE_SYSTEM rubric（教 LLM 新值）
#   3) 真值样本 eval/samples_real_seed.json + eval/_samples_to_label.json
# M2.5 补盲区（vs 20260308 xlsx）：
#   ficm_bluetooth          — 蓝牙电话 / 麦克风 / 无法听清（FICM-蓝牙相关）
#   ficm_voice              — Hello MG / 语音助手 / 语音语言（FICM-Voice Service）
#   ismart_music_streaming  — Amazon Music / Spotify / 在线音乐（FICM-Music）
#   ficm_radio              — DAB / RDS / 电台冻结（FICM-Radio）
#   ismart_translation      — UI 翻译错误 / 邮件语言错（iSMART-翻译相关）
#   tsp_data_missing        — 客户看不到自己的车辆数据（TSP-车辆信息缺失）；
#                             性质与 tsp_sensitive_data 相反（一个要数据 / 一个禁数据）
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
    "ismart_translation",      # UI / 邮件 翻译错误（M2.5 新增）
    "ismart_music_streaming",  # Amazon Music / Spotify / 在线音乐（M2.5 新增）

    # ── FICM ──
    "ficm_activation",
    "ficm_cpaa",
    "ficm_map",
    "ficm_bluetooth",          # 蓝牙电话 / 麦克风 / 无法听清（M2.5 新增）
    "ficm_voice",              # Hello MG / 语音助手（M2.5 新增）
    "ficm_radio",              # DAB / RDS / 电台冻结（M2.5 新增）

    # ── TSP ──
    "tsp_sensitive_data",      # GPS / 实时位置 / 车辆密码（禁数据）
    "tsp_data_missing",        # 客户看不到自己的车辆数据（M2.5 新增；要数据）

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


# SubIntent — 二级路由（67 L2 from 20260729 xlsx + 兜底）。
# 与 Intent 关系：每个 Intent 落到 1..N 个 SubIntent；同一封邮件可能同时命中
# 多个 SubIntent（multi-label），但 triage 时只输出主 SubIntent（max score）。
# M2.5 引入：drafter 据此选模板段落（template_reply.match 优先用 SubIntent）。
SubIntent = Literal[
    # ── iSMART 相关（32 L2） ──
    "app_vehicle_control_status",     # App-车控车态相关
    "app_binding_flow",               # App-绑车解绑相关
    "app_account_management",         # App-账号管理相关
    "ismart_feature_inquiry",         # iSMART-功能咨询
    "ficm_map",                       # FICM-地图相关
    "ismart_privacy",                 # iSMART-隐私协议相关（含 FICM-隐私协议）
    "ismart_user_manual",             # iSMART-用户手册相关
    "ficm_activation",                # FICM-激活相关
    "app_guide_manual",               # App-引导手册
    "app_messages",                   # App-消息中心相关
    "ficm_network",                   # FICM-网络相关
    "ficm_cpaa",                      # FICM-CPAA相关
    "ficm_bluetooth",                 # FICM-蓝牙相关
    "ficm_online_service",            # FICM-Online Service相关
    "ismart_region_support",          # iSMART-支持车区域
    "ficm_software_update",           # FICM-软件更新
    "ficm_radio",                     # FICM-Radio相关
    "ficm_ui_complexity",             # FICM-界面层级复杂
    "app_map",                        # App-地图相关
    "app_energy_stats",               # App-能耗统计相关
    "app_charging_mode_not_saved",    # App-无法记忆充电模式
    "app_vehicle_image",              # App-车体图相关
    "app_update",                     # App-软件更新
    "tsp_data_missing",               # TSP-车辆信息缺失
    "tsp_sensitive_data",             # TSP-敏感数据要求
    "ficm_music",                     # FICM-Music相关
    "ficm_voice",                     # FICM-Voice Service相关
    "ficm_volume",                    # FICM-Volume相关
    "ficm_driving_mode_not_saved",    # FICM-无法记忆驾驶模式
    "ficm_auto_reboot",               # FICM-自动重启
    "ismart_translation",             # iSMART-翻译相关
    "app_charging_schedule",          # App-预约充电相关

    # ── 关闭（2 L2） ──
    "close_resolved",                 # 关闭-已解决
    "close_invalid",                  # 关闭-无效邮件

    # ── 新需求（15 L2） ──
    "feat_request_charging_history",
    "feat_request_app_feedback",
    "feat_request_charging_notification",
    "feat_request_remote_window",
    "feat_request_total_mileage",
    "feat_request_ota",
    "feat_request_adjustable_charging_power",
    "feat_request_watch_app",
    "feat_request_multi_account_binding",
    "feat_request_theme_switching",
    "feat_request_split_charging_schedule",
    "feat_request_service_booking",
    "feat_request_seat_heating_schedule",
    "feat_request_remote_lights",
    "feat_request_ev_trip_planner",
    "ismart_owner_change",             # M2.5 补：卖车 / 新车主过户
    "feat_request_charging_schedule",  # M2.5 补：自定义预约充电

    # ── 通用处理（5 L2） ──
    "general_request_complete_vehicle_info",
    "general_request_vin",
    "general_vin_incorrect",
    "general_request_screenshot_video",
    "general_backend_analyzing",

    # ── 非iSMART相关（13 L2） ──
    "non_ismart_charging",
    "non_ismart_after_sales_service",
    "non_ismart_unsupported_model",
    "non_ismart_body_control",
    "non_ismart_ac",
    "adas_general",                   # 通用 ADAS（非具体 ACC/AEB/LKA/TJA）
    "ipk_general",                    # IPK 相关（仪表盘）
    "non_ismart_feature_inquiry",
    "adas_acc",
    "adas_aeb",
    "adas_lka",
    "adas_tja",
    "non_ismart_ipk",

    # ── 兜底 ──
    "sub_intent_unknown",             # 真不属于上面任何一类
]
Urgency = Literal["P0", "P1", "P2", "P3"]

# M2.5 知识库 (iSMART自动回复知识库_全量模板_20260729.xlsx) 引入的 5 档自动回复分级。
# 全部对应"自动生成草稿 → 人工后审 → Send"的工位流程，**不是直接 send**。
#   auto_draft_safe         → 可自动回复（默认路径，createReply 落草稿）
#   auto_draft_needs_info   → 可自动回复-补信息（草稿先发，回信再补；
#                              M1.5 简化：仍走 createReply，但草稿正文会
#                              提示"please provide X"，人工 review 时一眼看出）
#   auto_draft_review       → 可自动回复-需人工复核（建草稿，标题不加 [REVIEW]）
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
# iSmart 销售区 —— 以《国家清单-大区分类对照表.xlsx》为准（agent/region_catalog.py）。
# 英国仍记 Europe，合规用 country=GB 走 United Kingdom。美加不在对照表，单独 North America。
# 改枚举请同步 TRIAGE_SYSTEM region 段 + region_catalog。
Region = Literal[
    "Europe",         # 对照表「欧洲」：含英国 / 土耳其 / 俄罗斯 / 白俄罗斯 / 留尼汪；不含以色列
    "Middle East",    # 对照表「中东」：沙特 / 阿联酋 / 以色列等；M2 未接合规，升人工
    "Asia",           # 对照表「亚洲」：东盟 / 南亚 / 港澳等；不含 GCC / 俄 / 以色列
    "South America",  # 对照表「美洲」：墨西哥 + 加勒比 + 拉美；不含美加
    "Africa",         # 对照表「非洲」
    "Oceania",        # 对照表「大洋洲」
    "North America",  # US / Canada（表外）
]


class TriageResult(BaseModel):
    """IntentTriageAgent 的唯一输出。"""

    model_config = ConfigDict(extra="forbid")

    intent: Intent = Field(..., description="邮件落入的售后意图类别")
    # ── M2.5 新增：二级路由（67 L2 from 20260729 xlsx）────────────
    # drafter 据此选模板段落；与 intent 是 1:N 关系（一 intent 可落多 sub_intent，
    # triage 只输出主 SubIntent）。fallback 时为 sub_intent_unknown。
    sub_intent: Optional[SubIntent] = Field(
        default=None,
        description="二级路由；67 L2 from 20260729 xlsx。drafter 用这个选模板段落。",
    )
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
            "5 档自动回复分级；控制 createReply 是否落草稿、"
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

class TemplatePick(BaseModel):
    """第二次 LLM：只选知识库行 ID，不写客户回信。"""

    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(
        ...,
        description="Exact template_id from the candidate list, e.g. STATUS-003",
    )


class IssueTagPick(BaseModel):
    """按最新一段的完整诉求选一个问题分类。"""

    model_config = ConfigDict(extra="forbid")

    category: str = Field(
        "",
        description="One allowed category name, or empty if no concrete vehicle/app fault",
    )


class IssueTagVerify(BaseModel):
    """核对所选分类是否盖住最新一段的全部诉求，而不是只对上其中一句。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool = Field(
        ...,
        description="True only if the category fits the whole latest message",
    )
    category: str = Field(
        "",
        description="If ok, the same category. If not, the one allowed name that fits the whole message, or empty",
    )
    reason: str = Field("", description="Short English reason")


class SemanticDraftReview(BaseModel):
    """建稿前语义复核：模板/草稿是否在回答客户最新一段的主诉。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool = Field(
        ...,
        description="True if the row answers at least one latest-turn ask; extra unmatched asks must not force false",
    )
    reason: str = Field(
        "",
        description="Short English reason when ok is false; empty when ok is true",
    )


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
