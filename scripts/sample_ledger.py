#!/usr/bin/env python3
"""
sample_ledger.py — M2.6: 从 harvest_samples 候选池分层抽样 → 待标队列。

输入:  eval/candidates.jsonl  (harvest_samples.py 输出，已 redact)
输出:  eval/_samples_to_label.json (gitignored)

分层策略:
- 每条候选先用本地启发式（CS 类别正则 + body 关键词）粗分到 32 值 intent 桶
- 每桶按 --per-bucket 抽样（默认 7 条），32 桶齐 = 224 条
- 小桶（< per-bucket）全部抽；大桶按时间最近优先 + 桶内随机
- 桶匹配 fallback → "other" 桶

label_review.py 接着读 _samples_to_label.json，按 4 字段标：
  intent_truth  (32 值 Intent enum)
  urgency_truth (P0/P1/P2/P3)
  region_truth  (6 区 Region enum)
  country_truth (可选, ISO alpha-2 / 英文国名)

为什么只做"启发式粗分":
- sample_ledger 不调 LLM，0 token / 0 成本
- 真业务标注由 label_review.py 的人工完成（仍是 ground truth）
- 启发式只用来"让小类有样本"，不是用来"判 intent"

inference source 三档（透明）：
- "category_match" : body 命中 CATEGORY_TO_INTENT（高置信）
- "body_keyword"  : fallback 关键词正则（中置信）
- "fallback"      : 都没命中 → "other"（低置信，标注时重点 review）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── Intent 推断所需的两张表（mirror template_reply.py）────────────
# 与 template_reply.CATEGORY_TO_INTENT 完全一致；如改回需三同步：
# 1) agent/template_reply.py
# 2) scripts/sample_ledger.py
# 3) eval/coverage 报告里的 key list
CATEGORY_TO_INTENT: dict[str, str] = {
    # ── App 侧 ──
    "App-车控车态相关":       "app_vehicle_control",
    "App-账号管理相关":       "app_account",
    "App-绑车解绑相关":       "app_binding",
    "App-消息中心相关":       "app_messages",
    "App-优化建议":           "app_suggestion",
    "App-软件更新":           "app_update",
    # ── iSMART 平台 ──
    "iSMART-功能咨询":        "ismart_feature_inquiry",
    "iSMART-用户手册相关":     "ismart_user_manual",
    "iSMART-支持区域":        "ismart_region_support",
    "iSMART-翻译相关":         "ismart_translation",     # M2.5 补盲区
    # ── TSP ──
    "TSP-敏感数据要求":       "tsp_sensitive_data",
    "TSP-车辆信息缺失":        "tsp_data_missing",        # M2.5 补盲区
    # ── FICM ──
    "FICM-激活相关":          "ficm_activation",
    "FICM-CPAA相关":          "ficm_cpaa",
    "FICM-软件更新":          "app_update",               # per template_reply.py:49
    "FICM-隐私协议相关":      "ismart_privacy",
    "FICM-地图相关":          "ficm_map",
    "FICM-网络相关":           "ismart_connectivity",
    "FICM-Voice Service相关":  "ficm_voice",              # M2.5 补盲区
    "FICM-Radio相关":          "ficm_radio",              # M2.5 补盲区
    "FICM-蓝牙相关":           "ficm_bluetooth",          # M2.5 补盲区
    "FICM-Music相关":          "ismart_music_streaming",  # M2.5 补盲区
    "FICM-自动重启或黑屏":     "hardware_warranty",
    "FICM-空调相关":           "hardware_warranty",
    "FICM-充电相关":           "charging",
    "FICM-Online Service相关": "ismart_connectivity",
    "FICM-无法记忆驾驶模式":   "hardware_warranty",
    # ── 非 iSMART ──
    "非iSMART-功能咨询":      "hardware_warranty",
    "非iSMART-不支持车型":    "vehicle_incompatible",
    "非iSMART-充电相关":      "charging",
    "非iSMART-IPK相关":       "hardware_warranty",
    "非iSMART-车身控制相关":   "hardware_warranty",
    "非iSMART-售前售后服务":  "delivery_handover",
    "非iSMART-空调相关":       "hardware_warranty",
    # ── ADAS ──
    "Adas-ACC":              "adas_acc",
    "Adas-AEB":              "adas_aeb",
    "Adas-LKA":              "adas_lka",
    "Adas-TJA":              "adas_tja",
    # ── 关闭 ──
    "关闭-已解决":            "follow_up_resolved",
    "Negative":               "complaint",
}


# 二级路由（67 L2 from 20260729 xlsx → SubIntent enum）
# key = xlsx 二级问题分类原值；value = schemas.SubIntent 枚举值
CATEGORY_TO_SUBINTENT: dict[str, str] = {
    # ── iSMART 相关（32）──
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
    # ── 关闭（2）──
    "关闭-已解决":            "close_resolved",
    "关闭-无效邮件":          "close_invalid",
    # ── 新需求（15）──
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
    # ── 通用处理（5）──
    "请求完整车辆信息":          "general_request_complete_vehicle_info",
    "请求VIN/提示VIN位置":      "general_request_vin",
    "VIN不正确":                "general_vin_incorrect",
    "请求视频/截图":            "general_request_screenshot_video",
    "后台分析中":               "general_backend_analyzing",
    # ── 非 iSMART（12 + 1 adas_general + 1 ipk_general）──
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

# Body 关键词 fallback（中置信）—— 顺序敏感：先匹配先赢
# 每条 (regex_pattern, intent_enum, label_zh)
BODY_KEYWORD_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b(binding|二维码|QR code|绑车|解绑|二手车主?)\b", re.I),
     "app_binding", "bind"),
    (re.compile(r"\b(digital key|数字钥匙|keyless|bluetooth key)\b", re.I),
     "ismart_digital_key", "digital_key"),
    (re.compile(r"\b(login|登录|账号|account|password reset|密码)\b", re.I),
     "app_account", "account"),
    (re.compile(r"\b(air.conditioning|空调|A/C|AC\b)\b", re.I),
     "ismart_remote_control", "AC"),
    (re.compile(r"\b(charging|充电|charge.fail|charge.fail)\b", re.I),
     "charging", "charging"),
    (re.compile(r"\b(warranty|保修|recall|TSB)\b", re.I),
     "hardware_warranty", "warranty"),
    (re.compile(r"\b(privacy|隐私|GDPR|data protection)\b", re.I),
     "ismart_privacy", "privacy"),
    (re.compile(r"\b(navigation|导航|map|地图)\b", re.I),
     "ficm_map", "map"),
    (re.compile(r"\b(ACC|adaptive cruise)\b", re.I),
     "adas_acc", "adas_acc"),
    (re.compile(r"\b(AEB|automatic emergency)\b", re.I),
     "adas_aeb", "adas_aeb"),
    (re.compile(r"\b(lane keep|LKA|lane depart)\b", re.I),
     "adas_lka", "adas_lka"),
    (re.compile(r"\b(traffic jam|TJA)\b", re.I),
     "adas_tja", "adas_tja"),
    (re.compile(r"\b(lawyer|attorney|legal action|court)\b", re.I),
     "complaint", "complaint_lawyer"),
    (re.compile(r"\b(media|press|journalist|newspaper)\b", re.I),
     "complaint", "complaint_media"),
    (re.compile(r"\b(delivery|handover|交车|新车交付)\b", re.I),
     "delivery_handover", "handover"),
    (re.compile(r"\b(suggestion|feature request|建议|新需求)\b", re.I),
     "app_suggestion", "suggestion"),
    (re.compile(r"\b(app update|FICM update|software update|升级)\b", re.I),
     "app_update", "update"),
    (re.compile(r"\b(messages|notification|告警|推送)\b", re.I),
     "app_messages", "messages"),
    (re.compile(r"\b(incompatible|not supported|不支持|无此车型)\b", re.I),
     "vehicle_incompatible", "incompatible"),
    (re.compile(r"\b(data plan|SIM|data usage|流量)\b", re.I),
     "ismart_data_plan", "data_plan"),
    (re.compile(r"\b(FOTA|OTA|over.the.air)\b", re.I),
     "ismart_fota", "fota"),
    (re.compile(r"\b(resolved|already solved|已解决|thanks for)\b", re.I),
     "follow_up_resolved", "resolved"),
    # ── M2.5 新增 ──
    (re.compile(r"\b(bluetooth|hands.free|handsfree|麦克风|microphone)\b", re.I),
     "ficm_bluetooth", "bluetooth"),
    (re.compile(r"\b(hello mg|voice command|voice assistant|语音助手)\b", re.I),
     "ficm_voice", "voice"),
    (re.compile(r"\b(amazon music|spotify|music app|在线音乐)\b", re.I),
     "ismart_music_streaming", "music"),
    (re.compile(r"\b(DAB|RDS|电台|radio station|fm)\b", re.I),
     "ficm_radio", "radio"),
    (re.compile(r"\b(translation|翻译错误|wrong text|language error)\b", re.I),
     "ismart_translation", "translation"),
    (re.compile(r"\b(vehicle data missing|no vehicle data|car information missing|TSP.*missing)\b", re.I),
     "tsp_data_missing", "tsp_missing"),
]

# 38 个 intent 桶名（schemas.py Intent Literal；M2.5 加 6）
ALL_INTENTS = [
    "app_vehicle_control", "app_account", "app_binding", "app_update",
    "app_messages", "app_suggestion",
    "ismart_data_plan", "ismart_remote_control", "ismart_digital_key",
    "ismart_fota", "ismart_connectivity", "ismart_account_inquiry",
    "ismart_owner_change", "ismart_region_support", "ismart_privacy",
    "ismart_user_manual", "ismart_feature_inquiry",
    "ismart_translation", "ismart_music_streaming",    # M2.5
    "ficm_activation", "ficm_cpaa", "ficm_map",
    "ficm_bluetooth", "ficm_voice", "ficm_radio",       # M2.5
    "tsp_sensitive_data", "tsp_data_missing",          # M2.5
    "adas_acc", "adas_aeb", "adas_lka", "adas_tja",
    "hardware_warranty", "charging", "delivery_handover", "vehicle_incompatible",
    "complaint", "follow_up_resolved", "other",
]

LOG = logging.getLogger("sample_ledger")


# ── 推断 ─────────────────────────────────────────────────────────────

def _make_sample_id(graph_message_id: str) -> str:
    """stable sample_id from graph id (sha256[:8])。
    同一封邮件重跑 sample_ledger 不会改 sample_id，方便 diff 真值。
    """
    h = hashlib.sha256(graph_message_id.encode("utf-8")).hexdigest()[:8]
    return f"cand_{h}"


def infer_intent(candidate: dict) -> tuple[str, str, str]:
    """返回 (intent_enum, sub_intent_enum, source)。

    source ∈ {"category_match", "body_keyword", "keyword_classifier", "fallback"}
    sub_intent：M2.5 新增；二級路由（67 L2）。未命中时 = "sub_intent_unknown"。

    优先级（前面的胜出）：
      1) category_match       — xlsx 的 CS 类别字符串出现在 body（高置信，但不常见）
      2) keyword_classifier   — 调 20260729 xlsx 关键词引擎；medium+ 才接受（M2.5）
      3) body_keyword         — 内置 regex fallback（粗粒度，仅 intent）
      4) fallback             — ("other", "sub_intent_unknown")
    """
    body = candidate.get("body_text_redacted", "") or ""
    subject = candidate.get("subject_redacted", "") or ""
    haystack = f"{subject}\n{body}"

    # 1) category regex：CS 类别通常出现在 body 里 "类别: App-xxx" 或 "Categories: ..."
    for cat, intent in CATEGORY_TO_INTENT.items():
        if cat in haystack:
            sub = CATEGORY_TO_SUBINTENT.get(cat, "sub_intent_unknown")
            return intent, sub, "category_match"

    # 2) keyword classifier（M2.5）— 调 20260729 xlsx 关键词引擎
    #    接受策略：medium+ confidence 全收；low confidence 只收 intent ≠ "other"
    #    （即"猜到具体 sub_intent 比猜到 other 有用"，让 body_keyword 接管太粗）
    try:
        from scripts.intent_keyword_classifier import classify, _load_index
        index = _load_index()  # lazy load (cheap, ~1ms after first call)
        result = classify(body, subject, index=index, top_k=1, min_score=1.0)
        top = result["top"]
        if top:
            if result["confidence"] in ("high", "medium"):
                return top["intent"], top["sub_intent"], "keyword_classifier"
            if result["confidence"] == "low" and top["intent"] != "other":
                return top["intent"], top["sub_intent"], "keyword_classifier"
    except (FileNotFoundError, ImportError, KeyError):
        pass  # index 还没 build / 路径不对 → 静默 fallback

    # 3) body keyword fallback（粗粒度，仅 intent；sub_intent 留 unknown）
    for pat, intent, _label in BODY_KEYWORD_PATTERNS:
        if pat.search(haystack):
            return intent, "sub_intent_unknown", "body_keyword"

    # 4) 兜底
    return "other", "sub_intent_unknown", "fallback"


# ── main ─────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="从 harvest 候选池分层抽样 → 待标队列。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--candidates", default="eval/candidates.jsonl",
        help="harvest_samples 输出 JSONL。",
    )
    ap.add_argument(
        "--output", default="eval/_samples_to_label.json",
        help="待标队列 JSON (gitignored)；下接 label_review.py。",
    )
    ap.add_argument(
        "--per-bucket", type=int, default=7,
        help="每个 intent 桶抽样条数。32 × 7 = 224 条候选。",
    )
    ap.add_argument(
        "--min-bucket", type=int, default=1,
        help="桶内至少 --min-bucket 条候选才参与抽样；避免空桶。",
    )
    ap.add_argument(
        "--max-bucket", type=int, default=15,
        help="桶内抽样上限（防止大桶独占）；0 = 不限。",
    )
    ap.add_argument(
        "--seed", type=int, default=42,
        help="随机种子（保证可复现）。",
    )
    ap.add_argument(
        "--include-sources",
        default="category_match,body_keyword,keyword_classifier,fallback",
        help="逗号分隔；要纳入抽样的 inference source。",
    )
    ap.add_argument(
        "--exclude-existing", action="store_true",
        help="排除已存在 --output 的 sample_id（增量抽样）。",
    )
    ap.add_argument(
        "--body-excerpt-chars", type=int, default=2000,
        help="写到 output 的 body 截断长度。template 邮件 1500 字左右，"
             "默认 2000 几乎不截。",
    )
    ap.add_argument(
        "--verbose", "-v", action="store_true",
    )
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    rng = random.Random(args.seed)
    include_sources = set(s.strip() for s in args.include_sources.split(",") if s.strip())

    candidates_path = Path(args.candidates)
    if not candidates_path.exists():
        print(f"[ERR] candidates 文件不存在: {candidates_path}", file=sys.stderr)
        print(f"      先跑: python3 scripts/harvest_samples.py", file=sys.stderr)
        return 2

    # ── 加载已有 sample_id（增量模式）────────────────────────────
    out_path = Path(args.output)
    existing_ids: set[str] = set()
    existing_records: list[dict] = []
    if args.exclude_existing and out_path.exists():
        try:
            existing_records = json.loads(out_path.read_text(encoding="utf-8"))
            existing_ids = {r["sample_id"] for r in existing_records if "sample_id" in r}
            LOG.info("[sample_ledger] 已存在 %d 条；增量模式追加", len(existing_ids))
        except json.JSONDecodeError as e:
            LOG.warning("[sample_ledger] %s 不是合法 JSON: %s —— 全量重写",
                        out_path, e)

    # ── 读 candidates，分桶 ─────────────────────────────────────
    LOG.info("[sample_ledger] 读 %s ...", candidates_path)
    buckets: dict[str, list[tuple[dict, str, str, str]]] = defaultdict(list)
    #   value = (candidate_dict, intent, sub_intent, source)
    n_total = 0
    n_skipped_existing = 0
    n_skipped_source = 0
    n_skipped_empty = 0

    with candidates_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                cand = json.loads(line)
            except json.JSONDecodeError:
                LOG.warning("[sample_ledger] JSON parse 失败，跳过: %s", line[:80])
                continue
            n_total += 1

            sample_id = _make_sample_id(cand.get("graph_message_id", ""))
            if args.exclude_existing and sample_id in existing_ids:
                n_skipped_existing += 1
                continue

            intent, sub_intent, source = infer_intent(cand)
            if source not in include_sources:
                n_skipped_source += 1
                continue

            # body 必须有内容（空 body / 全 PII mask 都不入抽样）
            body = cand.get("body_text_redacted", "") or ""
            if len(body.strip()) < 30:
                n_skipped_empty += 1
                continue

            buckets[intent].append((cand, intent, sub_intent, source))

    LOG.info(
        "[sample_ledger] 读入 %d 条；分到 %d 个桶；"
        "跳过 existing=%d source=%d empty=%d",
        n_total, len(buckets), n_skipped_existing, n_skipped_source, n_skipped_empty,
    )

    # ── 每桶抽样 ───────────────────────────────────────────────
    per_bucket = args.per_bucket
    max_bucket = args.max_bucket or 10**9
    selected: list[dict] = []

    for intent in ALL_INTENTS:
        cands = buckets.get(intent, [])
        if len(cands) < args.min_bucket:
            continue  # 桶太小，整桶跳过
        # 按 received_at 降序（新邮件优先）后随机抽
        cands_sorted = sorted(cands, key=lambda t: t[0].get("received_at", ""), reverse=True)
        # rng.sample 不保留顺序；这里要保桶内多样性，shuffle 一下
        rng.shuffle(cands_sorted)
        take = min(per_bucket, len(cands_sorted), max_bucket)
        for cand, _, sub_intent, source in cands_sorted[:take]:
            sample_id = _make_sample_id(cand["graph_message_id"])
            body_full = cand.get("body_text_redacted", "") or ""
            body_excerpt = body_full[:args.body_excerpt_chars]
            if len(body_full) > args.body_excerpt_chars:
                body_excerpt += "\n... [truncated]"

            record = {
                "sample_id":          sample_id,
                "graph_message_id":   cand.get("graph_message_id"),
                "mailbox":            cand.get("mailbox"),
                "received_at":        cand.get("received_at"),
                "subject_redacted":   cand.get("subject_redacted"),
                "sender_redacted":    cand.get("sender_redacted"),
                "sender_display_name": cand.get("sender_display_name"),
                "body_excerpt":       body_excerpt,
                "inferred_intent":    intent,
                "inferred_sub_intent": sub_intent,
                "inferred_source":    source,
                "labels": {
                    "intent_truth":      None,
                    "sub_intent_truth":  None,   # M2.5 新增
                    "urgency_truth":     None,
                    "region_truth":      None,
                    "country_truth":     None,
                },
                "labeled_by":         None,
                "labeled_at":         None,
            }
            selected.append(record)

    # ── 合并 + 写盘 ────────────────────────────────────────────
    final = existing_records + selected if args.exclude_existing else selected
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(final, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        out_path.chmod(0o600)
    except OSError:
        pass

    # ── 报告 ────────────────────────────────────────────────────
    bucket_counts = {k: len(v) for k, v in buckets.items()}
    sampled_counts: dict[str, int] = defaultdict(int)
    for r in selected:
        sampled_counts[r["inferred_intent"]] += 1
    print()
    print("=== sample_ledger summary ===")
    print(f"candidates input:        {n_total}")
    print(f"skipped existing:        {n_skipped_existing}")
    print(f"skipped source:          {n_skipped_source}")
    print(f"skipped empty body:      {n_skipped_empty}")
    print(f"---")
    print(f"non-empty buckets:       {len(buckets)} / 38")
    print(f"sampled (this run):      {len(selected)}")
    print(f"total in {out_path.name}: {len(final)}")
    print(f"---")
    print(f"per-bucket sampled (intent | n_sampled / n_in_bucket):")
    for intent in ALL_INTENTS:
        n_in = bucket_counts.get(intent, 0)
        n_out = sampled_counts.get(intent, 0)
        if n_in or n_out:
            print(f"  {intent:<24} {n_out:>3} / {n_in:<3}")

    print(f"\nnext step: python3 scripts/label_review.py --pool {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())