#!/usr/bin/env python3
"""
label_review.py — M2.6: CLI 单条流式人工标注 sample_ledger 的待标队列。

输入:  eval/_samples_to_label.json  (sample_ledger.py 输出)
输出:  同文件 in-place 更新（每标完一条立即落盘，断了也不丢）

每个未标样本依次显示：
  - sample_id / received_at
  - sender (redacted) + display_name
  - subject (redacted)
  - body excerpt (前 600 字)
  - inferred_intent (sample_ledger 启发式建议)

人工标 4 字段：
  1) intent_truth  : 32 值 enum (默认 = inferred_intent，回车采纳)
  2) urgency_truth : P0 / P1 / P2 / P3
  3) region_truth  : Europe / Asia / South America / Africa / Oceania / North America
  4) country_truth : 可空，ISO alpha-2 或英文国名

特殊命令：
  s / skip    - 跳过本条（labels 全留空）
  b / back    - 上一条（撤销最近一次落盘）
  q / quit    - 退出（已标完的保留在文件里）
  ? / help    - 字段含义
  show        - 重显本条详情

约定：
- 标注是 ground truth；输错也没事，下次跑可改（v1.1 加 --fix 模式）
- 不调 LLM；不连外网；纯 stdin/stdout
- 文件 chmod 600（即便已 redact 也加一道）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

# ── enum 白名单（mirror schemas.py）──────────────────────────────────
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

# 67 L2 from iSMART自动回复知识库_全量模板_20260729.xlsx + 兜底
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

LOG = logging.getLogger("label_review")


# ── 工具 ────────────────────────────────────────────────────────────

def _save(records: list[dict], path: Path) -> None:
    path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _ask(prompt: str, default: Optional[str], allowed: Optional[list[str]] = None) -> str:
    """单行 prompt：default 是空 Enter 时的取值；allowed 校验（小写比较）。"""
    while True:
        suffix = f" [{default}]" if default else ""
        try:
            raw = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            print()
            return default or ""
        if not raw and default is not None:
            return default
        if not raw:
            print(f"  ! 不能为空（也没有 default）。Ctrl-D 退出。")
            continue
        if allowed is None:
            return raw
        # 不区分大小写匹配
        norm = raw.lower()
        match = next((a for a in allowed if a.lower() == norm), None)
        if match:
            return match
        print(f"  ! 取值必须是: {', '.join(allowed)}（大小写不限）。再输一次。")


def _print_record(r: dict, *, verbose: bool = False) -> None:
    print("─" * 78)
    print(f"sample_id : {r['sample_id']}")
    print(f"received  : {r.get('received_at', '')}")
    print(f"sender    : {r.get('sender_redacted', '')}  ({r.get('sender_display_name', '')})")
    print(f"subject   : {r.get('subject_redacted', '')}")
    if verbose:
        print(f"mailbox   : {r.get('mailbox', '')}")
        print(f"graph_id  : {r.get('graph_message_id', '')}")
    print(f"inferred  : {r.get('inferred_intent', '?')}  "
          f"sub={r.get('inferred_sub_intent', '?')}  "
          f"(source={r.get('inferred_source', '?')})")
    if r["labels"]["intent_truth"]:
        prev = r["labels"]
        print(f"已标      : intent={prev['intent_truth']} sub={prev.get('sub_intent_truth')}"
              f" urgency={prev['urgency_truth']}"
              f" region={prev['region_truth']} country={prev['country_truth']}")
    print()
    print("BODY (excerpt):")
    for line in (r.get("body_excerpt", "") or "").splitlines():
        print(f"  {line}")
    print()


# ── main ─────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="CLI 单条流式人工标注 _samples_to_label.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands during labeling:\n"
            "  s / skip    跳过本条（labels 留空）\n"
            "  b / back    回到上一条（撤销）\n"
            "  q / quit    退出（已标的保留）\n"
            "  ? / help    显示字段说明\n"
            "  show        重显本条详情\n"
        ),
    )
    ap.add_argument("--pool", default="eval/_samples_to_label.json",
                    help="待标队列 JSON。")
    ap.add_argument("--start-from", default="next_unlabeled",
                    choices=["next_unlabeled", "first", "index"],
                    help="从哪开始：next_unlabeled=第一个未标 / first=从头 / index=用 --index")
    ap.add_argument("--index", type=int, default=0,
                    help="配合 --start-from index；0-based。")
    ap.add_argument("--labeled-by", default=os.environ.get("USER", "human"),
                    help="标注人标识（写到 labels.labeled_by）。")
    ap.add_argument("--fix-mode", action="store_true",
                    help="允许改已标条目（v1.1；默认跳过已标）。")
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    pool_path = Path(args.pool)
    if not pool_path.exists():
        print(f"[ERR] 池文件不存在: {pool_path}", file=sys.stderr)
        print(f"      先跑: python3 scripts/sample_ledger.py", file=sys.stderr)
        return 2

    records = json.loads(pool_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        print(f"[ERR] {pool_path} 不是 list 结构（应是 [...]）", file=sys.stderr)
        return 2
    LOG.info("[label_review] 加载 %d 条", len(records))

    # ── 起始位置 ───────────────────────────────────────────────
    if args.start_from == "first":
        idx = 0
    elif args.start_from == "index":
        idx = args.index
    else:  # next_unlabeled
        idx = next(
            (i for i, r in enumerate(records) if not (r.get("labels") or {}).get("intent_truth")),
            len(records),  # 全部已标，从尾部开始（一般会立即退出）
        )

    print(f"[label_review] 起始 idx={idx} (of {len(records)})")
    print(f"[label_review] 标注人 = {args.labeled_by}")
    print(f"[label_review] 已标 = 跳过；未标 = 提示输入。修改已标用 --fix-mode。")
    print(f"[label_review] 命令：s=skip b=back q=quit ?=help show=重显")
    print()

    history: list[int] = []  # for "back" undo
    n_labeled_this_run = 0
    n_skipped_this_run = 0

    while idx < len(records):
        r = records[idx]
        labels = r.setdefault("labels", {
            "intent_truth": None, "sub_intent_truth": None,
            "urgency_truth": None,
            "region_truth": None, "country_truth": None,
        })

        # 跳过已标（除非 --fix-mode）
        already = bool(labels.get("intent_truth"))
        if already and not args.fix_mode:
            idx += 1
            continue

        _print_record(r)

        # 标
        try:
            cmd = input(
                f"[{idx+1}/{len(records)}] (s=skip b=back q=quit ?=help show=重显): "
            ).strip().lower()
        except EOFError:
            print()
            break

        if cmd in ("q", "quit"):
            print("[label_review] 退出。已标的保留。")
            break
        if cmd in ("s", "skip"):
            n_skipped_this_run += 1
            history.append(idx)
            idx += 1
            continue
        if cmd in ("b", "back"):
            if history:
                idx = history.pop()
                # 撤销：把当前条 label 清掉
                records[idx]["labels"] = {
                    "intent_truth": None, "sub_intent_truth": None,
                    "urgency_truth": None,
                    "region_truth": None, "country_truth": None,
                }
                records[idx]["labeled_by"] = None
                records[idx]["labeled_at"] = None
                _save(records, pool_path)
                LOG.info("[label_review] 撤销 idx=%d 并已落盘", idx)
            else:
                print("[label_review] 已经在第一条。")
            continue
        if cmd in ("?", "help"):
            print()
            print("字段含义（参考 CLAUDE.md / schemas.py）：")
            print("  intent_truth      : 邮件落入的售后意图类别；38 值 enum (M2.5)")
            print("                      回车默认 = inferred_intent（仅参考，不一定对）")
            print("  sub_intent_truth  : 二级路由；67 L2 + unknown (M2.5)")
            print("                      回车默认 = inferred_sub_intent")
            print("  urgency_truth     : P0=律师函/安全事故 P1=保修失效风险/ADAS P2=一般 P3=低")
            print("                      ADAS 4 类默认 P1；其它默认 P2")
            print("  region_truth      : 客户地区；影响合规规则选哪套")
            print("  country_truth     : 可空；UK/Europe 区分需要 country 字段")
            print()
            continue
        if cmd == "show":
            continue  # 上面的 _print_record 已显示；重新进入循环会再显示
        if cmd:
            print(f"  ! 未知命令 {cmd!r}；输入 s/b/q/?/show 或直接回车开始标注。")
            continue

        # ── 5 字段 prompt ──────────────────────────────────────
        print(f"  (回车 = 采纳默认值)")
        intent = _ask("intent     ", default=r.get("inferred_intent"), allowed=INTENTS)
        sub_intent = _ask("sub_intent ", default=r.get("inferred_sub_intent") or "sub_intent_unknown",
                          allowed=SUB_INTENTS)
        urgency = _ask("urgency    ", default="P2", allowed=URGENCIES)
        region = _ask("region     ", default="Europe", allowed=REGIONS)
        country = _ask("country    ", default="", allowed=None)

        labels["intent_truth"] = intent
        labels["sub_intent_truth"] = sub_intent
        labels["urgency_truth"] = urgency
        labels["region_truth"] = region
        labels["country_truth"] = country or None
        r["labeled_by"] = args.labeled_by
        r["labeled_at"] = datetime.now(timezone.utc).isoformat()

        _save(records, pool_path)
        n_labeled_this_run += 1
        history.append(idx)
        idx += 1
        print(f"  ✓ 已落盘 (labeled so far: {n_labeled_this_run})")
        print()

    # ── 报告 ────────────────────────────────────────────────────
    n_total_labeled = sum(1 for r in records if (r.get("labels") or {}).get("intent_truth"))
    print()
    print("=== label_review summary ===")
    print(f"labeled this run : {n_labeled_this_run}")
    print(f"skipped this run : {n_skipped_this_run}")
    print(f"total labeled    : {n_total_labeled} / {len(records)}")
    print(f"output           : {pool_path}")
    if n_total_labeled >= 200:
        print()
        print("🎉 ≥ 200 条已标！可以 eval:")
        print("   python3 eval/run_eval.py --samples eval/_samples_to_label.json \\")
        print("       --triage-only --out eval/runs/v12_200_triage.json")
        print("   然后人工 review → 把已标条目 merge 进 eval/samples_real_seed.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())