"""
template_reply.py — M1.5 过渡工具：跳过 LLM 草稿，直接查 xlsx 模板。

为什么需要：
M1 PoC 完整流水线（Triage → Retrieval → Drafting → Compliance）
跑一封邮件要 4-7 个 LLM 调用，单封 60-90 秒、$0.10+。
但 iSmart 客服 77 条模板已经覆盖 80% 流量，
对模板能直接答的邮件，**不需要 LLM 起草** ——
Triage 判 intent → 查 xlsx 模板 → 人改两行 → 发。

速度：单封约 2–3 次 LLM（Triage + 选 template_id + 建稿前语义复核），正文仍用 xlsx，不让模型起草。

后续 M2 规范慢慢补：模板覆盖不到的 intent（边界 case / 多步排查）
再走完整 EmailAgent 流水线。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import openpyxl

ROOT = Path(__file__).resolve().parent.parent

# ── xlsx 场景 enrichment 缓存（M2.5.5）───────────────────────────────
# 离线脚本 [scripts/enrich_xlsx_scenarios.py](scripts/enrich_xlsx_scenarios.py)
# 把 iSMART xlsx 第 4 列「问题场景」翻成英文 + 扩展 8-15 关键词，
# 落到 agent/xlsx_scenarios_enriched.json。runtime 启动期 load 一次，
# 后续 pick_template：闸门 + GPT 选 template_id（同 Intent 候选），
# 失败则英文关键词打分兜底。GPT 不写客户正文。
_ENRICHED_JSON_PATH = ROOT / "agent" / "xlsx_scenarios_enriched.json"

# 模块级缓存：避免每次 pick_template 都重新读 JSON
_ENRICHED_CACHE: dict[str, dict] | None = None
_ENRICHED_LOAD_ERROR: str | None = None


def _load_enriched_json() -> dict[str, dict]:
    """读 xlsx_scenarios_enriched.json → {template_id: enriched_record}。

    失败时不抛异常（runtime 不能让一条坏 JSON 卡死所有 triage）；
    只记一次错误日志，返回空 dict。后续 pick_template 在没有 enrichment
    数据时会 fallback 到 body token overlap。
    """
    global _ENRICHED_CACHE, _ENRICHED_LOAD_ERROR
    if _ENRICHED_CACHE is not None:
        return _ENRICHED_CACHE
    if _ENRICHED_LOAD_ERROR is not None:
        return {}
    if not _ENRICHED_JSON_PATH.exists():
        _ENRICHED_LOAD_ERROR = f"missing: {_ENRICHED_JSON_PATH}"
        return {}
    try:
        payload = json.loads(_ENRICHED_JSON_PATH.read_text(encoding="utf-8"))
        out: dict[str, dict] = {}
        for t in payload.get("templates", []):
            tid = t.get("template_id", "")
            if tid:
                out[tid] = t
        _ENRICHED_CACHE = out
        return out
    except Exception as e:
        _ENRICHED_LOAD_ERROR = f"{type(e).__name__}: {e}"
        return {}

# 同一进程里复用 schema
from .schemas import TemplatePick, TriageResult


# ── xlsx 加载 ──────────────────────────────────────────────────────────

# xlsx Category → Intent enum 映射（人工建立，从 [agent/schemas.py:18-69] 推出）
CATEGORY_TO_INTENT: dict[str, str] = {
    # ── 原 v1 category（来自 20260308.xlsx，77 条模板表） ──────────
    "App-车控车态相关":       "app_vehicle_control",
    "App-账号管理相关":       "app_account",
    "App-绑车解绑相关":       "app_binding",
    "App-消息中心相关":       "app_messages",
    "APP-消息中心相关":       "app_messages",
    "APP-充电管理相关":       "charging",
    "App-充电管理相关":       "charging",
    "App-优化建议":           "app_suggestion",
    "App-软件更新":           "app_update",
    "iSMART-功能咨询":        "ismart_feature_inquiry",
    "iSMART-用户手册相关":     "ismart_user_manual",
    "iSMART-支持区域":        "ismart_region_support",
    "iSMART-支持车区域":      "ismart_region_support",  # 20260729 主表 L2 原文
    "TSP-敏感数据要求":       "tsp_sensitive_data",
    # ── M2.7: v5 模板新加：4 个之前 0 模板的 intent ─────────────
    # iSMART 业务里常合并到 iSMART-功能咨询 / App-账号管理相关 等相邻类目；
    # 单独建 4 个 L2 类目让模板路由能直接命中。
    "iSMART-账号查询":        "ismart_account_inquiry",
    "iSMART-车主变更":        "ismart_owner_change",
    "iSMART-数字钥匙":        "ismart_digital_key",
    "APP-蓝牙钥匙":           "ismart_digital_key",
    "iSMART-流量套餐":        "ismart_data_plan",
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
    "非ismart-售前售后服务":  "delivery_handover",
    "Adas-ACC":              "adas_acc",
    "Adas-AEB":              "adas_aeb",
    "Adas-LKA":              "adas_lka",
    "Adas-TJA":              "adas_tja",
    "关闭-已解决":            "follow_up_resolved",

    # ── M1.8 扩展：来自 operations_mail.xls 的 22,430 条 AUTO: token 报告 ──
    # （eval/runs/auto_intent_coverage.json  53557 行统计中 AUTO 未映射的 token）
    "FICM-网络相关":           "ismart_connectivity",   # 92 次
    "FICM-Voice Service相关":  "ficm_voice",
    "FICM-Radio相关":          "ficm_radio",
    "FICM-蓝牙相关":           "ficm_bluetooth",
    "FICM-自动重启或黑屏":     "hardware_warranty",    # 6 次（硬件故障）
    "iSMART-翻译相关":         "ismart_translation",
    "非iSMART-空调相关":       "hardware_warranty",    # 2 次
    "FICM-充电相关":           "charging",             # 2 次
    "FICM-Online Service相关": "ismart_connectivity",   # 2 次（在线服务 / 远程连接类）
    "FICM-无法记忆驾驶模式":   "hardware_warranty",    # 1 次
    # ── Negative token：2,099 封负面情绪 → complaint ──────────
    # parse_category() 也会把 "Negative" 加入 status_toks 列表，
    # CATEGORY_TO_INTENT 接管后 → intent_mapped=complaint
    "Negative":               "complaint",
}

# 9-18 拍板：PLAN-001 / NET-PLAN-001 下线。3G、重启套餐、问到期档位均无行 → 转人工。
_RETIRED_TEMPLATE_IDS = frozenset({"PLAN-001", "NET-PLAN-001", "DK-001"})


def load_xlsx(
    paths: list[Path] | None = None,
    *,
    include_legacy: bool = False,
) -> list[dict]:
    """加载模板表，可指定多个文件；前面的优先级高。

    默认（生产路由）：**只加载** `iSMART自动回复知识库_全量模板_*.xlsx`
    （20260729 起为唯一派单标准：等级 / 必填信息 / 人工条件 / 禁则）。
    旧 v2/v3/v4 3 列表没有这些列，混进路由会把几乎全部行标成 auto_draft_safe。

    include_legacy=True：额外加载 v*_*.xlsx + 20260308 原表（评测 / 对照旧覆盖用）。
    若知识库新表不存在，自动回退到旧表，避免 0 模板。

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
        kb = sorted(
            (
                p for p in ROOT.glob("iSMART自动回复知识库_全量模板_*.xlsx")
                if not p.name.startswith("~$")
            ),
            reverse=True,
        )
        # 日期最新的全量表优先；旧表只补新表没有的 template_id。
        paths.extend(kb)
        if include_legacy or not kb:
            for p in sorted(ROOT.glob("iSMART售后服务邮箱回复方法_v*_*.xlsx"), reverse=True):
                paths.append(p)
            paths.append(ROOT / "iSMART售后服务邮箱回复方法_20260308.xlsx")

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    # 已下线 ID：旧全量表里还有行，也当作删除，不补进路由。
    seen_ids: set[str] = set(_RETIRED_TEMPLATE_IDS)
    for path in paths:
        if not path.exists():
            continue
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = (
            wb["自动回复知识库"]
            if "自动回复知识库" in wb.sheetnames
            else wb.active
        )
        # 探测 schema：第 1 行 col1 是 "模板ID" → 新表；否则旧表（3 列）
        first_cell = ws.cell(row=1, column=1).value or ""
        is_new_schema = str(first_cell).strip() == "模板ID"
        if is_new_schema:
            scope_kws = _parse_category_dict(wb)
            _load_new_schema_rows(
                ws, path, rows, seen, scope_kws=scope_kws, seen_ids=seen_ids,
            )
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
    "FICM-Volume相关":         "ismart_feature_inquiry",
    "FICM-界面层级复杂":       "ismart_feature_inquiry",
    "FICM-自动重启":           "hardware_warranty",
    "IPK相关":                "hardware_warranty",
    "TSP-车辆信息缺失":        "tsp_data_missing",
    "iSMART-翻译相关":         "ismart_translation",
    "iSMART-支持车区域":       "ismart_region_support",
    "FICM-蓝牙相关":           "ficm_bluetooth",
    "FICM-Voice Service相关":  "ficm_voice",
    "FICM-Radio相关":          "ficm_radio",
    "FICM-Music相关":          "ismart_music_streaming",
    "关闭-无效邮件":           "follow_up_resolved",
    "非iSMART-IPK相关":        "hardware_warranty",
    "非iSMART-空调相关":       "hardware_warranty",
    "非iSMART-车身控制相关":    "hardware_warranty",
    # ── M2.7: 4 个 v5 新 intent 的 L2 路由（新表 schema 用） ──────
    "iSMART-账号查询":        "ismart_account_inquiry",
    "iSMART-车主变更":        "ismart_owner_change",
    "iSMART-数字钥匙":        "ismart_digital_key",
    "APP-蓝牙钥匙":           "ismart_digital_key",
    "iSMART-流量套餐":        "ismart_data_plan",
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


_LEVEL_NOTE_RE = re.compile(r"【([^】]+)】")


def _extract_level_note(raw: str) -> str:
    """抽出等级列里的【人工说明】，例如「需接入 TCMP」「强制解绑完才可回复」。"""
    s = (raw or "").strip()
    if not s:
        return ""
    m = _LEVEL_NOTE_RE.search(s)
    if m:
        return m.group(1).strip()
    if "\n" in s:
        tail = s.split("\n", 1)[1].strip().strip("【】")
        return tail
    return ""


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


def _parse_category_dict(wb) -> dict[str, list[str]]:
    """读「分类字典」sheet：二级分类 → 适用范围短语，并入路由关键词。"""
    if "分类字典" not in getattr(wb, "sheetnames", []):
        return {}
    out: dict[str, list[str]] = {}
    ws = wb["分类字典"]
    for row in ws.iter_rows(min_row=2, values_only=True):
        l2 = str(row[1] or "").strip() if row and len(row) > 1 else ""
        scope = str(row[2] or "").strip() if row and len(row) > 2 else ""
        if not l2 or not scope:
            continue
        parts = [p.strip() for p in re.split(r"[、，,;/]+", scope) if len(p.strip()) >= 2]
        if parts:
            out[l2] = parts
    return out


def _new_schema_col_index(ws) -> dict[str, int]:
    """飞书导出可能多一列「修改日期及状态」；按表头取列，缺省回退旧 12 列位移。"""
    headers = [
        str(ws.cell(1, c).value or "").strip()
        for c in range(1, 16)
    ]

    def find(*names: str, default: int) -> int:
        for name in names:
            if name in headers:
                return headers.index(name)
        return default

    offset = 1 if headers[1:2] == ["修改日期及状态"] else 0
    return {
        "tmpl_id": find("模板ID", default=0),
        "status": find("修改日期及状态", default=-1),
        "l1": find("一级类别", default=1 + offset),
        "l2": find("二级问题分类", default=2 + offset),
        "scenario": find("问题场景", default=3 + offset),
        "keywords": find("触发关键词", default=4 + offset),
        "level": find("自动回复等级", default=5 + offset),
        "req": find("必填信息", default=6 + offset),
        "tmpl": find("英文邮件模板", "自动回复模板", default=7 + offset),
        "next": find("下一步动作", default=8 + offset),
        "human": find("人工介入条件", default=9 + offset),
        "forbidden": find("禁止|注意表述", "禁止/注意表述", default=10 + offset),
        "src_type": find("来源类型", default=11 + offset),
    }


def _row_cell(row, idx: int) -> str:
    if idx < 0 or idx >= len(row):
        return ""
    v = row[idx]
    return (v or "").strip() if isinstance(v, str) else str(v or "").strip()


