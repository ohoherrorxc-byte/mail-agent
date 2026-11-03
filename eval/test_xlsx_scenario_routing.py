"""
test_xlsx_scenario_routing.py — 验证 pick_template 在同 intent 多候选下，
按 xlsx 第 4/5 列「问题场景 / 触发关键词」+ LLM 扩展关键词打分，选对模板。

根因（M2.5.5 之前）：pick_template 完全没用 xlsx 问题场景 / 触发关键词，
只在同 intent 候选里走硬编码正则（只覆盖 app_binding 的 3 个 sub_scenario）
或 body token overlap。xlsx 96 条模板里人工事先分的 32 个 app_binding
候选只能用第一条 / 偶然 token 重叠选，80% 选错。

修复：
  - scripts/enrich_xlsx_scenarios.py 一次性把 scenario 翻英文 + 扩 8-15 关键词
  - agent/template_reply.py _enriched_keyword_score 按 (existing + extracted)
    + en_scenario token 打分
  - pick_template 在 sub_scenario 没命中时优先走 enriched 关键词打分

覆盖：
  Part A. _enriched_keyword_score 单元：existing / extracted / scenario_tokens 三档权重
  Part B. _kw_match 边界：单词 vs 多 token / 大小写 / 空字符串
  Part C. pick_template 端到端选 template：GEN-001 vs GEN-002 vs GEN-004 vs BIND-001 等
  Part D. enriched JSON 缺数据时 fallback 到 body token overlap
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.template_reply import (
    load_xlsx,
    pick_template,
    _enriched_keyword_score,
    _kw_match,
    _load_enriched_json,
    fill_bracket_slots,
    fill_placeholders_naive,
    human_trigger_should_escalate,
    run_template_reply,
    _extract_level_note,
    _strip_screenshot_asks_when_attached,
)


# ── Part A: _enriched_keyword_score 三档权重 ────────────────────────

def test_enriched_score_existing_weight_higher_than_extracted():
    """人工 existing 关键词权重 (3.0) > LLM 扩展 extracted (2.0)。"""
    existing_score, _ = _enriched_keyword_score(
        {"keywords_existing": ["vin missing"], "keywords_extracted": [], "scenario_en_tokens": []},
        "I have a vin missing issue",
    )
    extracted_score, _ = _enriched_keyword_score(
        {"keywords_existing": [], "keywords_extracted": ["vin missing"], "scenario_en_tokens": []},
        "I have a vin missing issue",
    )
    assert existing_score == 3.0, f"existing 关键词命中应得 3.0，实测 {existing_score}"
    assert extracted_score == 2.0, f"extracted 关键词命中应得 2.0，实测 {extracted_score}"
    assert existing_score > extracted_score
    print("  ✓ existing (3.0) > extracted (2.0) 权重")


def test_enriched_score_scenario_tokens_lowest_weight():
    """scenario_en_tokens 权重 0.5（兜底信号）。"""
    tok_score, _ = _enriched_keyword_score(
        {"keywords_existing": [], "keywords_extracted": [], "scenario_en_tokens": ["vague"]},
        "the problem is vague",
    )
    assert tok_score == 0.5, f"scenario token 命中应得 0.5，实测 {tok_score}"
    print("  ✓ scenario_en_tokens 权重 0.5（兜底）")


def test_enriched_score_returns_breakdown_dict():
    """返回 (score, debug_breakdown) 包含 existing_hits / extracted_hits / scenario_tokens_hit。"""
    _, dbg = _enriched_keyword_score(
        {"keywords_existing": ["vin missing", "chassis"],
         "keywords_extracted": ["no details"],
         "scenario_en_tokens": ["vague", "missing"]},
        "I have a vin missing issue with no details, vague situation",
    )
    assert "existing_hits" in dbg
    assert "extracted_hits" in dbg
    assert "scenario_tokens_hit" in dbg
    assert "vin missing" in dbg["existing_hits"]
    assert "chassis" not in dbg["existing_hits"]
    assert "no details" in dbg["extracted_hits"]
    assert "vague" in dbg["scenario_tokens_hit"]
    print("  ✓ debug_breakdown 包含三类命中列表")


def test_enriched_score_empty_body_returns_zero():
    score, dbg = _enriched_keyword_score(
        {"keywords_existing": ["help"], "keywords_extracted": ["no details"], "scenario_en_tokens": ["vague"]},
        "",
    )
    assert score == 0.0
    assert dbg["existing_hits"] == []
    print("  ✓ 空 body 返 0 分（短路）")


def test_enriched_score_no_enriched_data_returns_zero():
    """模板没有 enriched 字段（legacy xlsx）→ 0 分，让 pick_template fallback。"""
    score, _ = _enriched_keyword_score(
        {"keywords_existing": [], "keywords_extracted": [], "scenario_en_tokens": []},
        "any body text",
    )
    assert score == 0.0
    print("  ✓ 无 enriched 数据 → 0 分")


# ── Part B: _kw_match 边界 ───────────────────────────────────────────

def test_kw_match_multi_token_uses_substring():
    """多 token 关键词 → 子串匹配（"VIN missing" in "I have a VIN missing issue"）。"""
    assert _kw_match("vin missing", "i have a vin missing issue")
    print("  ✓ 多 token 关键词用子串匹配")


def test_kw_match_long_single_token_uses_substring():
    """长度 ≥4 的单词 → 子串匹配（避免误杀）。"""
    assert _kw_match("help", "I need help please")
    print("  ✓ 长单词 (>=4 char) 用子串匹配")


def test_kw_match_short_token_uses_word_boundary():
    """长度 <4 的单词 → 词边界（避免 "a" 误中 "another"）。"""
    assert _kw_match("a", "another option here") is False
    assert _kw_match("a", "I need a VIN") is True
    print("  ✓ 短单词 (<4 char) 用词边界避免误中")


def test_kw_match_case_insensitive():
    """大小写不敏感：kw 与 body 都在内部 lower。"""
    assert _kw_match("VIN Missing", "i have a vin missing issue")
    assert _kw_match("vin missing", "i have a vin missing issue")
    assert _kw_match("VIN Missing", "I HAVE A VIN MISSING ISSUE")
    print("  ✓ 大小写不敏感")


def test_kw_match_empty_kw_returns_false():
    assert _kw_match("", "any body") is False
    assert _kw_match("   ", "any body") is False
    print("  ✓ 空关键词返 False")


def test_kw_match_substring_too_short_word_should_not_partial_match():
    """短单词 "QR" 不应该子串匹配 "QRScan" — 必须词边界。

    但我们的实现：长度 <4 → 词边界；"QR" 是 2 字符 → 词边界。
    "QR" 在 "QRScan" 里作为词边界成立吗？re.search(r"\bQR\b", "QRScan") → False
    验证：
    """
    import re
    # "QR" 在 "I scanned QRScan code" 不应命中
    assert _kw_match("QR", "I scanned QRScan code") is False
    # 但 "QR" 在 "scan the QR code" 应命中
    assert _kw_match("QR", "please scan the QR code")
    print("  ✓ 短单词 'QR' 词边界：'QRScan' 不命中，'QR code' 命中")


# ── Part C: pick_template 端到端选 template ───────────────────────────

def _gen_intent_candidates(templates, intent):
    return [t for t in templates if t["intent"] == intent]


def test_pick_template_vague_selects_gen001():
    """笼统描述、缺细节 → GEN-001（请求完整车辆信息）。"""
    templates = load_xlsx()
    chosen = pick_template(
        "app_binding", templates,
        body_text="Hi, I have a problem with my car but I don't know what details to provide. Please help.",
    )
    assert chosen is not None
    assert chosen.get("template_id") == "GEN-001", f"got {chosen.get('template_id')}"
    print("  ✓ 笼统描述 → GEN-001")


def test_pick_template_missing_vin_selects_gen002():
    """缺 VIN / 不知道 VIN 在哪 → GEN-002（请求VIN/提示VIN位置）。"""
    templates = load_xlsx()
    chosen = pick_template(
        "app_binding", templates,
        body_text="I want to bind my car but I cannot find the VIN. Where is VIN located on the vehicle?",
    )
    assert chosen.get("template_id") == "GEN-002", f"got {chosen.get('template_id')}"
    print("  ✓ 缺 VIN → GEN-002")


def test_pick_template_unclear_description_selects_gen004():
    """描述不足、要视频截图 → GEN-004（在 app_vehicle_control intent 下）。"""
    templates = load_xlsx()
    chosen = pick_template(
        "app_vehicle_control", templates,
        body_text="The app shows an error but I have no idea which step caused it. No error code visible.",
    )
    assert chosen.get("template_id") == "GEN-004", f"got {chosen.get('template_id')}"
    print("  ✓ 描述不足要视频截图 → GEN-004")


def test_pick_template_previous_owner_selects_bind001():
    """二手车 / 前车主未解绑 → BIND-001。"""
    templates = load_xlsx()
    chosen = pick_template(
        "app_binding", templates,
        body_text="My VIN is LSVAB12345678901X. The app shows vehicle already bound to a previous owner.",
    )
    assert chosen.get("template_id") == "BIND-001", f"got {chosen.get('template_id')}"
    print("  ✓ 前车主已绑定 → BIND-001")


def test_pick_template_vin_invalid_selects_gen003():
    """VIN 错 / 查不到 → GEN-003。"""
    templates = load_xlsx()
    chosen = pick_template(
        "app_binding", templates,
        body_text="I gave the wrong VIN, the system says VIN not found or invalid.",
    )
    assert chosen.get("template_id") == "GEN-003", f"got {chosen.get('template_id')}"
    print("  ✓ VIN 错 / 查不到 → GEN-003")


def test_pick_template_no_body_returns_first_candidate():
    """无 body 时不调用 enriched 评分 → 直接返 candidates[0]。"""
    templates = load_xlsx()
    candidates = _gen_intent_candidates(templates, "app_binding")
    chosen = pick_template("app_binding", templates, body_text="")
    assert chosen is candidates[0] or chosen.get("template_id") == candidates[0].get("template_id"), \
        "无 body 时应返第一条候选"
    print("  ✓ 无 body 返第一条候选（不调 enriched 打分）")


def test_pick_template_no_signal_returns_first_candidate():
    """body 完全不含任何关键词 → 走 fallback 到第一条候选。"""
    templates = load_xlsx()
    # 完全无关的 body
    chosen = pick_template(
        "app_binding", templates,
        body_text="xyzzy plugh foobar baz qux",  # 没有任何英文单词（>=4 char）能匹配
    )
    candidates = _gen_intent_candidates(templates, "app_binding")
    assert chosen is candidates[0]
    print("  ✓ 无任何信号 → fallback 到第一条候选")


def test_pick_template_intent_no_candidates_falls_back_to_ismart():
    """未知 intent → 补信息 GEN-001，避免落到「后台分析中」等不可自动回复。"""
    templates = load_xlsx()
    chosen = pick_template("nonexistent_intent_xyz", templates, body_text="any body")
    assert chosen is not None
    assert chosen.get("template_id") == "GEN-001", f"got {chosen.get('template_id')}"
    print("  ✓ 未知 intent → GEN-001 补信息")


# ── Part D: enriched JSON 缺数据时 fallback ─────────────────────────

def test_pick_template_fallback_when_enriched_missing():
    """enriched JSON 缺失 → pick_template 走 body token overlap 兜底，不崩。

    这里通过手动构造 templates_clean（清空所有 enriched 字段）模拟 legacy xlsx。
    """
    templates = load_xlsx()
    # 把所有 template 的 enriched 字段清空（模拟 legacy xlsx 模板）
    templates_clean = []
    for t in templates:
        tt = dict(t)
        tt["keywords_existing"] = []
        tt["keywords_extracted"] = []
        tt["scenario_en_tokens"] = []
        templates_clean.append(tt)
    # 选一封 body 含明显关键词的邮件
    chosen = pick_template(
        "app_binding", templates_clean,
        body_text="The QR code error says the vehicle is still bound to the previous owner.",
    )
    # 应走 body_token_overlap 兜底；不要求选哪条，但不应崩 + 应返 candidates[0] 或高 overlap
    assert chosen is not None
    assert chosen["intent"] == "app_binding"
    print("  ✓ enriched 缺失时 body_token_overlap 兜底（不崩）")


# ── Part E: enriched JSON 文件本身校验 ─────────────────────────────

def test_enriched_json_exists_and_loadable():
    """agent/xlsx_scenarios_enriched.json 必须存在且可读。"""
    enr = _load_enriched_json()
    assert enr, "enriched JSON 应非空"
    assert "GEN-001" in enr, "GEN-001 必须在 enriched JSON"
    assert "GEN-002" in enr
    assert "BIND-001" in enr
    g = enr["GEN-001"]
    assert g["scenario_en"], "scenario_en 必须非空"
    assert len(g["keywords_extracted"]) >= 8, "扩展关键词至少 8 条"
    assert len(g["scenario_en_tokens"]) >= 5, "scenario_en_tokens 至少 5 条"
    print("  ✓ enriched JSON 存在且包含 GEN-001/002/BIND-001 等")


def test_enriched_json_check_script_passes():
    """scripts/enrich_xlsx_scenarios.py --check 在 CI 上必须 PASS（xlsx ≤ JSON mtime）。"""
    import subprocess
    result = subprocess.run(
        ["python3", "scripts/enrich_xlsx_scenarios.py", "--check"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0, f"--check failed:\n{result.stdout}\n{result.stderr}"
    print("  ✓ scripts/enrich_xlsx_scenarios.py --check PASS")


def test_reg001_l2_maps_to_region_support():
    templates = load_xlsx()
    row = next(t for t in templates if t.get("template_id") == "REG-001")
    assert row["intent"] == "ismart_region_support", row["intent"]
    assert row["level"] == "no_auto_reply", row["level"]
    print("  ✓ REG-001 → ismart_region_support / no_auto_reply")


def test_load_xlsx_default_is_knowledge_base_only():
    templates = load_xlsx()
    sources = {t.get("source", "") for t in templates}
    assert any(
        "20260923" in s or "20260918" in s or "20260916" in s for s in sources
    ), sources
    assert any("全量模板" in s for s in sources)
    g002 = next(t for t in templates if t.get("template_id") == "APP-GUIDE-002")
    assert "全量模板" in (g002.get("source") or "")
    assert "please follow the steps" in (g002.get("template") or "").lower()
    assert not any("v4_templates" in s or "v2_app_binding" in s for s in sources), sources
    print("  ✓ 默认 load_xlsx 只用最新全量知识库")


def test_pick_template_region_not_supported_selects_reg001():
    """国家 /「不支持」不再选 REG-001。登录失败按账号问题走，不转区域人工。"""
    templates = load_xlsx()
    chosen = pick_template(
        "ismart_region_support", templates,
        body_text="iSMART is not supported in Malaysia. The car was imported from another country.",
    )
    assert not chosen or chosen.get("template_id") != "REG-001", chosen
    login = (
        "Country/Region: Pakistan\n"
        "Vehicle Identification Number: NFPW94396TP001440\n"
        "Issue Type: My Profile\n"
        "Description of Issues: I can't login to my app. Fix this issue asap\n"
    )
    login_row = pick_template("ismart_region_support", templates, body_text=login)
    assert login_row and login_row.get("template_id") != "REG-001", login_row
    assert login_row.get("level") != "no_auto_reply", login_row.get("level")
    print("  ✓ 不看区域：马来不支持 / 巴基斯坦登录都不走 REG-001")


def test_americas_form_country_fixed_draft_and_human():
    """对照表美洲表单国家：固定不支持稿，保留 Dear Customer，并标记转人工。"""
    from agent.outlook_drafts import Mail, MailboxRouter, process_mail
    from agent.template_reply import (
        AMERICAS_GRAY_BODY,
        AMERICAS_GRAY_TEMPLATE_ID,
        draft_semantic_mismatch_reason,
        run_template_reply,
    )

    templates = load_xlsx()
    chile = (
        "Country/Region: Chile\n"
        "VIN: LSJWP4393RZ209912\n"
        "Description of Issues: I cannot bind the car in the app.\n"
    )
    row = pick_template("app_binding", templates, body_text=chile, use_llm=False)
    assert row and row.get("template_id") == AMERICAS_GRAY_TEMPLATE_ID, row
    assert row.get("template") == AMERICAS_GRAY_BODY
    assert row.get("also_queue_human") is True
    assert "\n\nBest regards,\niSMART Service Team" in row["template"]

    for body in (
        "Country/Region: Morocco\nI cannot bind.\n",
        "Country/Region: United States\nI cannot bind.\n",
        "Country/Region: Canada\nI cannot bind.\n",
        "Country/Region: Netherlands\nI cannot bind.\n",
        "I live in Chile and cannot bind the car.\n",
        "Country/Region:\nI cannot bind.\n",
    ):
        other = pick_template("app_binding", templates, body_text=body, use_llm=False)
        assert not other or other.get("template_id") != AMERICAS_GRAY_TEMPLATE_ID, body

    cl = pick_template(
        "app_binding", templates,
        body_text="Country/Region: CL\nI cannot bind.\n",
        use_llm=False,
    )
    assert cl and cl.get("template_id") == AMERICAS_GRAY_TEMPLATE_ID
    cn = pick_template(
        "app_binding", templates,
        body_text="国家/地区: 智利\n绑不上。\n",
        use_llm=False,
    )
    assert cn and cn.get("template_id") == AMERICAS_GRAY_TEMPLATE_ID

    out = run_template_reply(
        chile, use_llm=False, templates=templates,
        sender_display_name="Ana Rojas",
    )
    assert out["filled_body"].startswith("Dear Customer,")
    assert "Dear Mr./Ms." not in out["filled_body"]
    assert draft_semantic_mismatch_reason(
        chile, AMERICAS_GRAY_TEMPLATE_ID, out["filled_body"],
        use_llm=True, llm=object(),
    ) is None

    mail = Mail(
        id="m-cl", subject="Issues with Vehicle or iSmart App",
        sender="a@x.com", received_at="2026-10-10T01:00:00Z",
        body_text=chile, is_read=False, sender_display_name="Ana Rojas",
    )
    rec = process_mail(
        mail, g=None, llm=None, templates=templates,
        allowlist=None, dry_run=True, use_llm=False,
    )
    assert not rec.get("skipped"), rec
    assert rec.get("template_id") == AMERICAS_GRAY_TEMPLATE_ID
    assert rec.get("queue_tag") == "转人工"
    assert rec.get("would_post_body", "").startswith("Dear Customer,")
    assert "not supported for this vehicle in your region" in rec["would_post_body"]

    follow = (
        "Country/Region: Chile\n"
        "Still cannot bind.\n"
        "On Mon, customer.care <customer.care@ismart-service.com> wrote:\n"
        "Please send the VIN.\n"
    )
    rec_follow = process_mail(
        Mail(
            id="m-cl-2", subject="Re: Issues",
            sender="a@x.com", received_at="2026-10-10T02:00:00Z",
            body_text=follow, is_read=False,
        ),
        g=None, llm=None, templates=templates, allowlist=None,
        dry_run=True, use_llm=False,
        router=MailboxRouter(default="customer.care@ismart-service.com"),
    )
    assert rec_follow.get("skipped") != "follow_up_needs_human", rec_follow
    assert "not supported for this vehicle in your region" in rec_follow.get("would_post_body", "")

    fraud = (
        "Country/Region: Brazil\n"
        "Someone else logged into my account without my authorization. "
        "Please unbind this unknown user.\n"
    )
    rec_fraud = process_mail(
        Mail(
            id="m-br", subject="Account",
            sender="b@y.com", received_at="2026-10-10T03:00:00Z",
            body_text=fraud, is_read=False,
        ),
        g=None, llm=None, templates=templates, allowlist=None,
        dry_run=True, use_llm=False,
    )
    assert rec_fraud.get("skipped") != "fraud_force_unbind_needs_human", rec_fraud
    assert rec_fraud.get("template_id") == AMERICAS_GRAY_TEMPLATE_ID
    print("  ✓ 美洲表单国家固定稿 + 转人工；摩洛哥/美加/无栏不走")


def test_fill_bracket_slots_climate_from_body():
    raw = "Please use 【the temperature adjustment or related climate function】 in the app."
    out = fill_bracket_slots(raw, "The seat heating option is missing from the climate slider.")
    assert "【" not in out
    assert "seat heating" in out
    print("  ✓ 【climate】 按邮件填 seat heating")


def test_fill_bracket_slots_strips_brackets_when_unknown():
    raw = "See 【the screenshot you attached】 please."
    out = fill_bracket_slots(raw, "hello", image_descriptions=None)
    assert "【" not in out
    print("  ✓ 无截图时去掉【】保留英文槽位")


def test_strip_screenshot_asks_when_image_attached():
    priv = """To help us investigate further, please provide:
