#!/usr/bin/env python3
"""
build_intent_keyword_index.py — M2.5: 解析 iSMART自动回复知识库_全量模板_20260729.xlsx
→ 输出 eval/_intent_keyword_index.json，给 intent_keyword_classifier.py 用。

输入:
  iSMART自动回复知识库_全量模板_20260729.xlsx (97 模板 × 12 列)

输出:
  eval/_intent_keyword_index.json
    {
      "version": "M2.5",
      "source": "iSMART自动回复知识库_全量模板_20260729.xlsx",
      "built_at": "2026-08-27T...",
      "n_templates": 97,
      "templates": [
        {
          "template_id": "GEN-001",
          "level1": "通用处理",
          "level2": "请求完整车辆信息",
          "sub_intent": "general_request_complete_vehicle_info",
          "intent": "other",                  # 32 → 38 enum
          "scenario": "客户描述过于笼统...",
          "triggers": ["help", "not working", ...],
          "auto_reply_level": "auto_draft_needs_info",
          "required_info": ["basic_vehicle"],
          "scenario_keywords": [...]           # 从 scenario 文本提取的额外关键词
        },
        ...
      ],
      "keyword_index": {                       # 倒排索引：keyword → template_ids
        "binding": ["BIND-001", "BIND-002", ...],
        "QR code": ["BIND-001", "BIND-002"],
        ...
      },
      "l2_to_sub_intent": { ... },             # 67 L2 → SubIntent enum
      "l2_to_intent":      { ... }             # 67 L2 → 38 Intent enum
    }

设计：
  - triggers 列在 xlsx 里就是逗号分隔（含中英文混合）。直接 split + lower + strip。
  - scenario 列作为辅助关键词池（让"客服没标的"也能命中）。
  - 一个 keyword 可命中多个 templates（多义）；score 由调用方算。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LOG = logging.getLogger("build_intent_keyword_index")


# ── 67 L2 → SubIntent（mirror schemas.SubIntent Literal）───────────
L2_TO_SUB_INTENT: dict[str, str] = {
    "App-车控车态相关":       "app_vehicle_control_status",
    "App-绑车解绑相关":       "app_binding_flow",
    "App-账号管理相关":       "app_account_management",
    "iSMART-功能咨询":        "ismart_feature_inquiry",
    "FICM-地图相关":          "ficm_map",
    "FICM-隐私协议相关":      "ismart_privacy",
    "iSMART-用户手册相关":     "ismart_user_manual",
    "FICM-激活相关":          "ficm_activation",
    "App-引导手册":           "app_guide_manual",
    "App-消息中心相关":       "app_messages",
    "FICM-网络相关":          "ficm_network",
    "FICM-CPAA相关":         "ficm_cpaa",
    "FICM-蓝牙相关":          "ficm_bluetooth",
    "FICM-Online Service相关": "ficm_online_service",
    "iSMART-支持车区域":      "ismart_region_support",
    "FICM-软件更新":          "ficm_software_update",
    "FICM-Radio相关":         "ficm_radio",
    "FICM-界面层级复杂":      "ficm_ui_complexity",
    "App-地图相关":           "app_map",
    "App-能耗统计相关":       "app_energy_stats",
    "App-无法记忆充电模式":   "app_charging_mode_not_saved",
    "App-车体图相关":         "app_vehicle_image",
    "App-软件更新":           "app_update",
    "TSP-车辆信息缺失":       "tsp_data_missing",
    "TSP-敏感数据要求":       "tsp_sensitive_data",
    "FICM-Music相关":        "ficm_music",
    "FICM-Voice Service相关": "ficm_voice",
    "FICM-Volume相关":        "ficm_volume",
    "FICM-无法记忆驾驶模式":   "ficm_driving_mode_not_saved",
    "FICM-自动重启":          "ficm_auto_reboot",
    "iSMART-翻译相关":        "ismart_translation",
    "App-预约充电相关":       "app_charging_schedule",
    "关闭-已解决":            "close_resolved",
    "关闭-无效邮件":          "close_invalid",
    "新需求-App-充电历史记录":   "feat_request_charging_history",
    "新需求-App-Feedback":       "feat_request_app_feedback",
    "新需求-App-充电提醒":       "feat_request_charging_notification",
    "新需求-App-远控车窗":       "feat_request_remote_window",
    "新需求-App-总里程":         "feat_request_total_mileage",
    "新需求-OTA升级":            "feat_request_ota",
    "新需求-FICM/App-可调充电功率": "feat_request_adjustable_charging_power",
    "新需求-Watch版App":         "feat_request_watch_app",
    "新需求-App-多账号绑车":     "feat_request_multi_account_binding",
    "新需求-App-多主题切换":     "feat_request_theme_switching",
    "新需求-App-分时段预约充电": "feat_request_split_charging_schedule",
    "新需求-App-预约维保":       "feat_request_service_booking",
    "新需求-App-预约座椅加热":   "feat_request_seat_heating_schedule",
    "新需求-App-远控关灯":       "feat_request_remote_lights",
    "新需求-FICM- EV Trip Planner": "feat_request_ev_trip_planner",
    "请求完整车辆信息":          "general_request_complete_vehicle_info",
    "请求VIN/提示VIN位置":      "general_request_vin",
    "VIN不正确":                "general_vin_incorrect",
    "请求视频/截图":            "general_request_screenshot_video",
    "后台分析中":               "general_backend_analyzing",
    "非iSMART-充电相关":         "non_ismart_charging",
    "非iSMART-售前售后服务":     "non_ismart_after_sales_service",
    "非iSMART-不支持车型":       "non_ismart_unsupported_model",
    "非iSMART-车身控制相关":     "non_ismart_body_control",
    "非iSMART-空调相关":         "non_ismart_ac",
    "ADAS相关":                 "adas_general",
    "IPK相关":                  "ipk_general",
    "非iSMART-功能咨询":         "non_ismart_feature_inquiry",
    "Adas-ACC":                 "adas_acc",
    "Adas-AEB":                 "adas_aeb",
    "Adas-LKA":                 "adas_lka",
    "Adas-TJA":                 "adas_tja",
    "非iSMART-IPK相关":         "non_ismart_ipk",
}


# ── 67 L2 → 38 Intent (mirror sample_ledger.CATEGORY_TO_INTENT) ────
L2_TO_INTENT: dict[str, str] = {
    "App-车控车态相关":       "app_vehicle_control",
    "App-绑车解绑相关":       "app_binding",
    "App-账号管理相关":       "app_account",
    "iSMART-功能咨询":        "ismart_feature_inquiry",
    "FICM-地图相关":          "ficm_map",
    "FICM-隐私协议相关":      "ismart_privacy",
    "iSMART-用户手册相关":     "ismart_user_manual",
    "FICM-激活相关":          "ficm_activation",
    "App-引导手册":           "ismart_user_manual",         # → 引导归手册类
    "App-消息中心相关":       "app_messages",
    "FICM-网络相关":          "ismart_connectivity",
    "FICM-CPAA相关":         "ficm_cpaa",
    "FICM-蓝牙相关":          "ficm_bluetooth",              # M2.5
    "FICM-Online Service相关": "ismart_connectivity",
    "iSMART-支持车区域":      "ismart_region_support",
    "FICM-软件更新":          "app_update",                  # per template_reply.py:49
    "FICM-Radio相关":         "ficm_radio",                  # M2.5
    "FICM-界面层级复杂":      "ismart_feature_inquiry",      # UI 显示问题归功能咨询
    "App-地图相关":           "app_vehicle_control",
    "App-能耗统计相关":       "app_vehicle_control",
    "App-无法记忆充电模式":   "app_vehicle_control",
    "App-车体图相关":         "app_vehicle_control",
    "App-软件更新":           "app_update",
    "TSP-车辆信息缺失":       "tsp_data_missing",            # M2.5
    "TSP-敏感数据要求":       "tsp_sensitive_data",
    "FICM-Music相关":        "ismart_music_streaming",      # M2.5
    "FICM-Voice Service相关": "ficm_voice",                  # M2.5
    "FICM-Volume相关":        "ficm_radio",                  # 音量归 radio
    "FICM-无法记忆驾驶模式":   "hardware_warranty",
    "FICM-自动重启":          "hardware_warranty",
    "iSMART-翻译相关":        "ismart_translation",          # M2.5
    "App-预约充电相关":       "app_vehicle_control",
    "关闭-已解决":            "follow_up_resolved",
    "关闭-无效邮件":          "other",
    # 新需求 → 大部分归 ismart_feature_inquiry 或 app_suggestion
    "新需求-App-充电历史记录":   "ismart_feature_inquiry",
    "新需求-App-Feedback":       "app_suggestion",
    "新需求-App-充电提醒":       "ismart_feature_inquiry",
    "新需求-App-远控车窗":       "ismart_feature_inquiry",
    "新需求-App-总里程":         "ismart_feature_inquiry",
    "新需求-OTA升级":            "ismart_fota",
    "新需求-FICM/App-可调充电功率": "ismart_feature_inquiry",
    "新需求-Watch版App":         "ismart_feature_inquiry",
    "新需求-App-多账号绑车":     "ismart_feature_inquiry",
    "新需求-App-多主题切换":     "app_suggestion",
    "新需求-App-分时段预约充电": "ismart_feature_inquiry",
    "新需求-App-预约维保":       "delivery_handover",
    "新需求-App-预约座椅加热":   "ismart_feature_inquiry",
    "新需求-App-远控关灯":       "ismart_feature_inquiry",
    "新需求-FICM- EV Trip Planner": "ismart_connectivity",
    # 通用处理 → other（fallback）
    "请求完整车辆信息":          "other",
    "请求VIN/提示VIN位置":      "other",
    "VIN不正确":                "other",
    "请求视频/截图":            "other",
    "后台分析中":               "other",
    # 非 iSMART
    "非iSMART-充电相关":         "charging",
    "非iSMART-售前售后服务":     "delivery_handover",
    "非iSMART-不支持车型":       "vehicle_incompatible",
    "非iSMART-车身控制相关":     "hardware_warranty",
    "非iSMART-空调相关":         "hardware_warranty",
    "ADAS相关":                 "adas_acc",                   # fallback 到 acc
    "IPK相关":                  "hardware_warranty",
    "非iSMART-功能咨询":         "hardware_warranty",
    "Adas-ACC":                 "adas_acc",
    "Adas-AEB":                 "adas_aeb",
    "Adas-LKA":                 "adas_lka",
    "Adas-TJA":                 "adas_tja",
    "非iSMART-IPK相关":         "hardware_warranty",
}


# ── 必填信息组 → RequiredInfoGroup (mirror schemas.RequiredInfoGroup)
L2_TO_REQUIRED_INFO: dict[str, list[str]] = {
    # 跨多组映射（覆盖 必填信息字典 8 组里大部分）
    "App-车控车态相关":       ["basic_vehicle", "app_tech"],
    "App-绑车解绑相关":       ["binding"],
    "App-账号管理相关":       ["basic_vehicle", "app_tech"],
    "App-引导手册":           ["basic_vehicle"],
    "App-地图相关":           ["app_tech"],
    "App-能耗统计相关":       ["app_tech"],
    "App-无法记忆充电模式":   ["app_tech"],
    "App-车体图相关":         ["app_tech"],
    "App-软件更新":           ["app_tech"],
    "App-消息中心相关":       ["basic_vehicle", "app_tech"],
    "App-预约充电相关":       ["app_tech"],
    "FICM-激活相关":          ["avn_activation"],
    "FICM-地图相关":          ["location_map"],
    "FICM-CPAA相关":         ["basic_vehicle"],
    "FICM-蓝牙相关":          ["basic_vehicle"],
    "FICM-网络相关":          ["location_map"],
    "FICM-Online Service相关": ["location_map"],
    "FICM-Voice Service相关": ["basic_vehicle"],
    "FICM-Radio相关":         ["basic_vehicle"],
    "FICM-Volume相关":        ["basic_vehicle"],
    "FICM-Music相关":        ["basic_vehicle"],
    "FICM-Radio相关":         ["basic_vehicle"],
    "FICM-界面层级复杂":      ["basic_vehicle"],
    "FICM-软件更新":          ["basic_vehicle"],
    "FICM-无法记忆驾驶模式":   ["basic_vehicle"],
    "FICM-自动重启":          ["basic_vehicle"],
    "TSP-车辆信息缺失":       ["basic_vehicle"],
    "TSP-敏感数据要求":       ["basic_vehicle"],
    "iSMART-支持车区域":      ["basic_vehicle"],
    "iSMART-用户手册相关":     ["basic_vehicle"],
    "iSMART-翻译相关":        ["basic_vehicle"],
    "iSMART-隐私协议相关":    ["basic_vehicle"],
    "非iSMART-充电相关":       ["charging_diff"],
    "非iSMART-车身控制相关":   ["non_ismart"],
    "非iSMART-空调相关":       ["non_ismart"],
    "非iSMART-IPK相关":       ["non_ismart"],
    "非iSMART-售前售后服务":   ["basic_vehicle"],
    "非iSMART-功能咨询":       ["non_ismart"],
    "非iSMART-不支持车型":     ["basic_vehicle"],
    "ADAS相关":                ["non_ismart"],
    "Adas-ACC":                ["non_ismart"],
    "Adas-AEB":                ["non_ismart"],
    "Adas-LKA":                ["non_ismart"],
    "Adas-TJA":                ["non_ismart"],
    "IPK相关":                 ["non_ismart"],
    "请求完整车辆信息":        ["basic_vehicle"],
    "请求VIN/提示VIN位置":    ["basic_vehicle"],
    "VIN不正确":               ["basic_vehicle"],
    "请求视频/截图":           ["non_ismart"],
    "后台分析中":              ["basic_vehicle"],
}


# ── 工具 ────────────────────────────────────────────────────────────

def _norm_trigger(t: str) -> str:
    """normalize keyword: lower, strip, collapse whitespace"""
    return re.sub(r"\s+", " ", t.strip().lower())


def _parse_triggers(triggers_raw: str) -> list[str]:
    """xlsx 的 触发关键词 列是逗号分隔；含中英文混合 + 单引号 + 顿号。
    按 , / 、 / ; 切，然后 normalize。
    """
    if not triggers_raw:
        return []
    parts = re.split(r"[,、;]+", triggers_raw)
    return [_norm_trigger(p) for p in parts if p.strip()]


def _extract_scenario_keywords(scenario: str) -> list[str]:
    """从 问题场景 文本里再提一些关键词（让"客服没标的"也能命中）。
    启发式：长度 > 2 的英文/数字 token，跳过常见停用词。
    """
    if not scenario:
        return []
    STOP = {
        "the", "and", "for", "with", "from", "into", "that", "this",
        "you", "your", "are", "can", "will", "have", "has", "had",
        "not", "but", "all", "any", "may", "via", "etc",
        "客户", "邮件", "需要", "是否", "进行", "通过", "查询", "提供",
    }
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]+|\d+|[一-鿿]{2,}", scenario)
    return [t.lower() for t in tokens if t.lower() not in STOP and len(t) >= 2]


# ── main ─────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xlsx", default="iSMART自动回复知识库_全量模板_20260729.xlsx",
                    help="20260729 全量模板 xlsx 路径。")
    ap.add_argument("--output", default="eval/_intent_keyword_index.json",
                    help="输出 JSON 路径。")
    args = ap.parse_args(argv)

    xlsx_path = Path(args.xlsx)
    if not xlsx_path.exists():
        print(f"[ERR] xlsx 不存在: {xlsx_path}", file=sys.stderr)
        return 2

    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["自动回复知识库"]

    templates = []
    keyword_index: dict[str, list[str]] = {}

    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue  # header
        if not row or not row[0]:
            continue

        template_id = (row[0] or "").strip()
        level1 = (row[1] or "").strip()
        level2 = (row[2] or "").strip()
        scenario = (row[3] or "").strip()
        triggers_raw = (row[4] or "").strip()
        auto_reply_raw = (row[5] or "").strip()
        required_raw = (row[6] or "").strip()

        triggers = _parse_triggers(triggers_raw)
        scenario_kw = _extract_scenario_keywords(scenario)

        # map → enum
        sub_intent = L2_TO_SUB_INTENT.get(level2, "sub_intent_unknown")
        intent = L2_TO_INTENT.get(level2, "other")
        required = L2_TO_REQUIRED_INFO.get(level2, [])

        # normalize auto_reply_level: 提取关键词
        # "可自动回复-补信息" → "auto_draft_needs_info"
        # "不可自动回复【...】" → "no_auto_reply"
        if "可自动回复并关闭" in auto_reply_raw:
            auto_reply_level = "auto_draft_and_close"
        elif "可自动回复-需人工复核" in auto_reply_raw or "需人工复核" in auto_reply_raw and "不可自动" not in auto_reply_raw:
            auto_reply_level = "auto_draft_review"
        elif "不可自动回复-需人工复核" in auto_reply_raw:
            auto_reply_level = "no_auto_reply"  # 严格 no
        elif "不可自动回复" in auto_reply_raw:
            auto_reply_level = "no_auto_reply"
        elif "可自动回复-补信息" in auto_reply_raw or "补附件" in auto_reply_raw:
            auto_reply_level = "auto_draft_needs_info"
        elif "可自动回复" in auto_reply_raw:
            auto_reply_level = "auto_draft_safe"
        else:
            auto_reply_level = "auto_draft_safe"  # default

        t = {
            "template_id":      template_id,
            "level1":           level1,
            "level2":           level2,
            "sub_intent":       sub_intent,
            "intent":           intent,
            "scenario":         scenario,
            "triggers":         triggers,
            "scenario_keywords": scenario_kw,
            "auto_reply_level": auto_reply_level,
            "required_info":    required,
        }
        templates.append(t)

        # 倒排索引：keyword → template_ids
        for kw in triggers + scenario_kw:
            keyword_index.setdefault(kw, [])
            if template_id not in keyword_index[kw]:
                keyword_index[kw].append(template_id)

    out = {
        "version":       "M2.5",
        "source":        str(xlsx_path),
        "built_at":      datetime.now(timezone.utc).isoformat(),
        "n_templates":   len(templates),
        "templates":     templates,
        "keyword_index": keyword_index,
        "l2_to_sub_intent": L2_TO_SUB_INTENT,
        "l2_to_intent":      L2_TO_INTENT,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[build_index] {len(templates)} templates → {out_path}")
    print(f"[build_index] {len(keyword_index)} unique keywords indexed")
    # coverage stats
    by_intent = {}
    by_sub_intent = {}
    for t in templates:
        by_intent[t["intent"]] = by_intent.get(t["intent"], 0) + 1
        by_sub_intent[t["sub_intent"]] = by_sub_intent.get(t["sub_intent"], 0) + 1
    print(f"\n[by intent] {len(by_intent)} distinct 38-enum values covered:")
    for k, v in sorted(by_intent.items(), key=lambda x: -x[1])[:10]:
        print(f"  {v:>2} × {k}")
    print(f"\n[by sub_intent] {len(by_sub_intent)} distinct 67+1-enum values covered:")
    for k, v in sorted(by_sub_intent.items(), key=lambda x: -x[1])[:10]:
        print(f"  {v:>2} × {k}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())