def _load_new_schema_rows(
    ws, path: Path, rows: list[dict], seen: set[tuple[str, str]],
    scope_kws: dict[str, list[str]] | None = None,
    seen_ids: set[str] | None = None,
) -> None:
    """解析 M2.5 自动回复知识库新表（12 列，或带修改日期列）。"""
    if seen_ids is None:
        seen_ids = set()
    col = _new_schema_col_index(ws)
    for row in ws.iter_rows(min_row=2, values_only=True):
        tmpl_id   = _row_cell(row, col["tmpl_id"])
        status    = _row_cell(row, col["status"])
        l1_cat    = _row_cell(row, col["l1"])
        l2_cat    = _row_cell(row, col["l2"])
        scenario  = _row_cell(row, col["scenario"])
        keywords  = _row_cell(row, col["keywords"])
        level_raw = _row_cell(row, col["level"])
        req_info  = _row_cell(row, col["req"])
        tmpl      = _row_cell(row, col["tmpl"])
        next_step = _row_cell(row, col["next"])
        human_trig= _row_cell(row, col["human"])
        forbidden = _row_cell(row, col["forbidden"])
        src_type  = _row_cell(row, col["src_type"])
        if not tmpl_id and not tmpl:
            continue
        if "删除" in status or tmpl_id in _RETIRED_TEMPLATE_IDS:
            if tmpl_id:
                seen_ids.add(tmpl_id)
            continue
        # 飞书 MAP-004 = 导航没声音；地图数据质量在旧表同 ID，改挂 MAP-006。
        if tmpl_id == "MAP-004" and re.search(r"POI|数据质量|charging.?station|map data", scenario + " " + tmpl, re.I):
            tmpl_id = "MAP-006"

        intent = _resolve_intent_from_l2(l2_cat)
        if tmpl_id in {"APP-GUIDE-002", "APP-GUIDE-003"}:
            # L2「App-引导手册」会进 ismart_user_manual；副账号应跟账号咨询走。
            intent = "ismart_account_inquiry"
        if str(tmpl_id).startswith("APP-DIGITAL"):
            intent = "ismart_digital_key"
        if str(tmpl_id).startswith("APP-NOTIFICATION"):
            intent = "app_messages"
        if tmpl_id in {"DK-003", "DK-004"}:
            intent = "ismart_feature_inquiry"
        if tmpl_id == "MAP-006":
            intent = "ficm_map"
        if tmpl_id in {"STATUS-012", "STATUS-013"}:
            intent = "charging"
        if tmpl_id == "SVC-004":
            intent = "delivery_handover"
        if tmpl_id in {"ACC-007", "ACC-008", "ACC-009"}:
            intent = "app_account"
        if not intent:
            continue

        level = _normalize_level(level_raw)
        level_note = _extract_level_note(level_raw)
        # REG-001：不支持车型/区域需人工确认，保持知识库「不可自动回复」（9-23）。
        if tmpl_id == "MAP-002" and level == "no_auto_reply":
            level = "auto_draft_safe"
        req_groups = _parse_required_info(req_info)
        # M2.5.4: 默认称谓 "Dear Customer," → {{salutation}}, 占位符由
        # greeting.personalize_greeting() 在草稿生成时按发件人姓名替换
        tmpl = _normalize_greeting_placeholder(tmpl)

        if tmpl_id:
            if tmpl_id in seen_ids:
                continue
            seen_ids.add(tmpl_id)
        else:
            key = (intent, tmpl)
            if key in seen:
                continue
            seen.add(key)
        rows.append({
            "intent":         intent,
            "template":       tmpl,
            "category":       l2_cat,
            "level":          level,
            "level_note":     level_note,
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
            # M2.5.5: 由 scripts/enrich_xlsx_scenarios.py 离线写入的
            # scenario_en / keywords_existing / keywords_extracted /
            # keywords_all / scenario_en_tokens 五个字段。这里 merge 进来：
        })
        # M2.5.5: merge enriched JSON（scenario_en + 扩展关键词）
        enr = _load_enriched_json().get(tmpl_id)
        if enr:
            rows[-1]["scenario_en"]        = enr.get("scenario_en", "")
            rows[-1]["keywords_existing"]  = list(enr.get("keywords_existing") or [])
            rows[-1]["keywords_extracted"] = enr.get("keywords_extracted", [])
            rows[-1]["keywords_all"]       = enr.get("keywords_all", [])
            rows[-1]["scenario_en_tokens"] = enr.get("scenario_en_tokens", [])
        else:
            rows[-1]["keywords_existing"] = [
                k.strip() for k in keywords.split(",") if k.strip()
            ]
        # 分类字典「适用范围」并入 existing 关键词（中文 L2 路由）
        extra = (scope_kws or {}).get(l2_cat) or []
        if extra:
            have = {k.lower() for k in rows[-1].get("keywords_existing") or []}
            merged = list(rows[-1].get("keywords_existing") or [])
            for p in extra:
                if p.lower() not in have:
                    merged.append(p)
                    have.add(p.lower())
            rows[-1]["keywords_existing"] = merged


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
                    "level_note":     "",
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
    thread_context: str | None = None,             # M2.5.5: Graph 会话线程历史
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

    M2.5.5:
      thread_context 是 Graph 拉的会话线程历史（caller 要保证已经 redact
      过或不含 PII），以"--- Conversation history ---"段拼接在 user_content
      末尾。**位置**很重要：
        - email_body 在前 → LLM 先读最新一封（决定 intent）
        - image_descriptions 居中（如果多张图）
        - thread_context 居后 → 提供"之前讨论过的背景"（避免和最新封冲突）
      Triage 模型看到客户是 follow-up 时也能正确分类（比如"上次问的
      charging 已经搞定，问的是新问题：app 登录失败"→ intent=app_account）。
    """
    if not use_llm:
        from .region_catalog import apply_to_triage
        blob = email_body or ""
        if image_descriptions:
            blob = blob + "\n" + "\n".join(image_descriptions)
        return apply_to_triage(_keyword_classify(blob), blob)

    from .prompts import TRIAGE_SYSTEM

    user_content = _webform_score_text(email_body) or email_body
    if image_descriptions:
        # 把 image 描述拼成一段，紧跟 email_body。
        # 顺序很重要：body 在前 → LLM 先读正文判断，screenshots 辅助验证。
        ctx = "\n\n--- Attached screenshot descriptions (M1.6 vision context) ---\n"
        ctx += "\n".join(image_descriptions)
        ctx += "\n--- end vision context ---"
        user_content = user_content + ctx
    if thread_context:
        # thread_context 居后：先读最新邮件（决定主 intent），
        # 再读线程历史（理解 customer 之前问过什么、是否同一话题 follow-up）。
        ctx_t = "\n\n--- Conversation history (M2.5.5 thread context) ---\n"
        ctx_t += thread_context
        ctx_t += "\n--- end conversation history ---"
        user_content = user_content + ctx_t

    triage, _call = llm.invoke_json(
        schema=TriageResult,
        messages=[
            {"role": "system", "content": TRIAGE_SYSTEM},
            {"role": "user",   "content": user_content},
        ],
        reasoning_effort="low",
        role="triage",
    )
    from .region_catalog import apply_to_triage
    return apply_to_triage(triage, email_body)


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
    red_flags: list = []
    if any(k in text for k in ["lawyer", "attorney", "media", "going public", "social media", "safety", "律师", "媒体"]):
        best = "complaint"
        urgency = "P0"
        if any(k in text for k in ["lawyer", "attorney", "律师"]):
            red_flags.append("litigation_threat")
        if any(k in text for k in ["media", "going public", "social media", "媒体"]):
            red_flags.append("social_media_threat")
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
        red_flags=red_flags,
    )


# ── 模板占位符检测 ──────────────────────────────────────────────────────

# 模板里 {xxx} 是占位符。不要吃 {{salutation}} 这种双花括号（留给 greeting）。
_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})")

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


_BRACKET_SLOT_RE = re.compile(r"【([^】]*)】")

_CLIMATE_SLOT_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"seat\s*heat|座椅加热", re.I), "seat heating"),
    (re.compile(r"mirror|后视镜", re.I), "the mirror-fold function"),
    (re.compile(r"air.?con|climate|A/C|空调|temperature", re.I),
     "the climate / air-conditioning function"),
]


def fill_bracket_slots(
    template: str,
    email_body: str = "",
    image_descriptions: list[str] | None = None,
) -> str:
    """把模板里的【...】换成客户邮件里能落地的英文短语。

    填不上时仍去掉中括号，留下槽位里的英文说明，避免【】漏到客户草稿。
    """
    if not template or "【" not in template:
        return template
    body = email_body or ""
    images = image_descriptions or []

    def _repl(m: re.Match[str]) -> str:
        slot = (m.group(1) or "").strip()
        slot_l = slot.lower()
        if "screenshot" in slot_l or "dashboard" in slot_l or "infotainment" in slot_l:
            if images:
                return "the screenshot you attached"
            return slot
        if "temperature" in slot_l or "climate" in slot_l:
            for pat, phrase in _CLIMATE_SLOT_HINTS:
                if pat.search(body):
                    return phrase
            return slot
        if "online application" in slot_l or "music" in slot_l:
            for kw, phrase in (
                (r"spotify", "Spotify"),
                (r"amazon music", "Amazon Music"),
                (r"weather", "the weather app"),
                (r"music", "the built-in music app"),
            ):
                if re.search(kw, body, re.I):
                    return phrase
            return slot
        if "incorrect data" in slot_l:
            return "the incorrect data shown in the app"
        if "issue you described" in slot_l:
            return "the issue you described"
        if (
            "service history" in slot_l
            or "warranty status" in slot_l
            or "repair progress" in slot_l
            or "maintenance schedule" in slot_l
        ):
            if _is_maintenance_history_ask(body):
                return "maintenance history"
            return slot
        langs = (
            ("italian", "Italian"),
            ("italiano", "Italian"),
            ("german", "German"),
            ("deutsch", "German"),
            ("french", "French"),
            ("français", "French"),
            ("spanish", "Spanish"),
            ("español", "Spanish"),
            ("english", "English"),
        )
        if slot_l in {a for a, _ in langs} or slot_l in {
            "german", "english", "french", "spanish", "italian",
        }:
            for pat, phrase in langs:
                if re.search(rf"\b{pat}\b", body, re.I):
                    return phrase
            return slot or "the selected system language"
        if not slot:
            for pat, phrase in (
                (r"digital\s+key|数字钥匙", "Digital Key"),
                (r"hello\s*mg", "Hello MG"),
                (r"temperature|climate|空调", "remote climate control"),
            ):
                if re.search(pat, body, re.I):
                    return phrase
            return "requested function"
        return slot

    return _BRACKET_SLOT_RE.sub(_repl, template)


def _stated_vehicle_model(body: str) -> Optional[str]:
    m = re.search(
        r"(?im)^[^\n]{0,48}(vehicle\s+model|modello|fahrzeugmodell|"
        r"mod[eè]le de v[eé]hicule|modell)\s*[:：]\s*(.+)$",
        body or "",
    )
    if not m:
        return None
    model = re.sub(r"\s+", " ", (m.group(2) or "").strip()).strip(" .;，。")
    if not model or len(model) > 48:
        return None
    return model


_HARDCODED_MG_MODEL_RE = re.compile(
    r"\bMG(?:\s|-)?(?:"
    r"ZS(?:\s+(?:EV|Trophy|Hybrid\+?))?"
    r"|4(?:\s+(?:Electric|EV(?:\s+URBAN)?))?"
    r"|5(?:\s+Electric)?"
    r"|3(?:\s+Hybrid)?"
    r"|HS(?:\s+PHEV)?"
    r"|Marvel\s*R"
    r")\b",
    re.I,
)


def _fill_stated_vehicle_model(text: str, body: str) -> str:
    """模板写死某款 MG 时，换成客户表单里的车型。"""
    model = _stated_vehicle_model(body)
    if not text or not model:
        return text
    return _HARDCODED_MG_MODEL_RE.sub(model, text)


def fill_placeholders_naive(template: str, ctx: dict, *,
                             strip_unfilled: bool = True) -> str:
    """用 ctx dict 填占位符。

    Args:
        template: 含 {xxx} 占位符的模板正文
        ctx:      占位符 → 替换值
        strip_unfilled: True → 任何 ctx 缺失的占位符**替换为空字符串**（不漏到
                        Outlook Drafts）。False → 保留 {xxx} 字面量（开发
                        调试用）。

    Why strip_unfilled=True (M2.5.5 fix):
        模板里出现过 {customer_email} / {phone} / {account} / {language} 等
        占位符，填不上时会原样落到 Drafts —— 用户报告 v6 起草的草稿里有
        "{customer_email}" 字面量。生产路径必须 strip，不能让 placeholder
        漏到客户可能看到的草稿。
    """
    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key == "salutation":
            return m.group(0)
        if key in ctx:
            return ctx[key]
        return "" if strip_unfilled else m.group(0)
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
            r"previous\s*user",
            r"connected to the previous",
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


# ── M2.5.5: enriched 关键词打分 ──────────────────────────────────────────
# 打分规则（pick_template 主用这套；sub_scenario 命中时仍优先 sub_scenario 路径）：
#   - xlsx 列 5 人工关键词 (keywords_existing):  每个 3.0（人工事先选，权重高）
#   - LLM 扩展关键词 (keywords_extracted):       每个 2.0
#   - en_scenario 描述高频 token:                每个 0.5（兜底信号）
_KEYWORD_STOPWORDS = frozenset({
    "hi", "hello", "hey", "dear", "thanks", "thank you", "please",
    "help", "issue", "problem", "how to", "how can i", "how can",
})
#
# Why three tiers:
#   - existing 是人工事先从客户邮件里挑过的「金标准」短语（"VIN missing" /
#     "chassis number"），命中即强信号
#   - extracted 是 LLM 从 en_scenario 描述里扩出来的同义变体（"no vin" /
#     "cannot find vin" / "where to find vin"），覆盖人工没列到的口语化表达
#   - scenario_en_tokens 兜底，body 含 scenario 描述里的关键名词时也算

_KEYWORD_MATCH_RE = re.compile(r"\b")


def _kw_match(kw: str, body_lower: str) -> bool:
    """关键词在邮件 body 里命中？

    规则：
      - 多 token / 长度 ≥4 → 子串匹配（"VIN missing" 直接 in body）
      - 单词 / 长度 <4 → 词边界匹配（避免 "i" 误中 "in"/"if"/"is"）
      - 空字符串 → False
      - 连字符与空格等价（off-peak == off peak）
    """
    if not kw:
        return False
    kw_lower = re.sub(r"[-_/]+", " ", kw.lower().strip())
    if not kw_lower:
        return False
    body_lower = re.sub(r"[-_/]+", " ", body_lower.lower())
    if " " in kw_lower or len(kw_lower) >= 4:
        return kw_lower in body_lower
    return bool(re.search(rf"\b{re.escape(kw_lower)}\b", body_lower))


def _enriched_keyword_score(template: dict, body_text: str) -> tuple[float, dict]:
    """返回 (score, debug_breakdown)。

    debug_breakdown 写进 record 方便 audit：
      {"existing_hits": [...], "extracted_hits": [...], "scenario_tokens_hit": [...]}
    """
    if not body_text:
        return 0.0, {"existing_hits": [], "extracted_hits": [], "scenario_tokens_hit": []}
    body_lower = _webform_score_text(body_text).lower()
    existing_hits: list[str] = []
    extracted_hits: list[str] = []
    scenario_tokens_hit: list[str] = []
    score = 0.0

    for kw in template.get("keywords_existing", []):
        kw_low = kw.strip().lower()
        if kw_low in _KEYWORD_STOPWORDS:
            continue
        if kw_low and _kw_match(kw_low, body_lower):
            existing_hits.append(kw_low)
            score += 3.0
    for kw in template.get("keywords_extracted", []):
        kw_low = kw.strip().lower()
        if kw_low in _KEYWORD_STOPWORDS:
            continue
        if kw_low and _kw_match(kw_low, body_lower):
            extracted_hits.append(kw_low)
            score += 2.0
    for tok in template.get("scenario_en_tokens", []):
        tok_low = tok.strip().lower()
        if tok_low and _kw_match(tok_low, body_lower):
            scenario_tokens_hit.append(tok_low)
            score += 0.5
    return score, {
        "existing_hits":      existing_hits,
        "extracted_hits":     extracted_hits,
        "scenario_tokens_hit": scenario_tokens_hit,
    }


# ── 主入口 ─────────────────────────────────────────────────────────────

_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
_RADAR_ADAS_RE = re.compile(
    r"\b(radar|radarn|radars?|lidar|adas|aeb|lka|"
    r"lane\s+keep|lane\s+assist|front\s+collision|"
    r"adaptive\s+cruise|blind\s+spot)\b"
    r"|雷达|车道保持|自适应巡航|主动刹车",
    re.I,
)
# 软件升级范围问句（会不会也改 LKA）≠ ADAS 故障进店检查。
_INSPECTION_TEMPLATE_IDS = frozenset({
    "ADAS-ACC-001", "ADAS-AEB-001", "ADAS-LKA-001", "ADAS-TJA-001", "NON-003",
})
_UPDATE_SIGNAL_RE = re.compile(
    r"(software\s+(update|upgrade|version)"
    r"|infotainment\s+update"
    r"|\bfota\b|\bota\b|over[\s-]?the[\s-]?air"
    r"|软件(升级|更新)|系统升级"
    r"|mise\s+à\s+jour|aggiornamento)",
    re.I,
)
_UPDATE_SCOPE_Q_RE = re.compile(
    r"((will|would|does|can|could|wird).{0,90}(also|auch|improve|include|cover)"
    r"|(also|auch)\s+(improve|include|cover)"
    r"|(update|ota|fota|upgrade).{0,40}\b(include|cover|improve)"
    r"|\b(include|cover)\b.{0,40}(update|ota|fota|upgrade)"
    r"|current or next"
    r"|upcoming.{0,40}(update|software|ota)"
    r"|会不会|也会.{0,20}(升级|更新|包含))",
    re.I | re.S,
)
_VPN_RE = re.compile(r"\bvpn\b", re.I)
_VPN_RESTRICT_RE = re.compile(
    r"(restrict|restriction|block(?:ed|ing)?|remove|disable|not\s+allow|"
    r"cannot\s+use|can't\s+use|forbidden|gesperrt|einschränk|"
    r"lift.{0,24}vpn|vpn.{0,24}(off|away|restriction))",
    re.I,
)


def _is_vpn_restriction(text: str) -> bool:
    """要解除车机 VPN 限制 ≠ NET-001（热点 / 月初流量）。无专属行 → 转人工。"""
    blob = text or ""
    if not _VPN_RE.search(blob):
        return False
    return bool(_VPN_RESTRICT_RE.search(blob))


# 热词锁错桶：点名功能（Android Auto / LKA / VPN）≠ 主诉。
# 处理套路固定四步：检测器 → 强制正确行或 return None → 候选菜单禁错 id
# → TEMPLATE_PICK_SYSTEM 一条规则 + 正反例测试。
_CPAA_HOWTO_IDS = frozenset({"CPAA-001"})
_HEADUNIT_CORE_RE = re.compile(
    r"("
    r"(central|centre|center|infotainment|head[\s-]?unit)"
    r".{0,48}(screen|display|monitor)?"
    r".{0,32}(turns?\s+off|switching?\s+off|shuts?\s+(off|down)|"
    r"powers?\s+off|goes?\s+(black|blank|dark|off)|black\s+screen|"
    r"blank\s+screen|white\s+screen|reboots?|restart)"
    r"|"
    r"(screen|display|monitor).{0,24}(turns?\s+off\s+completely|"
    r"goes?\s+(completely\s+)?(black|blank|dark)|black\s+screen|"
    r"blank\s+screen|white\s+screen|keeps?\s+rebooting)"
    r"|"
    r"(中控|车机).{0,12}(黑屏|白屏|熄灭|重启|黑|灭)"
    r"|"
    r"(黑屏|白屏|反复重启)"
    r"|"
    r"(ecr[aã]o?\s+central|pantalla\s+central|bildschirm).{0,32}"
    r"(desliga|apaga|geht\s+aus|schwarz)"
    r")",
    re.I | re.S,
)
_GENERIC_SCREEN_OFF_RE = re.compile(
    r"\b(the\s+)?(screen|display)\b.{0,120}"
    r"(turns?\s+off|goes?\s+(black|blank|dark|off)|black\s+screen|reboots?)",
    re.I | re.S,
)
_CPAA_NEAR_RE = re.compile(
    r"(android\s+auto|carplay|apple\s+car\s*play)",
    re.I,
)


def _is_headunit_display_fault(text: str) -> bool:
    """中控整屏熄灭/黑屏/重启 ≠ CPAA 断连 how-to。AA/CarPlay 作为连带症状不算配对问题。"""
    blob = text or ""
    if not blob.strip():
        return False
    if _HEADUNIT_CORE_RE.search(blob):
        return True
    m = _GENERIC_SCREEN_OFF_RE.search(blob)
    if not m:
        return False
    window = blob[max(0, m.start() - 48):m.end()]
    if _CPAA_NEAR_RE.search(window):
        return False
    return True


def _pick_headunit_display_template(templates: list[dict]) -> Optional[dict]:
    return _template_by_id(templates, "FICM-003")


_SUB_ACCOUNT_RE = re.compile(
    r"(sub[\s-]?account|secondary\s+account|second(ary)?\s+user|"
    r"additional\s+user|副账号|二级账号|compte\s+secondaire|sous[\s-]?compte|"
    r"contatto secondario|account secondario|utente secondario|"
    r"sekund[äa]rkonto|zweitkonto|zweiten?\s+benutzer|"
    r"invitation.{0,48}(wife|husband|partner|car|vehicle|app)|"
    r"(wife|husband|partner).{0,48}(invitation|link|bind|app|account)|"
    r"(create|add|set\s*up|authorize|authorise).{0,48}"
    r"(spouse|wife|husband|partner|family|conjoint|user|account))",
    re.I | re.S,
)
_MULTI_PRIMARY_RE = re.compile(
    r"(don'?t want.{0,40}secondary|two\s+main\s+accounts|"
    r"multiple\s+(main|primary)\s+accounts|不想用副账号)",
    re.I | re.S,
)
_SUB_ACCOUNT_NOT_REG_RE = re.compile(
    r"(not\s+registered|isn'?t\s+registered|has\s+not\s+been\s+registered|"
    r"non[^\n]{0,40}registrat|nicht\s+registriert|"
    r"no\s+est[aá]\s+registrad|mi dice che non)",
    re.I,
)


def _is_secondary_cs_unlink(text: str) -> bool:
    """请客服删/解绑副账号 → ACC-009，不是副账号 how-to。"""
    blob = text or ""
    if _is_secondary_not_registered(blob):
        return False
    if not (
        _SUB_ACCOUNT_RE.search(blob)
        or re.search(r"secondary accounts?\s+linked", blob, re.I)
    ):
        return False
    return bool(
        re.search(
            r"("
            r"unlink.{0,48}secondary|remove.{0,48}secondary|"
            r"delete.{0,48}secondary|secondary.{0,48}(unlink|remove|delete)|"
            r"帮忙.{0,16}副账号|删除副账号|解绑副账号"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_secondary_account_howto(text: str) -> bool:
    """要创建/授权副账号 ≠ GEN-005。不含「报未注册」（那走 APP-GUIDE-003）。"""
    blob = text or ""
    if _is_change_registered_email(blob):
        return False
    if not _SUB_ACCOUNT_RE.search(blob):
        return False
    if _MULTI_PRIMARY_RE.search(blob):
        return False
    if _is_secondary_not_registered(blob):
        return False
    if _is_secondary_cs_unlink(blob):
        return False
    return True


def _is_secondary_not_registered(text: str) -> bool:
    """授权时报对方未注册 → 审稿第三列，不要 8 步教程。"""
    blob = text or ""
    if not _SUB_ACCOUNT_RE.search(blob):
        return False
    if _MULTI_PRIMARY_RE.search(blob):
        return False
    return bool(_SUB_ACCOUNT_NOT_REG_RE.search(blob))


def _pick_secondary_account_template(
    templates: list[dict], text: str = "",
) -> Optional[dict]:
    if _is_secondary_not_registered(text or ""):
        return (
            _template_by_id(templates, "APP-GUIDE-003")
            or _template_by_id(templates, "APP-GUIDE-002")
        )
    return _template_by_id(templates, "APP-GUIDE-002")


# 来自《AI回复修改.xlsx》审稿：how-to / 咨询被套成进店检修或只问 VIN。
# 仍走四步：检测器 → 强制行 → 禁错 id → TEMPLATE_PICK_SYSTEM。
_CHG_FAULT_RE = re.compile(
    r"(will\s+not\s+charge|won'?t\s+charge|cannot\s+charge|can'?t\s+charge|"
    r"charging\s+(stopped|interrupted|fail)|charger\s+port|v2l\s+stop)",
    re.I,
)
_SCHED_RE = re.compile(
    r"(off[\s-]?peak|charging\s+schedul|scheduled\s+charging|"
    r"预约充电|充电时段|定时充电|"
    r"(outlet|socket|electrical).{0,40}(clock|timer)|"
    r"(clock|timer).{0,40}charg|"
    r"program.{0,40}charg|"
    r"(charg\w*.{0,80}offset|offset.{0,80}charg))",
    re.I | re.S,
)
_CHG_POWER_SET_RE = re.compile(
    r"((where|how|set|adjust|limit|find).{0,48}charging\s+(power|current)"
    r"|charging\s+power\s+level|maximum\s+current|max(?:imum)?\s+current)",
    re.I | re.S,
)
_PREV_OWNER_RE = re.compile(
    r"(previous\s+(owner|user)|former\s+owner|old\s+owner|"
    r"already\s+bound|"
    r"connected to the previous|前车主|已绑定)",
    re.I,
)
_CANCEL_UNBIND_RE = re.compile(
    r"(cancel.{0,24}unbind|unbind.{0,40}still\s+(own|have)|"
    r"still\s+(own|have).{0,40}unbind|still\s+own\s+vehicle)",
    re.I | re.S,
)
_NEW_PHONE_RE = re.compile(
    r"(changed?\s+my\s+phone|new\s+phone|add\s+a\s+new\s+phone|"
    r"switch(?:ed)?\s+phone|更换手机)",
    re.I,
)
_DK_NOTICE_RE = re.compile(
    r"(stay\s+active|keep.{0,28}digital\s+key\s+active|"
    r"digital\s+key\s+service\s+active|digital\s+key\s+is\s+running|"
    r"turn\s+off\s+the\s+message)",
    re.I | re.S,
)
_MANUAL_PDF_RE = re.compile(
    r"(pdf.{0,24}manual|manual.{0,24}pdf|download.{0,28}manual|"
    r"send.{0,28}(me\s+)?(a\s+|the\s+)?(bound\s+copy|manual)|"
    r"bound\s+copy)",
    re.I | re.S,
)
_MANUAL_FIND_RE = re.compile(
    r"((cannot|can'?t|unable to)\s+(find|download).{0,40}manual(?!\s+mode)|"
    r"manual.{0,80}not\s+listed|"
    r"(need|want|send).{0,48}(user|owner.?s?|fahrzeug|instruction)?\s*manual|"
    r"instruction\s+manual|"
    r"my\s+manual|用户手册)",
    re.I | re.S,
)
_PAINT_RE = re.compile(
    r"(paint.{0,48}(warranty|torn|peel|bumper)|bumper.{0,24}paint|"
    r"k[äa]rcher)",
    re.I | re.S,
)
_POWERTRAIN_RE = re.compile(
    r"(combustion\s+engine\s+stopp|engine\s+stopp|"
    r"tow\s+truck|公路.{0,12}发动机)",
    re.I,
)
_SERVICE_CONSULT_RE = re.compile(
    r"(when.{0,28}(should\s+i\s+)?(service|serviced)|"
    r"due\s+for\s+service|"
    r"maintenance\s+schedule|warranty\s+(transfer|transferred)|"
    r"ensure\s+the\s+warranty|book\s+in\s+for\s+service|"
    r"maintenance\s+alert|service\s+reminder|"
    r"(km|days)\s+remaining|"
    r"remaining.{0,40}(service|maintenance).{0,24}(km|days|mileage)|"
    r"second[\s-]?hand.{0,80}(warranty|service\s+plan)|"
    r"outstanding\s+service\s+plan|"
    r"cosmetic\s+maintenance|"
    r"maintenance\s+plan|"
    r"who.{0,48}(do\s+i\s+|should\s+i\s+|to\s+)?contact.{0,80}"
    r"(plan|maintenance|warranty|claim|dealer)|"
    r"(took\s+out|purchased|bought|paid\s+for).{0,80}"
    r"(maintenance|service|cosmetic)\s+plan|"
    r"make\s+use\s+of\s+(this|the|my)\s+(plan|warranty|cover)|"
    r"which\s+dealer.{0,40}(service|maintenance|warranty)|"
    r"register.{0,48}warranty|"
    r"service\s+history|"
    r"maintenance\s+history|"
    r"find.{0,32}(service|maintenance)\s+(history|record)|"
    r"book\s+a\s+(service|repair|appointment)|"
    r"make\s+an\s+appointment|"
    r"schedule\s+a\s+service|"
    r"repair\s+progress|"
    r"service\s+booking)",
    re.I | re.S,
)
_MAINTENANCE_HISTORY_RE = re.compile(
    r"("
    r"maintenance\s+history|service\s+history|service\s+records?|"
    r"historique\s+(d[e']?\s*)?(entretien|maintenance)|"
    r"historial\s+de\s+(mantenimiento|servicio)|"
    r"cronologia\s+(dei\s+)?tagliandi|storico\s+(manutenzione|tagliandi)|"
    r"Wartungshistorie|Servicehistorie|Wartungsnachweis|"
    r"保养(历史|记录)|维修(历史|记录)"
    r")",
    re.I,
)
_ENGINE_EMISSION_WARN_RE = re.compile(
    r"("
    r"engine\s+emissions?\s+(fault\s+)?warning|"
    r"emissions?\s+(system\s+)?(fault|warning|light|lamp)|"
    r"emission\s+fault|"
    r"故障灯.{0,24}排放|排放.{0,24}(故障|警告|灯)|"
    r"Abgas.{0,24}(Warn|Störung|Fehler)|"
    r"testemunho.{0,24}emiss|aviso.{0,40}emiss"
    r")",
    re.I | re.S,
)
_APP_NOT_RESPONDING_RE = re.compile(
    r"("
    r"(the\s+)?(application|app|ismart(\s+app)?)\s+"
    r"(is\s+)?not\s+responding|"
    r"problems?\s+with\s+the\s+application\s+not\s+responding|"
    r"app(lication)?.{0,24}(no\s+response|unresponsive|frozen|\bhangs?\b)|"
    r"应用(无响应|卡住|卡死)|app.{0,12}无响应"
    r")",
    re.I | re.S,
)
_PREV_OWNER_MAP_LOCK_RE = re.compile(
    r"("
    r"(gps|map|navigation|nav).{0,64}"
    r"(previous\s+owner|former\s+owner|前车主).{0,40}"
    r"(lock|locked|delete|release|unbind|bound)|"
    r"(previous\s+owner|former\s+owner|前车主).{0,64}"
    r"(gps|map|navigation|nav).{0,40}"
    r"(lock|locked|update|delete|release)|"
    r"(can'?t|cannot|unable\s+to)\s+update\s+the\s+(gps|map|navigation)"
    r".{0,80}(previous\s+owner|former\s+owner|前车主)|"
    r"(previous\s+owner|former\s+owner).{0,40}(has\s+)?locked"
    r")",
    re.I | re.S,
)
_SEAT_MEMORY_FAULT_RE = re.compile(
    r"(memory\s+seat|seat.{0,48}remember|driver'?s?\s+memory)",
    re.I | re.S,
)
_PARTS_BUY_RE = re.compile(
    r"(where.{0,48}(can\s+i\s+)?buy.{0,48}(mud|part|spare|guard|bumper)|"
    r"buy\s+a\s+front\s+mud|mudguard|spare\s+parts?|"
    r"挡泥板|买.{0,12}零件)",
    re.I | re.S,
)
_BODYIMG_RE = re.compile(
    r"(vehicle\s+colou?r|car\s+colou?r|wrong\s+colou?r|"
    r"app.{0,40}(show|shows|showing).{0,40}colou?r|"
    r"flare\s+red|diamond\s+red|body\s+image|vehicle\s+picture|"
    r"车体图|车辆颜色)",
    re.I | re.S,
)
_ENERGY_STATS_RE = re.compile(
    r"(energy\s+consumption|平均能耗|能耗统计|"
    r"overall\s+average.{0,24}consumption|"
    r"consumption\s+since\s+the\s+first)",
    re.I | re.S,
)
_NOT_RESPOND_RE = re.compile(
    r"(vehicle\s+did\s+not\s+respond|vehicle\s+not\s+responded|"
    r"the\s+vehicle\s+did\s+not\s+respond|车辆未响应)",
    re.I,
)
_BIND_PAIR_RE = re.compile(
    r"((can'?t|cannot|can\s+not)\s+pair.{0,40}(phone|car|vehicle)|"
    r"pair\s+my\s+phone\s+with\s+(my\s+)?(car|vehicle)|"
    r"how\s+to\s+bind|bind\s+(the\s+|my\s+)?(car|vehicle)|"
    r"connect\s+the\s+car\s+to\s+the\s+(mg\s+)?ismart\s+app|"
    r"connect\s+the\s+car\s+to\s+the\s+app)",
    re.I | re.S,
)
_APP_CAR_CONNECT_FAULT_RE = re.compile(
    r"("
    r"(doesn'?t|does\s+not|cannot|can'?t|won'?t|will\s+not)\s+"
    r"connect\s+to\s+(the\s+)?((mg\s+)?ismart\s+)?app"
    r"|no\s+connection\s+between.{0,48}app"
    r"|(car|vehicle).{0,32}(not|isn'?t)\s+connect.{0,24}app"
    r"|app.{0,32}(not|isn'?t|doesn'?t)\s+connect.{0,32}(car|vehicle)"
    r"|verbindet?\s+nicht.{0,48}app"
    r"|keine\s+verbindung.{0,48}app"
    r"|app.{0,40}(verbindet\s+nicht|keine\s+verbindung)"
    r"|ne\s+se\s+connecte\s+pas"
    r"|n['’]arrive\s+pas\s+[àa]\s+(me\s+)?connecter"
    r"|pas\s+de\s+connexion.{0,40}(app|ismart|v[ée]hicule)"
    r"|non\s+(si\s+)?connette"
    r"|non\s+riesco\s+a\s+connetter"
    r"|no\s+se\s+conecta"
    r"|no\s+conecta.{0,32}(app|ismart)"
    r"|n[aã]o\s+conecta"
    r"|anslut(er)?\s+inte"
    r"|verbindt\s+niet"
    r"|ei\s+yhdist"
    r"|ba[gğ]lanm[iı]yor"
    r"|не\s+(подключ|з['’]?єднан)"
    r"|(cannot|can'?t|unable\s+to)\s+(see|find|locate)\s+"
    r"(the\s+)?(car|vehicle).{0,40}app"
    r"|not\s+visible.{0,32}app"
    r"|offline.{0,24}(in\s+the\s+)?app"
    r"|the\s+network\s+is\s+not\s+available"
    r"|unable\s+to\s+use.{0,24}i[\s-]?smart.{0,80}network"
    r"|(no\s+longer\s+|cannot\s+|can'?t\s+|unable\s+to\s+)"
    r"(access|control|reach).{0,24}(the\s+)?(car|vehicle).{0,24}remot"
    r"|access\s+the\s+car\s+remotely"
    r"|(already\s+bound|already\s+binded|vehicle\s+is\s+bound|"
    r"car\s+is\s+bound|tells?\s+me.{0,40}bound)"
    r".{0,160}(remot|will\s+not\s+connect|won'?t\s+connect|"
    r"cannot\s+connect|can'?t\s+connect|no\s+longer)"
    r")",
    re.I | re.S,
)
_VEHICLE_MGMT_HOWTO_RE = re.compile(
    r"("
    r"where.{0,32}vehicle\s+management"
    r"|vehicle\s+management\s+(page|section|icon|menu)"
    r"|multiple\s+(cars|vehicles)"
    r"|delete\s+(a\s+)?vehicle"
    r"|remove\s+(the\s+)?(old\s+)?(car|vehicle)"
    r"|remove it from the app"
    r"|(sell|sold).{0,48}how to remove"
    r"|another vehicle is not registered"
    r"|changed my car from"
    r"|update my app with my new car"
    r"|two\s+vehicles"
    r")",
    re.I,
)
_SELF_UNBIND_REBIND_RE = re.compile(
    r"("
    r"desvincular"
    r"|volver\s+a\s+vincular"
    r"|no\s+puedo\s+desvincul"
    r"|(cannot|can't|unable\s+to)\s+(unbind|unlink)"
    r"|unbind.{0,60}(again|re-?\s*bind)"
    r"|unlink.{0,48}(bind|vincular)"
    r"|解绑.{0,40}(重新绑定|再绑定|重绑|再绑)"
    r")",
    re.I | re.S,
)
_FORGOT_PASSWORD_RE = re.compile(
    r"(forgot\s+(the\s+)?(lonin|login|password)|"
    r"forgot\s+lonin|"
    r"recover\s+my\s+password|"
    r"(cannot|can'?t|don'?t|do\s+not)\s+remember\s+my\s+log\s?-?\s?in|"
    r"reset\s+(my\s+)?password|忘记密码|"
    r"mot\s+de\s+passe.{0,80}(oubli|souviens\s+plus|r[eé]cup[eé]r|modifier)|"
    r"(oubli[eé]|souviens\s+plus|r[eé]cup[eé]rer).{0,80}mot\s+de\s+passe|"
    r"passwort\s+vergessen|"
    r"contrase[nñ]a.{0,40}olvid|"
    r"(password|passworda)\s+dimenticat|"
    r"non\s+ricordo.{0,48}(password|passworda))",
    re.I,
)
_PRIVACY_LOOP_RE = re.compile(
    r"("
    r"privacy\s+needs\s+to\s+be\s+agreed|"
    r"repeated\s+privacy|"
    r"privacy\s+notice\s+(denied|refused|declined|rejected)|"
    r"(privacy\s+notice|informativa.{0,12}privacy).{0,48}"
    r"(pop|again|repeat|won'?t|doesn'?t|keep|agreed|disappear|denied|refus)|"
    r"privacy.{0,40}(again|many\s+times|doesn'?t\s+disappear|denied|refused)|"
    r"avis de confidentialit[eé].{0,48}refus|"
    r"confidentialit[eé]\s+refus|"
    r"(cannot|can'?t|unable|nothing happens).{0,48}"
    r"(accept|agree).{0,40}privacy|"
    r"隐私协议.{0,24}(弹|反复|同意|拒绝)"
    r")",
    re.I | re.S,
)
_WEBFORM_FOOTER_START_RE = re.compile(
    r"(I can read the latest|Je peux lire|Posso leggere|Puedo leer|"
    r"Enviando esta|By sending this|"
    r"Ich kann die neueste|Ik kan de meest recente|"
    r"posso ler o mais recente|puedo leer el aviso)",
    re.I,
)
_PRIVACY_BOILERPLATE_RE = re.compile(
    r"(i can read the latest privacy notice"
    r"|puedo leer el aviso de privacidad"
    r"|posso ler o mais recente aviso"
    r"|veri işleme faaliyetleri"
    r"|ich kann (die|den) neueste"
    r"|posso leggere l['’]informativa"
    r"|ik kan de meest recente"
    r").{0,1600}?(protection rights|derechos de protecci[oó]n"
    r"|prote[cç][aã]o de dados|koruma haklar"
    r"|datenschutz|diritti di protezione).{0,120}",
    re.I | re.S,
)


def _strip_webform_privacy_footer(text: str) -> str:
    """网页表单固定 Privacy Notice 页脚。打分/锁桶都先剥掉，否则每封都像隐私故障。"""
    blob = text or ""
    if not blob.strip():
        return ""
    blob = _WEBFORM_FOOTER_START_RE.split(blob, maxsplit=1)[0]
    blob = _PRIVACY_BOILERPLATE_RE.sub(" ", blob)
    blob = re.sub(r"https?://\S+", " ", blob)
    return blob.strip()


def _strip_webform_dropdown_lines(text: str) -> str:
    """Issue Type / Problem Type 是表单下拉，不是主诉（Vehicle Management ≠ BIND-006）。"""
    return _WEBFORM_DROPDOWN_LINE_RE.sub(" ", text or "")


def _webform_score_text(text: str) -> str:
    return _strip_webform_dropdown_lines(_strip_webform_privacy_footer(text or ""))


_ISMART_PLAN_3G_RE = re.compile(
    r"(reactivate.{0,24}(ismart\s+)?plan|"
    r"ismart\s+plan|"
    r"screen\s+remains\s+in\s+3g|"
    r"\b3g\b.{0,40}(connect|plan|signal)|"
    r"how\s+do\s+i\s+reactivate)",
    re.I | re.S,
)
_NAV_VOICE_LANG_RE = re.compile(
    r"(navigat\w*.{0,48}(italian|italiano)|"
    r"italian.{0,48}(navigat|voice\s+guidance)|"
    r"navigator\s+only\s+in\s+english|"
    r"voice\s+commands?\s+in\s+english.{0,80}italian|"
    r"(dutch|turkish|nederlands).{0,40}(navigat|voice\s+guidance))",
    re.I | re.S,
)
_AA_INFOTAINMENT_FAULT_RE = re.compile(
    r"("
        r"(android\s+auto|carplay).{0,240}"
        r"(slowdown|slowdowns|restart|restarts|crash|crashed|froze|frozen)"
        r"|"
        r"(steering\s+wheel).{0,80}(slowdown|slowdowns|lag|stutter)"
    r"|"
    r"(navigator|navigation).{0,40}(crash|crashed|froze)"
    r"|"
    r"(on-board computer|cluster).{0,40}restart"
    r")",
    re.I | re.S,
)
_MAP_UPDATE_FAIL_RE = re.compile(
    r"("
    r"map\s+update.{0,80}(fail|not\s+complet|unsuccess|error|nicht)"
    r"|"
    r"(fail|error|unsuccess).{0,80}map\s+update"
    r"|"
    r"update.{0,32}the\s+map.{0,48}(fail|error|not\s+complet)"
    r"|"
    r"update\s+failed.{0,80}map"
    r"|"
    r"naviextras"
    r"|"
    r"content\s+folder\s+not\s+exist"
    r"|"
    r"Karten(update|aktualisierung).{0,48}(fehl|nicht)"
    r")",
    re.I | re.S,
)
_MAP_QUALITY_FB_RE = re.compile(
    r"("
    r"gps.{0,48}(shortcoming|poor|bad|unworthy|serious)"
    r"|"
    r"(mapping|map data|map).{0,48}(shortcoming|update soon|charg)"
    r"|"
    r"search for streets"
    r"|"
    r"charging stations.{0,80}(update|implement|mapping)"
    r"|"
    r"envisage an update.{0,80}(map|navigat|charg|street)"
    r")",
    re.I | re.S,
)
_BT_CALL_RE = re.compile(
    r"(microphone|hands[\s-]?free|call\s+quality|cannot\s+hear)",
    re.I,
)
_CHG_ALERT_RE = re.compile(
    r"((alarm|alert|notif\w*).{0,40}charg|charg\w*.{0,40}(alarm|alert|notif\w*)|"
    r"charging\s+completed|mins?\s+left.{0,20}charg)",
    re.I | re.S,
)
_GPS_BG_RE = re.compile(
    r"(gps.{0,48}(remain|stays?|keep|still|closed)|"
    r"gps.{0,32}(battery|drain|耗电))",
    re.I | re.S,
)
_CRUISE_HOWTO_RE = re.compile(
    r"(activate.{0,40}(cruise|cruising\s+speed)|"
    r"how\s+to.{0,40}(cruise|cruising)|"
    r"can'?t\s+find.{0,40}cruise|"
    r"only\s+the\s+limiter)",
    re.I | re.S,
)
_TOW_ACC_RE = re.compile(
    r"(tow\s*bar|hitch|bicycle\s+carrier|bike\s+rack|拖车钩)",
    re.I,
)
_FOTA_CONNECT_RE = re.compile(
    r"(can'?t\s+connect\s+to\s+the\s+service|"
    r"cannot\s+connect\s+to\s+the\s+service|"
    r"when.{0,48}(is\s+)?(my\s+)?(ota|fota|upgrade|update)\b|"
    r"upgrade\s+when)",
    re.I | re.S,
)
_INFOTAIN_UPDATE_RE = re.compile(
    r"(aggiornamento|update|upgrade|mise\s+à\s+jour|软件(升级|更新)).{0,120}"
    r"(car\s*play|android\s*auto|info[\s\-]?t[aeiym]+n|infotainment|"
    r"head[\s-]?unit|sistema info)"
    r"|"
    r"(car\s*play|android\s*auto|info[\s\-]?t[aeiym]+n|infotainment|"
    r"head[\s-]?unit|sistema info).{0,120}"
    r"(aggiornamento|update|upgrade|mise\s+à\s+jour|软件(升级|更新))",
    re.I | re.S,
)
_CPAA_PAIR_FAULT_RE = re.compile(
    r"(disconnect|not\s+connect|cannot\s+connect|can'?t\s+connect|"
    r"pairing\s+fail|unpair|delete.{0,40}pairing|"
    r"turns?\s+off after|drops? after)",
    re.I | re.S,
)
_INSPECTION_LIKE_IDS = frozenset({
    "CHG-001", "ADAS-ACC-001", "ADAS-AEB-001", "ADAS-LKA-001",
    "ADAS-TJA-001", "NON-003", "IPK-001",
})


_TYRE_RE = re.compile(
    r"(tyre\s+pressure|tire\s+pressure|\btpms\b|胎压|"
    r"tyres?\s+are\s+cold|low\s+tyre|low\s+tire)",
    re.I,
)
_ADDRESS_RE = re.compile(
    r"(change\s+my\s+address|update\s+my\s+address|"
    r"please\s+change\s+my\s+address|更改地址|修改地址)",
    re.I,
)
_DK_HOWTO_RE = re.compile(
    r"(what.{0,48}digital\s+key|"
    r"(can'?t|cannot|unable\s+to).{0,16}select.{0,24}digital\s+key|"
    r"don'?t understand.{0,40}digital\s+key|"
    r"how.{0,48}(activat|deactivat|enable).{0,24}digital\s+key|"
    r"digital\s+key.{0,40}(activat|deactivat|enable|entry)|"
    r"(find|where).{0,32}digital\s+key|"
    r"数字钥匙.{0,20}(激活|怎么|如何|入口))",
    re.I | re.S,
)
_SLEEP_WAKE_RE = re.compile(
    r"(parked|after\s+(unlock|start)|unlock(?:ing)?\s+or\s+start|"
    r"low-?power|sleep\s+mode|shortly\s+after\s+lock)",
    re.I,
)
_HU_NO_INTERNET_RE = re.compile(
    r"((car|vehicle|infotainment|head.?unit|screen).{0,48}no\s+internet"
    r"|no\s+internet\s+connection)",
    re.I | re.S,
)
_TELEM_PLAN_ASK_RE = re.compile(
    r"(telematics\s+data\s+plan|data\s+plan\s+is\s+active|"
    r"(confirm|check).{0,80}(data\s+plan|telematics).{0,40}(expir|active))",
    re.I | re.S,
)
_BACKEND_TELEM_RE = re.compile(
    r"(from\s+your\s+side|identify\s+the\s+cause|factory\s+reset\s+done|"
    r"sends?\s+no\s+data)",
    re.I,
)
_INCIDENTAL_BT_KEY_RE = re.compile(r"bluetooth\s+key", re.I)
_DK_AVAIL_RE = re.compile(
    r"(what\s+is.{0,24}digital\s+key|how.{0,24}(activate|turn\s+off).{0,24}digital\s+key|"
    r"why.{0,40}digital\s+key|does\s+my\s+(vehicle|model).{0,40}digital\s+key|"
    r"can\s+i\s+use.{0,24}digital\s+key|digital\s+key.{0,40}not\s+(get|appear|show|support))",
    re.I | re.S,
)


def _is_charging_schedule_howto(text: str) -> bool:
    blob = text or ""
    if not _SCHED_RE.search(blob):
        return False
    # “schedule fails / cannot set / will not save” 不是谷电 how-to。
    if _is_charge_schedule_fail(blob):
        return False
    if _CHG_FAULT_RE.search(blob) and not re.search(
        r"(how\s+(can|do|to)|off[\s-]?peak|timer|预约)",
        blob,
        re.I,
    ):
        return False
    return True


def _is_accessory_battery_fault(text: str) -> bool:
    return bool(re.search(
        r"accessor(y|ies)\s+batter|auxiliary\s+batter|12\s*v\s+batter|"
        r"batterie\s+auxiliaire|batteria\s+ausiliar",
        text or "",
        re.I,
    ))


def _is_non_ismart_hardware_fault(text: str) -> bool:
    """辅助电瓶 / 仪表报警等车身硬件，不是 App 充电管理或 App 告警。"""
    if _is_accessory_battery_fault(text):
        return True
    blob = text or ""
    if re.search(
        r"\b(acc|aeb|lka|adas|lane\s*keep|radar|airbag|abs|tja)\b",
        blob,
        re.I,
    ):
        return False
    if re.search(
        r"\b(ismart|app)\b.{0,40}(warning|alarm|alert)",
        blob,
        re.I,
    ) and not re.search(
        r"dashboard|instrument|cluster|\bipk\b|while driving",
        blob,
        re.I,
    ):
        return False
    return bool(re.search(
        r"("
        r"warning.{0,48}(while driving|on the (dashboard|cluster|instrument))"
        r"|(dashboard|instrument cluster|\bipk\b|cluster display)"
        r".{0,48}(warning|alarm|fault|message)"
        r"|warning message appears while driving"
        r")",
        blob,
        re.I | re.S,
    ))


def _is_charge_schedule_fail(text: str) -> bool:
    """预约/充电设置保存失败，不是谷电时段 how-to。"""
    blob = text or ""
    if _is_accessory_battery_fault(blob):
        return False
    # 不要回调 _is_charge_target_mismatch：它会再进 howto → 死循环。
    if re.search(
        r"80\s*%.{0,40}100\s*%|100\s*%.{0,40}80\s*%|"
        r"target (soc|value|charge)",
        blob,
        re.I | re.S,
    ):
        return False
    if not re.search(
        r"schedul|instant\s+load|charge\s+planner|预约|充电计划|ladezeit",
        blob,
        re.I,
    ):
        return False
    return bool(re.search(
        r"("
        r"fail|cannot\s+set|can'?t\s+set|unable\s+to\s+set|difficult\s+to\s+set|"
        r"will\s+not\s+save|won'?t\s+save|cannot\s+save|can'?t\s+save|"
        r"not being saved|fail(s|ed)?\s+to\s+be\s+saved"
        r")",
        blob,
        re.I,
    ))


def _is_infotainment_language_request(text: str) -> bool:
    """要车机加某语言 / language update，不是问当前软件版本（FICM-001）。"""
    blob = text or ""
    if _is_nav_language_howto(blob) or _is_nav_voice_language(blob):
        return False
    if _is_manual_find_request(blob):
        return False
    return bool(re.search(
        r"("
        r"language\s+update|"
        r"is a language.{0,32}available|"
        r"language (pack|support|update).{0,24}available|"
        r"(estonia|estonian|finnish|hungarian|czech|slovak|croatian|"
        r"romanian|bulgarian|greek|turkish|dutch|polish|portuguese|"
        r"swedish|danish|norwegian|latvian|lithuanian|slovenian)"
        r".{0,40}language|"
        r"language.{0,40}(estonia|estonian|support|please|available)|"
        r"(add|support|available|want|need).{0,32}language|"
        r"(infotainment|head.?unit|ivi|ficm).{0,32}language|"
        r"小语种|语言需求|车机语言"
        r")",
        blob,
        re.I | re.S,
    ))


def _is_charging_power_howto(text: str) -> bool:
    return bool(_CHG_POWER_SET_RE.search(text or ""))


_HOME_WALLBOX_RE = re.compile(
    r"("
    r"wall\s*box|wallbox|heimlad|家充|家用桩|"
    r"home\s+(wall\s*)?charg|"
    r"third[\s-]?party.{0,32}charg|"
    r"drittanbieter.{0,24}(lad|charg)|"
    r"\bampere\b|\bzaptec\b|\beasee\b|\bohme\b|go-e|"
    r"\bkeba\b|\bhypervolt\b|\bmyenergi\b|\bzappi\b|"
    r"第三方.{0,12}(桩|充电|充电桩)"
    r")",
    re.I,
)
_HOME_CHARGER_FAIL_RE = re.compile(
    r"("
    r"will\s+not\s+charge|won'?t\s+charge|cannot\s+charge|can'?t\s+charge|"
    r"not\s+charg(e|ing)|not\s+loading|"
    r"nicht\s+geladen|wird\s+aber\s+nicht|"
    r"connected.{0,24}not\s+charg|"
    r"verbunden.{0,40}nicht|"
    r"charging\s+(stopped|interrupted|fail)|充不上|无法充电"
    r")",
    re.I | re.S,
)


def _is_third_party_home_charger_fault(text: str) -> bool:
    """第三方家充桩/Wallbox 充不上 → CHG-001，不是 App 充电管理。"""
    blob = text or ""
    if _is_charging_schedule_howto(blob) and not _HOME_CHARGER_FAIL_RE.search(blob):
        return False
    if _is_charging_power_howto(blob):
        return False
    if not _HOME_WALLBOX_RE.search(blob):
        return False
    return bool(_HOME_CHARGER_FAIL_RE.search(blob) or _CHG_FAULT_RE.search(blob))


def _is_bound_but_remote_fail(text: str) -> bool:
    """已经绑上了，但远控/连不上 —— 不是前车主解绑。"""
    blob = text or ""
    bound = re.search(
        r"(already\s+bound|already\s+binded|already\s+bind\b|"
        r"vehicle is bound|car is bound|tells?\s+me.{0,48}bound|"
        r"is (already )?linked)",
        blob,
        re.I,
    )
    remote = re.search(
        r"(remot|will not connect|won'?t connect|cannot connect|"
        r"can'?t connect|no longer.{0,32}(access|connect|control)|"
        r"unable to (access|control|use).{0,24}(remote|app|ismart))",
        blob,
        re.I,
    )
    return bool(bound and remote)


def _is_previous_owner_bind(text: str) -> bool:
    blob = text or ""
    if _is_forgot_password(blob) and re.search(
        r"still\s+(me|myself|the\s+customer)|i\s+am\s+still\s+the",
        blob,
        re.I,
    ):
        return False
    if _is_bound_but_remote_fail(blob) and not re.search(
        r"previous\s+(owner|user)|former\s+owner|old\s+owner|前车主",
        blob,
        re.I,
    ):
        return False
    return bool(_PREV_OWNER_RE.search(blob))


# 冒用 / 非授权占用车账号 → 请客服强制解绑（无自动行，转人工）。
# 不是前车主 72h（BIND-001）、不是自己解绑重绑（BIND-007）、不是副账号（ACC-009）。
_FRAUD_ACCOUNT_RE = re.compile(
    r"("
    r"fraud(ulent|ulos[aoe]?)?|"
    r"unauthori[sz]ed|"
    r"without\s+(my\s+)?(authori[sz]ation|consent|permission)|"
    r"hacked|stolen\s+account|"
    r"someone\s+else.{0,48}(logged|log\s*in|connected|using|bound|linked)|"
    r"(unknown|other)\s+(person|user|account).{0,48}"
    r"(logged|connected|bound|linked|using)|"
    r"not\s+(me|myself|the\s+owner).{0,40}(logged|connected|using)|"
    r"illegally\s+(connected|bound|linked|using)|"
    r"冒用|盗用|被人(登录|登陆|占用)|非本人|"
    r"conectat[ăa]\s+fraudulos|fraudulos"
    r")",
    re.I | re.S,
)
_CS_FORCE_UNBIND_ASK_RE = re.compile(
    r"("
    r"(please\s+)?(force\s+)?(un)?(bind|link)\b|"
    r"(request|ask|want|need|dores[sc]).{0,40}"
    r"(un)?(bind|link|disconnect|deconect|desconect|解绑)|"
    r"remove.{0,40}(account|user|access|connection)|"
    r"disconnect.{0,40}(account|user|app)|"
    r"deconect|desconect|desvincul|解绑|解除绑定|"
    r"unlink.{0,40}(account|user|old|vehicle|car)"
    r")",
    re.I | re.S,
)


def _is_fraud_force_unbind(text: str) -> bool:
    """冒用/非授权占用 → 请客服强制解绑。无自动行，转人工。"""
    blob = f"{_webform_score_text(text or '')}\n{_form_issue_description(text or '')}"
    if not blob.strip():
        return False
    if _is_previous_owner_bind(blob):
        return False
    if _is_secondary_cs_unlink(blob) or _is_secondary_account_howto(blob):
        return False
    if not _FRAUD_ACCOUNT_RE.search(blob):
        return False
    return bool(_CS_FORCE_UNBIND_ASK_RE.search(blob))


def fraud_force_unbind_needs_human(text: str) -> bool:
    """冒用/非授权强制解绑：要查后台，不建草稿。禁 GEN-001 / BIND-001。"""
    blob = _latest_customer_turn(text or "") or (text or "")
    return _is_fraud_force_unbind(blob)


def _is_new_phone_or_cancel_unbind(text: str) -> bool:
    blob = text or ""
    return bool(_CANCEL_UNBIND_RE.search(blob) or _NEW_PHONE_RE.search(blob))


def _is_digital_key_status_notice(text: str) -> bool:
    return bool(_DK_NOTICE_RE.search(text or ""))


def _is_manual_pdf_request(text: str) -> bool:
    return bool(_MANUAL_PDF_RE.search(text or ""))


def _is_manual_find_request(text: str) -> bool:
    blob = text or ""
    if _is_manual_pdf_request(blob):
        return False
    if _is_manual_wrong_model(blob):
        return False
    return bool(_MANUAL_FIND_RE.search(blob))


def _is_paint_warranty(text: str) -> bool:
    return bool(_PAINT_RE.search(text or ""))


def _is_powertrain_roadside(text: str) -> bool:
    return bool(_POWERTRAIN_RE.search(text or ""))


def _is_service_consult(text: str) -> bool:
    blob = text or ""
    if _is_paint_warranty(blob) or _is_powertrain_roadside(blob):
        return False
    if _is_colour_code_ask(blob):
        return False
    if _SEAT_MEMORY_FAULT_RE.search(blob):
        return False
    if _is_infotainment_update_consult(blob):
        return False
    return bool(_SERVICE_CONSULT_RE.search(blob))


def _is_maintenance_history_ask(text: str) -> bool:
    """要保养/维修历史记录，不是剩余保养里程/天数或当前服务计划。"""
    blob = f"{_webform_score_text(text or '')}\n{_form_issue_description(text or '')}"
    return bool(_MAINTENANCE_HISTORY_RE.search(blob))


def _is_engine_emission_ipk(text: str) -> bool:
    """仪表/排放系统故障警告 → NON-004（IPK），不是 App 告警话术。"""
    blob = f"{_webform_score_text(text or '')}\n{_form_issue_description(text or '')}"
    if re.search(r"\b(ismart|app)\b.{0,24}(emission|emissions)", blob, re.I):
        return False
    return bool(_ENGINE_EMISSION_WARN_RE.search(blob))


_REMOTE_UI_STUCK_RE = re.compile(
    r"("
    r"(remain|stuck|stays?|staying|always).{0,48}(in\s+)?(update|updating)\s+mode"
    r"|(lock|unlock)\s+icon.{0,80}(running|spinning|loading)"
    r"|(open|unlock)\s+command.{0,24}still\s+running"
    r"|command\s+was\s+still\s+running"
    r"|modalit[aà](\s+di)?\s+aggiornamento"
    r"|lucchetto.{0,80}(esecuzione|sbloccare)"
    r"|comando\s+di\s+apertura.{0,48}esecuzione"
    r"|modo\s+de\s+actualizaci[oó]n"
    r"|mode\s+(de\s+)?mise\s+[aà]\s+jour.{0,40}(bloqu|reste|tourne)"
    r"|aktualisierungsmodus|update[\s-]?modus"
    r")",
    re.I | re.S,
)


def _is_remote_ui_stuck_updating(text: str) -> bool:
    """远控/车况页一直停在 updating，锁图标转圈。不是地图更新、也不是 OTA 咨询。"""
    blob = text or ""
    if _is_map_online_update_failed(blob) or _is_infotainment_update_consult(blob):
        return False
    return bool(_REMOTE_UI_STUCK_RE.search(blob))


def _is_app_not_responding(text: str) -> bool:
    """App/application not responding → STATUS-003（先开阔信号区），不是 GEN-004 要截图。"""
    blob = f"{_webform_score_text(text or '')}\n{_form_issue_description(text or '')}"
    if re.search(
        r"has stopped|force.?clos|keeps? crashing|keeps? stopping",
        blob,
        re.I,
    ):
        return False
    return bool(_APP_NOT_RESPONDING_RE.search(blob))


def _is_prev_owner_blocks_map_update(text: str) -> bool:
    """前车主锁车导致 GPS/地图无法更新：拆成确认解绑帮助 + 网络刷新提示。"""
    blob = f"{_webform_score_text(text or '')}\n{_form_issue_description(text or '')}"
    if fraud_force_unbind_needs_human(blob):
        return False
    return bool(_PREV_OWNER_MAP_LOCK_RE.search(blob))


_EXPLICIT_REGION_UNSUPPORTED_RE = re.compile(
    r"("
    r"(ismart|connected\s+services?).{0,48}"
    r"(not\s+supported|unsupported|unavailable|not\s+available)|"
    r"(not\s+supported|unsupported).{0,32}"
    r"in\s+(my\s+|this\s+|the\s+)?(country|region|market)|"
    r"(country|region|market).{0,24}(not\s+supported|unsupported)|"
    r"services?\s+may\s+not\s+be\s+available|"
    r"区域不支持|地区不支持|不支持(该|此)?(国家|地区|区域)"
    r")",
    re.I | re.S,
)

_BIND_ASK_BEFORE_REGION_RE = re.compile(
    r"("
    r"(can'?t|cannot|unable\s+to)\s+find.{0,32}qr(\s*code)?"
    r"|find\s+the\s+qr(\s*code)?"
    r"|use\s+(the\s+)?vin\s+to\s+(register|bind|pair|activate)"
    r"|register\s+(the\s+)?(car|vehicle|vin)?.{0,24}"
    r"on\s+the\s+(ismart\s+)?app"
    r"|qr(\s*code)?.{0,48}(bind|register|找不到|无法找到)"
    r"|绑(不上|不定|车).{0,24}(二维码|vin|app)"
    r"|二维码.{0,24}(找不到|没有|缺失)"
    # 光杆「绑不上车」：无教程问句 → 先要信息，不是 APP-GUIDE 教程
    r"|(can'?t|cannot|unable\s+to)\s+bind(\s+(the\s+|my\s+|a\s+)?(car|vehicle|app))?"
    r"|bind(\s+(the\s+|my\s+)?(car|vehicle))?\s+(fail|failed|failure|error|issue|problem)"
    r"|无法绑(定|车|上)"
    r"|绑不上(车|app)?"
    r")",
    re.I | re.S,
)

_APP_SAYS_RANGE_DROP_RE = re.compile(
    r"("
    r"(app|ismart).{0,24}says?.{0,80}"
    r"(range|km|miles?).{0,80}"
    r"(drop|drops|dropped|reduc|fall|falls|fell|decreas)"
    r"|(range|km|miles?).{0,48}"
    r"(drop|drops|dropped|reduc|fall|falls|fell|decreas).{0,80}"
    r"(app|ismart).{0,24}says?"
    r"|unplug.{0,64}(range|km|miles?).{0,40}(drop|reduc|fall)"
    r"|(range|km|miles?).{0,40}(drop|reduc|fall).{0,64}unplug"
    r")",
    re.I | re.S,
)

_APP_BLOCKS_CHARGING_RE = re.compile(
    r"("
    r"(app|ismart).{0,48}"
    r"(buggy|bug|glitch|faulty|broken|won'?t\s+let|will\s+not\s+let|"
    r"doesn'?t\s+let|cannot|can'?t|prevent|blocking|阻止).{0,48}"
    r"charg"
    r"|won'?t\s+let\s+me\s+charg"
    r"|will\s+not\s+let\s+me\s+charg"
    r"|app.{0,32}(prevent|block|stop).{0,32}charg"
    r"|(charg\w*).{0,40}(because\s+of|due\s+to|via).{0,24}(the\s+)?(app|ismart)"
    r"|app.{0,24}(导致|无法|不让).{0,24}充"
    r")",
    re.I | re.S,
)

_MANUAL_WRONG_MODEL_RE = re.compile(
    r"("
    r"(manual|handbook).{0,64}"
    r"(wrong|incorrect|showing|shows|display|for\s+a\s+)"
    r".{0,40}(hybrid|model|vehicle|car).{0,48}"
    r"(don'?t\s+have|do\s+not\s+have|not\s+(my|the)\s+(model|car|vehicle)|"
    r"mismatch|wrong|incorrect|amended?)"
    r"|(manual|handbook).{0,48}(mismatch|wrong\s+model|incorrect\s+model)"
    r"|(wrong|incorrect)\s+(user\s+)?manual"
    r"|用户手册.{0,24}(不匹配|不对|错误|车型)"
    r")",
    re.I | re.S,
)


def _is_explicit_region_unsupported(text: str) -> bool:
    """客户明确说本区/本国不支持 iSMART，才走 REG-001（不可自动）。"""
    blob = f"{_webform_score_text(text or '')}\n{_form_issue_description(text or '')}"
    return bool(_EXPLICIT_REGION_UNSUPPORTED_RE.search(blob))


def _is_bind_ask_before_region_confirm(text: str) -> bool:
    """绑不上/找不到二维码/用 VIN 注册：人工确认不支持前，先按绑车要信息。"""
    blob = f"{_webform_score_text(text or '')}\n{_form_issue_description(text or '')}"
    if _is_explicit_region_unsupported(blob):
        return False
    if _is_qr_unreadable(blob):
        return False
    if _is_previous_owner_bind(blob) or _is_prev_owner_blocks_map_update(blob):
        return False
    if fraud_force_unbind_needs_human(blob):
        return False
    if _is_tbox_replaced(blob):
        return False
    return bool(_BIND_ASK_BEFORE_REGION_RE.search(blob))


def _is_app_says_range_drop(text: str) -> bool:
    """拔枪后续航骤降且点名 app says → APP-充电管理要截图，不是 CHG-002。"""
    blob = f"{_webform_score_text(text or '')}\n{_form_issue_description(text or '')}"
    if _is_third_party_home_charger_fault(blob):
        return False
    if _is_charge_target_mismatch(blob):
        return False
    return bool(_APP_SAYS_RANGE_DROP_RE.search(blob))


def _is_app_blocks_charging(text: str) -> bool:
    """主诉 App buggy / 不让充电 → APP-充电管理要截图，不是 CHG-001 进店。"""
    blob = f"{_webform_score_text(text or '')}\n{_form_issue_description(text or '')}"
    if _is_third_party_home_charger_fault(blob):
        return False
    if _is_charging_schedule_howto(blob) or _is_charging_power_howto(blob):
        return False
    return bool(_APP_BLOCKS_CHARGING_RE.search(blob))


def _is_manual_wrong_model(text: str) -> bool:
    """App 手册车型与实车不匹配：保留手册路径 + 要截图。"""
    blob = f"{_webform_score_text(text or '')}\n{_form_issue_description(text or '')}"
    if _is_manual_pdf_request(blob):
        return False
    return bool(_MANUAL_WRONG_MODEL_RE.search(blob))


_MAINT_HISTORY_TEMPLATE = """\
{{salutation}},