Screenshots showing the Privacy Notice message and the time when it appeared.
A short video showing that the Privacy Notice has been accepted on the vehicle infotainment system.
Your iSMART App version.
"""
    out = _strip_screenshot_asks_when_attached(
        priv,
        ["[image.png] app=iSMART; Avis de confidentialité refusé"],
    )
    assert "Screenshots showing" not in out
    assert "short video" in out.lower()
    assert "App version" in out
    same = _strip_screenshot_asks_when_attached(priv, None)
    assert "Screenshots showing" in same
    combo = (
        "If it still fails, please provide screenshots or a short screen "
        "recording showing the error."
    )
    combo_out = _strip_screenshot_asks_when_attached(
        combo, ["[a.jpg] app=iSMART; error 59"],
    )
    assert "screenshot" not in combo_out.lower()
    assert "screen recording" in combo_out.lower()
    ack = "Please see the screenshot you attached for the error text."
    assert "screenshot you attached" in _strip_screenshot_asks_when_attached(
        ack, ["[a.png] app=iSMART; x"],
    )
    bind = """If the vehicle is still shown as bound after 72 hours, please provide the following information:

1. Country/Region
2. Vehicle brand and model
3. Full 17-character VIN
4. Your iSMART account email address or mobile number
5. Proof of vehicle ownership
6. Copy of your driving licence
7. Screenshot of the binding message shown in the App

Thank you for your understanding.
"""
    bind_out = _strip_screenshot_asks_when_attached(
        bind, ["[shot.png] app=iSMART; binding message"],
    )
    assert "Screenshot of the binding" not in bind_out
    assert re.search(r"^6\.\s+Copy of your driving licence\s*$", bind_out, re.M)
    assert not re.search(r"^\s*\d+\.\s*$", bind_out, re.M)
    assert "Thank you for your understanding" in bind_out
    print("  ✓ 已附图不再要截图，仍要视频")


def test_run_template_privacy_skips_screenshot_ask_when_attached():
    templates = load_xlsx()
    body = (
        "Description des problèmes: Bonjour, c’est écris avis de "
        "confidentialité refusé alors qu’il est valide sur le véhicule"
    )
    result = run_template_reply(
        body,
        use_llm=False,
        templates=templates,
        image_descriptions=[
            "[image.png] app=iSMART; Avis de confidentialité refusé 18:26",
        ],
        sender="user_001@example.com",
    )
    assert result["template_doc"]["template_id"] == "FICM-PRIV-003"
    filled = result["filled_body"]
    assert "provide screenshots" not in filled.lower()
    print("  ✓ FICM-PRIV-003 已附图不重复要截图")


def test_fill_placeholders_preserves_salutation_mustache():
    """{{salutation}} 不能被 {xxx} 填充器吃成 {}。"""
    raw = "{{salutation}},\n\nPlease send VIN {VIN}.\n"
    out = fill_placeholders_naive(raw, {"VIN": "ABC"})
    assert "{{salutation}}" in out, out
    assert "ABC" in out
    assert "{}" not in out
    print("  ✓ {{salutation}} 不被 {VIN} 填充器破坏")


def test_run_template_reply_greeting_not_empty_braces():
    templates = load_xlsx()
    result = run_template_reply(
        "I want to bind my car but I cannot find the VIN. Where is VIN located?",
        use_llm=False,
        templates=templates,
        sender="user_001@example.com",
    )
    body = result["filled_body"]
    assert not body.lstrip().startswith("{},"), body[:80]
    assert "Dear customer" in body[:200] or "Dear Mr." in body[:200]
    assert "CS NOTE" not in body
    assert "Next step:" not in body
    assert "Escalate if:" not in body
    assert result.get("cs_next_step")
    print("  ✓ 草稿开头不是 {}, 且不含 CS NOTE")


def test_human_trigger_escalates_urgent_no_vin():
    assert human_trigger_should_escalate(
        "客户无法提供 VIN 但问题紧急",
        "My digital key won't connect and I'm stuck in a parking lot. Very urgent.",
        "P1",
    )
    assert not human_trigger_should_escalate(
        "客户无法提供 VIN 但问题紧急",
        "Please tell me where to find the VIN on the car.",
        "P3",
    )
    print("  ✓ 人工介入条件：紧急升人工，普通问 VIN 不升")


def test_category_dict_keywords_merged_into_binding():
    templates = load_xlsx()
    bind = [t for t in templates if t.get("category") == "App-绑车解绑相关"]
    assert bind
    kws = " ".join(bind[0].get("keywords_existing") or [])
    assert "绑车" in kws or "解绑" in kws or "QR" in kws or "前任" in kws
    print("  ✓ 分类字典适用范围并入绑车模板关键词")


def test_extract_level_note_from_bracket_and_newline():
    assert _extract_level_note("不可自动回复【需人工复核】") == "需人工复核"
    assert "TCMP" in _extract_level_note(
        "不可自动回复\n【需要接入TCMP或人工查询VIN码是否无绑定账号】"
    )
    assert _extract_level_note("可自动回复") == ""
    print("  ✓ 等级列【】抽出人工说明")


def test_pick_template_data_plan_without_vin_selects_gen002():
    templates = load_xlsx()
    chosen = pick_template(
        "ismart_data_plan", templates,
        body_text="How do I upgrade my iSMART data plan after the free data period?",
    )
    assert chosen.get("template_id") != "PLAN-001"
    assert chosen.get("template_id") != "NET-PLAN-001", chosen.get("template_id")
    print("  ✓ 问套餐到期/升级不走 PLAN-001 / NET-PLAN-001")


def test_pick_template_data_plan_with_vin_does_not_select_plan001():
    templates = load_xlsx()
    assert not any(t.get("template_id") == "PLAN-001" for t in templates)
    vin = "LSJWN4A57NZ123456"
    chosen = pick_template(
        "ismart_data_plan", templates,
        body_text=f"VIN {vin}. How do I upgrade my iSMART data plan / telematics subscription?",
    )
    assert chosen.get("template_id") != "PLAN-001"
    assert chosen.get("template_id") != "NET-PLAN-001", chosen.get("template_id")
    print("  ✓ PLAN-001 已下线；有 VIN 也不套 3G 行")


def test_pick_template_vin_mask_counts_as_vin_present():
    templates = load_xlsx()
    chosen = pick_template(
        "ismart_data_plan", templates,
        body_text="VIN_MASK_78e7fce7 How do I upgrade my iSMART data plan / telematics subscription?",
    )
    assert chosen.get("template_id") != "GEN-002", chosen.get("template_id")
    assert chosen.get("template_id") != "PLAN-001"
    print("  ✓ VIN_MASK_ 视为已提供 VIN，且不回 PLAN-001")


def test_pick_template_vin_in_attachment_counts():
    """VIN 只在 Word/截图抽取里 → 不走 GEN-002。"""
    templates = load_xlsx()
    chosen = pick_template(
        "ismart_data_plan", templates,
        body_text="How do I upgrade my iSMART data plan after the free data period?",
        image_descriptions=[
            "[courrier.docx] (Word)\nVIN VIN_MASK_78e7fce7 telematics subscription",
        ],
    )
    assert chosen.get("template_id") != "GEN-002", chosen.get("template_id")
    assert chosen.get("template_id") != "PLAN-001"
    print("  ✓ 附件里的 VIN_MASK_ 不当缺 VIN")


def test_pick_template_screenshot_detail_not_vague_gen001():
    templates = load_xlsx()
    chosen = pick_template(
        "ismart_connectivity", templates,
        body_text="Please help I have a problem, not working.",
        image_descriptions=[
            "[fota.jpg] FOTA cannot connect to the service, error 47",
        ],
    )
    assert chosen.get("template_id") != "GEN-001", chosen.get("template_id")
    print("  ✓ 截图有 FOTA 细节 → 不套 GEN-001")


def test_pick_template_digital_key_howto_selects_key001():
    templates = load_xlsx()
    vin = "LSJWN4A57NZ123456"
    chosen = pick_template(
        "ismart_digital_key", templates,
        body_text=f"VIN {vin}. My digital key is not working and I cannot unlock with phone.",
    )
    assert chosen.get("template_id") == "KEY-001", chosen.get("template_id")
    print("  ✓ 数字钥匙配对失败 → KEY-001")


def test_pick_template_keys_locked_inside_selects_key002():
    templates = load_xlsx()
    chosen = pick_template(
        "ismart_digital_key", templates,
        body_text="My keys are locked inside the car and I am stranded in the parking lot.",
    )
    assert chosen.get("template_id") == "KEY-002", chosen.get("template_id")
    assert chosen.get("level") == "no_auto_reply"
    print("  ✓ 钥匙锁车内 → KEY-002 / no_auto_reply")


def test_llm_pick_template_uses_status_not_gen004():
    """GPT 选行：法文车态描述应落到 STATUS-003，而不是 Intent 表第一行 GEN-004。"""
    templates = load_xlsx()
    from agent.schemas import TemplatePick

    class _FakeLLM:
        def invoke_json(self, **kwargs):
            return TemplatePick(template_id="STATUS-003"), None

    body = (
        "Le véhicule n'est plus visible dans l'application. "
        "Il apparaît offline / extended standby alors que le véhicule est démarré. "
        "VIN: LSJE24098NS028936. France. MG Marvel R."
    )
    chosen = pick_template(
        "app_vehicle_control", templates, body_text=body,
        llm=_FakeLLM(), use_llm=True,
    )
    assert chosen.get("template_id") == "STATUS-003", chosen.get("template_id")
    print("  ✓ LLM 选 STATUS-003（非法文关键词兜底 GEN-004）")


def test_llm_pick_invalid_id_falls_back_to_keywords():
    templates = load_xlsx()
    from agent.schemas import TemplatePick

    class _FakeLLM:
        def invoke_json(self, **kwargs):
            return TemplatePick(template_id="NOT-A-ROW"), None

    chosen = pick_template(
        "app_vehicle_control", templates,
        body_text="The app shows an error but I have no idea which step caused it. No error code visible.",
        llm=_FakeLLM(), use_llm=True,
    )
    assert chosen.get("template_id") == "GEN-004", chosen.get("template_id")
    print("  ✓ LLM 非法 id 回退关键词 GEN-004")


def test_llm_pick_refuses_gen002_when_vin_present():
    templates = load_xlsx()
    from agent.schemas import TemplatePick

    class _FakeLLM:
        def invoke_json(self, **kwargs):
            return TemplatePick(template_id="GEN-002"), None

    vin = "LSJWN4A57NZ123456"
    chosen = pick_template(
        "ismart_data_plan", templates,
        body_text=f"VIN {vin}. How do I upgrade my iSMART data plan / telematics subscription?",
        llm=_FakeLLM(), use_llm=True,
    )
    assert chosen.get("template_id") != "GEN-002", chosen.get("template_id")
    assert chosen.get("template_id") != "PLAN-001"
    print("  ✓ LLM 选 GEN-002 但已有 VIN → 丢弃，且不回 PLAN-001")


_FERNANDO_FOLLOW = """
Hi the details required are below

VIN number - LSJWH4099PN101557
Country - UK (Scotland)
Picture of the screen in the car is attached

Thank you
Rohan

________________________________________
From: customer.care <customer.care@ismart-service.com>
Subject: 回复: Issues with Vehicle or iSmart App
could you please provide the following information:
Vehicle VIN
Country/Region
Photos

