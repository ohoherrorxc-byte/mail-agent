"""
build_xlsx_with_zh.py — 给 iSMART售后服务邮箱回复方法_20260308.xlsx
加 3 列中文意译：
  - Category 原文（英中混合）
  - Category 中文：英文/拼音部分的中文译法
  - Issue description 中文：英文问题描述的中文意译
  - Intent 中文：通过 CATEGORY_TO_INTENT 映射到 32 Intent 之一，再查表给中文

输入：iSMART售后服务邮箱回复方法_20260308.xlsx
输出：iSMART售后服务邮箱回复方法_zh_20260308.xlsx （同级，不覆盖原文件）

注意：
  - 不修改原文件。新文件带 _zh_ 后缀。
  - 中文映射是人工建表的，覆盖源 xlsx 里出现的所有 category；
    32 Intent 的中文名从 agent/schemas.py 的注释里抽（人工对照）。
  - Issue description 的中译：用关键词模式做"意译"而非真翻译——保留所有
    原始技术词（VIN、QR、App、T-Box、FOTA、ADAS、ACC/AEB/LKA/TJA、CPAA、
    AVN、IPK 等）原样，只翻译连接语/说明句。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
# 让 `from agent.template_reply import …` 能找到包
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 32 Intent 映射表：从 agent.template_reply 拿真实映射（顶层 import，避开脚本子目录）
from agent.template_reply import CATEGORY_TO_INTENT  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402

DEFAULT_INPUT = ROOT / "iSMART售后服务邮箱回复方法_20260308.xlsx"

# ── Category → 中文 ──────────────────────────────────────────────
# 23 个 category（含一个 typo 行 "App-车控车态相关\nApp-绑车解绑相关"，
# 我们按第一个 token 处理）。
CATEGORY_ZH: dict[str, str] = {
    # App 侧
    "App-车控车态相关":             "App 车辆控制与状态",
    "App-账号管理相关":             "App 账号管理",
    "App-绑车解绑相关":             "App 绑车/解绑",
    "App-消息中心相关":             "App 消息中心",
    "App-优化建议":                 "App 优化建议",
    "App-软件更新":                 "App/FICM 软件更新",
    # iSMART 平台
    "iSMART-功能咨询":              "iSMART 功能咨询",
    "iSMART-用户手册相关":          "iSMART 用户手册",
    "iSMART-支持区域":              "iSMART 支持区域",
    # FICM
    "FICM-激活相关":                "FICM 激活",
    "FICM-CPAA相关":                "FICM 无线 CarPlay/Android Auto",
    "FICM-软件更新":                "FICM 软件更新",
    "FICM-隐私协议相关":            "FICM 隐私协议",
    "FICM-地图相关":                "FICM 地图",
    # TSP
    "TSP-敏感数据要求":             "TSP 敏感数据请求",
    # ADAS
    "Adas-ACC":                     "ADAS 自适应巡航 (ACC)",
    "Adas-AEB":                     "ADAS 自动紧急制动 (AEB)",
    "Adas-LKA":                     "ADAS 车道保持辅助 (LKA)",
    "Adas-TJA":                     "ADAS 交通拥堵辅助 (TJA)",
    # 非 iSMART
    "非iSMART-功能咨询":            "非 iSMART 功能咨询",
    "非iSMART-IPK相关":             "非 iSMART IPK 警告灯",
    "非iSMART-充电相关":            "非 iSMART 充电",
    "非iSMART-不支持车型":          "非 iSMART 车型不支持",
    "非iSMART-售前售后服务":        "非 iSMART 售前/售后",
    "非iSMART-车身控制相关":        "非 iSMART 车身控制",
    # 关闭/已解决
    "关闭-已解决":                  "已关闭/已解决",
    "询问客户问题是否解决":         "询问客户问题是否解决",
}


# ── 32 Intent → 中文 ─────────────────────────────────────────────
# 来自 agent/schemas.py 的注释 + prompts.py 的 TRIAGE_SYSTEM 描述。
INTENT_ZH: dict[str, str] = {
    # App 侧
    "app_vehicle_control":      "App 车辆控制与状态（数据陈旧 / 错误码 47/59 / 车辆无响应）",
    "app_account":              "App 账号管理（登录失败 / session expired / 密码重置失败）",
    "app_binding":              "App 绑车/解绑（QR 不显示 / 二手车 / 强制解绑）",
    "app_update":               "App Store / Play Store / FICM 软件升级失败",
    "app_messages":             "App 消息中心（重复/过多告警）",
    "app_suggestion":           "App 优化建议",
    # iSMART 平台
    "ismart_data_plan":         "iSMART SIM 流量套餐",
    "ismart_remote_control":    "iSMART 远程控制（空调 / 车窗 / 充电功率）",
    "ismart_digital_key":       "iSMART 数字钥匙 / Keyless Start",
    "ismart_fota":              "iSMART FOTA / OTA 升级可用性",
    "ismart_connectivity":      "iSMART 无线 CarPlay/Android Auto / EV Trip Planner / e-call / 区域扩展",
    "ismart_account_inquiry":   "iSMART 主副账号 / 密码重置 / PIN / 副账号创建",
    "ismart_owner_change":      "iSMART 车主变更（卖车/过户）",
    "ismart_region_support":    "iSMART 国家/区域支持查询",
    "ismart_privacy":           "iSMART 隐私协议（拒绝后核心功能关闭）",
    "ismart_user_manual":       "iSMART 车主手册查询",
    "ismart_feature_inquiry":   "iSMART 其它功能咨询",
    # FICM
    "ficm_activation":          "FICM 激活（指南缺失或不清晰）",
    "ficm_cpaa":                "FICM 无线 CarPlay/Android Auto 不稳定",
    "ficm_map":                 "FICM 地图升级失败 / 不可用",
    # TSP
    "tsp_sensitive_data":       "TSP 敏感数据请求（GPS/实时位置/车辆密码）",
    # ADAS
    "adas_acc":                 "ADAS ACC 自适应巡航",
    "adas_aeb":                 "ADAS AEB 自动紧急制动",
    "adas_lka":                 "ADAS LKA 车道保持辅助",
    "adas_tja":                 "ADAS TJA 交通拥堵辅助",
    # 非 iSMART
    "hardware_warranty":        "硬件保修（保修索赔/零件/IPK 警告灯/车身控制）",
    "charging":                 "充电失败",
    "delivery_handover":        "售前/交车培训",
    "vehicle_incompatible":     "车型不支持 iSMART",
    # 生命周期
    "complaint":                "投诉（含 PR / 律师 / 媒体升级）",
    "follow_up_resolved":       "询问解决状态 / 已关闭",
    "other":                    "其它（fallback）",
}

# Issue description 中关键词 → 中文意译。
# 翻译策略：保留所有技术词原样（VIN、QR、App、T-Box、FOTA、ADAS、ACC/AEB/LKA/TJA、
# CPAA、AVN、IPK、TBOX 等），只译连接语和说明句。
DESC_TRANSLATION_RULES: list[tuple[re.Pattern, str]] = [
    # 整段结构："1. xxx: yyy"
    (re.compile(r"^\s*\d+\.\s+", re.M), ""),  # 去编号
    # 段首常见动词短句
    (re.compile(r"\bStale/Old Vehicle Data\b", re.I), "车辆状态数据陈旧/旧"),
    (re.compile(r"\bThe app consistently displays vehicle status\b", re.I), "App 持续显示车辆状态"),
    (re.compile(r"\bVehicle Not Responding\b", re.I), "车辆无响应"),
    (re.compile(r"\bError Code (\d+)\b", re.I), r"错误码 \1"),
    (re.compile(r"\bMedia evidence\b.*?\bshall be evaluated\b", re.I),
     "需评估视频/截图证据"),
    (re.compile(r"\bMedia evidence\b.*?\bto diagnose\b", re.I),
     "用视频/截图证据诊断"),
    (re.compile(r"\bshall be evaluated\b", re.I), "应评估"),
    (re.compile(r"\bshall be\b", re.I), "应"),
    (re.compile(r"\bshall\b", re.I), "应"),
    (re.compile(r"\bscreenshot(s)?\b", re.I), "截图"),
    (re.compile(r"\bvideo(s)?\b", re.I), "视频"),
    (re.compile(r"\bnetwork connectivity\b", re.I), "网络连接"),
    (re.compile(r"\bin the current location\b", re.I), "在当前位置"),
    (re.compile(r"\bconnection\b", re.I), "连接"),
    (re.compile(r"\bindicates\b", re.I), "表明"),
    (re.compile(r"\bindicate(s|d)?\b", re.I), "表明"),
    (re.compile(r"\bcommand(s)?\b", re.I), "指令"),
    (re.compile(r"\bsuccessfully sends commands to the vehicle\b", re.I),
     "App 成功发出指令给车辆"),
    (re.compile(r"\bthe vehicle does not (respond|react)\b", re.I),
     "车辆未响应"),
    (re.compile(r"\bPlease\s+", re.I), "请"),
    (re.compile(r"\bprovide\b", re.I), "提供"),
    (re.compile(r"\bthank(s|ed)? you\b", re.I), "感谢"),
    (re.compile(r"\bwe (will|would) (need|require)\b", re.I), "我们需要"),
    (re.compile(r"\bin order to\b", re.I), "为了"),
    (re.compile(r"\bin order\b", re.I), "为了"),
    (re.compile(r"\bbased on\b", re.I), "根据"),
    (re.compile(r"\bcheck(ing)?\b", re.I), "核查"),
    (re.compile(r"\bfurther\b", re.I), "进一步"),
    (re.compile(r"\bissue\b", re.I), "问题"),
    (re.compile(r"\bsymptom\b", re.I), "症状"),
    (re.compile(r"\bconfirm(ed|ation)?\b", re.I), "确认"),
    (re.compile(r"\bbefore\b", re.I), "在...前"),
    (re.compile(r"\bwhether\b", re.I), "是否"),
    (re.compile(r"\bor not\b", re.I), "或否"),
    (re.compile(r"\bincluding\b", re.I), "包括"),
    (re.compile(r"\bif any\b", re.I), "如有"),
    (re.compile(r"\bif (provided|applicable)\b", re.I), "如提供"),
    (re.compile(r"\bafter\b", re.I), "之后"),
    (re.compile(r"\bcan be\b", re.I), "可以"),
    (re.compile(r"\bcaused by\b", re.I), "由...导致"),
    (re.compile(r"\bdue to\b", re.I), "由于"),
    (re.compile(r"\bbecause\b", re.I), "因为"),
    (re.compile(r"\bin this case\b", re.I), "在这种情况下"),
    (re.compile(r"\bthen\b", re.I), "则"),
    (re.compile(r"\bmeans\b", re.I), "意味着"),
    (re.compile(r"\byou (can|may|need|should)\b", re.I), "您可以/可以/需要/应"),
    (re.compile(r"\byou (have|had)\b", re.I), "您"),
    (re.compile(r"\byou\b", re.I), "您"),
    (re.compile(r"\byour\b", re.I), "您的"),
    (re.compile(r"\bour team\b", re.I), "我方团队"),
    (re.compile(r"\bour\b", re.I), "我方"),
    (re.compile(r"\bwe (will|would|are|can)\b", re.I), "我方会"),
    (re.compile(r"\bwe\b", re.I), "我方"),
    (re.compile(r"\bin the vehicle\b", re.I), "车辆内"),
    (re.compile(r"\bvehicle\b", re.I), "车辆"),
    (re.compile(r"\bcustomer\b", re.I), "客户"),
    (re.compile(r"\bprovided\b", re.I), "已提供"),
    (re.compile(r"\bsame\b", re.I), "相同"),
    (re.compile(r"\binformation\b", re.I), "信息"),
    (re.compile(r"\brelevant\b", re.I), "相关"),
    (re.compile(r"\brequire(d|s|ment)?\b", re.I), "需要"),
    (re.compile(r"\bwhich\b", re.I), "其中"),
    (re.compile(r"\bthat\b", re.I), "该"),
    (re.compile(r"\bwhile\b", re.I), "而"),
    (re.compile(r"\bonly\b", re.I), "仅"),
    (re.compile(r"\balso\b", re.I), "也"),
    (re.compile(r"\bmust\b", re.I), "必须"),
    (re.compile(r"\bsend (us|to us)\b", re.I), "发送给我们"),
    (re.compile(r"\betc\.?\b", re.I), "等"),
    (re.compile(r"\.\s*$", re.M), "。"),
]


def _first_category_token(cat: str) -> str:
    """原表 Category 字段可能形如 'A/B' 或 'A\nB'；按 '/' 切，取第一个非空 token。"""
    parts = re.split(r"[/\n]", cat)
    for p in parts:
        p = p.strip()
        if p:
            return p
    return cat.strip()


def translate_description(text: str) -> str:
    """Issue description 关键词意译（保留技术词）。仅作为 --no-llm fallback。"""
    if not text:
        return ""
    out = text
    for pat, repl in DESC_TRANSLATION_RULES:
        out = pat.sub(repl, out)
    # 收尾：多余空白压平
    out = re.sub(r"\s+", " ", out).strip()
    return out


# ── LLM 翻译 Issue description ──────────────────────────────────

_ISSUE_TRANSLATE_SYSTEM = """\
You translate iSMART after-sales email Issue descriptions into Chinese (zh-CN).