Thank you for your email.

Unfortunately, we are unable to access, query, or provide your vehicle's maintenance or service history by email. We kindly recommend that you contact your local MG dealer directly, as they can check the vehicle records using the VIN and provide you with the relevant information.

Best regards,
iSMART Service Team
"""

_NON004_NEUTRAL_WARNING = (
    "The warning you described indicates that your vehicle requires further "
    "inspection. We kindly recommend that you contact and visit your nearest "
    "MG authorized dealer as soon as possible to arrange a vehicle function "
    "check. The dealer will be able to diagnose the cause of the warning and "
    "provide the appropriate assistance."
)

_NON004_EMISSION_WARNING = (
    "The engine emissions system warning indicates that your vehicle requires "
    "further inspection. We kindly recommend that you contact and visit your "
    "nearest MG authorized dealer as soon as possible to arrange a vehicle "
    "function check. The dealer will be able to diagnose the cause of the "
    "warning and provide the appropriate assistance."
)

_PREV_OWNER_MAP_LOCK_TEMPLATE = """\
{{salutation}},

Thank you for your email.

iSMART Service can assist if the vehicle is still bound to the previous owner's account. After verifying that you are the current owner, we can help remove the previous owner's binding.

Could you please confirm whether you need assistance with this process?

Regarding the GPS / map update issue, it may also be related to a network connection problem. Please try refreshing the system and then attempt the update again.