________________________________________
发件人: rohan fernando
主题: Issues with Vehicle or iSmart App
• Country/Region:
• Brand:
• Vehicle Model:
• Vehicle Identification Number:
I cannot seem to bind my vehicle to the app. When I go to the user section there is no QR code
"""


def test_latest_turn_strips_quoted_from():
    from agent.template_reply import _latest_customer_turn
    latest = _latest_customer_turn(_FERNANDO_FOLLOW)
    assert "LSJWH4099PN101557" in latest
    assert "no QR code" not in latest
    print("  ✓ 最新一段不含引用里的空表单")


def test_follow_up_with_vin_rejects_gen001():
    templates = load_xlsx()
    from agent.schemas import TemplatePick

    class _FakeLLM:
        def invoke_json(self, **kwargs):
            return TemplatePick(template_id="GEN-001"), None

    chosen = pick_template(
        "follow_up_resolved", templates, body_text=_FERNANDO_FOLLOW,
        llm=_FakeLLM(), use_llm=True,
    )
    assert chosen.get("template_id") != "GEN-001", chosen.get("template_id")
    assert chosen.get("template_id") == "GEN-005", chosen.get("template_id")
    print("  ✓ 跟进已交 VIN → GEN-001 改 GEN-005")


def test_follow_up_menu_includes_bind_when_qr_in_thread():
    from agent.template_reply import _llm_template_candidates
    templates = load_xlsx()
    ids = {t["template_id"] for t in _llm_template_candidates(
        "follow_up_resolved", templates, _FERNANDO_FOLLOW,
    )}
    assert "GEN-001" not in ids
    assert "GEN-005" in ids
    assert any(i.startswith("BIND-") for i in ids), ids
    print("  ✓ 跟进+QR 菜单含 BIND、不含 GEN-001")


def test_follow_up_vague_latest_uses_thread_vin():
    """最新一句很空，VIN/绑车在线程里 → 不要再套 GEN-001。"""
    templates = load_xlsx()
    chosen = pick_template(
        "follow_up_resolved", templates,
        body_text="Please help I have a problem, still not working.",
        thread_context=(
            "[2026-09-01] customer: I cannot bind, no QR code. "
            "VIN LSJWH4099PN101557"
        ),
    )
    assert chosen.get("template_id") != "GEN-001", chosen.get("template_id")
    print("  ✓ 跟进空一句但线程有 VIN → 不套 GEN-001")


def test_cs_asked_for_vin_in_history_not_gen002():
    """客服历史在要 VIN，客户最新一段已经给了 → 不套 GEN-002。"""
    templates = load_xlsx()
    chosen = pick_template(
        "ismart_data_plan", templates,
        body_text=(
            "VIN LSJWH4099PN101557. Please upgrade the data plan.\n"
            "From: customer.care\n"
            "could you please provide the VIN. Where is the VIN?"
        ),
    )
    assert chosen.get("template_id") != "GEN-002", chosen.get("template_id")
    print("  ✓ 引用里客服要 VIN 但最新已给 → 不套 GEN-002")


def test_conversation_vin_note_redacts_vin_past_old_300_clip():
    from agent.outlook_drafts import _conversation_vin_note
    vin = "LSJWH4099PN101557"
    early = "Dear team, " + ("please help. " * 40) + f"VIN {vin} thanks"
    assert early.find(vin) > 300
    note = _conversation_vin_note("", [], [
        {"body_text": early, "is_draft": False},
    ])
    assert "VIN_MASK_" in note, note
    assert vin not in note
    chosen = pick_template(
        "follow_up_resolved", load_xlsx(),
        body_text="Please help I have a problem, still not working.",
        thread_context=note,
    )
    assert chosen.get("template_id") != "GEN-001", chosen.get("template_id")
    print("  ✓ 线程后半段 VIN 抽成 VIN_MASK_ 后不套 GEN-001")


def test_pick_template_update_scope_not_adas_inspection():
    """会不会也随软件升级改善 LKA → FICM-001，不进店检查。"""
    from agent.template_reply import _is_update_scope_consult, _llm_template_candidates

    body = (
        "Hello, VIN LSJWH4099PN101557. We will ask the dealer about the "
        "infotainment update. Will the current or upcoming software update "
        "also improve LKA and traffic sign recognition?"
    )
    assert _is_update_scope_consult(body)
    templates = load_xlsx()
    chosen = pick_template("adas_lka", templates, body_text=body)
    assert chosen.get("template_id") == "FICM-001", chosen.get("template_id")
    menu_ids = {t.get("template_id") for t in _llm_template_candidates(
        "adas_lka", templates, body,
    )}
    assert "ADAS-LKA-001" not in menu_ids
    assert "FICM-001" in menu_ids
    print("  ✓ 升级范围问句 → FICM-001，挡 ADAS-LKA-001")


def test_pick_template_post_update_fault_still_adas():
    """升级之后 LKA 仍跑偏/报警 → 仍走 ADAS 检查行。"""
    from agent.template_reply import _is_update_scope_consult

    body = (
        "VIN LSJWH4099PN101557. After the software update, lane keep assist "
        "still drifts to the left and the warning is on."
    )
    assert not _is_update_scope_consult(body)
    chosen = pick_template("adas_lka", load_xlsx(), body_text=body)
    assert chosen.get("template_id") == "ADAS-LKA-001", chosen.get("template_id")
    print("  ✓ 升级后故障 → ADAS-LKA-001")


def test_pick_template_ota_scope_selects_fota001():
    """下一包 OTA 会不会也带地图 → FOTA-001。"""
    body = (
        "VIN LSJWH4099PN101557. Will the next OTA also include map updates?"
    )
    chosen = pick_template("ismart_fota", load_xlsx(), body_text=body)
    assert chosen.get("template_id") == "FOTA-001", chosen.get("template_id")
    print("  ✓ OTA 范围问句 → FOTA-001")


def test_pick_template_vpn_restriction_not_net001():
    from agent.template_reply import _is_vpn_restriction

    body = (
        "Please remove the VPN restriction on the infotainment so I can "
        "use the vehicle network without a hotspot."
    )
    assert _is_vpn_restriction(body)
    chosen = pick_template("ismart_connectivity", load_xlsx(), body_text=body)
    assert chosen.get("template_id") == "APP-VPN-001", chosen.get("template_id")
    print("  ✓ VPN 限制 → APP-VPN-001 不是 NET-001")


def test_hotspot_month_start_not_vpn_restriction():
    from agent.template_reply import _is_vpn_restriction

    body = (
        "The hotspot stops working on the 1st of the month. Please check "
        "the data plan."
    )
    assert _is_vpn_restriction(body) is False
    chosen = pick_template("ismart_connectivity", load_xlsx(), body_text=body)
    assert chosen.get("template_id") == "NET-001", chosen.get("template_id")
    print("  ✓ 热点/月初流量仍可 NET-001")


def test_pick_template_headunit_off_not_cpaa():
    """中控整屏熄灭 + AA 也灭 → FICM-003，不进删配对。"""
    from agent.template_reply import (
        _is_headunit_display_fault, _llm_template_candidates,
    )

    body = (
        "VIN LSJWH4099PN101557. I bought the MG4 two days ago. The central "
        "screen turns off completely several times a day. Android Auto and "
        "Maps also turn off when that happens."
    )
    assert _is_headunit_display_fault(body)
    templates = load_xlsx()
    chosen = pick_template("ficm_cpaa", templates, body_text=body)
    assert chosen.get("template_id") == "FICM-003", chosen.get("template_id")
    menu_ids = {t.get("template_id") for t in _llm_template_candidates(
        "ficm_cpaa", templates, body,
    )}
    assert "CPAA-001" not in menu_ids
    assert "FICM-003" in menu_ids
    print("  ✓ 中控熄屏 → FICM-003，挡 CPAA-001")


def test_android_auto_disconnect_still_cpaa():
    """只有 AA 断连、屏还在 → 仍 CPAA-001。"""
    from agent.template_reply import _is_headunit_display_fault

    body = (
        "VIN LSJWH4099PN101557. Android Auto turns off after a few minutes "
        "on the USB cable. CarPlay will not connect either. The screen "
        "itself stays on."
    )
    assert _is_headunit_display_fault(body) is False
    chosen = pick_template("ficm_cpaa", load_xlsx(), body_text=body)
    assert chosen.get("template_id") == "CPAA-001", chosen.get("template_id")
    print("  ✓ 仅 AA 断连仍 CPAA-001")


def test_pick_template_sub_account_not_gen005():
    """建副账号失败 → APP-GUIDE-002，不要 GEN-005 无草稿。"""
    from agent.outlook_drafts import Mail, should_skip
    from agent.template_reply import (
        _is_secondary_account_howto, _llm_template_candidates,
    )

    body = (
        "VIN LSJWH4099PN101557. I'm trying to create a sub account for my "
        "spouse but I can't. The member login is user_mask@example.com."
    )
    assert _is_secondary_account_howto(body)
    templates = load_xlsx()
    chosen = pick_template("ismart_account_inquiry", templates, body_text=body)
    assert chosen.get("template_id") == "APP-GUIDE-002", chosen.get("template_id")
    assert "please follow the steps" in (chosen.get("template") or "").lower()
    menu_ids = {t.get("template_id") for t in _llm_template_candidates(
        "ismart_account_inquiry", templates, body,
    )}
    assert "GEN-005" not in menu_ids
    assert "APP-GUIDE-002" in menu_ids
    mail = Mail(
        id="x", subject="profile", sender="a@b.com",
        received_at="2026-01-01", body_text=body, is_read=False, attachments=[],
    )
    reason = should_skip(
        mail,
        {"intent": "ismart_account_inquiry", "urgency": "P2",
         "region": "Europe", "language": "en",
         "requires_recall_check": False, "requires_warranty_lookup": False,
         "red_flags": []},
        chosen, None,
    )
    assert reason is None, reason
    print("  ✓ 副账号 how-to → APP-GUIDE-002，可起草")


def test_digital_manual_not_secondary_account():
    from agent.template_reply import _is_secondary_account_howto

    body = (
        "VIN LSJWH4099PN101557. Where is the digital owner's manual in "
        "the app? I cannot find My Manual."
    )
    assert _is_secondary_account_howto(body) is False
    print("  ✓ 手册问询不是副账号")


def test_review_lock_charging_schedule_not_inspection():
    templates = load_xlsx()
    body = (
        "How can I charge my car during off-peak hours from 1 a.m. to 6 a.m. "
        "Can I put a clock on my electrical outlet?"
    )
    chosen = pick_template("hardware_warranty", templates, body_text=body)
    assert chosen.get("template_id") == "CHG-SCHED-001", chosen.get("template_id")
    print("  ✓ 谷电预约充电不进 CHG-001 检修")


def test_review_lock_charging_power_not_inspection():
    templates = load_xlsx()
    body = (
        "where can I find to set the charging power level? "
        "My hub charges at 3.9kw even if I set the maximum current."
    )
    chosen = pick_template("charging", templates, body_text=body)
    assert chosen.get("template_id") == "NEW-CHGPOWER-001", chosen.get("template_id")
    print("  ✓ 调节充电功率不进 CHG-001")


def test_review_lock_third_party_wallbox_is_chg001():
    templates = load_xlsx()
    body = (
        "Issue Type: Charging Management\n"
        "Description of Issues: ich habe eine neue Wallbox zuhause angebracht "
        "dich wenn ich sie mit dem auto verbinden möchre steht "
        '"verbunden - wird aber nicht geladen". die ladebix ist von der '
        "Firma Ampere und sollte funktionsfähig sein."
    )
    chosen = pick_template("charging", templates, body_text=body)
    assert chosen.get("template_id") == "CHG-001", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "function inspection" in text or "charging port" in text
    assert "ampere customer support" not in text
    sched = pick_template(
        "hardware_warranty", templates,
        body_text=(
            "How can I charge my car during off-peak hours from 1 a.m. to 6 a.m."
        ),
    )
    assert sched.get("template_id") == "CHG-SCHED-001", sched.get("template_id")
    print("  ✓ 第三方 Wallbox 充不上 → CHG-001；谷电 how-to 仍 CHG-SCHED")


def test_review_lock_previous_user_bind001():
    templates = load_xlsx()
    body = "Problem Description: the car is connected to the previous user."
    chosen = pick_template("ismart_feature_inquiry", templates, body_text=body)
    assert chosen.get("template_id") == "BIND-001", chosen.get("template_id")
    print("  ✓ previous user → BIND-001 不是只问 VIN")


def test_review_lock_manual_not_gen002():
    templates = load_xlsx()
    body = (
        "In the iSmart app, the manual for the MG4 EV is not listed when you "
        "filter for vehicle manuals in Europe for 2026."
    )
    chosen = pick_template("ismart_feature_inquiry", templates, body_text=body)
    assert chosen.get("template_id") == "MAN-001", chosen.get("template_id")
    print("  ✓ 找不到手册 → MAN-001 不是 GEN-002")


def test_review_lock_pdf_manual():
    templates = load_xlsx()
    body = (
        "I am driving a 2022 ZS EV and I am unable to download the owner's "
        "manual in German. Please send me a bound copy."
    )
    chosen = pick_template("ismart_user_manual", templates, body_text=body)
    assert chosen.get("template_id") == "MAN-002", chosen.get("template_id")
    print("  ✓ 索要 PDF/装订本 → MAN-002")


def test_review_lock_dk_notice_not_nfc():
    templates = load_xlsx()
    body = (
        "I get a message that the digital key is running and I should stay "
        "active, what does that mean and how can I turn off the message?"
    )
    chosen = pick_template("ismart_digital_key", templates, body_text=body)
    assert chosen.get("template_id") == "KEY-003", chosen.get("template_id")
    assert "screenshot of the message" in (chosen.get("template") or "").lower()
    print("  ✓ 数字钥匙保活通知 → KEY-003")


def test_review_lock_dk_howto_attaches_guide_row():
    """审稿 #15：是什么/怎么激活 → APP-DIGITAL KEY-001（第 68 行 + PDF），不是副账号 8 步。"""
    from agent.outlook_drafts import kb_reply_pdf_paths

    templates = load_xlsx()
    body = (
        "Hello, I don't understand what the digital key is, "
        "how it is activated and how it is deactivated ???"
    )
    chosen = pick_template("ismart_digital_key", templates, body_text=body)
    assert chosen.get("template_id") == "APP-DIGITAL KEY-001", chosen.get("template_id")
    body_tmpl = chosen.get("template") or ""
    assert "\n\nKind regards,\niSMART Service Team" in body_tmpl
    assert "\n\nThank you for contacting us.\n\n" in body_tmpl
    names = [p.name for p in kb_reply_pdf_paths(chosen.get("template_id") or "")]
    assert names == ["Digital Key Function.pdf"], names
    howto = pick_template(
        "ismart_account_inquiry", templates,
        body_text="How do I add an additional user/secondary account to the app?",
    )
    assert howto.get("template_id") == "APP-GUIDE-002", howto.get("template_id")
    assert not kb_reply_pdf_paths("APP-GUIDE-002")
    print("  ✓ 数字钥匙 how-to → 第68行 PDF；副账号 how-to → APP-GUIDE-002 无 PDF")


def test_review_lock_pair_phone_not_bluetooth_call():
    templates = load_xlsx()
    body = "Country: Hungary. I can't pair my phone with my car"
    chosen = pick_template("ficm_bluetooth", templates, body_text=body)
    assert chosen.get("template_id") == "APP-GUIDE-001", chosen.get("template_id")
    print("  ✓ 手机配车 → APP-GUIDE-001 不是 BT-001")


def test_review_lock_paint_not_function_inspection():
    templates = load_xlsx()
    body = (
        "while passing the car to the kärcher I have the paint that has torn "
        "off the front bumper. Is this taken under warranty?"
    )
    chosen = pick_template("charging", templates, body_text=body)
    assert chosen.get("template_id") == "SVC-003", chosen.get("template_id")
    print("  ✓ 漆面保修 → SVC-003 不是功能检修")


def test_review_lock_cruise_howto_not_adas():
    templates = load_xlsx()
    body = (
        "I can't find a way to activate the cruising speed of my vehicle, "
        "only the limiter jumps"
    )
    chosen = pick_template("adas_acc", templates, body_text=body)
    assert chosen.get("template_id") == "CRUISE-001", chosen.get("template_id")
    assert "user manual" in (chosen.get("template") or "").lower()
    print("  ✓ 如何开巡航 → CRUISE-001")


def test_review_lock_towbar_acc_still_inspection():
    from agent.template_reply import _is_cruise_howto

    body = (
        "VIN LSJWH4099SN021250. an original hitch was mounted. unfortunately "
        "the ACC disabled when used with the bike rack"
    )
    assert _is_cruise_howto(body) is False
    templates = load_xlsx()
    chosen = pick_template("adas_acc", templates, body_text=body)
    assert chosen.get("template_id") in {"ADAS-ACC-001", "NON-003"}, (
        chosen.get("template_id")
    )
    print("  ✓ 拖车钩关闭 ACC 仍走检测")


def test_review_lock_wife_invitation_secondary():
    templates = load_xlsx()
    body = (
        "I don't receive the invitation from my wife's car, to be able to "
        "link to the App"
    )
    chosen = pick_template("app_binding", templates, body_text=body)
    assert chosen.get("template_id") == "APP-GUIDE-002", chosen.get("template_id")
    print("  ✓ 妻子车辆邀请 → APP-GUIDE-002")


def test_review_lock_secondary_not_registered_is_guide003():
    """App 报未注册 → APP-GUIDE-003 第三列，不要 8 步。"""
    templates = load_xlsx()
    body = (
        "I get an error whenever I try to add a secondary account. "
        "My wife is registered and when I authorize it she gives me an error. "
        "It says that she is not registered."
    )
    chosen = pick_template("ismart_account_inquiry", templates, body_text=body)
    assert chosen.get("template_id") == "APP-GUIDE-003", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "backend system" in text
    assert "please follow the steps" not in text
    print("  ✓ 副账号报未注册 → APP-GUIDE-003")


def test_review_lock_tyre_not_body_inspection():
    templates = load_xlsx()
    body = (
        "The app shows that my tyre pressure is low. Should I take the car "
        "to an official MG dealer, or can I go to any tyre shop?"
    )
    chosen = pick_template("hardware_warranty", templates, body_text=body)
    assert chosen.get("template_id") == "TYRE-001", chosen.get("template_id")
    text = chosen.get("template") or ""
    assert "tyre shop" in text.lower()
    print("  ✓ 胎压去哪修 → TYRE-001")


def test_review_lock_address_not_network_only():
    templates = load_xlsx()
    body = "Hello, please change my address: Christine-Koch-Weg 9, Wickede"
    chosen = pick_template("app_account", templates, body_text=body)
    from agent.template_reply import _acc006_handles_address
    if _acc006_handles_address(templates):
        assert chosen.get("template_id") == "ACC-006", chosen.get("template_id")
        assert "unable to process address" in (chosen.get("template") or "").lower()
    else:
        assert chosen.get("template_id") != "ACC-006", chosen.get("template_id")
    print("  ✓ 改地址")


def test_review_lock_engine_stop_not_app_warning():
    templates = load_xlsx()
    body = (
        "Yesterday on the highway the combustion engine stopped working. "
        "The tow truck took it. Does the warranty cover the repair 100%?"
    )
    chosen = pick_template("app_vehicle_control", templates, body_text=body)
    assert chosen.get("template_id") == "SVC-PWR-001", chosen.get("template_id")
    text = chosen.get("template") or ""
    assert "roadside" in text.lower()
    assert "100%" in text
    print("  ✓ 发动机中断 → SVC-PWR-001")


def test_updated_templates_answer_review_cases():
    templates = load_xlsx()
    by_id = {t.get("template_id"): t["template"].lower() for t in templates}
    assert "external clock or timer" in by_id["CHG-SCHED-001"]
    assert "two-hour offset" in by_id["CHG-SCHED-001"]
    assert "charging power" in by_id["NEW-CHGPOWER-001"]
    assert "changed your phone" in by_id["ACC-005"] or "new phone" in by_id["ACC-005"]
    text_002 = by_id["APP-GUIDE-002"]
    assert "please follow the steps" in text_002
    assert "tap the" in text_002
    text_003 = by_id["APP-GUIDE-003"]
    assert "backend system" in text_003
    assert "email address or phone number" in text_003
    assert "please follow the steps" not in text_003
    assert "user manual" in by_id["MAN-001"]
    assert "pdf" in by_id["MAN-002"]
    assert "DK-001" not in by_id
    assert "FICM-PRIV-002" not in by_id
    assert "STATUS-010" not in by_id
    assert "PLAN-001" not in by_id
    assert "NET-PLAN-001" not in by_id
    assert "digital key" in by_id["APP-DIGITAL KEY-001"]
    assert "DK-002" in by_id
    assert by_id.get("KEY-003") and "stay active" in by_id["KEY-003"]
    assert "forgot password" in by_id["ACC-002"]
    assert "attachment" in by_id["BIND-003"]
    assert "after-sales service process" in by_id["SVC-001"] or "local mg dealer" in by_id["SVC-001"]
    assert "privacy" in by_id["FICM-PRIV-003"]
    assert "without a vpn" in by_id["APP-VPN-001"]
    assert "italian" in by_id["MAP-001"] or "not currently available" in by_id["MAP-001"]
    assert "function check" in by_id["FICM-004"]
    assert "map data" in by_id["MAP-006"] or "navigation system" in by_id["MAP-006"]
    print("  ✓ 审稿场景模板正文已补齐")


def test_review_lock_parts_buy_not_gen002():
    templates = load_xlsx()
    body = "hello, I wanted to know where it is I can buy a front mud passenger side thank you"
    chosen = pick_template("hardware_warranty", templates, body_text=body)
    assert chosen.get("template_id") == "SVC-003", chosen.get("template_id")
    print("  ✓ 买配件 → SVC-003 不是 GEN-002")


