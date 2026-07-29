"""
test_intent_mapping.py — 32 Intent 枚举一致性回归。

防三件事再发生：
  1. CATEGORY_TO_INTENT 映射到不存在的 Intent 字面量（曾出现 ficm_update）
  2. xlsx Category 文本拼写改了但 CATEGORY_TO_INTENT 没同步
  3. 真值样本里有 schema 之外或缺失的 Intent

不连 Azure，全部本地检查，跑在 CI smoke 阶段。
"""
from __future__ import annotations

import sys
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.schemas import Intent  # noqa: E402
from agent.template_reply import CATEGORY_TO_INTENT, load_xlsx  # noqa: E402


def test_intent_enum_count_is_32():
    """schemas.py 的 Intent literal 必须是 32 值（M1 拍板）。"""
    legal = set(typing.get_args(Intent))
    assert len(legal) == 32, f"Intent enum 应有 32 值，实测 {len(legal)}: {sorted(legal)}"


def test_category_to_intent_all_legal():
    """CATEGORY_TO_INTENT 所有 value 必须命中合法 Intent。"""
    legal = set(typing.get_args(Intent))
    bad = {
        cat: intent
        for cat, intent in CATEGORY_TO_INTENT.items()
        if intent not in legal
    }
    assert not bad, f"非法 Intent 映射: {bad}"


def test_xlsx_loads_without_error():
    """xlsx 加载不能因 422/校验失败而崩。"""
    rows = load_xlsx()
    assert len(rows) > 0, "xlsx 加载后无行"


def test_xlsx_every_intent_legal():
    """加载出来的 row.intent 必须在 32 个 literal 中。"""
    legal = set(typing.get_args(Intent))
    rows = load_xlsx()
    bad = sorted({r["intent"] for r in rows} - legal)
    assert not bad, f"xlsx 行命中非法 Intent: {bad}"


def test_no_legacy_ficm_update():
    """防回归：ficm_update 这个已删除的字面量不应再出现。"""
    assert "ficm_update" not in CATEGORY_TO_INTENT.values(), (
        "FICM-软件更新 又被映射到已废弃的 ficm_update"
    )


def test_ground_truth_samples_all_legal():
    """真值样本（samples_real_seed.json + samples_real_na.json）的 truth.intent
    必须在 32 Intent 内。

    注：run_eval.py 消费的字段是 'truth'，不是 'expected'（早期可能叫 expected，
    后改为 truth。混淆后看着像"intent 全部 None"，实则字段路径不对）。
    """
    import json
    legal = set(typing.get_args(Intent))
    repo = ROOT / "eval"
    for fname in ("samples_real_seed.json", "samples_real_na.json"):
        path = repo / fname
        if not path.exists():
            continue
        samples = json.loads(path.read_text())
        for s in samples:
            truth = s.get("truth")
            if not truth:
                continue
            intent = truth.get("intent")
            assert intent in legal, (
                f"{fname} {s.get('id')} 真值 intent={intent!r} 不在 32 enum 中"
            )


def test_ficm_software_update_maps_to_app_update():
    """xlsx 里 FICM-软件更新 行的 intent 应是 app_update。"""
    rows = load_xlsx()
    targets = [r for r in rows if r["category"] == "FICM-软件更新"]
    if not targets:
        # xlsx 可能没有这一类（部分批次），跳过即可
        return
    bad = [r for r in targets if r["intent"] != "app_update"]
    assert not bad, f"FICM-软件更新 应映射到 app_update, 实际: {[r['intent'] for r in targets]}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