If you would like us to check the binding status, please provide:

1. Country/Region
2. Vehicle brand and model
3. Full 17-character VIN
4. Your iSMART account email address or mobile number
5. Proof of vehicle ownership

Best regards,
iSMART Service Team
"""

_APP_RANGE_DROP_ASK_TEMPLATE = """\
{{salutation}},

Thank you for your email.

To help us investigate the issue further, could you please provide the following information:

1. Photos or videos showing the range before and after unplugging the charger
2. A screen recording of the iSMART App displaying the battery percentage and driving range
3. The date and time when the issue occurred (or several recent occurrences if possible)

This information will assist our technical team in analyzing the situation more accurately.

We look forward to your reply.

Best regards,
iSMART Service Team
"""

_APP_BLOCKS_CHARGING_ASK_TEMPLATE = """\
{{salutation}},

Thank you for your feedback.

To help us investigate your concern that the iSMART app is preventing the vehicle from charging, could you please provide:

1. Screenshots of the error message shown in the iSMART app
2. The exact date and time when the charging issue occurred
3. A brief description of what happened when you attempted to charge the vehicle
4. Any warning messages displayed on the vehicle dashboard or infotainment screen (photos would be appreciated)

This information will help us determine whether the issue is related to the app, the vehicle, or the charging equipment.

We look forward to your reply.

Best regards,
iSMART Service Team
"""

_MANUAL_WRONG_MODEL_TEMPLATE = """\
{{salutation}},

Thank you for your email.

You can first bind your vehicle to the iSMART app. Once the binding is completed, please scroll to the bottom of the app home page and select "My Manual". There, you will be able to view the user manual corresponding to your vehicle online.

You can also find the digital User Manual in the iSMART App by following:

My Profile > User Manual

If the iSMART App is displaying an incorrect user manual for your vehicle, could you please provide screenshots showing the issue within the App? This will help us investigate further.

When sending the screenshots, please include:

1. The page where the incorrect manual is displayed
2. The vehicle model information shown in the App (if visible)
3. Any other relevant screenshots that may help us identify the issue

Once we receive the screenshots, we will review them and assist you further.

Best regards,
iSMART Service Team
"""


_CAR_NOT_FOUND_ASK_TEMPLATE = """\
{{salutation}},

Thank you for your email.

To look into the new vehicle showing "car not found" while the previous vehicle information is still displayed, please send:

1. The email address or mobile number registered on your iSMART account
2. A short video showing the old vehicle information still displayed, the attempt to register the new car, and the "car not found" message
3. The 17-character VIN of the new vehicle

Best regards,
iSMART Service Team
"""

_CHARGE_TARGET_OPTION_TEMPLATE = """\
{{salutation}},

Thank you for your email.

Please open Charging Management in the iSMART app and check whether a CHARGING SCHEDULE option is shown. If that option is not there, this vehicle configuration does not support setting a target charge level in the app. For further questions about the vehicle configuration, please contact your local MG dealer.

Best regards,
iSMART Service Team
"""

_BATTERY_CAPACITY_ASK_TEMPLATE = """\
{{salutation}},

Thank you for your email.

Please send screenshots from the app that show the mileage or range and the battery information for the larger pack, together with official documentation that shows the actual battery capacity of the vehicle.

Best regards,
iSMART Service Team
"""

_BATTERY_CONFIG_MANUAL_TEMPLATE = """\
{{salutation}},

Thank you for your email.

Please check the Owner's Manual for the battery configuration of your vehicle. If it is not listed there, your local MG dealer can confirm the configuration for this vehicle.