def test_review_lock_forgot_password_not_gen001():
    templates = load_xlsx()
    body = "Description of problems: I forgot lonin and password"
    chosen = pick_template("app_account", templates, body_text=body)
    assert chosen.get("template_id") == "ACC-002", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "forgot password" in text
    assert "17-character vin" not in text
    print("  ✓ 忘记密码 → ACC-002 不补齐信息")


def test_review_lock_french_forgot_password_acc002():
    templates = load_xlsx()
    body = (
        "Cher service iSmart,\n"
        "Type de problème: Mon profil\n"
        "Description des problèmes: bonjour je souhaiterais savoir comment on fait "
        "pour modifier le mot de passe de l'application je me souviens plus de celui "
        "que j'ai mis est-ce qu'on peut le récupérer par la bonne mail\n"
    )
    chosen = pick_template("app_account", templates, body_text=body)
    assert chosen.get("template_id") == "ACC-002", chosen.get("template_id")
    print("  ✓ 法语忘记密码 → ACC-002")


def test_review_lock_bind_howto_pdf_not_vin_only():
    templates = load_xlsx()
    body = (
        "I would like to connect the car to the MG iSmart app "
        "how can I proceed"
    )
    chosen = pick_template("app_binding", templates, body_text=body)
    assert chosen.get("template_id") in {"APP-GUIDE-001", "BIND-003"}, chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "binding instructions" in text or "attachment" in text
    print("  ✓ 绑车 how-to → 教程+PDF 不是只要 VIN")


def test_review_lock_privacy_notice_not_status():
    templates = load_xlsx()
    body = (
        "the app is not working and says my car is not responding. "
        "There is a red message that says privacy needs to be agreed"
    )
    chosen = pick_template("ismart_privacy", templates, body_text=body)
    assert chosen.get("template_id") == "FICM-PRIV-003", chosen.get("template_id")
    print("  ✓ 反复隐私协议 → FICM-PRIV-003")


def test_review_lock_privacy_denied_used_car_not_status003():
    templates = load_xlsx()
    en = (
        "I have just acquired a car and cannot connect it to the ISmart "
        "application. The app indicates a privacy notice denied issue, "
        "asking to accept the settings through the vehicle infotainment. "
        "Despite the parameters accepted in the application and in the car, "
        "the problem is not resolved. Used car VIN VIN_MASK_90171648."
    )
    chosen = pick_template("app_binding", templates, body_text=en)
    assert chosen.get("template_id") == "FICM-PRIV-003", chosen.get("template_id")
    fr = (
        "je viens d'acquérir une voiture et ne parviens pas à la connecter "
        "à l'application ISmart. l'application indique un problème "
        "avis de confidentialité refusé, demandant d'accepter les paramètres "
        "via le système d'infodivertissement."
    )
    chosen_fr = pick_template("ficm_cpaa", templates, body_text=fr)
    assert chosen_fr.get("template_id") == "FICM-PRIV-003", chosen_fr.get("template_id")
    print("  ✓ privacy notice denied / avis refusé → FICM-PRIV-003 不是 STATUS-003")


def test_review_lock_service_history_not_privacy_footer():
    templates = load_xlsx()
    body = (
        "Dear iSmart service team,\n"
        "Description of Issues: please, how can I find my cars service history?\n"
        "I can read the latest Privacy Notice for more details of the data "
        "processing activities. I can find in the Privacy Notice at "
        "https://page.soimt.com/soimt/privacy/en-GB_privacy.html\n"
        "By sending this email, I consent to the data processing.\n"
        "Please how can I find my cars service history?\n"
        "Kind Regards\nAndy"
    )
    chosen = pick_template("hardware_warranty", templates, body_text=body)
    assert chosen.get("template_id") == "SVC-001", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "privacy notice" not in text
    assert "dealer" in text or "after-sales" in text or "service document" in text
    print("  ✓ 查保养记录 → SVC-001 不是表单 Privacy 页脚")


def test_webform_privacy_footer_never_picks_ficm_priv():
    templates = load_xlsx()
    footer = (
        "\nI can read the latest Privacy Notice for more details of the data "
        "processing activities. I can find in the Privacy Notice at "
        "https://page.soimt.com/soimt/privacy/en-GB_privacy.html\n"
        "By sending this email, I consent to the data processing.\n"
        "I can also contact the privacy office as written in the Privacy Notice."
    )
    tyre = (
        "Description of Issues: the car tyre warning has been on, which we "
        "pumped, but it's still showing tyre pressure low for the 4 tyres"
        + footer
    )
    chosen = pick_template("hardware_warranty", templates, body_text=tyre)
    assert chosen.get("template_id") != "FICM-PRIV-002", chosen.get("template_id")
    assert chosen.get("template_id") != "FICM-PRIV-003", chosen.get("template_id")
    from agent.template_reply import draft_semantic_mismatch_reason
    assert draft_semantic_mismatch_reason(
        tyre, "FICM-PRIV-003", "repeated Privacy Notice messages",
        llm=None, use_llm=False,
    )
    print("  ✓ 表单页脚 Privacy Notice 不进 FICM-PRIV-003")


def test_parked_headlights_footer_not_privacy():
    """停车闪灯 + 表单 Privacy 页脚 → 不是 FICM-PRIV-002。"""
    templates = load_xlsx()
    body = (
        "Country/Region: United Kingdom\n"
        "Brand: MG\n"
        "Vehicle Model: ZS EV\n"
        "Vehicle Identification Number: LSJWAAAAAAAAAAAAA\n"
        "Issue Type: My Vehicle\n"
        "Description of Issues: The headlights flash periodically when car "
        "is locked and parked in driveway. Battery voltage shows 12.8 in "
        "the app and cannot see any abnormalities.\n"
        "I can read the latest Privacy Notice for more details of the data "
        "processing activities. I can find in the Privacy Notice at "
        "https://page.soimt.com/soimt/privacy/en-GB_privacy.html\n"
        "By sending this email, I consent to the data processing.\n"
    )
    chosen = pick_template("ismart_privacy", templates, body_text=body)
    assert chosen.get("template_id") != "FICM-PRIV-002", chosen.get("template_id")
    assert chosen.get("template_id") != "FICM-PRIV-003", chosen.get("template_id")
    from agent.template_reply import _is_privacy_notice_loop
    assert _is_privacy_notice_loop(body) is False
    print("  ✓ 停车闪灯+页脚不是隐私故障", chosen.get("template_id"))


