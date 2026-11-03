#!/usr/bin/env python3
"""
intent_keyword_classifier.py — M2.5: 用 20260729 xlsx 的触发关键词做 intent 分类。

定位：本地 <10ms deterministic 分类器，跟 LLM rubric 形成 A/B 或 hybrid。

输入:
  body: str (邮件正文)
  subject: str = "" (可选)
  index_path: str (默认 eval/_intent_keyword_index.json)

输出:
  {
    "candidates": [                    # top-K 候选，按 score 降序
      {
        "template_id": "ACC-001",
        "level1": "iSMART相关",
        "level2": "App-账号管理相关",
        "sub_intent": "app_account_management",
        "intent": "app_account",
        "score": 3.0,                  # 命中关键词数（去重）
        "matched_keywords": ["login", "another device", ...],
        "auto_reply_level": "auto_draft_safe",
        "required_info": ["basic_vehicle", "app_tech"]
      },
      ...
    ],
    "top": {...},                       # 同上第一个
    "confidence": "high"|"medium"|"low"|"none"
  }

评分：
  - 每个 trigger 匹配（substring case-insensitive）得 +1 分
  - scenario_keyword 匹配得 +0.5 分（次级信号）
  - multi-keyword 模板只要 ≥1 命中即算候选
  - 总分 = 去重命中数
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

LOG = logging.getLogger("intent_keyword_classifier")

# 一些停用词（避免垃圾匹配）
_JUNK_SUBSTR = {
    "the", "and", "for", "with", "from", "into", "your",
    "您好", "你好", "谢谢",
}

# 短关键词（≤2 字符）需要 word-boundary 匹配，避免误中（"AC" 撞 "back"）
_WORD_BOUNDARY_KW_THRESHOLD = 3

# 每封 iSMART 网页表单都会附 Privacy Notice + soimt URL（含 themeType）。
# 不剥掉的话 STATUS-005 / NEW-THEME 会压过真正的 region/bind 信号。
_URL_RE = re.compile(r"https?://\S+", re.I)
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


def _strip_form_noise(text: str) -> str:
    """去掉网页表单固定页脚，避免 privacy/theme 关键词误判 intent。"""
    if not text:
        return ""
    s = _PRIVACY_BOILERPLATE_RE.sub(" ", text)
    s = _URL_RE.sub(" ", s)
    return s


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().strip())


def _kw_matches(kw: str, haystack_norm: str) -> bool:
    """关键词命中判断。

    规则：
      - kw 太短（≤2 字符）→ 用 word-boundary regex 避免误中
      - kw 含空格（多词短语）→ 直接 substring 匹配（因为是 phrase）
      - 其它 → substring case-insensitive 匹配
    """
    if not kw or kw in _JUNK_SUBSTR:
        return False
    if len(kw) <= _WORD_BOUNDARY_KW_THRESHOLD:
        # word-boundary；kw 已经是 normalized
        pat = re.compile(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])")
        return bool(pat.search(haystack_norm))
    return kw in haystack_norm


def _score_template(t: dict, haystack_norm: str) -> tuple[float, list[str]]:
    """对单个模板算分。返回 (score, matched_keywords)。"""
    matched: list[str] = []
    for kw in t.get("triggers", []):
        kw_n = _norm(kw)
        if not kw_n:
            continue
        if _kw_matches(kw_n, haystack_norm):
            matched.append(f"trigger:{kw}")
    for kw in t.get("scenario_keywords", []):
        kw_n = _norm(kw)
        if not kw_n:
            continue
        if _kw_matches(kw_n, haystack_norm):
            matched.append(f"scenario:{kw}")
    # 去重 + 计分
    seen = set()
    score = 0.0
    final_matched: list[str] = []
    for m in matched:
        # 同一 trigger 多次出现 → 只算一次
        bare = m.split(":", 1)[1]
        if bare in seen:
            continue
        seen.add(bare)
        final_matched.append(m)
        score += 1.0 if m.startswith("trigger:") else 0.5
    return score, final_matched


def classify(
    body: str,
    subject: str = "",
    index: Optional[dict] = None,
    *,
    top_k: int = 5,
    min_score: float = 1.0,
    curated: Optional[dict[str, list[str]]] = None,
) -> dict:
    """主入口：body + subject → top-K 候选。

    Args:
      body / subject: 邮件正文 / 主题
      index:          加载好的 index dict（None = 调 _load_index）
      top_k:          返回前 K 个候选
      min_score:      低于此分视为无命中（confidence="none"）
      curated:        手工补的 {intent: [triggers...]} 短语补丁——客户邮件高频但模板库
                      没覆盖的痛点短语（如 "neden bağlanamadım"/"iSMART not working"/
                      "region not supported"）。以虚拟模板注入，trigger 命中 +1。
                      用于补 keyword 覆盖率，不替代 build_intent_keyword_index.py。

    Returns:
      {candidates: [...], top: {...} or None, confidence: "high"|"medium"|"low"|"none"}
    """
    if index is None:
        index = _load_index()

    haystack = _norm(_strip_form_noise(f"{subject}\n{body}"))

    # ── curated 短语补丁：合并进模板库（虚拟 _CUR-XXX 模板）────────
    effective_templates = list(index.get("templates", []))
    if curated:
        for intent, triggers in curated.items():
            if not intent or intent.startswith("_") or not triggers:
                continue
            effective_templates.append({
                "template_id":      f"_CUR-{intent}",
                "level1":           "_curated",
                "level2":           "_curated",
                "sub_intent":       "sub_intent_unknown",
                "intent":           intent,
                "triggers":         list(triggers),
                "scenario_keywords": [],
                "auto_reply_level": "auto_draft_review",
                "required_info":    [],
            })

    scored: list[dict] = []
    for t in effective_templates:
        score, matched = _score_template(t, haystack)
        if score <= 0:
            continue
        scored.append({
            "template_id":      t["template_id"],
            "level1":           t["level1"],
            "level2":           t["level2"],
            "sub_intent":       t["sub_intent"],
            "intent":           t["intent"],
            "score":            round(score, 2),
            "matched_keywords": matched,
            "auto_reply_level": t["auto_reply_level"],
            "required_info":    t["required_info"],
        })

    # 排序：score 降序；平分时优先 intent ≠ "other"（更具体优于通用 GEN-*）
    scored.sort(key=lambda c: (-c["score"], c["intent"] == "other"))
    candidates = scored[:top_k]

    if not candidates or candidates[0]["score"] < min_score:
        confidence = "none"
    else:
        top_score = candidates[0]["score"]
        top_intent = candidates[0]["intent"]
        runner_up = next(
            (c["score"] for c in candidates[1:] if c["intent"] != top_intent),
            0.0,
        )
        if runner_up <= 0:
            confidence = "high" if top_score >= 2 else "medium"
        elif top_score >= 3 and top_score >= 2 * runner_up:
            confidence = "high"
        elif top_score >= 2 and top_score > runner_up:
            confidence = "medium"
        else:
            confidence = "low"

    return {
        "candidates":  candidates,
        "top":         candidates[0] if candidates else None,
        "confidence":  confidence,
    }


def _load_index(path: str = "eval/_intent_keyword_index.json") -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} 不存在。先跑 python3 scripts/build_intent_keyword_index.py"
        )
    return json.loads(p.read_text(encoding="utf-8"))


# ── CLI ─────────────────────────────────────────────────────────────

def _parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--body", help="邮件正文（命令行直传）")
    ap.add_argument("--subject", default="", help="邮件主题（可选）")
    ap.add_argument("--body-file", help="邮件正文文件路径（替代 --body）")
    ap.add_argument("--index", default="eval/_intent_keyword_index.json")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--min-score", type=float, default=1.0)
    ap.add_argument("--samples",
                    help="eval/_samples_to_label.json 路径；批量评所有待标样本")
    ap.add_argument("--limit", type=int, default=20,
                    help="--samples 模式下的 limit（默认 20 防刷屏）")
    return ap.parse_args(argv)


def _eval_mode(args) -> int:
    """批量评 eval/_samples_to_label.json 里的所有待标样本。"""
    index = _load_index(args.index)
    samples = json.loads(Path(args.samples).read_text(encoding="utf-8"))
    n = min(args.limit, len(samples))
    print(f"[classifier] 评 {n}/{len(samples)} 条 sample（top-1 confidence 分布）：\n")

    conf_dist: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "none": 0}
    intent_dist: dict[str, int] = {}
    for i, r in enumerate(samples[:n]):
        body = r.get("body_excerpt", "") or ""
        subj = r.get("subject_redacted", "") or ""
        result = classify(body, subj, index=index, top_k=args.top_k)
        top = result["top"]
        conf = result["confidence"]
        conf_dist[conf] += 1
        if top:
            intent_dist[top["intent"]] = intent_dist.get(top["intent"], 0) + 1

        print(f"[{i+1:>3}] {r['sample_id']}  hint={r.get('inferred_intent','?'):<24}"
              f"  →  {top['intent'] if top else '∅':<24}"
              f"  conf={conf:<7}"
              f"  score={top['score'] if top else 0:<4}"
              f"  matched={top['matched_keywords'][:3] if top else []}")

    print(f"\n[classifier] confidence 分布: {conf_dist}")
    print(f"[classifier] intent top-1 分布:")
    for k, v in sorted(intent_dist.items(), key=lambda x: -x[1]):
        print(f"  {v:>3} × {k}")
    return 0


def main(argv=None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.samples:
        return _eval_mode(args)

    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.body:
        body = args.body
    else:
        print("[ERR] 需要 --body / --body-file / --samples 其一", file=sys.stderr)
        return 2

    index = _load_index(args.index)
    result = classify(body, args.subject, index=index,
                      top_k=args.top_k, min_score=args.min_score)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())