Best regards,
iSMART Service Team
"""

_ECALL_DEALER_NOTE = (
    "For the SOS / e-call warning that appears and then clears, "
    "please ask your local MG dealer to check the e-call system."
)


def _warning_clearly_from_app(text: str) -> bool:
    """客户明确说警告来自 App 才保留 NON-004 的 App 归因句。"""
    blob = f"{_webform_score_text(text or '')}\n{_form_issue_description(text or '')}"
    return bool(re.search(
        r"("
        r"\b(ismart|app)\b.{0,40}(warning|alert|alarm|fault)|"
        r"(warning|alert|alarm).{0,40}\b(ismart|app)\b|"
        r"shown\s+in\s+the\s+app|in\s+the\s+app.{0,24}warning"
        r")",
        blob,
        re.I | re.S,
    ))


def _specialize_template_for_ask(
    tmpl: Optional[dict], body_text: str,
) -> Optional[dict]:
    """按主诉改写知识库行正文（分类 id 不变）。"""
    if not tmpl:
        return None
    tid = str(tmpl.get("template_id") or "")
    latest = _latest_customer_turn(body_text)
    out = dict(tmpl)
    if tid == "SVC-001" and _is_maintenance_history_ask(latest or body_text):
        out["template"] = _MAINT_HISTORY_TEMPLATE
        return out
    if tid == "NON-004" and not _warning_clearly_from_app(latest or body_text):
        body = out.get("template") or ""
        repl = (
            _NON004_EMISSION_WARNING
            if _is_engine_emission_ipk(latest or body_text)
            else _NON004_NEUTRAL_WARNING
        )
        new_body, n = re.subn(
            r"The warning shown in the App is generated based on vehicle "
            r"status information received by the connected system\.\s*"
            r"If the warning continues to appear or does not match the "
            r"actual vehicle condition, we recommend arranging a vehicle "
            r"function inspection so the related vehicle status and "
            r"warning information can be checked properly\.",
            repl,
            body,
            count=1,
            flags=re.I | re.S,
        )
        if n:
            out["template"] = new_body
        return out
    if tid == "BIND-001" and _is_prev_owner_blocks_map_update(latest or body_text):
        out["template"] = _PREV_OWNER_MAP_LOCK_TEMPLATE
        out["level"] = "auto_draft_needs_info"
        return out
    if tid == "GEN-001" and _is_bind_ask_before_region_confirm(latest or body_text):
        out["category"] = "App-绑车解绑相关"
        out["level"] = "auto_draft_needs_info"
        return out
    if tid == "STATUS-012" and _is_app_says_range_drop(latest or body_text):
        out["template"] = _APP_RANGE_DROP_ASK_TEMPLATE
        out["category"] = "APP-充电管理相关"
        out["level"] = "auto_draft_needs_info"
        return out
    if tid == "STATUS-012" and _is_app_blocks_charging(latest or body_text):
        out["template"] = _APP_BLOCKS_CHARGING_ASK_TEMPLATE
        out["category"] = "APP-充电管理相关"
        out["level"] = "auto_draft_needs_info"
        return out
    if tid == "MAN-001" and _is_manual_wrong_model(latest or body_text):
        out["template"] = _MANUAL_WRONG_MODEL_TEMPLATE
        out["level"] = "auto_draft_needs_info"
        return out
    if tid == "GEN-004" and _is_car_not_found_after_removal(latest or body_text):
        out["template"] = _CAR_NOT_FOUND_ASK_TEMPLATE
        out["level"] = "auto_draft_needs_info"
        return out
    if tid == "CHG-SCHED-001" and _is_charge_target_availability(latest or body_text):
        out["template"] = _CHARGE_TARGET_OPTION_TEMPLATE
        return out
    if tid == "STATUS-012" and _is_battery_capacity_mismatch(latest or body_text):
        out["template"] = _BATTERY_CAPACITY_ASK_TEMPLATE
        out["category"] = "APP-充电管理相关"
        out["level"] = "auto_draft_needs_info"
        return out
    if tid == "NON-005" and _is_battery_config_lookup(latest or body_text):
        out["template"] = _BATTERY_CONFIG_MANUAL_TEMPLATE
        return out
    if tid == "BIND-008" and _has_sos_ecall(latest or body_text):
        body = out.get("template") or ""
        if _ECALL_DEALER_NOTE.lower() not in body.lower():
            body, n = re.subn(
                r"\n*(Best regards,|Kind regards,)",
                "\n\n" + _ECALL_DEALER_NOTE + "\n\n\\1",
                body,
                count=1,
                flags=re.I,
            )
            if not n:
                body = body.rstrip() + "\n\n" + _ECALL_DEALER_NOTE + "\n"
            out["template"] = body
        return out
    return tmpl


def _is_app_bind_howto(text: str) -> bool:
    blob = text or ""
    if _is_qr_unreadable(blob):
        return False
    # 绑失败/绑不上是要信息或故障排查，不是「怎么绑」教程
    if _is_bind_ask_before_region_confirm(blob):
        return False
    if not _BIND_PAIR_RE.search(blob):
        return False
    if _BT_CALL_RE.search(blob):
        return False
    if re.search(r"digital\s+key", blob, re.I):
        return False
    if _is_privacy_notice_loop(blob):
        return False
    return True


def _is_vehicle_mgmt_howto(text: str) -> bool:
    """多车/删车/车辆管理入口。表单下拉 Vehicle Management 不算。"""
    hay = _webform_score_text(text or "")
    desc = _form_issue_description(text or "")
    return bool(_VEHICLE_MGMT_HOWTO_RE.search(f"{hay}\n{desc}"))


def _is_self_unbind_rebind(text: str) -> bool:
    """自己解绑再重绑。不是前任车主 72 小时。"""
    blob = f"{_webform_score_text(text or '')}\n{_form_issue_description(text or '')}"
    if _is_previous_owner_bind(blob):
        return False
    if _is_car_not_found_after_removal(blob):
        return False
    if _is_sold_delete_account(blob) or _is_bind_another_vehicle(blob):
        return False
    return bool(_SELF_UNBIND_REBIND_RE.search(blob) or _is_vehicle_mgmt_howto(text))


def _is_app_car_connect_fault(text: str) -> bool:
    """车/App 连不上（已有 VIN 的远联故障），不是绑车教程、也不是车辆管理页。"""
    hay = _webform_score_text(text or "")
    desc = _form_issue_description(text or "")
    blob = f"{hay}\n{desc}"
    if _is_app_bind_howto(blob) or _is_ismart_plan_or_3g(blob):
        return False
    if re.search(r"carplay|android\s+auto", blob, re.I):
        return False
    if _is_privacy_notice_loop(text) or _is_privacy_notice_loop(blob):
        return False
    if _is_vehicle_mgmt_howto(text):
        return False
    return bool(
        _APP_CAR_CONNECT_FAULT_RE.search(blob)
        or _is_bound_but_remote_fail(blob)
    )


def _is_forgot_password(text: str) -> bool:
    return bool(_FORGOT_PASSWORD_RE.search(text or ""))


def _is_privacy_notice_loop(text: str) -> bool:
    """只看问题描述（已剥表单页脚）。页脚 Privacy Notice 不是弹窗故障。"""
    desc = _strip_webform_privacy_footer(_form_issue_description(text or ""))
    return bool(_PRIVACY_LOOP_RE.search(desc))


def _is_ismart_plan_or_3g(text: str) -> bool:
    blob = text or ""
    if re.search(r"hotspot|1st\s+of\s+the\s+month|first\s+day\s+of\s+the\s+next\s+month", blob, re.I):
        return False
    return bool(_ISMART_PLAN_3G_RE.search(blob))


def ismart_plan_or_3g_needs_human(text: str) -> bool:
    """3G / 重启 iSMART 套餐：知识库行已下线，转人工。不要套 NET-001 热点/月初。"""
    blob = _latest_customer_turn(text or "") or (text or "")
    return _is_ismart_plan_or_3g(blob)


def _is_nav_voice_language(text: str) -> bool:
    return bool(_NAV_VOICE_LANG_RE.search(text or ""))


def _is_aa_infotainment_fault(text: str) -> bool:
    """AA/CarPlay 使用中车机卡顿/重启/导航崩溃 ≠ 删配对。"""
    blob = text or ""
    if not re.search(r"android\s+auto|carplay", blob, re.I):
        return False
    return bool(_AA_INFOTAINMENT_FAULT_RE.search(blob))


_VOICE_WAKE_RE = re.compile(
    r"("
    r"hello\s*mg|hallo\s*mg|"
    r"voice\s+(recognition|assistant|control|command)|"
    r"Sprach(erkennung|steuerung|assistent)|"
    r"commande\s+vocale|reconnaissance\s+vocale|"
    r"riconoscimento\s+vocale|assistente\s+vocale|"
    r"reconocimiento\s+de\s+voz|control\s+por\s+voz|"
    r"语音(助手|识别|控制)|车机语音"
    r")",
    re.I,
)
_VOICE_PHONE_USB_RE = re.compile(
    r"("
    r"connect(ed|er|ez)?\s+(the\s+)?phone.{0,48}usb"
    r"|usb.{0,48}(phone|handy|telefon|t[eé]l[eé]phone|android\s+auto|carplay)"
    r"|via\s+usb|per\s+usb|par\s+usb|"
    r"(cable|c[aâ]ble|kabel).{0,32}(phone|handy|telefon)|"
    r"plug\s+(in\s+)?(the\s+)?phone|"
    r"voice recognition.{0,80}(connected to|via|liée?e?\s+[àa]|collegat).{0,24}"
    r"(the\s+)?phone|"
    r"Telefon.{0,32}USB|USB.{0,32}(Telefon|Handy)|"
    r"brancher.{0,40}(t[eé]l[eé]phone|usb)|"
    r"collegare.{0,40}(telefono|usb)|"
    r"conectar.{0,40}(tel[eé]fono|usb)"
    r")",
    re.I | re.S,
)
_NATIVE_VOICE_SPEC_RE = re.compile(
    r"("
    r"classic voice|"
    r"(native|built[\s-]?in|on[\s-]?board|in[\s-]?car|vehicle)\s+voice|"
    r"does\s+the\s+(car|vehicle)\s+(not\s+)?have.{0,48}voice|"
    r"(car|vehicle).{0,24}(not\s+)?(have|support).{0,40}"
    r"(hello\s*mg|voice\s+(assistant|control|recognition))|"
    r"(user\s+manual|handbook|Betriebsanleitung|manuel).{0,80}"
    r"(phone|handy|t[eé]l[eé]phone|usb)|"
    r"keine\s+(eigene|klassische)\s+Sprach|"
    r"pas\s+de\s+commande\s+vocale\s+(int[eé]gr[eé]e|classique)|"
    r"没有.{0,12}(车机|独立)?语音"
    r")",
    re.I | re.S,
)
_AA_OR_CP_RE = re.compile(r"android\s+auto|carplay|apple\s+car\s*play", re.I)


def _is_hello_mg_phone_usb_voice(text: str) -> bool:
    """Hello MG / 方向盘语音只唤起连手机（USB/AA/CarPlay），或手册写手机语音、问有无车机语音。

    同类一律 FICM-VOICE-001，不走 CPAA 删配对、不因缺 VIN 改 GEN-002。
    """
    blob = text or ""
    if _is_nav_voice_language(blob) or _is_aa_infotainment_fault(blob):
        return False
    if not _VOICE_WAKE_RE.search(blob):
        return False
    if _VOICE_PHONE_USB_RE.search(blob):
        return True
    if _NATIVE_VOICE_SPEC_RE.search(blob):
        return True
    return bool(_AA_OR_CP_RE.search(blob) and re.search(
        r"hello\s*mg|hallo\s*mg|steering\s+wheel|Lenkrad|volant|volante",
        blob,
        re.I,
    ))


_PARKED_HINT_RE = re.compile(
    r"(parcheggiato|parked|estacionad|geparkt|à l'arrêt|停车)",
    re.I,
)
_FALSE_AWAKE_RE = re.compile(
    r"("
    r"accensione(\s+del\s+veicolo)?"
    r"|segnala l'accensione"
    r"|(vehicle|car|veicolo|app).{0,60}(acceso|accesa|running|started|turned on|is on)"
    r"|mostra.{0,40}(acceso|accesa|avviato)"
    r")",
    re.I | re.S,
)
_DRAIN_OR_DISABLED_RE = re.compile(
    r"("
    r"batteria scarica|battery.{0,24}(dead|flat|drain)|亏电|"
    r"batterie.{0,16}(leer|à plat)|"
    r"\binvalido\b|\bdisabled\b|handicap|disabilit"
    r")",
    re.I,
)


def parked_false_awake_needs_human(text: str) -> bool:
    """停车后仍报已启动/点火，并亏电或行动不便 → 转人工，不套 STATUS-001。"""
    blob = _latest_customer_turn(text or "")
    if not blob.strip():
        return False
    if not _PARKED_HINT_RE.search(blob):
        return False
    if not _FALSE_AWAKE_RE.search(blob):
        return False
    return bool(_DRAIN_OR_DISABLED_RE.search(blob))


_SERVICE_SUBSCRIPTION_RE = re.compile(
    r"("
    r"service\s+subscription|"
    r"connected\s+services?\s+subscription|"
    r"live\s+services?\s+subscription|"
    r"ismart\s+subscription|"
    r"(renew|renewal|expire[ds]?|expir(?:y|ation)|cancel|purchase|buy|"
    r"pay\s+for|subscribe\s+to).{0,48}"
    r"(ismart\s+)?(connected\s+|live\s+)?services?\s+subscription|"
    r"abonnement.{0,48}(ismart|service|connect[ée]?s?|live)|"
    r"(ismart|services?\s+connect[ée]?s?).{0,40}abonnement|"
    r"suscripci[oó]n.{0,40}(servicio|ismart|conectad)|"
    r"abbonamento.{0,40}(ismart|servizi\s+connessi)|"
    r"服务订阅"
    r")",
    re.I | re.S,
)


def _body_region_is_europe(text: str) -> bool:
    from .region_catalog import (
        extract_country_field, lookup_region, lookup_region_in_text,
    )
    field = extract_country_field(text or "")
    hit = lookup_region(field) if field else None
    if hit and hit.region == "Europe":
        return True
    hit2 = lookup_region_in_text(text or "")
    return bool(hit2 and hit2.region == "Europe")


def europe_service_subscription_needs_human(text: str) -> bool:
    """欧洲 iSMART/联网服务订阅（续费、到期、购买）→ 转人工。不是 3G 流量套餐。"""
    blob = _latest_customer_turn(text or "")
    if not blob.strip():
        return False
    if _is_ismart_plan_or_3g(blob):
        return False
    if not _SERVICE_SUBSCRIPTION_RE.search(blob):
        return False
    return _body_region_is_europe(text or "")


def _strip_vin_from_intake_list(text: str, body: str) -> str:
    """信里已有 VIN 时，去掉 STATUS-001 类清单里的 VIN 行并重编号。"""
    if not text or not _body_has_vin(body):
        return text
    if not re.search(r"^\s*\d+\.\s*VIN\b", text, re.I | re.M):
        return text
    lines = text.split("\n")
    out: list[str] = []
    in_list = False
    n = 1
    for line in lines:
        if re.search(r"provide the following information", line, re.I):
            in_list = True
            out.append(line)
            continue
        if in_list and re.match(r"\s*\d+\.\s", line):
            if re.match(r"\s*\d+\.\s*VIN\b", line, re.I):
                continue
            line = re.sub(r"^\s*\d+\.", f"{n}.", line, count=1)
            n += 1
            out.append(line)
            continue
        if in_list and line.strip() and not re.match(r"\s*\d+\.\s", line):
            in_list = False
        out.append(line)
    return "\n".join(out)


def _strip_already_known_intake_asks(
    text: str, body: str, template_id: str = "",
) -> str:
    """表单已有 VIN/车型，或手册行已讲清路径时，不要再要一遍。"""
    if not text:
        return text
    has_vin = _body_has_vin(body)
    has_model = bool(_stated_vehicle_model(body))
    tid = template_id or ""

    def _drop(sent: str) -> str | None:
        s = sent.strip()
        if not s:
            return sent
        if tid == "MAN-001" and re.search(
            r"if the manual is still not displayed|"
            r"please provide your VIN.{0,80}screenshot",
            s,
            re.I | re.S,
        ):
            return None
        if has_vin and has_model and re.search(
            r"(provide|send|share).{0,80}VIN.{0,48}vehicle model|"
            r"VIN,\s*vehicle model",
            s,
            re.I | re.S,
        ):
            return None
        if has_vin and re.search(
            r"(please\s+)?(provide|send|share).{0,40}(your\s+)?"
            r"(17[-\s]?character\s+)?VIN\b",
            s,
            re.I,
        ) and not re.search(
            r"screenshot|app version|software version|video",
            s,
            re.I,
        ):
            return None
        return sent

    paragraphs = text.split("\n")
    kept: list[str] = []
    for para in paragraphs:
        if not para.strip():
            kept.append(para)
            continue
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z“\"])", para)
        new_parts: list[str] = []
        for part in parts:
            out = _drop(part)
            if out:
                new_parts.append(out)
        if new_parts:
            kept.append(" ".join(new_parts))
    cleaned = "\n".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() + ("\n" if text.endswith("\n") else "")


_STILL_IMAGE_ASK_RE = re.compile(
    r"\b(screenshots?|photos?|pictures?|images?)\b",
    re.I,
)
_VIDEO_ASK_RE = re.compile(
    r"\b(videos?|screen\s*recordings?|short\s+video)\b",
    re.I,
)
_ACK_EXISTING_IMAGE_RE = re.compile(
    r"screenshot you (?:attached|sent)|photo you (?:attached|sent)|"
    r"from the screenshot|in your screenshot|the screenshot you attached",
    re.I,
)
_PDF_OR_DOC_DESC_RE = re.compile(
    r"\]\s*\(PDF|\]\s*\(docx|\.pdf\b|\.docx\b|旧版 \.doc",
    re.I,
)


def _customer_sent_images(image_descriptions: list[str] | None) -> bool:
    """vision / 图片附件描述才算有图；纯 PDF/Word 抽取不算。"""
    for line in image_descriptions or []:
        if not (line or "").strip():
            continue
        if _PDF_OR_DOC_DESC_RE.search(line) and not re.search(
            r"\.(png|jpe?g|gif|webp|heic|bmp)\b", line, re.I,
        ):
            continue
        return True
    return False


def _drop_still_image_phrases(sentence: str) -> str:
    s = sentence
    s = re.sub(
        r"screenshots?\s*(?:,|/|or)\s*",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"(?:photos?|pictures?|images?)\s*(?:,|/|or)\s*",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"\s*(?:,|/|or)\s*screenshots?\b",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    return s.strip()


def _is_numbered_intake_line(line: str) -> bool:
    """清单行「1. Foo」整行处理，勿在序号后的句点处切开。"""
    return bool(re.match(r"^\s*\d+\.\s+\S", line or ""))


def _strip_screenshot_asks_when_attached(
    text: str,
    image_descriptions: list[str] | None,
) -> str:
    """客户已附图时，草稿不再要截图/照片；视频、App 版本仍可要。"""
    if not text or not _customer_sent_images(image_descriptions):
        return text

    def _keep_sentence(sent: str) -> str | None:
        s = sent.strip()
        if not s:
            return sent
        if _ACK_EXISTING_IMAGE_RE.search(s):
            return sent
        has_still = bool(_STILL_IMAGE_ASK_RE.search(s))
        has_video = bool(_VIDEO_ASK_RE.search(s))
        if not has_still:
            return sent
        if has_video:
            return _drop_still_image_phrases(s)
        return None

    paragraphs = text.split("\n")
    kept: list[str] = []
    for para in paragraphs:
        if not para.strip():
            kept.append(para)
            continue
        # 「7. Screenshot …」里序号后的句点不能当句子边界，否则会留下空的「7.」
        if _is_numbered_intake_line(para):
            out = _keep_sentence(para)
            if out is None:
                continue
            if re.match(r"^\s*\d+\.\s*$", out):
                continue
            kept.append(out)
            continue
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z“\"])", para)
        if len(parts) == 1:
            # 无句号的清单行（Screenshots showing ...）
            out = _keep_sentence(para)
            if out is None:
                continue
            kept.append(out)
            continue
        new_parts: list[str] = []
        for part in parts:
            out = _keep_sentence(part)
            if out:
                new_parts.append(out)
        if new_parts:
            kept.append(" ".join(new_parts))

    lines = kept
    out: list[str] = []
    n = 1
    in_list = False
    for line in lines:
        m = re.match(r"^(\s*)(\d+)\.\s+(.*)$", line)
        if m:
            rest = (m.group(3) or "").strip()
            if not rest:
                continue
            if not in_list:
                in_list = True
                n = 1
            out.append(f"{m.group(1)}{n}. {rest}")
            n += 1
            continue
        in_list = False
        n = 1
        out.append(line)
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() + ("\n" if text.endswith("\n") else "")


def _is_map_quality_feedback(text: str) -> bool:
    """地图/充电桩数据差、问何时更新 ≠ 只要软件版本截图。"""
    if _is_nav_voice_language(text or ""):
        return False
    if _is_map_online_update_failed(text or ""):
        return False
    return bool(_MAP_QUALITY_FB_RE.search(text or ""))


def _is_map_online_update_failed(text: str) -> bool:
    """在线地图更新失败 / Update failed 不是何时 OTA、也不是地图数据吐槽。"""
    blob = f"{_form_issue_description(text or '')}\n{_webform_score_text(text or '')}"
    if _is_nav_language_howto(blob) or _is_nav_voice_language(blob):
        return False
    return bool(_MAP_UPDATE_FAIL_RE.search(blob))


def _is_charging_alert_request(text: str) -> bool:
    return bool(_CHG_ALERT_RE.search(text or ""))


def _is_app_gps_background(text: str) -> bool:
    return bool(_GPS_BG_RE.search(text or ""))


def _is_cruise_howto(text: str) -> bool:
    blob = text or ""
    if _TOW_ACC_RE.search(blob):
        return False
    return bool(_CRUISE_HOWTO_RE.search(blob))


def _is_infotainment_update_consult(text: str) -> bool:
    """车机/CarPlay/AA 有没有升级 ≠ CPAA 删配对重连。"""
    blob = text or ""
    if _is_infotainment_language_request(blob):
        return False
    if not _INFOTAIN_UPDATE_RE.search(blob):
        return False
    if _CPAA_PAIR_FAULT_RE.search(blob) and not re.search(
        r"(aggiornamento|update|upgrade)", blob, re.I,
    ):
        return False
    return True


def _is_tyre_pressure_consult(text: str) -> bool:
    return bool(_TYRE_RE.search(text or ""))


def _is_address_change_request(text: str) -> bool:
    return bool(_ADDRESS_RE.search(text or ""))


def _is_parts_purchase(text: str) -> bool:
    blob = text or ""
    if _is_paint_warranty(blob):
        return False
    return bool(_PARTS_BUY_RE.search(blob))


_AFTERMARKET_ACCESSORY_RE = re.compile(
    r"("
    r"after[\s-]?market|"
    r"pl[au]g\s*[&+]?\s*play|plug\s+and\s+play|"
    r"pedal\s*(sprint|box|booster)|throttle\s*(controller|box)|"
    r"third[\s-]?party.{0,48}accessor|"
    r"(accessor[iy]o?|accessorio).{0,80}"
    r"(fit|mount|install|montare|montaggio)|"
    r"(fit|mount|install|montare).{0,80}"
    r"(accessor|after[\s-]?market|pedal|anti-?theft)|"
    r"pre-?wired connectors|"
    r"改装|加装|后装|第三方配件"
    r")",
    re.I | re.S,
)


def _is_aftermarket_accessory_ask(text: str) -> bool:
    """第三方后装件能不能装 → NON-010，不是车机升级、也不是进店检修。"""
    blob = text or ""
    if _is_third_party_home_charger_fault(blob):
        return False
    return bool(_AFTERMARKET_ACCESSORY_RE.search(blob))


def _is_wireless_aa_update_consult(text: str) -> bool:
    if not _is_infotainment_update_consult(text or ""):
        return False
    return bool(re.search(
        r"wireless|without\s+cables|senza\s+cavi|sans\s+fil|"
        r"kabellos|sin\s+cables|无线",
        text or "",
        re.I,
    ))


def _is_body_image_mismatch(text: str) -> bool:
    if _is_colour_code_ask(text or ""):
        return False
    if _is_battery_capacity_mismatch(text or ""):
        return False
    return bool(_BODYIMG_RE.search(text or ""))


def _is_energy_stats_inquiry(text: str) -> bool:
    blob = text or ""
    if _is_charging_power_howto(blob):
        return False
    if re.search(r"petrol|gasoline|diesel|油耗", blob, re.I):
        return False
    return bool(_ENERGY_STATS_RE.search(blob))


_TBOX_REPLACED_RE = re.compile(
    r"("
    r"(replaced|更换|换了|ausgetauscht|gewechselt|remplac|"
    r"cambiad|sostituit).{0,48}t-?\s*box"
    r"|new\s+t-?\s*box"
    r"|t-?\s*box.{0,48}(replaced|already\s+replaced|was\s+replaced|"
    r"ausgetauscht|gewechselt|remplac|换了)"
    r"|after.{0,24}(t-?\s*box|tcu).{0,32}(replac|install|fit|activat)"
    r"|tcu.{0,32}(replaced|new\s+unit)"
    r")",
    re.I | re.S,
)
_TBOX_REPLACE_SPECULATION_RE = re.compile(
    r"("
    r"(need|needs|may\s+need|might\s+need|talk about).{0,40}"
    r"(t-?\s*box|tcu).{0,40}replac"
    r"|(t-?\s*box|tcu).{0,40}(need|needs).{0,24}(update|replac)"
    r")",
    re.I | re.S,
)


def _is_tbox_replaced(text: str) -> bool:
    """经销商已换 TBox/TCU 后绑不上/远联没有 ≠ 口头怀疑该换件。"""
    blob = _webform_score_text(text or "")
    desc = _form_issue_description(text or "")
    hay = f"{blob}\n{desc}"
    if _TBOX_REPLACE_SPECULATION_RE.search(hay) and not re.search(
        r"(already|has been|have been|was|were|刚|已经).{0,24}"
        r"(replac|ausgetauscht|换)",
        hay,
        re.I,
    ):
        if not re.search(
            r"(dealer|workshop|garage|4s).{0,40}(replac|fit|install)",
            hay,
            re.I,
        ):
            return False
    return bool(_TBOX_REPLACED_RE.search(hay))


def _is_backend_telematics_outage(text: str) -> bool:
    """远联长期无数据 / 车机无网 / 要后台查套餐 ≠ 停车休眠唤醒，也 ≠ 数字钥匙。"""
    blob = text or ""
    if _is_tbox_replaced(blob):
        return False
    if _SLEEP_WAKE_RE.search(blob):
        return False
    if re.search(r"digital\s+key|数字钥匙", blob, re.I):
        return False
    if not (
        _NOT_RESPOND_RE.search(blob) or _HU_NO_INTERNET_RE.search(blob)
    ):
        return False
    return bool(
        _TELEM_PLAN_ASK_RE.search(blob)
        or _BACKEND_TELEM_RE.search(blob)
    )


def _is_vehicle_not_responded(text: str) -> bool:
    blob = text or ""
    if _is_tbox_replaced(blob):
        return False
    if _is_backend_telematics_outage(blob):
        return False
    return bool(_NOT_RESPOND_RE.search(blob))


def _is_digital_key_availability(text: str) -> bool:
    blob = text or ""
    if _is_spec_feature_missing(blob):
        return False
    if _is_digital_key_status_notice(blob):
        return False
    if _is_digital_key_howto(blob):
        return False
    if re.search(r"(cannot|can'?t|unable).{0,24}(unlock|lock|start)", blob, re.I):
        return False
    if re.search(r"digital\s+key", blob, re.I) and re.search(
        r"can\s+i\s+use", blob, re.I,
    ):
        return True
    return bool(_DK_AVAIL_RE.search(blob))


def _is_digital_key_howto(text: str) -> bool:
    """怎么激活/是什么 → APP-DIGITAL KEY-001 + PDF。保活通知、有没有该功能不算。"""
    blob = text or ""
    if _is_spec_feature_missing(blob):
        return False
    if _is_digital_key_status_notice(blob):
        return False
    if _is_backend_telematics_outage(blob):
        return False
    if _INCIDENTAL_BT_KEY_RE.search(blob) and not re.search(
        r"digital\s+key|数字钥匙", blob, re.I,
    ):
        return False
    if re.search(r"(cannot|can'?t|unable).{0,24}(unlock|lock|start)", blob, re.I):
        return False
    if re.search(
        r"why.{0,48}digital\s+key.{0,80}(not\s+(get|appear|show)|doesn'?t)",
        blob, re.I | re.S,
    ):
        return False
    return bool(_DK_HOWTO_RE.search(blob))


def _is_app_crash_needs_shot(text: str) -> bool:
    """App 闪退 / iSMART has stopped：错误已点名但没有录屏，要截图。"""
    blob = text or ""
    if _is_app_tpms_alarm(blob):
        return False
    # "app not responding" 走 STATUS-003，不要 GEN-004
    if _APP_NOT_RESPONDING_RE.search(blob) and not re.search(
        r"has stopped|force.?clos|keeps? crashing|keeps? stopping",
        blob,
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"("
            r"ismart.{0,24}has stopped"
            r"|mg.{0,16}ismart.{0,24}stopped"
            r"|(app|ismart).{0,32}(keeps? stopping|force.?clos|keeps? crashing)"
            r"|has stopped['\s]*warning"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_error_code_47(text: str) -> bool:
    return bool(re.search(r"error\s*code\s*47|\bcode\s*47\b", text or "", re.I))


def _is_network_unavailable_toast(text: str) -> bool:
    """App 弹 The network is not available / 网络不可用。不是扫码 BIND-008，也不是地区 REG-001。"""
    blob = f"{_form_issue_description(text or '')}\n{_webform_score_text(text or '')}"
    if _is_qr_unreadable(blob):
        return False
    if re.search(r"\bqr\b", blob, re.I) and re.search(
        r"service\s+is\s+not\s+available", blob, re.I
    ):
        return False
    return bool(
        re.search(
            r"("
            r"the\s+network\s+is\s+not\s+available"
            r"|network\s+(is\s+)?not\s+available"
            r"|network\s+unavailable"
            r"|r[eé]seau.{0,32}pas\s+disponible"
            r"|netz(werk)?\s+nicht\s+verf"
            r"|netwerk\s+niet\s+beschikbaar"
            r"|red\s+no\s+disponible"
            r"|rete\s+non\s+disponibile"
            r"|网络不可用"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_qr_unreadable(text: str) -> bool:
    blob = text or ""
    return bool(
        re.search(
            r"("
            r"qr\s*(code)?\s*.{0,48}(recogn|invalid|unreadable|try again|contact us)"
            r"|qr.{0,40}(very\s+small|too\s+small)"
            r"|(can'?t|cannot|unable).{0,24}scan.{0,20}qr"
            r"|can'?t\s+recogniz.{0,32}(qr|code|bind)"
            r"|cannot\s+recogniz.{0,32}(qr|code)"
            r"|scan.{0,32}(qr|code).{0,32}(fail|error|retry|recogn)"
            r"|二维码.{0,16}(无法识别|请重试|请联系)"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_colour_code_ask(text: str) -> bool:
    return bool(
        re.search(
            r"(colou?r\s*code|paint\s*code|ral\s*code|色号|颜色代码)",
            text or "",
            re.I,
        )
    )


def _is_fuel_economy_complaint(text: str) -> bool:
    blob = text or ""
    if _is_energy_stats_inquiry(blob):
        return False
    return bool(
        re.search(
            r"("
            r"(fuel|petrol|gasoline|diesel|gas)\s+(consumption|economy).{0,24}"
            r"(high|too\s+much|poor|bad)"
            r"|too\s+much.{0,16}(petrol|gasoline|fuel|diesel)"
            r"|take[s]?\s+to+\s+much\s+spend\s+petrol"
            r"|consum.{0,24}(too\s+much|a\s+lot).{0,16}(fuel|petrol)"
            r"|油耗过高|费油"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_nav_language_howto(text: str) -> bool:
    blob = text or ""
    if _is_nav_voice_language(blob):
        return False
    return bool(
        re.search(
            r"("
            r"how\s+to\s+change.{0,40}(navigat|nav).{0,24}language"
            r"|change\s+the\s+language\s+of\s+the\s+navigation"
            r"|navigation.{0,24}language.{0,24}(change|switch|set)"
            r"|更换导航语言|导航语言"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_spec_feature_missing(text: str) -> bool:
    """配置没有某 App 页/功能，不是故障、也不是数字钥匙激活教程。"""
    blob = text or ""
    if _is_error_code_47(blob):
        return False
    if re.search(
        r"how.{0,24}(activat|enable|add).{0,24}digital\s+key",
        blob,
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"("
            r"don'?t\s+have\s+digital\s+key"
            r"|do\s+not\s+have\s+digital\s+key"
            r"|no\s+digital\s+key\s+(page|tab|menu|section)"
            r"|digital\s+key.{0,24}(page|tab).{0,24}(not|missing|don'?t)"
            r"|can'?t\s+use\s+hello\s*mg"
            r"|hello\s*mg.{0,32}(not\s+(shown|available)|missing|don'?t)"
            r"|other\s+(owners?|cars?|vehicles?).{0,40}have"
            r"|别的车主"
            r"|(no|missing|not\s+shown|don'?t\s+have).{0,40}"
            r"(temperature|climate).{0,24}(page|slider|screen)"
            r"|没有.{0,12}(温度|空调|数字钥匙|Hello\s*MG).{0,12}(页面|功能)"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_app_mileage_howto(text: str) -> bool:
    blob = text or ""
    if _is_energy_stats_inquiry(blob):
        return False
    return bool(
        re.search(
            r"("
            r"(\bwhere\b|\bhow\b).{0,40}(total\s+)?mileage"
            r"|\bview\b.{0,24}(total\s+)?mileage"
            r"|total\s+mileage.{0,24}(app|ismart)"
            r"|车辆总里程|查看.{0,12}里程"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_charge_target_mismatch(text: str) -> bool:
    """预约充电目标 80/100 与实充不一致 → STATUS-013。"""
    blob = text or ""
    if _is_charge_target_availability(blob):
        return False
    if _is_third_party_home_charger_fault(blob):
        return False
    if _is_charging_schedule_howto(blob) and not re.search(
        r"(80\s*%|100\s*%|target|not being saved|不匹配|不一致)",
        blob,
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"("
            r"80\s*%.{0,40}100\s*%|100\s*%.{0,40}80\s*%|"
            r"charging setting.{0,32}(not being saved|changes back)|"
            r"target (soc|value|charge).{0,32}(mismatch|not|wrong|80|100)|"
            r"设置充.{0,16}(80|100)|实际充.{0,16}(80|100)|目标值"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_charge_mgmt_fault(text: str) -> bool:
    blob = text or ""
    if _is_third_party_home_charger_fault(blob):
        return False
    if _is_charge_target_mismatch(blob):
        return False
    if _is_charging_schedule_howto(blob) and not re.search(
        r"(no\s+data|empty|not\s+(shown|display)|didn'?t\s+(start|work)|invalid)",
        blob,
        re.I,
    ):
        return False
    if _is_charging_power_howto(blob):
        return False
    return bool(
        re.search(
            r"("
            r"charging\s+management"
            r"|charge\s+management"
            r"|(charging\s+)?schedule.{0,40}(not\s+work|didn'?t|invalid|mismatch)"
            r"|cannot\s+(see|view)\s+charg"
            r"|no\s+(charg\w+\s+)?(status|data|info).{0,32}(app|page)"
            r"|预约充电.{0,16}(无效|没数据|不一致)"
            r"|充电管理"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_app_tpms_alarm(text: str) -> bool:
    blob = text or ""
    if re.search(
        r"tyre\s+shop|tire\s+shop|where.{0,40}(take|go)|any\s+(tyre|tire|dealer)",
        blob,
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"("
            r"(app|ismart|notification).{0,48}(keeps?|always|still|repeated).{0,24}"
            r"(tyre|tire)"
            r"|(tyre|tire).{0,32}(alarm|alert|notification)"
            r"|low\s+pressure\s+tire\s+alarm"
            r"|一直提示.{0,12}胎压|胎压.{0,16}(报警|通知|一直)"
            r"|alarm opon|niskim ci[sś]nieni|ciśnieniu.{0,24}opon"
            r"|dopompowaniu.{0,40}alarm"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_vehicle_hotspot_password(text: str) -> bool:
    return bool(
        re.search(
            r"("
            r"hot\s?spot\s+password|hotpot\s+password"
            r"|(wifi|wi-fi)\s+password.{0,24}(car|vehicle|infotainment)"
            r"|热点密码"
            r")",
            text or "",
            re.I,
        )
    )


def _is_sold_delete_account(text: str) -> bool:
    """卖车/还租车后要删账号或解绑，不是换车再绑。"""
    blob = text or ""
    if _is_bind_another_vehicle(blob):
        return False
    if _is_remove_vehicle_from_app(blob):
        return False
    return bool(
        re.search(
            r"("
            r"delete my account|remove my account|delete the account"
            r"|delete my email and phone"
            r"|delete all my personal"
            r"|sold the car|just sold|i('ve| have) sold"
            r"|handing back.{0,40}lease|lease car.{0,48}delete"
            r"|uninstall the app.{0,80}(sold|unlink|unbind)"
            r"|(sold|sell).{0,80}(unlink|unbind|uninstall)"
            r"|把车卖了|删账号|删除账号"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_bind_another_vehicle(text: str) -> bool:
    """换车后再绑 / 同一账号加新车，不是卖车删号。"""
    blob = text or ""
    return bool(
        re.search(
            r"("
            r"bind another (vehicle|car)"
            r"|change another vehicle to bind"
            r"|add(ing)? (my |the )?(new )?vehicle"
            r"|swapped?.{0,24}(car|vehicle|mg)"
            r"|traded.{0,24}(car|vehicle|mg)"
            r"|getauscht|gegen einen neuen"
            r"|dieses fahrzeug mit der app"
            r"|换车.{0,24}绑|绑多辆|再绑一辆"
            r")",
            blob,
            re.I | re.S,
        )
    )


def _is_fota_when_or_connect(text: str) -> bool:
    blob = text or ""
    if _is_post_update_fault(blob):
        return False
    if _is_map_online_update_failed(blob):
        return False
    if re.search(r"(revers|camera|black\s+screen|infotainment\s+hang)", blob, re.I):
        return False
    return bool(_FOTA_CONNECT_RE.search(blob))


def _is_car_not_found_after_removal(text: str) -> bool:
    """删掉旧车后新车 car not found、旧信息还在。要账号、视频和新 VIN，不是车辆管理教程。"""
    blob = text or ""
    if not re.search(r"car\s+not\s+found|vehicle\s+not\s+found", blob, re.I):
        return False
    return bool(re.search(
        r"(removed|remove).{0,48}(old\s+)?(car|vehicle)"
        r"|old\s+(info|information|details).{0,32}still"
        r"|still\s+(shown|displayed)"
        r"|replac",
        blob,
        re.I | re.S,
    ))


def _is_remove_vehicle_from_app(text: str) -> bool:
    """卖车后从 App 移除车辆。不是删除账号。"""
    blob = text or ""
    if re.search(
        r"delete\s+(my|the)\s+account|delete my email and phone",
        blob,
        re.I,
    ):
        return False
    if _is_car_not_found_after_removal(blob):
        return False
    return bool(re.search(
        r"remove it from the app"
        r"|(sell|sold).{0,48}how to remove"
        r"|how to remove.{0,40}(it|the car|the vehicle|my car).{0,24}(from the )?app",
        blob,
        re.I | re.S,
    ))


_CHARGE_TARGET_AVAIL_RE = re.compile(
    r"("
    r"(unable|cannot|can'?t|not able).{0,40}charge\s+to\s+80"
    r"|only\s+(has|have|got|option).{0,48}(fully\s+charge|full\s+charge)"
    r"|option to fully charge"
    r"|charge[\s-]?target.{0,40}(option|feature|available|support|exist)"
    r"|(does|do)\s+(the\s+)?(car|vehicle).{0,48}(have|support).{0,32}"
    r"(target\s+charge|charge\s+target|charging\s+schedule)"
    r")",
    re.I | re.S,
)
_CHARGE_TARGET_NOT_SAVED_RE = re.compile(
    r"(not being saved|changes back|reverts|keeps setting|"
    r"changed the target back)",
    re.I,
)


def _is_charge_target_availability(text: str) -> bool:
    """在问有没有充电目标/预约项。设了又跳回 80/100 仍是 STATUS-013。"""
    blob = text or ""
    if _CHARGE_TARGET_NOT_SAVED_RE.search(blob):
        return False
    if _is_third_party_home_charger_fault(blob):
        return False
    return bool(_CHARGE_TARGET_AVAIL_RE.search(blob))


def _is_battery_capacity_mismatch(text: str) -> bool:
    """App 上的电量和车上的电池容量不是同一个数。"""
    blob = text or ""
    kwh = re.findall(r"(\d{2}(?:[.,]\d+)?)\s*kwh", blob, re.I)
    norm = {n.replace(",", ".") for n in kwh}
    if len(norm) >= 2:
        return True
    caps = re.findall(r"\d{2}[.,]\d+", blob)
    if len(set(caps)) < 2:
        return False
    return bool(re.search(r"\bkwh\b|battery|pack", blob, re.I)) and bool(
        re.search(r"mileage|range|charge|battery", blob, re.I)
    )


def _is_battery_config_lookup(text: str) -> bool:
    """问电池配置怎么查。没有两个容量数字。"""
    blob = text or ""
    if _is_battery_capacity_mismatch(blob):
        return False
    if _is_accessory_battery_fault(blob):
        return False
    return bool(re.search(
        r"(battery\s+config|battery configuration|"
        r"(can'?t|cannot|unable).{0,24}(get|find|see).{0,24}battery)",
        blob,
        re.I | re.S,
    ))


_REBOOT_LOOP_RE = re.compile(
    r"("
    r"(screen|display|infotainment|head[\s-]?unit|central|centre|center)"
    r".{0,48}(continuously|keeps?|constantly|repeatedly)"
    r".{0,20}(restart|reboot)"
    r"|(continuously|keeps?|constantly|repeatedly).{0,20}(restart|reboot)"
    r".{0,48}(screen|display|journey|cruise|camera)"
    r"|reboot\s+loop|反复重启"
    r")",
    re.I | re.S,
)


def _is_infotainment_reboot_loop(text: str) -> bool:
    """车机行驶中自己反复重启。黑屏后恢复仍是 FICM-003。AA/CarPlay 仍是 FICM-004。"""
    blob = text or ""
    if _is_aa_infotainment_fault(blob):
        return False
    return bool(_REBOOT_LOOP_RE.search(blob))


def _is_key_left_in_boot(text: str) -> bool:
    """钥匙留在后备箱、车锁上、App 开不了门。人被锁在车外滞留仍是 KEY-002。"""
    blob = text or ""
    if re.search(r"stranded|parking\s+(lot|area)|car\s+park", blob, re.I):
        return False
    if not re.search(r"(key|keys).{0,48}(boot|trunk)|(boot|trunk).{0,48}(key|keys)", blob, re.I):
        return False
    return bool(re.search(
        r"(lock|unlock|cannot connect|can'?t connect|unable to connect|\bapp\b)",
        blob,
        re.I,
    ))


def _is_connect_pin_code(text: str) -> bool:
    """绑车时没收到 PIN。验证码和 error code 不算。"""
    blob = text or ""
    if re.search(
        r"verification\s+code|error\s*code|code\s*47|sms\s+code|one[\s-]?time",
        blob,
        re.I,
    ):
        return False
    return bool(re.search(
        r"("
        r"(didn'?t|did\s+not|don'?t|do\s+not|never|not|no).{0,24}"
        r"(get|receive|got|received).{0,32}(a\s+)?code.{0,48}"
        r"(connect|vehicle|car|app)"
        r"|code\s+to\s+connect\s+(my\s+|the\s+)?(vehicle|car)"
        r")",
        blob,
        re.I | re.S,
    ))


def _is_vehicle_status_stale(text: str) -> bool:
    """App 车况停在某一天或某一电量，车已经变了。不是 OTA，也不是 update mode。"""
    blob = text or ""
    if _is_remote_ui_stuck_updating(blob) or _is_fota_when_or_connect(blob):
        return False
    if _is_map_online_update_failed(blob):
        return False
    if re.search(r"\b(fota|ota)\b|update mode|when will.{0,24}update", blob, re.I):
        return False
    return bool(re.search(
        r"("
        r"does not update the status|doesn'?t update the status"
        r"|status.{0,24}stuck"
        r"|stuck on\s+\d"
        r"|charg\w*.{0,16}stuck|stuck at\s+\d"
        r"|(car|vehicle)\s+updates?.{0,48}(not|doesn'?t|does not).{0,20}app"
        r"|(car|vehicle)\s+updates?.{0,48}app\s+(does\s+not|doesn'?t|will\s+not|won'?t)"
        r"|app.{0,32}(does not|doesn'?t|won'?t|will not)\s+update"
        r"|won'?t update.{0,20}charg"
        r"|charge stuck"
        r"|(range|miles|soc).{0,24}stuck"
        r")",
        blob,
        re.I | re.S,
    ))


def _is_change_registered_email(text: str) -> bool:
    """把 App 换到另一个邮箱，好让自己的邮箱去绑新车。不是前车主 72 小时。"""
    blob = text or ""
    if _is_forgot_password(blob) or _is_sold_delete_account(blob):
        return False
    return bool(re.search(
        r"("
        r"(husband|wife|spouse|partner).{0,48}e-?mail"
        r"|e-?mail.{0,48}(husband|wife|spouse)"
        r"|registered\s+e-?mail"
        r"|(put|move|transfer|change).{0,32}(the\s+)?(app|account).{0,32}e-?mail"
        r"|own\s+e-?mail.{0,40}bind"
        r"|bind.{0,40}own\s+e-?mail"
        r")",
        blob,
        re.I | re.S,
    ))


def _is_other_device_login(text: str) -> bool:
    blob = text or ""
    return bool(re.search(
        r"("
        r"another\s+device\s+is\s+connected"
        r"|other\s+device.{0,32}(connected|logged)"
        r"|kicked\s+out.{0,48}(device|login|logged)"
        r"|logged\s+(out|off).{0,40}another\s+device"
        r")",
        blob,
        re.I | re.S,
    ))


def _is_explicit_en_rebind(text: str) -> bool:
    """英文里明确先解绑再重新绑定。西语 desvincular 仍只走 BIND-007。"""
    blob = text or ""
    if re.search(r"desvincular|volver\s+a\s+vincular", blob, re.I):
        return False
    if (
        _is_previous_owner_bind(blob)
        or _is_sold_delete_account(blob)
        or _is_car_not_found_after_removal(blob)
    ):
        return False
    return bool(re.search(
        r"(unbind|unlink).{0,80}(re[\s-]*bind|bind\s+again)"
        r"|(re[\s-]*bind).{0,48}(unbind|unlink)",
        blob,
        re.I | re.S,
    ))


def _has_sos_ecall(text: str) -> bool:
    return bool(re.search(r"\be-?call\b|\bsos\b", text or "", re.I))


_VAGUE_APP_COMPLAINT_RE = re.compile(
    r"\b(very\s+bad|terrible|awful|horrible)\s+app\b",
    re.I,
)
_CONCRETE_FAULT_WORD_RE = re.compile(
    r"\b(bind|password|charg|qr|screen|key|connect|update|owner|account|vin|login)\b",
    re.I,
)


def _is_vague_app_complaint(text: str) -> bool:
    """只有 App 很差，没有具体故障。"""
    desc = _form_issue_description(text or "")
    one = re.sub(r"\s+", " ", desc).strip()
    if not _VAGUE_APP_COMPLAINT_RE.search(one):
        return False
    if len(one) > 80:
        return False
    core = _VAGUE_APP_COMPLAINT_RE.sub(" ", one)
    return not bool(_CONCRETE_FAULT_WORD_RE.search(core))


def vague_app_connect_needs_human(text: str) -> bool:
    """描述只有 unable to connect to the application。有具体连不上的故障仍走 STATUS-003。"""
    desc = _form_issue_description(text or "")
    one = re.sub(r"\s+", " ", desc).strip().strip(" .!")
    return bool(re.fullmatch(r"unable to connect to the application", one, re.I))


def _review_lock(latest: str) -> tuple[Optional[str], frozenset[str]]:
    """审稿表硬闸：返回 (强制 template_id, 禁选 id)。无命中则 (None, empty)。"""
    blob = latest or ""
    checks: list[tuple[bool, str, frozenset[str]]] = [
        (_is_car_not_found_after_removal(blob), "GEN-004",
         frozenset({
             "ACC-008", "BIND-007", "GEN-002", "APP-GUIDE-001", "ACC-007",
         })),
        (_is_remove_vehicle_from_app(blob), "BIND-007",
         frozenset({"ACC-007", "ACC-008", "GEN-002", "APP-GUIDE-001"})),
        (_is_sold_delete_account(blob), "ACC-007",
         frozenset({"BIND-007", "ACC-005", "APP-GUIDE-001", "GEN-002", "MAN-001"})),
        (_is_bind_another_vehicle(blob), "ACC-008",
         frozenset({"BIND-007", "APP-GUIDE-001", "ACC-005", "GEN-002", "STATUS-007"})),
        (_is_app_crash_needs_shot(blob), "GEN-004",
         frozenset({"MAN-001", "GEN-001", "GEN-005", "STATUS-007", "IPK-001"})),
        (_is_error_code_47(blob), "STATUS-006",
         frozenset({"STATUS-005", "STATUS-008", "STATUS-010", "GEN-002", "TBOX-001"})),
        (_is_network_unavailable_toast(blob), "STATUS-003",
         frozenset({
             "BIND-008", "BIND-001", "APP-GUIDE-001", "GEN-005", "REG-001",
             "GEN-001", "GEN-002",
         })),
        # 2026-10-09：选模板不看区域。国家栏 /「本国不支持」都不进 REG-001。
        (_is_bind_ask_before_region_confirm(blob), "GEN-001",
         frozenset({
             "REG-001", "GEN-002", "MAN-001", "CHG-001", "CHG-002",
             "APP-GUIDE-001", "STATUS-003",
         })),
        (_is_qr_unreadable(blob), "BIND-008",
         frozenset({"BIND-001", "APP-GUIDE-001", "GEN-001", "GEN-002", "REG-001"})),
        (_is_colour_code_ask(blob), "SVC-004",
         frozenset({"APP-BODYIMG-001", "SVC-003", "GEN-002"})),
        (_is_fuel_economy_complaint(blob), "NON-009",
         frozenset({"NON-005", "APP-ENERGY-001", "GEN-002"})),
        (_is_spec_feature_missing(blob), "DK-003",
         frozenset({
             "APP-DIGITAL KEY-001", "APP-DIGITAL KEY-002", "STATUS-004",
             "FICM-VOICE-001", "GEN-002",
         })),
        (_is_battery_capacity_mismatch(blob), "STATUS-012",
         frozenset({
             "APP-BODYIMG-001", "FICM-PRIV-002", "FICM-PRIV-003", "GEN-002",
             "NON-005", "DK-004",
         })),
        (_is_app_mileage_howto(blob), "DK-004",
         frozenset({"IPK-001", "APP-ENERGY-001", "GEN-002"})),
        (_is_nav_language_howto(blob), "MAP-005",
         frozenset({"MAP-001", "FICM-VOICE-001", "GEN-002"})),
        (_is_map_online_update_failed(blob), "MAP-002",
         frozenset({
             "FOTA-001", "MAP-004", "MAP-005", "FICM-001", "STATUS-007",
             "GEN-001", "GEN-002",
         })),
        (_is_third_party_home_charger_fault(blob), "CHG-001",
         frozenset({
             "STATUS-012", "STATUS-013", "STATUS-009", "CHG-SCHED-001",
             "NEW-CHGPOWER-001", "GEN-002",
         })),
        (_is_app_says_range_drop(blob), "STATUS-012",
         frozenset({
             "CHG-002", "CHG-001", "STATUS-013", "CHG-SCHED-001", "MAN-001",
             "GEN-002", "NON-006",
         })),
        (_is_app_blocks_charging(blob), "STATUS-012",
         frozenset({
             "CHG-001", "CHG-002", "STATUS-013", "CHG-SCHED-001", "MAN-001",
             "GEN-002", "NON-006",
         })),
        (_is_non_ismart_hardware_fault(blob), "NON-001",
         frozenset({"CHG-SCHED-001", "CHG-001", "STATUS-012", "MAN-001", "GEN-002",
                    "APP-NOTIFICATION-001", "IPK-001"})),
        (_is_infotainment_language_request(blob), "REQ-001",
         frozenset({"FICM-001", "FOTA-001", "MAP-001", "MAP-005", "GEN-002"})),
        (_is_charge_target_availability(blob), "CHG-SCHED-001",
         frozenset({"STATUS-013", "STATUS-012", "CHG-001", "GEN-002"})),
        (_is_charge_target_mismatch(blob), "STATUS-013",
         frozenset({"STATUS-012", "STATUS-009", "CHG-001", "CHG-SCHED-001", "GEN-002"})),
        (_is_charge_schedule_fail(blob), "STATUS-012",
         frozenset({"CHG-SCHED-001", "CHG-001", "GEN-002"})),
        (_is_charge_mgmt_fault(blob), "STATUS-012",
         frozenset({"STATUS-007", "STATUS-009", "STATUS-013", "CHG-001", "CHG-SCHED-001", "GEN-002"})),
        (_is_app_tpms_alarm(blob), "APP-NOTIFICATION-001",
         frozenset({"TYRE-001", "STATUS-009", "IPK-001", "NON-005"})),
        (_is_vehicle_hotspot_password(blob), "NON-005",
         frozenset({"NET-001", "NET-PLAN-001"})),
        (_is_charging_schedule_howto(blob), "CHG-SCHED-001",
         frozenset({"CHG-001", "GEN-002"})),
        (_is_charging_power_howto(blob), "NEW-CHGPOWER-001",
         frozenset({"CHG-001", "NON-006"})),
        (_is_charging_alert_request(blob), "REQ-002",
         frozenset({"CHG-001", "STATUS-009"})),
        (_is_battery_config_lookup(blob), "NON-005",
         frozenset({
             "FICM-PRIV-002", "FICM-PRIV-003", "GEN-002", "STATUS-012",
         })),
        (_is_forgot_password(blob), "ACC-002",
         frozenset({"GEN-001", "GEN-002", "BIND-001", "ACC-005"})),
        (_is_privacy_notice_loop(blob), "FICM-PRIV-003",
         frozenset({"GEN-001", "GEN-005", "STATUS-002", "STATUS-003", "NET-001", "APP-GUIDE-001", "FICM-PRIV-002"})),
        (_is_ismart_plan_or_3g(blob), "",
         frozenset({"NET-001", "NET-PLAN-001", "GEN-001", "GEN-005"})),
        # 冒用/非授权 → 转人工；禁补信息 GEN-001、前车主 BIND-001
        (_is_fraud_force_unbind(blob), "",
         frozenset({
             "GEN-001", "GEN-002", "GEN-005", "BIND-001", "BIND-007",
             "ACC-009", "APP-GUIDE-001", "APP-GUIDE-002",
         })),
        (_is_maintenance_history_ask(blob), "SVC-001",
         frozenset({"NEW-SERVICE-001", "GEN-001", "GEN-002", "NON-005"})),
        (_is_engine_emission_ipk(blob), "NON-004",
         frozenset({"NON-005", "NON-001", "IPK-001", "STATUS-001", "APP-NOTIFICATION-001", "GEN-004"})),
        (_is_app_not_responding(blob), "STATUS-003",
         frozenset({"GEN-004", "GEN-001", "GEN-005", "NON-009", "STATUS-002"})),
        (_is_remote_ui_stuck_updating(blob), "STATUS-003",
         frozenset({
             "GEN-001", "GEN-002", "GEN-004", "GEN-005", "FOTA-001",
             "MAP-002", "STATUS-001", "STATUS-002",
         })),
        (_is_prev_owner_blocks_map_update(blob), "BIND-001",
         frozenset({"GEN-002", "GEN-001", "MAP-002", "MAP-006", "BIND-007"})),
        (_is_vpn_restriction(blob), "APP-VPN-001",
         frozenset({"NET-001"})),
        (_is_nav_voice_language(blob), "MAP-001",
         frozenset({"FICM-VOICE-001", "GEN-002"})),
        (_is_map_quality_feedback(blob), "MAP-006",
         frozenset({"FICM-001", "FOTA-001", "GEN-002", "MAP-002", "MAP-003", "MAP-004"})),
        (_is_hello_mg_phone_usb_voice(blob), "FICM-VOICE-001",
         frozenset({
             "CPAA-001", "DK-003", "MAN-001", "FICM-004", "FICM-VOLUME-001",
             "GEN-001", "GEN-002",
         })),
        (_is_aa_infotainment_fault(blob), "FICM-004",
         frozenset({"CPAA-001", "GEN-002", "ADAS-LKA-001", "FICM-003"})),
        (_is_tbox_replaced(blob), "TBOX-001",
         frozenset({
             "STATUS-003", "STATUS-002", "GEN-002", "APP-GUIDE-001", "GEN-005",
         })),
        (_is_backend_telematics_outage(blob), "GEN-005",
         frozenset({
             "APP-DIGITAL KEY-001", "APP-DIGITAL KEY-002", "KEY-001", "KEY-003",
             "STATUS-002", "TBOX-001", "GEN-002",
         })),
        (_is_vehicle_not_responded(blob), "STATUS-002",
         frozenset({"TBOX-001", "GEN-002", "NET-001"})),
        (_is_body_image_mismatch(blob), "APP-BODYIMG-001",
         frozenset({"GEN-002", "ACC-004"})),
        (_is_energy_stats_inquiry(blob), "APP-ENERGY-001",
         frozenset({"GEN-002", "CHG-001"})),
        (_is_parts_purchase(blob), "SVC-003",
         frozenset({"GEN-002", "IPK-001"}) | _INSPECTION_LIKE_IDS),
        (_is_aftermarket_accessory_ask(blob)
         and not _is_infotainment_update_consult(blob), "NON-010",
         frozenset({
             "FICM-001", "CPAA-001", "GEN-002", "NON-001", "NON-005",
             "SVC-003", "CHG-001",
         })),
        (_is_secondary_cs_unlink(blob), "ACC-009",
         frozenset({"ACC-007", "APP-GUIDE-002", "APP-GUIDE-003", "BIND-007", "GEN-002"})),
        (_is_secondary_not_registered(blob), "APP-GUIDE-003",
         frozenset({"GEN-005", "APP-GUIDE-002", "ACC-009"})),
        (_is_secondary_account_howto(blob), "APP-GUIDE-002",
         frozenset({"GEN-005", "APP-GUIDE-003", "ACC-009"})),
        (_is_change_registered_email(blob), "ACC-005",
         frozenset({"BIND-001", "ACC-008", "GEN-002", "APP-GUIDE-001"})),
        (_is_other_device_login(blob), "ACC-001",
         frozenset({"ACC-007", "GEN-002"})),
        (_is_new_phone_or_cancel_unbind(blob), "ACC-005",
         frozenset({"GEN-002", "BIND-002"})),
        (_is_self_unbind_rebind(blob), "BIND-007",
         frozenset({"BIND-001", "BIND-006", "GEN-002"})),
        (_is_key_left_in_boot(blob), "NON-007",
         frozenset({"GEN-002", "KEY-002", "STATUS-003", "APP-GUIDE-001"})),
        (_is_connect_pin_code(blob), "ACC-004",
         frozenset({
             "APP-GUIDE-001", "BIND-003", "BIND-001", "GEN-002", "ACC-003",
         })),
        (_is_vehicle_status_stale(blob), "STATUS-003",
         frozenset({"GEN-002", "GEN-004", "FOTA-001", "APP-GUIDE-001"})),
        (_is_infotainment_reboot_loop(blob), "FICM-REBOOT-001",
         frozenset({"FICM-003", "GEN-004", "GEN-002", "CPAA-001"})),
        (_is_previous_owner_bind(blob), "BIND-001",
         frozenset({"GEN-002"})),
        (_is_app_bind_howto(blob), "APP-GUIDE-001",
         frozenset({"BT-001", "REG-001"})),
        (_is_app_car_connect_fault(blob), "STATUS-003",
         frozenset({
             "BIND-006", "BIND-001", "BIND-003", "BIND-007", "BIND-008",
             "REG-001", "APP-GUIDE-001", "GEN-002", "GEN-003", "GEN-005",
             "TBOX-001",
         })),
        (_is_in_car_internet_fault(blob), "STATUS-003",
         frozenset({"TBOX-001", "GEN-005", "GEN-002", "BIND-006"})),
        (_is_vehicle_mgmt_howto(blob), "BIND-007",
         frozenset({"GEN-002", "STATUS-003", "BIND-001", "BIND-006"})),
        (_is_digital_key_status_notice(blob), "KEY-003",
         frozenset({"KEY-001", "GEN-004", "APP-DIGITAL KEY-001", "APP-DIGITAL KEY-002"})),
        (_is_digital_key_howto(blob), "APP-DIGITAL KEY-001",
         frozenset({"KEY-001", "GEN-002", "GEN-004", "APP-DIGITAL KEY-002"})),
        (_is_digital_key_availability(blob), "APP-DIGITAL KEY-001",
         frozenset({"KEY-001", "GEN-002", "APP-DIGITAL KEY-002"})),
        (_is_manual_pdf_request(blob), "MAN-002",
         frozenset({"GEN-002", "MAN-001"})),
        (_is_manual_wrong_model(blob), "MAN-001",
         frozenset({"GEN-002", "GEN-001", "MAN-002", "CHG-001"})),
        (_is_manual_find_request(blob), "MAN-001",
         frozenset({"GEN-002"})),
        (_is_address_change_request(blob), "ACC-006",
         frozenset({"GEN-002", "NET-001"})),
        (_is_paint_warranty(blob), "SVC-003",
         _INSPECTION_LIKE_IDS | {"CHG-001", "GEN-002"}),
        (_is_tyre_pressure_consult(blob), "TYRE-001",
         _INSPECTION_LIKE_IDS | {"GEN-002", "SVC-003"}),
        (_is_powertrain_roadside(blob), "SVC-PWR-001",
         _INSPECTION_LIKE_IDS | {"GEN-002", "SVC-001"}),
        (_is_service_consult(blob), "SVC-001",
         _INSPECTION_LIKE_IDS | {"GEN-002", "NEW-SERVICE-001", "FICM-PRIV-002"}),
        (_is_cruise_howto(blob), "CRUISE-001",
         frozenset({"ADAS-ACC-001", "NON-003", "IPK-001", "SVC-001"})),
        (_is_infotainment_update_consult(blob), "FICM-001",
         frozenset({"CPAA-001", "GEN-002", "SVC-001"})),
        (_is_fota_when_or_connect(blob), "FOTA-001",
         frozenset({"GEN-002", "GEN-001"})),
        (_is_vague_app_complaint(blob), "COMP-001",
         frozenset({"REQ-001", "REQ-002", "GEN-001", "GEN-002", "GEN-004"})),
        (_is_app_gps_background(blob), "APP-SW-001",
         frozenset({"GEN-002"})),
    ]
    for hit, tid, banned in checks:
        if hit:
            return tid, banned
    return None, frozenset()


_POST_UPDATE_RE = re.compile(
    r"((after|since|following).{0,50}(software\s+)?(update|upgrade|ota|fota)"
    r"|(update|upgrade|ota).{0,30}(ago|later)"
    r"|升级后|更新后)",
    re.I | re.S,
)
_ADAS_FAULT_SYMPTOM_RE = re.compile(
    r"(not\s+work|doesn'?t\s+work|warning|fault|fail|drift"
    r"|pulls?\s+(to\s+the\s+)?(left|right)|broken|error\s*(code)?"
    r"|still\s+(has|have|does)|无法|报警|跑偏)",
    re.I,
)
# 生产路径先 redact 再 pick_template：17 位 VIN 已变成 VIN_MASK_<8 hex>。
# 闸门要认这个占位符，否则几乎所有「要 VIN」的行都会误打 GEN-002。
_VIN_MASK_RE = re.compile(r"\bVIN_MASK_[0-9a-f]{8}\b", re.I)
_VIN_MISSING_RE = re.compile(
    r"(where\s+(is|can\s+i\s+find|to\s+find)\s+(the\s+)?vin"
    r"|cannot\s+find\s+(the\s+)?vin"
    r"|vin\s+missing"
    r"|no\s+vin"
    r"|don'?t\s+have.{0,24}vin"
    r"|chassis\s+number)",
    re.I,
)
_VIN_INVALID_RE = re.compile(
    r"(wrong\s+vin|vin\s+(is\s+)?(incorrect|invalid|not\s+found)"
    r"|不是\s*17\s*位|查询不到|chassis\s+invalid)",
    re.I,
)
_VAGUE_GENERIC_RE = re.compile(
    r"(please\s+help|don'?t\s+know\s+what|no\s+details|"
    r"have\s+a\s+problem|have\s+an\s+issue|not\s+working)",
    re.I,
)
_VAGUE_SPECIFIC_RE = re.compile(
    r"\b(bind|unbind|qr\b|fota|\bota\b|t-?box|bluetooth|login|password|"
    r"digital\s+key|charg|map\b|navigation|privacy|error\s*\d+|"
    r"previous\s+owner|telematics|region\s+not\s+supported)\b",
    re.I,
)
_INFO_NEEDS_VIN = frozenset({"basic_vehicle", "binding", "avn_activation"})
_SOURCE_TYPE_WEIGHT = {
    "明确规则": 1.0,
    "过往案例归纳": 0.85,
    "规则补全需人工复核": 0.55,
    "v5_新增_chg_sched": 1.0,
}


def _template_by_id(templates: list[dict], template_id: str) -> Optional[dict]:
    for t in templates:
        if t.get("template_id") == template_id:
            return t
    return None


def _body_has_vin(body_text: str) -> bool:
    text = body_text or ""
    return bool(_VIN_RE.search(text) or _VIN_MASK_RE.search(text))


def _pick_evidence(
    body_text: str,
    image_descriptions: list[str] | None = None,
    thread_context: str | None = None,
) -> str:
    """正文 + 附件抽取 + 线程。VIN 闸门必须看到 Word/PDF 里的 VIN_MASK。"""
    parts = [body_text or ""]
    if image_descriptions:
        parts.extend(image_descriptions)
    if thread_context:
        parts.append(thread_context)
    return "\n".join(parts)


_QUOTE_CUT_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\n_{5,}"),
    re.compile(r"\n[-_]{5,}\s*Original Message", re.I),
    re.compile(r"\nFrom:\s", re.I),
    re.compile(r"\n发件人\s*[：:]", re.I),
    re.compile(r"\nOn .+ wrote:", re.I),
    re.compile(r"\nEl .+ escribi[oó]:", re.I),
)


def _latest_customer_turn(body_text: str) -> str:
    """切掉 Outlook 引用，只留客户最新一段。未切到引用且过短才退回全文。"""
    text = body_text or ""
    if not text.strip():
        return text
    cut = len(text)
    for pat in _QUOTE_CUT_RES:
        m = pat.search(text)
        if m and m.start() >= 12 and m.start() < cut:
            cut = m.start()
    latest = text[:cut].strip()
    if len(latest) < 24 and cut >= len(text):
        latest = text
    # 网页表单页脚 / Issue Type 下拉不是主诉；选行和 LLM 都只看剥完后的段。
    cleaned = _webform_score_text(latest)
    return cleaned if len(cleaned) >= 24 else latest


def _is_post_update_fault(latest_text: str) -> bool:
    """升级之后仍有故障症状 → 仍走检查/故障行。"""
    latest = latest_text or ""
    return bool(_POST_UPDATE_RE.search(latest) and _ADAS_FAULT_SYMPTOM_RE.search(latest))


def _is_update_scope_consult(latest_text: str) -> bool:
    """最新一段：在问当前/即将软件升级会不会也覆盖某功能。

    必须同时有升级信号 + 范围问句。功能名（LKA 等）单独不够。
    升级后故障不算咨询。
    """
    latest = latest_text or ""
    if not _UPDATE_SIGNAL_RE.search(latest) or not _UPDATE_SCOPE_Q_RE.search(latest):
        return False
    if _is_post_update_fault(latest):
        return False
    if _is_map_online_update_failed(latest):
        return False
    return True


def _pick_update_scope_template(templates: list[dict], latest_text: str) -> Optional[dict]:
    blob = (latest_text or "").lower()
    prefer_fota = bool(re.search(r"\bota\b|\bfota\b|over[\s-]?the[\s-]?air", blob))
    order = ("FOTA-001", "FICM-001", "GEN-005") if prefer_fota else (
        "FICM-001", "FOTA-001", "GEN-005",
    )
    for tid in order:
        hit = _template_by_id(templates, tid)
        if hit:
            return hit
    return None


def _is_vague_request(latest_text: str, evidence: str | None = None) -> bool:
    """笼统闸门看最新客户一句；VIN / 具体故障词看全文（含线程、附件）。"""
    latest = latest_text or ""
    blob = evidence if evidence is not None else latest
    if not latest or not _VAGUE_GENERIC_RE.search(latest):
        return False
    if _VAGUE_SPECIFIC_RE.search(blob):
        return False
    if _body_has_vin(blob):
        return False
    return True


def _source_weight(template: dict) -> float:
    src = (template.get("source_type") or "").strip()
    return _SOURCE_TYPE_WEIGHT.get(src, 1.0)


_URGENT_BODY_RE = re.compile(
    r"\b(urgent|emergency|stuck|stranded|locked\s+inside|asap)\b|紧急|无法启动|困在",
    re.I,
)


def human_trigger_should_escalate(
    trigger: str,
    body: str,
    urgency: str = "P3",
) -> bool:
    """xlsx「人工介入条件」命中则不要落草稿（转人工）。

    只做高置信硬信号，避免把中文条件整段当正则。
    """
    if not (trigger or "").strip():
        return False
    if "紧急" in trigger and _URGENT_BODY_RE.search(body or ""):
        return True
    if urgency == "P0" and any(k in trigger for k in ("律师", "法律", "媒体", "投诉")):
        return True
    return False


def _apply_row_policy(template: dict) -> dict:
    """命中行上的来源类型可把 safe 升到 review；不改缓存里的原 row。"""
    if (
        template.get("source_type") == "规则补全需人工复核"
        and template.get("level") == "auto_draft_safe"
    ):
        out = dict(template)
        out["level"] = "auto_draft_review"
        return out
    return template


def _score_template(template: dict, body_text: str, triage_intent: str) -> float:
    score, _ = _enriched_keyword_score(template, body_text)
    if score <= 0:
        return 0.0
    score *= _source_weight(template)
    cat = str(template.get("category") or "") + str(template.get("l1_category") or "")
    if "新需求" in cat:
        score *= 0.5
    if triage_intent and template.get("intent") == triage_intent:
        score += 2.0
    tid = str(template.get("template_id") or "")
    if tid in _RETIRED_TEMPLATE_IDS or tid == "REG-001":
        return 0.0
    if tid == "CHG-SCHED-001" and not _is_charging_schedule_howto(body_text):
        return 0.0
    if tid == "FICM-001" and _is_infotainment_language_request(body_text):
        return 0.0
    if tid == "FICM-001" and _is_aftermarket_accessory_ask(body_text) and not (
        _is_infotainment_update_consult(body_text)
    ):
        return 0.0
    if tid == "NON-010" and not _is_aftermarket_accessory_ask(body_text):
        return 0.0
    if tid == "MAN-001" and _is_accessory_battery_fault(body_text):
        return 0.0
    if tid in {"GEN-001", "GEN-002"} and _body_has_vin(body_text):
        return 0.0
    if tid in {"FICM-PRIV-002", "FICM-PRIV-003"} and not _is_privacy_notice_loop(body_text):
        return 0.0
    if tid == "ACC-006" and _is_address_change_request(body_text):
        sc = str(template.get("scenario") or "")
        if "地址" not in sc and "change" not in sc.lower():
            return 0.0
    if tid in {"STATUS-012", "STATUS-013"} and _is_third_party_home_charger_fault(body_text):
        return 0.0
    if tid == "BIND-008" and not _is_qr_unreadable(body_text):
        return 0.0
    if tid == "COMP-001" and not _is_vague_app_complaint(body_text):
        return 0.0
    if tid == "BIND-007" and not (
        _is_vehicle_mgmt_howto(body_text) or _is_self_unbind_rebind(body_text)
    ):
        return 0.0
    if tid == "CPAA-001" and _is_hello_mg_phone_usb_voice(body_text):
        return 0.0
    if tid == "GEN-001" and _VAGUE_SPECIFIC_RE.search(body_text or ""):
        return 0.0
    latest = _latest_customer_turn(body_text)
    if tid in {"APP-DIGITAL KEY-001", "APP-DIGITAL KEY-002", "KEY-001", "KEY-003"}:
        if _INCIDENTAL_BT_KEY_RE.search(latest) and not re.search(
            r"digital\s+key|数字钥匙", latest, re.I,
        ):
            return 0.0
    _force, banned = _review_lock(latest)
    if tid in banned:
        return 0.0
    if tid == "SVC-001" and _SEAT_MEMORY_FAULT_RE.search(latest):
        return 0.0
    if tid in _INSPECTION_TEMPLATE_IDS and _is_update_scope_consult(
        _latest_customer_turn(body_text)
    ):
        return 0.0
    if tid == "NET-001" and _is_vpn_restriction(_latest_customer_turn(body_text)):
        return 0.0
    if tid in _CPAA_HOWTO_IDS and (
        _is_headunit_display_fault(latest)
        or _is_infotainment_update_consult(latest)
    ):
        return 0.0
    if tid == "TBOX-001" and not _is_tbox_replaced(latest):
        return 0.0
    if tid == "GEN-005":
        if _is_backend_telematics_outage(latest):
            pass
        elif (
            _blocks_backend_analyze(latest)
            or _is_secondary_account_howto(latest)
            or _is_secondary_not_registered(latest)
            or triage_intent != "follow_up_resolved"
        ):
            return 0.0
    if tid == "APP-GUIDE-002" and _is_secondary_not_registered(
        _latest_customer_turn(body_text)
    ):
        return 0.0
    if tid == "APP-GUIDE-003" and _is_secondary_account_howto(
        _latest_customer_turn(body_text)
    ):
        return 0.0
    return score


_LLM_CANDIDATE_RECALL = 12
_LLM_CANDIDATE_MAX = 28


def _llm_template_candidates(
    triage_intent: str, templates: list[dict], body_text: str,
) -> list[dict]:
    """召回菜单给 GPT：同 Intent + 全表关键词高分 + 硬闸目标行。

    Intent 不锁死菜单。关键词只负责把可能对的行放进候选，由语义选中。
    """
    by_id: dict[str, dict] = {}
    for t in templates:
        tid = str(t.get("template_id") or "")
        if t.get("intent") == triage_intent and tid:
            by_id[tid] = t
    extras: list[str] = ["GEN-003"]
    latest = _latest_customer_turn(body_text)
    if _is_backend_telematics_outage(latest):
        extras.append("GEN-005")
    elif (
        _body_has_vin(body_text)
        and triage_intent == "follow_up_resolved"
        and not _blocks_backend_analyze(latest)
        and not (
            _is_secondary_account_howto(latest)
            or _is_secondary_not_registered(latest)
        )
    ):
        extras.append("GEN-005")
    elif not _body_has_vin(body_text):
        extras.extend(["GEN-001", "GEN-002"])
    blob = (body_text or "").lower()
    extra_intents: list[str] = []
    if triage_intent == "follow_up_resolved":
        if re.search(r"\bqr\b|bind my|cannot bind|绑车|二维码", blob):
            extra_intents.append("app_binding")
        if re.search(r"offline|standby|not visible|t-?box|telematics", blob):
            extra_intents.append("app_vehicle_control")
    if _UPDATE_SIGNAL_RE.search(latest) and not _is_post_update_fault(latest):
        extras.extend(["FICM-001", "FOTA-001"])
    if _is_update_scope_consult(latest):
        extras.extend(["FICM-001", "FOTA-001"])
    if _is_infotainment_update_consult(latest):
        extras.append("FICM-001")
    if _is_aa_infotainment_fault(latest):
        extras.append("FICM-004")
        by_id.pop("CPAA-001", None)
    elif _is_infotainment_reboot_loop(latest):
        extras.append("FICM-REBOOT-001")
        by_id.pop("FICM-003", None)
    elif _is_headunit_display_fault(latest):
        extras.append("FICM-003")
    if _is_map_quality_feedback(latest):
        extras.append("MAP-006")
        by_id.pop("FICM-001", None)
    if _is_secondary_not_registered(latest):
        extras.append("APP-GUIDE-003")
    elif _is_secondary_account_howto(latest):
        extras.append("APP-GUIDE-002")
    force_tid, banned = _review_lock(latest)
    if force_tid:
        extras.append(force_tid)
    elif _RADAR_ADAS_RE.search(body_text or ""):
        extra_intents.append("adas_acc")
        extras.append("NON-003")
    for intent in extra_intents:
        for t in templates:
            tid = str(t.get("template_id") or "")
            if t.get("intent") == intent and tid and not tid.startswith("GEN-"):
                by_id[tid] = t
    for tid in extras:
        row = _template_by_id(templates, tid)
        if row and row.get("template_id"):
            by_id[str(row["template_id"])] = row
    if (body_text or "").strip():
        ranked: list[tuple[float, dict]] = []
        for t in templates:
            tid = str(t.get("template_id") or "")
            if not tid:
                continue
            s = _score_template(t, body_text, triage_intent)
            if s > 0:
                ranked.append((s, t))
        ranked.sort(key=lambda x: x[0], reverse=True)
        recalled = 0
        for _s, t in ranked:
            tid = str(t.get("template_id") or "")
            if tid.startswith("GEN-"):
                continue
            if tid not in by_id:
                by_id[tid] = t
                recalled += 1
            if recalled >= _LLM_CANDIDATE_RECALL:
                break
    if _is_update_scope_consult(latest):
        for banned_id in _INSPECTION_TEMPLATE_IDS:
            by_id.pop(banned_id, None)
    if _is_vpn_restriction(latest):
        by_id.pop("NET-001", None)
    if _is_headunit_display_fault(latest):
        for banned_id in _CPAA_HOWTO_IDS:
            by_id.pop(banned_id, None)
    if _is_infotainment_update_consult(latest):
        by_id.pop("CPAA-001", None)
    if not _is_privacy_notice_loop(latest):
        by_id.pop("FICM-PRIV-002", None)
        by_id.pop("FICM-PRIV-003", None)
    if not _is_tbox_replaced(latest):
        by_id.pop("TBOX-001", None)
    if (
        _is_secondary_account_howto(latest)
        or _is_secondary_not_registered(latest)
        or _blocks_backend_analyze(latest)
        or (
            not _is_backend_telematics_outage(latest)
            and not (
                triage_intent == "follow_up_resolved"
                and not _blocks_backend_analyze(latest)
            )
        )
    ):
        by_id.pop("GEN-005", None)
    if banned:
        for bid in banned:
            by_id.pop(bid, None)
    for rid in _RETIRED_TEMPLATE_IDS:
        by_id.pop(rid, None)
    by_id.pop("REG-001", None)
    out = [t for t in by_id.values() if t.get("template_id")]
    if len(out) > _LLM_CANDIDATE_MAX:
        same = [t for t in out if t.get("intent") == triage_intent]
        extra = [t for t in out if t.get("intent") != triage_intent]
        out = (same + extra)[:_LLM_CANDIDATE_MAX]
    return out


def _format_template_menu(candidates: list[dict]) -> str:
    lines = []
    for t in candidates:
        tid = t.get("template_id") or ""
        l2 = (t.get("l2_category") or t.get("category") or "").strip()
        scen = (t.get("scenario_en") or t.get("scenario") or "").strip()
        if len(scen) > 220:
            scen = scen[:217] + "..."
        lines.append(f"- {tid}: [{l2}] {scen}")
    return "\n".join(lines)


def _llm_pick_template_id(
    *,
    llm,
    body_text: str,
    candidates: list[dict],
    triage_intent: str = "",
    image_descriptions: list[str] | None = None,
    thread_context: str | None = None,
) -> Optional[str]:
    if not llm or not candidates or not (body_text or "").strip():
        return None
    allowed = {str(t.get("template_id") or "") for t in candidates}
    allowed.discard("")
    from .prompts import TEMPLATE_PICK_SYSTEM

    latest = _latest_customer_turn(body_text)
    user = (
        "Triage intent (hint only, not a lock): "
        + (triage_intent or "(none)")
        + "\nCandidate templates (pick one template_id that matches the ASK):\n"
        + _format_template_menu(candidates)
        + "\n\n--- Latest customer turn (ignore quoted history below) ---\n"
        + (latest or "")[:4000]
    )
    if body_text and latest != body_text:
        user += (
            "\n\n--- Quoted thread (context only; empty form fields here "
            "are NOT missing if the latest turn already supplied them) ---\n"
            + body_text[:2500]
        )
    if image_descriptions:
        user += (
            "\n\n--- Attached screenshot descriptions ---\n"
            + "\n".join(image_descriptions)
        )
    if thread_context:
        user += (
            "\n\n--- Conversation history ---\n"
            + thread_context[:3000]
        )
    try:
        picked, _call = llm.invoke_json(
            schema=TemplatePick,
            messages=[
                {"role": "system", "content": TEMPLATE_PICK_SYSTEM},
                {"role": "user", "content": user},
            ],
            reasoning_effort="low",
            max_completion_tokens=1024,
            role="template_pick",
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("[template_pick] LLM failed: %s", e)
        return None
    tid = (picked.template_id or "").strip()
    if tid not in allowed:
        if (
            tid in {"GEN-001", "GEN-002"}
            and "GEN-005" in allowed
            and _body_has_vin(body_text)
            and triage_intent == "follow_up_resolved"
        ):
            return "GEN-005"
        return None
    return tid


def _apply_vin_post_gate(
    winner: Optional[dict],
    body_text: str,
    templates: list[dict],
    triage_intent: str = "",
) -> Optional[dict]:
    if winner is None:
        return None
    tid = str(winner.get("template_id") or "")
    if tid in {"GEN-001", "GEN-002"} and _body_has_vin(body_text):
        if triage_intent == "follow_up_resolved":
            return _template_by_id(templates, "GEN-005") or None
        return None
    req = set(winner.get("required_info") or [])
    needs_vin = bool(req & _INFO_NEEDS_VIN)
    is_gen = str(winner.get("template_id") or "").startswith("GEN-")
    force_tid, _ = _review_lock(_latest_customer_turn(body_text))
    if force_tid and tid == force_tid:
        return winner
    if (
        needs_vin
        and not is_gen
        and winner.get("level") not in {"no_auto_reply", "auto_draft_review"}
        and body_text
        and not _body_has_vin(body_text)
    ):
        return _template_by_id(templates, "GEN-002") or winner
    return _demote_unwarranted_gen005(winner, templates, body_text)


_IN_CAR_INTERNET_RE = re.compile(
    r"("
    r"internet\s+in\s+the\s+(car|vehicle|infotainment)"
    r"|(car|vehicle).{0,32}itself.{0,48}internet"
    r"|internet.{0,48}(car|vehicle).{0,24}itself"
    r"|(car|vehicle|infotainment|head.?unit).{0,48}"
    r"(no\s+internet|no\s+network|cannot\s+go\s+online|can'?t\s+go\s+online|"
    r"internet\s+does\s+not\s+work|network\s+does\s+not\s+work)"
    r"|internet\s+im\s+(auto|wagen|fahrzeug)"
    r"|(auto|fahrzeug).{0,32}(kein\s+internet|kein\s+netz)"
    r"|internet.{0,28}(nell[a']?\s+(auto|macchina)|dans\s+la\s+voiture)"
    r"|车机.{0,16}(没网|无法上网|不能上网|网络(不好|不行|不可用))"
    r")",
    re.I | re.S,
)
_CPAA_UNSTABLE_RE = re.compile(
    r"("
    r"jumps?\s+on\s+and\s+off"
    r"|springt\s+an\s+und\s+aus"
    r"|si\s+disconnette"
    r"|keeps?\s+(dropping|disconnect)"
    r"|does\s+not\s+work\s+optimally"
    r"|(carplay|android\s+auto).{0,48}"
    r"(not\s+work|unstable|intermittent|drops?|cuts?\s+out)"
    r")",
    re.I | re.S,
)
_ONLINE_SERVICE_NET001_RE = re.compile(
    r"("
    r"weather\s+(forecast|app|in\s+the\s+car)|wetter(vorhersage)?"
    r"|youtube|tiktok"
    r"|4g.{0,60}(unavailable|not work|network)"
    r"|network\s+unavailable"
    r"|hotspot\s+(not\s+found|not\s+work|kann\s+nicht)"
    r"|e-?sim.{0,48}(poor|reception|not|schlecht)"
    r"|online\s+services?\s+(unavailable|not\s+work)"
    r")",
    re.I | re.S,
)


def _is_in_car_internet_fault(text: str) -> bool:
    """车机自身没网 / 在线服务，可与 CarPlay 同封并存。"""
    latest = _latest_customer_turn(text or "")
    desc = _form_issue_description(latest)
    hay = _webform_score_text(latest)
    blob = f"{desc}\n{hay}"
    if _is_ismart_plan_or_3g(blob) or _is_vpn_restriction(blob):
        return False
    return bool(_IN_CAR_INTERNET_RE.search(blob) or _HU_NO_INTERNET_RE.search(blob))


def _is_cpaa_pair_issue(text: str) -> bool:
    """CarPlay / AA 断连或不稳，不是整屏熄灭、不是问有没有升级。"""
    latest = _latest_customer_turn(text or "")
    blob = f"{_form_issue_description(latest)}\n{_webform_score_text(latest)}"
    if not re.search(r"android\s+auto|carplay|apple\s+car\s*play", blob, re.I):
        return False
    if (
        _is_aa_infotainment_fault(blob)
        or _is_infotainment_update_consult(blob)
        or _is_headunit_display_fault(blob)
    ):
        return False
    if _is_hello_mg_phone_usb_voice(blob):
        return False
    return bool(_CPAA_PAIR_FAULT_RE.search(blob) or _CPAA_UNSTABLE_RE.search(blob))


def _is_app_car_connect_allow_cpaa(text: str) -> bool:
    """App 连不上车；合写时不要因为同封还有 CarPlay 就丢掉 STATUS-003。"""
    hay = _webform_score_text(text or "")
    desc = _form_issue_description(text or "")
    blob = f"{hay}\n{desc}"
    if _is_app_bind_howto(blob) or _is_ismart_plan_or_3g(blob):
        return False
    if _is_vehicle_mgmt_howto(text):
        return False
    return bool(_APP_CAR_CONNECT_FAULT_RE.search(blob))


def _is_online_service_net001(text: str) -> bool:
    """车机天气/YouTube/4G/热点/eSIM，不是 VPN、不是 3G 套餐。"""
    latest = _latest_customer_turn(text or "")
    blob = f"{_form_issue_description(latest)}\n{_webform_score_text(latest)}"
    if _is_vpn_restriction(blob) or _is_ismart_plan_or_3g(blob):
        return False
    return bool(_ONLINE_SERVICE_NET001_RE.search(blob))


def _blocks_backend_analyze(text: str) -> bool:
    """首封已能对上自动行时，不要因为表单里有 VIN 就 GEN-005 后台分析。"""
    blob = text or ""
    if _is_backend_telematics_outage(blob):
        return False
    return bool(
        _is_app_car_connect_fault(blob)
        or _is_app_car_connect_allow_cpaa(blob)
        or _is_app_bind_howto(blob)
        or _is_app_not_responding(blob)
        or _is_in_car_internet_fault(blob)
        or _is_online_service_net001(blob)
        or _is_secondary_account_howto(blob)
        or _is_secondary_not_registered(blob)
        or _is_forgot_password(blob)
    )


def _demote_unwarranted_gen005(
    winner: Optional[dict],
    templates: list[dict],
    body_text: str,
) -> Optional[dict]:
    if winner is None:
        return None
    tid = str(winner.get("template_id") or "")
    latest = _latest_customer_turn(body_text)
    if tid == "TBOX-001" and not _is_tbox_replaced(latest):
        force_tid, _banned = _review_lock(latest)
        if force_tid and force_tid != "TBOX-001":
            hit = _template_by_id(templates, force_tid)
            if hit:
                return hit
        merged = _try_merged_template(templates, latest)
        if merged:
            return merged
        return _template_by_id(templates, "STATUS-003") or winner
    if tid != "GEN-005":
        return winner
    if _is_backend_telematics_outage(latest):
        return winner
    if not _blocks_backend_analyze(latest):
        return winner
    force_tid, _banned = _review_lock(latest)
    if force_tid and force_tid != "GEN-005":
        hit = _template_by_id(templates, force_tid)
        if hit:
            return hit
    merged = _try_merged_template(templates, latest)
    if merged:
        return merged
    if _is_app_car_connect_allow_cpaa(latest) or _is_app_car_connect_fault(latest):
        return _template_by_id(templates, "STATUS-003") or winner
    if _is_app_bind_howto(latest):
        return _template_by_id(templates, "APP-GUIDE-001") or winner
    if _is_in_car_internet_fault(latest) or _is_online_service_net001(latest):
        return _template_by_id(templates, "NET-001") or winner
    return winner


_LANE_ADDR = {"tid": "ACC-006", "label": "the address request", "body": ""}
_LANE_STATUS = {
    "tid": "STATUS-003",
    "label": "",
    "body": (
        "Please first move the vehicle to an open area with stable "
        "network coverage, then restart the infotainment system and "
        "try again. You may also refresh the vehicle status in the "
        "iSMART App after the vehicle has reconnected."
    ),
}
_LANE_NET = {"tid": "NET-001", "label": "online services", "body": ""}
_LANE_FICM = {"tid": "FICM-001", "label": "software update", "body": ""}
_LANE_ACCESSORY = {
    "tid": "NON-010",
    "label": "aftermarket accessories",
    "body": (
        "Regarding aftermarket / third-party accessories, we are unable to "
        "advise whether they can be installed on the vehicle. Please contact "
        "the accessory manufacturer or their official customer support team "
        "directly for compatibility and installation guidance."
    ),
}
_LANE_FICM_WIRELESS_AA = {
    "tid": "FICM-001",
    "label": "wireless Android Auto",
    "body": (
        "Regarding wireless Android Auto, the availability of this feature "
        "depends on the specific vehicle model and configuration. For "
        "information on whether a software update supporting wireless "
        "Android Auto is available for your vehicle, please consult your "
        "local MG dealer."
    ),
}
_LANE_CPAA = {
    "tid": "CPAA-001",
    "label": "CarPlay",
    "body": (
        "For CarPlay, please delete the existing CarPlay/Android Auto, "
        "Bluetooth, and Wi-Fi pairing records from both the phone and "
        "the vehicle, then restart both devices and reconnect.\n\n"
        "For wired connection, please use an original or certified USB cable."
    ),
}


_LANE_FORGOT = {"tid": "ACC-002", "label": "password recovery", "body": ""}
_LANE_DELETE_ACCOUNT = {"tid": "ACC-007", "label": "account deletion", "body": ""}
_LANE_OTHER_DEVICE = {"tid": "ACC-001", "label": "another device", "body": ""}
_LANE_DK_HOWTO = {"tid": "APP-DIGITAL KEY-001", "label": "digital key", "body": ""}
_LANE_SUB_HOWTO = {"tid": "APP-GUIDE-002", "label": "secondary account", "body": ""}
_LANE_VEHICLE_MGMT = {"tid": "BIND-007", "label": "vehicle management", "body": ""}
_LANE_BIND_PDF = {"tid": "APP-GUIDE-001", "label": "binding the vehicle", "body": ""}


def detect_issue_lanes(text: str) -> list[dict]:
    """独立主诉车道。≥2 条才合写。远联/在线服务只留一条（STATUS-003 优先于 NET-001）。"""
    blob = text or ""
    out: list[dict] = []

    def _add(lane: dict) -> None:
        if any(item.get("tid") == lane.get("tid") for item in out):
            return
        out.append(dict(lane))

    if _is_forgot_password(blob) and _is_sold_delete_account(blob):
        _add(_LANE_FORGOT)
        _add(_LANE_DELETE_ACCOUNT)
    if _is_other_device_login(blob) and _is_sold_delete_account(blob):
        _add(_LANE_OTHER_DEVICE)
        _add(_LANE_DELETE_ACCOUNT)
    if _is_digital_key_howto(blob) and _is_secondary_account_howto(blob):
        _add(_LANE_DK_HOWTO)
        _add(_LANE_SUB_HOWTO)
    if _is_explicit_en_rebind(blob):
        _add(_LANE_VEHICLE_MGMT)
        _add(_LANE_BIND_PDF)
    if _is_address_change_request(blob):
        out.append(dict(_LANE_ADDR))
    if _is_in_car_internet_fault(blob) or _is_app_car_connect_allow_cpaa(blob):
        out.append(dict(_LANE_STATUS))
    elif _is_online_service_net001(blob):
        out.append(dict(_LANE_NET))
    if _is_cpaa_pair_issue(blob):
        out.append(dict(_LANE_CPAA))
    if _is_aftermarket_accessory_ask(blob):
        out.append(dict(_LANE_ACCESSORY))
    if _is_infotainment_update_consult(blob):
        if _is_wireless_aa_update_consult(blob):
            out.append(dict(_LANE_FICM_WIRELESS_AA))
        else:
            out.append(dict(_LANE_FICM))
    return out


def _strip_template_shell(text: str) -> str:
    blob = (text or "").replace("{{salutation}},", "").replace("{{salutation}}", "")
    blob = re.sub(r"^Thank you for your email\.?\s*", "", blob.strip(), flags=re.I)
    blob = re.sub(
        r"\n*(Best regards|Kind regards|Thank you for your cooperation)"
        r"[\s\S]*$",
        "",
        blob,
        flags=re.I,
    )
    blob = re.sub(r"\n*iSMART Service Team\s*$", "", blob, flags=re.I)
    return blob.strip()


def _acc006_handles_address(templates: list[dict]) -> bool:
    t = _template_by_id(templates, "ACC-006")
    if not t:
        return False
    blob = f"{t.get('scenario') or ''} {t.get('template') or ''}"
    return "地址" in blob or "unable to process address" in blob.lower()


def compose_merged_template(
    templates: list[dict], lanes: list[dict],
) -> Optional[dict]:
    if len(lanes) < 2:
        return None
    rows: list[dict] = []
    kept: list[dict] = []
    levels: list[str] = []
    for lane in lanes:
        row = _template_by_id(templates, lane["tid"])
        if not row:
            continue
        if lane["tid"] == "ACC-006" and not _acc006_handles_address(templates):
            continue
        applied = _apply_row_policy(row)
        lv = str(applied.get("level") or "")
        if lv == "no_auto_reply":
            continue
        rows.append(applied)
        kept.append(lane)
        levels.append(lv)
    if len(rows) == 1:
        return rows[0]
    if len(rows) < 2:
        return None
    parts: list[str] = []
    for i, lane in enumerate(kept):
        chunk = (lane.get("body") or "").strip()
        if not chunk:
            chunk = _strip_template_shell(rows[i].get("template") or "")
            label = (lane.get("label") or "").strip()
            if i > 0 and label and chunk and not chunk.lower().startswith("for "):
                rest = chunk[0].lower() + chunk[1:] if chunk[:1].isupper() else chunk
                chunk = f"For {label}, {rest}"
        if chunk:
            parts.append(chunk)
    if len(parts) < 2:
        return rows[0] if rows else None
    if "auto_draft_review" in levels:
        level = "auto_draft_review"
    elif "auto_draft_needs_info" in levels:
        level = "auto_draft_needs_info"
    else:
        level = "auto_draft_safe"
    ids = [str(lane["tid"]) for lane in kept]
    out = dict(rows[0])
    out["template_id"] = "+".join(ids)
    out["merged_ids"] = ids
    out["source_type"] = "merged_lanes"
    out["level"] = level
    out["intent"] = rows[0].get("intent")
    out["scenario_en"] = "Merged: " + "; ".join(
        (r.get("scenario_en") or r.get("scenario") or ids[i])[:180]
        for i, r in enumerate(rows)
    )
    out["template"] = (
        "{{salutation}},\n\n"
        "Thank you for your email.\n\n"
        + "\n\n".join(parts)
        + "\n\nThank you for your cooperation.\n\n"
        "iSMART Service Team\n"
    )
    return out


def _try_merged_template(templates: list[dict], text: str) -> Optional[dict]:
    return compose_merged_template(templates, detect_issue_lanes(text))


AMERICAS_GRAY_TEMPLATE_ID = "AMER-001"

AMERICAS_GRAY_BODY = """\
Dear Customer,