Rules:
- Keep ALL technical terms untranslated. Do not transliterate, do not translate:
  VIN, QR, App, T-Box, FOTA, OTA, ADAS, ACC, AEB, LKA, TJA, CPAA,
  CarPlay, Android Auto, AVN, IPK, TPMS, iSMART, FICM, App Store, Google Play,
  Tesla, Supercharger, CCS, NACS, Type 2, Bluetooth, USB-C, Lightning,
  RPM, kW, km/h, GPS, OTA, T-Box, PCM, BCM, VIN, OBD, MIL, ECU, EV, PHEV.
- Keep brand names untranslated: MG, Tesla, Apple, Google, Android, iPhone.
- Keep error codes in original Arabic numerals: "Error Code 59", "Error Code 47".
- Keep units and numbers in original form: "10–15 km/h", "1.5 kW", "60 km/h", "10–15 minutes".
- Sentence style: concise Chinese, no flowery wording. Keep parallel structure of the original numbered list.
- Each numbered item in the source becomes one numbered item in Chinese.
- For empty input rows, output an empty string.
- For rows that are short operational reminders (e.g. "Check the backend system. Currently,
  this vehicle is not bound to any account."), translate naturally as a single sentence in 中文.
- IMPORTANT: output ONLY a JSON object — no markdown, no extra prose.
"""


class _IssueItem(BaseModel):
    row: int = Field(..., description="源 xlsx 行号 (1-based, 不含 header)")
    description_zh: str = Field(..., description="中文意译；保留所有技术词")


class _IssueTranslateResp(BaseModel):
    translations: list[_IssueItem]


_TEMPLATE_TRANSLATE_SYSTEM = """\
You translate iSMART after-sales email reply templates into Chinese (zh-CN).

The output is for INTERNAL reference (training, knowledge base review).
Production email DRAFTS still go out in English — this is just bilingual documentation.

Rules:
- Translate the body into natural concise Chinese, keeping the original tone and politeness level.
- "Dear customer," → "尊敬的客户，"
- "Thanks for your email." / "Thank you for your email." → "感谢您的来信。"
- "Best regards," / "Sincerely," → "此致敬礼，"
- "iSMART Service Team" / "iSMART Service Team" → "iSMART 客服团队" (keep brand name)
- Preserve numbered lists (1. 2. 3.) and bullet structures.
- Keep ALL technical terms untranslated, same list as Issue rules
  (VIN, QR, App, T-Box, FOTA, OTA, ADAS, ACC/AEB/LKA/TJA, CPAA, CarPlay, Android Auto,
   AVN, IPK, TPMS, iSMART, FICM, Tesla, CCS, NACS, etc.).
- Keep any URL, email, phone number, model code, or VIN MASK <hash> verbatim.
- If a row has no template body, output an empty string.
- IMPORTANT: output ONLY a JSON object — no markdown, no extra prose.
"""


class _TemplateItem(BaseModel):
    row: int = Field(..., description="源 xlsx 行号 (1-based, 不含 header)")
    template_zh: str = Field(..., description="中文意译；保留所有技术词、占位符、签名块")


class _TemplateTranslateResp(BaseModel):
    translations: list[_TemplateItem]


def translate_descriptions_with_llm(
    descs: list[tuple[int, str]],
    *,
    batch_size: int = 15,
) -> dict[int, str]:
    """调 Azure OpenAI gpt-5.4 把 descs 行逐条翻译为中文意译。
    保留所有技术词原样；分批以避免单次 prompt 超 max_completion_tokens。
    Returns: {row_index: description_zh}
    """
    from agent.azure_llm import AzureLLMClient

    llm = AzureLLMClient()
    out: dict[int, str] = {}

    # 过滤掉空行
    non_empty = [(row, txt) for row, txt in descs if txt and txt.strip()]
    if not non_empty:
        return out

    return _batched_translate(
        llm=llm,
        non_empty=non_empty,
        system_prompt=_ISSUE_TRANSLATE_SYSTEM,
        schema_cls=_IssueTranslateResp,
        batch_size=batch_size,
        role="issue_translate_zh",
    )


def translate_templates_with_llm(
    templates: list[tuple[int, str]],
    *,
    batch_size: int = 8,
) -> dict[int, str]:
    """调 Azure OpenAI 翻译 Template 列。模板比 Issue description 长，batch 收紧到 8。"""
    from agent.azure_llm import AzureLLMClient

    llm = AzureLLMClient()
    out: dict[int, str] = {}

    non_empty = [(row, txt) for row, txt in templates if txt and txt.strip()]
    if not non_empty:
        return out

    return _batched_translate(
        llm=llm,
        non_empty=non_empty,
        system_prompt=_TEMPLATE_TRANSLATE_SYSTEM,
        schema_cls=_TemplateTranslateResp,
        batch_size=batch_size,
        role="template_translate_zh",
    )


def _batched_translate(
    *,
    llm,
    non_empty: list[tuple[int, str]],
    system_prompt: str,
    schema_cls: type,
    batch_size: int,
    role: str,
) -> dict[int, str]:
    """通用 batch 翻译骨架：每批发送 user 消息 "[<row>] <text>\\n\\n[<row>] <text>..."，
    LLM 返回 schema 校验的 JSON 列表。"""
    import time
    out: dict[int, str] = {}
    for batch_start in range(0, len(non_empty), batch_size):
        batch = non_empty[batch_start:batch_start + batch_size]
        user_lines = [f"[{row}] {txt}" for row, txt in batch]
        user_msg = "\n\n".join(user_lines)

        resp, _call = llm.invoke_json(
            schema=schema_cls,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            reasoning_effort="low",
            role=role,
            max_completion_tokens=8192,
        )
        for item in resp.translations:
            row = item.row
            text = item.description_zh if hasattr(item, "description_zh") else item.template_zh
            out[row] = text
        time.sleep(0.2)
    return out


def build_xlsx_with_zh(
    input_path: Path,
    output_path: Path,
    *,
    use_llm: bool = True,
) -> int:
    """读 input，写 output；返回写入行数。

    use_llm=True  → Issue 中文 + Template 中文 都通过 Azure OpenAI 翻译
    use_llm=False → 两列都走本地关键词意译（fallback，离线可用）
    """
    wb_in = openpyxl.load_workbook(input_path, data_only=True)
    ws_in = wb_in.active

    # 在内存里建新 wb；保留原 3 列 + 加 4 列
    wb_out = openpyxl.Workbook()
    ws_out = wb_out.active
    ws_out.title = ws_in.title
    # 表头：原 3 列 + 4 中文列
    headers = [
        "Category", "Template", "Issue description",
        "Category 中文", "Template 中文", "Issue 中文", "Intent 中文",
    ]
    ws_out.append(headers)

    # 先把所有行收上来 → 决定 LLM 翻译前是否要翻译；再写
    rows_data: list[tuple[int, str, str, str]] = []  # (row_idx, cat, tmpl, desc)
    for src_row_idx, row in enumerate(ws_in.iter_rows(min_row=2, values_only=True), start=2):
        cat = (row[0] or "").strip()
        tmpl = (row[1] or "").strip()
        desc = (row[2] or "").strip()
        if not cat and not tmpl:
            continue
        rows_data.append((src_row_idx, cat, tmpl, desc))

    # Issue 中文 / Template 中文 整列翻译 (可选)
    desc_zh_map: dict[int, str] = {}
    tmpl_zh_map: dict[int, str] = {}
    if use_llm and rows_data:
        print(f"[llm] translating {len(rows_data)} Issue descriptions (batch=15)…", flush=True)
        try:
            desc_zh_map = translate_descriptions_with_llm(
                [(r, d) for r, _, _, d in rows_data],
                batch_size=15,
            )
        except Exception as e:
            print(f"[llm] WARN: Azure Issue translation failed: {e!r}; "
                  f"falling back to keyword translate_description()", flush=True)
            desc_zh_map = {}

        print(f"[llm] translating {len(rows_data)} Templates (batch=8)…", flush=True)
        try:
            tmpl_zh_map = translate_templates_with_llm(
                [(r, t) for r, _, t, _ in rows_data],
                batch_size=8,
            )
        except Exception as e:
            print(f"[llm] WARN: Azure Template translation failed: {e!r}; "
                  f"falling back to keyword translate_description()", flush=True)
            tmpl_zh_map = {}

    # 32 Intent 映射表已在模块顶部导入
    written = 0
    for src_row_idx, cat, tmpl, desc in rows_data:
        first_token = _first_category_token(cat)
        cat_zh = CATEGORY_ZH.get(first_token, first_token)
        if src_row_idx in desc_zh_map:
            desc_zh = desc_zh_map[src_row_idx]
        else:
            desc_zh = translate_description(desc)
        if src_row_idx in tmpl_zh_map:
            tmpl_zh = tmpl_zh_map[src_row_idx]
        else:
            tmpl_zh = translate_description(tmpl)
        # Intent: 取 first token 在 CATEGORY_TO_INTENT 里的值（fallback "ismart_feature_inquiry"）
        intent = CATEGORY_TO_INTENT.get(first_token, "ismart_feature_inquiry")
        intent_zh = INTENT_ZH.get(intent, intent)

        ws_out.append([cat, tmpl, desc, cat_zh, tmpl_zh, desc_zh, intent_zh])
        written += 1

    # 列宽
    widths = [30, 80, 80, 30, 80, 80, 60]
    for i, w in enumerate(widths, 1):
        ws_out.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws_out.freeze_panes = "A2"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb_out.save(output_path)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                    help="源 xlsx（默认 iSMART售后服务邮箱回复方法_20260308.xlsx）")
    ap.add_argument("--output", type=Path, default=None,
                    help="输出 xlsx（默认 <input stem>_zh_<input stem suffix>.xlsx）")
    ap.add_argument("--no-llm", action="store_true",
                    help="不用 LLM 翻译 Issue description 列，纯离线 (默认调 Azure OpenAI)")
    args = ap.parse_args()

    if args.output is None:
        stem = args.input.stem          # iSMART售后服务邮箱回复方法_20260308
        args.output = args.input.parent / f"{stem}_zh.xlsx"

    n = build_xlsx_with_zh(args.input, args.output, use_llm=not args.no_llm)

    # 摘要
    print(f"input:  {args.input}")
    print(f"output: {args.output}")
    print(f"rows written: {n}")

    # 加载输出 + 展示头部 3 行中文列
    wb = openpyxl.load_workbook(args.output, data_only=True)
    ws = wb.active
    print(f"output sheet: {ws.title}  rows: {ws.max_row}  cols: {ws.max_column}")
    print("--- 前 3 行中文列 ---")
    # 列序：Category / Template / Issue / Category 中文 / Template 中文 / Issue 中文 / Intent 中文
    for row in ws.iter_rows(min_row=2, max_row=4, values_only=True):
        print(f"  cat_zh={row[3]!r}")
        print(f"  tmpl_zh={row[4][:200]!r}  (len={len(row[4] or '')})")
        print(f"  desc_zh={row[5][:120]!r}")
        print(f"  intent_zh={row[6]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())