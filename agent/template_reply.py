"""
template_reply.py — M1.5 过渡工具：跳过 LLM 草稿，直接查 xlsx 模板。

为什么需要：
M1 PoC 完整流水线（Triage → Retrieval → Drafting → Compliance）
跑一封邮件要 4-7 个 LLM 调用，单封 60-90 秒、$0.10+。
但 iSmart 客服 77 条模板已经覆盖 80% 流量，
对模板能直接答的邮件，**不需要 LLM 起草** ——
Triage 判 intent → 查 xlsx 模板 → 人改两行 → 发。

速度：单封 < 5 秒（1 个 LLM 调用），成本 < $0.01。

后续 M2 规范慢慢补：模板覆盖不到的 intent（边界 case / 多步排查）
再走完整 EmailAgent 流水线。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

import openpyxl

# 同一进程里复用 schema
from .schemas import TriageResult

ROOT = Path(__file__).resolve().parent.parent


# ── xlsx 加载 ──────────────────────────────────────────────────────────

# xlsx Category → Intent enum 映射（人工建立，从 [agent/schemas.py:18-69] 推出）
CATEGORY_TO_INTENT: dict[str, str] = {
    # ── 原 v1 category（来自 20260308.xlsx，77 条模板表） ──────────
    "App-车控车态相关":       "app_vehicle_control",
    "App-账号管理相关":       "app_account",
    "App-绑车解绑相关":       "app_binding",
    "App-消息中心相关":       "app_messages",
    "App-优化建议":           "app_suggestion",
    "App-软件更新":           "app_update",
    "iSMART-功能咨询":        "ismart_feature_inquiry",
    "iSMART-用户手册相关":     "ismart_user_manual",
    "iSMART-支持区域":        "ismart_region_support",
    "TSP-敏感数据要求":       "tsp_sensitive_data",
    "FICM-激活相关":          "ficm_activation",
    "FICM-CPAA相关":          "ficm_cpaa",
    "FICM-软件更新":          "app_update",  # M1.6 fix: 之前映射到不存在的 ficm_update；schemas.py 注释 (app_update) 已覆盖 "FICM 升级失败"
    "FICM-隐私协议相关":      "ismart_privacy",
    "FICM-地图相关":          "ficm_map",
    "非iSMART-功能咨询":      "hardware_warranty",
    "非iSMART-不支持车型":    "vehicle_incompatible",
    "非iSMART-充电相关":      "charging",
    "非iSMART-IPK相关":       "hardware_warranty",
    "非iSMART-车身控制相关":   "hardware_warranty",
    "非iSMART-售前售后服务":  "delivery_handover",
    "Adas-ACC":              "adas_acc",
    "Adas-AEB":              "adas_aeb",
    "Adas-LKA":              "adas_lka",
    "Adas-TJA":              "adas_tja",
    "关闭-已解决":            "follow_up_resolved",

    # ── M1.8 扩展：来自 operations_mail.xls 的 22,430 条 AUTO: token 报告 ──
    # （eval/runs/auto_intent_coverage.json  53557 行统计中 AUTO 未映射的 token）
    "FICM-网络相关":           "ismart_connectivity",   # 92 次
    "FICM-Voice Service相关":  "ismart_feature_inquiry",  # 23 次（语音服务是一般功能咨询）
    "FICM-Radio相关":          "ismart_feature_inquiry",  # 15 次
    "FICM-蓝牙相关":           "ismart_connectivity",   # 9 次（蓝牙连接类）
    "FICM-自动重启或黑屏":     "hardware_warranty",    # 6 次（硬件故障）
    "iSMART-翻译相关":         "ismart_feature_inquiry",  # 5 次
    "非iSMART-空调相关":       "hardware_warranty",    # 2 次
    "FICM-充电相关":           "charging",             # 2 次
    "FICM-Online Service相关": "ismart_connectivity",   # 2 次（在线服务 / 远程连接类）
    "FICM-无法记忆驾驶模式":   "hardware_warranty",    # 1 次
    # ── Negative token：2,099 封负面情绪 → complaint ──────────
    # parse_category() 也会把 "Negative" 加入 status_toks 列表，
    # CATEGORY_TO_INTENT 接管后 → intent_mapped=complaint
    "Negative":               "complaint",
}


def load_xlsx(paths: list[Path] | None = None) -> list[dict]:
    """加载模板表，可指定多个文件；前面的优先级高。

    默认加载顺序（先者优先，未找到 fallback）：
      1. iSMART自动回复知识库_全量模板_*.xlsx（M2.5 新：12 列 schema，
         含自动回复等级 / 必填信息组 / 禁止表述）
      2. iSMART售后服务邮箱回复方法_v*_*.xlsx（v2 / v3 / v4 旧版 3 列 schema，
         仅作为 fallback，新版未覆盖的旧模板仍可命中）
      3. iSMART售后服务邮箱回复方法_20260308.xlsx（基线原表）

    Returns 统一 schema：
      {
        "intent":            str,         # Intent enum
        "template":          str,         # 英文模板正文
        "category":          str,         # 二级问题分类原文（新表）/ 旧表 Category
        "level":             str,         # auto_draft_safe / ... (新表独有)
        "required_info":     list[str],   # RequiredInfoGroup enum 列表（新表独有）
        "next_step":         str,         # 下一步动作（新表）
        "human_trigger":     str,         # 人工介入条件（新表）
        "forbidden":         str,         # 禁止/注意表述原文（新表）
        "source":            str,         # xlsx 文件名
      }

    同 intent + template 文本去重，保留先出现的（新版优先）。
    """
    if paths is None:
        paths = []
        # M2.5 优先：自动回复知识库（新表，多列）
        for p in sorted(ROOT.glob("iSMART自动回复知识库_全量模板_*.xlsx"), reverse=True):
            paths.append(p)
        # v2 / v3 / v4 旧表（fallback）
        for p in sorted(ROOT.glob("iSMART售后服务邮箱回复方法_v*_*.xlsx"), reverse=True):
            paths.append(p)
        # 基线原表
        paths.append(ROOT / "iSMART售后服务邮箱回复方法_20260308.xlsx")

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        if not path.exists():
            continue
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        # 探测 schema：第 1 行 col1 是 "模板ID" → 新表；否则旧表（3 列）
        first_cell = ws.cell(row=1, column=1).value or ""
        is_new_schema = str(first_cell).strip() == "模板ID"
        if is_new_schema:
            _load_new_schema_rows(ws, path, rows, seen)
        else:
            _load_legacy_schema_rows(ws, path, rows, seen)
    return rows


# ── M2.5 自动回复知识库 解析 ─────────────────────────────────────────

# 自动回复等级 (xlsx 原始) → AutoReplyLevel enum
# 注意：xlsx 里 "不可自动回复\n【...】" 这种带 newline 的条目先按首行归类。
_LEVEL_TO_ENUM: dict[str, str] = {
    "可自动回复":              "auto_draft_safe",
    "可自动回复-补信息":        "auto_draft_needs_info",
    "可自动回复-需人工复核":    "auto_draft_review",
    "不可自动回复":            "no_auto_reply",
    "可自动回复并关闭":        "auto_draft_and_close",
    "可自动回复（补附件）":    "auto_draft_needs_info",  # 补附件 = 需要更多信息
    "不可自动回复-需人工复核":  "no_auto_reply",         # 不可自动优先于复核
}

# 必填信息组（xlsx 一级信息组中文）→ RequiredInfoGroup enum
_INFO_GROUP_TO_ENUM: dict[str, str] = {
    "基础车辆信息":      "basic_vehicle",
    "App技术信息":       "app_tech",
    "绑车/解绑":         "binding",
    "AVN/FICM激活":      "avn_activation",
    "位置/地图":         "location_map",
    "充电/App车态差异":  "charging_diff",
    "非iSMART车辆功能":  "non_ismart",
}

# 二级问题分类 → Intent enum（M2.5 新表独有的类目扩展；旧表类目仍走 CATEGORY_TO_INTENT）
L2_TO_INTENT_EXTRA: dict[str, str] = {
    # ── M2.5 新表独有 ──
    "请求VIN/提示VIN位置":      "app_binding",
    "请求完整车辆信息":         "app_binding",
    "请求视频/截图":            "app_vehicle_control",
    "后台分析中":              "ismart_feature_inquiry",
    "VIN不正确":               "app_binding",
    "新需求-App-Feedback":      "app_suggestion",
    "新需求-App-充电历史记录":   "app_suggestion",
    "新需求-App-充电提醒":       "app_suggestion",
    "新需求-App-分时段预约充电":  "charging",
    "新需求-App-多主题切换":    "app_suggestion",
    "新需求-App-多账号绑车":    "app_binding",
    "新需求-App-总里程":        "app_suggestion",
    "新需求-App-远控关灯":      "ismart_remote_control",
    "新需求-App-远控车窗":      "ismart_remote_control",
    "新需求-App-预约座椅加热":   "ismart_remote_control",
    "新需求-App-预约维保":      "hardware_warranty",
    "新需求-FICM- EV Trip Planner": "ismart_connectivity",
    "新需求-FICM/App-可调充电功率": "ismart_remote_control",
    "新需求-OTA升级":          "ismart_fota",
    "新需求-Watch版App":       "app_suggestion",
    # ── M2.5.5: 用户新增模板（CHG-SCHED-001）─────────────────────────
    # 客户问"如何在 App 设置充电时段"——how-to，不是"建议加新功能"。
    # 区别于 NEW-SPLIT-CHG-001（后者是需求反馈型）。
    "App-预约充电相关":        "charging",
    # ── 新表新出现的 FICM / App 子类 ──
    "ADAS相关":                "adas_acc",  # generic ADAS → 默认 acc
    "App-地图相关":            "ficm_map",
    "App-引导手册":            "ismart_user_manual",
    "App-无法记忆充电模式":     "charging",
    "App-能耗统计相关":        "ismart_feature_inquiry",
    "App-车体图相关":          "ismart_feature_inquiry",
    "FICM-Music相关":          "ismart_feature_inquiry",
    "FICM-Volume相关":         "ismart_feature_inquiry",
    "FICM-界面层级复杂":       "ismart_feature_inquiry",
    "FICM-自动重启":           "hardware_warranty",
    "IPK相关":                "hardware_warranty",
    "TSP-车辆信息缺失":        "ismart_feature_inquiry",
    "iSMART-翻译相关":         "ismart_feature_inquiry",
    "关闭-无效邮件":           "follow_up_resolved",
    "非iSMART-IPK相关":        "hardware_warranty",
    "非iSMART-空调相关":       "hardware_warranty",
    "非iSMART-车身控制相关":    "hardware_warranty",
}


def _resolve_intent_from_l2(l2: str) -> Optional[str]:
    """二级问题分类 → Intent enum。优先 L2_TO_INTENT_EXTRA，再 fallback CATEGORY_TO_INTENT。"""
    if not l2:
        return None
    if l2 in L2_TO_INTENT_EXTRA:
        return L2_TO_INTENT_EXTRA[l2]
    if l2 in CATEGORY_TO_INTENT:
        return CATEGORY_TO_INTENT[l2]
    return None


def _normalize_level(raw: str) -> str:
    """xlsx 自动回复等级字符串 → AutoReplyLevel enum。
    处理：strip、newline-separated 注解（如 "不可自动回复\n【...】"）、同义词。
    """
    s = (raw or "").strip()
    if "\n" in s:
        s = s.split("\n", 1)[0].strip()
    if s in _LEVEL_TO_ENUM:
        return _LEVEL_TO_ENUM[s]
    if "不可" in s:
        return "no_auto_reply"
    if "补信息" in s or "补附件" in s:
        return "auto_draft_needs_info"
    if "复核" in s:
        return "auto_draft_review"
    if "关闭" in s:
        return "auto_draft_and_close"
    return "auto_draft_safe"


def _parse_required_info(raw: str) -> list[str]:
    """xlsx 必填信息列 → RequiredInfoGroup enum 列表。"""
    if not raw:
        return []
    s = raw.strip()
    if not s or s in ("无", "/", "-", "—", "N/A"):
        return []
    out: list[str] = []
    for key, enum in _INFO_GROUP_TO_ENUM.items():
        if key in s and enum not in out:
            out.append(enum)
    s_low = s.lower()
    keyword_map = [
        (("country", "vehicle model", "17-char", "17 char", "vin"), "basic_vehicle"),
        (("app version", "phone model", "os version", "screenshot"), "app_tech"),
        (("current owner", "previous owner", "driving licence", "ownership"), "binding"),
        (("activation",), "avn_activation"),
        (("map", "navigation", "weather location", "gps"), "location_map"),
        (("charging", "dashboard", "charge state"), "charging_diff"),
        (("warning", "symptom", "mileage"), "non_ismart"),
    ]
    for keywords, enum in keyword_map:
        if any(k in s_low for k in keywords) and enum not in out:
            out.append(enum)
    return out


def _load_new_schema_rows(
    ws, path: Path, rows: list[dict], seen: set[tuple[str, str]],
) -> None:
    """解析 M2.5 自动回复知识库新表（12 列）。"""
    for row in ws.iter_rows(min_row=2, values_only=True):
        tmpl_id   = (row[0] or "").strip() if len(row) > 0 else ""
        l1_cat    = (row[1] or "").strip() if len(row) > 1 else ""
        l2_cat    = (row[2] or "").strip() if len(row) > 2 else ""
        scenario  = (row[3] or "").strip() if len(row) > 3 else ""
        keywords  = (row[4] or "").strip() if len(row) > 4 else ""
        level_raw = (row[5] or "").strip() if len(row) > 5 else ""
        req_info  = (row[6] or "").strip() if len(row) > 6 else ""
        tmpl      = (row[7] or "").strip() if len(row) > 7 else ""
        next_step = (row[8] or "").strip() if len(row) > 8 else ""
        human_trig= (row[9] or "").strip() if len(row) > 9 else ""
        forbidden = (row[10] or "").strip() if len(row) > 10 else ""
        src_type  = (row[11] or "").strip() if len(row) > 11 else ""
        if not tmpl_id and not tmpl:
            continue

        intent = _resolve_intent_from_l2(l2_cat)
        if not intent:
            continue

        level = _normalize_level(level_raw)
        req_groups = _parse_required_info(req_info)
        # M2.5.4: 默认称谓 "Dear Customer," → {{salutation}}, 占位符由
        # greeting.personalize_greeting() 在草稿生成时按发件人姓名替换
        tmpl = _normalize_greeting_placeholder(tmpl)

        key = (intent, tmpl)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "intent":         intent,
            "template":       tmpl,
            "category":       l2_cat,
            "level":          level,
            "required_info":  req_groups,
            "next_step":      next_step,
            "human_trigger":  human_trig,
            "forbidden":      forbidden,
            "source":         path.name,
            "template_id":    tmpl_id,
            "scenario":       scenario,
            "keywords":       keywords,
            "l1_category":    l1_cat,
            "source_type":    src_type,
        })


def _load_legacy_schema_rows(
    ws, path: Path, rows: list[dict], seen: set[tuple[str, str]],
) -> None:
    """解析旧 v2 / v3 / v4 / 原表（3 列：Category | Template | Description）。"""
    for row in ws.iter_rows(min_row=2, values_only=True):
        cat = (row[0] or "").strip()
        tmpl = (row[1] or "").strip()
        desc = (row[2] or "").strip()
        if not cat and not tmpl:
            continue
        # M2.5.4: 默认称谓 → {{salutation}} 占位符（greeting 阶段替换）
        tmpl = _normalize_greeting_placeholder(tmpl)
        tokens = [t.strip() for t in re.split(r"[,/]", cat) if t.strip()]
        for tok in tokens:
            bare = tok
            if bare.startswith("AUTO:"):
                bare = bare[5:].strip()
            if bare.startswith("Region:"):
                continue
            if bare.startswith("关闭-"):
                continue
            intent = CATEGORY_TO_INTENT.get(bare)
            if intent:
                key = (intent, tmpl)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "intent":         intent,
                    "template":       tmpl,
                    "category":       bare,
                    "level":          "auto_draft_safe",   # 旧表无 level
                    "required_info":  [],
                    "next_step":      "",
                    "human_trigger":  "",
                    "forbidden":      "",
                    "source":         path.name,
                    # v4 模板第 3 列 Description 含元数据（"v4: source=cs-...;
                    # region=...; intent=...; supporting_replies=N"）。
                    # 旧表无第 4-11 列，原表 Description 是客服备注；都保留。
                    "description":    desc,
                })
                break


# ── Triage 单步 ────────────────────────────────────────────────────────

def classify_intent(
    email_body: str,
    *,
    llm,                      # AzureLLMClient
    use_llm: bool = True,
    image_descriptions: list[str] | None = None,   # M1.6: vision 解析后的截图描述
) -> TriageResult:
    """
    如果 use_llm=True → 调一次 Triage（reasoning=low, 快）
    否则 → 用关键词做粗匹配（不调 LLM, 完全本地, 0 成本）

    关键词匹配只为 template_reply CLI 启动快 / smoke test 用；
    真业务流量请用 use_llm=True。

    M1.6:
      image_descriptions 由 caller 通过 vision (gpt-5.4 multimodal) 抽取，
      每条形如 "[IMG_6079.png] 意大利 iSMART app 报错界面，错误 59 ..."
      作为额外 context 注入 user message，**不改 system prompt**
      （保持 prompt cache 命中）。
    """
    if not use_llm:
        return _keyword_classify(email_body)

    from .prompts import TRIAGE_SYSTEM

    user_content = email_body
    if image_descriptions:
        # 把 image 描述拼成一段，紧跟 email_body。
        # 顺序很重要：body 在前 → LLM 先读正文判断，screenshots 辅助验证。
        ctx = "\n\n--- Attached screenshot descriptions (M1.6 vision context) ---\n"
        ctx += "\n".join(image_descriptions)
        ctx += "\n--- end vision context ---"
        user_content = email_body + ctx

    triage, _call = llm.invoke_json(
        schema=TriageResult,
        messages=[
            {"role": "system", "content": TRIAGE_SYSTEM},
            {"role": "user",   "content": user_content},
        ],
        reasoning_effort="low",
        role="triage",
    )
    return triage


_KEYWORDS = {
    "app_binding":       ["bind", "qr code", "二维码", "force unbind", "previous owner", "解绑", "绑车"],
    "app_vehicle_control": ["not responding", "stale data", "range", "mileage", "无响应", "数据"],
    "app_account":       ["login", "session expired", "password", "登录", "密码"],
    "ismart_digital_key": ["digital key", "keyless", "数字钥匙", "locked", "锁在车里"],
    "ismart_data_plan":  ["data plan", "sim", "套餐", "免费周期"],
    "ismart_account_inquiry": ["secondary account", "副账号", "family", "family sharing"],
    "ismart_region_support": ["does my country", "supported in", "norway support", "支不支持"],
    "ismart_feature_inquiry": ["how do i", "how to", "free usage", "remote control", "fota"],
    "ismart_user_manual": ["user manual", "owner manual", "车主手册"],
    "charging":          ["charging", "充电", "charge session"],
    "follow_up_resolved": ["resolved", "thank you", "已解决", "感谢"],
    "complaint":         ["lawyer", "attorney", "media", "going public", "social media", "律师", "媒体"],
}


def _keyword_classify(email_body: str) -> TriageResult:
    text = email_body.lower()
    scores: dict[str, int] = {}
    for intent, kws in _KEYWORDS.items():
        scores[intent] = sum(1 for kw in kws if kw in text)
    # 默认 fallback
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        best = "other"
    # urgency 简单判
    if any(k in text for k in ["lawyer", "media", "social media", "safety", "律师", "媒体"]):
        urgency = "P0"
    elif any(k in text for k in ["locked", "lockout", "urgent", "锁在"]):
        urgency = "P1"
    elif any(k in text for k in ["how to", "how do", "general", "一般", "如何"]):
        urgency = "P3"
    else:
        urgency = "P2"
    return TriageResult(
        intent=best,
        urgency=urgency,
        region="North America",  # 默认值；M2 接 6 区真实分类
        language="en",
        requires_recall_check=False,
        requires_warranty_lookup=False,
        red_flags=[],
    )


# ── 模板占位符检测 ──────────────────────────────────────────────────────

# 模板里 {xxx} 是占位符
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# 模板正文里的默认称谓 → {{salutation}} 占位符（M2.5.4）
# xlsx 写死 "Dear Customer,"，LLM 起草时常改写为 "Dear Valued Customer," /
# "Hi there," / "Dear John,"，原正则匹配不到会漏网替换。运行时统一改写为
# {{salutation}} 让 greeting.personalize_greeting() 确定性兜底。
_DEFAULT_GREETING_RE = re.compile(
    r"(?:Dear|Hi|Hello)\s+(?:Customer|customer)\s*,",
    re.IGNORECASE,
)


def _normalize_greeting_placeholder(tmpl: str) -> str:
    """把模板正文里默认称谓 → {{salutation}} 占位符。

    只改写"开头出现的"（前面允许空行 / 空白），不动正文里的合法短语。
    没匹配到就原样返回。"""
    if not tmpl:
        return tmpl
    m = _DEFAULT_GREETING_RE.search(tmpl)
    if not m:
        return tmpl
    # 只在开头或前面只允许空白 / 换行
    head = tmpl[:m.start()]
    if head.strip():
        return tmpl
    return head + "{{salutation}}," + tmpl[m.end():]


def extract_placeholders(template: str) -> list[str]:
    """从模板里抽出 {xxx} 占位符"""
    return list(dict.fromkeys(_PLACEHOLDER_RE.findall(template)))


def fill_placeholders_naive(template: str, ctx: dict) -> str:
    """用 ctx dict 填占位符；缺失保留 {xxx}"""
    def _sub(m: re.Match) -> str:
        key = m.group(1)
        return ctx.get(key, m.group(0))
    return _PLACEHOLDER_RE.sub(_sub, template)


# ── 子场景检测（M1.5 新增）──────────────────────────────────────────────
# 当 intent = app_binding 时，邮件可能落在不同子场景：
#   - qr_not_showing   QR 码根本不显示
#   - scan_error       扫了码但报错
#   - previous_owner   二手车 / 前车主未解绑
# 用 body 关键词检测子场景，再在同 intent 模板里挑最相关的。

_SUB_SCENARIO_SIGNALS: dict[str, dict[str, list[str]]] = {
    "app_binding": {
        "qr_not_showing": [
            r"qr\s*(code\s*)?(doesn'?t|not|never)\s*(show|display|appear)",
            r"二维码\s*不.*(显示|出来|出现)",
            r"no\s*qr",
        ],
        "scan_error": [
            r"error\s*(message\s*)?(immediately|after|during).*scan",
            r"scanned?\s*(the\s*)?qr.*(error|fail)",
            r"scan.*(error|fail)",
            r"无\s*commit",
            r"no\s*commit(ment)?",
            r"扫\s*描.*(报\s*错|错\s*误|失\s*败)",
        ],
        "previous_owner": [
            r"previous\s*owner",
            r"used\s*car.*(bind|unbind)",
            r"force\s*unbind",
            r"二手.*车",
            r"前.*车主",
        ],
    },
}


def detect_sub_scenario(body_text: str, intent: str) -> Optional[str]:
    """Return sub-scenario name if body matches known signals; else None."""
    signals = _SUB_SCENARIO_SIGNALS.get(intent, {})
    if not signals:
        return None
    text = body_text.lower()
    best_name, best_score = None, 0
    for name, patterns in signals.items():
        score = sum(1 for p in patterns if re.search(p, text, re.I))
        if score > best_score:
            best_name, best_score = name, score
    return best_name


def _template_sub_scenario_relevance(template: dict, sub_scenario: str) -> float:
    """模板 body 含多少 sub_scenario 信号词的 keyword token。
    用来在同 intent 候选里挑最相关的。"""
    signals = _SUB_SCENARIO_SIGNALS.get(template["intent"], {}).get(sub_scenario, [])
    body_lower = template["template"].lower()
    score = 0.0
    for pattern in signals:
        # 从 pattern 里抽 [a-z]{4,} token，看是否在模板 body 出现
        tokens = re.findall(r"[a-z]{4,}", pattern.lower())
        for tok in tokens:
            if tok in body_lower:
                score += 1.0
    return score


def _template_body_token_overlap(template: dict, body_text: str) -> float:
    """模板 body ∩ 邮件 body 的 token 数（按 [a-z]{4,} 分词）。

    用作 pick_template() 的二级排序：sub_scenario 没命中时挑最相关的候选。
    解决 charging / ismart_feature_inquiry 等多候选 intent 下，首条候选不一定是
    客户问题对应的模板（如 CHG-001 充电失败 vs CHG-SCHED-001 预约充电教学）。
    """
    body_lower = (template.get("template") or "").lower()
    if not body_lower or not body_text:
        return 0.0
    mail_tokens = set(re.findall(r"[a-z]{4,}", body_text.lower()))
    if not mail_tokens:
        return 0.0
    return sum(1.0 for tok in mail_tokens if tok in body_lower)


# ── 主入口 ─────────────────────────────────────────────────────────────

def pick_template(triage_intent: str, templates: list[dict],
                  body_text: str = "") -> Optional[dict]:
    """按 intent 找模板；如有 sub_scenario 信号，优先选最相关的。
    优先找 intent 完全匹配的；找不到再 fallback 到 ismart_feature_inquiry。

    M2.5.5: 加 body token 重叠评分 fallback —— 同 intent 多候选时
    （如 charging 9 条），按模板 body ∩ 邮件 body 的 token 数排序，
    解决首条候选不一定是客户问题对应的模板（如 CHG-001 vs CHG-SCHED-001）。
    """
    candidates = [t for t in templates if t["intent"] == triage_intent]
    if not candidates:
        fallback = [t for t in templates if t["intent"] == "ismart_feature_inquiry"]
        return fallback[0] if fallback else None

    # 子场景打分（如识别到）—— 优先级最高
    sub = detect_sub_scenario(body_text, triage_intent)
    if sub:
        scored = [(c, _template_sub_scenario_relevance(c, sub)) for c in candidates]
        scored.sort(key=lambda x: x[1], reverse=True)
        if scored[0][1] > 0:   # 至少命中一个 keyword
            return scored[0][0]

    # M2.5.5: body token 重叠评分 fallback
    if body_text and len(candidates) > 1:
        scored = [(c, _template_body_token_overlap(c, body_text))
                  for c in candidates]
        scored.sort(key=lambda x: x[1], reverse=True)
        if scored[0][1] > 0:   # 至少命中一个 token（>= 4 字符）
            return scored[0][0]

    return candidates[0]   # 默认第一条


def run_template_reply(
    email_body: str,
    *,
    use_llm: bool = True,
    llm=None,
    templates: list[dict] | None = None,
    image_descriptions: list[str] | None = None,   # M1.6
    sender: str | None = None,                     # M1.7: 草稿开头 "Dear customer," → "Dear Mr./Ms. {Last},"
) -> dict:
    """端到端：email → (triage, template, placeholders, filled_body)

    M1.6: image_descriptions 由 caller 提前调 vision 抽取（gpt-5.4 multimodal），
    作为额外 triage 上下文。多个描述会被拼到 user message 末尾。
    M1.7: sender 用于把 "Dear customer," 替换成 "Dear Mr./Ms. {Last Name},"
          —— 仅替换开头一次，不破坏模板正文。拿不到名字时不动草稿。
    """
    if templates is None:
        templates = load_xlsx()

    triage = classify_intent(
        email_body,
        llm=llm, use_llm=use_llm,
        image_descriptions=image_descriptions,
    )
    tmpl = pick_template(triage.intent, templates, body_text=email_body)

    placeholders = extract_placeholders(tmpl["template"]) if tmpl else []
    # Naive fill：VIN_MASK_xxx / user_xxx@example.com 已经有；其他占位符留空
    ctx = {
        "VIN":           "VIN_MASK_xxx",
        "vehicle model": "your vehicle",
        "feature":       triage.intent.replace("_", " "),
        "region":        triage.region,
        "strategy":      "supported",
        "vehicle":       "your vehicle",
    }
    filled = fill_placeholders_naive(tmpl["template"], ctx) if tmpl else ""

    # M1.7: 个性化称谓 "Dear customer," → "Dear Mr./Ms. {Last},"
    if sender and filled:
        from .greeting import personalize_greeting
        filled = personalize_greeting(filled, sender)

    return {
        "triage":       triage.model_dump(),
        "template_doc": tmpl,
        "placeholders": placeholders,
        "filled_body":  filled,
        # M2.5.1: 模板自带的「禁止/注意表述」原文（xlsx 第 11 列）。
        # 仅供审计 / 给 LLM 改写时知道模板边界用 —— 模板正文是 xlsx 写死的，
        # 不会触发 forbidden 短语（已验：93 条带 forbidden 的模板正文 0 命中）。
        "template_forbidden":  (tmpl or {}).get("forbidden", "") or "",
        "image_descriptions": image_descriptions or [],   # 透传给 process_mail
        "sender":       sender or "",                     # M1.7: 透传给 process_mail（审计用）
    }


# ── M2.5.1: template_forbidden post-check 工具函数 ───────────────────────
# 用途：EmailAgent._compliance_step() 对 LLM 起草正文做 post-check。
# 路径 B (run_template_reply) 不调 LLM 起草，模板正文是 xlsx 写死的，已避开
# forbidden 短语（93/93 验证），所以**不**在路径 B 暴露这个 post-check。
#
# 算法：
#   1. forbidden 原文 (中文「不要 X」) → _extract_action_phrases() 剥否定词抽 X
#   2. 在 body_md 里查 X 子串 / word-boundary → 命中返回 X，否则 None
#
# 跟 check_forbidden_phrasings() 的区别：F1-F8 是代码写死的 8 条通用禁则；
# template_forbidden 是 xlsx 每条模板独有的边界（94/95 非空）。
#
# 已知设计局限：xlsx forbidden 是中文、LLM draft 必为英文（CLAUDE.md 硬约束）。
# 子串匹配对英文译法无能为力。本防线**仅**防 LLM 中文 hallucination 或
# prompt injection 触发的中英混合违规，不替代 F1-F8 regex post-check。

import re as _re_mod

# 否定词 + 至少一个空白符 / 句首边界 —— 用 re.sub 从头剥，迭代直到剥不动。
_NEG_TOKENS_TF = (
    "不要", "不能", "不可", "不",
    "never", "please do not", "don't", "do not",
)


def _extract_action_phrases(forbidden_text: str) -> list[str]:
    """从 forbidden 句抽动作短语（去掉否定词后剩余的 2+ 字 token）。

    三步：
      1. 按句号/分号/逗号切分（保留多禁则）
      2. 每段剥前缀否定词（迭代）
      3. 全段扫一遍中段否定词
    """
    if not forbidden_text:
        return []
    s = forbidden_text.strip()
    parts = _re_mod.split(r"[。.;;；\n，,]+", s)
    out: list[str] = []
    for p in parts:
        p = p.strip().lstrip("，。,.；;：:")
        if not p:
            continue
        # 迭代去前缀否定词
        changed = True
        while changed and p:
            changed = False
            pl = p.lower()
            for tok in _NEG_TOKENS_TF:
                if pl.startswith(tok):
                    rest = p[len(tok):].lstrip()
                    if rest != p:
                        p = rest
                        changed = True
                    break
        # 全段去掉任何位置的否定 token + 两侧标点
        for tok in _NEG_TOKENS_TF:
            p = _re_mod.sub(_re_mod.escape(tok), " ", p, flags=_re_mod.IGNORECASE)
        p = _re_mod.sub(r"\s+", " ", p).strip().strip("，。,.；;：: ")
        if len(p) >= 2:
            out.append(p)
    return out


def _check_template_forbidden(
    body_md: str, forbidden_text: str,
) -> Optional[str]:
    """body_md 命中 forbidden 句里的动作短语 → 返回命中短语；否则 None。

    用途：EmailAgent._compliance_step() 对 LLM 起草正文做 post-check。
    命中 → verdict.pass_=False + violations.append + 触发 reflection loop 改写。
    """
    if not body_md or not forbidden_text:
        return None
    phrases = _extract_action_phrases(forbidden_text)
    if not phrases:
        return None
    body_lower = body_md.lower()
    for phrase in phrases:
        # 中文短语用子串匹配；英文 token 用 word-boundary
        if _re_mod.search(r"[一-鿿]", phrase):
            if phrase in body_md:
                return phrase
        else:
            if _re_mod.search(rf"\b{_re_mod.escape(phrase.lower())}\b", body_lower):
                return phrase
    return None


# ── CLI ─────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description="邮件 → intent 分类 → xlsx 模板 → 人工填写占位符 → 发",
    )
    p.add_argument("--email-file", help="邮件正文文件路径")
    p.add_argument("--email-stdin", action="store_true", help="从 stdin 读邮件")
    p.add_argument("--no-llm", action="store_true", help="用关键词分类，不调 LLM（smoke test）")
    p.add_argument("--out", help="写到 JSON 报告")
    args = p.parse_args()

    if args.email_file:
        email_body = Path(args.email_file).read_text(encoding="utf-8")
    elif args.email_stdin:
        email_body = sys.stdin.read()
    else:
        print("[ERR] --email-file 或 --email-stdin 二选一", file=sys.stderr)
        return 2

    llm = None
    if not args.no_llm:
        try:
            from .azure_llm import AzureLLMClient
            llm = AzureLLMClient()
        except KeyError as e:
            print(f"[ERR] missing env {e}", file=sys.stderr)
            return 3

    result = run_template_reply(email_body, use_llm=not args.no_llm, llm=llm)

    print("=" * 60)
    print(f"intent:    {result['triage']['intent']}")
    print(f"urgency:   {result['triage']['urgency']}")
    print(f"region:    {result['triage']['region']}")
    print(f"red_flags: {result['triage']['red_flags']}")
    if result["template_doc"]:
        print(f"category:  {result['template_doc']['category']}")
        print(f"placeholders to fill: {result['placeholders']}")
    print("=" * 60)
    print("FILLED TEMPLATE (human reviews & edits before sending):")
    print("-" * 60)
    print(result["filled_body"])
    print("=" * 60)

    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"[wrote] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())