Thank you for your email.

After checking the relevant vehicle information, we regret to inform you that iSMART connected services are currently not supported for this vehicle in your region.

For further information regarding iSMART vehicle binding and regional service availability, we kindly recommend contacting your local MG dealer, who will be able to provide more detailed guidance based on your vehicle specification and market.

We apologize for any inconvenience this may cause and appreciate your understanding.

Best regards,
iSMART Service Team
"""


def americas_gray_market_row() -> dict:
    """对照表美洲表单国家：固定正文，建稿后仍保留转人工。不进知识库 REG-001。"""
    return {
        "template_id": AMERICAS_GRAY_TEMPLATE_ID,
        "template": AMERICAS_GRAY_BODY,
        "level": "auto_draft_safe",
        "intent": "ismart_region_support",
        "scenario": "Americas form country: iSMART not supported in region",
        "scenario_en": "Americas form country: iSMART not supported in region",
        "also_queue_human": True,
        "required_info": [],
        "next_step": "",
        "human_trigger": "",
        "forbidden": "",
        "source": "americas-form-country",
    }


def pick_template(triage_intent: str, templates: list[dict],
                  body_text: str = "",
                  *,
                  llm=None,
                  use_llm: bool = False,
                  image_descriptions: list[str] | None = None,
                  thread_context: str | None = None) -> Optional[dict]:
    """闸门 → GPT 按语义选行（菜单含跨 Intent 召回）→ 关键词兜底。

    顺序：
      0) 表单国家落在对照表美洲 → 固定 AMER-001，不再按主诉选行
      0b) 硬闸 / review_lock
      0c) use_llm：候选菜单里 GPT 只输出 template_id（Intent 只是 hint）
      1) 同 intent 的 sub_scenario 硬信号
      2) 全表 enriched 关键词兜底
      3) VIN 闸门
      4) token overlap / 第一条候选
    """
    pick_template.last_debug = []  # type: ignore[attr-defined]
    from .region_catalog import americas_form_country
    if americas_form_country(body_text):
        pick_template.last_debug = [("americas_gray", AMERICAS_GRAY_TEMPLATE_ID)]  # type: ignore[attr-defined]
        return americas_gray_market_row()
    if not templates:
        return None
    # 国家 / 大区不参与选行。表单 Country 或「本国不支持」都不能把意图锁进 REG-001。
    if triage_intent == "ismart_region_support":
        triage_intent = ""
    evidence = _pick_evidence(body_text, image_descriptions, thread_context)

    def _finish(row: Optional[dict]) -> Optional[dict]:
        row = _demote_unwarranted_gen005(row, templates, body_text)
        row = _apply_row_policy(row) if row else None
        return _specialize_template_for_ask(row, body_text) if row else None

    # 0) 知识库「通用处理」闸门
    # 缺 VIN / 笼统：只看最新客户段 + 附件，避免线程里客服「请提供 VIN」误触发。
    # 已有 VIN：看全文（正文引用 + 附件 + 线程）。
    latest = _latest_customer_turn(body_text)
    latest_ev = _pick_evidence(latest, image_descriptions, None)
    if ismart_plan_or_3g_needs_human(evidence or body_text):
        pick_template.last_debug = [("human", "ismart_plan_or_3g")]  # type: ignore[attr-defined]
        return None
    if fraud_force_unbind_needs_human(evidence or body_text):
        pick_template.last_debug = [("human", "fraud_force_unbind")]  # type: ignore[attr-defined]
        return None
    if vague_app_connect_needs_human(body_text or evidence):
        pick_template.last_debug = [("human", "vague_app_connect")]  # type: ignore[attr-defined]
        return None
    if evidence:
        if _VIN_INVALID_RE.search(latest_ev) and not _body_has_vin(evidence):
            hit = _template_by_id(templates, "GEN-003")
            if hit:
                return _finish(hit)
        if _VIN_MISSING_RE.search(latest_ev) and not _body_has_vin(evidence):
            hit = _template_by_id(templates, "GEN-002")
            if hit:
                return _finish(hit)
        if _is_update_scope_consult(latest):
            hit = _pick_update_scope_template(templates, latest)
            if hit:
                return _finish(hit)
        if _is_vpn_restriction(latest):
            hit = _template_by_id(templates, "APP-VPN-001")
            if hit:
                return _finish(hit)
            return None
        if _is_headunit_display_fault(latest) or _is_infotainment_reboot_loop(latest):
            if _is_aa_infotainment_fault(latest):
                hit = _template_by_id(templates, "FICM-004")
                if hit:
                    return _finish(hit)
            if _is_infotainment_reboot_loop(latest):
                hit = _template_by_id(templates, "FICM-REBOOT-001")
                if hit:
                    return _finish(hit)
            hit = _pick_headunit_display_template(templates)
            if hit:
                return _finish(hit)
        if _is_secondary_cs_unlink(latest):
            hit = _template_by_id(templates, "ACC-009")
            if hit:
                return _finish(hit)
        merged = _try_merged_template(templates, latest)
        if merged:
            pick_template.last_debug = [("merged", merged.get("template_id") or "")]  # type: ignore[attr-defined]
            return _finish(merged)
        if _is_secondary_not_registered(latest) or _is_secondary_account_howto(latest):
            hit = _pick_secondary_account_template(templates, latest)
            if hit:
                return _finish(hit)
        force_tid, _banned = _review_lock(latest)
        if force_tid == "ACC-006" and not _acc006_handles_address(templates):
            force_tid = None
        if force_tid:
            hit = _template_by_id(templates, force_tid)
            if hit:
                return _finish(hit)
        if _is_vague_request(latest, evidence):
            hit = _template_by_id(templates, "GEN-001")
            if hit:
                return _finish(hit)

    if use_llm and llm and evidence.strip():
        cands = _llm_template_candidates(triage_intent, templates, evidence)
        tid = _llm_pick_template_id(
            llm=llm, body_text=evidence, candidates=cands,
            triage_intent=triage_intent,
            image_descriptions=None,
            thread_context=None,
        )
        pick_template.last_debug = [("llm", tid or "")]  # type: ignore[attr-defined]
        if tid and tid != "REG-001":
            hit = _apply_vin_post_gate(
                _template_by_id(templates, tid), evidence, templates,
                triage_intent,
            )
            if hit:
                return _finish(hit)

    intent_candidates = [
        t for t in templates
        if t["intent"] == triage_intent
        and str(t.get("template_id") or "") not in _RETIRED_TEMPLATE_IDS
        and str(t.get("template_id") or "") != "REG-001"
    ]
    if not intent_candidates:
        # 知识库没有该 intent 的行：补信息 GEN-001，不要掉进「后台分析中」等不可自动回复
        gen1 = _template_by_id(templates, "GEN-001")
        if gen1:
            intent_candidates = [gen1]
        else:
            intent_candidates = [
                t for t in templates
                if t["intent"] == "ismart_feature_inquiry"
                and t.get("level") != "no_auto_reply"
                and not str(t.get("template_id") or "").startswith("GEN-")
            ]

    # 1) 子场景打分（精确信号）
    sub = detect_sub_scenario(evidence, triage_intent) if evidence.strip() else None
    if sub and intent_candidates:
        scored = [
            (c, _template_sub_scenario_relevance(c, sub)) for c in intent_candidates
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        if scored[0][1] > 0:
            return _finish(scored[0][0])

    # 2) 全表场景/关键词（Intent 只加分，不锁桶）
    pool = templates if evidence.strip() else intent_candidates
    if evidence.strip() and pool:
        enriched_scored = []
        enriched_debug = []
        for c in pool:
            s = _score_template(c, evidence, triage_intent)
            enriched_scored.append((c, s))
            enriched_debug.append((c.get("template_id", ""), s))
        enriched_scored.sort(key=lambda x: x[1], reverse=True)
        pick_template.last_debug = enriched_debug[:12]  # type: ignore[attr-defined]
        winner, win_score = enriched_scored[0]
        intent_best = max(
            (s for c, s in enriched_scored if c.get("intent") == triage_intent),
            default=0.0,
        )
        # 跨 intent 必须比本桶明显更高，避免 CLOSE-002 的 hello 一类泛词抢稿
        raw_win = win_score - (2.0 if winner.get("intent") == triage_intent else 0.0)
        raw_intent = intent_best - (2.0 if intent_best else 0.0)
        if raw_win <= 0:
            winner = None
        elif winner.get("intent") != triage_intent and raw_win < raw_intent + 3.0:
            winner = None
        if winner is not None:
            gated = _apply_vin_post_gate(
                winner, evidence, templates, triage_intent,
            )
            if gated is None and _body_has_vin(evidence):
                for c, s in enriched_scored:
                    cid = str(c.get("template_id") or "")
                    if cid in {"GEN-001", "GEN-002"} or s <= 0:
                        continue
                    if cid == "TBOX-001" and not _is_tbox_replaced(
                        _latest_customer_turn(evidence)
                    ):
                        continue
                    gated = _apply_vin_post_gate(
                        c, evidence, templates, triage_intent,
                    )
                    if gated:
                        break
            if gated is None and _body_has_vin(evidence) and triage_intent == "follow_up_resolved":
                gated = _template_by_id(templates, "GEN-005")
            if gated:
                return _finish(gated)

    if not intent_candidates:
        return None

    # 3) 同 intent token overlap（兜底）
    if evidence.strip() and len(intent_candidates) > 1:
        scored = [
            (c, _template_body_token_overlap(c, evidence))
            for c in intent_candidates
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        if scored[0][1] > 0:
            return _finish(scored[0][0])

    return _finish(intent_candidates[0])


_FORM_DESC_RE = re.compile(
    r"(?:Description des problèmes|Description of (?:the )?issues?|"
    r"Problem description|Description of (?:the )?problems?|"
    r"Problembeschreibung|Beschreibung der Probleme|"
    r"Descrizione dei problemi|"
    r"Descripción de los problemas|Kuvaus ongelmista|"
    r"Beskrivning av problemen|Descrierea problemelor|"
    r"Opis problemów|Beschrijving van problemen)[:\s]*",
    re.I,
)
_WEBFORM_DROPDOWN_LINE_RE = re.compile(
    r"(?m)^[•\-\*]?\s*(Problem Type|Issue Type|Tipo de (?:problema|incidencia)|"
    r"Art des Problems|Tipo di problema)[:\s].*$",
    re.I,
)
_HAIL_INSURANCE_RE = re.compile(
    r"(grandinata|grandine|\bhail\b|assicurazione|\binsurance\b)",
    re.I,
)
_POWERTRAIN_ASK_RE = re.compile(
    r"(s'?emballe|n'accélère|engine.{0,60}(rev|stall|race)|"
    r"moteur.{0,40}accél)",
    re.I | re.S,
)
_PARTS_ASK_RE = re.compile(
    r"(wheel\s+locking\s+nut|locking\s+nut|mudguard|mud\s+passenger|"
    r"buy\s+a\s+front|where.{0,24}buy)",
    re.I,
)
_TYRE_LAMP_RE = re.compile(
    r"(falt[ae]\s+presi[oó]n|tyre\s+pressure|tire\s+pressure|"
    r"ruedas.{0,40}naranja|presión.{0,40}ruedas)",
    re.I | re.S,
)
_UK_FORWARD_RE = re.compile(
    r"(mutual\s+customer|please\s+could\s+you\s+respond\s+to\s+the\s+customer|"
    r"mg\s+uk\s+customer\s+care)",
    re.I,
)
_CONNEXION_ONLY_RE = re.compile(
    r"^connexion\s*\?+\s*$",
    re.I,
)


def _form_issue_description(text: str) -> str:
    blob = text or ""
    m = _FORM_DESC_RE.search(blob)
    if not m:
        return _strip_webform_privacy_footer(blob)
    rest = blob[m.end():]
    rest = re.split(
        r"(I can read the latest|Je peux lire|Posso leggere|Puedo leer|"
        r"Enviando esta|By sending this)",
        rest,
        maxsplit=1,
    )[0]
    return _strip_webform_privacy_footer(rest.strip())


def _rule_semantic_mismatch(latest: str, template_id: str) -> Optional[str]:
    """确定性错桶：不调 LLM。对不上返回英文原因。"""
    tid = (template_id or "").strip()
    blob = latest or ""
    if not tid:
        return None
    if "+" in tid:
        for part in tid.split("+"):
            hit = _rule_semantic_mismatch(latest, part.strip())
            if hit:
                return hit
        return None
    if tid in {"APP-DIGITAL KEY-001", "APP-DIGITAL KEY-002"}:
        if _is_backend_telematics_outage(blob):
            return "digital-key row vs telematics/data-plan ask"
        if _INCIDENTAL_BT_KEY_RE.search(blob) and not re.search(
            r"digital\s+key|数字钥匙", blob, re.I,
        ):
            return "digital-key row vs incidental bluetooth key"
    if tid == "FICM-001" and _is_aftermarket_accessory_ask(blob) and not (
        _is_infotainment_update_consult(blob)
    ):
        return "software-version row vs aftermarket accessory"
    if tid == "CPAA-001":
        if _is_hello_mg_phone_usb_voice(blob):
            return "CarPlay pairing row vs Hello MG / phone-USB voice spec"
        if _is_aa_infotainment_fault(blob):
            return "CarPlay pairing row vs infotainment crash/restart"
        if _is_infotainment_update_consult(blob):
            return "CarPlay pairing row vs infotainment update consult"
    if tid == "FOTA-001" and _is_map_online_update_failed(blob):
        return "OTA-when row vs online map update failed"
    if tid in {"FICM-PRIV-002", "FICM-PRIV-003"} and not _is_privacy_notice_loop(blob):
        return "privacy-notice row vs web-form footer / other ask"
    if tid == "BIND-008" and _is_network_unavailable_toast(blob):
        return "QR-unreadable row vs network-unavailable toast"
    if tid == "BIND-001" and _is_self_unbind_rebind(blob):
        return "previous-owner 72h row vs self unbind/rebind"
    if tid in {"GEN-001", "GEN-002", "BIND-001", "BIND-007"} and fraud_force_unbind_needs_human(blob):
        return "info/previous-owner row vs fraud/unauthorized force-unbind"
    if tid == "GEN-004" and _is_app_not_responding(blob):
        return "screenshot row vs app-not-responding (open-area first)"
    if tid == "BIND-007" and not (
        _is_vehicle_mgmt_howto(blob) or _is_self_unbind_rebind(blob)
    ):
        return "vehicle-management row vs form dropdown / connect fault"
    if tid == "REG-001" and _is_app_car_connect_fault(blob):
        return "unsupported-region row vs app-car connect fault"
    if tid == "REG-001" and _is_bind_ask_before_region_confirm(blob):
        return "unsupported-region row vs bind/QR ask before human confirm"
    if tid == "APP-GUIDE-001" and _is_bind_ask_before_region_confirm(blob):
        return "bind-tutorial row vs bind failure / ask-info"
    if tid in {"CHG-001", "CHG-002"} and (
        _is_app_says_range_drop(blob) or _is_app_blocks_charging(blob)
    ):
        return "non-iSMART charging row vs app charging-management ask"
    if tid == "STATUS-013" and _is_app_blocks_charging(blob):
        return "charge-target row vs app-blocks-charging"
    if tid == "MAN-001" and _is_bind_ask_before_region_confirm(blob):
        return "user-manual row vs bind/QR ask"
    if tid == "STATUS-001" and parked_false_awake_needs_human(blob):
        return "stale-app-data row vs parked false-ignition / 12V drain"
    if tid == "NON-005":
        if _HAIL_INSURANCE_RE.search(blob):
            return "user-manual row vs hail/insurance ask"
        if _POWERTRAIN_ASK_RE.search(blob):
            return "user-manual row vs powertrain fault"
        if _PARTS_ASK_RE.search(blob):
            return "user-manual row vs parts/hardware ask"
    if tid == "IPK-001" and _TYRE_LAMP_RE.search(blob):
        return "IPK language row vs tyre-pressure warning"
    if tid in {"ACC-001", "ACC-005"}:
        desc = _form_issue_description(blob)
        desc_one = re.sub(r"\s+", " ", desc).strip()
        if _CONNEXION_ONLY_RE.match(desc_one) or desc_one.lower() in {
            "connexion ??", "connexion?",
        }:
            return "account-login row vs empty connexion ask"
    if tid == "CLOSE-002" and _UK_FORWARD_RE.search(blob):
        return "empty-mail row vs dealer/UK forward"
    return None


def draft_semantic_mismatch_reason(
    body_text: str,
    template_id: str,
    filled_body: str = "",
    *,
    llm=None,
    use_llm: bool = True,
    scenario: str = "",
) -> Optional[str]:
    """建稿前语义复核。返回 skip 原因；None 表示可以落草稿。

    规则命中 → 拦。LLM 在规则都过后再看；调用失败或拿不到 ok 字段 → 放行。
    M1_SKIP_SEMANTIC_REVIEW=1 整段跳过（仅应急）。
    """
    if os.environ.get("M1_SKIP_SEMANTIC_REVIEW", "").strip() == "1":
        return None
    if (template_id or "").strip() == AMERICAS_GRAY_TEMPLATE_ID:
        return None
    latest = _latest_customer_turn(body_text)
    hit = _rule_semantic_mismatch(latest, template_id)
    if hit:
        return hit
    tid = (template_id or "").strip()
    if tid == "STATUS-003" and _is_remote_ui_stuck_updating(latest or body_text):
        return None
    if not use_llm or llm is None:
        return None
    from .prompts import SEMANTIC_DRAFT_REVIEW_SYSTEM
    from .schemas import SemanticDraftReview

    draft_clip = re.sub(r"\s+", " ", filled_body or "").strip()[:900]
    user = (
        f"template_id: {template_id or '(none)'}\n"
        f"scenario: {(scenario or '')[:400]}\n"
        f"draft_excerpt: {draft_clip or '(empty)'}\n\n"
        "--- Latest customer turn ---\n"
        + (latest or "")[:4000]
    )
    try:
        picked, _ = llm.invoke_json(
            schema=SemanticDraftReview,
            messages=[
                {"role": "system", "content": SEMANTIC_DRAFT_REVIEW_SYSTEM},
                {"role": "user", "content": user},
            ],
            reasoning_effort="low",
            max_completion_tokens=512,
            role="semantic_review",
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("[semantic_review] LLM failed: %s", e)
        return None
    if not isinstance(picked, SemanticDraftReview):
        return None
    if picked.ok:
        return None
    reason = (picked.reason or "semantic mismatch").strip()
    return reason[:240] or "semantic mismatch"


def run_template_reply(
    email_body: str,
    *,
    use_llm: bool = True,
    llm=None,
    templates: list[dict] | None = None,
    image_descriptions: list[str] | None = None,   # M1.6
    sender: str | None = None,                     # M1.7: 草稿开头 "Dear customer," → "Dear Mr./Ms. {Last Name},"
    sender_display_name: str | None = None,        # M2.5.4 fix: Graph .name 原始显示名
    thread_context: str | None = None,             # M2.5.5: Graph 会话线程历史（prepend 到 LLM triage context）
) -> dict:
    """端到端：email → (triage, template, placeholders, filled_body)

    M1.6: image_descriptions 由 caller 提前调 vision 抽取（gpt-5.4 multimodal），
    作为额外 triage 上下文。多个描述会被拼到 user message 末尾。
    M1.7: sender 用于把 "Dear customer," 替换成 "Dear Mr./Ms. {Last Name},"
          —— 仅替换开头一次，不破坏模板正文。拿不到名字时不动草稿。
    M2.5.4 fix: sender_display_name（Graph 解出的原始显示名）优先级最高，
                因为 sender 经过 PII redact 后只剩 `user_xxx@example.com`
                没法抽显示名。**始终**替换占位符（fallback 也是 "Dear customer,"），
                避免 `{{salutation}}` 字面量泄漏。
    M2.5.5: thread_context 是 Graph 拉的会话线程历史（caller 已经 redact 过
                或自己保证无 PII；这里不再做 redact 以免破坏上下文连贯性），
                作为额外 triage context 喂给 LLM。让 LLM 看到客户之前几封
                问过什么，我们之前回过什么，避免重复回答 / 跟上下文接不上。
    """
    if templates is None:
        templates = load_xlsx()

    triage = classify_intent(
        email_body,
        llm=llm, use_llm=use_llm,
        image_descriptions=image_descriptions,
        thread_context=thread_context,
    )
    tmpl = pick_template(
        triage.intent, templates, body_text=email_body,
        llm=llm, use_llm=use_llm,
        image_descriptions=image_descriptions,
        thread_context=thread_context,
    )

    if tmpl:
        # 命中行的自动回复等级 / 必填信息覆盖 LLM triage（xlsx 是派单标准）
        if tmpl.get("level"):
            triage.auto_reply_level = tmpl["level"]  # type: ignore[assignment]
        if tmpl.get("required_info"):
            triage.required_info_group = list(tmpl["required_info"])  # type: ignore[assignment]
        tid = str(tmpl.get("template_id") or "")
        if (
            tmpl.get("intent")
            and tmpl["intent"] != triage.intent
            and not tid.startswith("GEN-")
            and triage.urgency != "P0"
            and not triage.red_flags
        ):
            # L2/场景选中了另一行：intent 跟模板走。GEN / P0 红牌不改原 intent。
            triage.intent = tmpl["intent"]  # type: ignore[assignment]

    placeholders = extract_placeholders(tmpl["template"]) if tmpl else []
    # Naive fill：VIN_MASK_xxx / user_xxx@example.com 已经有；其他占位符留空
    # M2.5.5 fix: customer_email 用脱敏后的发件人邮箱（redact 后形如
    # user_<sha8>@example.com，不会泄漏原邮箱）；其它未填占位符被
    # fill_placeholders_naive(strip_unfilled=True) 全部 strip 掉。
    sender_for_fill = sender or ""
    customer_email_for_fill = ""
    if sender_for_fill:
        customer_email_for_fill = sender_for_fill
    ctx = {
        "VIN":            "VIN_MASK_xxx",
        "vehicle model":  "your vehicle",
        "feature":        triage.intent.replace("_", " "),
        "region":         triage.region,
        "strategy":       "supported",
        "vehicle":        "your vehicle",
        "customer_email": customer_email_for_fill,
        "phone":          "",
        "account":        "",
        "language":       triage.region and triage.region.lower() or "en",
    }
    filled = fill_placeholders_naive(tmpl["template"], ctx) if tmpl else ""
    if filled:
        filled = fill_bracket_slots(
            filled, email_body, image_descriptions=image_descriptions,
        )
        filled = _fill_stated_vehicle_model(filled, email_body)
        filled = _strip_already_known_intake_asks(
            filled, email_body, str(tmpl.get("template_id") or ""),
        )
        filled = _strip_vin_from_intake_list(filled, email_body)
        filled = _strip_screenshot_asks_when_attached(
            filled, image_descriptions,
        )

    # M1.7 + M2.5.4 fix: 个性化称谓 "Dear customer," / "{{salutation}}" → "Dear Mr./Ms. {Last},"
    # 注意：**始终**调用（即便没 sender / display_name），让 fallback 把
    # 模板正文里的 {{salutation}}, 占位符替换成 "Dear customer," —— 避免
    # placeholder 字面量泄漏到 Outlook Drafts。
    if filled and str((tmpl or {}).get("template_id") or "") != AMERICAS_GRAY_TEMPLATE_ID:
        from .greeting import personalize_greeting
        filled = personalize_greeting(
            filled, sender or "",
            display_name=sender_display_name,
        )

    # next_step / human_trigger / level_note 只写审计 record，不进草稿正文。
    # 草稿会落到客户线程里，内部中文操作说明不能出现在任何 mailbox。
    cs_next = (tmpl or {}).get("next_step") or ""
    cs_trig = (tmpl or {}).get("human_trigger") or ""
    cs_level_note = (tmpl or {}).get("level_note") or ""

    return {
        "triage":       triage.model_dump(),
        "template_doc": tmpl,
        "placeholders": placeholders,
        "filled_body":  filled,
        # M2.5.1: 模板自带的「禁止/注意表述」原文（xlsx 第 11 列）。
        # 仅供审计 / 给 LLM 改写时知道模板边界用 —— 模板正文是 xlsx 写死的，
        # 不会触发 forbidden 短语（已验：93 条带 forbidden 的模板正文 0 命中）。
        "template_forbidden":  (tmpl or {}).get("forbidden", "") or "",
        "cs_next_step":     cs_next,
        "cs_human_trigger": cs_trig,
        "cs_level_note":    cs_level_note,
        "image_descriptions": image_descriptions or [],   # 透传给 process_mail
        "sender":       sender or "",                     # M1.7: 透传给 process_mail（审计用）
        # M2.5.5: pick_template enriched 打分诊断（caller 写到 record）
        "pick_template_debug": getattr(pick_template, "last_debug", []),
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