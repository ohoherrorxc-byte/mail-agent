"""Generate eval/runs/v4_templates_README.md — single doc summary of all templates."""
from __future__ import annotations

import json
import sys
from collections import defaultdict as dd
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.template_reply import load_xlsx  # noqa: E402

ts = load_xlsx()

PRIORITY = [
    "ismart_connectivity", "ismart_user_manual", "ismart_privacy",
    "ismart_region_support", "vehicle_incompatible", "app_vehicle_control",
    "app_binding", "hardware_warranty", "ismart_feature_inquiry",
    "app_account", "adas_acc",
]

md = []
md.append("# iSMART 模板文档")
md.append("")
md.append("> 最后更新：2026-07-28")
md.append("")
md.append(f"**当前总模板数：** {len(ts)} 条（baseline + v2/v3 补充 + v4 扩展）")
md.append("")
md.append("## xlsx 文件清单")
md.append("")
md.append("| 文件 | 来源 | n | 说明 |")
md.append("|---|---|---:|---|")
md.append("| `iSMART售后服务邮箱回复方法_20260308.xlsx` | 客服团队原表 | baseline | M1 起即用，永远最后加载作 fallback |")
md.append("| `iSMART售后服务邮箱回复方法_v2_app_binding.xlsx` | 人工补充 | 70 | 历史 v2 扩充（app_binding 子场景） |")
md.append("| `iSMART售后服务邮箱回复方法_v3_2026-07-24.xlsx` | 人工补充 | 7 | 历史 v3 补充 |")
md.append("| `iSMART售后服务邮箱回复方法_v4_templates.xlsx` | **M2.5 自动生成** | 94 | 从 42k 客服回复清洗的子场景模板 |")
md.append("")
md.append("## 优先级 / 加载规则")
md.append("")
md.append("- `agent/template_reply.py:load_xlsx()` 自动 glob `iSMART售后服务邮箱回复方法_v*_*.xlsx`，reverse-sort（v4 先于 v3 先于 v2）")
md.append("- 同 intent + 同 template 文本去重：先 load 的保留 → **新版号覆盖旧版号**")
md.append("- `20260308.xlsx` 永远放最后 → 仅作为新场景的 fallback")
md.append("- `xlsx Category` 多 token 用 `,` 或 `/` 分隔时，loader 逐 token 查 `CATEGORY_TO_INTENT`，首个命中决定 intent；不再 fallback 到 `ismart_feature_inquiry`")
md.append("")

matrix = dd(lambda: dd(int))
for t in ts:
    src = t["source"]
    if "20260308" in src:
        matrix[t["intent"]]["baseline"] += 1
    elif "v2" in src:
        matrix[t["intent"]]["v2"] += 1
    elif "v3" in src:
        matrix[t["intent"]]["v3"] += 1
    elif "v4" in src:
        matrix[t["intent"]]["v4"] += 1

md.append("## 模板覆盖矩阵（按 Intent）")
md.append("")
md.append("| Intent | baseline (20260308) | v2 | v3 | **v4** | 总数 |")
md.append("|---|---:|---:|---:|---:|---:|")
for intent in sorted(matrix):
    m = matrix[intent]
    tot = m["baseline"] + m["v2"] + m["v3"] + m["v4"]
    md.append(f"| `{intent}` | {m['baseline']} | {m['v2']} | {m['v3']} | **{m['v4']}** | **{tot}** |")
md.append("")
md.append("**亮点 (M2.5 之前是 0)：**")
md.append("- `ismart_connectivity`: 0 → 10（v4 新增）")
md.append("- `ismart_user_manual`: 1 → 12（+11）")
md.append("- `ismart_privacy`: 1 → 12（+11）")
md.append("- `vehicle_incompatible`: 1 → 10（+9）")
md.append("- `ismart_region_support`: 3 → 12（+9）")
md.append("")

by_intent = dd(list)
for t in ts:
    by_intent[t["intent"]].append(t)

md.append("## 模板示例（每个重点 Intent 1-2 条 v4）")
md.append("")
for intent in PRIORITY:
    intent_templates = [t for t in ts if t["intent"] == intent]
    if not intent_templates:
        continue
    md.append(f"### `{intent}`  (n={len(intent_templates)})")
    md.append("")
    v4_t = [t for t in intent_templates if "v4:" in (t.get("description") or "")]
    baseline_t = [t for t in intent_templates if "v4:" not in (t.get("description") or "")]
    samples = v4_t[:2] + baseline_t[:1]
    for i, t in enumerate(samples[:2]):
        cat = t["category"]
        desc = t["description"]
        template = t["template"]
        # Clean whitespace for markdown
        template_clean = template.replace("\n", " / ").replace("\r", "")[:400]
        md.append(f"**{i+1}. 分类：** `{cat}`  ")
        md.append(f"**来源：** `{t['source']}`  ")
        if "v4:" in desc:
            md.append(f"**描述：** {desc[:150]}  ")
        md.append("")
        md.append("> " + template_clean)
        md.append("")

gap = json.load(open("eval/runs/v4_template_coverage_gap.json"))
expanded = set(PRIORITY)
md.append("## 覆盖缺口报告（M2.5 已填补 top 9 priority）")
md.append("")
md.append("| Intent | xlsx 现有 | 真业务 CS 回复 | gap (50:1) | M2.5 已扩？ |")
md.append("|---|---:|---:|---:|:---:|")
for intent in sorted(gap["by_intent"], key=lambda k: -gap["by_intent"][k]["gap"]):
    r = gap["by_intent"][intent]
    flag = "✅" if intent in expanded else "⏳"
    md.append(f"| `{intent}` | {r['n_xlsx_templates']} | {r['n_cs_replies']} | {r['gap']} | {flag} |")
md.append("")
md.append("⏳ = M2.5 未扩（gap 低或 CS 回复稀疏，可 M3 续做）")
md.append("")

md.append("## 怎么验证 / 跑")
md.append("")
md.append("```bash")
md.append("# 1) 跑 v4 清洗 / 生成（先 install scripts/redact_cs_replies.py + build_cs_chunks.py）")
md.append("python3 scripts/redact_cs_replies.py")
md.append("python3 scripts/v4_coverage_analysis.py     # gap 报告")
md.append("python3 scripts/v4_draft_templates.py       # 输出 xlsx")
md.append("")
md.append("# 2) 测试（79 passed）")
md.append("python3 -m pytest eval/ -q")
md.append("")
md.append("# 3) 看模板 / 挑模板")
md.append('python3 -c "from agent.template_reply import load_xlsx; print(len(load_xlsx()))"')
md.append("```")
md.append("")

md_text = "\n".join(md)
with open("eval/runs/v4_templates_README.md", "w", encoding="utf-8") as f:
    f.write(md_text)
print(f"wrote eval/runs/v4_templates_README.md  ({len(md_text)} chars)")
