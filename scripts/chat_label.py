#!/usr/bin/env python3
"""
chat_label.py — M2.6: 聊天友好的批量标注，与 label_review.py 共用同一池文件。

为什么另写一个：
  label_review.py 是单条交互 CLI —— 一条一提示，适合关在终端里埋头标。
  本脚本是"展示一批 → 粘给 Claude 帮你审 → 一次粘贴"的批量流，
  让你在跟我讨论策略 / 看 schema 的同时把标做了。

用法（每轮 ~10 条）：
  # 第 1 步：看 10 条未标
  python3 scripts/chat_label.py --round 1

  # 复制打印出来的整块 records 粘给我 → 我帮你审 / 帮你猜 intent

  # 第 2 步：把审过的答案粘回 stdin（每行：idx\\tintent\\turgency\\tregion\\tcountry）
  python3 scripts/chat_label.py --round 1 <<'EOF'
  1	ismart_account_inquiry	P2	Oceania	AU
  2	app_binding	P2	Europe	DE
  ...
  EOF

  # 第 3 步：看下一批
  python3 scripts/chat_label.py --round 2

字段约束（继承 label_review.py / schemas.py）：
  - intent   ∈ 32 值 enum（大小写不限）
  - urgency  ∈ {P0, P1, P2, P3}
  - region   ∈ {Europe, Asia, South America, Africa, Oceania, North America}
  - country  自由文本（ISO alpha-2 或英文名皆可）；空 = 留空

约定：
  - 每条标完立即落盘（断电也不丢）
  - 文件 chmod 600
  - idx 用 1-based（输出里显示的）
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── enum 白名单（mirror label_review.py / schemas.py）────────────────
INTENTS = [
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
SUB_INTENTS = [
    "app_vehicle_control_status", "app_binding_flow", "app_account_management",
    "ismart_feature_inquiry", "ficm_map", "ismart_privacy", "ismart_user_manual",
    "ficm_activation", "app_guide_manual", "app_messages", "ficm_network",
    "ficm_cpaa", "ficm_bluetooth", "ficm_online_service", "ismart_region_support",
    "ficm_software_update", "ficm_radio", "ficm_ui_complexity",
    "app_map", "app_energy_stats", "app_charging_mode_not_saved",
    "app_vehicle_image", "app_update", "tsp_data_missing", "tsp_sensitive_data",
    "ficm_music", "ficm_voice", "ficm_volume",
    "ficm_driving_mode_not_saved", "ficm_auto_reboot", "ismart_translation",
    "app_charging_schedule",
    "close_resolved", "close_invalid",
    "feat_request_charging_history", "feat_request_app_feedback",
    "feat_request_charging_notification", "feat_request_remote_window",
    "feat_request_total_mileage", "feat_request_ota",
    "feat_request_adjustable_charging_power", "feat_request_watch_app",
    "feat_request_multi_account_binding", "feat_request_theme_switching",
    "feat_request_split_charging_schedule", "feat_request_service_booking",
    "feat_request_seat_heating_schedule", "feat_request_remote_lights",
    "feat_request_ev_trip_planner",
    "ismart_owner_change", "feat_request_charging_schedule",
    "general_request_complete_vehicle_info", "general_request_vin",
    "general_vin_incorrect", "general_request_screenshot_video",
    "general_backend_analyzing",
    "non_ismart_charging", "non_ismart_after_sales_service",
    "non_ismart_unsupported_model", "non_ismart_body_control", "non_ismart_ac",
    "adas_general", "ipk_general", "non_ismart_feature_inquiry",
    "adas_acc", "adas_aeb", "adas_lka", "adas_tja", "non_ismart_ipk",
    "sub_intent_unknown",
]
URGENCIES = ["P0", "P1", "P2", "P3"]
REGIONS = ["Europe", "Middle East", "Asia", "South America", "Africa", "Oceania", "North America"]

LOG = logging.getLogger("chat_label")


# ── 工具 ────────────────────────────────────────────────────────────

def _save(records: list, path: Path) -> None:
    path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _validate(value: str, allowed: list, field: str) -> str:
    if value in allowed:
        return value
    norm = value.lower()
    match = next((a for a in allowed if a.lower() == norm), None)
    if match:
        return match
    raise ValueError(f"{field}={value!r} ∉ {allowed}")


def _print_record_compact(idx: int, r: dict) -> None:
    labels = r.get("labels") or {}
    done = "✓" if labels.get("intent_truth") else "·"
    hint = r.get("inferred_intent", "?")
    sub_hint = r.get("inferred_sub_intent") or "?"
    src = r.get("inferred_source", "?")
    print(f"[{idx + 1}] {done} {r['sample_id']}  "
          f"hint={hint}  sub={sub_hint}  src={src}  "
          f"recv={r.get('received_at', '')[:10]}")
    subj = r.get("subject_redacted", "") or ""
    print(f"     subject: {subj[:100]}")
    sender = r.get("sender_redacted", "") or ""
    name = r.get("sender_display_name", "") or ""
    print(f"     sender : {sender}  ({name})")
    body = (r.get("body_excerpt", "") or "").strip()
    # 截 280 字、单行
    body_one = " | ".join(s.strip() for s in body.splitlines() if s.strip())
    if len(body_one) > 280:
        body_one = body_one[:1200] + "…"
    print(f"     body   : {body_one}")
    # 附件提示（如有）
    n_att = r.get("attachment_count") or 0
    names = r.get("attachment_names") or []
    summaries = r.get("attachment_summaries") or []
    if n_att and names:
        shown = ", ".join(names[:5])
        more = f" (+{n_att - 5})" if n_att > 5 else ""
        print(f"     attach : {n_att} 个 [{shown}{more}]")
        if summaries:
            # 截 280 字、单行
            for s in summaries[:5]:
                txt = (s.get("summary") or "").replace("\n", " ").strip()
                if not txt:
                    txt = "(no summary)"
                if len(txt) > 200:
                    txt = txt[:200] + "…"
                print(f"       📎 {s.get('name','?')}: {txt}")
    elif n_att:
        print(f"     attach : {n_att} 个（无名）")
    print()


# ── main ─────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="批量展示 + stdin 读取 — 与 label_review.py 共用池文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--pool", default="eval/_samples_to_label.json",
                    help="待标队列 JSON。")
    ap.add_argument("--round", type=int, default=1,
                    help="第几轮；每轮展示 --batch-size 条未标。")
    ap.add_argument("--batch-size", type=int, default=10,
                    help="每轮条数；200 条 ÷ 10 = 20 轮。")
    ap.add_argument("--labeled-by", default="human")
    ap.add_argument("--show", type=int, default=None,
                    help="只显示 idx 的详情（不读 stdin），方便聊天时回看某条。")
    args = ap.parse_args(argv)

    pool_path = Path(args.pool)
    if not pool_path.exists():
        print(f"[ERR] 池文件不存在: {pool_path}", file=sys.stderr)
        return 2

    records = json.loads(pool_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        print(f"[ERR] {pool_path} 不是 list", file=sys.stderr)
        return 2

    # ── --show：单条详情 ─────────────────────────────────────
    if args.show is not None:
        i = args.show - 1  # 1-based → 0-based
        if 0 <= i < len(records):
            _print_record_compact(i, records[i])
            return 0
        print(f"[ERR] idx={args.show} 越界（1..{len(records)}）", file=sys.stderr)
        return 2

    # ── 找未标 ─────────────────────────────────────────────
    unlabeled_idx = [
        i for i, r in enumerate(records)
        if not ((r.get("labels") or {}).get("intent_truth"))
    ]
    n_total_labeled = len(records) - len(unlabeled_idx)

    print(f"=== chat_label round {args.round} ===")
    print(f"已标 {n_total_labeled}/{len(records)}；剩余未标 {len(unlabeled_idx)}")
    print()

    start = (args.round - 1) * args.batch_size
    end = start + args.batch_size
    batch = unlabeled_idx[start:end]

    if not batch:
        print(f"[chat_label] round {args.round} 无新未标条目。")
        if n_total_labeled >= 200:
            print(f"🎉 ≥ 200 条已标；可 eval:")
            print("  python3 eval/run_eval.py --samples eval/_samples_to_label.json \\")
            print("      --triage-only --out eval/runs/v12_200_triage.json")
        return 0

    print(f"--- 本轮 {len(batch)} 条（粘给 Claude 帮你审 / 猜） ---")
    print()
    for i in batch:
        _print_record_compact(i, records[i])

    # ── stdin 读 labels ─────────────────────────────────────
    print("--- 等 stdin (Ctrl-D 结束；空行忽略；# 开头忽略) ---")
    print("    格式：idx\\tintent\\tsub_intent\\turgency\\tregion\\tcountry")
    print("    （M2.5: sub_intent 是新加的 67 L2 + unknown；可空 = 默认 sub_intent_unknown）")
    print()

    n_done = 0
    try:
        for line in sys.stdin:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                print(f"  ! 跳过（字段数 < 4）: {line!r}", file=sys.stderr)
                continue
            try:
                idx_1based = int(parts[0].strip())
                idx = idx_1based - 1
                intent = _validate(parts[1].strip(), INTENTS, "intent")
                # sub_intent 可空 → 默认 sub_intent_unknown（最小惊讶）
                if len(parts) > 2 and parts[2].strip():
                    sub_intent = _validate(parts[2].strip(), SUB_INTENTS, "sub_intent")
                else:
                    sub_intent = "sub_intent_unknown"
                urgency = _validate(parts[3].strip(), URGENCIES, "urgency")
                region = _validate(parts[4].strip(), REGIONS, "region") if len(parts) > 4 else "Europe"
                country = parts[5].strip() if len(parts) > 5 else ""
            except (ValueError, IndexError) as e:
                print(f"  ! 解析失败: {line!r} err={e}", file=sys.stderr)
                continue

            if not (0 <= idx < len(records)):
                print(f"  ! idx={idx_1based} 越界（1..{len(records)}）", file=sys.stderr)
                continue

            r = records[idx]
            r.setdefault("labels", {
                "intent_truth": None, "sub_intent_truth": None,
                "urgency_truth": None,
                "region_truth": None, "country_truth": None,
            })
            r["labels"]["intent_truth"] = intent
            r["labels"]["sub_intent_truth"] = sub_intent
            r["labels"]["urgency_truth"] = urgency
            r["labels"]["region_truth"] = region
            r["labels"]["country_truth"] = country or None
            r["labeled_by"] = args.labeled_by
            r["labeled_at"] = datetime.now(timezone.utc).isoformat()

            _save(records, pool_path)
            n_done += 1
            print(
                f"  ✓ [{idx + 1}] {r['sample_id']}  "
                f"intent={intent} sub={sub_intent} "
                f"urgency={urgency} region={region} country={country or '∅'}",
                file=sys.stderr,
            )
    except KeyboardInterrupt:
        print("\n[chat_label] Ctrl-C；已标的保留。")

    # ── 报告 ──────────────────────────────────────────────
    print()
    print(f"=== chat_label 完成本轮 {n_done}/{len(batch)} ===")
    remaining = len(unlabeled_idx) - (start + n_done)
    if remaining > 0:
        print(f"剩余 {remaining} 条；下一轮:")
        print(f"  python3 scripts/chat_label.py --round {args.round + 1}")
    else:
        n_total = sum(
            1 for r in records
            if (r.get("labels") or {}).get("intent_truth")
        )
        print(f"🎉 全部已标 ({n_total}/{len(records)})")
        if n_total >= 200:
            print("可 eval:")
            print("  python3 eval/run_eval.py --samples eval/_samples_to_label.json \\")
            print("      --triage-only --out eval/runs/v12_200_triage.json")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())