def test_webform_vehicle_management_dropdown_not_bind006():
    templates = load_xlsx()
    body = (
        "Dear iSmart Service Team,\n"
        "• Country/Region: Israel\n"
        "• Brand:\n"
        "• Car Model:\n"
        "• Vehicle Identification Number: LSJE24000PG000001\n"
        "• Problem Type: Vehicle Management\n"
        "• Problem description: Doesn't connect to the app\n"
    )
    chosen = pick_template("app_vehicle_control", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-003", chosen.get("template_id")
    from agent.template_reply import draft_semantic_mismatch_reason
    assert draft_semantic_mismatch_reason(
        body, "BIND-007", "Vehicle Management page steps",
        llm=None, use_llm=False,
    )
    real = (
        "Description of Issues: where is vehicle management in the app "
        "so I can delete the old car"
    )
    mgmt = pick_template("app_binding", templates, body_text=real)
    assert mgmt.get("template_id") == "BIND-007", mgmt.get("template_id")
    print("  ✓ 表单 Vehicle Management 下拉不是 BIND-007")


def test_spanish_self_unbind_bind007_not_bind001():
    templates = load_xlsx()
    body = (
        "Descripción de los problemas: No puedo desvincular men MG i-SMRT, "
        "para poder volver a vincular\n"
    )
    chosen = pick_template("app_binding", templates, body_text=body)
    assert chosen.get("template_id") == "BIND-007", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "vehicle management" in text
    assert "72 hours" not in text
    print("  ✓ 西语自己解绑重绑 → BIND-007 不是 BIND-001")


def test_merge_in_car_internet_and_carplay():
    templates = load_xlsx()
    body = (
        "Dear iSmart service team,\n"
        "• Country/Region: Denmark\n"
        "• Brand: MG\n"
        "• Vehicle model: MGS6 EV\n"
        "• Vehicle identification number: LSJWX4092TN000001\n"
        "• Problem Type: Other\n"
        "• Description of problems: My internet in the car itself does not "
        "work and Apple CarPlay does not work optimally jumps on and off\n"
    )
    chosen = pick_template("ficm_cpaa", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-003+CPAA-001", chosen.get(
        "template_id"
    )
    text = chosen.get("template") or ""
    assert "open area with stable network coverage" in text
    assert "refresh the vehicle status in the iSMART App" in text
    assert "delete the existing CarPlay/Android Auto" in text
    assert "original or certified USB cable" in text
    assert "Thank you for your cooperation." in text
    assert "wireless connection, availability depends" not in text.lower()
    from agent.template_reply import draft_semantic_mismatch_reason
    assert draft_semantic_mismatch_reason(
        body, chosen["template_id"], text, llm=None, use_llm=False,
    ) is None
    print("  ✓ 车机没网 + CarPlay 合写 STATUS-003+CPAA-001")


def test_compose_skips_no_auto_lane_keeps_match():
    from agent.template_reply import compose_merged_template
    templates = load_xlsx()
    out = compose_merged_template(templates, [
        {
            "tid": "STATUS-003",
            "label": "",
            "body": "Please first move the vehicle to an open area.",
        },
        {"tid": "BIND-002", "label": "", "body": "backend unbind"},
    ])
    assert out is not None
    assert out.get("template_id") == "STATUS-003", out.get("template_id")
    print("  ✓ 合写跳过 no_auto 行，留下能套上的")


def test_partial_cover_forgot_password_plus_unmatched_ask():
    templates = load_xlsx()
    body = (
        "Description of problems: I forgot lonin and password. "
        "Also what is SAIC's share price tomorrow."
    )
    chosen = pick_template("app_account", templates, body_text=body)
    assert chosen.get("template_id") == "ACC-002", chosen.get("template_id")
    from agent.template_reply import draft_semantic_mismatch_reason
    assert draft_semantic_mismatch_reason(
        body, "ACC-002", chosen.get("template") or "",
        llm=None, use_llm=False,
    ) is None
    print("  ✓ 有模板的先回，对不上的问不拦草稿")


def test_partial_cover_aftermarket_plus_wireless_aa_update():
    """改装 + 无线 AA 升级问句 → NON-010+FICM-001 合写。"""
    templates = load_xlsx()
    body = (
        "Descrizione dei problemi:\n"
        "1: Vorrei montare il pedal sprint, accessorio plug and play, "
        "non taglia cavi, posso farlo?\n"
        "2: Poi vi chiedo anche se c'è un aggiornamento per avere "
        "android auto senza cavi.\n"
    )
    chosen = pick_template("hardware_warranty", templates, body_text=body)
    assert chosen.get("template_id") == "NON-010+FICM-001", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "manufacturer" in text or "customer support" in text
    assert "wireless android auto" in text or "mg dealer" in text
    assert "software version" not in text
    from agent.template_reply import draft_semantic_mismatch_reason
    assert draft_semantic_mismatch_reason(
        body, chosen.get("template_id") or "", chosen.get("template") or "",
        llm=None, use_llm=False,
    ) is None
    print("  ✓ 改装+无线AA升级合写 NON-010+FICM-001")


def test_aftermarket_accessory_only_non010():
    from agent.mail_tags import extract_known_issue_type
    templates = load_xlsx()
    body = (
        "I would like to mount the pedal sprint, a plug and play accessory. "
        "It does not cut cables. Can I install it?"
    )
    chosen = pick_template("hardware_warranty", templates, body_text=body)
    assert chosen.get("template_id") == "NON-010", chosen.get("template_id")
    assert extract_known_issue_type(body) == "非iSMART-功能咨询"
    print("  ✓ 仅第三方加装 → NON-010 非iSMART功能咨询")


def test_merge_app_connect_and_infotainment_update():
    templates = load_xlsx()
    body = (
        "VIN LSJWX4092TN000001. The app doesn't connect to the car. "
        "Is there an infotainment update for wireless Android Auto?"
    )
    chosen = pick_template("ismart_connectivity", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-003+FICM-001", chosen.get(
        "template_id"
    )
    text = (chosen.get("template") or "").lower()
    assert "open area with stable network coverage" in text
    assert "software version" in text or "mg dealer" in text
    print("  ✓ 连不上 + 车机升级问句合写 STATUS-003+FICM-001")


def test_carplay_only_not_merged_with_status():
    templates = load_xlsx()
    body = "Android Auto disconnects after a few minutes. The screen stays on."
    chosen = pick_template("ficm_cpaa", templates, body_text=body)
    assert chosen.get("template_id") == "CPAA-001", chosen.get("template_id")
    print("  ✓ 仅 AA 断连不合写 STATUS-003")


def test_merge_app_connect_and_carplay():
    templates = load_xlsx()
    body = (
        "VIN LSJWX4092TN000001. The app doesn't connect to the car and "
        "Apple CarPlay disconnects after a few minutes."
    )
    chosen = pick_template("ismart_connectivity", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-003+CPAA-001", chosen.get(
        "template_id"
    )
    text = chosen.get("template") or ""
    assert "open area with stable network coverage" in text
    assert "delete the existing CarPlay/Android Auto" in text
    print("  ✓ App 连不上 + CarPlay 合写")


def test_merge_net001_and_carplay():
    templates = load_xlsx()
    body = (
        "VIN LSJWX4092TN000001. The car shows 4G but YouTube and weather "
        "are unavailable, and CarPlay jumps on and off."
    )
    chosen = pick_template("ficm_cpaa", templates, body_text=body)
    assert chosen.get("template_id") == "NET-001+CPAA-001", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "delete the existing carplay" in text
    assert "youtube" in text or "hotspot" in text or "network" in text
    print("  ✓ 车机在线服务 + CarPlay 合写 NET-001")


def test_merge_hotspot_and_carplay():
    templates = load_xlsx()
    body = (
        "VIN LSJWX4092TN000001. Vehicle hotspot not found and Apple "
        "CarPlay disconnects after a few minutes."
    )
    chosen = pick_template("ficm_cpaa", templates, body_text=body)
    assert chosen.get("template_id") == "NET-001+CPAA-001", chosen.get("template_id")
    print("  ✓ 热点找不到 + CarPlay 合写 NET-001")


def test_merge_german_in_car_internet_and_carplay():
    templates = load_xlsx()
    body = (
        "VIN LSJWX4092TN000001. Das Internet im Auto selbst funktioniert "
        "nicht und Apple CarPlay springt an und aus."
    )
    chosen = pick_template("ficm_cpaa", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-003+CPAA-001", chosen.get(
        "template_id"
    )
    print("  ✓ 德语车机没网 + CarPlay 合写")


def test_bind_and_carplay_still_bind_howto():
    """绑车教程 + CarPlay 仍先走绑车硬闸，不合写（不是同级车机故障）。"""
    templates = load_xlsx()
    body = (
        "VIN LSJWX4092TN000001. How do I bind the car to the iSMART App? "
        "Also Apple CarPlay will not connect."
    )
    chosen = pick_template("app_binding", templates, body_text=body)
    assert chosen.get("template_id") == "APP-GUIDE-001", chosen.get("template_id")
    print("  ✓ 绑车+CarPlay 仍 APP-GUIDE-001")


def test_connect_with_vin_not_gen005():
    from agent.template_reply import _llm_template_candidates
    templates = load_xlsx()
    vin = "LSJWH4095RS123456"
    bodies = [
        f"VIN {vin}. Die App verbindet nicht mit dem Auto.",
        f"VIN {vin}. L'application ne se connecte pas au véhicule.",
        f"VIN {vin}. I cannot see the vehicle in the iSmart app.",
    ]
    from agent.schemas import TemplatePick

    class _FakeGEN:
        def invoke_json(self, **kwargs):
            return TemplatePick(template_id="GEN-005"), None

    for body in bodies:
        chosen = pick_template(
            "ismart_feature_inquiry", templates, body_text=body, use_llm=False,
        )
        assert chosen.get("template_id") == "STATUS-003", (
            body, chosen.get("template_id")
        )
        forced = pick_template(
            "ismart_feature_inquiry", templates, body_text=body,
            llm=_FakeGEN(), use_llm=True,
        )
        assert forced.get("template_id") == "STATUS-003", (
            body, forced.get("template_id")
        )
        menu = {
            t.get("template_id")
            for t in _llm_template_candidates(
                "ismart_feature_inquiry", templates, body,
            )
        }
        assert "GEN-005" not in menu, menu
    print("  ✓ 连不上+VIN 不走 GEN-005")


def test_since_date_in_car_internet_not_gen005():
    templates = load_xlsx()
    body = (
        "VIN LSJWH4095RS123456. No internet in the car infotainment "
        "since 03/09. Please help."
    )
    chosen = pick_template("ismart_connectivity", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-003", chosen.get("template_id")
    assert chosen.get("level") != "no_auto_reply", chosen.get("level")
    print("  ✓ 仅 since 日期的车机没网不是 GEN-005")


def test_tbox_keyword_not_replaced_is_status003():
    templates = load_xlsx()
    from agent.schemas import TemplatePick
    from agent.template_reply import _llm_template_candidates

    body = (
        "VIN LSJWH4095RS123456. The iSmart app has no connection. "
        "Forums mention TBox / TCU / telematics."
    )

    class _FakeTbox:
        def invoke_json(self, **kwargs):
            return TemplatePick(template_id="TBOX-001"), None

    chosen = pick_template(
        "ismart_connectivity", templates, body_text=body, use_llm=False,
    )
    assert chosen.get("template_id") == "STATUS-003", chosen.get("template_id")
    assert chosen.get("level") != "no_auto_reply", chosen.get("level")
    forced = pick_template(
        "ismart_connectivity", templates, body_text=body,
        llm=_FakeTbox(), use_llm=True,
    )
    assert forced.get("template_id") == "STATUS-003", forced.get("template_id")
    menu = {
        t.get("template_id")
        for t in _llm_template_candidates("ismart_connectivity", templates, body)
    }
    assert "TBOX-001" not in menu
    print("  ✓ 点名 TBox 未换件 → STATUS-003")


def test_tbox_replaced_still_tbox001():
    templates = load_xlsx()
    body = (
        "VIN LSJWH4095RS123456. The dealer replaced the TBox yesterday "
        "and I still cannot bind the car in the app."
    )
    chosen = pick_template("ismart_connectivity", templates, body_text=body)
    assert chosen.get("template_id") == "TBOX-001", chosen.get("template_id")
    print("  ✓ 已换 TBox 仍绑不上 → TBOX-001")


def test_review_lock_3g_plan_not_net001():
    from agent.template_reply import ismart_plan_or_3g_needs_human
    from agent.outlook_drafts import (
        GraphClient, Mail, process_mail, skip_needs_human_queue,
    )
    from unittest.mock import MagicMock

    templates = load_xlsx()
    body = (
        "I can no longer connect to my car, the screen remains in 3G. "
        "How do I reactivate my iSmart plan?"
    )
    assert ismart_plan_or_3g_needs_human(body) is True
    chosen = pick_template("ismart_data_plan", templates, body_text=body)
    assert chosen is None or chosen.get("template_id") not in {
        "NET-001", "NET-PLAN-001",
    }
    g = MagicMock(spec=GraphClient)
    g.create_draft_reply = MagicMock()
    g.get_conversation_thread = MagicMock(return_value=[])
    mail = Mail(
        id="m-3g", subject="3G plan", sender="a@b.com",
        received_at="2026-09-18T10:00:00Z", body_text=body, is_read=False,
        conversation_id="c-3g",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=templates, allowlist=None,
        dry_run=True, use_llm=False, router=None, dedup=None,
    )
    assert rec.get("skipped") == "ismart_plan_or_3g_needs_human", rec
    assert skip_needs_human_queue(rec.get("skipped"))
    g.create_draft_reply.assert_not_called()
    print("  ✓ 3G/套餐 → 转人工，不是 NET-001")


def test_fraud_force_unbind_needs_human_not_gen001_or_bind001():
    """冒用/非授权 → 转人工；禁 GEN-001 / BIND-001。前车主仍 BIND-001。"""
    from agent.template_reply import fraud_force_unbind_needs_human
    from agent.outlook_drafts import (
        GraphClient, Mail, process_mail, skip_needs_human_queue,
    )
    from unittest.mock import MagicMock

    templates = load_xlsx()
    # 正例：罗马尼亚冒用 + 要求解绑（cotoiiulian 类）
    ro = (
        "Țară/Regiune: Romania\n"
        "Marcă: MG\n"
        "Model vehicul: ZS\n"
        "Număr de identificare vehicul: LSJWS439XSZ257833\n"
        "Tip problemă: Autentificare și deconectare\n"
        "Descrierea problemelor: Doresc deconectarea contului asociat cu "
        "masina in acest moment. deoarece persoana care este conectata "
        "fraudulos la acest moment imi creaza disconfort."
    )
    assert fraud_force_unbind_needs_human(ro) is True
    chosen = pick_template("app_binding", templates, body_text=ro)
    assert chosen is None or chosen.get("template_id") not in {
        "GEN-001", "GEN-002", "BIND-001", "BIND-007",
    }
    g = MagicMock(spec=GraphClient)
    g.create_draft_reply = MagicMock()
    g.get_conversation_thread = MagicMock(return_value=[])
    mail = Mail(
        id="m-fraud", subject="Probleme iSmart", sender="a@b.com",
        received_at="2026-09-15T13:00:00Z", body_text=ro, is_read=False,
        conversation_id="c-fraud",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=templates, allowlist=None,
        dry_run=True, use_llm=False, router=None, dedup=None,
    )
    assert rec.get("skipped") == "fraud_force_unbind_needs_human", rec
    assert skip_needs_human_queue(rec.get("skipped"))
    g.create_draft_reply.assert_not_called()

    en = (
        "Country/Region: Romania\nBrand: MG\nModel: ZS\n"
        "VIN: LSJWS439XSZ257833\n"
        "Someone fraudulently connected to my car account. "
        "Please force unbind / disconnect that account from the vehicle."
    )
    assert fraud_force_unbind_needs_human(en) is True
    assert pick_template("app_binding", templates, body_text=en) is None

    # 反例：前车主仍 BIND-001
    prev = "Problem Description: the car is connected to the previous user."
    assert fraud_force_unbind_needs_human(prev) is False
    prev_hit = pick_template("app_binding", templates, body_text=prev)
    assert prev_hit.get("template_id") == "BIND-001", prev_hit.get("template_id")

    # 反例：自己解绑重绑仍 BIND-007
    self_ub = (
        "no puedo desvincular el vehiculo de mi cuenta y volver a vincular"
    )
    assert fraud_force_unbind_needs_human(self_ub) is False
    print("  ✓ 冒用强制解绑 → 转人工；前车主仍 BIND-001")


def test_review920_maintenance_history_not_remaining_days():
    templates = load_xlsx()
    body = (
        "Country/Region: France\nBrand: MG\nVehicle Model: MG3\n"
        "VIN: LSJWP4394RZ231479\n"
        "Description of problems: Would you have the maintenance history "
        "of my vehicle?"
    )
    chosen = pick_template("delivery_handover", templates, body_text=body)
    assert chosen.get("template_id") == "SVC-001", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "unable to access" in text or "maintenance" in text
    assert "remaining service" not in text
    print("  ✓ maintenance history → SVC-001 历史记录，不是剩余保养天数")


def test_review920_emission_warning_not_app_attributed():
    templates = load_xlsx()
    body = (
        "Country/Region: Portugal\nBrand: MG\nVIN: LSJWS4392TZ143228\n"
        "The engine emissions fault warning has appeared. "
        "Where can I go to check the fault?"
    )
    chosen = pick_template("hardware_warranty", templates, body_text=body)
    assert chosen.get("template_id") == "NON-004", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "shown in the app" not in text
    assert "dealer" in text or "function check" in text
    print("  ✓ 排放警告 → NON-004，不归因 App")


def test_review920_app_not_responding_status003():
    templates = load_xlsx()
    body = (
        "Country/Region: Italy\nBrand: MG\nVIN: LSJW94397SG076874\n"
        "Problem description: Problems with the application not responding"
    )
    chosen = pick_template("app_vehicle_control", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-003", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "open area" in text or "stable network" in text
    assert "screenshot" not in text
    print("  ✓ app not responding → STATUS-003")


def test_remote_ui_stuck_updating_is_status003_not_gen002():
    templates = load_xlsx()
    body = (
        "Hello, I am the owner of an MG ZS Hybrid+.\n"
        "The connection between the app and the car worked perfectly until "
        "a few days ago. Now it always remains in update mode both on the "
        "screen to remotely open and close the car, and on the vehicle "
        "status display screen.\n"
        "I tried to open the car from the app, but the Unlock icon always "
        "remained running despite the car was already open, and Lock failed "
        "because the open command was still running."
    )
    chosen = pick_template("app_vehicle_control", templates, body_text=body)
    assert chosen and chosen.get("template_id") == "STATUS-003", (
        None if not chosen else chosen.get("template_id")
    )
    text = (chosen.get("template") or "").lower()
    assert "open area" in text or "stable network" in text or "head unit" in text
    from agent.mail_tags import customer_issue_l2_for_audit
    assert customer_issue_l2_for_audit(body) == "App-车控车态相关"
    it = (
        "adesso rimane sempre in modalità aggiornamento sia nella schermata "
        "per aprire e chiudere in remoto l'auto. l'icona del lucchetto aperto "
        "(Sbloccare) rimaneva sempre in esecuzione e il comando di apertura "
        "era ancora in esecuzione."
    )
    chosen_it = pick_template("app_vehicle_control", templates, body_text=it)
    assert chosen_it and chosen_it.get("template_id") == "STATUS-003", (
        None if not chosen_it else chosen_it.get("template_id")
    )
    assert customer_issue_l2_for_audit(it) == "App-车控车态相关"
    print("  ✓ 远控一直 update mode → STATUS-003，标签车控车态")


def test_review920_prev_owner_gps_confirm_unbind():
    templates = load_xlsx()
    body = (
        "Country/Region: Denmark\n"
        "Description of problems: I can't update the GPS because the "
        "previous owner has locked it. Have tried to get him to "
        "delete/release the car countless times. What do I do?"
    )
    chosen = pick_template("app_binding", templates, body_text=body)
    assert chosen.get("template_id") == "BIND-001", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "confirm" in text and "previous owner" in text
    assert "gps" in text or "map" in text
    assert "17-character vin" in text or "vin" in text
    # 不要变成纯 GEN-002
    assert "where can i find" not in text
    print("  ✓ 前车主锁 GPS → 确认解绑帮助 + 网络刷新")


def test_review_lock_italian_nav_not_voice_assistant():
    templates = load_xlsx()
    body = (
        "the navigator only in English, voice commands in English. "
        "Hybrid+ has everything in Italian."
    )
    chosen = pick_template("ficm_voice", templates, body_text=body)
    assert chosen.get("template_id") == "MAP-001", chosen.get("template_id")
    print("  ✓ 导航意大利语 → MAP-001")


def test_review_lock_secondhand_warranty_not_appservice():
    templates = load_xlsx()
    body = (
        "I have recently purchased my MG ZS second hand. "
        "Do I need to register my details for the warranty and am I able "
        "to check if there was any kind of outstanding service plan "
        "already on the vehicle?"
    )
    chosen = pick_template("delivery_handover", templates, body_text=body)
    assert chosen.get("template_id") == "SVC-001", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "dealer" in text or "after-sales" in text or "service document" in text
    assert "recorded your feedback regarding service booking" not in text
    print("  ✓ 二手保修/服务计划 → SVC-001 不是 NEW-SERVICE-001")


def test_review_lock_aa_infotainment_fault_not_cpaa():
    templates = load_xlsx()
    body = (
        "when I use the mobile phone with Android auto to strong slowdowns "
        "on the steering wheel controls. the on-board computer which is on "
        "the right restarts. the map of the car's navigator also crashed "
        "and the speed limit detection no longer even worked."
    )
    chosen = pick_template("ficm_cpaa", templates, body_text=body)
    assert chosen.get("template_id") == "FICM-004", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "function check" in text
    assert "pairing records" not in text
    print("  ✓ AA 车机卡顿重启 → FICM-004 不是 CPAA")


def test_review_lock_map_quality_feedback_not_version_shot():
    templates = load_xlsx()
    body = (
        "the GPS has serious shortcomings. Do you envisage an update soon "
        "for the mapping, the search for streets and the updated "
        "implementation of charging stations."
    )
    chosen = pick_template("ficm_map", templates, body_text=body)
    assert chosen.get("template_id") == "MAP-006", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "dealer" in text
    assert "current infotainment software version displayed" not in text
    print("  ✓ 地图/充电桩反馈 → MAP-006 不是 FICM-001")


def test_review_lock_maintenance_alert_logo():
    templates = load_xlsx()
    body = (
        "could you tell me if the vehicle will display a maintenance alert "
        "logo and if so what is the delay? (km and days remaining)"
    )
    chosen = pick_template("ismart_feature_inquiry", templates, body_text=body)
    assert chosen.get("template_id") == "SVC-001", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "remaining service" in text or "dealer" in text or "after-sales" in text or "service document" in text
    print("  ✓ 保养提醒标识 → SVC-001")


def test_review_lock_parked_not_responded_not_tbox():
    templates = load_xlsx()
    body = (
        'When the car is parked and not connected to a charger, the app '
        'shows "The vehicle did not respond." After unlocking or starting '
        "the car, the connection usually returns."
    )
    chosen = pick_template("ismart_connectivity", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-002", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "unlock or start" in text or "low-power" in text
    print("  ✓ 停车未响应 → STATUS-002 不是 TBOX-001")


def test_review_lock_app_color_mismatch():
    templates = load_xlsx()
    body = (
        'Why does the MG iSMART app show the vehicle color as "Flare Red" '
        'when the official vehicle color is called "Diamond Red"?'
    )
    chosen = pick_template("ismart_feature_inquiry", templates, body_text=body)
    assert chosen.get("template_id") == "APP-BODYIMG-001", chosen.get("template_id")
    print("  ✓ App 车色不符 → APP-BODYIMG-001")


def test_review_lock_energy_stats_not_vin_only():
    templates = load_xlsx()
    body = (
        "the app does not provide overall average energy consumption "
        "since the first day of use"
    )
    chosen = pick_template("ismart_feature_inquiry", templates, body_text=body)
    assert chosen.get("template_id") == "APP-ENERGY-001", chosen.get("template_id")
    print("  ✓ 能耗统计 → APP-ENERGY-001")


def test_review_lock_address_beats_vague_gen001():
    templates = load_xlsx()
    body = (
        "please change my address: Carina Knese, Christine-Koch-Weg 9. "
        "I would also like to ask why the weather forecast is currently "
        "not working in the car."
    )
    chosen = pick_template("ismart_connectivity", templates, body_text=body)
    from agent.template_reply import _acc006_handles_address
    if _acc006_handles_address(templates):
        assert chosen.get("template_id") == "ACC-006+NET-001", chosen.get("template_id")
        text = (chosen.get("template") or "").lower()
        assert "unable to process address" in text
        assert "weather" in text or "network" in text or "hotspot" in text
    else:
        assert "ACC-006" not in (chosen.get("template_id") or "")
    print("  ✓ 改地址+天气")


def test_review_lock_driving_black_screen_not_cpaa():
    templates = load_xlsx()
    body = (
        "While driving the main screen with all driving visual information "
        "including speed and sat nav goes black the car resets and it comes "
        "back on. This happens three or four times a day."
    )
    chosen = pick_template("ficm_cpaa", templates, body_text=body)
    assert chosen.get("template_id") == "FICM-003", chosen.get("template_id")
    print("  ✓ 行驶中控黑屏 → FICM-003 不是 CPAA")


def test_review_lock_memory_seat_not_warranty_transfer():
    from agent.template_reply import _is_service_consult

    body = (
        "The vehicle has reached its 1-year ownership mark, 5000 km. "
        "Should I book in for service. Additionally I need a warranty "
        "repair for the driver's memory seat. The seat frequently fails "
        "to remember."
    )
    assert _is_service_consult(body) is False
    templates = load_xlsx()
    chosen = pick_template("hardware_warranty", templates, body_text=body)
    assert chosen.get("template_id") != "SVC-001", chosen.get("template_id")
    print("  ✓ 记忆座椅故障不套保修过户 SVC-001")


def test_review_lock_infotainment_update_not_cpaa():
    """审稿 AC：车机/CarPlay/AA 有没有升级，不是删配对。"""
    from agent.template_reply import _is_infotainment_update_consult

    body = (
        "VIN LSJW94397TG018135. Italia HS Hybrid+. "
        "Buongiorno volevo sapere se per il sistema info taimen c'era "
        "qualche aggiornamento compreso aggiornamento di car play e "
        "Android auto per la mia mg HS hybrid anche perché ancora non ho "
        "fatto il tagliando in quanto non è passato neanche un anno."
    )
    assert _is_infotainment_update_consult(body)
    templates = load_xlsx()
    chosen = pick_template("ficm_cpaa", templates, body_text=body)
    assert chosen.get("template_id") == "FICM-001", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "software version" in text
    assert "pairing records" not in text
    print("  ✓ 车机/CarPlay 升级咨询 → FICM-001 不是 CPAA-001")


def test_llm_menu_semantics_not_locked_to_triage_intent():
    """Triage 即使是 ficm_cpaa，升级咨询菜单也必须有 FICM-001、不能只剩配对行。"""
    from agent.template_reply import _llm_template_candidates

    update_body = (
        "VIN LSJW94397TG018135. Buongiorno volevo sapere se per il sistema "
        "info taimen c'era qualche aggiornamento compreso aggiornamento di "
        "car play e Android auto."
    )
    pair_body = (
        "VIN LSJWH4099PN101557. Android Auto turns off after a few minutes "
        "on a wired connection. The infotainment screen stays on."
    )
    templates = load_xlsx()
    update_ids = {t.get("template_id") for t in _llm_template_candidates(
        "ficm_cpaa", templates, update_body,
    )}
    assert "FICM-001" in update_ids
    assert "CPAA-001" not in update_ids
    pair_ids = {t.get("template_id") for t in _llm_template_candidates(
        "ficm_cpaa", templates, pair_body,
    )}
    assert "CPAA-001" in pair_ids
    print("  ✓ GPT 菜单：升级咨询有 FICM-001；断连仍有 CPAA-001")


def test_review_lock_bt_key_works_not_digital_key():
    """远联无数据 + 车机无网 + 查套餐；Bluetooth key works 只是对比。"""
    templates = load_xlsx()
    body = (
        "Pays/Région: Australie\n"
        "Modèle de véhicule: MG4\n"
        "Numéro d'identification du véhicule: LSJWH4093SN037198\n"
        "Type de problème: Connexion et déconnexion\n"
        "Description des problèmes: Hello, MG4 VIN LSJWH4093SN037198.\n"
        "I log into the MG iSMART AU & NZ app and see the car, but since "
        "25/06/2026 it sends no data: \"Vehicle did not respond\", and the "
        "car screen shows \"no internet connection\". Bluetooth key works, "
        "car drives fine, factory reset done. Can you identify the cause "
        "from your side? Please also confirm if the telematics data plan "
        "is active, its expiry date.\n"
    )
    chosen = pick_template("ismart_digital_key", templates, body_text=body)
    assert chosen.get("template_id") == "GEN-005", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "digital key" not in text
    parked = pick_template(
        "ismart_connectivity", templates,
        body_text=(
            'When the car is parked and not connected to a charger, the app '
            'shows "The vehicle did not respond." After unlocking or starting '
            "the car, the connection usually returns."
        ),
    )
    assert parked.get("template_id") == "STATUS-002", parked.get("template_id")
    print("  ✓ Bluetooth key works 不是数字钥匙；停车未响应仍 STATUS-002")


def test_semantic_review_blocks_known_mismatches():
    from agent.template_reply import draft_semantic_mismatch_reason

    hail = (
        "Ho avuto un evento atmosferico auto grandinata "
        "devo contattare l'assicurazione vero"
    )
    assert draft_semantic_mismatch_reason(
        hail, "NON-005", "Please refer to the vehicle User Manual",
        llm=None, use_llm=False,
    )
    cotineau = (
        'since 25/06/2026 it sends no data: "Vehicle did not respond", '
        'and the car screen shows "no internet connection". Bluetooth key '
        "works. Please confirm if the telematics data plan is active."
    )
    assert draft_semantic_mismatch_reason(
        cotineau, "APP-DIGITAL KEY-001", "Digital Key entry under Remote Control",
        llm=None, use_llm=False,
    )
    ac = (
        "volevo sapere se per il sistema info taimen c'era qualche "
        "aggiornamento compreso aggiornamento di car play e Android auto"
    )
    assert draft_semantic_mismatch_reason(
        ac, "CPAA-001", "delete pairing records", llm=None, use_llm=False,
    )
    tyre = (
        "Me sale en naranja como que a las ruedas le falta presión"
    )
    assert draft_semantic_mismatch_reason(
        tyre, "IPK-001", "instrument cluster language", llm=None, use_llm=False,
    )
    aa = (
        "Android Auto turns off after a few minutes on a wired connection. "
        "The infotainment screen stays on."
    )
    assert draft_semantic_mismatch_reason(
        aa, "CPAA-001", "reconnect Android Auto", llm=None, use_llm=False,
    ) is None
    dk = (
        "Hello, I don't understand what the digital key is, "
        "how it is activated and how it is deactivated ???"
    )
    assert draft_semantic_mismatch_reason(
        dk, "APP-DIGITAL KEY-001", "tap Digital Key under Remote Control",
        llm=None, use_llm=False,
    ) is None
    print("  ✓ 建稿前语义复核拦错桶、放过真配对/真钥匙 how-to")


def test_kb_20260916_new_ids_load():
    templates = load_xlsx()
    by_id = {t.get("template_id"): t for t in templates}
    assert any("20260916" in (t.get("source") or "") for t in templates)
    assert "error code 47" in (by_id["STATUS-006"]["template"] or "").lower()
    assert "t-box" in (by_id["STATUS-006"]["template"] or "").lower()
    for tid in (
        "STATUS-012", "DK-003", "BIND-008", "DK-004", "MAP-005",
        "SVC-004", "APP-NOTIFICATION-001", "NON-009", "NON-010", "ACC-007", "ACC-008",
    ):
        assert tid in by_id, tid
    assert "paint" in (by_id["SVC-003"]["template"] or "").lower() or "warranty" in (
        by_id["SVC-003"].get("scenario") or ""
    ).lower() or "车漆" in (by_id["SVC-003"].get("scenario") or "")
    assert "color code" in (by_id["SVC-004"]["template"] or "").lower() or "colour" in (
        by_id["SVC-004"]["template"] or ""
    ).lower()
    print("  ✓ 20260916 全量含 9-16 新行且 SVC-003 漆面仍在")


def test_review_lock_916_error47_qr_mileage_colour_fuel():
    templates = load_xlsx()
    assert pick_template(
        "app_vehicle_control", templates,
        body_text="Remote climate failed with error code 47 in the iSMART App.",
    ).get("template_id") == "STATUS-006"
    assert pick_template(
        "app_binding", templates,
        body_text="I scanned the QR code but the app can't recognize it and says please try again.",
    ).get("template_id") == "BIND-008"
    assert pick_template(
        "ismart_feature_inquiry", templates,
        body_text="Where can I find the total mileage in the iSMART app?",
    ).get("template_id") == "DK-004"
    assert pick_template(
        "delivery_handover", templates,
        body_text="Please send me the colour code / paint code for my car.",
    ).get("template_id") == "SVC-004"
    assert pick_template(
        "hardware_warranty", templates,
        body_text="The car takes too much petrol. Fuel consumption is very high.",
    ).get("template_id") == "NON-009"
    print("  ✓ 9-16 锁：47 / 二维码 / 里程 / 色号 / 油耗")


def test_review_lock_916_missing_feature_nav_charge_tpms():
    templates = load_xlsx()
    assert pick_template(
        "ismart_digital_key", templates,
        body_text="I don't have digital key page in the app. Other owners have this function.",
    ).get("template_id") == "DK-003"
    assert pick_template(
        "ficm_map", templates,
        body_text="How to change the language of the navigation system?",
    ).get("template_id") == "MAP-005"
    assert pick_template(
        "charging", templates,
        body_text="Charging management page has no data and I cannot view charging status in the app.",
    ).get("template_id") == "STATUS-012"
    assert pick_template(
        "app_messages", templates,
        body_text="The iSMART app keeps sending a low pressure tire alarm notification.",
    ).get("template_id") == "APP-NOTIFICATION-001"
    howto = pick_template(
        "charging", templates,
        body_text="How do I set a charging schedule / off-peak timer in the app?",
    )
    assert howto.get("template_id") == "CHG-SCHED-001", howto.get("template_id")
    print("  ✓ 9-16 锁：缺功能 / 导航语言 / 充电管理 / 胎压通知；预约充电 how-to 仍 CHG-SCHED")


def test_review_lock_916_sold_account_and_swap_bind():
    templates = load_xlsx()
    by_id = {t.get("template_id") for t in templates}
    assert "ACC-007" in by_id and "ACC-008" in by_id
    sold = pick_template(
        "app_account", templates,
        body_text=(
            "I’ve just sold the car so do I just uninstall the app or "
            "do I need to unlink myself from the vehicle?"
        ),
    )
    assert sold.get("template_id") == "ACC-007", sold.get("template_id")
    assert "delete account" in (sold.get("template") or "").lower()
    lease = pick_template(
        "app_account", templates,
        body_text=(
            "I forgot to clear my account before handing back my Lease car. "
            "can you delete all my personal details from the car"
        ),
    )
    assert lease.get("template_id") == "ACC-007", lease.get("template_id")
    swap = pick_template(
        "app_binding", templates,
        body_text=(
            "Habe den MG 5 gegen einen neuen MG 4 getauscht und will "
            "dieses Fahrzeug mit der App nutzen"
        ),
    )
    assert swap.get("template_id") == "ACC-008", swap.get("template_id")
    pl = pick_template(
        "app_messages", templates,
        body_text=(
            "Opis problemów: Pokazał mi się alarm opon o niskim ciśnieniu. "
            "Po dopompowaniu dalej jest alarm. Co mam zrobić?"
        ),
    )
    assert pl.get("template_id") == "APP-NOTIFICATION-001", pl.get("template_id")
    jake = pick_template(
        "app_vehicle_control", templates,
        body_text=(
            "Description of Issues: Repeated 'MG ismart has stopped ' warning\n"
            "VIN VIN_MASK_c5a67e93 Brand MG Vehicle Model MGS5 EV"
        ),
    )
    assert jake.get("template_id") == "GEN-004", jake.get("template_id")
    text = (jake.get("template") or "").lower()
    assert "screenshot" in text or "video" in text
    print("  ✓ 卖车删号 ACC-007 / 换车再绑 ACC-008 / 波兰语胎压通知 / App stopped → GEN-004")


def test_review_lock_918_unlink_privacy_charge_target():
    templates = load_xlsx()
    by_id = {t.get("template_id"): t for t in templates}
    assert "ACC-009" in by_id and "STATUS-013" in by_id and "FICM-PRIV-003" in by_id
    assert "STATUS-010" not in by_id and "FICM-PRIV-002" not in by_id
    unlink = pick_template(
        "app_account", templates,
        body_text=(
            "Can you please unlink the 2 secondary accounts linked with my account"
        ),
    )
    assert unlink.get("template_id") == "ACC-009", unlink.get("template_id")
    assert "unable to remove secondary" in (unlink.get("template") or "").lower()
    target = pick_template(
        "charging", templates,
        body_text=(
            "I set charge to 80% but it actually charges to 100%. "
            "The charging setting is not being saved."
        ),
    )
    assert target.get("template_id") == "STATUS-013", target.get("template_id")
    print("  ✓ 9-18 锁：删副账号 ACC-009 / 充电目标 STATUS-013；PRIV-002/STATUS-010 已下线")


def test_review_lock_map_update_failed_not_fota():
    templates = load_xlsx()
    row = next(t for t in templates if t.get("template_id") == "MAP-002")
    assert row["level"] != "no_auto_reply", row["level"]
    body = (
        "Country/Region: Germany Brand: MG Vehicle Model: MG S9 PHEV "
        "Vehicle Identification Number: LSJWC4394TZ574831 Type of Problem: Other "
        "Description of Issues: When I try to update the map online, I get the "
        'error message "Update failed it seems that the map update has not '
        'completed successfully..." I can read the latest Privacy Policy to get '
        "more details about the data processing activities. By sending this email, "
        "I consent to data processing. I can also contact the Data Protection Unit."
    )
    chosen = pick_template("ismart_fota", templates, body_text=body)
    assert chosen.get("template_id") == "MAP-002", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "restart" in text or "dealer" in text
    from agent.outlook_drafts import should_skip
    from unittest.mock import MagicMock
    skip = should_skip(
        MagicMock(),
        {"intent": "ismart_fota", "urgency": "P2", "region": "Europe",
         "language": "en", "requires_recall_check": False,
         "requires_warranty_lookup": False, "red_flags": []},
        chosen, None, region="Europe",
    )
    assert skip is None, skip
    ota = pick_template(
        "ismart_fota", templates,
        body_text="VIN LSJWH4099PN101557. Will the next OTA also include map updates?",
    )
    assert ota.get("template_id") == "FOTA-001", ota.get("template_id")
    print("  ✓ 在线地图更新失败 → MAP-002 不是 FOTA；OTA 范围问句仍 FOTA")


def test_review_lock_hello_mg_usb_aa_not_cpaa_or_vin():
    templates = load_xlsx()
    body = (
        "Only when it comes to voice recognition am I still undecided. "
        'Voice recognition in the MG3 Luxury is activated via "Hello MG" '
        "or a button on the steering wheel. In the user manual it is written "
        'about a "voice recognition function connected to the phone". '
        "I always receive the request to connect the phone via USB via both "
        "the button and the command Hello MG. This brings me to Android Auto. "
        "Does the vehicle not have classic voice control, which also has other "
        "functions ... volume etc.?"
    )
    chosen = pick_template("ficm_cpaa", templates, body_text=body)
    assert chosen.get("template_id") == "FICM-VOICE-001", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "voice" in text or "hello" in text
    from agent.outlook_drafts import Mail, should_skip
    mail = Mail(
        id="x", subject="voice", sender="a@b.com",
        received_at="2026-01-01", body_text=body, is_read=False, attachments=[],
    )
    skip = should_skip(
        mail,
        {"intent": "ficm_cpaa", "urgency": "P2", "region": "Europe",
         "language": "en", "requires_recall_check": False,
         "requires_warranty_lookup": False, "red_flags": []},
        chosen, None, region="Europe",
    )
    assert skip is None, skip
    de = pick_template(
        "ficm_cpaa", templates,
        body_text=(
            "Hallo MG und die Taste am Lenkrad verlangen, das Handy per USB "
            "zu verbinden. Hat das Fahrzeug keine eigene Sprachsteuerung?"
        ),
    )
    assert de.get("template_id") == "FICM-VOICE-001", de.get("template_id")
    spec = pick_template(
        "ismart_feature_inquiry", templates,
        body_text=(
            "Does the vehicle not have Hello MG / native in-car voice control? "
            "CarPlay starts when I press the steering wheel voice button."
        ),
    )
    assert spec.get("template_id") == "FICM-VOICE-001", spec.get("template_id")
    print("  ✓ Hello MG 只唤起 USB/AA → FICM-VOICE-001；德语/CarPlay 同类同样锁")


def test_review_lock_network_unavailable_not_bind008():
    templates = load_xlsx()
    body = (
        "Country/Region: Netherlands Brand: MG Vehicle model: MG S9 PHEV "
        "Vehicle identification number: LSJWC4397TZ551270 Problem Type: Others "
        "Description of problems: Unable to use Mg i smart after a week of "
        "purchasing the New MG S9! Do I get this message every time "
        "( The network is not available. Please try again later.)"
    )
    chosen = pick_template("app_binding", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-003", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "network" in text or "open area" in text
    from agent.outlook_drafts import Mail, should_skip
    mail = Mail(
        id="x", subject="network", sender="a@b.com",
        received_at="2026-01-01", body_text=body, is_read=False, attachments=[],
    )
    skip = should_skip(
        mail,
        {"intent": "app_binding", "urgency": "P2", "region": "Europe",
         "language": "en", "requires_recall_check": False,
         "requires_warranty_lookup": False, "red_flags": []},
        chosen, None, region="Europe",
    )
    assert skip is None, skip
    qr = pick_template(
        "app_binding", templates,
        body_text="I scanned the QR code but the app can't recognize it and says please try again.",
    )
    assert qr.get("template_id") == "BIND-008", qr.get("template_id")
    print("  ✓ 网络不可用 toast → STATUS-003 不是 BIND-008；真扫码失败仍 BIND-008")


def test_parked_false_awake_not_status001():
    from agent.template_reply import (
        parked_false_awake_needs_human,
        draft_semantic_mismatch_reason,
        _strip_vin_from_intake_list,
        load_xlsx,
    )
    body = (
        "il veicolo mi segnala l'accensione del veicolo, quando in realtà "
        "è già da una mezzora che il veicolo è parcheggiato. "
        "batteria scarica. essendo un invalido."
    )
    assert parked_false_awake_needs_human(body)
    assert draft_semantic_mismatch_reason(
        body, "STATUS-001", "move the vehicle to an open area",
        llm=None, use_llm=False,
    )
    parked = (
        'When the car is parked and not connected to a charger, the app '
        'shows "The vehicle did not respond."'
    )
    assert not parked_false_awake_needs_human(parked)
    tmpl = next(t for t in load_xlsx() if t.get("template_id") == "STATUS-001")
    filled = _strip_vin_from_intake_list(
        tmpl["template"],
        "VIN_MASK_aabbccdd. App mileage is stuck at 1200 km.",
    )
    assert "1. VIN" not in filled
    assert "1. Country/Region" in filled
    print("  ✓ 停车假点火亏电转人工；STATUS-001 已有 VIN 不重复要")


def test_europe_service_subscription_needs_human():
    from agent.template_reply import europe_service_subscription_needs_human
    from agent.outlook_drafts import (
        GraphClient, Mail, process_mail, skip_needs_human_queue,
    )
    from unittest.mock import MagicMock

    eu = (
        "Country/Region: Germany\n"
        "My iSMART service subscription has expired. How do I renew it?"
    )
    assert europe_service_subscription_needs_human(eu) is True
    au = (
        "Country/Region: Australia\n"
        "My iSMART service subscription has expired. How do I renew it?"
    )
    assert europe_service_subscription_needs_human(au) is False
    plan = (
        "Country/Region: Germany\n"
        "I can no longer connect to my car, the screen remains in 3G. "
        "How do I reactivate my iSmart plan?"
    )
    assert europe_service_subscription_needs_human(plan) is False
    used = (
        "I have recently purchased my MG ZS second hand. "
        "Do I need to register my details for the warranty and am I able "
        "to check if there was any kind of outstanding service plan "
        "already on the vehicle?"
    )
    assert europe_service_subscription_needs_human(used) is False
    g = MagicMock(spec=GraphClient)
    g.create_draft_reply = MagicMock()
    g.get_conversation_thread = MagicMock(return_value=[])
    mail = Mail(
        id="m-sub", subject="iSMART subscription", sender="a@b.com",
        received_at="2026-09-18T10:00:00Z", body_text=eu, is_read=False,
        conversation_id="c-sub",
    )
    rec = process_mail(
        mail, g=g, llm=None, templates=[], allowlist=None,
        dry_run=True, use_llm=False, router=None, dedup=None,
    )
    assert rec.get("skipped") == "europe_service_subscription_needs_human", rec
    assert skip_needs_human_queue(rec["skipped"])
    g.create_draft_reply.assert_not_called()
    print("  ✓ 欧洲服务订阅转人工；3G 套餐/二手 service plan 不误伤")


def test_review918_schedule_fail_not_chgsched():
    templates = load_xlsx()
    body = (
        "Vehicle Model: HS\n"
        "The charging schedule in HS PHEV continuously fails and it is "
        "very difficult to set up an instant load."
    )
    chosen = pick_template("charging", templates, body_text=body)
    assert chosen.get("template_id") != "CHG-SCHED-001", chosen.get("template_id")
    print("  ✓ 预约充电设置失败不是谷电 how-to")


def test_review918_digital_key_uses_stated_model():
    from agent.template_reply import _fill_stated_vehicle_model
    body = "Vehicle Model: MG4 Electric\ni can not start my mg4 with the phone."
    raw = "the Digital Key cannot start your MG ZS EV, and we understand"
    assert "MG4 Electric" in _fill_stated_vehicle_model(raw, body)
    print("  ✓ 数字钥匙稿用表单车型替换 ZS EV")


def test_review918_charge_target_tag_not_non_ismart():
    from agent.mail_tags import extract_known_issue_type
    templates = load_xlsx()
    body = (
        "My MG5 intermittently stops charging. the charger reported "
        "suspended by the EV. I changed the target back to 100% and "
        "charging resumed. it keeps setting target charge to 80%."
    )
    chosen = pick_template("charging", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-013", chosen.get("template_id")
    assert extract_known_issue_type(body) == "APP-充电管理相关"
    print("  ✓ 80/100 目标不匹配打 APP-充电管理，不是非iSMART-充电")


def test_review918_language_update_is_new_demand():
    templates = load_xlsx()
    body = (
        "Vehicle Model: ZS\n"
        "Is a language update available for the MG ZS?\n"
        "Estonia language please"
    )
    chosen = pick_template("ismart_feature_inquiry", templates, body_text=body)
    assert chosen.get("template_id") == "REQ-001", chosen.get("template_id")
    print("  ✓ 车机小语种是新需求，不是 FICM-001")


def test_review918_accessory_battery_non_ismart():
    from agent.mail_tags import extract_known_issue_type
    templates = load_xlsx()
    body = (
        "the accessory battery does not charge. It was changed at my expense. "
        "a car that doesn't start charging or faults in the middle of nowhere."
    )
    chosen = pick_template("charging", templates, body_text=body)
    assert chosen.get("template_id") == "NON-001", chosen.get("template_id")
    assert extract_known_issue_type(body) == "非iSMART-车身控制相关"
    print("  ✓ 辅助蓄电池是非iSMART，不是充电预约")


def test_review918_cosmetic_maintenance_svc001():
    templates = load_xlsx()
    body = (
        "I took out a Cosmetic Maintenance plan for £357.50 and now have "
        "a need to make use of this plan. contact details. My vehicle number "
        "is WL52ANN."
    )
    chosen = pick_template("ismart_feature_inquiry", templates, body_text=body)
    assert chosen.get("template_id") == "SVC-001", chosen.get("template_id")
    print("  ✓ Cosmetic Maintenance → SVC-001 售前售后")


def test_review918_bound_remote_is_status003():
    templates = load_xlsx()
    body = (
        "Vehicle identification number: LSJWP439XRZ257343\n"
        "the application tells me that the vehicle is bound I can no longer "
        "access the car remotely, I tried to reset both the car and the "
        "application but nothing. chassis number A774337MI25"
    )
    chosen = pick_template("app_binding", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-003", chosen.get("template_id")
    print("  ✓ 已绑定但远控不了 → STATUS-003，不是绑车/GEN-003")


def test_review918_family_language_finnish_req001():
    templates = load_xlsx()
    body = "Is Finnish language available on the infotainment? Please add it."
    chosen = pick_template("ismart_feature_inquiry", templates, body_text=body)
    assert chosen.get("template_id") == "REQ-001", chosen.get("template_id")
    print("  ✓ 同类小语种（芬兰语）也走 REQ-001")


def test_review918_family_who_to_contact_svc001():
    templates = load_xlsx()
    body = (
        "Who should I contact to make use of my maintenance plan? "
        "I purchased the plan from the dealer last year."
    )
    chosen = pick_template("ismart_feature_inquiry", templates, body_text=body)
    assert chosen.get("template_id") == "SVC-001", chosen.get("template_id")
    print("  ✓ 谁来兑现保养计划 → SVC-001")


def test_review918_family_schedule_will_not_save():
    templates = load_xlsx()
    body = "The charging schedule will not save. Settings fail every time I try."
    chosen = pick_template("charging", templates, body_text=body)
    assert chosen.get("template_id") != "CHG-SCHED-001", chosen.get("template_id")
    assert chosen.get("template_id") == "STATUS-012", chosen.get("template_id")
    print("  ✓ 预约设置保存失败 → STATUS-012，不是谷电 how-to")


def test_review918_family_already_bound_remote():
    templates = load_xlsx()
    body = (
        "VIN: LSJWP4096RS000011\n"
        "I already bound the car but remote control does not work after a reset."
    )
    chosen = pick_template("app_binding", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-003", chosen.get("template_id")
    print("  ✓ already bound + 远控失败 → STATUS-003，不是前车主 BIND-001")


def test_review918_family_fill_any_hardcoded_model():
    from agent.template_reply import _fill_stated_vehicle_model
    body = "Vehicle Model: MG HS PHEV\ncannot start with the phone."
    raw = "the Digital Key cannot start your MG4 Electric, and we understand"
    assert "MG HS PHEV" in _fill_stated_vehicle_model(raw, body)
    assert "MG4 Electric" not in _fill_stated_vehicle_model(raw, body)
    print("  ✓ 任意写死车型都换成表单车型")


def test_review918_family_strip_vin_model_already_given():
    from agent.template_reply import _strip_already_known_intake_asks
    body = (
        "Vehicle identification number: LSJWP4096RS000011\n"
        "Vehicle Model: MG4 Electric\n"
        "cannot find the user manual"
    )
    draft = (
        "Please find the manual in the App. "
        "If the manual is still not displayed, please provide your VIN "
        "and a screenshot. Please provide your VIN, vehicle model as well."
    )
    out = _strip_already_known_intake_asks(draft, body, "MAN-001")
    assert "provide your VIN" not in out
    assert "still not displayed" not in out.lower()
    print("  ✓ 已有 VIN/车型时手册稿不再追要")


def test_review918_family_cluster_warning_non_ismart():
    from agent.mail_tags import extract_known_issue_type
    templates = load_xlsx()
    body = (
        "A warning message appears on the instrument cluster while driving. "
        "The accessory battery was already replaced."
    )
    chosen = pick_template("charging", templates, body_text=body)
    assert chosen.get("template_id") == "NON-001", chosen.get("template_id")
    assert extract_known_issue_type(body) == "非iSMART-车身控制相关"
    print("  ✓ 仪表报警/辅助电瓶 → NON-001，不是充电")


def test_should_skip_includes_level_note_for_reg001():
    from agent.outlook_drafts import Mail, should_skip
    templates = load_xlsx()
    tmpl = next(t for t in templates if t.get("template_id") == "REG-001")
    assert tmpl.get("level") == "no_auto_reply"
    mail = Mail(
        id="x", subject="region", sender="a@b.com",
        received_at="2026-01-01", body_text="not supported in Malaysia",
        is_read=False, attachments=[],
    )
    reason = should_skip(
        mail,
        {"intent": "ismart_region_support", "urgency": "P2",
         "region": "North America", "language": "en",
         "requires_recall_check": False, "requires_warranty_lookup": False,
         "red_flags": []},
        tmpl, None,
    )
    assert reason is not None and "no_auto_reply" in reason, reason
    print("  ✓ REG-001 不可自动（no_auto_reply）")


def test_review923_unable_to_bind_asks_info_not_app_guide():
    templates = load_xlsx()
    body = (
        "Country/Region: United Kingdom\nBrand:\nVehicle Model:\n"
        "Vehicle Identification Number:\nIssue Type: Others\n"
        "Description of Issues: Unable to bind vehicle\n\n"
        "I can read the latest Privacy Notice for more details."
    )
    chosen = pick_template("app_binding", templates, body_text=body)
    assert chosen.get("template_id") == "GEN-001", chosen.get("template_id")
    assert chosen.get("category") == "App-绑车解绑相关"
    text = (chosen.get("template") or "").lower()
    assert "provide" in text and "vin" in text
    assert "binding instructions" not in text and "attachment" not in text
    from agent.mail_tags import extract_known_issue_type
    assert extract_known_issue_type(body) == "App-绑车解绑相关"
    print("  ✓ Unable to bind → GEN-001 要信息，不是 APP-GUIDE 教程")


def test_review923_iraq_qr_bind_asks_info_not_reg001():
    templates = load_xlsx()
    body = (
        "Country/Region: Iraq\n"
        "Explanation of problems: I can't find the QR code, can I use VIN "
        "to register on the iSmart app, knowing that my car is 2026 and it "
        "shows the user manual."
    )
    chosen = pick_template("app_binding", templates, body_text=body)
    assert chosen.get("template_id") == "GEN-001", chosen.get("template_id")
    assert chosen.get("category") == "App-绑车解绑相关"
    assert chosen.get("level") == "auto_draft_needs_info"
    text = (chosen.get("template") or "").lower()
    assert "provide" in text and "vin" in text
    assert "may not be available" not in text
    from agent.mail_tags import extract_known_issue_type
    assert extract_known_issue_type(body) == "App-绑车解绑相关"
    print("  ✓ Iraq 绑车/二维码 → GEN-001 要信息，不是 REG-001")


def test_review923_app_says_range_drop_status012():
    templates = load_xlsx()
    body = (
        "Brand: MG\nVehicle Model: IM5\nVIN: LSJWT4099SS056192\n"
        "Description of Issues: I have an IM5 Platinum and when the car is "
        "charged at home to 80% (app says 524km), however as soon as I "
        "unplug the charger and turn the car on, the range drops to 498km "
        "(app still says 80%)."
    )
    chosen = pick_template("charging", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-012", chosen.get("template_id")
    assert chosen.get("category") == "APP-充电管理相关"
    text = (chosen.get("template") or "").lower()
    assert "screen recording" in text or "photos or videos" in text
    assert "energy consumption calculation" not in text
    from agent.mail_tags import extract_known_issue_type
    assert extract_known_issue_type(body) == "APP-充电管理相关"
    print("  ✓ app says 续航骤降 → STATUS-012 要截图，不是 CHG-002")


def test_review923_manual_wrong_model_keeps_guide_asks_shots():
    templates = load_xlsx()
    body = (
        "Country/Region: United Kingdom\nBrand: MG\nVehicle Model: ZS\n"
        "VIN: LSJWS4U91TZ229497\n"
        "Description of Issues: Manual on the app is showing a hybrid model "
        "which I don’t have. Can this be amended please?"
    )
    chosen = pick_template("ismart_user_manual", templates, body_text=body)
    assert chosen.get("template_id") == "MAN-001", chosen.get("template_id")
    text = (chosen.get("template") or "").lower()
    assert "my manual" in text or "user manual" in text
    assert "screenshot" in text
    assert "incorrect user manual" in text or "incorrect" in text
    print("  ✓ 手册车型不匹配 → MAN-001 保留路径 + 要截图")


def test_review923_app_blocks_charging_asks_shots_not_chg001():
    templates = load_xlsx()
    body = (
        "Brand: MG\nVehicle Model: HS Super Hybrid\n"
        "VIN: LSJW94395TG020725\n"
        "Description of Problems: The app is constantly buggy! "
        "And it won't let me charge the car!"
    )
    chosen = pick_template("charging", templates, body_text=body)
    assert chosen.get("template_id") == "STATUS-012", chosen.get("template_id")
    assert chosen.get("category") == "APP-充电管理相关"
    text = (chosen.get("template") or "").lower()
    assert "screenshot" in text
    assert "vehicle function inspection" not in text
    from agent.mail_tags import extract_known_issue_type
    assert extract_known_issue_type(body) == "APP-充电管理相关"
    print("  ✓ App buggy 不让充电 → STATUS-012 要截图，不是 CHG-001")


def test_review1026_wrong_template_family():
    """10-26 审稿：锁到对应行，两问合写，空描述转人工。"""
    templates = load_xlsx()

    def pick(body: str, intent: str = "app_binding"):
        return pick_template(intent, templates, body_text=body)

    sell = (
        "• Issue Type: Digital Key\n"
        "• Description of problems: If you sell the vehicle, "
        "how do you remove it from the app?\n"
    )
    chosen = pick(sell)
    assert chosen.get("template_id") == "BIND-007", chosen.get("template_id")

    email = (
        "Please put the app on my husband's email so I can bind "
        "a new car on my own email."
    )
    chosen = pick(email)
    assert chosen.get("template_id") == "ACC-005", chosen.get("template_id")

    old = "The car is still bound to the old owner. Please remove that link."
    chosen = pick(old)
    assert chosen.get("template_id") == "BIND-001", chosen.get("template_id")

    both = (
        "I need to recover my password. This was a rental car and "
        "I also need to delete my account."
    )
    chosen = pick(both, "ismart_account_inquiry")
    assert chosen.get("template_id") == "ACC-002+ACC-007", chosen.get("template_id")

    missing = (
        "I bought an MG S5 to replace my MG4. I removed the old car. "
        "The new car says car not found and the old information is still shown."
    )
    chosen = pick(missing)
    assert chosen.get("template_id") == "GEN-004", chosen.get("template_id")
    text = chosen.get("template") or ""
    assert "car not found" in text.lower()
    assert "17-character" in text.lower()

    config = "I cannot get the battery configuration on the app."
    chosen = pick(config, "ismart_feature_inquiry")
    assert chosen.get("template_id") == "NON-005", chosen.get("template_id")
    assert "owner's manual" in (chosen.get("template") or "").lower()

    prev = (
        "I cannot link the car. The previous owner continues to use it. "
        "Please force unlink."
    )
    chosen = pick(prev)
    assert chosen.get("template_id") == "BIND-001", chosen.get("template_id")

    reboot = (
        "The screen continuously restarts during the journey after about "
        "30 km. Cruise and the cameras stop."
    )
    chosen = pick(reboot, "ficm_cpaa")
    assert chosen.get("template_id") == "FICM-REBOOT-001", chosen.get("template_id")

    rebind = "I need to unbind the car from the phone so I can re bind it."
    chosen = pick(rebind)
    assert chosen.get("template_id") == "BIND-007+APP-GUIDE-001", chosen.get("template_id")

    target = (
        "I am unable to charge to 80%. Overnight I only have the option "
        "to fully charge. Does the car have a charge-target feature?"
    )
    chosen = pick(target, "charging")
    assert chosen.get("template_id") == "CHG-SCHED-001", chosen.get("template_id")
    assert "target charge level" in (chosen.get("template") or "").lower()

    saved = (
        "I set the charge target to 80% and it changes back to 100%. "
        "The charging setting is not being saved."
    )
    chosen = pick(saved, "charging")
    assert chosen.get("template_id") == "STATUS-013", chosen.get("template_id")

    dk = "I can't select a digital key and I can't activate a secondary account."
    chosen = pick(dk, "ismart_digital_key")
    assert chosen.get("template_id") == "APP-DIGITAL KEY-001+APP-GUIDE-002", (
        chosen.get("template_id")
    )

    stale = (
        "The app does not update the status. It is stuck on 28.08.2026. "
        "I already tried a factory reset, reinstall, and unbind."
    )
    chosen = pick(stale, "app_vehicle_control")
    assert chosen.get("template_id") == "STATUS-003", chosen.get("template_id")

    second = (
        "I can't log in as a second user. The app says the primary user "
        "must register them."
    )
    chosen = pick(second, "ismart_account_inquiry")
    assert chosen.get("template_id") == "APP-GUIDE-002", chosen.get("template_id")

    vague = (
        "• Issue Type: My Profile\n"
        "• Description of problems: unable to connect to the application\n"
    )
    assert pick(vague, "app_vehicle_control") is None

    boot = (
        "The keys were left in the boot. The car locks and the App "
        "cannot connect to unlock it."
    )
    chosen = pick(boot, "ismart_digital_key")
    assert chosen.get("template_id") == "NON-007", chosen.get("template_id")

    former = (
        "I forced a stop of the link with the former owner. "
        "72 hours later the car is still linked."
    )
    chosen = pick(former)
    assert chosen.get("template_id") == "BIND-001", chosen.get("template_id")

    login = (
        "I cannot remember my login. I am trying to bind to another email. "
        "The app says it notified the previous owner, who is still the customer."
    )
    chosen = pick(login, "ismart_account_inquiry")
    assert chosen.get("template_id") == "ACC-002", chosen.get("template_id")

    device = (
        "I was kicked out because another device is connected. "
        "Please delete the account completely so I can register again."
    )
    chosen = pick(device, "ismart_account_inquiry")
    assert chosen.get("template_id") == "ACC-001+ACC-007", chosen.get("template_id")

    caps = (
        "The app shows mileage and charge for a 53.9 battery. "
        "The car is 42.8 kWh."
    )
    chosen = pick(caps, "charging")
    assert chosen.get("template_id") == "STATUS-012", chosen.get("template_id")
    assert "official documentation" in (chosen.get("template") or "").lower()

    wipe = (
        "Please delete my email and phone number of the brand. "
        "I changed to another phone and still cannot connect."
    )
    chosen = pick(wipe, "ismart_account_inquiry")
    assert chosen.get("template_id") == "ACC-007", chosen.get("template_id")

    other_car = "Another vehicle is not registered in my app."
    chosen = pick(other_car)
    assert chosen.get("template_id") == "BIND-007", chosen.get("template_id")

    qr = (
        "The QR code is too small to scan. I have attached a photo. "
        "The SOS e-call failure disappears in about 2 minutes."
    )
    chosen = pick(qr)
    assert chosen.get("template_id") == "BIND-008", chosen.get("template_id")
    assert "e-call" in (chosen.get("template") or "").lower()

    charge_stuck = (
        "The app charge is stuck at 91 miles. The car updates, the app does not."
    )
    chosen = pick(charge_stuck, "app_vehicle_control")
    assert chosen.get("template_id") == "STATUS-003", chosen.get("template_id")

    pin = "I didn't get a code to connect the vehicle to the MG app."
    chosen = pick(pin)
    assert chosen.get("template_id") == "ACC-004", chosen.get("template_id")

    changed = (
        "I changed my car from MG3 to MG S5. "
        "How do I update my app with my new car?"
    )
    chosen = pick(changed)
    assert chosen.get("template_id") == "BIND-007", chosen.get("template_id")

    due = "Should it not have written when it is due for service?"
    chosen = pick(due, "hardware_warranty")
    assert chosen.get("template_id") == "SVC-001", chosen.get("template_id")

    bad = "• Description of problems: very bad app!!\n"
    chosen = pick(bad, "app_suggestion")
    assert chosen.get("template_id") == "COMP-001", chosen.get("template_id")

    bad_fault = "very bad app and I cannot bind the car."
    chosen = pick(bad_fault)
    assert (chosen or {}).get("template_id") != "COMP-001"

    sold_account = "I sold the car. Please delete my account."
    chosen = pick(sold_account, "ismart_account_inquiry")
    assert chosen.get("template_id") == "ACC-007", chosen.get("template_id")

    stranded = (
        "My keys are locked inside the car and I am stranded in the parking lot."
    )
    chosen = pick(stranded, "ismart_digital_key")
    assert chosen.get("template_id") == "KEY-002", chosen.get("template_id")

    spanish = "No puedo desvincular men MG i-SMRT, para poder volver a vincular"
    chosen = pick(spanish)
    assert chosen.get("template_id") == "BIND-007", chosen.get("template_id")

    connect = (
        "• Problem Type: Vehicle Management\n"
        "• Vehicle Identification Number: LSJE24000PG000001\n"
        "• Problem description: Doesn't connect to the app\n"
    )
    chosen = pick(connect, "app_vehicle_control")
    assert chosen.get("template_id") == "STATUS-003", chosen.get("template_id")

    still_owner = "The vehicle is already bound to a previous owner."
    chosen = pick(still_owner)
    assert chosen.get("template_id") == "BIND-001", chosen.get("template_id")
    print("  ✓ 10-26 审稿错行已锁")


# ── runner ───────────────────────────────────────────────────────────

TESTS = [
    # Part A
    ("A1 existing 权重 (3.0) > extracted (2.0)",
     test_enriched_score_existing_weight_higher_than_extracted),
    ("A2 scenario_en_tokens 权重 0.5",
     test_enriched_score_scenario_tokens_lowest_weight),
    ("A3 返回 debug_breakdown",
     test_enriched_score_returns_breakdown_dict),
    ("A4 空 body 返 0 分",
     test_enriched_score_empty_body_returns_zero),
    ("A5 无 enriched 数据返 0 分",
     test_enriched_score_no_enriched_data_returns_zero),
    # Part B
    ("B1 多 token 用子串匹配",
     test_kw_match_multi_token_uses_substring),
    ("B2 长单词 (>=4) 用子串匹配",
     test_kw_match_long_single_token_uses_substring),
    ("B3 短单词 (<4) 用词边界",
     test_kw_match_short_token_uses_word_boundary),
    ("B4 大小写不敏感",
     test_kw_match_case_insensitive),
    ("B5 空关键词返 False",
     test_kw_match_empty_kw_returns_false),
    ("B6 短单词 'QR' 词边界精确",
     test_kw_match_substring_too_short_word_should_not_partial_match),
    # Part C
    ("C1 笼统描述 → GEN-001",
     test_pick_template_vague_selects_gen001),
    ("C2 缺 VIN → GEN-002",
     test_pick_template_missing_vin_selects_gen002),
    ("C3 描述不足 → GEN-004 (app_vehicle_control)",
     test_pick_template_unclear_description_selects_gen004),
    ("C4 前车主已绑定 → BIND-001",
     test_pick_template_previous_owner_selects_bind001),
    ("C5 VIN 错 / 查不到 → GEN-003",
     test_pick_template_vin_invalid_selects_gen003),
    ("C6 无 body 返第一条候选",
     test_pick_template_no_body_returns_first_candidate),
    ("C7 无任何信号 → fallback 第一条",
     test_pick_template_no_signal_returns_first_candidate),
    ("C8 未知 intent → GEN-001",
     test_pick_template_intent_no_candidates_falls_back_to_ismart),
    # Part D
    ("D1 enriched 缺失 → body_token_overlap 兜底",
     test_pick_template_fallback_when_enriched_missing),
    # Part E
    ("E1 enriched JSON 存在且可读",
     test_enriched_json_exists_and_loadable),
    ("E2 enrichment --check PASS",
     test_enriched_json_check_script_passes),
    ("F1 REG-001 L2 映射",
     test_reg001_l2_maps_to_region_support),
    ("F2 默认只加载知识库新表",
     test_load_xlsx_default_is_knowledge_base_only),
    ("F3 不看区域，不选 REG-001",
     test_pick_template_region_not_supported_selects_reg001),
    ("F3b 美洲表单国家固定稿+转人工",
     test_americas_form_country_fixed_draft_and_human),
    ("F4 【】 climate 槽填充",
     test_fill_bracket_slots_climate_from_body),
    ("F5 【】 无值时去括号",
     test_fill_bracket_slots_strips_brackets_when_unknown),
    ("F5b 已附图不再要截图",
     test_strip_screenshot_asks_when_image_attached),
    ("F5c 隐私行已附图不重复要图",
     test_run_template_privacy_skips_screenshot_ask_when_attached),
    ("F6 {{salutation}} 不被填充器破坏",
     test_fill_placeholders_preserves_salutation_mustache),
    ("F7 草稿开头不是 {}",
     test_run_template_reply_greeting_not_empty_braces),
    ("F8 紧急缺 VIN 升人工",
     test_human_trigger_escalates_urgent_no_vin),
    ("F9 分类字典关键词并入",
     test_category_dict_keywords_merged_into_binding),
    ("F10 等级列【】抽出说明",
     test_extract_level_note_from_bracket_and_newline),
    ("F11 REG-001 不可自动",
     test_should_skip_includes_level_note_for_reg001),
    ("F105 Iraq 绑车要信息不是 REG-001",
     test_review923_iraq_qr_bind_asks_info_not_reg001),
    ("F105b Unable to bind → GEN-001 不是教程",
     test_review923_unable_to_bind_asks_info_not_app_guide),
    ("F106 app says 续航骤降 → STATUS-012",
     test_review923_app_says_range_drop_status012),
    ("F107 手册车型不匹配要截图",
     test_review923_manual_wrong_model_keeps_guide_asks_shots),
    ("F108 App buggy 不让充电要截图",
     test_review923_app_blocks_charging_asks_shots_not_chg001),
    ("F12 问套餐到期不走 PLAN/3G 行",
     test_pick_template_data_plan_without_vin_selects_gen002),
    ("F13 PLAN-001 已下线",
     test_pick_template_data_plan_with_vin_does_not_select_plan001),
    ("F13b VIN_MASK_ 视为已有 VIN",
     test_pick_template_vin_mask_counts_as_vin_present),
    ("F13c VIN 在附件抽取里",
     test_pick_template_vin_in_attachment_counts),
    ("F13d 截图有细节不 GEN-001",
     test_pick_template_screenshot_detail_not_vague_gen001),
    ("F14 数字钥匙 how-to → KEY-001",
     test_pick_template_digital_key_howto_selects_key001),
    ("F15 钥匙锁车内 → KEY-002",
     test_pick_template_keys_locked_inside_selects_key002),
    ("F16 LLM 选 STATUS-003 非法文兜底",
     test_llm_pick_template_uses_status_not_gen004),
    ("F17 LLM 非法 id 回退关键词",
     test_llm_pick_invalid_id_falls_back_to_keywords),
    ("F18 LLM GEN-002 + 已有 VIN 丢弃",
     test_llm_pick_refuses_gen002_when_vin_present),
    ("F19 切引用只留最新一段",
     test_latest_turn_strips_quoted_from),
    ("F20 跟进已交 VIN 不用 GEN-001",
     test_follow_up_with_vin_rejects_gen001),
    ("F21 跟进菜单含 BIND",
     test_follow_up_menu_includes_bind_when_qr_in_thread),
    ("F22 跟进空一句线程有 VIN",
     test_follow_up_vague_latest_uses_thread_vin),
    ("F23 引用里客服要 VIN 最新已给",
     test_cs_asked_for_vin_in_history_not_gen002),
    ("F24 线程后半段 VIN 仍算已提供",
     test_conversation_vin_note_redacts_vin_past_old_300_clip),
    ("F25 升级范围问句不进 ADAS 检查",
     test_pick_template_update_scope_not_adas_inspection),
    ("F26 升级后故障仍 ADAS-LKA-001",
     test_pick_template_post_update_fault_still_adas),
    ("F27 OTA 范围问句 → FOTA-001",
     test_pick_template_ota_scope_selects_fota001),
    ("F28 VPN 限制不选 NET-001",
     test_pick_template_vpn_restriction_not_net001),
    ("F29 热点月初不是 VPN 限制",
     test_hotspot_month_start_not_vpn_restriction),
    ("F30 中控熄屏不进 CPAA",
     test_pick_template_headunit_off_not_cpaa),
    ("F31 仅 AA 断连仍 CPAA-001",
     test_android_auto_disconnect_still_cpaa),
    ("F32 副账号不走 GEN-005",
     test_pick_template_sub_account_not_gen005),
    ("F33 手册问询不是副账号",
     test_digital_manual_not_secondary_account),
    ("F34 谷电预约不进检修",
     test_review_lock_charging_schedule_not_inspection),
    ("F35 调节充电功率不进检修",
     test_review_lock_charging_power_not_inspection),
    ("F82 第三方 Wallbox 充不上 → CHG-001",
     test_review_lock_third_party_wallbox_is_chg001),
    ("F36 previous user → BIND-001",
     test_review_lock_previous_user_bind001),
    ("F37 找不到手册不 GEN-002",
     test_review_lock_manual_not_gen002),
    ("F38 PDF/装订本 → MAN-002",
     test_review_lock_pdf_manual),
    ("F39 数字钥匙通知不走 NFC",
     test_review_lock_dk_notice_not_nfc),
    ("F39b 数字钥匙 how-to 贴 PDF",
     test_review_lock_dk_howto_attaches_guide_row),
    ("F40 手机配车不是蓝牙通话",
     test_review_lock_pair_phone_not_bluetooth_call),
    ("F41 漆面保修不进功能检修",
     test_review_lock_paint_not_function_inspection),
    ("F42 开巡航 how-to 不进 ADAS",
     test_review_lock_cruise_howto_not_adas),
    ("F43 拖车钩 ACC 仍检测",
     test_review_lock_towbar_acc_still_inspection),
    ("F44 妻子邀请是副账号",
     test_review_lock_wife_invitation_secondary),
    ("F44b 副账号报未注册第三列",
     test_review_lock_secondary_not_registered_is_guide003),
    ("F45 胎压去哪修",
     test_review_lock_tyre_not_body_inspection),
    ("F46 改地址",
     test_review_lock_address_not_network_only),
    ("F47 发动机中断",
     test_review_lock_engine_stop_not_app_warning),
    ("F48 审稿模板正文",
     test_updated_templates_answer_review_cases),
    ("F49 买配件",
     test_review_lock_parts_buy_not_gen002),
    ("F50 保养提醒标识",
     test_review_lock_maintenance_alert_logo),
    ("F50b 忘记密码不补齐",
     test_review_lock_forgot_password_not_gen001),
    ("F50b2 法语忘记密码",
     test_review_lock_french_forgot_password_acc002),
    ("F50c 绑车教程 PDF",
     test_review_lock_bind_howto_pdf_not_vin_only),
    ("F50d 反复隐私协议",
     test_review_lock_privacy_notice_not_status),
    ("F50d3 隐私拒绝绑不上不是远联",
     test_review_lock_privacy_denied_used_car_not_status003),
    ("F50d2 保养记录不是隐私页脚",
     test_review_lock_service_history_not_privacy_footer),
    ("F50d3 表单页脚不是隐私故障",
     test_webform_privacy_footer_never_picks_ficm_priv),
    ("F50d4 停车闪灯页脚不是隐私",
     test_parked_headlights_footer_not_privacy),
    ("F62 表单 Vehicle Management 下拉不是 BIND-007",
     test_webform_vehicle_management_dropdown_not_bind006),
    ("F62b 西语自己解绑重绑",
     test_spanish_self_unbind_bind007_not_bind001),
    ("F63 车机没网 + CarPlay 合写",
     test_merge_in_car_internet_and_carplay),
    ("F73 合写跳过无自动行",
     test_compose_skips_no_auto_lane_keeps_match),
    ("F73b 多问先回能套上的",
     test_partial_cover_forgot_password_plus_unmatched_ask),
    ("F73c 改装+无线AA升级合写 NON-010+FICM-001",
     test_partial_cover_aftermarket_plus_wireless_aa_update),
    ("F98 仅第三方加装 NON-010",
     test_aftermarket_accessory_only_non010),
    ("F73d 连不上+车机升级合写",
     test_merge_app_connect_and_infotainment_update),
    ("F63b 仅 AA 断连不合写",
     test_carplay_only_not_merged_with_status),
    ("F64 App连不上+CarPlay 合写",
     test_merge_app_connect_and_carplay),
    ("F65 4G/天气+CarPlay 合写",
     test_merge_net001_and_carplay),
    ("F66 热点+CarPlay 合写",
     test_merge_hotspot_and_carplay),
    ("F67 德语车机没网+CarPlay",
     test_merge_german_in_car_internet_and_carplay),
    ("F68 绑车+CarPlay 仍教程",
     test_bind_and_carplay_still_bind_howto),
    ("F69 连不上+VIN 不走 GEN-005",
     test_connect_with_vin_not_gen005),
    ("F70 仅 since 日期车机没网不是后台分析",
     test_since_date_in_car_internet_not_gen005),
    ("F71 点名 TBox 未换件不是 TBOX-001",
     test_tbox_keyword_not_replaced_is_status003),
    ("F72 已换 TBox 仍绑不上才 TBOX-001",
     test_tbox_replaced_still_tbox001),
    ("F50e 3G 套餐转人工不套热点",
     test_review_lock_3g_plan_not_net001),
    ("F99 冒用强制解绑转人工",
     test_fraud_force_unbind_needs_human_not_gen001_or_bind001),
    ("F100 maintenance history 不是剩余天数",
     test_review920_maintenance_history_not_remaining_days),
    ("F101 排放警告不归因 App",
     test_review920_emission_warning_not_app_attributed),
    ("F102 app not responding → STATUS-003",
     test_review920_app_not_responding_status003),
    ("F104 远控停在 update mode → STATUS-003",
     test_remote_ui_stuck_updating_is_status003_not_gen002),
    ("F103 前车主锁 GPS 确认解绑",
     test_review920_prev_owner_gps_confirm_unbind),
    ("F50f 导航意大利语",
     test_review_lock_italian_nav_not_voice_assistant),
    ("F50g 二手保修服务计划",
     test_review_lock_secondhand_warranty_not_appservice),
    ("F50h AA 车机故障不是删配对",
     test_review_lock_aa_infotainment_fault_not_cpaa),
    ("F50i 地图质量反馈不是只要版本截图",
     test_review_lock_map_quality_feedback_not_version_shot),
    ("F51 停车未响应",
     test_review_lock_parked_not_responded_not_tbox),
    ("F52 App 车色",
     test_review_lock_app_color_mismatch),
    ("F53 能耗统计",
     test_review_lock_energy_stats_not_vin_only),
    ("F54 改地址+天气合写",
     test_review_lock_address_beats_vague_gen001),
    ("F55 行驶黑屏",
     test_review_lock_driving_black_screen_not_cpaa),
    ("F56 记忆座椅不套过户",
     test_review_lock_memory_seat_not_warranty_transfer),
    ("F57 车机升级不是删配对",
     test_review_lock_infotainment_update_not_cpaa),
    ("F58 GPT 菜单不锁 Intent",
     test_llm_menu_semantics_not_locked_to_triage_intent),
    ("F59 Bluetooth key works 不是数字钥匙",
     test_review_lock_bt_key_works_not_digital_key),
    ("F60 建稿前语义复核",
     test_semantic_review_blocks_known_mismatches),
    ("F61 停车假点火亏电不是 STATUS-001",
     test_parked_false_awake_not_status001),
    ("F83 欧洲服务订阅转人工",
     test_europe_service_subscription_needs_human),
    ("F84 预约充电设置失败不是谷电 how-to",
     test_review918_schedule_fail_not_chgsched),
    ("F85 数字钥匙稿用表单车型",
     test_review918_digital_key_uses_stated_model),
    ("F86 80/100 打 APP-充电管理",
     test_review918_charge_target_tag_not_non_ismart),
    ("F87 车机小语种是新需求",
     test_review918_language_update_is_new_demand),
    ("F88 辅助蓄电池非iSMART",
     test_review918_accessory_battery_non_ismart),
    ("F89 Cosmetic Maintenance → SVC-001",
     test_review918_cosmetic_maintenance_svc001),
    ("F90 已绑定远控失败 STATUS-003",
     test_review918_bound_remote_is_status003),
    ("F91 同类芬兰语车机语言 REQ-001",
     test_review918_family_language_finnish_req001),
    ("F92 谁兑现保养计划 SVC-001",
     test_review918_family_who_to_contact_svc001),
    ("F93 预约保存失败不是谷电 how-to",
     test_review918_family_schedule_will_not_save),
    ("F94 already bound 远控 STATUS-003",
     test_review918_family_already_bound_remote),
    ("F95 写死 MG4 也换成表单车型",
     test_review918_family_fill_any_hardcoded_model),
    ("F96 已有 VIN/车型不再追要",
     test_review918_family_strip_vin_model_already_given),
    ("F97 仪表报警走非iSMART 车身",
     test_review918_family_cluster_warning_non_ismart),
    ("F74 20260916 加载 9-16 新行",
     test_kb_20260916_new_ids_load),
    ("F75 error47/二维码/里程/色号/油耗",
     test_review_lock_916_error47_qr_mileage_colour_fuel),
    ("F76 缺功能/导航语言/充电管理/胎压通知",
     test_review_lock_916_missing_feature_nav_charge_tpms),
    ("F77 卖车删号/换车再绑/波兰语胎压",
     test_review_lock_916_sold_account_and_swap_bind),
    ("F81 9-18 删副账号/充电目标/隐私下线",
     test_review_lock_918_unlink_privacy_charge_target),
    ("F78 在线地图更新失败 → MAP-002",
     test_review_lock_map_update_failed_not_fota),
    ("F79 Hello MG 只唤起 USB/AA → FICM-VOICE",
     test_review_lock_hello_mg_usb_aa_not_cpaa_or_vin),
    ("F80 网络不可用 toast → STATUS-003",
     test_review_lock_network_unavailable_not_bind008),
    ("F109 10-26 审稿错行",
     test_review1026_wrong_template_family),
]


def main() -> int:
    print("=" * 70)
    print("xlsx Scenario Routing Tests (M2.5.5)")
    print("=" * 70)
    passed = 0
    total = len(TESTS)
    for label, fn in TESTS:
        try:
            fn()
            passed += 1
            print(f"  ✓ {label}")
        except Exception as e:
            print(f"  ✗ {label}")
            print(f"      {type(e).__name__}: {e}")
    print("=" * 70)
    print(f"{passed}/{total} cases passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())